"""保障规则与有约束抢占/重排求解。

纯函数式规则层：输入是从事件折叠出的态势、资源台账与当前已承诺
（ACTIVE/FROZEN 方案）的占用，输出每个区域的期望分配草案、规则命中解释与
抢占记录。规则常量集中在此，保证“为什么分给这个区域”可追溯。
"""

from __future__ import annotations

import dataclasses as dc
import math
from typing import Any

from .clock import parse_iso
from .models import AreaType, Severity

# ---------------------------------------------------------------------------
# 保障规则：基础带宽、每退服站点/中断光缆增量、保障覆盖比例与抢占底线
# ---------------------------------------------------------------------------
GUARANTEE_RULES: dict[AreaType, dict[str, float]] = {
    AreaType.HOSPITAL: {
        "base_mbps": 30.0,
        "per_station_mbps": 20.0,
        "per_cable_mbps": 10.0,
        "coverage_ratio": 1.0,   # 医院要求足额保障
        "floor_mbps": 30.0,      # 抢占不得使医院的有效带宽低于底线
    },
    AreaType.SHELTER: {
        "base_mbps": 10.0,
        "per_station_mbps": 10.0,
        "per_cable_mbps": 5.0,
        "coverage_ratio": 0.8,
        "floor_mbps": 10.0,
    },
    AreaType.NORMAL: {
        "base_mbps": 5.0,
        "per_station_mbps": 5.0,
        "per_cable_mbps": 2.0,
        "coverage_ratio": 0.5,
        "floor_mbps": 0.0,       # 普通区域无抢占保护
    },
}

STATION_MBPS = 10.0  # 单个便携站提供的回传能力


@dc.dataclass
class Situation:
    """单个区域在某时刻的折叠态势。"""

    area: str
    area_type: AreaType
    severity: Severity
    population: int
    coords: tuple[float, float] | None
    offline_stations: set[str]
    cut_cables: set[str]
    factor: float                       # 本区域链路退化系数（1 为正常）
    demand_mbps: float
    target_mbps: float                  # 按覆盖比例应保障的有效带宽
    floor_mbps: float
    latest_occurred_at: str
    basis: set[str] = dc.field(default_factory=set)

    @property
    def needs_repair(self) -> bool:
        return bool(self.offline_stations or self.cut_cables)

    def priority_components(self) -> dict[str, int]:
        type_score = {AreaType.HOSPITAL: 1000, AreaType.SHELTER: 500, AreaType.NORMAL: 100}[self.area_type]
        severity_score = {
            Severity.CRITICAL: 400,
            Severity.HIGH: 250,
            Severity.MEDIUM: 100,
            Severity.LOW: 30,
        }[self.severity]
        population_score = min(self.population // 100, 200)
        return {
            "area_type": type_score,
            "severity": severity_score,
            "population": population_score,
            "total": type_score + severity_score + population_score,
        }


@dc.dataclass
class Inventory:
    windows: dict[str, dict[str, Any]]
    total_stock: int
    teams: dict[str, dict[str, Any]]
    global_factor: float
    raw_satellite_capacity: float = 0.0


@dc.dataclass
class Committed:
    """当前已授权方案对资源的占用快照。"""

    satellite: dict[str, list[dict[str, Any]]]   # area -> [{action_id, mbps}]
    stations: dict[str, list[dict[str, Any]]]    # area -> [{action_id, qty}]
    teams: dict[str, str]                         # area -> "action_id|team_id"
    frozen_areas: set[str] = dc.field(default_factory=set)

    @classmethod
    def empty(cls) -> "Committed":
        return cls(satellite={}, stations={}, teams={}, frozen_areas=set())


@dc.dataclass
class Reclaim:
    """一次抢占记录：受益区域从受害区域动作处收回资源。"""

    victim_area: str
    victim_action_id: str
    beneficiary_area: str
    kind: str                # satellite / station / team
    amount: float


@dc.dataclass
class Draft:
    area: str
    basis_event_ids: list[str]
    rule_hits: list[dict[str, Any]]
    satellite_grant: int
    station_qty: int
    team_id: str | None
    reclaims: list[Reclaim]


# ---------------------------------------------------------------------------
# 事件折叠
# ---------------------------------------------------------------------------
def _latest(records: list[tuple[str, dict[str, Any]]]) -> dict[str, Any] | None:
    if not records:
        return None
    return max(records, key=lambda r: (r[0], r[1].get("_seq", 0)))[1]


def fold(events: list[Any], now_iso: str) -> tuple[dict[str, Situation], Inventory]:
    """把乱序、重复、迟到的事件折叠成当前态势与资源台账。

    * 值类型上报（灾情、库存、窗口、队伍位置、退化系数）以 occurred_at 最新者
      为准（同刻以接收序号决胜）；
    * 退服基站/中断光缆按资源记录最新事件时间，恢复上报只在不早于退服上报时
      生效，避免迟到旧报把已恢复资源重新标红。
    """
    from .models import EventType

    meta: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    # 资源状态：area -> id -> (最新事件时间, 是否故障)
    station_state: dict[str, dict[str, tuple[str, bool]]] = {}
    cable_state: dict[str, dict[str, tuple[str, bool]]] = {}
    factor_latest: dict[str, tuple[str, float]] = {}
    global_factor_latest: tuple[str, float] | None = None
    latest_event: dict[str, str] = {}
    basis: dict[str, set[str]] = {}

    windows: dict[str, tuple[str, dict[str, Any]]] = {}
    stock_latest: dict[str, tuple[str, int]] = {}
    team_records: dict[str, list[tuple[str, dict[str, Any]]]] = {}

    def touch(area: str, event_id: str, occurred: str) -> None:
        basis.setdefault(area, set()).add(event_id)
        if occurred > latest_event.get(area, ""):
            latest_event[area] = occurred

    def mark_fault(table: dict[str, dict[str, tuple[str, bool]]],
                   area: str, rid: str, occurred: str, fault: bool) -> None:
        per_area = table.setdefault(area, {})
        prev = per_area.get(rid)
        if prev is None or occurred >= prev[0]:
            per_area[rid] = (occurred, fault)

    for ev in events:
        p = ev.payload
        occurred = ev.occurred_at
        if ev.type == EventType.DISASTER_REPORT:
            area = p["area"]
            rec = dict(p)
            rec["_seq"] = ev.sequence
            meta.setdefault(area, []).append((occurred, rec))
            touch(area, ev.event_id, occurred)
        elif ev.type == EventType.BASE_STATION_DOWN:
            mark_fault(station_state, p["area"], p["station_id"], occurred, True)
            touch(p["area"], ev.event_id, occurred)
        elif ev.type == EventType.CABLE_CUT:
            mark_fault(cable_state, p["area"], p["cable_id"], occurred, True)
            touch(p["area"], ev.event_id, occurred)
        elif ev.type == EventType.RECOVERY:
            area = p["area"]
            for sid in p.get("restored_station_ids", []):
                mark_fault(station_state, area, sid, occurred, False)
            for cid in p.get("restored_cable_ids", []):
                mark_fault(cable_state, area, cid, occurred, False)
            touch(area, ev.event_id, occurred)
        elif ev.type == EventType.LINK_DEGRADATION:
            value = float(p["factor"])
            if p.get("area"):
                if factor_latest.get(p["area"], ("",))[0] <= occurred:
                    factor_latest[p["area"]] = (occurred, value)
                touch(p["area"], ev.event_id, occurred)
            elif global_factor_latest is None or global_factor_latest[0] <= occurred:
                global_factor_latest = (occurred, value)
        elif ev.type == EventType.SATELLITE_WINDOW:
            win = {
                "window_id": p["window_id"],
                "start": p["start"],
                "end": p["end"],
                "capacity_mbps": float(p["capacity_mbps"]),
            }
            if p["window_id"] not in windows or windows[p["window_id"]][0] <= occurred:
                windows[p["window_id"]] = (occurred, win)
        elif ev.type == EventType.PORTABLE_STOCK:
            source = p.get("source") or ev.source
            if source not in stock_latest or stock_latest[source][0] <= occurred:
                stock_latest[source] = (occurred, int(p["quantity"]))
        elif ev.type == EventType.REPAIR_TEAM:
            team_records.setdefault(p["team_id"], []).append((occurred, p))

    def fault_ids(table: dict[str, dict[str, tuple[str, bool]]], area: str) -> set[str]:
        return {rid for rid, (_t, fault) in table.get(area, {}).items() if fault}

    situations: dict[str, Situation] = {}
    all_areas = set(meta) | set(station_state) | set(cable_state) | set(factor_latest)
    for area in sorted(all_areas):
        rec = _latest(meta.get(area, [])) or {}
        area_type = AreaType(rec.get("area_type", "normal"))
        severity = Severity(rec.get("severity", "medium"))
        population = int(rec.get("population", 0))
        coords = None
        if rec.get("lat") is not None and rec.get("lng") is not None:
            coords = (float(rec["lat"]), float(rec["lng"]))
        factor = factor_latest.get(area, ("", 1.0))[1]
        rule = GUARANTEE_RULES[area_type]
        down_set = fault_ids(station_state, area)
        cut_set = fault_ids(cable_state, area)
        demand = (
            rule["base_mbps"]
            + rule["per_station_mbps"] * len(down_set)
            + rule["per_cable_mbps"] * len(cut_set)
        )
        situations[area] = Situation(
            area=area,
            area_type=area_type,
            severity=severity,
            population=population,
            coords=coords,
            offline_stations=down_set,
            cut_cables=cut_set,
            factor=factor,
            demand_mbps=demand,
            target_mbps=demand * rule["coverage_ratio"],
            floor_mbps=rule["floor_mbps"],
            latest_occurred_at=latest_event.get(area, ""),
            basis=basis.get(area, set()),
        )

    now_dt = parse_iso(now_iso)
    active_capacity = 0.0
    final_windows: dict[str, dict[str, Any]] = {}
    for _occurred, win in windows.values():
        final_windows[win["window_id"]] = win
        if parse_iso(win["start"]) <= now_dt <= parse_iso(win["end"]):
            active_capacity += win["capacity_mbps"]

    teams: dict[str, dict[str, Any]] = {}
    for team_id, records in team_records.items():
        merged: dict[str, Any] = {"team_id": team_id, "status": "available",
                                  "area": None, "lat": None, "lng": None}
        for _occurred, p in sorted(records, key=lambda r: r[0]):
            for key in ("status", "area", "lat", "lng"):
                if key in p:
                    merged[key] = p[key]
        teams[team_id] = merged

    return situations, Inventory(
        windows=final_windows,
        total_stock=sum(qty for _occurred, qty in stock_latest.values()),
        teams=teams,
        global_factor=global_factor_latest[1] if global_factor_latest else 1.0,
        raw_satellite_capacity=active_capacity,
    )


def satellite_capacity(inventory: Inventory) -> float:
    """经全局链路退化折减后的可用卫星容量。"""
    return inventory.raw_satellite_capacity * inventory.global_factor


def active_window_ids(inventory: Inventory, now: Any) -> list[str]:
    return sorted(
        win["window_id"] for win in inventory.windows.values()
        if parse_iso(win["start"]) <= now <= parse_iso(win["end"])
    )


# ---------------------------------------------------------------------------
# 求解
# ---------------------------------------------------------------------------
def solve(situations: dict[str, Situation], inventory: Inventory, committed: Committed) -> dict[str, Draft]:
    """按优先级分配卫星窗口、便携站与抢修队，执行有约束抢占。

    不变量：
      * FROZEN 方案占用的资源受保护，不参与重排；
      * 抢占只能从低优先级区域回收，且不得把其有效保障压到底线以下；
      * 同一动作可被多个高优先级区域回收，累计回收不超过其实际占用；
      * 所有排序均带名称决胜，保证可重复重放。
    """
    ranked = sorted(
        (s for s in situations.values() if s.target_mbps > 0),
        key=lambda s: (-s.priority_components()["total"], s.area),
    )
    frozen = committed.frozen_areas
    active = [s for s in ranked if s.area not in frozen]
    index_of = {s.area: i for i, s in enumerate(active)}

    hits: dict[str, list[dict[str, Any]]] = {s.area: [] for s in active}
    reclaims: dict[str, list[Reclaim]] = {s.area: [] for s in active}

    for sit in active:
        comp = sit.priority_components()
        rule = GUARANTEE_RULES[sit.area_type]
        hits[sit.area].append({"rule": "priority", "detail": {
            "area_type": sit.area_type.value,
            "severity": sit.severity.value,
            "population": sit.population,
            "components": comp,
        }})
        hits[sit.area].append({"rule": "guarantee_level", "detail": {
            "base_mbps": rule["base_mbps"],
            "per_station_mbps": rule["per_station_mbps"],
            "per_cable_mbps": rule["per_cable_mbps"],
            "coverage_ratio": rule["coverage_ratio"],
            "floor_mbps": rule["floor_mbps"],
            "offline_stations": sorted(sit.offline_stations),
            "cut_cables": sorted(sit.cut_cables),
            "demand_mbps": sit.demand_mbps,
            "target_mbps": sit.target_mbps,
            "link_factor": sit.factor,
        }})

    # ---- 阶段 1：卫星窗口容量（名义 Mbps）-----------------------------------
    # 卫星动作以窗口名义 Mbps 记账；区域获得的有效带宽 = 名义 × 全局退化系数。
    # 退化只会减少有效交付，不会放大窗口的名义容量，故容量池仍按原始容量计。
    gfactor = inventory.global_factor
    frozen_cap = sum(m["mbps"] for a in frozen for m in committed.satellite.get(a, []))
    pool = max(0.0, inventory.raw_satellite_capacity - frozen_cap)

    assignment: dict[str, float] = {}
    holders: dict[str, list[dict[str, Any]]] = {}
    remaining_by_action: dict[str, float] = {}
    for sit in active:
        acts = [dict(m) for m in committed.satellite.get(sit.area, [])]
        holders[sit.area] = acts
        committed_nominal = sum(m["mbps"] for m in acts)
        assignment[sit.area] = committed_nominal
        pool -= committed_nominal
        for m in acts:
            remaining_by_action[m["action_id"]] = float(m["mbps"])
    pool = max(0.0, pool)
    blocked_hits: dict[str, list[str]] = {s.area: [] for s in active}

    def lower_areas(area: str) -> list[Situation]:
        return active[index_of[area] + 1:]

    for sit in active:
        area = sit.area
        # 需要的名义 Mbps：目标有效带宽按全局退化系数放大
        desired = math.ceil(sit.target_mbps / gfactor)
        if desired <= assignment[area] + 1e-9:
            continue
        need = desired - assignment[area]
        give = min(need, pool)
        assignment[area] += give
        pool -= give
        need -= give
        for victim in lower_areas(area):
            if need <= 1e-9:
                break
            # 受害方有效保障底线换算成名义 Mbps
            floor_nominal = victim.floor_mbps / gfactor
            reclaimable = sum(
                remaining_by_action.get(m["action_id"], 0.0) for m in holders[victim.area]
            )
            reclaimable = min(reclaimable, max(0.0, assignment[victim.area] - floor_nominal))
            if reclaimable <= 1e-9:
                if assignment[victim.area] > floor_nominal + 1e-9 or \
                        any(remaining_by_action.get(m["action_id"], 0) > 0 for m in holders[victim.area]):
                    blocked_hits[area].append(victim.area)
                continue
            for m in sorted(holders[victim.area], key=lambda x: x["action_id"], reverse=True):
                if need <= 1e-9 or reclaimable <= 1e-9:
                    break
                avail = remaining_by_action.get(m["action_id"], 0.0)
                take = min(avail, reclaimable, need)
                if take <= 1e-9:
                    continue
                remaining_by_action[m["action_id"]] = avail - take
                rec = Reclaim(victim.area, m["action_id"], area, "satellite", take)
                reclaims[victim.area].append(rec)
                reclaims[area].append(rec)
                assignment[victim.area] -= take
                assignment[area] += take
                reclaimable -= take
                need -= take
        for victim_area in blocked_hits[area]:
            hits[area].append({"rule": "preemption_blocked_by_floor", "detail": {
                "victim_area": victim_area,
                "floor_mbps": situations[victim_area].floor_mbps}})

    # ---- 阶段 2：便携站补有效带宽缺口 ----------------------------------------
    # 便携站自带链路，提供 STATION_MBPS 有效带宽，不受卫星窗口退化折减。
    frozen_stations = sum(m["qty"] for a in frozen for m in committed.stations.get(a, []))
    stock_left = max(0, inventory.total_stock - frozen_stations)
    station_assign: dict[str, int] = {}
    station_holders: dict[str, list[dict[str, Any]]] = {}
    station_remaining: dict[str, int] = {}
    for sit in active:
        acts = [dict(m) for m in committed.stations.get(sit.area, [])]
        station_holders[sit.area] = acts
        qty = sum(int(m["qty"]) for m in acts)
        station_assign[sit.area] = qty
        stock_left -= qty
        for m in acts:
            station_remaining[m["action_id"]] = int(m["qty"])
    stock_left = max(0, stock_left)

    def effective_bandwidth(area: str) -> float:
        return assignment[area] * gfactor + station_assign[area] * STATION_MBPS

    for sit in active:
        area = sit.area
        sat_eff = assignment[area] * gfactor
        residual = max(0.0, sit.target_mbps - sat_eff - station_assign[area] * STATION_MBPS)
        desired_qty = math.ceil(residual / STATION_MBPS)
        if desired_qty <= 0:
            continue
        need_qty = desired_qty
        give = min(need_qty, stock_left)
        station_assign[area] += give
        stock_left -= give
        need_qty -= give
        for victim in lower_areas(area):
            if need_qty <= 0:
                break
            # 受害方在让出便携站后，卫星+剩余便携站的有效带宽不得低于底线
            victim_sat_eff = assignment[victim.area] * gfactor
            floor_units = max(0, math.ceil(
                (victim.floor_mbps - victim_sat_eff) / STATION_MBPS - 1e-9))
            reclaimable = max(0, station_assign[victim.area] - floor_units)
            for m in sorted(station_holders[victim.area], key=lambda x: x["action_id"], reverse=True):
                if need_qty <= 0 or reclaimable <= 0:
                    break
                avail = station_remaining.get(m["action_id"], 0)
                take = min(avail, reclaimable, need_qty)
                if take <= 0:
                    continue
                station_remaining[m["action_id"]] = avail - take
                rec = Reclaim(victim.area, m["action_id"], area, "station", float(take))
                reclaims[victim.area].append(rec)
                reclaims[area].append(rec)
                station_assign[victim.area] -= take
                station_assign[area] += take
                reclaimable -= take
                need_qty -= take
        if effective_bandwidth(area) + 1e-9 < sit.target_mbps:
            hits[area].append({"rule": "capacity_shortfall", "detail": {
                "resource": "backhaul",
                "shortfall_mbps": sit.target_mbps - effective_bandwidth(area)}})

    # ---- 阶段 3：抢修队派遣 ---------------------------------------------------
    busy: dict[str, str] = {}       # team_id -> area（含冻结区域，不可改派）
    for sit in ranked:
        enc = committed.teams.get(sit.area)
        if enc:
            _, team_id = enc.split("|", 1)
            busy[team_id] = sit.area
    dispatch: dict[str, str] = {}
    for sit in active:
        if not sit.needs_repair:
            continue
        enc = committed.teams.get(sit.area)
        current_team = enc.split("|", 1)[1] if enc else None
        if current_team and busy.get(current_team) == sit.area:
            dispatch[sit.area] = current_team
            continue
        candidates = [
            t for t in inventory.teams.values()
            if t.get("status", "available") == "available" and t["team_id"] not in busy
        ]
        chosen: str | None = None
        if candidates:
            chosen = sorted(candidates, key=lambda t: (_distance(t, sit), t["team_id"]))[0]["team_id"]
        else:
            lower = [s for s in lower_areas(sit.area)
                     if s.area in busy.values() and s.area not in frozen]
            victim_areas = sorted(
                (s for s in lower if s.priority_components()["total"] < sit.priority_components()["total"]),
                key=lambda s: (s.priority_components()["total"], s.area),
            )
            if victim_areas:
                victim_area = victim_areas[0].area
                enc_v = committed.teams.get(victim_area)
                if enc_v:
                    vact, vteam = enc_v.split("|", 1)
                    rec = Reclaim(victim_area, vact, sit.area, "team", 1.0)
                    reclaims[victim_area].append(rec)
                    reclaims[sit.area].append(rec)
                    busy.pop(vteam, None)
                    chosen = vteam
            if chosen is None:
                hits[sit.area].append({"rule": "preemption_blocked_frozen_or_busy",
                                       "detail": {"resource": "team"}})
        if chosen:
            dispatch[sit.area] = chosen
            busy[chosen] = sit.area
            if not current_team or current_team != chosen:
                hits[sit.area].append({"rule": "team_dispatch",
                                       "detail": {"team_id": chosen,
                                                  "reason": "nearest_or_reassigned"}})

    drafts: dict[str, Draft] = {}
    for sit in active:
        area = sit.area
        area_hits = list(hits[area])
        # 受害方解释：本区域将在受益方草案批准时让出的资源
        for rec in reclaims[area]:
            if rec.beneficiary_area != area:
                area_hits.append({"rule": "yielded_on_approval", "detail": {
                    "resource": rec.kind,
                    "amount": rec.amount,
                    "beneficiary_area": rec.beneficiary_area,
                    "victim_action_id": rec.victim_action_id,
                }})
        area_hits.append({"rule": "allocation_result", "detail": {
            "satellite_mbps": int(assignment[area]),
            "station_qty": station_assign[area],
            "team_id": dispatch.get(area),
        }})
        drafts[area] = Draft(
            area=area,
            basis_event_ids=sorted(sit.basis),
            rule_hits=area_hits,
            satellite_grant=int(assignment[area]),
            station_qty=station_assign[area],
            team_id=dispatch.get(area),
            reclaims=reclaims[area],
        )
    return drafts


def _distance(team: dict[str, Any], target: Situation) -> float:
    if target.coords is None or team.get("lat") is None or team.get("lng") is None:
        return 0.0
    return math.hypot(float(team["lat"]) - target.coords[0], float(team["lng"]) - target.coords[1])
