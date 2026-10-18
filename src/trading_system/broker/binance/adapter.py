"""바이낸스 Spot **테스트넷** 브로커 어댑터 — 공통 계약 BrokerAdapter·SpotOrderQueries 구현 (CP-036 D-203·D-204, T-047).

BinanceSpotClient 하나를 감싼다. 엔진·실행기·해소기·대조기는 공통 계약만 본다(03 §7, D-119 — 바이낸스 타입은 밖으로 안 나감).

주문 (D-104·D-110·D-112, CLAUDE.md §8-5·§8-7)
- 매수 지정가 → IOC 지정가. 매도 시장가 → 수량 지정 시장가. 매도 지정가(시세 지연, D-120) → IOC 지정가.
  시장가 매수 → 전송 전 거부(D-104). 정정 → 전송 전 거부(정정 미지원). GTC·quoteOrderQty·OCO 없음.
- 수량·가격 = 게이트가 승인한 값 그대로. 단위(units: 마지막 정상 거래 규칙)의 배수가 아니면 전송 전 실패 — 몰래 반올림 없음.
  시장가 수량 단위 = MARKET_LOT_SIZE stepSize, 0이면 LOT_SIZE stepSize(D-208). 호가 단위 0(비활성)이면 지정가는 전송 전 실패
  [보수 — 단위 근거 없는 가격 문자열을 만들지 않음].
- 고객 주문 ID = 요청의 client_order_id(실행기가 멱등키에서 결정적으로 만들어 전송 전에 저장, D-110). 없으면 전송 전 실패.
- **한 번만 보낸다.** 429·5XX·타임아웃·418·-2010 중복·해석 불가 = 결과 불명(클라이언트 4분류 그대로). 자동 재전송 없음.
  결과 불명 해소는 SpotUnknownResolver가 조회로(D-111).
- 우선순위(D-113): 매수 = 위험 확대(INCREASE), 매도·취소 = 위험 축소(REDUCE).
- 주문 전송 스위치(D-204): `orders_enabled`가 False면 신규·취소 모두 전송 전 실패 + 경보 ORDER_BLOCKED(알림).
  전송 계층(run_guard.RunGuard)도 같은 스위치로 한 번 더 막는다.

체결·종결 통보 (D-109·D-114·D-118)
- FULL 응답의 fills → FILL 통보(금액은 price × qty 임시값, quote_confirmed=False). 응답 상태가 EXPIRED(IOC 미체결분 소멸)·
  CANCELED·REJECTED면 종결 통보를 뒤에 붙인다.
- 사용자 스트림 executionReport(ingest_stream, T-048 스트림 스레드가 부른다) → TRADE = FILL(보고 금액 Y 확정),
  EXPIRED·EXPIRED_IN_MATCH·CANCELED·REJECTED = 종결. TRADE_PREVENTION은 체결이 아니다.
- 같은 체결(심볼, tradeId)은 FillLedger로 한 번만: 새 체결 → 통보, 임시 → 확정 → 확정 통보 한 번(실행기가 금액 확정),
  중복 → 버림, 충돌(같은 ID 다른 값) → 통보는 넘기고(실행기가 수수료 차이를 K4로) 경고.
- 통보 반영은 판정·실행 스레드(executor.process_events → poll_events)만 한다. 큐는 잠금으로 보호(D-215).
- 취소 접수: 응답 orderId는 대상 주문과 같은 번호라 브로커 주문 식별(계좌, 심볼, orderId, D-110)과 겹친다 → 취소 기록 번호는
  `<orderId>-CXL`(CANCEL_SUFFIX) [DP-55]. 대상 주문의 종결은 CANCELLED 통보로.

경보(on_alert(kind, message)): 클라이언트의 K5(418)·K6(-1021 연속 3회)·CLOCK과 어댑터의 FILTER(-1013 → 규칙 재적재 요청,
on_filter_reject)·ORDER_BLOCKED·FILL_CONFLICT. 정지 연결은 app이 halt/broker_alerts.BrokerAlertHalts를 큐 경유로 건다(D-215).
문구에 키·서명·쿼리 문자열·응답 원문을 넣지 않는다.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal

from trading_system.broker.base import BrokerQueryError, LookupKind, OrderLookup
from trading_system.broker.ratelimit import Priority, current_priority
from trading_system.domain.enums import OrderType, SubmitOutcome
from trading_system.domain.market import CashView
from trading_system.domain.models import (
    BrokerOrderRecord, BrokerPosition, FillRecord, OrderEvent, OrderRequest, SubmitResult,
)
from trading_system.domain.trading_rules import SymbolRules as DomainRules
from trading_system.domain.trading_rules import market_qty_step

from .classify import Flag, QueryKind
from .client import BinanceQueryError, BinanceSpotClient, OrderSubmit
from .orders import NewOrder, OrderKind, OrderSide
from .readonly import BinanceReadOnlyQueries, to_fill_record, to_order_record
from .stream import ExecutionReport, Fill, FillLedger, OrderState, Recorded, StreamEvent
from .weights import CallPriority, WeightBudget

CANCEL_SUFFIX = "-CXL"
_TERMINAL = {"EXPIRED": "EXPIRED", "EXPIRED_IN_MATCH": "EXPIRED", "CANCELED": "CANCELLED", "REJECTED": "REJECTED"}
_REASON = {"EXPIRED": "IOC 미체결분 소멸 (D-104)", "CANCELLED": "취소 확인", "REJECTED": "접수 후 거부"}
_PRIORITY = {Priority.RESOLVE: CallPriority.RESOLVE, Priority.ORDER: CallPriority.REDUCE,
             Priority.RECONCILE: CallPriority.RECONCILE, Priority.OPS: CallPriority.RECONCILE,
             Priority.MARKET: CallPriority.MARKET}

Alert = Callable[[str, str], None]


def _ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


class BinanceSpotAdapter:
    def __init__(self, client: BinanceSpotClient, symbols: Sequence[str], *, account: str,
                 now_ms: Callable[[], int], units: Callable[[str], DomainRules | None], orders_enabled: bool,
                 on_alert: Alert | None = None, on_filter_reject: Callable[[str], None] | None = None,
                 budget: WeightBudget | None = None) -> None:
        self._client = client
        self._symbols = tuple(symbols)
        self._account = account
        self._units = units
        self._orders = bool(orders_enabled)
        self._alert_fn = on_alert or (lambda kind, msg: None)
        self._on_filter = on_filter_reject
        self._budget = budget
        self._queries = BinanceReadOnlyQueries(client, self._symbols, now_ms)
        self._ledger = FillLedger()
        self._events: deque[OrderEvent] = deque()
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"BinanceSpotAdapter(symbols={list(self._symbols)}, orders_enabled={self._orders})"

    @property
    def orders_enabled(self) -> bool:
        return self._orders

    # --- 주문 변경 (BrokerAdapter) ------------------------------------------------------------------
    def place_order(self, request: OrderRequest) -> SubmitResult:
        refused = self._refuse_change(request, "NEW")
        if refused:
            return refused
        if not request.client_order_id:
            return _pre("고객 주문 ID 없음 (D-110) — 전송하지 않음")
        sell = request.side == "SELL"
        if request.side not in ("BUY", "SELL"):
            return _pre(f"주문 방향 아님: {request.side}")
        market = request.order_type is OrderType.MARKET
        if market and not sell:
            return _pre("시장가 매수는 보내지 않는다 (D-104)")
        u = self._units(request.instrument)
        if u is None:
            return _pre(f"{request.instrument}: 거래 규칙(수량·호가 단위) 모름 — 전송하지 않음 (D-106)")
        order = NewOrder(request.instrument, OrderSide.SELL if sell else OrderSide.BUY,
                         OrderKind.MARKET if market else OrderKind.LIMIT_IOC, request.quantity,
                         None if market else request.price, request.client_order_id)
        tick = u.tick_size if u.tick_size > 0 else None
        step = market_qty_step(u) if market else u.lot_step
        r = self._client.place(order, CallPriority.REDUCE if sell else CallPriority.INCREASE, qty_step=step, tick=tick)
        self._after(r, request.instrument)
        if r.outcome is SubmitOutcome.ACCEPTED and r.ack is not None:
            self._from_ack(r.ack)
        return SubmitResult(r.outcome, order_no=None if r.order_id is None or r.outcome is not SubmitOutcome.ACCEPTED
                            else str(r.order_id), reason=r.reason, code=r.code)

    def cancel_order(self, request: OrderRequest) -> SubmitResult:
        refused = self._refuse_change(request, "CANCEL")
        if refused:
            return refused
        target = request.target_order_no or ""
        if not target.isdigit():
            return _pre("취소 대상 orderId 형식 아님 — 전송하지 않음")
        r = self._client.cancel(request.instrument, CallPriority.REDUCE, order_id=int(target))
        self._after(r, request.instrument)
        if r.outcome is not SubmitOutcome.ACCEPTED:
            return SubmitResult(r.outcome, reason=r.reason, code=r.code)
        if r.ack is not None and r.ack.status in _TERMINAL:
            self._push(self._terminal(request.instrument, str(r.ack.order_id), _TERMINAL[r.ack.status]))
        return SubmitResult(r.outcome, order_no=f"{r.order_id}{CANCEL_SUFFIX}", reason=r.reason)

    def modify_order(self, request: OrderRequest) -> SubmitResult:
        return _pre("정정 미지원 (D-203) — 전송하지 않음")

    def _refuse_change(self, request: OrderRequest, kind: str) -> SubmitResult | None:
        if request.kind != kind:
            return _pre(f"요청 종류 {request.kind} ≠ {kind}")
        if request.instrument not in self._symbols:
            return _pre(f"{request.instrument}: 유니버스 밖 심볼 — 전송하지 않음")
        if not self._orders:
            msg = f"주문 전송 스위치 꺼짐 (D-204) — {kind} {request.instrument} 전송하지 않음"
            self._alert("ORDER_BLOCKED", msg)
            return _pre(msg)
        return None

    def _alert(self, kind: str, message: str) -> None:
        """경보 전달 실패가 주문 판정(전송 전 실패 등)을 결과 불명으로 바꾸지 않게 삼킨다."""
        try:
            self._alert_fn(kind, message)
        except Exception:  # noqa: BLE001 — 알림 경로 오류는 주문 결과와 무관
            pass

    def _after(self, r: OrderSubmit, symbol: str) -> None:
        if r.flag is Flag.FILTER:
            self._alert("FILTER", f"{symbol}: -1013 필터 거부 — 거래 규칙 재적재 요청 (D-106)")
            if self._on_filter is not None:
                self._on_filter(symbol)

    def resume_after_ban(self) -> None:
        """사람이 IP 차단 해제를 확인했을 때만(BrokerAlertHalts.confirm_ip_ban_cleared가 부른다, D-113)."""
        if self._budget is not None:
            self._budget.resume_after_ban()

    # --- 통보 -------------------------------------------------------------------------------------
    def poll_events(self) -> list[OrderEvent]:
        with self._lock:
            out = list(self._events)
            self._events.clear()
        return out

    def ingest_stream(self, event: StreamEvent) -> None:
        """사용자 스트림 이벤트(T-048 스트림 스레드). 반영은 하지 않고 큐에만 넣는다(D-215)."""
        if not isinstance(event, ExecutionReport):
            return
        if event.fill is not None:
            self._fill(event.fill)
        state = _TERMINAL.get(event.order_status) if event.execution_type != "TRADE" else None
        if state is not None and event.execution_type in ("EXPIRED", "CANCELED", "REJECTED", "TRADE_PREVENTION"):
            self._push(self._terminal(event.symbol, str(event.order_id), state))

    def _from_ack(self, ack: OrderState) -> None:
        for f in ack.fills:
            self._fill(f)
        state = _TERMINAL.get(ack.status)
        if state is not None:
            self._push(self._terminal(ack.symbol, str(ack.order_id), state))

    def _fill(self, f: Fill) -> None:
        with self._lock:
            rec = self._ledger.record(f)
            if rec is Recorded.DUPLICATE:
                return
            kept = self._ledger.get(f.key) if rec is not Recorded.CONFLICT else f
            self._events.append(OrderEvent(
                kind="FILL", order_no=str(f.order_id), fill_id=str(f.trade_id), quantity=f.quantity, price=f.price,
                at=kept.at, account=self._account, commission=f.commission, commission_asset=f.commission_asset,
                instrument=f.symbol, quote_qty=kept.quote_qty, quote_confirmed=kept.quote_confirmed))
        if rec is Recorded.CONFLICT:
            self._alert("FILL_CONFLICT", f"{f.symbol} 체결 {f.trade_id}: 같은 체결 ID의 값이 다름 — 대조 필요")

    def _terminal(self, symbol: str, order_no: str, kind: str) -> OrderEvent:
        return OrderEvent(kind=kind, order_no=order_no, account=self._account, instrument=symbol, reason=_REASON[kind])

    def _push(self, ev: OrderEvent) -> None:
        with self._lock:
            self._events.append(ev)

    # --- 조회 (BrokerAdapter) -----------------------------------------------------------------------
    def query_orders(self, account: str) -> list[BrokerOrderRecord]:
        return self._queries.query_orders(account)

    def query_fills(self, account: str) -> list[FillRecord]:
        return self._queries.query_fills(account)

    def query_balances(self, account: str) -> dict[str, tuple[Decimal, Decimal]]:
        return self._queries.query_balances(account)

    def query_positions(self, account: str) -> list[BrokerPosition]:
        """유니버스 기초 자산 잔고(free + locked, D-103). 평균가는 거래소가 주지 않는다(0)."""
        bal = self.query_balances(account)
        out = []
        for sym in self._symbols:
            u = self._units(sym)
            base = sym[: -len("USDT")] if sym.endswith("USDT") else None
            if base is None or u is None:
                continue
            free, locked = bal.get(base, (Decimal("0"), Decimal("0")))
            if free + locked != 0:
                out.append(BrokerPosition(sym, free + locked, Decimal("0")))
        return out

    def query_cash(self, account: str) -> CashView:
        """USDT: 예수금 = free + locked, 주문 가능 = free (M2 현금 정책은 SpotAccountFacts가 따로 정한다)."""
        free, locked = self.query_balances(account).get("USDT", (Decimal("0"), Decimal("0")))
        return CashView(deposit=free + locked, broker_orderable=free)

    # --- 조회 (SpotOrderQueries) ---------------------------------------------------------------------
    def lookup_order(self, account: str, instrument: str, *, order_no: str | None,
                     client_order_id: str | None) -> OrderLookup:
        oid = None
        if order_no is not None:
            if not order_no.isdigit():
                raise BrokerQueryError(f"orderId 형식 아님: {order_no}")
            oid = int(order_no)
        try:
            q = self._client.query_order(instrument, order_id=oid, client_order_id=None if oid else client_order_id,
                                         priority=self._priority())
        except BinanceQueryError as exc:
            raise BrokerQueryError(str(exc)) from None
        if q.kind is QueryKind.NOT_FOUND:
            return OrderLookup(LookupKind.NOT_FOUND)
        if q.kind is QueryKind.ARCHIVED:
            return OrderLookup(LookupKind.ARCHIVED)
        if q.order is None:
            raise BrokerQueryError("주문 조회 응답 없음")
        return OrderLookup(LookupKind.FOUND, to_order_record(q.order))

    def orders_between(self, account: str, instrument: str, start: datetime, end: datetime) -> list[BrokerOrderRecord]:
        try:
            rows = self._client.all_orders(instrument, start_ms=_ms(start), end_ms=_ms(end), priority=self._priority())
        except BinanceQueryError as exc:
            raise BrokerQueryError(str(exc)) from None
        return [to_order_record(o) for o in rows]

    def trades_between(self, account: str, instrument: str, start: datetime, end: datetime) -> list[FillRecord]:
        try:
            rows = self._client.my_trades(instrument, start_ms=_ms(start), end_ms=_ms(end), priority=self._priority())
        except BinanceQueryError as exc:
            raise BrokerQueryError(str(exc)) from None
        return [to_fill_record(f) for f in rows]

    @staticmethod
    def _priority() -> CallPriority:
        return _PRIORITY.get(current_priority(), CallPriority.RECONCILE)


def _pre(reason: str) -> SubmitResult:
    return SubmitResult(SubmitOutcome.PRE_SEND_FAILURE, reason=reason)
