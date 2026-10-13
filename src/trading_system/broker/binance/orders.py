"""주문·취소·조회 요청 파라미터 (CP-024 D-104·D-110·D-111·D-118·D-120, BN §5·§7).

- 고객 주문 ID(D-110): 멱등키에서 결정적으로 만든다 — `M2-` + SHA-256 앞 32자(16진) = 35자.
  규칙은 FIX API 정규식 `^[a-zA-Z0-9-_]{1,36}$`을 보수 규칙으로 채택(REST 문서에 규칙 없음, B-U16 → PV-M2-09).
  같은 의도는 재시작 후에도 같은 ID, 다른 의도는 다른 ID. 서버 중복 거부를 멱등성으로 보지 않는다(B-U2).
- 주문 유형(D-104·D-120): 매수 = IOC 지정가만. 매도 = 수량 지정 시장가, 또는 시세 지연 중 IOC 지정가.
  GTC·quoteOrderQty·정정은 만들지 않는다. newOrderRespType = FULL.
- 값(D-118): 가격·수량은 게이트가 승인한 값을 그대로 보낸다. tick·step 배수가 아니면 OrderBuildError(전송 전 실패) —
  어댑터가 몰래 반올림하지 않는다. 문자열은 고정소수, 단위 자릿수(지수 표기 없음).
- 조회 창(D-111): allOrders·myTrades의 startTime~endTime은 24시간 이내(BN §7).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .numbers import PrecisionError, format_exact

CLIENT_ORDER_ID = re.compile(r"[A-Za-z0-9_-]{1,36}", re.ASCII)
CLIENT_ID_PREFIX = "M2-"
SYMBOL = re.compile(r"[A-Z0-9]{2,20}", re.ASCII)
MAX_WINDOW_MS = 86_400_000

Params = list[tuple[str, str]]


class OrderBuildError(ValueError):
    """요청을 만들 수 없음 → 전송 전 실패."""


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderKind(StrEnum):
    LIMIT_IOC = "LIMIT_IOC"
    MARKET = "MARKET"


@dataclass(frozen=True)
class NewOrder:
    symbol: str
    side: OrderSide
    kind: OrderKind
    quantity: Decimal
    price: Decimal | None
    client_order_id: str


def client_order_id(idem_key: str) -> str:
    if not isinstance(idem_key, str) or not idem_key:
        raise ValueError("멱등키가 비어 있다")
    return CLIENT_ID_PREFIX + hashlib.sha256(idem_key.encode("utf-8")).hexdigest()[:32]


def validate_client_order_id(value: object) -> str:
    if not isinstance(value, str) or not CLIENT_ORDER_ID.fullmatch(value):
        raise OrderBuildError("고객 주문 ID 형식 아님(1~36자 A-Za-z0-9_-)")
    return value


def _symbol(value: object) -> str:
    if not isinstance(value, str) or not SYMBOL.fullmatch(value):
        raise OrderBuildError("심볼 형식 아님")
    return value


def _exact(value: Decimal, step: Decimal | None, field: str) -> str:
    if step is None or not step.is_finite() or step <= 0:
        raise OrderBuildError(f"{field}: 단위 근거 없음(0·없음)")
    try:
        return format_exact(value, step, field)
    except PrecisionError as exc:
        raise OrderBuildError(str(exc)) from None


def new_order_params(order: NewOrder, *, qty_step: Decimal, tick: Decimal | None) -> Params:
    """신규 주문 파라미터. qty_step은 시장가면 MARKET_LOT_SIZE(0이면 호출자가 LOT_SIZE로 대체), 지정가면 LOT_SIZE."""
    symbol = _symbol(order.symbol)
    cid = validate_client_order_id(order.client_order_id)
    qty = _exact(order.quantity, qty_step, "quantity")
    params: Params = [("symbol", symbol), ("side", OrderSide(order.side).value)]
    if order.kind is OrderKind.LIMIT_IOC:
        if order.price is None:
            raise OrderBuildError("지정가 주문에 가격 없음")
        params += [("type", "LIMIT"), ("timeInForce", "IOC"), ("quantity", qty),
                   ("price", _exact(order.price, tick, "price"))]
    elif order.kind is OrderKind.MARKET:
        if order.side is not OrderSide.SELL:
            raise OrderBuildError("시장가 매수는 만들지 않는다(D-104)")
        if order.price is not None:
            raise OrderBuildError("시장가 주문에 가격 지정 불가")
        params += [("type", "MARKET"), ("quantity", qty)]
    else:  # pragma: no cover - 열거형 밖
        raise OrderBuildError("주문 유형 아님")
    return params + [("newClientOrderId", cid), ("newOrderRespType", "FULL")]


def _order_ref(order_id: int | None, client_id: str | None) -> Params:
    if order_id is not None:
        if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id < 0:
            raise OrderBuildError("orderId 형식 아님")
        return [("orderId", str(order_id))]
    if client_id is not None:
        return [("origClientOrderId", validate_client_order_id(client_id))]
    raise OrderBuildError("orderId 또는 고객 주문 ID 필요")


def cancel_params(symbol: str, *, order_id: int | None, client_order_id: str | None) -> Params:
    return [("symbol", _symbol(symbol)), *_order_ref(order_id, client_order_id)]


def query_order_params(symbol: str, *, order_id: int | None, client_order_id: str | None) -> Params:
    """주문 조회: orderId가 있으면 orderId(심볼과 함께), 없으면 origClientOrderId (D-111)."""
    return [("symbol", _symbol(symbol)), *_order_ref(order_id, client_order_id)]


def symbol_params(symbol: str) -> Params:
    """심볼 하나만 받는 조회(exchangeInfo·openOrders, T-M2-14)."""
    return [("symbol", _symbol(symbol))]


def window_params(symbol: str, *, start_ms: int, end_ms: int) -> Params:
    if end_ms < start_ms or end_ms - start_ms > MAX_WINDOW_MS:
        raise OrderBuildError("조회 창은 24시간 이내, 시작 ≤ 끝")
    return [("symbol", _symbol(symbol)), ("startTime", str(int(start_ms))), ("endTime", str(int(end_ms)))]
