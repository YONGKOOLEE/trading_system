"""M2(크립토 현물) 백테스트 체결 모델 (04 2-M2.2, CP-024 §2.6·D-101·D-104·D-118, T-M2-12). 실운영과 같은 BrokerAdapter 계약.

- 주문은 접수만 하고 `match`가 체결을 정한다(판정 봉 종가 체결 금지 — 룩어헤드). 기준가 = 판정 시점 가격(그날 UTC 일봉 시가).
- 매수(IOC 지정가 모사, D-104): 체결가 = 기준가 × (1 + 슬리피지)를 tickSize로 올림. 체결가 ≤ 주문가(= 판정 가격 × (1 + 완충)
  내림)일 때만 체결, 아니면 IOC 미체결분 EXPIRED. 시장가 매수는 받지 않는다(거부).
- 매도: 시장가 = 기준가 × (1 − 슬리피지)를 tickSize로 내림. 시세 지연 IOC 지정가(D-120)는 그 값 ≥ 주문가일 때만 체결.
- 가격제한 없음(상·하한가 미적용). 수량은 의도의 수량(stepSize 배수 — 게이트 G10), 봉 거래량 상한(기본 10%, 04 D-072
  공통 기본값을 M2에도 적용 [제안])을 stepSize로 내림해 넘는 부분은 EXPIRED.
- 수수료 = 받는 자산에서 차감(D-101): 매수 = 기초 자산 수량 × 요율, 매도 = USDT 금액 × 요율. 매도세 0. 보고 금액 = 수량 × 체결가.
  거래소의 수수료 자릿수 반올림은 미확인 — 계산값 그대로(Decimal) [가정].
- 슬리피지 값은 사용자 결정 항목(▢, T-M2-12 착수 전) — 기본값을 두지 않는다. 호출자가 반드시 준다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from trading_system.broker.base import BrokerQueryError
from trading_system.domain.enums import OrderType, SubmitOutcome
from trading_system.domain.market import CashView
from trading_system.domain.models import (
    BrokerOrderRecord, BrokerPosition, FillRecord, OrderEvent, OrderRequest, SubmitResult,
)

_OPEN = ("ACCEPTED", "PARTIAL")
_ZERO = Decimal("0")


def _to_unit(value: Decimal, unit: Decimal, rounding: str) -> Decimal:
    if unit <= 0:
        return value
    return (value / unit).to_integral_value(rounding=rounding) * unit


@dataclass(frozen=True)
class SpotFill:
    order_no: str
    instrument: str
    side: str
    quantity: Decimal
    price: Decimal
    quote_qty: Decimal
    commission: Decimal
    commission_asset: str
    at: datetime


@dataclass
class _Order:
    account: str
    rec: BrokerOrderRecord


class SpotBacktestBroker:
    query_fills_trusted = True

    def __init__(self, *, pairs: Mapping[str, tuple[str, str]], fee_rate: Decimal, slippage: Decimal,
                 tick_of: Callable[[str], Decimal], step_of: Callable[[str], Decimal],
                 volume_cap: Decimal = Decimal("0.10")) -> None:
        for name, v in (("fee_rate", fee_rate), ("slippage", slippage)):
            if not isinstance(v, Decimal) or not v.is_finite() or not Decimal("0") <= v < Decimal("1"):
                raise ValueError(f"{name}는 0 이상 1 미만의 Decimal이어야 한다")
        if not Decimal("0") < volume_cap <= Decimal("1"):
            raise ValueError("거래량 상한은 0 초과 1 이하")
        self._pairs = dict(pairs)
        self.fee_rate = fee_rate
        self.slippage = slippage
        self._tick_of = tick_of
        self._step_of = step_of
        self.volume_cap = volume_cap
        self._seq = 0
        self._fill_seq = 0
        self._book: dict[str, _Order] = {}
        self._fills: list[tuple[str, SpotFill]] = []
        self._events: list[OrderEvent] = []
        self._used: dict[str, Decimal] = {}

    # --- 주문 ---------------------------------------------------------------------------
    def place_order(self, request: OrderRequest) -> SubmitResult:
        if request.order_type is OrderType.MARKET and request.side == "BUY":
            return SubmitResult(SubmitOutcome.REJECTED, reason="M2 시장가 매수 없음 (D-104)")
        self._seq += 1
        no = f"{self._seq:07d}"
        self._book[no] = _Order(request.account, BrokerOrderRecord(
            order_no=no, kind="NEW", instrument=request.instrument, side=request.side, quantity=request.quantity,
            filled_quantity=_ZERO, price=request.price, order_type=request.order_type, state="ACCEPTED",
            client_order_id=request.client_order_id))
        return SubmitResult(SubmitOutcome.ACCEPTED, order_no=no)

    def cancel_order(self, request: OrderRequest) -> SubmitResult:
        return SubmitResult(SubmitOutcome.REJECTED, reason="M2 백테스트 체결 모델: 열린 주문 없음 (IOC·시장가)")

    def modify_order(self, request: OrderRequest) -> SubmitResult:
        return SubmitResult(SubmitOutcome.REJECTED, reason="M2 정정 미지원 (D-104)")

    # --- 체결 ---------------------------------------------------------------------------
    def match(self, instrument: str, reference: Decimal, volume: Decimal, at: datetime) -> None:
        """이 종목의 열린 주문을 기준가(판정 시점 가격)로 체결하고 남은 수량은 소멸(IOC·시장가)."""
        tick = self._tick_of(instrument)
        step = self._step_of(instrument)
        for o in self._book.values():
            r = o.rec
            if r.instrument != instrument or r.state not in _OPEN:
                continue
            buy = r.side == "BUY"
            px = _to_unit(reference * (1 + self.slippage) if buy else reference * (1 - self.slippage), tick,
                          ROUND_CEILING if buy else ROUND_FLOOR)
            ok = px > 0 and (r.order_type is OrderType.MARKET or (px <= r.price if buy else px >= r.price))
            if ok:
                cap = _to_unit(volume * self.volume_cap - self._used.get(instrument, _ZERO), step, ROUND_FLOOR)
                qty = min(r.quantity - r.filled_quantity, max(cap, _ZERO))
                if qty > 0:
                    self._used[instrument] = self._used.get(instrument, _ZERO) + qty
                    self._fill(o, qty, px, at)
            if o.rec.state in _OPEN:
                o.rec = replace(o.rec, state="EXPIRED")
                self._events.append(OrderEvent(kind="EXPIRED", order_no=r.order_no, instrument=instrument,
                                               reason="IOC·시장가 미체결분 소멸 (백테스트 체결 모델)"))

    def _fill(self, o: _Order, qty: Decimal, price: Decimal, at: datetime) -> None:
        r = o.rec
        base, quote = self._pairs[r.instrument]
        amount = qty * price
        fee, asset = (qty * self.fee_rate, base) if r.side == "BUY" else (amount * self.fee_rate, quote)
        filled = r.filled_quantity + qty
        o.rec = replace(r, filled_quantity=filled, state="FILLED" if filled == r.quantity else "PARTIAL")
        self._fill_seq += 1
        fid = f"S{self._fill_seq:08d}"
        self._fills.append((o.account, SpotFill(r.order_no, r.instrument, r.side, qty, price, amount, fee, asset, at)))
        self._events.append(OrderEvent(kind="FILL", order_no=r.order_no, fill_id=fid, quantity=qty, price=price, at=at,
                                       commission=fee, commission_asset=asset, instrument=r.instrument,
                                       quote_qty=amount, quote_confirmed=True))

    def end_of_day(self) -> None:
        for o in self._book.values():
            if o.rec.state in _OPEN:
                o.rec = replace(o.rec, state="EXPIRED")
                self._events.append(OrderEvent(kind="EXPIRED", order_no=o.rec.order_no, instrument=o.rec.instrument))
        self._used.clear()

    # --- 통보·조회 -----------------------------------------------------------------------
    def poll_events(self) -> list[OrderEvent]:
        ev, self._events = self._events, []
        return ev

    def query_orders(self, account: str) -> list[BrokerOrderRecord]:
        return [o.rec for o in self._book.values() if o.account == account]

    def query_fills(self, account: str) -> list[FillRecord]:
        return [FillRecord(f.order_no, f"S{i:08d}", f.quantity, f.price, f.at, commission=f.commission,
                           commission_asset=f.commission_asset, quote_qty=f.quote_qty, instrument=f.instrument)
                for i, (a, f) in enumerate(self._fills, start=1) if a == account]

    def query_positions(self, account: str) -> list[BrokerPosition]:
        raise BrokerQueryError("백테스트 체결 모델은 잔고 조회를 제공하지 않음 (내부 원장 사용)")

    def query_cash(self, account: str) -> CashView:
        raise BrokerQueryError("백테스트 체결 모델은 현금 조회를 제공하지 않음 (내부 계산)")

    def fills_for(self, account: str) -> list[SpotFill]:
        return [f for a, f in self._fills if a == account]
