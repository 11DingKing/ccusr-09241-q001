"""领域模型：上报事件、区域灾情、保障方案与行动。

模型只承载状态与状态机规则，不做持久化与网络交互。
"""

from __future__ import annotations

import dataclasses as dc
import enum
from typing import Any


class EventType(str, enum.Enum):
    """可重复、可能迟到的上报类型。"""

    BASE_STATION_DOWN = "base_station_down"       # 基站退服
    CABLE_CUT = "cable_cut"                       # 光缆中断
    SATELLITE_WINDOW = "satellite_window"         # 卫星链路可用窗口
    PORTABLE_STOCK = "portable_station_stock"     # 便携站库存
    REPAIR_TEAM = "repair_team_report"            # 抢修队位置/状态
    DISASTER_REPORT = "disaster_report"           # 灾情（含区域保障等级）
    LINK_DEGRADATION = "link_degradation"         # 链路退化
    RECOVERY = "recovery_report"                  # 基础设施恢复


class AreaType(str, enum.Enum):
    HOSPITAL = "hospital"       # 医院
    SHELTER = "shelter"         # 避难点
    NORMAL = "normal"           # 普通区域


class Severity(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PlanState(str, enum.Enum):
    PROPOSED = "proposed"       # 已形成、待值班员授权
    ACTIVE = "active"           # 已授权、行动执行中
    FROZEN = "frozen"           # 冻结：暂停新的抢占/重排，既有行动维持
    WITHDRAWN = "withdrawn"     # 撤回：方案作废
    SUPERSEDED = "superseded"   # 已被新版本接续替代
    DISCARDED = "discarded"     # 未批准草案被更新草案替代
    COMPLETED = "completed"     # 行动全部闭环


class ActionType(str, enum.Enum):
    ALLOCATE_CAPACITY = "allocate_capacity"  # 为区域分配回传容量
    DISPATCH_TEAM = "dispatch_team"          # 派遣抢修队
    DEPLOY_STATION = "deploy_station"        # 下放便携站


class ActionState(str, enum.Enum):
    PLANNED = "planned"
    ACTIVE = "active"
    PREEMPTED = "preempted"   # 被更高优先级需求抢占
    SUPERSEDED = "superseded"  # 被同区域新版方案的行动接续替代
    COMPLETED = "completed"
    CANCELLED = "cancelled"   # 随方案撤回而取消


# 人工操作允许的起始状态（应用服务据此执行生命周期校验）
PLAN_TRANSITIONS: dict[str, tuple["PlanState", ...]] = {
    "approve": (PlanState.PROPOSED,),
    "freeze": (PlanState.ACTIVE,),
    "resume": (PlanState.FROZEN,),
    "withdraw": (PlanState.PROPOSED, PlanState.ACTIVE, PlanState.FROZEN),
    "complete": (PlanState.ACTIVE,),
}


@dc.dataclass(frozen=True)
class Event:
    """一次上报。同一来源可重复上报；迟到与否由 occurred_at 判定。"""

    event_id: str
    type: EventType
    occurred_at: str           # 事件实际发生时间（上报方提供）
    received_at: str           # 指挥侧接收时间（时钟端口生成）
    source: str
    payload: dict[str, Any]
    sequence: int              # 接收序号，乱序重放的稳定排序键之一

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": self.type.value,
            "occurred_at": self.occurred_at,
            "received_at": self.received_at,
            "source": self.source,
            "payload": self.payload,
            "sequence": self.sequence,
        }


@dc.dataclass
class Action:
    action_id: str
    type: ActionType
    area: str
    detail: dict[str, Any]                 # 数量、窗口、队伍等参数
    state: ActionState = ActionState.PLANNED
    created_version: int = 1
    preempted_by: str | None = None        # 抢占方行动 ID
    preempted_at: str | None = None
    superseded_by: str | None = None       # 接续它的新行动 ID（同区域新版方案）
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "type": self.type.value,
            "area": self.area,
            "detail": self.detail,
            "state": self.state.value,
            "created_version": self.created_version,
            "preempted_by": self.preempted_by,
            "preempted_at": self.preempted_at,
            "completed_at": self.completed_at,
        }


@dc.dataclass
class Plan:
    """某区域的一个保障方案版本。"""

    plan_id: str
    area: str
    version: int
    state: PlanState
    created_at: str
    created_by: str                       # 触发者："system" 或操作人
    basis_event_ids: list[str]            # 输入版本：本版依据的事件
    rule_hits: list[dict[str, Any]]       # 规则命中解释
    actions: list[Action]
    supersedes: str | None = None         # 被替代的上一版 plan_id
    frozen_at: str | None = None
    frozen_by: str | None = None
    withdrawn_at: str | None = None
    withdrawn_by: str | None = None
    decision_log: list[dict[str, Any]] = dc.field(default_factory=list)

    def find_action(self, action_id: str) -> Action | None:
        for action in self.actions:
            if action.action_id == action_id:
                return action
        return None

    def live_actions(self) -> list[Action]:
        return [a for a in self.actions if a.state in (ActionState.PLANNED, ActionState.ACTIVE)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "area": self.area,
            "version": self.version,
            "state": self.state.value,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "basis_event_ids": list(self.basis_event_ids),
            "rule_hits": list(self.rule_hits),
            "actions": [a.to_dict() for a in self.actions],
            "supersedes": self.supersedes,
            "frozen_at": self.frozen_at,
            "frozen_by": self.frozen_by,
            "withdrawn_at": self.withdrawn_at,
            "withdrawn_by": self.withdrawn_by,
            "decision_log": list(self.decision_log),
        }
