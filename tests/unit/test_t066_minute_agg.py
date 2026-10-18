"""CP-040 T-066 (A-115·A-123 ④) — 집계 봉 규칙 (D-259, V-328)·이력 나눔 계획 (D-261, V-330 일부)·주기 격자 횟수 (D-258, V-337 일부).

순수 함수만(가상 1분 봉). 네트워크·저장소 없음.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_system.domain.minute_agg import (
    AGG_MINUTES, AGG_RULE_VERSION, AggStatus, check_agg_window, missing_ranges, split_pages,
)
from trading_system.domain.minute_bar import MinuteBar
from trading_system.domain.minute_series import NO_ADJUSTMENT, PeriodGrid

D = Decimal
SYM = "BTCUSDT"
M = timedelta(minutes=1)


def utc(hh: int, mm: int, ss: int = 0, day: int = 10) -> datetime:
    return datetime(2026, 10, day, hh, mm, ss, tzinfo=UTC)


def bar(t: datetime, o: str = "100", h: str = "101", lo: str = "99", c: str = "100", v: str = "1",
        ext: bool = True) -> MinuteBar:
    if ext:
        return MinuteBar(SYM, 1, t, D(o), D(h), D(lo), D(c), D(v), NO_ADJUSTMENT,
                         quote_volume=D("100"), trades=3, taker_buy_base=D("0.4"), taker_buy_quote=D("40"))
    return MinuteBar(SYM, 1, t, D(o), D(h), D(lo), D(c), D(v), NO_ADJUSTMENT)


def series(first: datetime, n: int, *, skip: frozenset[datetime] = frozenset(), ext: bool = True) -> list[MinuteBar]:
    return [bar(first + M * i, ext=ext) for i in range(n) if first + M * i not in skip]


# --- V-328 값 식 -----------------------------------------------------------------------------------------------

def test_v328_ohlc_and_sums_for_60_and_15() -> None:
    bars = []
    for i in range(60):
        t = utc(0, 0) + M * i
        bars.append(MinuteBar(SYM, 1, t, D(100 + i), D(110 + i), D(90 + i), D(101 + i), D("0.5"), NO_ADJUSTMENT,
                              quote_volume=D("10"), trades=2, taker_buy_base=D("0.1"), taker_buy_quote=D("1.5")))
    w = check_agg_window(bars, utc(1, 0, 2), 60, 1)
    assert w.status is AggStatus.OK and len(w.bars) == 1
    b = w.bars[0]
    assert (b.at, b.open, b.high, b.low, b.close, b.volume) == (utc(0, 0), D(100), D(169), D(90), D(160), D(30))
    assert (b.quote_volume, b.trades, b.taker_buy_base, b.taker_buy_quote) == (D(600), 120, D("6.0"), D("90.0"))
    w15 = check_agg_window(bars, utc(1, 0, 2), 15, 4)
    assert w15.status is AggStatus.OK and [x.at for x in w15.bars] == [utc(0, 0), utc(0, 15), utc(0, 30), utc(0, 45)]
    assert (w15.bars[1].open, w15.bars[1].high, w15.bars[1].low, w15.bars[1].close, w15.bars[1].volume) == \
        (D(115), D(139), D(105), D(130), D("7.5"))


def test_v328_missing_minute_makes_bucket_missing_no_fill() -> None:
    bars = series(utc(0, 0), 120, skip=frozenset({utc(0, 20)}))
    w = check_agg_window(bars, utc(2, 0, 2), 60, 2, max_missing=1)
    assert w.status is AggStatus.OK
    assert [b.at for b in w.bars] == [utc(1, 0)] and w.missing == (utc(0, 0),)   # 00:00 봉 결측 — 만들지 않음
    w0 = check_agg_window(bars, utc(2, 0, 2), 60, 2, max_missing=0)
    assert w0.status is AggStatus.TOO_MANY_MISSING and w0.bars == () and w0.missing == (utc(0, 0),)


def test_v328_last_closed_bucket_missing_invalidates() -> None:
    bars = series(utc(0, 0), 120, skip=frozenset({utc(1, 59)}))
    w = check_agg_window(bars, utc(2, 0, 2), 60, 2, max_missing=5)
    assert w.status is AggStatus.LAST_MISSING and w.bars == ()


def test_v328_in_progress_minute_not_confirmed() -> None:
    bars = series(utc(0, 0), 61)                                   # 01:00 분(진행 중)까지
    early = check_agg_window(bars, utc(1, 0, 1), 60, 1)            # 01:00:01 − 2초 → [00:00, 01:00) 아직 미확정
    assert early.last_start == utc(23, 0, day=9) and early.status is AggStatus.LAST_MISSING
    ok = check_agg_window(bars, utc(1, 0, 2), 60, 1)
    assert ok.status is AggStatus.OK and [b.at for b in ok.bars] == [utc(0, 0)]
    assert ok.used_minutes == 60                                   # 01:00 분은 쓰지 않음


def test_v328_utc_alignment_across_midnight() -> None:
    bars = series(utc(22, 30, day=9), 120)                         # 22:30 ~ 00:29
    w = check_agg_window(bars, utc(0, 30, 2), 60, 1)
    assert [b.at for b in w.bars] == [utc(23, 0, day=9)] and w.status is AggStatus.OK
    w15 = check_agg_window(bars, utc(0, 15, 2), 15, 2)
    assert [b.at for b in w15.bars] == [utc(23, 45, day=9), utc(0, 0)]


def test_v328_old_missing_within_allowance() -> None:
    bars = series(utc(0, 0), 180, skip=frozenset({utc(0, 5)}))
    assert check_agg_window(bars, utc(3, 0, 2), 60, 3, max_missing=0).status is AggStatus.TOO_MANY_MISSING
    ok = check_agg_window(bars, utc(3, 0, 2), 60, 3, max_missing=1)
    assert ok.status is AggStatus.OK and ok.missing == (utc(0, 0),) and len(ok.bars) == 2


def test_v328_same_input_same_fingerprint_and_rule_version() -> None:
    a = check_agg_window(series(utc(0, 0), 120), utc(2, 0, 2), 60, 2)
    b = check_agg_window(list(reversed(series(utc(0, 0), 120))), utc(2, 0, 2), 60, 2)
    assert a.fingerprint == b.fingerprint and len(a.fingerprint) == 64 and AGG_RULE_VERSION
    other = series(utc(0, 0), 120)
    other[10] = bar(utc(0, 10), c="100.5", h="101")
    assert check_agg_window(other, utc(2, 0, 2), 60, 2).fingerprint != a.fingerprint
    ext_changed = series(utc(0, 0), 120)
    ext_changed[3] = MinuteBar(SYM, 1, utc(0, 3), D(100), D(101), D(99), D(100), D(1), NO_ADJUSTMENT,
                               quote_volume=D("100"), trades=3, taker_buy_base=D("0.9"), taker_buy_quote=D("40"))
    assert check_agg_window(ext_changed, utc(2, 0, 2), 60, 2).fingerprint != a.fingerprint   # 확장 열도 지문에


def test_v328_rejects_other_periods_and_bad_args() -> None:
    assert AGG_MINUTES == (15, 60)
    for n in (5, 30, 1440):
        with pytest.raises(ValueError):
            check_agg_window([], utc(1, 0, 2), n, 1)
    with pytest.raises(ValueError):
        check_agg_window([], utc(1, 0, 2), 60, 0)
    with pytest.raises(ValueError):
        check_agg_window([], utc(1, 0, 2), 60, 2, max_missing=-1)


# --- V-329 흐름 열 선언 → 확장 결측 = 결측 ---------------------------------------------------------------------

def test_v329_flow_declared_counts_ext_missing_as_missing() -> None:
    bars = series(utc(0, 0), 120)
    bars[7] = bar(utc(0, 7), ext=False)                           # 00:00 봉 구성 분 하나 확장 열 없음
    plain = check_agg_window(bars, utc(2, 0, 2), 60, 2)
    assert plain.status is AggStatus.OK and plain.bars[0].taker_buy_base is None   # OHLCV 판정 그대로, 그 열만 None
    assert plain.bars[0].volume == D(60)
    flow = check_agg_window(bars, utc(2, 0, 2), 60, 2, flow=True)
    assert flow.status is AggStatus.TOO_MANY_MISSING and flow.missing == (utc(0, 0),)
    assert check_agg_window(bars, utc(2, 0, 2), 60, 2, max_missing=1, flow=True).status is AggStatus.OK


# --- V-330 나눔 계획 (저장 0일·30일·60일) ----------------------------------------------------------------------

def test_v330_pages_only_for_missing_minutes() -> None:
    last = utc(0, 59, day=20)
    first = last - M * (60 * 1440 - 1)                             # 60일 창 = 86,400분
    assert len(split_pages(missing_ranges([], first, last))) == 87          # 저장 0일 → 87회 (≈ weight 174)
    half = [last - M * i for i in range(30 * 1440)]                # 최근 30일 저장
    pages = split_pages(missing_ranges(half, first, last))
    assert len(pages) == 44 and pages[0][0] == first and pages[-1][1] == last - M * (30 * 1440)
    assert all((e - s) // M + 1 <= 1000 for s, e in pages)
    full = [first + M * i for i in range(60 * 1440)]
    assert split_pages(missing_ranges(full, first, last)) == []    # 저장 60일 → 조회 없음


def test_v330_missing_ranges_merge_gaps() -> None:
    first, last = utc(0, 0), utc(0, 9)
    present = [utc(0, m) for m in (0, 1, 4, 5, 9)]
    assert missing_ranges(present, first, last) == [(utc(0, 2), utc(0, 3)), (utc(0, 6), utc(0, 8))]
    assert split_pages([(utc(0, 0), utc(0, 9))], page_max=4) == [
        (utc(0, 0), utc(0, 3)), (utc(0, 4), utc(0, 7)), (utc(0, 8), utc(0, 9))]


# --- V-337 판정 횟수 -------------------------------------------------------------------------------------------

def test_v337_daily_decision_counts() -> None:
    g60, g15 = PeriodGrid(60), PeriodGrid(15)
    t60 = g60.day_times(utc(0, 0).date())
    assert len(t60) == 23 and t60[0] == utc(1, 0, 2) and t60[-1] == utc(23, 0, 2)
    t15 = g15.day_times(utc(0, 0).date())
    assert len(t15) == 95 and t15[0] == utc(0, 15, 2) and t15[-1] == utc(23, 45, 2)
