"""전략 후보(제안, 미등록) 단위 시험 (T-018 준비, A-044 3단계): 명세·파라미터 검증, 신호 형식, 크기 상한."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.config.capabilities import load_capabilities
from trading_system.config.limits_file import load_limits_file
from trading_system.config.strategy_params import load_params_file
from trading_system.config.strategy_registry import validate_spec
from trading_system.domain.enums import Mode, RequestType
from trading_system.domain.strategy import Bar, EvaluationSnapshot, InstrumentView, PositionView
from trading_system.strategy.candidates import L7_AMOUNT, WORST_PRICE_FACTOR, Breakout, MeanRevRSI, TrendSMA

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 5, 0, 30, tzinfo=UTC)
CASES = [(TrendSMA, "cand-trend-sma-v1.toml"), (Breakout, "cand-breakout-v1.toml"),
         (MeanRevRSI, "cand-meanrev-rsi-v1.toml")]


def bars(closes: list[float]) -> tuple[Bar, ...]:
    d0 = date(2026, 1, 1)
    return tuple(Bar(d0 + timedelta(days=i), Decimal(str(c)), Decimal(str(c * 1.01)), Decimal(str(c * 0.99)),
                     Decimal(str(c)), Decimal("1000")) for i, c in enumerate(closes))


def snap(v: InstrumentView) -> EvaluationSnapshot:
    return EvaluationSnapshot("ev", "M1", "KR-BT-01", T0, Decimal("100000000"), (v,))


@pytest.mark.parametrize("cls,params", CASES)
def test_spec_and_params_validate_for_backtest(cls, params) -> None:  # type: ignore[no-untyped-def]
    s = cls().spec
    caps = load_capabilities(ROOT / "docs" / "capabilities.md")
    limits = load_limits_file(ROOT / "config" / "limits.backtest.toml").limits
    assert validate_spec(s, caps, limits, Mode.BACKTEST) == []
    assert load_params_file(s, ROOT / "config" / "strategies" / params)
    assert "제안" in s.description                                  # 미등록 표시


def _params(cls, name):  # type: ignore[no-untyped-def]
    return load_params_file(cls().spec, ROOT / "config" / "strategies" / name)


def test_trend_entry_has_stop_and_size_within_l7_at_worst_price() -> None:
    closes = [50000 + 100 * i for i in range(80)]                    # 꾸준한 상승 → SMA20 > SMA60
    [sig] = TrendSMA().evaluate(snap(InstrumentView("005930", bars(closes))), _params(TrendSMA, CASES[0][1]))
    assert sig.kind is RequestType.ENTRY and sig.stop_price is not None and sig.stop_price < sig.price
    assert sig.quantity * sig.price * WORST_PRICE_FACTOR <= L7_AMOUNT


def test_breakout_exit_on_new_low_when_held() -> None:
    closes = [60000.0] * 30 + [50000.0]
    pos = PositionView(Decimal("10"), Decimal("60000"), 5, True)
    [sig] = Breakout().evaluate(snap(InstrumentView("005930", bars(closes), position=pos)),
                                _params(Breakout, CASES[1][1]))
    assert sig.kind is RequestType.EXIT and sig.quantity == Decimal("10")


def test_meanrev_no_entry_below_trend_and_stop_exit() -> None:
    falling = [80000 - 100 * i for i in range(120)]
    assert MeanRevRSI().evaluate(snap(InstrumentView("005930", bars(falling))), _params(MeanRevRSI, CASES[2][1])) == []
    pos = PositionView(Decimal("5"), Decimal("80000"), 1, True)
    [sig] = MeanRevRSI().evaluate(snap(InstrumentView("005930", bars(falling), position=pos)),
                                  _params(MeanRevRSI, CASES[2][1]))
    assert sig.kind is RequestType.EXIT and sig.rule_id == "EXIT-STOP"


def test_no_signal_when_data_bad_or_pending() -> None:
    closes = [50000 + 100 * i for i in range(80)]
    p = _params(TrendSMA, CASES[0][1])
    assert TrendSMA().evaluate(snap(InstrumentView("005930", bars(closes), data_ok=False)), p) == []
    assert TrendSMA().evaluate(snap(InstrumentView("005930", bars(closes), has_pending_order=True)), p) == []
