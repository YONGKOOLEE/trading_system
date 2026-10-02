"""주입 가능한 시계 (implementation-guide, 03 D-040).

시각은 항상 시간대가 있는 UTC 값으로 다룬다. KST는 고정 오프셋 +09:00 (M1, 서머타임 없음).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from datetime import time as dtime
from typing import Protocol

KST = timezone(timedelta(hours=9), "KST")


class Clock(Protocol):
    def now(self) -> datetime: ...


def require_aware(value: datetime) -> datetime:
    """시간대 없는 시각을 거부하고 UTC로 바꾼다."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("시간대 없는 시각(naive datetime)은 허용하지 않는다")
    return value.astimezone(UTC)


def to_iso(value: datetime) -> str:
    return require_aware(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def from_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def next_kst_time_after(after: datetime, at: dtime) -> datetime:
    """`after` 이후(엄격히) 처음 오는 KST `at` 시각을 UTC로 돌려준다.

    정확히 `at`에 제출하면 다음 날이다. 휴장·주말은 고려하지 않는다 (달력은 T-010).
    """
    local = require_aware(after).astimezone(KST)
    candidate = datetime.combine(local.date(), at, tzinfo=KST)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def last_kst_time_at_or_before(at: datetime, t: dtime) -> datetime:
    """`at` 이전(같은 시각 포함) 마지막 KST `t` 시각을 UTC로 돌려준다. 일일 기준 시각으로 당일 범위를 정할 때 쓴다."""
    local = require_aware(at).astimezone(KST)
    candidate = datetime.combine(local.date(), t, tzinfo=KST)
    if candidate > local:
        candidate -= timedelta(days=1)
    return candidate.astimezone(UTC)


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """테스트·백테스트용 시계."""

    def __init__(self, start: datetime) -> None:
        self._now = require_aware(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta
