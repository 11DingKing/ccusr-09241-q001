"""单调标识生成器：``PLAN-0001`` 形式，重放时通过 observe 恢复计数。"""

from __future__ import annotations


class SequentialIds:
    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def next(self, prefix: str) -> str:
        value = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = value
        return f"{prefix}-{value:04d}"

    def observe(self, identifier: str) -> None:
        prefix, sep, tail = identifier.rpartition("-")
        if sep and tail.isdigit():
            self._counters[prefix] = max(self._counters.get(prefix, 0), int(tail))
