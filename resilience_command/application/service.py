"""指挥服务：接收上报、驱动重规划、执行值班员操作，并把一切写入事件日志。

状态恢复方式：服务启动时重放事件日志（REPORT + DECISION 记录）折叠出
内存状态，因此重启后未完成行动（ACTIVE/FROZEN 方案下的行动）原样恢复。
"""

from __future__ import annotations

import threading
from typing import Any, Iterable

from ..domain.exceptions import ConflictError, NotFoundError
from ..domain.models import (
    ALLOC_PORTABLE,
    ALLOC_REPAIR,
    ALLOC_SATELLITE,
    CATEGORY_RANK,
    DECISION_FREEZE,
    DECISION_REPLAN,
    DECISION_RESUME_AREA,
    DECISION_RESUME_PLAN,
    DECISION_WITHDRAW,
    PLAN_ACTIVE,
    PLAN_COMPLETED,
    PLAN_FROZEN,
    PLAN_SUPERSEDED,
    PLAN_WITHDRAWN,
    TEAM_AVAILABLE,
    UNFINISHED_STATUSES,
    Allocation,
    Decision,
    Plan,
    Report,
    allocation_to_action,
    build_facts,
    build_registry,
    format_time,
    input_versions_of,
    report_from_dict,
)
from ..domain.planner import plan_all
from ..domain.rules import (
    RULE_FROZEN,
    RULE_HOLD,
    RULE_PREEMPT,
    RULE_RESTORE,
    RULE_RESUME,
    RuleConfig,
)
from .ports import Clock, EventLog, IdGenerator

# 重排原因（写入决策，供追溯）
REASON_NEW_DEMAND = "NEW_DEMAND"  # 新灾情出现
REASON_PREEMPTED = "PREEMPTED"  # 被更高优先级需求抢占
REASON_DEGRADED = "RESOURCE_DEGRADED"  # 链路退化/资源失效后的重排
REASON_REBALANCED = "REBALANCED"  # 资源池变化引起的再平衡
REASON_SCALE_UP = "SCALE_UP"  # 需求或供给增加后的扩容
REASON_RESCHEDULED = "RESCHEDULED"  # 等量资源换源
REASON_RESOLVED = "DEMAND_RESOLVED"  # 灾情恢复，方案闭环
REASON_RESUMED = "RESUMED"  # 区域接续后重新编排
REASON_HOLD = "OPERATOR_HOLD"  # 区域被挂起

SYSTEM_OPERATOR = "system"


class CommandService:
    """灾后通信恢复指挥的用例入口。所有公共方法都可安全并发调用。"""

    def __init__(
        self,
        store: EventLog,
        clock: Clock,
        ids: IdGenerator,
        auth: Any,
        config: RuleConfig | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids
        self._auth = auth
        self._config = config or RuleConfig()
        self._lock = threading.RLock()
        self._reports: dict[str, Report] = {}
        self._plans: dict[str, Plan] = {}
        self._holds: set[str] = set()
        self._decisions: list[Decision] = []
        for record in store.records():
            self._apply_record(record)

    # ------------------------------------------------------------------
    # 事件日志折叠
    # ------------------------------------------------------------------

    def _apply_record(self, record: dict[str, Any]) -> None:
        rtype = record.get("type")
        payload = record.get("payload") or {}
        if rtype == "REPORT":
            report = Report.from_record(payload)
            self._reports[report.event_id] = report
        elif rtype == "DECISION":
            decision = Decision.from_record(payload)
            self._decisions.append(decision)
            self._ids.observe(decision.decision_id)
            self._apply_effects(decision.effects)

    def _apply_effects(self, effects: Iterable[dict[str, Any]]) -> None:
        for effect in effects:
            etype = effect.get("type")
            if etype == "PLAN_CREATED":
                plan = Plan.from_record(effect["plan"])
                self._plans[plan.plan_id] = plan
                self._ids.observe(plan.plan_id)
            elif etype == "PLAN_STATUS":
                plan = self._plans[effect["plan_id"]]
                plan.status = effect["status"]
                plan.replaced_by = effect.get("replaced_by")
                plan.supersede_reason = effect.get("reason")
            elif etype == "HOLD_SET":
                self._holds.add(effect["area_id"])
            elif etype == "HOLD_CLEARED":
                self._holds.discard(effect["area_id"])

    def _append(self, rtype: str, payload: dict[str, Any]) -> int:
        return self._store.append(
            {
                "type": rtype,
                "recorded_at": format_time(self._clock.now()),
                "payload": payload,
            }
        )

    # ------------------------------------------------------------------
    # 上报接收
    # ------------------------------------------------------------------

    def ingest_report(self, data: dict[str, Any]) -> dict[str, Any]:
        """接收一条上报。重复 event_id 幂等忽略；迟到上报按事件时间归位。"""
        report = report_from_dict(data)
        with self._lock:
            if report.event_id in self._reports:
                return {"accepted": False, "duplicate": True, "decisions": []}
            self._reports[report.event_id] = report
            self._append("REPORT", report.to_record())
            decisions = self._replan()
            return {
                "accepted": True,
                "duplicate": False,
                "decisions": [d.to_dict() for d in decisions],
            }

    # ------------------------------------------------------------------
    # 值班员操作（需要授权令牌）
    # ------------------------------------------------------------------

    def freeze_plan(self, plan_id: str, token: str) -> dict[str, Any]:
        """冻结方案：其资源被锁定，自动重排不得抢占。"""
        operator = self._auth.authorize(token, "freeze")
        with self._lock:
            plan = self._require_plan(plan_id)
            if plan.status != PLAN_ACTIVE:
                raise ConflictError(
                    "INVALID_STATE", f"仅 ACTIVE 方案可冻结，当前为 {plan.status}"
                )
            decision = self._record_decision(
                kind=DECISION_FREEZE,
                operator=operator,
                area_id=plan.area_id,
                plan_id=plan.plan_id,
                reason="OPERATOR_FREEZE",
                rules_hit=(RULE_FROZEN,),
                replaces=(),
                effects=(
                    {
                        "type": "PLAN_STATUS",
                        "plan_id": plan.plan_id,
                        "status": PLAN_FROZEN,
                        "reason": "OPERATOR_FREEZE",
                    },
                ),
                detail={},
            )
            followups = self._replan()
            return {"decision": decision.to_dict(), "followups": [d.to_dict() for d in followups]}

    def withdraw_plan(self, plan_id: str, token: str, reason: str | None = None) -> dict[str, Any]:
        """撤回方案：方案终止、资源释放，区域挂起等待人工接续。"""
        operator = self._auth.authorize(token, "withdraw")
        with self._lock:
            plan = self._require_plan(plan_id)
            if plan.status not in (PLAN_ACTIVE, PLAN_FROZEN):
                raise ConflictError(
                    "INVALID_STATE", f"仅 ACTIVE/FROZEN 方案可撤回，当前为 {plan.status}"
                )
            decision = self._record_decision(
                kind=DECISION_WITHDRAW,
                operator=operator,
                area_id=plan.area_id,
                plan_id=plan.plan_id,
                reason="OPERATOR_WITHDRAW",
                rules_hit=(RULE_HOLD,),
                replaces=(),
                effects=(
                    {
                        "type": "PLAN_STATUS",
                        "plan_id": plan.plan_id,
                        "status": PLAN_WITHDRAWN,
                        "reason": "OPERATOR_WITHDRAW",
                    },
                    {"type": "HOLD_SET", "area_id": plan.area_id},
                ),
                detail={"note": reason or ""},
            )
            followups = self._replan()
            return {"decision": decision.to_dict(), "followups": [d.to_dict() for d in followups]}

    def resume_plan(self, plan_id: str, token: str) -> dict[str, Any]:
        """接续被冻结的方案：解除锁定，重新纳入自动编排。"""
        operator = self._auth.authorize(token, "resume")
        with self._lock:
            plan = self._require_plan(plan_id)
            if plan.status != PLAN_FROZEN:
                raise ConflictError(
                    "INVALID_STATE", f"仅 FROZEN 方案可接续，当前为 {plan.status}"
                )
            decision = self._record_decision(
                kind=DECISION_RESUME_PLAN,
                operator=operator,
                area_id=plan.area_id,
                plan_id=plan.plan_id,
                reason="OPERATOR_RESUME",
                rules_hit=(RULE_RESUME,),
                replaces=(),
                effects=(
                    {
                        "type": "PLAN_STATUS",
                        "plan_id": plan.plan_id,
                        "status": PLAN_ACTIVE,
                        "reason": "OPERATOR_RESUME",
                    },
                ),
                detail={},
            )
            followups = self._replan()
            return {"decision": decision.to_dict(), "followups": [d.to_dict() for d in followups]}

    def resume_area(self, area_id: str, token: str) -> dict[str, Any]:
        """接续区域：解除挂起，恢复自动编排。"""
        operator = self._auth.authorize(token, "resume")
        with self._lock:
            if area_id not in self._holds:
                raise ConflictError("NOT_ON_HOLD", f"区域 {area_id} 未处于挂起状态")
            decision = self._record_decision(
                kind=DECISION_RESUME_AREA,
                operator=operator,
                area_id=area_id,
                plan_id=None,
                reason="OPERATOR_RESUME",
                rules_hit=(RULE_RESUME,),
                replaces=(),
                effects=({"type": "HOLD_CLEARED", "area_id": area_id},),
                detail={},
            )
            followups = self._replan(reason_hint=REASON_RESUMED)
            return {"decision": decision.to_dict(), "followups": [d.to_dict() for d in followups]}

    def reevaluate(self, token: str | None = None) -> dict[str, Any]:
        """按当前时钟重新评估（例如卫星窗口已过期）。值班员可凭令牌触发。"""
        operator = SYSTEM_OPERATOR
        if token is not None:
            operator = self._auth.authorize(token, "reevaluate")
        with self._lock:
            decisions = self._replan()
            return {
                "operator": operator,
                "decisions": [d.to_dict() for d in decisions],
            }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        with self._lock:
            return self._require_plan(plan_id).to_dict()

    def list_plans(
        self, status: str | None = None, area_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            plans = sorted(self._plans.values(), key=lambda p: p.plan_id)
            if status:
                plans = [p for p in plans if p.status == status]
            if area_id:
                plans = [p for p in plans if p.area_id == area_id]
            return [p.to_dict() for p in plans]

    def list_decisions(
        self, plan_id: str | None = None, area_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            out = self._decisions
            if plan_id:
                out = [d for d in out if d.plan_id == plan_id or plan_id in d.replaces]
            if area_id:
                out = [d for d in out if d.area_id == area_id]
            return [d.to_dict() for d in out]

    def snapshot(self) -> dict[str, Any]:
        """值班大屏快照：区域需求、资源余量、未完成行动。"""
        with self._lock:
            now = self._clock.now()
            facts = build_facts(self._reports.values())
            registry = build_registry(self._reports.values())
            live = [p for p in self._plans.values() if p.status in UNFINISHED_STATUSES]
            live_allocs = [a for p in live for a in p.allocations()]

            windows = []
            for key in sorted(facts.windows):
                payload = facts.windows[key].payload
                used = sum(
                    a.capacity_mbps
                    for a in live_allocs
                    if a.kind == ALLOC_SATELLITE and a.resource_ref == payload["sat_id"]
                )
                windows.append(
                    {
                        "sat_id": payload["sat_id"],
                        "window_start": format_time(payload["window_start"]),
                        "window_end": format_time(payload["window_end"]),
                        "capacity_mbps": payload["capacity_mbps"],
                        "remaining_mbps": max(0, payload["capacity_mbps"] - used),
                        "expired": payload["window_end"] <= now,
                    }
                )
            depots = []
            for key in sorted(facts.depots):
                payload = facts.depots[key].payload
                used_units = sum(
                    a.units
                    for a in live_allocs
                    if a.kind == ALLOC_PORTABLE and a.resource_ref == payload["depot_id"]
                )
                depots.append(
                    {
                        "depot_id": payload["depot_id"],
                        "available": payload["available"],
                        "reserved": used_units,
                        "station_mbps": payload["station_mbps"],
                    }
                )
            teams = []
            assigned = {
                a.resource_ref: a.area_id for a in live_allocs if a.kind == ALLOC_REPAIR
            }
            for key in sorted(facts.teams):
                payload = facts.teams[key].payload
                teams.append(
                    {
                        "team_id": payload["team_id"],
                        "status": payload["status"],
                        "x": payload["x"],
                        "y": payload["y"],
                        "assigned_area": assigned.get(payload["team_id"]),
                    }
                )

            plans_by_area: dict[str, list[Plan]] = {}
            for plan in sorted(self._plans.values(), key=lambda p: p.plan_id):
                plans_by_area.setdefault(plan.area_id, []).append(plan)
            areas = []
            for area_id in sorted(set(registry) | set(plans_by_area)):
                info = registry.get(area_id)
                area_plans = plans_by_area.get(area_id, [])
                live_plan = next(
                    (p for p in area_plans if p.status in UNFINISHED_STATUSES), None
                )
                areas.append(
                    {
                        "area_id": area_id,
                        "name": info.name if info else area_id,
                        "category": info.category if info else "ORDINARY",
                        "on_hold": area_id in self._holds,
                        "demand_mbps": live_plan.demand_mbps if live_plan else 0,
                        "unmet_mbps": live_plan.unmet_mbps if live_plan else 0,
                        "plans": [p.plan_id for p in area_plans],
                    }
                )

            unfinished = []
            for plan in live:
                for action in plan.to_dict()["actions"]:
                    unfinished.append(
                        dict(action, plan_id=plan.plan_id, area_id=plan.area_id)
                    )
            unfinished.sort(key=lambda a: a["action_id"])

            return {
                "time": format_time(now),
                "areas": areas,
                "resources": {
                    "satellite_windows": windows,
                    "portable_depots": depots,
                    "repair_teams": teams,
                },
                "unfinished_actions": unfinished,
                "counts": {
                    "reports": len(self._reports),
                    "plans": len(self._plans),
                    "decisions": len(self._decisions),
                    "holds": len(self._holds),
                },
            }

    # ------------------------------------------------------------------
    # 内部：重规划与决策落账
    # ------------------------------------------------------------------

    def _require_plan(self, plan_id: str) -> Plan:
        plan = self._plans.get(plan_id)
        if plan is None:
            raise NotFoundError("PLAN_NOT_FOUND", f"方案不存在: {plan_id}")
        return plan

    def _record_decision(
        self,
        *,
        kind: str,
        operator: str,
        area_id: str | None,
        plan_id: str | None,
        reason: str,
        rules_hit: tuple[str, ...],
        replaces: tuple[str, ...],
        effects: tuple[dict[str, Any], ...],
        detail: dict[str, Any],
        input_versions: tuple[dict[str, str], ...] | None = None,
    ) -> Decision:
        if input_versions is None:
            input_versions = input_versions_of(build_facts(self._reports.values()))
        decision = Decision(
            decision_id=self._ids.next("DEC"),
            seq=self._store.next_seq(),
            decided_at=self._clock.now(),
            kind=kind,
            operator=operator,
            area_id=area_id,
            plan_id=plan_id,
            reason=reason,
            rules_hit=rules_hit,
            input_versions=input_versions,
            replaces=replaces,
            effects=effects,
            detail=detail,
        )
        self._append("DECISION", decision.to_record())
        self._decisions.append(decision)
        self._apply_effects(effects)
        return decision

    def _replan(self, reason_hint: str | None = None) -> list[Decision]:
        """全量重算目标分配并与现行方案比对，差异落为决策记录。"""
        now = self._clock.now()
        facts = build_facts(self._reports.values())
        registry = build_registry(self._reports.values())
        pinned: list[Allocation] = []
        for plan in self._plans.values():
            if plan.status == PLAN_FROZEN:
                pinned.extend(plan.allocations())
        targets = plan_all(
            facts, registry, tuple(pinned), frozenset(self._holds), now, self._config
        )
        versions = input_versions_of(facts)

        active_by_area: dict[str, Plan] = {}
        for plan in self._plans.values():
            if plan.status == PLAN_ACTIVE:
                active_by_area[plan.area_id] = plan

        areas = sorted(set(targets) | set(active_by_area))
        # 先算出各区域容量与资源占用变化，供抢占判定使用。
        cap_delta: dict[str, tuple[int, int, int, frozenset, frozenset]] = {}
        for area_id in areas:
            target = targets.get(area_id)
            current = active_by_area.get(area_id)
            des_allocs = target.allocations if target else ()
            cur_allocs = current.allocations() if current else ()
            des_cap = sum(a.capacity_mbps for a in des_allocs)
            cur_cap = sum(a.capacity_mbps for a in cur_allocs)
            rank = CATEGORY_RANK.get(
                (registry.get(area_id).category if registry.get(area_id) else "ORDINARY"),
                2,
            )
            cap_delta[area_id] = (
                cur_cap,
                des_cap,
                rank,
                frozenset((a.kind, a.resource_ref) for a in cur_allocs),
                frozenset((a.kind, a.resource_ref) for a in des_allocs),
            )

        decisions: list[Decision] = []
        for area_id in areas:
            target = targets.get(area_id)
            current = active_by_area.get(area_id)

            if area_id in self._holds:
                if current is not None:
                    decisions.append(
                        self._supersede_only(current, REASON_HOLD, (RULE_HOLD,), versions)
                    )
                continue

            if target is None:
                if current is not None:
                    decisions.append(
                        self._complete_plan(current, versions)
                    )
                continue

            cur_allocs = current.allocations() if current else ()
            if (
                current is not None
                and set(cur_allocs) == set(target.allocations)
                and current.unmet_mbps == target.unmet_mbps
            ):
                continue

            reason = self._classify(
                area_id,
                current,
                cur_allocs,
                target.allocations,
                facts,
                now,
                cap_delta,
                reason_hint,
                has_frozen=any(a.area_id == area_id for a in pinned),
            )
            rules = list(target.rules)
            if reason == REASON_PREEMPTED:
                rules.append(RULE_PREEMPT)
            if reason == REASON_RESUMED:
                rules.append(RULE_RESUME)

            new_plan_id = self._ids.next("PLAN")
            new_plan = Plan(
                plan_id=new_plan_id,
                area_id=area_id,
                status=PLAN_ACTIVE,
                operator=SYSTEM_OPERATOR,
                created_at=now,
                rules_hit=tuple(dict.fromkeys(rules)),
                input_versions=versions,
                actions=tuple(
                    allocation_to_action(new_plan_id, i + 1, a)
                    for i, a in enumerate(target.allocations)
                ),
                demand_mbps=target.demand.required_mbps,
                unmet_mbps=target.unmet_mbps,
            )
            effects: list[dict[str, Any]] = []
            replaces: list[str] = []
            if current is not None:
                effects.append(
                    {
                        "type": "PLAN_STATUS",
                        "plan_id": current.plan_id,
                        "status": PLAN_SUPERSEDED,
                        "replaced_by": new_plan.plan_id,
                        "reason": reason,
                    }
                )
                replaces.append(current.plan_id)
            effects.append({"type": "PLAN_CREATED", "plan": new_plan.to_record()})
            decisions.append(
                self._record_decision(
                    kind=DECISION_REPLAN,
                    operator=SYSTEM_OPERATOR,
                    area_id=area_id,
                    plan_id=new_plan.plan_id,
                    reason=reason,
                    rules_hit=new_plan.rules_hit,
                    replaces=tuple(replaces),
                    effects=tuple(effects),
                    detail={
                        "demand_mbps": target.demand.required_mbps,
                        "unmet_mbps": target.unmet_mbps,
                        "demand_sources": list(target.demand.sources),
                    },
                    input_versions=versions,
                )
            )
        return decisions

    def _supersede_only(
        self,
        plan: Plan,
        reason: str,
        rules: tuple[str, ...],
        versions: tuple[dict[str, str], ...],
    ) -> Decision:
        return self._record_decision(
            kind=DECISION_REPLAN,
            operator=SYSTEM_OPERATOR,
            area_id=plan.area_id,
            plan_id=None,
            reason=reason,
            rules_hit=rules,
            replaces=(plan.plan_id,),
            effects=(
                {
                    "type": "PLAN_STATUS",
                    "plan_id": plan.plan_id,
                    "status": PLAN_SUPERSEDED,
                    "reason": reason,
                },
            ),
            detail={},
            input_versions=versions,
        )

    def _complete_plan(
        self, plan: Plan, versions: tuple[dict[str, str], ...]
    ) -> Decision:
        return self._record_decision(
            kind=DECISION_REPLAN,
            operator=SYSTEM_OPERATOR,
            area_id=plan.area_id,
            plan_id=None,
            reason=REASON_RESOLVED,
            rules_hit=(RULE_RESTORE,),
            replaces=(plan.plan_id,),
            effects=(
                {
                    "type": "PLAN_STATUS",
                    "plan_id": plan.plan_id,
                    "status": PLAN_COMPLETED,
                    "reason": REASON_RESOLVED,
                },
            ),
            detail={},
            input_versions=versions,
        )

    def _classify(
        self,
        area_id: str,
        current: Plan | None,
        cur_allocs: tuple[Allocation, ...],
        des_allocs: tuple[Allocation, ...],
        facts: Any,
        now: Any,
        cap_delta: dict[str, tuple[int, int, int]],
        reason_hint: str | None,
        has_frozen: bool,
    ) -> str:
        """判定重排原因：资源退化 > 被高优先级抢占 > 再平衡/换源/扩容。"""
        if current is None:
            if reason_hint:
                return reason_hint
            return REASON_SCALE_UP if has_frozen else REASON_NEW_DEMAND
        cur_cap = sum(a.capacity_mbps for a in cur_allocs)
        des_cap = sum(a.capacity_mbps for a in des_allocs)
        # 既有分配依赖的资源已退化：无论新分配形态如何，都属于退化重排。
        if cur_allocs and self._degraded(cur_allocs, facts, now):
            return REASON_DEGRADED
        des_keys = {(a.kind, a.resource_ref) for a in des_allocs}
        lost = [a for a in cur_allocs if (a.kind, a.resource_ref) not in des_keys]
        if des_cap < cur_cap or lost:
            my_rank = cap_delta[area_id][2]
            for other, (o_cur, o_des, o_rank, o_cur_keys, o_des_keys) in cap_delta.items():
                if other == area_id or o_rank >= my_rank:
                    continue
                if o_des > o_cur or (o_des_keys - o_cur_keys):
                    return REASON_PREEMPTED
            if des_cap < cur_cap:
                return REASON_REBALANCED
            return REASON_RESCHEDULED
        if des_cap > cur_cap:
            return reason_hint or REASON_SCALE_UP
        return REASON_RESCHEDULED

    @classmethod
    def _degraded(cls, cur_allocs: tuple[Allocation, ...], facts: Any, now: Any) -> bool:
        """既有分配依赖的资源是否已退化：单项失效，或同类资源总量超限。"""
        if any(not cls._alloc_usable(a, facts, now) for a in cur_allocs):
            return True
        sat_need: dict[str, int] = {}
        depot_need: dict[str, int] = {}
        for alloc in cur_allocs:
            if alloc.kind == ALLOC_SATELLITE:
                sat_need[alloc.resource_ref] = (
                    sat_need.get(alloc.resource_ref, 0) + alloc.capacity_mbps
                )
            elif alloc.kind == ALLOC_PORTABLE:
                depot_need[alloc.resource_ref] = (
                    depot_need.get(alloc.resource_ref, 0) + alloc.units
                )
        for sat_id, total in sat_need.items():
            report = facts.windows.get(f"satwin:{sat_id}")
            if (
                report is None
                or report.payload["capacity_mbps"] < total
                or report.payload["window_end"] <= now
            ):
                return True
        for depot_id, units in depot_need.items():
            report = facts.depots.get(f"depot:{depot_id}")
            if report is None or report.payload["available"] < units:
                return True
        return False

    @staticmethod
    def _alloc_usable(alloc: Allocation, facts: Any, now: Any) -> bool:
        """判断既有分配所依赖的资源是否仍然可用（链路退化检测）。"""
        if alloc.kind == ALLOC_SATELLITE:
            report = facts.windows.get(f"satwin:{alloc.resource_ref}")
            if report is None:
                return False
            payload = report.payload
            return (
                payload["window_end"] > now
                and payload["capacity_mbps"] >= alloc.capacity_mbps
                and payload["window_start"] <= (alloc.window_start or payload["window_start"])
                and payload["window_end"] >= (alloc.window_end or payload["window_end"])
            )
        if alloc.kind == ALLOC_REPAIR:
            report = facts.teams.get(f"team:{alloc.resource_ref}")
            return report is not None and report.payload["status"] == TEAM_AVAILABLE
        report = facts.depots.get(f"depot:{alloc.resource_ref}")
        return report is not None and report.payload["available"] > 0
