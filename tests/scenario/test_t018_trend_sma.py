"""T-018 첫 전략 trend-sma v1 (초안, 사용자 미승인) 단위 시험.

명세: docs/strategies/trend-sma/strategy.md, 파라미터: config/strategies/trend-sma-v1.toml.
진입·청산·손절·재진입 제한·무신호·파라미터 검증·룩어헤드 없음(스냅샷 밖 데이터 미사용)·결정성.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.config.capabilities import load_capabilities
from trading_system.config.errors import ConfigError
from trading_system.config.limits_file import load_limits_file
from trading_system.config.strategy_params import load_params_file, validate_params
from trading_system.config.strategy_registry import validate_spec
from trading_system.domain.clock import KST
from trading_system.domain.enums import Mode, RequestType
from trading_system.domain.market import TradingStatus
from trading_system.domain.strategy import Bar, EvaluationSnapshot, InstrumentView, PositionView
from trading_system.strategy.candidates import L7_AMOUNT, WORST_PRICE_FACTOR
from trading_system.strategy.trend_sma import TrendSmaV1

ROOT = Path(__file__).resolve().parents[2]
PARAMS_FILE = ROOT / "config" / "strategies" / "trend-sma-v1.toml"
T0 = datetime(2026, 10, 5, 0, 30, tzinfo=UTC)   # 09:30 KST 판정
D0 = date(2026, 1, 1)
RISING = [50000 + 100 * i for i in range(80)]    # SMA20 > SMA60
FALLING = [80000 - 100 * i for i in range(80)]   # SMA20 < SMA60


def bars(closes: list[float]) -> tuple[Bar, ...]:
    return tuple(Bar(D0 + timedelta(days=i), Decimal(str(c)), Decimal(str(c * 1.01)), Decimal(str(c * 0.99)),
                     Decimal(str(c)), Decimal("1000")) for i, c in enumerate(closes))


def snap(*views: InstrumentView) -> EvaluationSnapshot:
    return EvaluationSnapshot("ev", "M1", "KR-BT-01", T0, Decimal("100000000"), tuple(views))


def params(**over: object) -> dict[str, object]:
    p = dict(load_params_file(TrendSmaV1().spec, PARAMS_FILE))
    p.update(over)
    return p


def held(qty: str = "10", avg: str = "50000") -> PositionView:
    return PositionView(Decimal(qty), Decimal(avg), 5, True)


def exit_at(bar_index: int) -> datetime:
    """bars()의 bar_index번째 봉 날짜 15:00 KST에 청산 체결됐다고 가정."""
    return datetime.combine(D0 + timedelta(days=bar_index), time(15, 0), tzinfo=KST)


# --- 명세·파라미터 -------------------------------------------------------------------------

def test_spec_validates_for_backtest_and_is_draft() -> None:
    s = TrendSmaV1().spec
    caps = load_capabilities(ROOT / "docs" / "capabilities.md")
    limits = load_limits_file(ROOT / "config" / "limits.backtest.toml").limits
    assert validate_spec(s, caps, limits, Mode.BACKTEST) == []
    assert (s.strategy_id, s.version) == ("trend-sma", 1)
    assert "초안" in s.description
    assert {r.rule_id for r in s.rules} == {"EXIT-STOP", "EXIT-1", "ENTRY-1", "REENTRY-1"}


def test_params_file_loads_with_expected_values() -> None:
    p = params()
    assert (p["fast"], p["slow"], p["atr_n"], p["reentry_wait"]) == (20, 60, 14, 5)
    assert p["atr_mult"] == Decimal("2")


def test_params_out_of_range_rejected() -> None:
    raw = {"fast": 20, "slow": 60, "atr_n": 14, "atr_mult": "2", "reentry_wait": 99,
           "risk_ratio": "0.004", "max_weight": "0.09"}
    with pytest.raises(ConfigError):
        validate_params(TrendSmaV1().spec, raw)


def test_fast_not_less_than_slow_fails_evaluation() -> None:
    with pytest.raises(ValueError):
        TrendSmaV1().evaluate(snap(InstrumentView("005930", bars(RISING))), params(fast=60, slow=60))


# --- 진입 ------------------------------------------------------------------------------

def test_entry_in_uptrend_with_stop_and_size_capped_by_l7() -> None:
    [sig] = TrendSmaV1().evaluate(snap(InstrumentView("005930", bars(RISING))), params())
    assert sig.kind is RequestType.ENTRY and sig.rule_id == "ENTRY-1"
    assert sig.strategy_id == "trend-sma" and sig.strategy_version == 1
    assert sig.stop_price is not None and 0 < sig.stop_price < sig.price
    assert sig.quantity == sig.quantity.to_integral_value() and sig.quantity > 0
    assert sig.quantity * sig.price * WORST_PRICE_FACTOR <= L7_AMOUNT
    assert sig.valid_until - sig.as_of == timedelta(minutes=10)


def test_no_entry_in_downtrend() -> None:
    assert TrendSmaV1().evaluate(snap(InstrumentView("005930", bars(FALLING))), params()) == []


def test_no_signal_during_warmup() -> None:
    assert TrendSmaV1().evaluate(snap(InstrumentView("005930", bars(RISING[:59]))), params()) == []


@pytest.mark.parametrize("view", [
    InstrumentView("005930", bars(RISING), data_ok=False),
    InstrumentView("005930", bars(RISING), has_pending_order=True),
    InstrumentView("005930", bars(RISING), statuses=frozenset({TradingStatus.HALTED})),
    InstrumentView("000660", bars(RISING)),                                   # 유니버스 밖
    InstrumentView("005930", ()),
])
def test_no_entry_when_invalid(view: InstrumentView) -> None:
    assert TrendSmaV1().evaluate(snap(view), params()) == []


# --- 청산·손절 ---------------------------------------------------------------------------

def test_exit_on_dead_cross_when_held() -> None:
    v = InstrumentView("005930", bars(FALLING), position=held(avg="10000"))   # 손절가 한참 아래
    [sig] = TrendSmaV1().evaluate(snap(v), params())
    assert sig.kind is RequestType.EXIT and sig.rule_id == "EXIT-1" and sig.quantity == Decimal("10")


def test_stop_exit_even_in_uptrend() -> None:
    v = InstrumentView("005930", bars(RISING), position=held(avg="70000"))     # 종가 57900 <= 70000 − 2×ATR
    [sig] = TrendSmaV1().evaluate(snap(v), params())
    assert sig.kind is RequestType.EXIT and sig.rule_id == "EXIT-STOP"


def test_stop_has_priority_over_dead_cross() -> None:
    v = InstrumentView("005930", bars(FALLING), position=held(avg="90000"))
    [sig] = TrendSmaV1().evaluate(snap(v), params())
    assert sig.rule_id == "EXIT-STOP"


def test_hold_without_signal_in_uptrend_above_stop() -> None:
    v = InstrumentView("005930", bars(RISING), position=held(avg="55000"))
    assert TrendSmaV1().evaluate(snap(v), params()) == []


def test_exit_allowed_with_trading_status_but_not_when_pending() -> None:
    v = InstrumentView("005930", bars(FALLING), position=held(avg="10000"),
                       statuses=frozenset({TradingStatus.HALTED}))
    [sig] = TrendSmaV1().evaluate(snap(v), params())              # 위험 축소 신호는 낸다 (허용은 게이트 판단)
    assert sig.kind is RequestType.EXIT
    pend = InstrumentView("005930", bars(FALLING), position=held(avg="10000"), has_pending_order=True)
    assert TrendSmaV1().evaluate(snap(pend), params()) == []


def test_not_owned_position_gets_no_signal() -> None:
    other = PositionView(Decimal("10"), Decimal("10000"), 5, False)
    assert TrendSmaV1().evaluate(snap(InstrumentView("005930", bars(FALLING), position=other)), params()) == []
    assert TrendSmaV1().evaluate(snap(InstrumentView("005930", bars(RISING), position=other)), params()) == []


# --- 재진입 제한 --------------------------------------------------------------------------

def test_reentry_blocked_until_wait_bars_after_exit() -> None:
    last = len(RISING) - 1
    blocked = InstrumentView("005930", bars(RISING), last_exit_at=exit_at(last - 4), last_exit_reason="EXIT-STOP")
    assert TrendSmaV1().evaluate(snap(blocked), params()) == []            # 청산 뒤 확정 봉 4개 < 5
    ok = InstrumentView("005930", bars(RISING), last_exit_at=exit_at(last - 5), last_exit_reason="EXIT-STOP")
    [sig] = TrendSmaV1().evaluate(snap(ok), params())                       # 5개 → 진입 허용
    assert sig.rule_id == "ENTRY-1"


def test_reentry_block_applies_to_any_exit_reason_and_zero_disables() -> None:
    last = len(RISING) - 1
    v = InstrumentView("005930", bars(RISING), last_exit_at=exit_at(last), last_exit_reason=None)
    assert TrendSmaV1().evaluate(snap(v), params()) == []
    [sig] = TrendSmaV1().evaluate(snap(v), params(reentry_wait=0))
    assert sig.kind is RequestType.ENTRY


# --- 룩어헤드·결정성 -----------------------------------------------------------------------

def test_signal_depends_only_on_snapshot_prefix() -> None:
    """같은 과거(스냅샷)에 서로 다른 미래가 이어져도 그 시점 신호는 같다 — 전략은 스냅샷 밖을 보지 않는다."""
    up_future = RISING + [60000 + 500 * i for i in range(20)]
    crash_future = RISING + [30000.0] * 20
    s = TrendSmaV1()
    for k in range(60, len(RISING) + 1):
        a = s.evaluate(snap(InstrumentView("005930", bars(up_future)[:k])), params())
        b = s.evaluate(snap(InstrumentView("005930", bars(crash_future)[:k])), params())
        assert [(x.kind, x.rule_id, x.quantity, x.stop_price) for x in a] == \
               [(x.kind, x.rule_id, x.quantity, x.stop_price) for x in b]


def test_decision_uses_confirmed_close_not_last_price() -> None:
    """판정(진입 여부·손절가)은 확정 종가 기준. last_price(당일 시세)는 크기·기준가에만 쓴다."""
    s = TrendSmaV1()
    [a] = s.evaluate(snap(InstrumentView("005930", bars(RISING))), params())
    [b] = s.evaluate(snap(InstrumentView("005930", bars(RISING), last_price=Decimal("58500"))), params())
    assert a.rule_id == b.rule_id and a.stop_price == b.stop_price and b.price == Decimal("58500")


def test_deterministic() -> None:
    v = InstrumentView("005930", bars(RISING))
    s = TrendSmaV1()
    assert s.evaluate(snap(v), params()) == s.evaluate(snap(v), params())
