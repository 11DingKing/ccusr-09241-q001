"""指挥中心应用服务（事件溯源）。

语义约定：
  * 上报事件可重复、可迟到，按 occurred_at 参与态势折叠；
  * 重排只生成 PROPOSED 草案，不动用现役资源；草案批准时抢占才原子生效，
    旧版方案在同一时刻被接续替代（SUPERSEDED）；
  * FROZEN 方案的占用不参与重排，针对其动作的抢占在批准时被拒绝，
    必须重新编排；
  * 每条决策记录包含输入事件版本、规则命中、被替代/抢占关系与操作人；
  * 重启时从只追加日志按序重放，恢复全部方案与未完成行动。
"""

from __future__ import annotations

import dataclasses as dc
from typing import Any, Callable

from . import rules
from .auth import AuthorizationError, Operator
from .clock import Clock, SystemClock, to_iso
from .identifiers import IdGenerator, UuidIds
from .models import (
    Action,
    ActionState,
    ActionType,
    Event,
    EventType,
    PLAN_TRANSITIONS,
    Plan,
    PlanState,
)
from .store import EventStore, InMemoryStore

LIVE_PLAN_STATES = (PlanState.PROPOSED, PlanState.ACTIVE, PlanState.FROZEN)
# 仍占用资源、可作为重排基线的方案状态
COMMITTED_STATES = (PlanState.ACTIVE, PlanState.FROZEN)


class CommandError(RuntimeError):
    """业务规则拒绝该命令。"""


@dc.dataclass
class PendingChange:
    """待方案批准后原子生效的抢占。"""

    victim_action_id: str
    victim_area: str
    beneficiary_plan_id: str
    beneficiary_area: str
    beneficiary_action_id: str
    kind: str           # satellite / station / team
    amount: float
    victim_original: float | None = None   # 编排时受害动作的占用量，用于接续容错

    def to_dict(self) -> dict[str, Any]:
        return dc.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingChange":
        return cls(**data)


class CommandCenter:
    def __init__(
        self,
        store: EventStore | InMemoryStore | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        on_decision: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.store = store or InMemoryStore()
        self.clock = clock or SystemClock()
        self.ids = ids or UuidIds()
        self._on_decision = on_decision
        self.events: list[Event] = []
        self.plans: dict[str, Plan] = {}
        self._proposed: dict[str, str] = {}     # area -> 待批准草案
        self._current: dict[str, str] = {}      # area -> 最新现役谱系方案
        self._action_index: dict[str, tuple[str, Action]] = {}
        self._pending: dict[str, list[PendingChange]] = {}
        self._seq = 0
        self._decision_n = 0
        self._recover()

    # ============================================================== 重放恢复
    def _recover(self) -> None:
        event_ids: list[str] = []
        plan_ids: list[str] = []
        action_ids: list[str] = []
        for record in self.store.read_all():
            kind = record.get("kind")
            if kind == "input_event":
                event = self._event_from_dict(record["event"])
                self.events.append(event)
                self._seq = max(self._seq, event.sequence)
                event_ids.append(event.event_id)
            elif kind == "plan_decision":
                self._apply_decision(record["decision"])
                d = record["decision"]
                if d.get("plan_id"):
                    plan_ids.append(d["plan_id"])
                for action in d.get("new_actions", ()):
                    action_ids.append(action["action_id"])
        sync = getattr(self.ids, "sync", None)
        if callable(sync):
            sync(event_ids, plan_ids, action_ids)

    @staticmethod
    def _event_from_dict(data: dict[str, Any]) -> Event:
        return Event(
            event_id=data["event_id"],
            type=EventType(data["type"]),
            occurred_at=data["occurred_at"],
            received_at=data["received_at"],
            source=data["source"],
            payload=data["payload"],
            sequence=data["sequence"],
        )

    def _apply_decision(self, d: dict[str, Any]) -> None:
        dtype = d["type"]
        self._decision_n = max(self._decision_n, int(d["decision_id"].split("-")[1]))
        if dtype == "replan":
            plan = self._materialize_plan(d)
            self.plans[plan.plan_id] = plan
            self._register_actions(plan)
            self._pending[plan.plan_id] = [PendingChange.from_dict(c) for c in d["deferred_changes"]]
            self._proposed[plan.area] = plan.plan_id
        elif dtype == "proposal_discarded":
            old = self.plans[d["old_plan_id"]]
            anchor = d.get("anchor_action_id")
            old.state = PlanState.DISCARDED
            for action in old.actions:
                if action.state == ActionState.PLANNED:
                    action.state = ActionState.SUPERSEDED
                    action.superseded_by = anchor
            self._proposed[old.area] = d["new_plan_id"]
        elif dtype == "approve":
            self._activate_plan(d)
        elif dtype == "freeze":
            plan = self._require(d["plan_id"])
            plan.state = PlanState.FROZEN
            plan.frozen_at = d["at"]
            plan.frozen_by = d["operator"]
            plan.decision_log.append({"action": "freeze", "at": d["at"], "operator": d["operator"],
                                      "reason": d.get("reason", "")})
        elif dtype == "resume":
            plan = self._require(d["plan_id"])
            plan.state = PlanState.ACTIVE
            plan.decision_log.append({"action": "resume", "at": d["at"], "operator": d["operator"],
                                      "reason": d.get("reason", "")})
        elif dtype == "withdraw":
            plan = self._require(d["plan_id"])
            self._pending.pop(plan.plan_id, None)
            if plan.state == PlanState.PROPOSED:
                self._proposed.pop(plan.area, None)
            plan.state = PlanState.WITHDRAWN
            plan.withdrawn_at = d["at"]
            plan.withdrawn_by = d["operator"]
            for action in plan.live_actions():
                action.state = ActionState.CANCELLED
            plan.decision_log.append({"action": "withdraw", "at": d["at"], "operator": d["operator"],
                                      "reason": d.get("reason", "")})
        elif dtype == "complete":
            plan = self._require(d["plan_id"])
            plan.state = PlanState.COMPLETED
            for action in plan.live_actions():
                action.state = ActionState.COMPLETED
                action.completed_at = d["at"]
            plan.decision_log.append({"action": "complete", "at": d["at"], "operator": d["operator"],
                                      "reason": d.get("reason", "")})
        elif dtype == "action_complete":
            plan = self._require(d["plan_id"])
            action = self._find_action(d["action_id"])
            action.state = ActionState.COMPLETED
            action.completed_at = d["at"]
            action.detail = {**action.detail, "result": d.get("result")}
            if plan.actions and all(a.state == ActionState.COMPLETED for a in plan.actions):
                plan.state = PlanState.COMPLETED
        # replan_noop 仅用于审计，无状态变化

    def _materialize_plan(self, d: dict[str, Any]) -> Plan:
        actions = [
            Action(
                action_id=a["action_id"],
                type=ActionType(a["type"]),
                area=a["area"],
                detail=a["detail"],
                state=ActionState.PLANNED,
                created_version=a.get("created_version", d["new_version"]),
            )
            for a in d["new_actions"]
        ]
        return Plan(
            plan_id=d["plan_id"],
            area=d["area"],
            version=d["new_version"],
            state=PlanState.PROPOSED,
            created_at=d["at"],
            created_by=d.get("operator") or "system",
            basis_event_ids=list(d["basis_event_ids"]),
            rule_hits=list(d["rule_hits"]),
            actions=actions,
            supersedes=d.get("supersedes"),
            decision_log=[{"action": "replan", "at": d["at"], "operator": d.get("operator"),
                           "reason": d.get("reason", "")}],
        )

    def _activate_plan(self, d: dict[str, Any]) -> None:
        plan = self._require(d["plan_id"])
        at = d["at"]
        # 1) 原子落实跨区域抢占
        for change in (PendingChange.from_dict(c) for c in d["applied_changes"]):
            action = self._find_action(change.victim_action_id)
            if change.kind == "satellite":
                action.detail["mbps"] = max(0.0, float(action.detail.get("mbps", 0.0)) - change.amount)
                if action.detail["mbps"] <= 1e-9:
                    action.state = ActionState.PREEMPTED
                    action.preempted_by = change.beneficiary_action_id
                    action.preempted_at = at
            elif change.kind == "station":
                action.detail["quantity"] = max(0, int(action.detail.get("quantity", 0)) - int(change.amount))
                if action.detail["quantity"] <= 0:
                    action.state = ActionState.PREEMPTED
                    action.preempted_by = change.beneficiary_action_id
                    action.preempted_at = at
            elif change.kind == "team":
                action.state = ActionState.PREEMPTED
                action.preempted_by = change.beneficiary_action_id
                action.preempted_at = at
        # 2) 接续替代旧版方案
        if plan.supersedes and plan.supersedes in self.plans:
            old = self.plans[plan.supersedes]
            link = self._link_by_type(plan)
            for action in old.live_actions():
                action.state = ActionState.SUPERSEDED
                action.superseded_by = link.get(action.type)
            old.state = PlanState.SUPERSEDED
            old.decision_log.append({
                "action": "superseded", "at": at, "operator": d["operator"],
                "by_plan": plan.plan_id,
            })
        # 3) 新版生效
        plan.state = PlanState.ACTIVE
        for action in plan.actions:
            if action.state == ActionState.PLANNED:
                action.state = ActionState.ACTIVE
        self._pending.pop(plan.plan_id, None)
        self._proposed.pop(plan.area, None)
        self._current[plan.area] = plan.plan_id
        plan.decision_log.append({"action": "approve", "at": at, "operator": d["operator"],
                                  "reason": d.get("reason", "")})

    @staticmethod
    def _link_by_type(plan: Plan) -> dict[ActionType, str | None]:
        result: dict[ActionType, str | None] = {}
        for action in plan.actions:
            result.setdefault(action.type, action.action_id)
        return result

    def _register_actions(self, plan: Plan) -> None:
        for action in plan.actions:
            self._action_index[action.action_id] = (plan.plan_id, action)

    def _find_action(self, action_id: str) -> Action:
        if action_id not in self._action_index:
            raise CommandError(f"行动 {action_id} 不存在")
        return self._action_index[action_id][1]

    def _require(self, plan_id: str) -> Plan:
        if plan_id not in self.plans:
            raise CommandError(f"方案 {plan_id} 不存在")
        return self.plans[plan_id]

    # ============================================================== 事件接入
    def ingest(self, payload: dict[str, Any]) -> Event:
        """校验并记录一条上报；迟到/重复上报均合法。"""
        for key in ("type", "occurred_at", "payload"):
            if key not in payload:
                raise CommandError(f"上报缺少字段：{key}")
        try:
            etype = EventType(payload["type"])
        except ValueError as exc:
            raise CommandError(f"未知事件类型：{payload['type']}") from exc
        self._validate_payload(etype, payload["payload"])
        supplied_id = payload.get("event_id")
        if supplied_id:
            for existing in self.events:
                if existing.event_id == supplied_id:
                    return existing  # 幂等：同一上报重投只接收一次
        self._seq += 1
        event = Event(
            event_id=supplied_id or self.ids.new_event_id(),
            type=etype,
            occurred_at=payload["occurred_at"],
            received_at=to_iso(self.clock.now()),
            source=payload.get("source", "unknown"),
            payload=payload["payload"],
            sequence=self._seq,
        )
        self._commit_record({"kind": "input_event", "event": event.to_dict()}, apply=False)
        self.events.append(event)
        return event

    @staticmethod
    def _validate_payload(etype: EventType, p: dict[str, Any]) -> None:
        def need(*keys: str) -> None:
            for key in keys:
                if key not in p:
                    raise CommandError(f"{etype.value} 负载缺少字段：{key}")

        if etype == EventType.DISASTER_REPORT:
            need("area", "area_type")
            if p["area_type"] not in tuple(t.value for t in rules.AreaType):
                raise CommandError(f"非法区域类型：{p['area_type']}")
            if p.get("severity", "medium") not in tuple(s.value for s in rules.Severity):
                raise CommandError(f"非法灾情等级：{p.get('severity')}")
        elif etype == EventType.BASE_STATION_DOWN:
            need("area", "station_id")
        elif etype == EventType.CABLE_CUT:
            need("area", "cable_id")
        elif etype == EventType.SATELLITE_WINDOW:
            need("window_id", "start", "end", "capacity_mbps")
        elif etype == EventType.PORTABLE_STOCK:
            need("quantity")
        elif etype == EventType.REPAIR_TEAM:
            need("team_id")
        elif etype == EventType.LINK_DEGRADATION:
            need("factor")
        elif etype == EventType.RECOVERY:
            need("area")

    def ingest_many(self, payloads: list[dict[str, Any]]) -> list[Event]:
        return [self.ingest(p) for p in payloads]

    # ============================================================== 重编排
    def replan(self, operator: Operator | None = None, reason: str = "") -> list[Plan]:
        """依据全部已接收事件求解，为需要变化的非冻结区域生成新版草案。"""
        self._authorize(operator, "replan")
        now = to_iso(self.clock.now())
        situations, inventory = rules.fold(self.events, now)
        committed = self._committed_snapshot()
        drafts = rules.solve(situations, inventory, committed)
        active_windows = sorted(
            w["window_id"] for w in inventory.windows.values()
            if rules.parse_iso(w["start"]) <= self.clock.now() <= rules.parse_iso(w["end"])
        )
        created: list[Plan] = []
        for area in sorted(drafts):
            current = self._committed_plan(area)
            if current is not None and current.state == PlanState.FROZEN:
                self._audit_noop(area, current, drafts[area], operator, reason, now,
                                 extra_rule="area_frozen")
                continue
            # 无现役方案时，已有的待批草案就是比较基线，避免同分配反复生成新版本
            baseline = current or self.proposed_plan(area)
            baseline_alloc = self._plan_allocation(baseline) if baseline else None
            desired = (drafts[area].satellite_grant, drafts[area].station_qty, drafts[area].team_id)
            if baseline is not None and baseline_alloc == desired:
                self._audit_noop(area, baseline, drafts[area], operator, reason, now)
                continue
            created.append(self._create_proposal(
                area, drafts[area], current, active_windows, operator, reason, now))
        return created

    def _create_proposal(self, area, draft, current, active_windows, operator, reason, now) -> Plan:
        # 同一区域只保留一个待批准草案：旧草案标记为 DISCARDED
        old_proposal_id = self._proposed.get(area)
        base_version = current.version if current else 0
        if old_proposal_id:
            base_version = max(base_version, self.plans[old_proposal_id].version)
        version = base_version + 1

        new_actions: list[Action] = []
        sat_action = station_action = team_action = None
        if draft.satellite_grant > 0:
            sat_action = Action(
                action_id=self.ids.new_action_id(),
                type=ActionType.ALLOCATE_CAPACITY,
                area=area,
                detail={"mbps": draft.satellite_grant, "window_ids": list(active_windows)},
                created_version=version,
            )
            new_actions.append(sat_action)
        if draft.station_qty > 0:
            station_action = Action(
                action_id=self.ids.new_action_id(),
                type=ActionType.DEPLOY_STATION,
                area=area,
                detail={"quantity": draft.station_qty},
                created_version=version,
            )
            new_actions.append(station_action)
        if draft.team_id:
            team_action = Action(
                action_id=self.ids.new_action_id(),
                type=ActionType.DISPATCH_TEAM,
                area=area,
                detail={"team_id": draft.team_id},
                created_version=version,
            )
            new_actions.append(team_action)

        hits = list(draft.rule_hits)
        action_of_kind = {"satellite": sat_action and sat_action.action_id,
                          "station": station_action and station_action.action_id,
                          "team": team_action and team_action.action_id}

        # 聚合：同一受害动作可能被同一受益方的多条 reclaim 记录引用
        aggregated: dict[tuple[str, str], PendingChange] = {}
        for rec in draft.reclaims:
            if rec.beneficiary_area != area:
                continue
            beneficiary_action_id = action_of_kind[rec.kind]
            key = (rec.victim_action_id, rec.kind)
            if key in aggregated:
                aggregated[key].amount += rec.amount
                continue
            original = None
            holder = self._action_index.get(rec.victim_action_id)
            if holder is not None:
                holder_action = holder[1]
                if rec.kind == "satellite":
                    original = float(holder_action.detail.get("mbps", 0.0))
                elif rec.kind == "station":
                    original = float(holder_action.detail.get("quantity", 0))
                else:
                    original = 1.0
            aggregated[key] = PendingChange(
                victim_action_id=rec.victim_action_id,
                victim_area=rec.victim_area,
                beneficiary_plan_id="",
                beneficiary_area=area,
                beneficiary_action_id=beneficiary_action_id or "",
                kind=rec.kind,
                amount=rec.amount,
                victim_original=original,
            )
        for change in aggregated.values():
            hits.append({
                "rule": "preemption_on_approval",
                "detail": {
                    "resource": change.kind,
                    "amount": change.amount,
                    "victim_area": change.victim_area,
                    "victim_action_id": change.victim_action_id,
                    "beneficiary_action_id": change.beneficiary_action_id,
                },
            })
        if current is not None:
            old_alloc = self._plan_allocation(current)
            retained = (min(draft.satellite_grant, old_alloc[0]),
                        min(draft.station_qty, old_alloc[1]))
            if retained != (0, 0):
                hits.append({"rule": "continuation_retains",
                             "detail": {"satellite_mbps": retained[0], "station_qty": retained[1]}})

        plan_id = self.ids.new_plan_id()
        for change in aggregated.values():
            change.beneficiary_plan_id = plan_id
        plan = Plan(
            plan_id=plan_id,
            area=area,
            version=version,
            state=PlanState.PROPOSED,
            created_at=now,
            created_by=operator.name if operator else "system",
            basis_event_ids=draft.basis_event_ids,
            rule_hits=hits,
            actions=new_actions,
            supersedes=current.plan_id if current else None,
            decision_log=[{"action": "replan", "at": now,
                           "operator": operator.name if operator else None, "reason": reason}],
        )

        if old_proposal_id:
            anchor = new_actions[0].action_id if new_actions else None
            discard_record = self._decision_record("proposal_discarded", now, operator, reason, {
                "old_plan_id": old_proposal_id,
                "new_plan_id": plan_id,
                "anchor_action_id": anchor,
            })
            self._commit_record({"kind": "plan_decision", "decision": discard_record})

        record = self._decision_record("replan", now, operator, reason, {
            "plan_id": plan_id,
            "area": area,
            "new_version": version,
            "supersedes": current.plan_id if current else None,
            "basis_event_ids": list(draft.basis_event_ids),
            "rule_hits": hits,
            "new_actions": [a.to_dict() for a in new_actions],
            "deferred_changes": [c.to_dict() for c in aggregated.values()],
        })
        self._commit_record({"kind": "plan_decision", "decision": record})
        return plan

    def _audit_noop(self, area, current, draft, operator, reason, now, extra_rule: str | None = None) -> None:
        """重排未产生变化也要留痕：输入版本 + 规则结论（含容量缺口/抢占受阻）。"""
        keep = {"priority", "guarantee_level", "allocation_result", "capacity_shortfall",
                "preemption_blocked_by_floor", "preemption_blocked_frozen_or_busy"}
        hits = [h for h in draft.rule_hits if h["rule"] in keep]
        if extra_rule:
            hits = hits + [{"rule": extra_rule, "detail": {"area": area}}]
        record = self._decision_record("replan_noop", now, operator, reason, {
            "plan_id": current.plan_id if current else None,
            "area": area,
            "basis_event_ids": list(draft.basis_event_ids),
            "rule_hits": hits,
        })
        self._commit_record({"kind": "plan_decision", "decision": record})
        if current is not None:
            current.decision_log.append({"action": "replan_noop", "at": now,
                                         "operator": operator.name if operator else None, "reason": reason})

    # ============================================================== 授权操作
    def approve(self, plan_id: str, operator: Operator, reason: str = "") -> Plan:
        plan = self._require(plan_id)
        self._authorize(operator, "approve")
        if plan.state != PlanState.PROPOSED:
            raise CommandError(f"方案 {plan_id} 当前状态 {plan.state.value}，不能授权")
        # 批准前校验全部抢占仍可执行（受害方案未被冻结/撤回，资源仍在）
        applied = self._validate_pending(plan)
        now = to_iso(self.clock.now())
        record = self._decision_record("approve", now, operator, reason, {
            "plan_id": plan_id,
            "area": plan.area,
            "applied_changes": [c.to_dict() for c in applied],
            "supersedes": plan.supersedes,
        })
        self._commit_record({"kind": "plan_decision", "decision": record})
        return self.plans[plan_id]

    def _validate_pending(self, plan: Plan) -> list[PendingChange]:
        """批准前校验抢占仍可执行。

        若受害区域已先批准了自身的新版草案，受害动作会变为 SUPERSEDED；
        此时检查受害区域当前占用相对编排时是否已至少释放了同等资源，
        若是则跳过该抢占（已由受害方新版接续完成），否则要求重新编排。
        """
        kept: list[PendingChange] = []
        for change in self._pending.get(plan.plan_id, []):
            if change.victim_action_id not in self._action_index:
                raise CommandError("抢占目标已不存在，请重新编排")
            victim_plan_id, action = self._action_index[change.victim_action_id]
            victim_plan = self.plans[victim_plan_id]
            if victim_plan.state == PlanState.FROZEN:
                raise CommandError(
                    f"受害区域 {change.victim_area} 的方案 {victim_plan.plan_id} 已冻结，"
                    "抢占无法生效，请重新编排")
            if action.state == ActionState.SUPERSEDED:
                current_victim = self._committed_plan(change.victim_area)
                if current_victim is None:
                    raise CommandError("受害区域方案已撤回，请重新编排")
                cur = self._plan_allocation(current_victim)
                already_freed = False
                if change.victim_original is not None:
                    if change.kind == "satellite":
                        already_freed = cur[0] <= change.victim_original - change.amount + 1e-9
                    elif change.kind == "station":
                        already_freed = cur[1] <= int(change.victim_original) - int(change.amount)
                    else:
                        already_freed = cur[2] != action.detail.get("team_id")
                if not already_freed:
                    raise CommandError(
                        f"受害区域 {change.victim_area} 已接续新版但资源未相应释放，请重新编排")
                continue  # 资源已随受害方新版释放，抢占无需重复执行
            if action.state != ActionState.ACTIVE:
                raise CommandError(
                    f"受害动作 {change.victim_action_id} 状态为 {action.state.value}，请重新编排")
            if change.kind == "satellite" and float(action.detail.get("mbps", 0.0)) + 1e-9 < change.amount:
                raise CommandError("卫星容量在编排后发生变化，请重新编排")
            if change.kind == "station" and int(action.detail.get("quantity", 0)) < int(change.amount):
                raise CommandError("便携站数量在编排后发生变化，请重新编排")
            kept.append(change)
        return kept

    def freeze(self, plan_id: str, operator: Operator, reason: str = "") -> Plan:
        return self._lifecycle("freeze", plan_id, operator, reason)

    def resume(self, plan_id: str, operator: Operator, reason: str = "") -> Plan:
        return self._lifecycle("resume", plan_id, operator, reason)

    def withdraw(self, plan_id: str, operator: Operator, reason: str = "") -> Plan:
        return self._lifecycle("withdraw", plan_id, operator, reason)

    def complete(self, plan_id: str, operator: Operator, reason: str = "") -> Plan:
        return self._lifecycle("complete", plan_id, operator, reason)

    def _lifecycle(self, action, plan_id, operator, reason) -> Plan:
        plan = self._require(plan_id)
        self._authorize(operator, action)
        allowed = PLAN_TRANSITIONS[action]
        if plan.state not in allowed:
            raise CommandError(f"方案 {plan_id} 当前状态 {plan.state.value}，不能执行 {action}")
        now = to_iso(self.clock.now())
        record = self._decision_record(action, now, operator, reason, {"plan_id": plan_id, "area": plan.area})
        self._commit_record({"kind": "plan_decision", "decision": record})
        return self.plans[plan_id]

    def report_action_completed(self, action_id: str, operator: Operator, result: str = "") -> Action:
        self._authorize(operator, "report")
        plan_id = self._action_index.get(action_id, (None,))[0]
        plan = self._require(plan_id) if plan_id else None
        if plan is None or plan.state not in (PlanState.ACTIVE, PlanState.FROZEN):
            raise CommandError("只能回报执行中方案的行动")
        action = self._find_action(action_id)
        if action.state not in (ActionState.ACTIVE, ActionState.PLANNED):
            raise CommandError(f"行动 {action_id} 当前状态 {action.state.value}，不能回报完成")
        now = to_iso(self.clock.now())
        record = self._decision_record("action_complete", now, operator, "", {
            "plan_id": plan.plan_id,
            "action_id": action_id,
            "result": result,
        })
        self._commit_record({"kind": "plan_decision", "decision": record})
        return action

    # ============================================================== 查询
    def current_plan(self, area: str) -> Plan | None:
        plan_id = self._current.get(area)
        return self.plans.get(plan_id) if plan_id else None

    def proposed_plan(self, area: str) -> Plan | None:
        plan_id = self._proposed.get(area)
        return self.plans.get(plan_id) if plan_id else None

    def pending_actions(self) -> list[dict[str, Any]]:
        """重启恢复后仍未闭环的行动。"""
        result = []
        for area, plan_id in sorted(self._current.items()):
            plan = self.plans[plan_id]
            if plan.state in (PlanState.ACTIVE, PlanState.FROZEN):
                for action in plan.live_actions():
                    result.append({
                        "area": area,
                        "plan_id": plan.plan_id,
                        "version": plan.version,
                        "plan_state": plan.state.value,
                        "action": action.to_dict(),
                    })
        return result

    def event_log(self) -> list[dict[str, Any]]:
        return [r for r in self.store.read_all() if r.get("kind") == "input_event"]

    def decision_log(self) -> list[dict[str, Any]]:
        return [r["decision"] for r in self.store.read_all() if r.get("kind") == "plan_decision"]

    def proposals(self) -> list[Plan]:
        return [self.plans[pid] for pid in self._proposed.values()]

    def situation_view(self) -> dict[str, Any]:
        now = to_iso(self.clock.now())
        situations, inventory = rules.fold(self.events, now)
        return {
            "as_of": now,
            "areas": [
                {
                    "area": s.area,
                    "area_type": s.area_type.value,
                    "severity": s.severity.value,
                    "population": s.population,
                    "offline_stations": sorted(s.offline_stations),
                    "cut_cables": sorted(s.cut_cables),
                    "demand_mbps": s.demand_mbps,
                    "target_mbps": s.target_mbps,
                    "floor_mbps": s.floor_mbps,
                    "link_factor": s.factor,
                    "priority": s.priority_components(),
                }
                for s in sorted(situations.values(), key=lambda x: x.area)
            ],
            "resources": {
                "satellite_capacity_mbps": rules.satellite_capacity(inventory),
                "portable_station_stock": inventory.total_stock,
                "teams": [{k: v for k, v in t.items() if not k.startswith("_")}
                          for t in inventory.teams.values()],
                "global_link_factor": inventory.global_factor,
            },
        }

    # ============================================================== 辅助
    def _committed_plan(self, area: str) -> Plan | None:
        plan_id = self._current.get(area)
        plan = self.plans.get(plan_id) if plan_id else None
        if plan and plan.state in COMMITTED_STATES:
            return plan
        return None

    @staticmethod
    def _plan_allocation(plan: Plan) -> tuple[int, int, str | None]:
        mbps = qty = 0
        team_id = None
        for action in plan.live_actions():
            if action.type == ActionType.ALLOCATE_CAPACITY:
                mbps += int(action.detail.get("mbps", 0))
            elif action.type == ActionType.DEPLOY_STATION:
                qty += int(action.detail.get("quantity", 0))
            elif action.type == ActionType.DISPATCH_TEAM:
                team_id = action.detail.get("team_id")
        return mbps, qty, team_id

    def _committed_snapshot(self) -> rules.Committed:
        snap = rules.Committed.empty()
        for area, plan_id in self._current.items():
            plan = self.plans[plan_id]
            if plan.state not in COMMITTED_STATES:
                continue
            if plan.state == PlanState.FROZEN:
                snap.frozen_areas.add(area)
            for action in plan.live_actions():
                if action.type == ActionType.ALLOCATE_CAPACITY:
                    snap.satellite.setdefault(area, []).append(
                        {"action_id": action.action_id, "mbps": float(action.detail.get("mbps", 0.0))})
                elif action.type == ActionType.DEPLOY_STATION:
                    snap.stations.setdefault(area, []).append(
                        {"action_id": action.action_id, "qty": int(action.detail.get("quantity", 0))})
                elif action.type == ActionType.DISPATCH_TEAM:
                    snap.teams[area] = f"{action.action_id}|{action.detail.get('team_id')}"
        return snap

    @staticmethod
    def _authorize(operator: Operator | None, action: str) -> None:
        if operator is None:
            raise AuthorizationError(f"执行 {action} 需要授权操作人")
        if not operator.can(action):
            raise AuthorizationError(f"操作人 {operator.name}（{operator.role}）无权执行 {action}")

    def _decision_record(self, dtype: str, now: str, operator: Operator | None,
                         reason: str, body: dict[str, Any]) -> dict[str, Any]:
        record = {
            "type": dtype,
            "decision_id": f"dec-{self._decision_n + 1:04d}",
            "at": now,
            "operator": operator.name if operator else None,
            "reason": reason,
        }
        record.update(body)
        return record

    def _commit_record(self, record: dict[str, Any], apply: bool = True) -> None:
        self.store.append(record)
        if record.get("kind") == "plan_decision" and apply:
            self._apply_decision(record["decision"])
        if self._on_decision and record.get("kind") == "plan_decision":
            self._on_decision(record["decision"])
