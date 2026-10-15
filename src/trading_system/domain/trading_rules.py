"""현물 심볼 거래 규칙 값 객체·필터 사전 검사·M2 주문 유형·가격 규칙 (CP-024 D-104·D-106·D-120, T-M2-03).

- 규칙 값은 거래소 규칙 조회(exchangeInfo, T-M2-11)가 채운다. 여기서는 형식과 판정 규칙만 정한다. 값은 Decimal.
- 필터 규칙(BN §6 기록 문구): PRICE_FILTER price ≥ minPrice, ≤ maxPrice, price % tickSize == 0 (값 0이면 비활성) /
  LOT_SIZE quantity ≥ minQty, ≤ maxQty, quantity % stepSize == 0 / MARKET_LOT_SIZE 시장가에 같은 형태 /
  MIN_NOTIONAL·NOTIONAL price × quantity ≥ minNotional(≤ maxNotional), 시장가는 applyMinToMarket·applyMaxToMarket.
  [미확인] BN §6 LOT_SIZE 행은 "원문 대조 권장" 표시가 있다. 원문이 (quantity − minQty) % stepSize 형태라면
  minQty가 stepSize 배수인 한 결과가 같다 — 시작 점검(T-M2-11/14)에서 minQty·minPrice가 각 단위의 배수인지 확인할 것.
- 판정 방향(D-106): 위험 확대(매수)는 보수적으로(주문가 기준, 경계면 거부), 위험 축소(매도)는 확실한 위반만 거부.
  시장가 매도 명목가는 거래소가 참조가·평균가로 계산하므로(BN §6) 호출자가 준 참조가들(최근가·avgPrice·referencePrice)
  모두로도 위반일 때만 거부한다. 참조가를 모르면 미리 막지 않는다(거래소 -1013은 명시 거부, D-112).
- M2 주문 유형(D-104·D-120): 매수 = IOC 지정가(최근가 × (1 + 완충)를 tickSize로 **내림**), 매도 = 시세 정상이면 시장가,
  시세 지연(신선도 초과)이면 IOC 지정가(마지막 정상 시세 × (1 − 완충)를 tickSize로 **올림**). 유형·가격은 의도를 만들 때
  (게이트 전) 정하고 게이트는 요청을 바꾸지 않는다(00 D-009). 시장가 매수·정정은 M2에서 쓰지 않는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum

from .enums import OrderType, Side
from .market import Quote
from .models import GateRequest

_ZERO = Decimal("0")


def _dec(name: str, value: object, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name}은 Decimal이어야 한다 (float 금지)")
    if not value.is_finite():
        raise ValueError(f"{name}이 유한한 값이 아니다")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{name}은 {'0보다 커야' if positive else '0 이상이어야'} 한다")
    return value


@dataclass(frozen=True)
class SymbolRules:
    """심볼 하나의 거래 규칙 (D-106). 0 = 비활성인 필드는 주석에 표시. 값은 가상 또는 거래소 조회값만."""

    instrument: str
    tick_size: Decimal          # PRICE_FILTER tickSize (0 = 비활성)
    min_price: Decimal          # PRICE_FILTER minPrice (0 = 비활성)
    max_price: Decimal          # PRICE_FILTER maxPrice (0 = 비활성)
    lot_min_qty: Decimal        # LOT_SIZE minQty
    lot_max_qty: Decimal        # LOT_SIZE maxQty (0 = 비활성)
    lot_step: Decimal           # LOT_SIZE stepSize (> 0)
    market_min_qty: Decimal     # MARKET_LOT_SIZE minQty
    market_max_qty: Decimal     # MARKET_LOT_SIZE maxQty (0 = 비활성)
    market_step: Decimal        # MARKET_LOT_SIZE stepSize (0이면 시장가 수량 단위 = LOT_SIZE stepSize, CP-036 D-208)
    min_notional: Decimal       # NOTIONAL/MIN_NOTIONAL minNotional
    max_notional: Decimal | None  # NOTIONAL maxNotional (없으면 None)
    apply_min_to_market: bool
    apply_max_to_market: bool

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in ("instrument", "apply_min_to_market", "apply_max_to_market"):
                continue
            if f.name == "max_notional" and value is None:
                continue
            _dec(f.name, value, positive=f.name == "lot_step")
        if not self.instrument:
            raise ValueError("instrument가 비어 있다")


def _range_problem(name: str, value: Decimal, low: Decimal, high: Decimal, step: Decimal) -> str | None:
    if value < low:
        return f"{name}: {value} < 최소 {low}"
    if high > 0 and value > high:
        return f"{name}: {value} > 최대 {high}"
    if step > 0 and value % step != 0:
        return f"{name}: {value}이 단위 {step}의 배수가 아님"
    return None


def filter_violation(rules: SymbolRules, side: Side, order_type: OrderType, quantity: Decimal, price: Decimal,
                     market_prices: Sequence[Decimal] = ()) -> str | None:
    """주문 전 필터 사전 검사 (D-106, V-133). 위반이면 사유 문자열, 통과면 None.

    market_prices: 시장가 명목가 판정용 참조가들(최근가·avgPrice·referencePrice 중 아는 것). 지정가는 주문가를 쓴다.
    """
    buying = side is Side.BUY
    if order_type is OrderType.MARKET:
        if buying:
            return "M2 매수는 IOC 지정가만 허용 (시장가 매수 없음, D-104)"
        p = _range_problem("MARKET_LOT_SIZE", quantity, rules.market_min_qty, rules.market_max_qty,
                           market_qty_step(rules))   # step 0 → LOT_SIZE 단위 (D-208, 어댑터와 같은 규칙)
        if p:
            return p
        prices = [x for x in market_prices if x > 0]
        if prices and rules.apply_min_to_market and max(prices) * quantity < rules.min_notional:
            return f"NOTIONAL: 시장가 명목가 최대 {max(prices) * quantity} < 최소 {rules.min_notional} (확실한 위반)"
        if prices and rules.apply_max_to_market and rules.max_notional is not None \
                and min(prices) * quantity > rules.max_notional:
            return f"NOTIONAL: 시장가 명목가 최소 {min(prices) * quantity} > 최대 {rules.max_notional} (확실한 위반)"
        return None

    if rules.min_price > 0 and price < rules.min_price:
        return f"PRICE_FILTER: {price} < 최소 {rules.min_price}"
    if rules.max_price > 0 and price > rules.max_price:
        return f"PRICE_FILTER: {price} > 최대 {rules.max_price}"
    if rules.tick_size > 0 and price % rules.tick_size != 0:
        return f"PRICE_FILTER: {price}이 tickSize {rules.tick_size}의 배수가 아님"
    p = _range_problem("LOT_SIZE", quantity, rules.lot_min_qty, rules.lot_max_qty, rules.lot_step)
    if p:
        return p
    notional = price * quantity
    if buying:  # 위험 확대: 경계면 거부 (D-106)
        if notional <= rules.min_notional:
            return f"NOTIONAL: 매수 명목가 {notional} ≤ 최소 {rules.min_notional} (경계 포함 거부)"
        if rules.max_notional is not None and notional >= rules.max_notional:
            return f"NOTIONAL: 매수 명목가 {notional} ≥ 최대 {rules.max_notional} (경계 포함 거부)"
        return None
    if notional < rules.min_notional:  # 위험 축소: 확실한 위반만
        return f"NOTIONAL: 매도 명목가 {notional} < 최소 {rules.min_notional}"
    if rules.max_notional is not None and notional > rules.max_notional:
        return f"NOTIONAL: 매도 명목가 {notional} > 최대 {rules.max_notional}"
    return None


def _to_tick(value: Decimal, rules: SymbolRules, rounding: str) -> Decimal:
    if rules.tick_size == 0:
        return value
    return (value / rules.tick_size).to_integral_value(rounding=rounding) * rules.tick_size


def _check_price_inputs(last: Decimal, buffer: Decimal) -> None:
    _dec("last", last, positive=True)
    _dec("buffer", buffer)
    if buffer >= 1:
        raise ValueError("buffer는 1보다 작아야 한다")


def ioc_buy_price(last: Decimal, rules: SymbolRules, buffer: Decimal) -> Decimal:
    """IOC 지정가 매수 가격 = 최근가 × (1 + 완충)을 tickSize로 내림 (D-104: 가격 상한을 넘지 않는 쪽)."""
    _check_price_inputs(last, buffer)
    return _to_tick(last * (1 + buffer), rules, ROUND_FLOOR)


def stale_sell_price(last_good: Decimal, rules: SymbolRules, buffer: Decimal) -> Decimal:
    """시세 지연 중 IOC 지정가 매도 가격 = 마지막 정상 시세 × (1 − 완충)을 tickSize로 올림 (D-120: 하한을 계산값 아래로 내리지 않음)."""
    _check_price_inputs(last_good, buffer)
    return _to_tick(last_good * (1 - buffer), rules, ROUND_CEILING)


@dataclass(frozen=True)
class SpotOrderPolicy:
    """M2 주문 가격 규칙 값 (A-064: 매수 완충 0.5%, 시세 신선도 60초; A-065 Q4: 지연 매도 완충 0.5%)."""

    buy_buffer: Decimal
    sell_buffer: Decimal
    quote_max_age: timedelta

    def __post_init__(self) -> None:
        for n in ("buy_buffer", "sell_buffer"):
            v = _dec(n, getattr(self, n))
            if v >= 1:
                raise ValueError(f"{n}는 1보다 작아야 한다")
        if self.quote_max_age <= timedelta(0):
            raise ValueError("quote_max_age는 0보다 커야 한다")


def quote_is_fresh(quote: Quote, now: datetime, max_age: timedelta) -> bool:
    """게이트 G11과 같은 기준 (gate._fresh_quote): 오래됐거나 미래 시각이 허용치를 넘으면 지연."""
    return not (now - quote.at > max_age or quote.at > now + max_age)


def spot_order_terms(side: Side, quote: Quote | None, now: datetime, rules: SymbolRules | None,
                     policy: SpotOrderPolicy) -> tuple[OrderType, Decimal] | None:
    """M2 주문 유형·가격 (의도 생성 시, 게이트 전). 정할 수 없으면 None — 호출자는 의도를 만들지 않고 기록한다."""
    if quote is None or quote.price <= 0 or rules is None:
        return None
    if side is Side.BUY:
        return OrderType.LIMIT, ioc_buy_price(quote.price, rules, policy.buy_buffer)
    if quote_is_fresh(quote, now, policy.quote_max_age):
        return OrderType.MARKET, quote.price
    return OrderType.LIMIT, stale_sell_price(quote.price, rules, policy.sell_buffer)


class DustVerdict(StrEnum):
    """dust 판정 (D-102). SEND = 전송 대상(경계 포함), DUST_* = 전송 불가가 확실(포지션 없음 취급)."""

    SEND = "SEND"
    DUST_QUANTITY = "DUST_QUANTITY"   # (가) 청산 가능 수량 < minQty
    DUST_NOTIONAL = "DUST_NOTIONAL"   # (나) 명목가가 확실히 minNotional 미만


@dataclass(frozen=True)
class DustCheck:
    verdict: DustVerdict
    quantity: Decimal          # 청산 가능 수량 (보유를 매도 경로의 stepSize로 내림)
    notional_boundary: bool    # 금액 경계(가장 낮은 참조가로는 미만이거나 참조가 모름) — 전송 뒤 -1013이면 dust 확정


def market_qty_step(rules: SymbolRules) -> Decimal:
    """시장가 매도 수량 단위 (CP-036 D-208, A-106 Q-4 (가)): MARKET_LOT_SIZE stepSize, 0이면 LOT_SIZE stepSize.

    어댑터 주문 문자열(broker/binance/rules.SymbolRules.market_qty_step)·G10·청산 가능 수량·dust 판정·M2 백테스트가
    같은 규칙을 쓴다. 내림 잔량은 dust(D-102). step 0의 거래소 의미는 미확인(B-6) — PV-M2-08 실측이 다르면 CP."""
    return rules.market_step if rules.market_step > 0 else rules.lot_step


def sellable_quantity(held: Decimal, rules: SymbolRules, *, stale: bool) -> Decimal:
    """청산 가능 수량 = 보유를 실제로 보낼 매도 경로의 stepSize로 내림 (D-102 (가), D-118 수량 내림).

    평상시 = 시장가 → market_qty_step(MARKET_LOT_SIZE stepSize, 0이면 LOT_SIZE stepSize — CP-036 D-208, A-106 Q-4 (가)),
    시세 지연 = IOC 지정가 → LOT_SIZE. 내림 잔량은 dust(D-102)."""
    step = rules.lot_step if stale else market_qty_step(rules)
    if held <= 0:
        return _ZERO
    return (held / step).to_integral_value(rounding=ROUND_FLOOR) * step


def dust_verdict(held: Decimal, rules: SymbolRules, *, stale: bool, market_prices: Sequence[Decimal],
                 stale_price: Decimal | None) -> DustCheck:
    """dust 판정 (D-102, V-131·V-160). 거래소 규칙 그대로(완충 차감 없음), 실제로 보낼 매도 경로의 필터로.

    - 평상시(시장가): MARKET_LOT_SIZE minQty, 명목가 = 참조가(최근가·avgPrice·referencePrice) — 거래소가 쓰는 기준가를
      우리가 같게 얻는다는 근거가 없으므로 **가장 높은** 참조가로도 minNotional 미만일 때만 dust(확실). applyMinToMarket이
      거짓이면 시장가 명목가 최소를 적용하지 않는다.
    - 시세 지연(IOC 지정가, D-120): LOT_SIZE minQty, 명목가 = 주문가 × 수량(거래소와 같은 값). 주문가를 모르면 금액 판정 안 함.
    - 그 밖(경계 포함)은 SEND — 위험 축소를 미리 막지 않는다(CLAUDE.md §8-3). notional_boundary는 가장 낮은 참조가로는
      미만이거나 참조가를 모를 때 참 — 전송 뒤 -1013(필터 거부)을 받으면 dust로 확정하는 근거로 쓴다.
    """
    qty = sellable_quantity(held, rules, stale=stale)
    min_qty = rules.lot_min_qty if stale else rules.market_min_qty
    if qty <= 0 or qty < min_qty:
        return DustCheck(DustVerdict.DUST_QUANTITY, qty, False)
    if stale:
        if stale_price is None or stale_price <= 0:
            return DustCheck(DustVerdict.SEND, qty, True)
        if stale_price * qty < rules.min_notional:
            return DustCheck(DustVerdict.DUST_NOTIONAL, qty, False)
        return DustCheck(DustVerdict.SEND, qty, False)
    if not rules.apply_min_to_market:
        return DustCheck(DustVerdict.SEND, qty, False)
    prices = [p for p in market_prices if p > 0]
    if not prices:
        return DustCheck(DustVerdict.SEND, qty, True)
    if max(prices) * qty < rules.min_notional:
        return DustCheck(DustVerdict.DUST_NOTIONAL, qty, False)
    return DustCheck(DustVerdict.SEND, qty, min(prices) * qty < rules.min_notional)


def apply_spot_terms(req: GateRequest, quote: Quote | None, now: datetime, rules: SymbolRules | None,
                     policy: SpotOrderPolicy) -> GateRequest | None:
    """요청 원형에 M2 주문 유형·가격을 넣은 새 요청. 신규 주문(진입·증액·감액·청산)만 대상."""
    if not req.request_type.is_new_order:
        raise ValueError("M2 주문 유형·가격 규칙은 신규 주문 요청에만 적용한다 (취소는 그대로, 정정 미지원)")
    t = spot_order_terms(req.side, quote, now, rules, policy)
    if t is None:
        return None
    return replace(req, order_type=t[0], price=t[1])
