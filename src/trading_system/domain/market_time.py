"""시장별 시간 모델 (CP-024 D-108, T-M2-02).

- M1(KRX): 시장 현지 시각 = KST(고정 +09:00), 정규장 달력은 marketdata/calendar.py. 계산 결과는 clock.py의
  `last_kst_time_at_or_before`·`next_kst_time_after`와 같다(M1 동작 불변, V-152).
- M2(크립토 현물, 24/7): 시장 현지 시각 = UTC. 매일 UTC 00:00~24:00 한 구간이 열려 있고(주말 포함),
  거래일 = UTC 날짜, 일 경계 = UTC 00:00(KST 09:00) [결정 A-055]. 1d 봉은 "봉 시작 + 1일" 이후에만 확정.
표준 라이브러리만 쓴다 (03 §7).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum

from .clock import KST, require_aware
from .market import SessionInfo


class Market(StrEnum):
    """인도 차수(시장). M3는 아직 정의하지 않는다."""

    M1 = "M1"   # 국내주식·ETF (KRX, 키움)
    M2 = "M2"   # 크립토 현물 (바이낸스)


@dataclass(frozen=True)
class MarketTime:
    """한 시장의 시각 기준. tz = 시장 현지 시각대, always_open = 24/7 시장."""

    market: Market
    tz: tzinfo
    always_open: bool

    def local(self, at: datetime) -> datetime:
        return require_aware(at).astimezone(self.tz)

    def last_at_or_before(self, at: datetime, t: time) -> datetime:
        """`at` 이전(같은 시각 포함) 마지막 현지 `t` 시각(UTC). 일일 기준 시각으로 당일 범위를 정할 때 쓴다."""
        local = self.local(at)
        candidate = datetime.combine(local.date(), t, tzinfo=self.tz)
        if candidate > local:
            candidate -= timedelta(days=1)
        return candidate.astimezone(UTC)

    def next_after(self, at: datetime, t: time) -> datetime:
        """`at` 이후(엄격히) 처음 오는 현지 `t` 시각(UTC). 정확히 `t`이면 다음 날이다."""
        local = self.local(at)
        candidate = datetime.combine(local.date(), t, tzinfo=self.tz)
        if candidate <= local:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    def trade_date(self, at: datetime, reference_time: time) -> date:
        """`at`이 속한 거래일 = 직전(같은 시각 포함) 일일 기준 시각의 현지 날짜."""
        return self.last_at_or_before(at, reference_time).astimezone(self.tz).date()


M1_TIME = MarketTime(Market.M1, KST, always_open=False)
M2_TIME = MarketTime(Market.M2, UTC, always_open=True)
_BY_MARKET = {Market.M1: M1_TIME, Market.M2: M2_TIME}


def market_time(market: str | Market) -> MarketTime:
    try:
        return _BY_MARKET[Market(market)]
    except ValueError:
        raise ValueError(f"알 수 없는 시장: {market!r} (M1|M2)") from None


def session_247(trade_date: date) -> SessionInfo:
    """24/7 시장의 한 거래일(UTC 날짜): UTC 00:00 ~ 다음 날 UTC 00:00 한 구간 (D-108)."""
    start = datetime.combine(trade_date, time(0, 0), tzinfo=UTC)
    return SessionInfo(True, start, start + timedelta(days=1))


def daily_bar_final_at(bar_date: date) -> datetime:
    """UTC 날짜 `bar_date`의 1d 봉이 확정되는 시각 = 봉 시작 + 1일 (D-108, V-142)."""
    return datetime.combine(bar_date, time(0, 0), tzinfo=UTC) + timedelta(days=1)


def daily_bar_is_final(bar_date: date, at: datetime) -> bool:
    """`at` 시점에 그 1d 봉을 판정에 써도 되는지. 진행 중인 봉은 쓰지 않는다(룩어헤드 방지)."""
    return require_aware(at) >= daily_bar_final_at(bar_date)
