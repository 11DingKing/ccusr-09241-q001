"""标识与决策序号端口。

生产环境使用随机 UUID 与墙钟无关的单调序号；测试与稳定重放中使用
:class:`DeterministicIds`，使同一组乱序事件重放出完全一致的标识。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Protocol


class IdGenerator(Protocol):
    def new_event_id(self) -> str: ...
    def new_plan_id(self) -> str: ...
    def new_action_id(self) -> str: ...
    def next_plan_version(self, plan_id: str) -> int:
        """返回某方案的下一个版本号（首版为 1）。"""


class UuidIds:
    """生产环境标识生成器；版本号由聚合自身维护，此处做兜底计数。"""

    def __init__(self) -> None:
        self._versions: dict[str, int] = defaultdict(int)

    def new_event_id(self) -> str:
        return uuid.uuid4().hex

    def new_plan_id(self) -> str:
        return "plan-" + uuid.uuid4().hex[:12]

    def new_action_id(self) -> str:
        return "act-" + uuid.uuid4().hex[:12]

    def next_plan_version(self, plan_id: str) -> int:
        self._versions[plan_id] += 1
        return self._versions[plan_id]


class DeterministicIds:
    """确定性标识生成器：计数 + 前缀，事件/方案/行动各自独立编号。

    跨重放实例可以通过 *start* 延续编号；同一输入序列总是得到同一组标识。
    """

    def __init__(self, start: int = 0) -> None:
        self._event_n = start
        self._plan_n = start
        self._action_n = start
        self._versions: dict[str, int] = defaultdict(int)

    def new_event_id(self) -> str:
        self._event_n += 1
        return f"evt-{self._event_n:04d}"

    def new_plan_id(self) -> str:
        self._plan_n += 1
        return f"plan-{self._plan_n:04d}"

    def new_action_id(self) -> str:
        self._action_n += 1
        return f"act-{self._action_n:04d}"

    def next_plan_version(self, plan_id: str) -> int:
        self._versions[plan_id] += 1
        return self._versions[plan_id]

    def action_count(self) -> int:
        return self._action_n

    def sync(self, event_ids: list[str], plan_ids: list[str], action_ids: list[str]) -> None:
        """把计数推进到已知标识的最大编号，用于重启恢复后继续生成不冲突标识。"""
        def max_numeric(ids: list[str], prefix: str) -> int:
            best = 0
            for value in ids:
                if value.startswith(prefix):
                    tail = value[len(prefix):]
                    if tail.isdigit():
                        best = max(best, int(tail))
            return best

        self._event_n = max(self._event_n, max_numeric(event_ids, "evt-"))
        self._plan_n = max(self._plan_n, max_numeric(plan_ids, "plan-"))
        self._action_n = max(self._action_n, max_numeric(action_ids, "act-"))
