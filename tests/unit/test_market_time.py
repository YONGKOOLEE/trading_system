"""시장별 시간 모델 (CP-024 D-108, T-M2-02). V-140(일 경계 계산), V-142(1d 봉 확정), V-152(M1 계산 불변).

M1 = KST 정규장 시장(기존 clock.py 함수와 결과가 같아야 한다), M2 = UTC 24/7 시장(거래일 = UTC 날짜, 일 경계 UTC 00:00).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from trading_system.domain.clock import KST, last_kst_time_at_or_before, next_kst_time_after
from trading_system.domain.market_time import (
    M1_TIME, M2_TIME, Market, daily_bar_final_at, daily_bar_is_final, market_time, session_247,
)

SAMPLES = [
    datetime(2026, 10, 7, 0, 0, tzinfo=UTC),
    datetime(2026, 10, 7, 14, 59, 59, tzinfo=UTC),
    datetime(2026, 10, 7, 15, 0, tzinfo=UTC),     # KST 자정
    datetime(2026, 10, 7, 23, 59, 59, tzinfo=UTC),
    datetime(2026, 10, 8, 0, 0, 1, tzinfo=UTC),
]


@pytest.mark.parametrize("at", SAMPLES)
@pytest.mark.parametrize("t", [time(0, 0), time(8, 0), time(9, 0), time(16, 0)])
def test_m1_time_matches_existing_kst_functions(at: datetime, t: time) -> None:
    """V-152: M1 시장 시간은 기존 KST 함수와 결과가 같다."""
    assert M1_TIME.last_at_or_before(at, t) == last_kst_time_at_or_before(at, t)
    assert M1_TIME.next_after(at, t) == next_kst_time_after(at, t)
    assert M1_TIME.local(at).date() == at.astimezone(KST).date()


def test_market_lookup() -> None:
    assert market_time("M1") is M1_TIME
    assert market_time(Market.M2) is M2_TIME
    with pytest.raises(ValueError):
        market_time("M3")


def test_m2_day_boundary_is_utc_midnight_not_kst() -> None:
    """V-140: M2 일 경계는 UTC 00:00(KST 09:00). KST 자정(UTC 15:00)은 경계가 아니다."""
    ref = time(0, 0)
    before_kst_midnight = datetime(2026, 10, 7, 14, 59, tzinfo=UTC)
    after_kst_midnight = datetime(2026, 10, 7, 15, 1, tzinfo=UTC)
    start = datetime(2026, 10, 7, 0, 0, tzinfo=UTC)
    assert M2_TIME.last_at_or_before(before_kst_midnight, ref) == start
    assert M2_TIME.last_at_or_before(after_kst_midnight, ref) == start
    assert M2_TIME.trade_date(after_kst_midnight, ref) == date(2026, 10, 7)
    # 경계 시각 정각은 새 거래일
    assert M2_TIME.trade_date(datetime(2026, 10, 8, 0, 0, tzinfo=UTC), ref) == date(2026, 10, 8)
    assert M2_TIME.trade_date(datetime(2026, 10, 7, 23, 59, 59, tzinfo=UTC), ref) == date(2026, 10, 7)
    # 다음 경계
    assert M2_TIME.next_after(start, ref) == start + timedelta(days=1)


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError):
        M2_TIME.last_at_or_before(datetime(2026, 10, 7, 1, 0), time(0, 0))


def test_session_247_covers_whole_utc_day() -> None:
    s = session_247(date(2026, 10, 7))
    assert s.is_open
    assert s.regular_open == datetime(2026, 10, 7, tzinfo=UTC)
    assert s.regular_close == datetime(2026, 10, 8, tzinfo=UTC)
    assert s.in_regular(datetime(2026, 10, 7, 23, 59, 59, tzinfo=UTC))
    assert not s.in_regular(datetime(2026, 10, 8, 0, 0, tzinfo=UTC))
    # 주말도 열려 있다 (24/7)
    assert session_247(date(2026, 10, 10)).is_open


def test_daily_bar_final_only_after_bar_start_plus_one_day() -> None:
    """V-142: 1d 봉(UTC 날짜)은 봉 시작 + 1일 이후에만 확정. 진행 중 봉은 쓰지 않는다."""
    final = daily_bar_final_at(date(2026, 10, 7))
    assert final == datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    assert daily_bar_is_final(date(2026, 10, 7), datetime(2026, 10, 8, 0, 0, tzinfo=UTC))
    assert not daily_bar_is_final(date(2026, 10, 7), datetime(2026, 10, 7, 23, 59, 59, tzinfo=UTC))
