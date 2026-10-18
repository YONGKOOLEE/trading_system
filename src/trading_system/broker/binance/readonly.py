"""대조용 **조회 전용** 어댑터 (T-M2-14 시작 점검, CP-024 D-103·D-109·D-110·D-111, reconciliation/spot.SpotReconciler 입력).

- SpotReconciler가 부르는 조회 세 가지(query_balances·query_orders·query_fills)만 구현한다. 주문 변경 메서드(place·cancel·
  modify)는 **없다** — 이 객체로는 주문을 보낼 수 없다(시작 점검은 조회만, A-096).
- 범위: 유니버스 심볼별. 주문 = 미체결(openOrders) + 최근 24시간 전체 주문(allOrders 창, BN §7), 같은 orderId는 미체결 쪽
  (Memory 소스, 더 최신)을 쓴다. 체결 = 최근 24시간 myTrades. 식별 = (심볼, orderId) → order_no = str(orderId) (D-110).
- 상태 매핑(02 ENT-17 공통 값): NEW·PENDING_NEW → ACCEPTED, PARTIALLY_FILLED → PARTIAL, FILLED → FILLED,
  CANCELED → CANCELLED, EXPIRED·EXPIRED_IN_MATCH → EXPIRED, REJECTED → REJECTED, PENDING_CANCEL("Currently unused",
  BN §5) → ACCEPTED(아직 열린 것으로 봄 — 보수). 표에 없는 상태는 추정하지 않고 조회 실패(BrokerQueryError).
- 주문 유형: MARKET → MARKET, 그 밖(LIMIT·LIMIT_MAKER·STOP_LOSS_LIMIT 등 외부 주문 포함) → LIMIT [제안 — 공통 열거가 두 값뿐,
  대조는 유형을 비교하지 않음].
- 조회 실패(BinanceQueryError·해석 불가)는 BrokerQueryError로 바꾼다 — 대조기는 QUERY_FAILED로 기록(불일치 아님).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal

from trading_system.broker.base import BrokerQueryError
from trading_system.domain.enums import OrderType
from trading_system.domain.models import BrokerOrderRecord, FillRecord

from .client import BinanceQueryError, BinanceSpotClient
from .orders import MAX_WINDOW_MS
from .stream import Fill, OrderState

STATE = {"NEW": "ACCEPTED", "PENDING_NEW": "ACCEPTED", "PARTIALLY_FILLED": "PARTIAL", "FILLED": "FILLED",
         "CANCELED": "CANCELLED", "PENDING_CANCEL": "ACCEPTED", "REJECTED": "REJECTED", "EXPIRED": "EXPIRED",
         "EXPIRED_IN_MATCH": "EXPIRED"}


def to_order_record(o: OrderState) -> BrokerOrderRecord:
    state = STATE.get(o.status)
    if state is None:
        raise BrokerQueryError(f"{o.symbol} 주문 {o.order_id}: 알 수 없는 주문 상태 {o.status!r} — 추정하지 않음")
    return BrokerOrderRecord(order_no=str(o.order_id), kind="NEW", instrument=o.symbol, side=o.side,
                             quantity=o.orig_qty, filled_quantity=o.executed_qty, price=o.price,
                             order_type=OrderType.MARKET if o.order_type == "MARKET" else OrderType.LIMIT,
                             state=state, client_order_id=o.client_order_id)


def to_fill_record(f: Fill) -> FillRecord:
    if f.at is None:
        raise BrokerQueryError(f"{f.symbol} 체결 {f.trade_id}: 체결 시각 없음")
    return FillRecord(order_no=str(f.order_id), fill_id=str(f.trade_id), quantity=f.quantity, price=f.price,
                      at=f.at, commission=f.commission, commission_asset=f.commission_asset, quote_qty=f.quote_qty,
                      instrument=f.symbol, is_maker=f.is_maker)


class BinanceReadOnlyQueries:
    """조회 전용. 계좌 인자는 계약 모양을 맞추기 위한 것(키 하나 = 계좌 하나)."""

    def __init__(self, client: BinanceSpotClient, symbols: Sequence[str], now_ms: Callable[[], int]) -> None:
        self._client = client
        self._symbols = tuple(symbols)
        self._now = now_ms

    def __repr__(self) -> str:
        return f"BinanceReadOnlyQueries(symbols={list(self._symbols)})"

    def _window(self) -> tuple[int, int]:
        end = self._now()
        return end - MAX_WINDOW_MS, end

    def query_balances(self, account: str) -> dict[str, tuple[Decimal, Decimal]]:
        try:
            return self._client.account().balance_map()
        except BinanceQueryError as exc:
            raise BrokerQueryError(str(exc)) from None

    def query_orders(self, account: str) -> list[BrokerOrderRecord]:
        start, end = self._window()
        out: dict[tuple[str, int], OrderState] = {}
        try:
            for sym in self._symbols:
                for o in self._client.all_orders(sym, start_ms=start, end_ms=end):
                    out[(o.symbol, o.order_id)] = o
                for o in self._client.open_orders(sym):
                    out[(o.symbol, o.order_id)] = o
        except BinanceQueryError as exc:
            raise BrokerQueryError(str(exc)) from None
        return [to_order_record(o) for _k, o in sorted(out.items())]

    def query_fills(self, account: str) -> list[FillRecord]:
        start, end = self._window()
        try:
            fills = [f for sym in self._symbols for f in self._client.my_trades(sym, start_ms=start, end_ms=end)]
        except BinanceQueryError as exc:
            raise BrokerQueryError(str(exc)) from None
        return [to_fill_record(f) for f in fills]
