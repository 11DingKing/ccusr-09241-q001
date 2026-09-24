"""时间端口。

所有业务代码只依赖 :class:`Clock`，生产环境使用系统 UTC 时钟，
自动化测试与稳定重放使用固定/脚本时钟，从而获得可复现的决策时间戳。
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

UTC = dt.timezone.utc


def to_iso(value: dt.datetime) -> str:
    """把时间转为规范的 UTC ISO-8601 字符串（``Z`` 结尾）。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | dt.datetime) -> dt.datetime:
    """解析 ISO-8601 字符串；接受 ``Z`` 与显式偏移，返回 UTC aware 时间。"""
    if isinstance(value, dt.datetime):
        result = value
    else:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        result = dt.datetime.fromisoformat(text)
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


class Clock(Protocol):
    def now(self) -> dt.datetime:
        """返回当前时间。"""


class SystemClock:
    """生产环境时钟。"""

    def now(self) -> dt.datetime:
        return dt.datetime.now(tz=UTC)


class FixedClock:
    """固定时钟，可通过 :meth:`advance` 或 :meth:`set` 调整。"""

    def __init__(self, initial: str | dt.datetime | None = None) -> None:
        self._now = parse_iso(initial) if initial is not None else dt.datetime(2026, 9, 24, 9, 0, tzinfo=UTC)

    def now(self) -> dt.datetime:
        return self._now

    def set(self, value: str | dt.datetime) -> None:
        self._now = parse_iso(value)

    def advance(self, minutes: float = 0, seconds: float = 0) -> dt.datetime:
        self._now = self._now + dt.timedelta(minutes=minutes, seconds=seconds)
        return self._now


class ScriptedClock:
    """按预设时间序列依次返回，重放同一脚本时行为完全一致。"""

    def __init__(self, moments: list[str | dt.datetime]) -> None:
        if not moments:
            raise ValueError("ScriptedClock 至少需要一个时间点")
        self._moments = [parse_iso(m) for m in moments]
        self._index = 0

    def now(self) -> dt.datetime:
        value = self._moments[min(self._index, len(self._moments) - 1)]
        if self._index < len(self._moments) - 1:
            self._index += 1
        return value
