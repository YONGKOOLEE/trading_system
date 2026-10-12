"""주문 요청 파라미터 (CP-024 D-104·D-110·D-118, V-139(ID 부분)·V-156).

- 고객 주문 ID: 멱등키에서 결정적, 1~36자 `[A-Za-z0-9_-]` (BN B-U16, FIX 정규식 보수 채택).
- 유형: 매수 = IOC 지정가만, 매도 = 시장가 또는 IOC 지정가. GTC·quoteOrderQty 없음. newOrderRespType = FULL.
- 어댑터는 승인된 가격·수량을 바꾸지 않는다: tick·step 배수가 아니면 OrderBuildError(전송 전 실패).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_system.broker.binance import orders as o


def test_client_order_id_deterministic_and_valid() -> None:
    a = o.client_order_id("intent-2026-10-07-BTCUSDT-entry-1")
    assert a == o.client_order_id("intent-2026-10-07-BTCUSDT-entry-1")     # 재시작 후 같은 의도 = 같은 ID
    assert a != o.client_order_id("intent-2026-10-07-BTCUSDT-entry-2")     # 다른 의도 = 다른 ID
    assert o.CLIENT_ORDER_ID.fullmatch(a) and 1 <= len(a) <= 36


def test_client_order_id_rejects_empty_key() -> None:
    with pytest.raises(ValueError):
        o.client_order_id("")


@pytest.mark.parametrize("bad", ["", "a" * 37, "has space", "한글", "a.b", "x/y"])
def test_validate_client_order_id(bad: str) -> None:
    with pytest.raises(o.OrderBuildError):
        o.validate_client_order_id(bad)


def _buy(**kw: object) -> o.NewOrder:
    base = dict(symbol="BTCUSDT", side=o.OrderSide.BUY, kind=o.OrderKind.LIMIT_IOC,
                quantity=Decimal("0.00123"), price=Decimal("61234.56"), client_order_id="M2-abc")
    base.update(kw)
    return o.NewOrder(**base)  # type: ignore[arg-type]


def test_ioc_limit_buy_params() -> None:
    p = dict(o.new_order_params(_buy(), qty_step=Decimal("0.00001000"), tick=Decimal("0.01000000")))
    assert p == {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "timeInForce": "IOC",
                 "quantity": "0.00123", "price": "61234.56", "newClientOrderId": "M2-abc",
                 "newOrderRespType": "FULL"}


def test_market_sell_params_have_no_price_or_tif() -> None:
    order = _buy(side=o.OrderSide.SELL, kind=o.OrderKind.MARKET, price=None, quantity=Decimal("0.5"))
    p = dict(o.new_order_params(order, qty_step=Decimal("0.1"), tick=None))
    assert p == {"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quantity": "0.5",
                 "newClientOrderId": "M2-abc", "newOrderRespType": "FULL"}
    assert "quoteOrderQty" not in p


def test_ioc_limit_sell_allowed_for_stale_quote_exit() -> None:
    order = _buy(side=o.OrderSide.SELL, price=Decimal("60000.10"))
    p = dict(o.new_order_params(order, qty_step=Decimal("0.00001"), tick=Decimal("0.01")))
    assert p["type"] == "LIMIT" and p["timeInForce"] == "IOC" and p["side"] == "SELL"


@pytest.mark.parametrize(("order", "qty_step", "tick"), [
    (_buy(side=o.OrderSide.BUY, kind=o.OrderKind.MARKET, price=None), Decimal("0.00001"), None),  # 시장가 매수 금지(D-104)
    (_buy(price=None), Decimal("0.00001"), Decimal("0.01")),                    # 지정가에 가격 없음
    (_buy(quantity=Decimal("0.001234")), Decimal("0.00001"), Decimal("0.01")),  # step 배수 아님 → 반올림 안 함
    (_buy(price=Decimal("61234.567")), Decimal("0.00001"), Decimal("0.01")),    # tick 배수 아님
    (_buy(quantity=Decimal("0")), Decimal("0.00001"), Decimal("0.01")),
    (_buy(price=Decimal("-1")), Decimal("0.00001"), Decimal("0.01")),
    (_buy(symbol="btc/usdt"), Decimal("0.00001"), Decimal("0.01")),
    (_buy(client_order_id="bad id"), Decimal("0.00001"), Decimal("0.01")),
    (_buy(), Decimal("0"), Decimal("0.01")),                                    # step 0 = 근거 없음
    (_buy(side=o.OrderSide.SELL, kind=o.OrderKind.MARKET, price=Decimal("1")), Decimal("0.00001"), None),
])
def test_invalid_orders_fail_before_send(order: o.NewOrder, qty_step: Decimal, tick: Decimal | None) -> None:
    with pytest.raises(o.OrderBuildError):
        o.new_order_params(order, qty_step=qty_step, tick=tick)


def test_cancel_params_by_order_id_or_client_id() -> None:
    assert dict(o.cancel_params("BTCUSDT", order_id=12, client_order_id=None)) == {"symbol": "BTCUSDT", "orderId": "12"}
    assert dict(o.cancel_params("BTCUSDT", order_id=None, client_order_id="M2-x")) == {
        "symbol": "BTCUSDT", "origClientOrderId": "M2-x"}
    with pytest.raises(o.OrderBuildError):
        o.cancel_params("BTCUSDT", order_id=None, client_order_id=None)


def test_query_params_prefer_order_id() -> None:
    # D-111: orderId가 있으면 orderId(심볼과 함께), 없으면 origClientOrderId
    assert dict(o.query_order_params("BTCUSDT", order_id=7, client_order_id="M2-x")) == {
        "symbol": "BTCUSDT", "orderId": "7"}
    assert dict(o.query_order_params("BTCUSDT", order_id=None, client_order_id="M2-x")) == {
        "symbol": "BTCUSDT", "origClientOrderId": "M2-x"}


def test_window_params_limited_to_24_hours() -> None:
    p = dict(o.window_params("BTCUSDT", start_ms=1_000, end_ms=1_000 + 86_400_000))
    assert p == {"symbol": "BTCUSDT", "startTime": "1000", "endTime": str(1_000 + 86_400_000)}
    with pytest.raises(o.OrderBuildError):
        o.window_params("BTCUSDT", start_ms=1_000, end_ms=1_001 + 86_400_000)
    with pytest.raises(o.OrderBuildError):
        o.window_params("BTCUSDT", start_ms=5, end_ms=4)
