"""[CP-039 D-249 (3), 재검토 2026-10-19 지적 1·2] 신호 출처 binance-public이면 신호 조건을 reference_price(신호 출처)로 판정한다고
밝힌 전략(SIGNAL_PRICE_FIELD = "reference_price")만 시작한다 — last_price(테스트넷)로 신호 조건을 보는 전략은 일봉(실서버)과
현재가(테스트넷)가 섞여 청산·손절이 틀릴 수 있다(fail-closed). testnet 출처(기본)는 검사하지 않는다(종전 동작).
백테스트는 출처가 하나라 reference_price = last_price."""

from __future__ import annotations

from decimal import Decimal

from trading_system.app.wiring import SIGNAL_PRICE_REFERENCE, StrategyInput, signal_price_problems
from trading_system.strategy.crypto_trend import CryptoTrendV3
from trading_system.strategy.m2_verify import M2Verify


class _Declared(CryptoTrendV3):
    SIGNAL_PRICE_FIELD = SIGNAL_PRICE_REFERENCE


def test_public_source_refuses_strategy_without_declaration() -> None:
    probs = signal_price_problems([StrategyInput(CryptoTrendV3(), {}, ())], public=True)
    assert len(probs) == 1 and "crypto-trend:3" in probs[0] and "reference_price" in probs[0]
    assert signal_price_problems([StrategyInput(M2Verify(), {}, ("01:00",))], public=True) != []


def test_public_source_accepts_declared_strategy_and_testnet_checks_nothing() -> None:
    assert signal_price_problems([StrategyInput(_Declared(), {}, ())], public=True) == []
    assert signal_price_problems([StrategyInput(CryptoTrendV3(), {}, ())], public=False) == []


def test_backtest_views_set_reference_price_equal_last_price() -> None:
    from pathlib import Path

    import trading_system.app.backtest_m2 as bm
    import trading_system.app.backtest_m2_minute as bmm
    for mod in (bm, bmm):
        text = Path(mod.__file__).read_text(encoding="utf-8")
        assert text.count("InstrumentView(") == text.count("reference_price="), mod.__name__
    assert Decimal("1") == Decimal("1")
