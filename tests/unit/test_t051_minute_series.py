"""CP-037 T-051 — M2 분봉 순수 규칙 (D-220, V-292·V-293 L1). 네트워크·저장소 없음.

닫힌 봉 규칙(봉 시작 + 60초 ≤ 판정 시각 − 확정 여유), 판정 창(마지막 닫힌 봉 누락·창 안 결측), 품질(중복·역순·음수·
0 이하 가격·고저 모순), 봉 지문, 주기 격자(UTC 00:00 + k×N분 + 여유, 주문 창 안).
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest

from trading_system.domain.minute_bar import MinuteBar
from trading_system.domain.minute_series import (
    DEFAULT_CONFIRM_MARGIN, NO_ADJUSTMENT, PeriodGrid, WindowStatus, bar_fingerprint, check_window, closed_bars,
    last_closed_start, minute_quality_issues,
)

D = Decimal
SYM = "BTCUSDT"


def utc(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 10, 10, hh, mm, ss, tzinfo=UTC)


def bar(start: datetime, o: str = "100", h: str = "101", lo: str = "99", c: str = "100", v: str = "1") -> MinuteBar:
    return MinuteBar(SYM, 1, start, D(o), D(h), D(lo), D(c), D(v), NO_ADJUSTMENT)


def run(first: datetime, n: int) -> list[MinuteBar]:
    return [bar(first + timedelta(minutes=i)) for i in range(n)]


# --- 닫힌 봉 (V-292 진행 중 봉 미사용) ---------------------------------------------------------------------

def test_last_closed_start_uses_margin() -> None:
    assert DEFAULT_CONFIRM_MARGIN == timedelta(seconds=2)
    assert last_closed_start(utc(1, 0, 2), DEFAULT_CONFIRM_MARGIN) == utc(0, 59)     # 00:59 봉 끝 01:00:00 ≤ 01:00:00
    assert last_closed_start(utc(1, 0, 1), DEFAULT_CONFIRM_MARGIN) == utc(0, 58)     # 여유 안 → 한 봉 앞
    assert last_closed_start(utc(1, 0, 0), DEFAULT_CONFIRM_MARGIN) == utc(0, 58)


def test_closed_bars_drop_in_progress_bar() -> None:
    bars = run(utc(0, 55), 6)                        # 00:55 … 01:00 (01:00 = 진행 중)
    kept = closed_bars(bars, utc(1, 0, 2), DEFAULT_CONFIRM_MARGIN)
    assert [b.start for b in kept] == [utc(0, m) for m in range(55, 60)]


def test_naive_time_refused() -> None:
    with pytest.raises(ValueError):
        last_closed_start(datetime(2026, 10, 10, 1, 0), DEFAULT_CONFIRM_MARGIN)


# --- 판정 창 (V-292 누락·결측) -----------------------------------------------------------------------------

def test_window_ok_has_exact_lookback_and_fingerprint() -> None:
    bars = run(utc(0, 40), 21)                       # 00:40 … 01:00
    w = check_window(bars, utc(1, 0, 2), 10, DEFAULT_CONFIRM_MARGIN)
    assert w.status is WindowStatus.OK and w.missing == 0
    assert [b.start for b in w.bars] == [utc(0, m) for m in range(50, 60)]
    assert (w.first_start, w.last_start) == (utc(0, 50), utc(0, 59))
    assert w.fingerprint == bar_fingerprint(w.bars) and len(w.fingerprint) == 64


def test_window_last_bar_missing_is_invalid() -> None:
    bars = [b for b in run(utc(0, 40), 20) if b.start != utc(0, 59)]
    w = check_window(bars, utc(1, 0, 2), 10, DEFAULT_CONFIRM_MARGIN)
    assert w.status is WindowStatus.LAST_MISSING and w.bars == () and w.fingerprint == ""


def test_window_interior_gap_is_invalid() -> None:
    bars = [b for b in run(utc(0, 40), 20) if b.start != utc(0, 53)]
    w = check_window(bars, utc(1, 0, 2), 10, DEFAULT_CONFIRM_MARGIN)
    assert w.status is WindowStatus.GAP and w.missing == 1 and w.bars == ()


def test_window_ignores_in_progress_and_other_lengths() -> None:
    bars = run(utc(0, 50), 11) + [MinuteBar(SYM, 5, utc(0, 55), D(1), D(1), D(1), D(1), D(1), NO_ADJUSTMENT)]
    w = check_window(bars, utc(1, 0, 2), 10, DEFAULT_CONFIRM_MARGIN)
    assert w.status is WindowStatus.OK and all(b.minutes == 1 for b in w.bars) and w.bars[-1].start == utc(0, 59)


# --- 품질 (V-292 중복·역순·음수·고저 모순) -------------------------------------------------------------------

def test_quality_clean_run_has_no_issues() -> None:
    assert minute_quality_issues(run(utc(0, 0), 5)) == []


@pytest.mark.parametrize("bars, word", [
    ([bar(utc(0, 1)), bar(utc(0, 1))], "중복"),
    ([bar(utc(0, 2)), bar(utc(0, 1))], "역순"),
    ([bar(utc(0, 1), v="-1")], "음수"),
    ([bar(utc(0, 1), o="0", lo="0")], "0 이하"),
    ([bar(utc(0, 1), h="99.5")], "고가·저가 모순"),
    ([bar(utc(0, 1), lo="100.5")], "고가·저가 모순"),
])
def test_quality_violations(bars: list[MinuteBar], word: str) -> None:
    issues = minute_quality_issues(bars)
    assert issues and any(word in i for i in issues)


# --- 지문 (V-293) ------------------------------------------------------------------------------------------

def test_fingerprint_stable_under_decimal_representation_and_order_sensitive() -> None:
    a = [bar(utc(0, 1), o="100.0"), bar(utc(0, 2))]
    b = [bar(utc(0, 1), o="100.00"), bar(utc(0, 2))]
    assert bar_fingerprint(a) == bar_fingerprint(b)                    # 값이 같으면 자릿수 표기와 무관
    c = [bar(utc(0, 1), c="100.5", h="101"), bar(utc(0, 2))]
    assert bar_fingerprint(a) != bar_fingerprint(c)
    assert bar_fingerprint([]) == bar_fingerprint(())


# --- 주기 격자 (판정 일정 연동 — T-052가 엔진에서 같은 격자를 쓴다) ------------------------------------------

def test_grid_latest_at_or_before() -> None:
    g = PeriodGrid(5)
    assert g.latest_at_or_before(utc(0, 30, 2)) == utc(0, 30, 2)
    assert g.latest_at_or_before(utc(0, 30, 1)) == utc(0, 25, 2)
    assert g.latest_at_or_before(utc(0, 34, 59)) == utc(0, 30, 2)
    assert g.grace == timedelta(seconds=60)
    assert PeriodGrid(1).grace == timedelta(seconds=30)               # min(60초, N분의 절반)


def test_grid_only_inside_order_window() -> None:
    g = PeriodGrid(5)                                                  # 기본 주문 창 UTC 00:10~23:50 (A-064)
    assert g.latest_at_or_before(utc(0, 5, 2)) is None
    assert g.latest_at_or_before(utc(0, 10, 2)) == utc(0, 10, 2)
    assert g.latest_at_or_before(utc(23, 45, 2)) == utc(23, 45, 2)
    assert g.latest_at_or_before(utc(23, 50, 2)) is None              # 창 끝 제외 — 게이트 L8 start ≤ t < end와 같음 (A-124 ①)
    assert g.latest_at_or_before(utc(23, 55, 2)) is None
    g2 = PeriodGrid(15, window_start=time(0, 0), window_end=time(23, 59))
    assert g2.latest_at_or_before(utc(0, 0, 3)) == utc(0, 0, 2)


@pytest.mark.parametrize("n", [0, -5, 7, 1441, 2880])
def test_grid_period_must_divide_day(n: int) -> None:
    with pytest.raises(ValueError):
        PeriodGrid(n)
