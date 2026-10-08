"""백테스트 체결 모델 브로커 (04 D-071·D-072·§2.2). 실운영과 같은 BrokerAdapter 계약.

- 주문은 접수만 하고, `match_bar`가 다음 봉에서 체결 여부를 정한다 (판정 봉 종가 체결 금지 — 룩어헤드).
- 시장가: 봉 시가 + 불리한 방향 슬리피지(틱 수 × 틱). [CP-030 D-164] `slippage_pct`를 주면 % 방식:
  기준가 × (1 ± s)를 불리한 방향으로 `price_unit`(호가 표가 있으면 호가 단위, 없으면 1원) 배수에 맞춘다
  (매수 올림, 매도 내림). 상·하한가 규칙은 두 방식 모두 같다. 기본(None)은 틱 방식 그대로. 지정가: 가격이 **관통**해야 지정가로 체결(접촉만은 미체결).
- 봉 거래량의 상한 비율(기본 10%)을 넘는 수량은 미체결로 남는다(부분 체결).
- 상한가 매수·하한가 매도는 체결 없음(보수적). 거래정지일은 체결 없음.
- `end_of_day`: 남은 미체결은 만료(일봉 기준 당일 주문). 결과는 통보로 낸다.
"""

from __future__ import annotations

from collections.abc import Callable
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


@dataclass(frozen=True)
class BarFacts:
    """체결 판단에 쓰는 봉 하나 (시가·고가·저가·거래량과 그날의 상·하한가, 거래정지 여부)."""

    open: Decimal
    high: Decimal
    low: Decimal
    volume: Decimal
    upper_limit: Decimal | None = None
    lower_limit: Decimal | None = None
    halted: bool = False


@dataclass
class _Order:
    account: str
    rec: BrokerOrderRecord


class BacktestBroker:
    def __init__(self, tick_of: Callable[[str, Decimal], Decimal], slippage_ticks: int = 1,
                 volume_cap: Decimal = Decimal("0.10"), slippage_pct: Decimal | None = None,
                 price_unit: Callable[[str, Decimal], Decimal] | None = None) -> None:
        if slippage_ticks < 0 or not (Decimal("0") < volume_cap <= Decimal("1")):
            raise ValueError("슬리피지 틱 ≥ 0, 거래량 상한 0 초과 1 이하")
        if slippage_pct is not None and (not (Decimal("0") <= slippage_pct < Decimal("1")) or price_unit is None):
            raise ValueError("슬리피지 비율은 0 이상 1 미만이고 가격 단위 함수가 필요")
        self.slippage_pct = slippage_pct
        self._price_unit = price_unit
        self._tick_of = tick_of
        self.slippage_ticks = slippage_ticks
        self.volume_cap = volume_cap
        self._seq = 0
        self._fill_seq = 0
        self._book: dict[str, _Order] = {}
        self._fills: list[tuple[str, FillRecord]] = []
        self._events: list[OrderEvent] = []
        self._used_volume: dict[str, Decimal] = {}
        self.cash: dict[str, CashView] = {}

    # --- 주문 ---------------------------------------------------------------------------
    def place_order(self, request: OrderRequest) -> SubmitResult:
        self._seq += 1
        no = f"{self._seq:07d}"
        self._book[no] = _Order(request.account, BrokerOrderRecord(
            order_no=no, kind="NEW", instrument=request.instrument, side=request.side, quantity=request.quantity,
            filled_quantity=Decimal("0"), price=request.price, order_type=request.order_type, state="ACCEPTED"))
        return SubmitResult(SubmitOutcome.ACCEPTED, order_no=no)

    def cancel_order(self, request: OrderRequest) -> SubmitResult:
        o = self._book.get(request.target_order_no or "")
        if o is None or o.rec.state not in _OPEN:
            return SubmitResult(SubmitOutcome.REJECTED, reason="취소 대상 없음")
        left = o.rec.quantity - o.rec.filled_quantity
        o.rec = replace(o.rec, state="CANCELLED")
        self._events.append(OrderEvent(kind="CANCELLED", order_no=o.rec.order_no, quantity=left))
        self._seq += 1
        return SubmitResult(SubmitOutcome.ACCEPTED, order_no=f"{self._seq:07d}")

    def modify_order(self, request: OrderRequest) -> SubmitResult:
        return SubmitResult(SubmitOutcome.REJECTED, reason="백테스트 체결 모델은 정정을 지원하지 않음 (CAP-ORD-03 미지원)")

    # --- 체결 ---------------------------------------------------------------------------
    def match_bar(self, instrument: str, bar: BarFacts, at: datetime) -> None:
        """이 종목의 열린 주문을 봉 하나로 체결한다."""
        if bar.halted:
            return
        used = self._used_volume.get(instrument, Decimal("0"))
        cap = (bar.volume * self.volume_cap).to_integral_value(rounding=ROUND_FLOOR)
        for o in self._book.values():
            r = o.rec
            if r.instrument != instrument or r.state not in _OPEN:
                continue
            price = self._fill_price(r, bar)
            if price is None:
                continue
            left = r.quantity - r.filled_quantity
            qty = min(left, max(Decimal("0"), cap - used))
            if qty <= 0:
                continue
            used += qty
            self._fill(o, qty, price, at)
        self._used_volume[instrument] = used

    def _fill_price(self, r: BrokerOrderRecord, bar: BarFacts) -> Decimal | None:
        buy = r.side == "BUY"
        if buy and bar.upper_limit is not None and bar.open >= bar.upper_limit:
            return None
        if not buy and bar.lower_limit is not None and bar.open <= bar.lower_limit:
            return None
        if r.order_type is OrderType.MARKET:
            if self.slippage_pct is not None and self._price_unit is not None:
                raw = bar.open * (1 + self.slippage_pct) if buy else bar.open * (1 - self.slippage_pct)
                unit = self._price_unit(r.instrument, raw)
                price = (raw / unit).to_integral_value(rounding=ROUND_CEILING if buy else ROUND_FLOOR) * unit
            else:
                slip = self._tick_of(r.instrument, bar.open) * self.slippage_ticks
                price = bar.open + slip if buy else bar.open - slip
            if buy and bar.upper_limit is not None:
                price = min(price, bar.upper_limit)
            if not buy and bar.lower_limit is not None:
                price = max(price, bar.lower_limit)
            return price if price > 0 else None
        # 지정가: 관통해야 체결 (매수는 저가 < 지정가, 매도는 고가 > 지정가), 체결가는 지정가 (보수적)
        if buy and bar.low < r.price:
            return r.price
        if not buy and bar.high > r.price:
            return r.price
        return None

    def _fill(self, o: _Order, qty: Decimal, price: Decimal, at: datetime) -> None:
        r = o.rec
        filled = r.filled_quantity + qty
        o.rec = replace(r, filled_quantity=filled, state="FILLED" if filled == r.quantity else "PARTIAL")
        self._fill_seq += 1
        fid = f"B{self._fill_seq:08d}"
        self._fills.append((o.account, FillRecord(r.order_no, fid, qty, price, at)))
        self._events.append(OrderEvent(kind="FILL", order_no=r.order_no, fill_id=fid, quantity=qty, price=price,
                                       at=at))

    def end_of_day(self) -> None:
        """당일 미체결 만료, 거래량 사용량 초기화."""
        for o in self._book.values():
            if o.rec.state in _OPEN:
                o.rec = replace(o.rec, state="EXPIRED")
                self._events.append(OrderEvent(kind="EXPIRED", order_no=o.rec.order_no))
        self._used_volume.clear()

    def new_day(self) -> None:
        """다음 거래일: 지난 주문(만료됨)은 조회에서 빠지고 주문번호는 다시 시작한다 (D-046 시험 조건과 같음)."""
        self._book.clear()
        self._seq = 0

    # --- 통보·조회 -------------------------------------------------------------------------
    def poll_events(self) -> list[OrderEvent]:
        ev, self._events = self._events, []
        return ev

    def query_orders(self, account: str) -> list[BrokerOrderRecord]:
        return [o.rec for o in self._book.values() if o.account == account]

    def query_fills(self, account: str) -> list[FillRecord]:
        live = {o.rec.order_no for o in self._book.values()}
        return [f for a, f in self._fills if a == account and f.order_no in live]

    def query_positions(self, account: str) -> list[BrokerPosition]:
        raise BrokerQueryError("백테스트 체결 모델은 잔고 조회를 제공하지 않음 (내부 원장 사용)")

    def query_cash(self, account: str) -> CashView:
        if account not in self.cash:
            raise BrokerQueryError("예수금 정보 없음")
        return self.cash[account]

    def fills_for(self, account: str) -> list[FillRecord]:
        return [f for a, f in self._fills if a == account]

    def instrument_of(self, order_no: str) -> str | None:
        o = self._book.get(order_no)
        return o.rec.instrument if o else None

    def side_of(self, order_no: str) -> str | None:
        o = self._book.get(order_no)
        return o.rec.side if o else None
