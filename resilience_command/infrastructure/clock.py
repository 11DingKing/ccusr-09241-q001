"""时钟端口实现：系统时钟与可手动推进的演练时钟。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..domain.models import parse_time


class SystemClock:
    """生产时钟：返回当前 UTC 时间。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """演练/测试时钟：时间只能显式推进，保证结果可复现。"""

    def __init__(self, start: str | datetime) -> None:
        if isinstance(start, str):
            start = parse_time(start)
        self._now = start.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def set(self, moment: str | datetime) -> None:
        if isinstance(moment, str):
            moment = parse_time(moment)
        self._now = moment.astimezone(timezone.utc)

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)
