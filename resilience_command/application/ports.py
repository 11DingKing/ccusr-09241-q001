"""可替换端口：时间、事件日志与标识生成均通过协议注入，便于测试复现。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """返回当前 UTC 时间。"""
        ...


class EventLog(Protocol):
    def next_seq(self) -> int:
        """下一条记录的序号（从 1 开始单调递增）。"""
        ...

    def append(self, record: dict[str, Any]) -> int:
        """追加一条记录并返回其序号。"""
        ...

    def records(self) -> Iterable[dict[str, Any]]:
        """按序号顺序返回全部记录。"""
        ...


class IdGenerator(Protocol):
    def next(self, prefix: str) -> str:
        """生成 ``PREFIX-0001`` 形式的单调标识。"""
        ...

    def observe(self, identifier: str) -> None:
        """重放时登记已有标识，保证重启后编号连续。"""
        ...
