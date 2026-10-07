"""거래 규칙 값 객체·필터 사전 검사·M2 주문 가격 규칙 (CP-024 D-104·D-106·D-120, T-M2-03).

V-133(PRICE_FILTER·LOT_SIZE·MARKET_LOT_SIZE·NOTIONAL 위반), V-154(시세 지연 매도 가격 = 마지막 정상 시세 × 0.995, tick 올림),
D-104(매수 가격 = 최근가 × 1.005, tick 내림). 규칙 값은 모두 가상이다(바이낸스 실제 값 미확인 B-U4).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_system.domain.enums import OrderType, Side
from trading_system.domain.market import Quote
from trading_system.domain.trading_rules import (
    SpotOrderPolicy, SymbolRules, filter_violation, ioc_buy_price, spot_order_terms, stale_sell_price,
)

D = Decimal
NOW = datetime(2026, 10, 7, 1, 0, tzinfo=UTC)
RULES = SymbolRules(
    instrument="TSTA-USDT", tick_size=D("0.01"), min_price=D("0.01"), max_price=D("1000000"),
    lot_min_qty=D("0.0001"), lot_max_qty=D("9000"), lot_step=D("0.0001"),
    market_min_qty=D("0.001"), market_max_qty=D("100"), market_step=D("0.001"),
    min_notional=D("5"), max_notional=D("50000"), apply_min_to_market=True, apply_max_to_market=True,
)
POLICY = SpotOrderPolicy(buy_buffer=D("0.005"), sell_buffer=D("0.005"), quote_max_age=timedelta(seconds=60))


def v(side: Side, otype: OrderType, qty: str, price: str, prices: tuple[str, ...] = ()) -> str | None:
    return filter_violation(RULES, side, otype, D(qty), D(price), tuple(D(p) for p in prices))


# --- 값 객체 ------------------------------------------------------------------------------
def test_rules_reject_float_and_bad_values() -> None:
    with pytest.raises(TypeError):
        SymbolRules(**{**RULES.__dict__, "tick_size": 0.01})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SymbolRules(**{**RULES.__dict__, "lot_step": D("0")})
    with pytest.raises(ValueError):
        SymbolRules(**{**RULES.__dict__, "min_notional": D("-1")})


# --- 지정가 (매수 = 위험 확대: 보수, 경계면 거부) ---------------------------------------------
def test_limit_buy_within_rules_passes() -> None:
    assert v(Side.BUY, OrderType.LIMIT, "0.0010", "10050.00") is None


@pytest.mark.parametrize("qty,price,key", [
    ("0.00105", "10050.00", "LOT_SIZE"),      # stepSize 배수 아님
    ("0.00001", "10050.00", "LOT_SIZE"),      # minQty 미만
    ("9001", "1.00", "LOT_SIZE"),             # maxQty 초과
    ("0.0010", "10050.005", "PRICE_FILTER"),  # tickSize 배수 아님
    ("1", "0.001", "PRICE_FILTER"),           # minPrice 미만
    ("0.0001", "1000001", "PRICE_FILTER"),    # maxPrice 초과
    ("0.0004", "10000.00", "NOTIONAL"),       # 4 < 5
    ("0.0005", "10000.00", "NOTIONAL"),       # 매수 경계 = 5 → 보수 거부 (D-106)
    ("5", "10000.00", "NOTIONAL"),            # 50000 = maxNotional 경계 → 매수 거부
])
def test_limit_buy_violations(qty: str, price: str, key: str) -> None:
    reason = v(Side.BUY, OrderType.LIMIT, qty, price)
    assert reason is not None and key in reason


# --- 지정가 매도 (시세 지연 중 감액·청산, D-120: 확실한 위반만 거부) ---------------------------------
def test_limit_sell_notional_boundary_passes_but_below_rejected() -> None:
    assert v(Side.SELL, OrderType.LIMIT, "0.0005", "10000.00") is None          # = 5 → 거래소 규칙상 허용
    assert "NOTIONAL" in (v(Side.SELL, OrderType.LIMIT, "0.0004", "10000.00") or "")
    assert v(Side.SELL, OrderType.LIMIT, "5", "10000.00") is None               # = max 경계 허용
    assert "LOT_SIZE" in (v(Side.SELL, OrderType.LIMIT, "0.00015", "10000.00") or "")


# --- 시장가 매도 (MARKET_LOT_SIZE, 시장가 명목가는 참조가들로 확실한 위반만) -----------------------------
def test_market_sell_uses_market_lot_size() -> None:
    assert v(Side.SELL, OrderType.MARKET, "0.001", "10000", ("10000",)) is None
    # LOT_SIZE에는 맞지만 MARKET_LOT_SIZE step(0.001)에 안 맞음
    assert "MARKET_LOT_SIZE" in (v(Side.SELL, OrderType.MARKET, "0.0015", "10000", ("10000",)) or "")
    assert "MARKET_LOT_SIZE" in (v(Side.SELL, OrderType.MARKET, "101", "10", ("10",)) or "")


def test_market_sell_min_notional_only_certain_violation() -> None:
    # 최대 참조가 기준으로도 5 미만 → 확실한 위반
    assert "NOTIONAL" in (v(Side.SELL, OrderType.MARKET, "0.001", "1000", ("1000", "4000")) or "")
    # 참조가 하나라도 경계 이상이면 전송(거래소 -1013으로 확정, D-102·D-106)
    assert v(Side.SELL, OrderType.MARKET, "0.001", "1000", ("1000", "5000")) is None
    # 참조가를 모르면 미리 막지 않는다 (위험 축소)
    assert v(Side.SELL, OrderType.MARKET, "0.001", "1000", ()) is None


def test_market_sell_max_notional_only_certain_violation() -> None:
    assert "NOTIONAL" in (v(Side.SELL, OrderType.MARKET, "100", "600", ("600", "700")) or "")   # 최소 60000 > 50000
    assert v(Side.SELL, OrderType.MARKET, "100", "400", ("400", "600")) is None


def test_market_flags_disable_market_notional() -> None:
    rules = SymbolRules(**{**RULES.__dict__, "apply_min_to_market": False})
    assert filter_violation(rules, Side.SELL, OrderType.MARKET, D("0.001"), D("1"), (D("1"),)) is None


def test_market_buy_not_allowed_in_m2() -> None:
    assert "D-104" in (v(Side.BUY, OrderType.MARKET, "0.001", "10000", ("10000",)) or "")


# --- 가격 규칙 ----------------------------------------------------------------------------
def test_ioc_buy_price_floors_to_tick() -> None:
    # 10000.00 × 1.005 = 10050.0000 → 10050.00 / 1234.57 × 1.005 = 1240.74285 → 1240.74 (내림)
    assert ioc_buy_price(D("10000.00"), RULES, D("0.005")) == D("10050.00")
    assert ioc_buy_price(D("1234.57"), RULES, D("0.005")) == D("1240.74")


def test_stale_sell_price_ceils_to_tick() -> None:
    # 1234.57 × 0.995 = 1228.39715 → 1228.40 (올림, 하한을 계산값 아래로 내리지 않음 D-120)
    assert stale_sell_price(D("1234.57"), RULES, D("0.005")) == D("1228.40")
    assert stale_sell_price(D("10000.00"), RULES, D("0.005")) == D("9950.00")


def test_price_rules_reject_nonpositive_last() -> None:
    with pytest.raises(ValueError):
        ioc_buy_price(D("0"), RULES, D("0.005"))
    with pytest.raises(ValueError):
        stale_sell_price(D("-1"), RULES, D("0.005"))


# --- 의도 생성 시 주문 유형·가격 결정 (D-104·D-120: 게이트 전에 정하고 게이트는 바꾸지 않는다) -------------
def quote(age_s: int, price: str = "1234.57") -> Quote:
    return Quote("TSTA-USDT", D(price), NOW - timedelta(seconds=age_s))


def test_terms_buy_is_ioc_limit_with_buffer() -> None:
    assert spot_order_terms(Side.BUY, quote(0), NOW, RULES, POLICY) == (OrderType.LIMIT, D("1240.74"))


def test_terms_sell_fresh_is_market() -> None:
    assert spot_order_terms(Side.SELL, quote(60), NOW, RULES, POLICY) == (OrderType.MARKET, D("1234.57"))


def test_terms_sell_stale_is_ioc_limit_below_last_good() -> None:
    assert spot_order_terms(Side.SELL, quote(61), NOW, RULES, POLICY) == (OrderType.LIMIT, D("1228.40"))


def test_terms_without_quote_or_rules_is_none() -> None:
    assert spot_order_terms(Side.SELL, None, NOW, RULES, POLICY) is None
    assert spot_order_terms(Side.BUY, None, NOW, RULES, POLICY) is None
    assert spot_order_terms(Side.BUY, quote(0), NOW, None, POLICY) is None
