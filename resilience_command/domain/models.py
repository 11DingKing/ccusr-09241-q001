"""领域模型：上报、事实、需求、分配、方案与决策。

所有时间统一为 UTC aware datetime，序列化为带 ``Z`` 的 ISO-8601 字符串，
保证事件日志与快照在任何时区下逐字节一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .exceptions import ValidationError

# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------


def parse_time(text: str) -> datetime:
    """解析 ISO-8601 时间，要求显式时区；返回值统一为 UTC。"""
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("INVALID_TIME", f"时间格式非法: {text!r}")
    raw = text.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("INVALID_TIME", f"时间格式非法: {text!r}") from exc
    if dt.tzinfo is None:
        raise ValidationError("INVALID_TIME", f"时间必须携带时区: {text!r}")
    return dt.astimezone(timezone.utc)


def format_time(dt: datetime) -> str:
    """序列化为 UTC ISO-8601（``Z`` 后缀），是 ``parse_time`` 的逆运算。"""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# 区域类别与优先级
# ---------------------------------------------------------------------------

CATEGORY_HOSPITAL = "HOSPITAL"
CATEGORY_SHELTER = "SHELTER"
CATEGORY_ORDINARY = "ORDINARY"

#: 数字越小优先级越高，是排序与抢占约束的唯一依据。
CATEGORY_RANK = {CATEGORY_HOSPITAL: 0, CATEGORY_SHELTER: 1, CATEGORY_ORDINARY: 2}

CATEGORY_ALIASES = {
    "医院": CATEGORY_HOSPITAL,
    "避难所": CATEGORY_SHELTER,
    "避难点": CATEGORY_SHELTER,
    "普通区域": CATEGORY_ORDINARY,
    "普通": CATEGORY_ORDINARY,
    CATEGORY_HOSPITAL: CATEGORY_HOSPITAL,
    CATEGORY_SHELTER: CATEGORY_SHELTER,
    CATEGORY_ORDINARY: CATEGORY_ORDINARY,
}

CATEGORY_LABELS = {
    CATEGORY_HOSPITAL: "医院",
    CATEGORY_SHELTER: "避难点",
    CATEGORY_ORDINARY: "普通区域",
}


def normalize_category(raw: Any) -> str:
    if isinstance(raw, str) and raw in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[raw]
    raise ValidationError(
        "INVALID_CATEGORY", f"区域类别非法: {raw!r}，应为 医院/避难点/普通区域"
    )


# ---------------------------------------------------------------------------
# 上报（外部输入）
# ---------------------------------------------------------------------------

KIND_STATION = "station_status"  # 基站退服/恢复
KIND_FIBER = "fiber_status"  # 光缆中断/恢复
KIND_SATELLITE = "satellite_window"  # 卫星链路可用窗口
KIND_PORTABLE = "portable_inventory"  # 便携站库存
KIND_TEAM = "repair_team"  # 抢修队位置/状态

REPORT_KINDS = (KIND_STATION, KIND_FIBER, KIND_SATELLITE, KIND_PORTABLE, KIND_TEAM)

STATUS_OUTAGE = "OUTAGE"
STATUS_RESTORED = "RESTORED"
STATUS_CUT = "CUT"
TEAM_AVAILABLE = "AVAILABLE"
TEAM_BUSY = "BUSY"


@dataclass(frozen=True)
class Report:
    """一条被接受的外部上报。``event_id`` 是上报方提供的幂等键。"""

    event_id: str
    kind: str
    source_key: str
    occurred_at: datetime
    payload: dict[str, Any]

    def order_key(self) -> tuple[datetime, str]:
        """同一来源多份上报的规范次序：事件时间优先，事件号兜底。"""
        return (self.occurred_at, self.event_id)

    def to_record(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "source_key": self.source_key,
            "occurred_at": format_time(self.occurred_at),
            "payload": _payload_to_json(self.kind, self.payload),
        }

    @staticmethod
    def from_record(data: dict[str, Any]) -> "Report":
        return Report(
            event_id=data["event_id"],
            kind=data["kind"],
            source_key=data["source_key"],
            occurred_at=parse_time(data["occurred_at"]),
            payload=_payload_from_json(data["kind"], dict(data["payload"])),
        )


def _payload_to_json(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """payload 中的 datetime 转为 ISO 字符串（目前仅卫星窗口含时间）。"""
    if kind == KIND_SATELLITE:
        return {
            **payload,
            "window_start": format_time(payload["window_start"]),
            "window_end": format_time(payload["window_end"]),
        }
    return dict(payload)


def _payload_from_json(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    if kind == KIND_SATELLITE:
        return {
            **payload,
            "window_start": parse_time(payload["window_start"]),
            "window_end": parse_time(payload["window_end"]),
        }
    return payload


def _require(data: dict[str, Any], key: str, kind: str) -> Any:
    value = data.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValidationError("MISSING_FIELD", f"{kind} 缺少必填字段 {key}")
    return value


def _require_int(data: dict[str, Any], key: str, kind: str, minimum: int) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValidationError(
            "INVALID_FIELD", f"{kind} 字段 {key} 应为不小于 {minimum} 的整数: {value!r}"
        )
    return value


def _parse_area(raw: Any, kind: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("MISSING_FIELD", f"{kind} 缺少 area 对象")
    area_id = _require(raw, "area_id", kind)
    category = normalize_category(_require(raw, "category", kind))
    return {
        "area_id": str(area_id),
        "name": str(raw.get("name") or area_id),
        "category": category,
        "x": float(raw.get("x", 0.0)),
        "y": float(raw.get("y", 0.0)),
    }


def report_from_dict(data: dict[str, Any]) -> Report:
    """校验并规范化一条上报。接受平铺字段，内部整理为统一 payload。"""
    if not isinstance(data, dict):
        raise ValidationError("INVALID_REPORT", "上报必须是 JSON 对象")
    event_id = str(_require(data, "event_id", "report"))
    kind = _require(data, "kind", "report")
    if kind not in REPORT_KINDS:
        raise ValidationError(
            "INVALID_KIND", f"未知上报类型 {kind!r}，支持: {', '.join(REPORT_KINDS)}"
        )
    occurred_at = parse_time(_require(data, "occurred_at", kind))

    if kind == KIND_STATION:
        station_id = str(_require(data, "station_id", kind))
        status = _require(data, "status", kind)
        if status not in (STATUS_OUTAGE, STATUS_RESTORED):
            raise ValidationError("INVALID_FIELD", f"基站状态非法: {status!r}")
        payload = {
            "station_id": station_id,
            "status": status,
            "area": _parse_area(data.get("area"), kind),
        }
        source_key = f"station:{station_id}"
    elif kind == KIND_FIBER:
        cable_id = str(_require(data, "cable_id", kind))
        status = _require(data, "status", kind)
        if status not in (STATUS_CUT, STATUS_RESTORED):
            raise ValidationError("INVALID_FIELD", f"光缆状态非法: {status!r}")
        payload = {
            "cable_id": cable_id,
            "status": status,
            "area": _parse_area(data.get("area"), kind),
        }
        source_key = f"fiber:{cable_id}"
    elif kind == KIND_SATELLITE:
        sat_id = str(_require(data, "sat_id", kind))
        start = parse_time(_require(data, "window_start", kind))
        end = parse_time(_require(data, "window_end", kind))
        if not start < end:
            raise ValidationError("INVALID_FIELD", "卫星窗口开始必须早于结束")
        capacity = _require_int(data, "capacity_mbps", kind, 1)
        payload = {
            "sat_id": sat_id,
            "window_start": start,
            "window_end": end,
            "capacity_mbps": capacity,
        }
        source_key = f"satwin:{sat_id}"
    elif kind == KIND_PORTABLE:
        depot_id = str(_require(data, "depot_id", kind))
        payload = {
            "depot_id": depot_id,
            "available": _require_int(data, "available", kind, 0),
            "station_mbps": _require_int(data, "station_mbps", kind, 1),
        }
        source_key = f"depot:{depot_id}"
    else:  # KIND_TEAM
        team_id = str(_require(data, "team_id", kind))
        status = _require(data, "status", kind)
        if status not in (TEAM_AVAILABLE, TEAM_BUSY):
            raise ValidationError("INVALID_FIELD", f"抢修队状态非法: {status!r}")
        payload = {
            "team_id": team_id,
            "status": status,
            "x": float(data.get("x", 0.0)),
            "y": float(data.get("y", 0.0)),
        }
        source_key = f"team:{team_id}"

    return Report(
        event_id=event_id,
        kind=kind,
        source_key=source_key,
        occurred_at=occurred_at,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# 事实（由上报折叠出的当前世界状态）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Facts:
    """每个来源键的最新一份上报，按类别分组。与上报到达顺序无关。"""

    stations: dict[str, Report]
    fibers: dict[str, Report]
    windows: dict[str, Report]
    depots: dict[str, Report]
    teams: dict[str, Report]

    def all_reports(self) -> list[Report]:
        out: list[Report] = []
        for group in (self.stations, self.fibers, self.windows, self.depots, self.teams):
            out.extend(group.values())
        return sorted(out, key=lambda r: r.source_key)


def build_facts(reports: Iterable[Report]) -> Facts:
    """折叠上报：同一 source_key 取 (occurred_at, event_id) 最大者。

    这是乱序与迟到上报收敛到一致结果的关键：事实只取决于上报集合，
    不取决于到达顺序；迟到的旧状态上报自然被新状态覆盖。
    """
    latest: dict[str, Report] = {}
    for report in reports:
        current = latest.get(report.source_key)
        if current is None or report.order_key() > current.order_key():
            latest[report.source_key] = report
    groups: dict[str, dict[str, Report]] = {kind: {} for kind in REPORT_KINDS}
    for report in latest.values():
        groups[report.kind][report.source_key] = report
    return Facts(
        stations=groups[KIND_STATION],
        fibers=groups[KIND_FIBER],
        windows=groups[KIND_SATELLITE],
        depots=groups[KIND_PORTABLE],
        teams=groups[KIND_TEAM],
    )


def input_versions_of(facts: Facts) -> tuple[dict[str, str], ...]:
    """决策审计所需的输入版本清单：每个来源当前采纳的是哪一份上报。"""
    return tuple(
        {
            "source_key": r.source_key,
            "event_id": r.event_id,
            "occurred_at": format_time(r.occurred_at),
        }
        for r in facts.all_reports()
    )


@dataclass(frozen=True)
class AreaInfo:
    area_id: str
    name: str
    category: str
    x: float
    y: float


def build_registry(reports: Iterable[Report]) -> dict[str, AreaInfo]:
    """从灾情上报中累积区域登记信息，最新上报优先。"""
    ordered = sorted(
        (r for r in reports if r.kind in (KIND_STATION, KIND_FIBER)),
        key=lambda r: r.order_key(),
    )
    registry: dict[str, AreaInfo] = {}
    for report in ordered:
        area = report.payload["area"]
        registry[area["area_id"]] = AreaInfo(
            area_id=area["area_id"],
            name=area["name"],
            category=area["category"],
            x=area["x"],
            y=area["y"],
        )
    return registry


# ---------------------------------------------------------------------------
# 需求与资源分配
# ---------------------------------------------------------------------------

ALLOC_SATELLITE = "SATELLITE"
ALLOC_PORTABLE = "PORTABLE"
ALLOC_REPAIR = "REPAIR_TEAM"


@dataclass(frozen=True)
class Demand:
    area_id: str
    category: str
    required_mbps: int
    needs_repair: bool
    order_key: tuple[datetime, str]
    sources: tuple[str, ...]


@dataclass(frozen=True)
class Allocation:
    """一份资源指派。可哈希，分配集合相等即视为方案未变。"""

    area_id: str
    kind: str
    resource_ref: str
    capacity_mbps: int
    units: int = 1
    window_start: datetime | None = None
    window_end: datetime | None = None


@dataclass(frozen=True)
class AreaPlan:
    """规划器对单个区域的输出。"""

    area_id: str
    demand: Demand
    allocations: tuple[Allocation, ...]
    unmet_mbps: int
    rules: tuple[str, ...]


# ---------------------------------------------------------------------------
# 方案与决策（持久化于事件日志）
# ---------------------------------------------------------------------------

PLAN_ACTIVE = "ACTIVE"
PLAN_FROZEN = "FROZEN"
PLAN_SUPERSEDED = "SUPERSEDED"
PLAN_WITHDRAWN = "WITHDRAWN"
PLAN_COMPLETED = "COMPLETED"

PLAN_STATUSES = (PLAN_ACTIVE, PLAN_FROZEN, PLAN_SUPERSEDED, PLAN_WITHDRAWN, PLAN_COMPLETED)

#: 方案处于这些状态时，其行动视为"未完成"，重启后需要恢复。
UNFINISHED_STATUSES = (PLAN_ACTIVE, PLAN_FROZEN)


def allocation_to_action(plan_id: str, index: int, alloc: Allocation) -> dict[str, Any]:
    return {
        "action_id": f"{plan_id}/A{index}",
        "kind": alloc.kind,
        "resource_ref": alloc.resource_ref,
        "capacity_mbps": alloc.capacity_mbps,
        "units": alloc.units,
        "window_start": format_time(alloc.window_start) if alloc.window_start else None,
        "window_end": format_time(alloc.window_end) if alloc.window_end else None,
    }


def allocation_from_action(action: dict[str, Any], area_id: str) -> Allocation:
    return Allocation(
        area_id=area_id,
        kind=action["kind"],
        resource_ref=action["resource_ref"],
        capacity_mbps=action["capacity_mbps"],
        units=action.get("units", 1),
        window_start=parse_time(action["window_start"]) if action.get("window_start") else None,
        window_end=parse_time(action["window_end"]) if action.get("window_end") else None,
    )


@dataclass
class Plan:
    plan_id: str
    area_id: str
    status: str
    operator: str
    created_at: datetime
    rules_hit: tuple[str, ...]
    input_versions: tuple[dict[str, str], ...]
    actions: tuple[dict[str, Any], ...]
    demand_mbps: int
    unmet_mbps: int
    replaced_by: str | None = None
    supersede_reason: str | None = None

    def allocations(self) -> tuple[Allocation, ...]:
        return tuple(allocation_from_action(a, self.area_id) for a in self.actions)

    def to_record(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "area_id": self.area_id,
            "status": self.status,
            "operator": self.operator,
            "created_at": format_time(self.created_at),
            "rules_hit": list(self.rules_hit),
            "input_versions": list(self.input_versions),
            "actions": list(self.actions),
            "demand_mbps": self.demand_mbps,
            "unmet_mbps": self.unmet_mbps,
            "replaced_by": self.replaced_by,
            "supersede_reason": self.supersede_reason,
        }

    @staticmethod
    def from_record(data: dict[str, Any]) -> "Plan":
        return Plan(
            plan_id=data["plan_id"],
            area_id=data["area_id"],
            status=data["status"],
            operator=data["operator"],
            created_at=parse_time(data["created_at"]),
            rules_hit=tuple(data["rules_hit"]),
            input_versions=tuple(data["input_versions"]),
            actions=tuple(data["actions"]),
            demand_mbps=data["demand_mbps"],
            unmet_mbps=data["unmet_mbps"],
            replaced_by=data.get("replaced_by"),
            supersede_reason=data.get("supersede_reason"),
        )

    def to_dict(self) -> dict[str, Any]:
        action_status = "ACTIVE" if self.status in UNFINISHED_STATUSES else self.status
        return {
            "plan_id": self.plan_id,
            "area_id": self.area_id,
            "status": self.status,
            "operator": self.operator,
            "created_at": format_time(self.created_at),
            "rules_hit": list(self.rules_hit),
            "input_versions": list(self.input_versions),
            "actions": [dict(a, status=action_status) for a in self.actions],
            "demand_mbps": self.demand_mbps,
            "unmet_mbps": self.unmet_mbps,
            "replaced_by": self.replaced_by,
            "supersede_reason": self.supersede_reason,
        }


# 决策种类
DECISION_REPLAN = "REPLAN"
DECISION_FREEZE = "FREEZE"
DECISION_WITHDRAW = "WITHDRAW"
DECISION_RESUME_PLAN = "RESUME_PLAN"
DECISION_RESUME_AREA = "RESUME_AREA"


@dataclass
class Decision:
    """一次决策的完整审计记录：输入版本、命中规则、替代关系与操作人。"""

    decision_id: str
    seq: int
    decided_at: datetime
    kind: str
    operator: str
    area_id: str | None
    plan_id: str | None
    reason: str
    rules_hit: tuple[str, ...]
    input_versions: tuple[dict[str, str], ...]
    replaces: tuple[str, ...]
    effects: tuple[dict[str, Any], ...]
    detail: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "seq": self.seq,
            "decided_at": format_time(self.decided_at),
            "kind": self.kind,
            "operator": self.operator,
            "area_id": self.area_id,
            "plan_id": self.plan_id,
            "reason": self.reason,
            "rules_hit": list(self.rules_hit),
            "input_versions": list(self.input_versions),
            "replaces": list(self.replaces),
            "effects": list(self.effects),
            "detail": self.detail,
        }

    @staticmethod
    def from_record(data: dict[str, Any]) -> "Decision":
        return Decision(
            decision_id=data["decision_id"],
            seq=data["seq"],
            decided_at=parse_time(data["decided_at"]),
            kind=data["kind"],
            operator=data["operator"],
            area_id=data.get("area_id"),
            plan_id=data.get("plan_id"),
            reason=data["reason"],
            rules_hit=tuple(data["rules_hit"]),
            input_versions=tuple(data["input_versions"]),
            replaces=tuple(data["replaces"]),
            effects=tuple(data["effects"]),
            detail=dict(data.get("detail") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return self.to_record()
