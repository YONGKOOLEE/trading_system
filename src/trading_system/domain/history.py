"""판정 스냅샷의 거래 이력 (00 §2.2 "거래 이력": 직전 청산 시각·사유, 최근 N거래일 진입 횟수, 보유 거래일 수).

원자료는 저장소 `strategy_history`(소유 기록·체결·의도)에서 온다. 백테스트와 실운영이 같은 원자료·같은 계산을 쓴다.
- 보유 거래일 수: 진입 첫 체결일 다음 거래일부터 판정일까지 센 거래일 수 (진입 당일 0).
- 최근 진입 횟수: 판정일을 포함한 최근 N거래일 안에 생긴 진입(체결 0주로 끝난 진입 제외). N 기본 20(결정 대기 DP-10).
- 달력에 없는 날짜는 평일을 거래일로 본다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from trading_system.domain.clock import KST, from_iso

RECENT_ENTRY_DAYS = 20  # 결정 대기 DP-10 (전략 명세 항목으로 둘지)

TradingDayFn = Callable[[date], bool | None]


@dataclass(frozen=True)
class HistoryView:
    holding_days: int | None
    last_exit_at: datetime | None
    last_exit_reason: str | None
    entries_recent: int


def _is_trading(d: date, is_trading_day: TradingDayFn | None) -> bool:
    known = is_trading_day(d) if is_trading_day is not None else None
    return known if known is not None else d.weekday() < 5


def trading_days_after(start: date, end: date, is_trading_day: TradingDayFn | None) -> int:
    """start 다음 날부터 end까지의 거래일 수. end <= start면 0."""
    n, d = 0, start + timedelta(days=1)
    while d <= end:
        n += _is_trading(d, is_trading_day)
        d += timedelta(days=1)
    return n


def history_view(raw: dict, as_of: datetime, is_trading_day: TradingDayFn | None,
                 recent_days: int = RECENT_ENTRY_DAYS) -> HistoryView:
    today = as_of.astimezone(KST).date()

    def kst_day(text: str) -> date:
        return from_iso(text).astimezone(KST).date()

    entry = raw.get("entry_at")
    holding = trading_days_after(kst_day(entry), today, is_trading_day) if entry else None
    recent = 0
    for created in raw.get("entries", []):
        if from_iso(created) <= as_of and trading_days_after(kst_day(created), today, is_trading_day) < recent_days:
            recent += 1
    last_exit = raw.get("last_exit_at")
    exit_at = from_iso(last_exit) if last_exit else None
    if exit_at is not None and exit_at > as_of:
        exit_at = None  # 판정 기준 시각 이후 정보는 넣지 않는다 (R-10)
    return HistoryView(holding, exit_at, raw.get("last_exit_reason") if exit_at else None, recent)
