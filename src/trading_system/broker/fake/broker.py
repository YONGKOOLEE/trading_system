"""fake 브로커 (03 D-062). 결정적으로 동작하며, 동작 방식을 테스트에서 지정한다.

- 주문장(book)에 모든 주문(신규·취소·정정, 외부 주문 포함)을 남기고 조회로 돌려준다 (kt00009·ka10075 대응).
- 응답 유실: 브로커는 받았지만(또는 받지 않았지만) 호출자는 응답을 못 받은 상황을 만든다 (E-1, V-40·V-41).
- 조회 실패·조회 반영 지연을 주입한다 (V-42, PV-05).
- 단절(연결 수립 전 실패·조회 실패), 전송 후 연결 끊김(어댑터 예외), 통보 중복·순서 뒤바뀜·유실 (T-006, D-062).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from trading_system.broker.base import BrokerQueryError
from trading_system.domain.enums import OrderType, SubmitOutcome
from trading_system.domain.models import BrokerOrderRecord, FillRecord, OrderEvent, OrderRequest, SubmitResult

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
_OPEN = ("ACCEPTED", "PARTIAL")


class FakeBehavior(StrEnum):
    FILL = "FILL"                  # 접수 후 전량 체결
    ACCEPT_ONLY = "ACCEPT_ONLY"    # 접수만 (미체결)
    REJECT = "REJECT"              # 명시 거부
    PRE_SEND_FAIL = "PRE_SEND_FAIL"  # 전송 전 실패
    UNKNOWN = "UNKNOWN"            # 응답 없음, 브로커도 받지 않음
    LOST_ACCEPT = "LOST_ACCEPT"    # 브로커는 접수, 응답 유실 (미체결)
    LOST_FILL = "LOST_FILL"        # 브로커는 접수·전량 체결, 응답 유실


@dataclass
class _Order:
    account: str
    record: BrokerOrderRecord
    visible_after: int = 0  # 이 횟수만큼 조회한 뒤에야 조회 결과에 나타남 (반영 지연)
    day: int = 0            # 거래일 번호 (조회는 당일 주문만, kt00009 대응)


class FakeBroker:
    def __init__(self, behavior: FakeBehavior = FakeBehavior.FILL) -> None:
        self.behavior = behavior
        self.sent: list[OrderRequest] = []  # 실제로 "전송된" 요청만 기록
        self.query_failures = 0             # 다음 조회 N회 실패 (V-42)
        self.query_delay = 0                # 응답 유실 주문이 조회에 나타나기까지의 조회 횟수 (PV-05)
        self.queries = 0
        self.disconnected = False           # 단절: 주문은 전송 전 실패, 조회는 실패
        self.raise_after_send = False       # 브로커는 받았지만 응답 전에 연결이 끊김 → 어댑터 예외
        self._seq = 0
        self._day = 0
        self._fill_seq = 0
        self._book: dict[str, _Order] = {}
        self._fills: list[tuple[str, int, FillRecord]] = []  # (계좌, 거래일 번호, 체결)
        self._events: list[OrderEvent] = []

    # --- 주문 변경 -------------------------------------------------------------
    def _next_no(self) -> str:
        self._seq += 1
        return f"{self._seq:07d}"

    def place_order(self, request: OrderRequest) -> SubmitResult:
        if self.disconnected or self.behavior is FakeBehavior.PRE_SEND_FAIL:
            return SubmitResult(SubmitOutcome.PRE_SEND_FAILURE, reason="fake: 연결 없음")
        self.sent.append(request)
        if self.behavior is FakeBehavior.REJECT:
            return SubmitResult(SubmitOutcome.REJECTED, reason="fake: 거부")
        if self.behavior is FakeBehavior.UNKNOWN:
            return SubmitResult(SubmitOutcome.UNKNOWN, reason="fake: 응답 없음")
        order_no = self._new_order(request.account, "NEW", request.instrument, request.side, request.quantity,
                                   request.price, request.order_type, None)
        if self.behavior in (FakeBehavior.FILL, FakeBehavior.LOST_FILL):
            self.fill(order_no)
        if self.behavior in (FakeBehavior.LOST_ACCEPT, FakeBehavior.LOST_FILL):
            self._book[order_no].visible_after = self.queries + self.query_delay
            return SubmitResult(SubmitOutcome.UNKNOWN, reason="fake: 응답 유실")
        if self.raise_after_send:
            raise ConnectionResetError("fake: 전송 후 연결 끊김")
        return SubmitResult(SubmitOutcome.ACCEPTED, order_no=order_no)

    def cancel_order(self, request: OrderRequest) -> SubmitResult:
        return self._change(request, "CANCEL")

    def modify_order(self, request: OrderRequest) -> SubmitResult:
        return self._change(request, "MODIFY")

    def _change(self, request: OrderRequest, kind: str) -> SubmitResult:
        if self.disconnected or self.behavior is FakeBehavior.PRE_SEND_FAIL:
            return SubmitResult(SubmitOutcome.PRE_SEND_FAILURE, reason="fake: 연결 없음")
        self.sent.append(request)
        if self.behavior is FakeBehavior.REJECT:
            return SubmitResult(SubmitOutcome.REJECTED, reason="fake: 거부")
        if self.behavior is FakeBehavior.UNKNOWN:
            return SubmitResult(SubmitOutcome.UNKNOWN, reason="fake: 응답 없음")
        target_no = request.target_order_no
        target = self._book.get(target_no or "")
        if target is None or target.record.state not in _OPEN:
            return SubmitResult(SubmitOutcome.REJECTED, reason=f"fake: {kind} 대상 없음")
        t = target.record
        left = t.quantity - t.filled_quantity
        if kind == "CANCEL":
            self._set(t.order_no, state="CANCELLED")
            no = self._new_order(target.account, "CANCEL", t.instrument, t.side, left, Decimal("0"), t.order_type,
                                 t.order_no, state="CONFIRMED")
            self._events.append(OrderEvent(kind="CANCELLED", order_no=t.order_no, quantity=left))
        else:
            if request.quantity > left:
                return SubmitResult(SubmitOutcome.REJECTED, reason="fake: 정정 수량 초과")
            self._set(t.order_no, state="MODIFIED")
            no = self._new_order(target.account, "MODIFY", t.instrument, t.side, request.quantity, request.price,
                                 request.order_type, t.order_no)
            self._events.append(OrderEvent(kind="MODIFIED", order_no=t.order_no, new_order_no=no,
                                           quantity=request.quantity, price=request.price))
        if self.behavior in (FakeBehavior.LOST_ACCEPT, FakeBehavior.LOST_FILL):
            self._book[no].visible_after = self.queries + self.query_delay
            return SubmitResult(SubmitOutcome.UNKNOWN, reason="fake: 응답 유실")
        if self.raise_after_send:
            raise ConnectionResetError("fake: 전송 후 연결 끊김")
        return SubmitResult(SubmitOutcome.ACCEPTED, order_no=no)

    def _new_order(self, account: str, kind: str, instrument: str, side: str, quantity: Decimal, price: Decimal,
                   order_type: OrderType, original: str | None, state: str = "ACCEPTED") -> str:
        no = self._next_no()
        self._book[no] = _Order(account, day=self._day, record=BrokerOrderRecord(
            order_no=no, kind=kind, instrument=instrument, side=side, quantity=quantity,
            filled_quantity=Decimal("0"), price=price, order_type=order_type, state=state,
            original_order_no=original))
        return no

    def _set(self, order_no: str, **kw: object) -> None:
        o = self._book[order_no]
        o.record = replace(o.record, **kw)  # type: ignore[arg-type]

    # --- 시장 동작 (테스트가 호출) ----------------------------------------------
    def fill(self, order_no: str, quantity: Decimal | None = None, price: Decimal | None = None) -> None:
        """미체결 주문을 체결한다 (기본: 잔량 전부, 주문 가격)."""
        o = self._book[order_no].record
        left = o.quantity - o.filled_quantity
        qty = left if quantity is None else quantity
        if o.state not in _OPEN or qty <= 0 or qty > left:
            raise ValueError("fake: 체결할 수 없는 주문")
        self._fill_seq += 1
        fill_id = f"F{self._fill_seq:07d}"
        px = price if price is not None else o.price
        filled = o.filled_quantity + qty
        self._set(order_no, filled_quantity=filled, state="FILLED" if filled == o.quantity else "PARTIAL")
        at = _EPOCH
        self._fills.append((self._book[order_no].account, self._day, FillRecord(order_no, fill_id, qty, px, at)))
        self._events.append(OrderEvent(kind="FILL", order_no=order_no, fill_id=fill_id, quantity=qty, price=px))

    def expire_open(self) -> list[str]:
        """장 마감: 미체결 잔량을 소멸시킨다 (03 §6.4)."""
        expired = [no for no, o in self._book.items() if o.record.state in _OPEN and o.record.kind != "CANCEL"]
        for no in expired:
            self._set(no, state="EXPIRED")
            self._events.append(OrderEvent(kind="EXPIRED", order_no=no))
        return expired

    def add_external_order(self, account: str, instrument: str, side: str, quantity: Decimal,
                           price: Decimal = Decimal("10000")) -> str:
        """시스템 밖 주문 (증권사 앱 직접 거래, E-6). 통보는 내지 않고 조회에만 나타난다."""
        return self._new_order(account, "NEW", instrument, side, quantity, price, OrderType.LIMIT, None)

    def query_orders_unchecked(self) -> list[BrokerOrderRecord]:
        """테스트용: 장애 주입과 무관하게 주문장 전체를 본다 (조회 횟수에 세지 않음)."""
        return [o.record for o in self._book.values()]

    def new_day(self) -> None:
        """다음 거래일: 주문번호가 처음부터 다시 매겨진다고 가정한다 (키움 주문번호 유일 범위 미확인 K-U2, D-046)."""
        self._day += 1
        self._seq = 0
        self._book = {f"d{o.day}-{no}" if o.day != self._day else no: o for no, o in self._book.items()}

    def open_order_nos(self) -> list[str]:
        return [no for no, o in self._book.items() if o.record.state in _OPEN and o.record.kind != "CANCEL"]

    def duplicate_events(self) -> None:
        """대기 중인 통보를 모두 한 번 더 보낸다 (중복 통보)."""
        self._events = self._events + list(self._events)

    def shuffle_events(self, rng: random.Random) -> None:
        """대기 중인 통보 순서를 섞는다 (순서 뒤바뀐 통보)."""
        rng.shuffle(self._events)

    def drop_events(self) -> None:
        """실시간 통보 유실 (V-66 누락 체결 재현)."""
        self._events.clear()

    def inject(self, event: OrderEvent) -> None:
        """테스트용: 임의의 통보를 넣는다 (비정상·중복·순서 바뀐 통보 재현)."""
        self._events.append(event)

    def poll_events(self) -> list[OrderEvent]:
        events, self._events = self._events, []
        return events

    # --- 조회 -------------------------------------------------------------------
    def _query(self) -> None:
        self.queries += 1
        if self.disconnected:
            raise BrokerQueryError("fake: 연결 끊김")
        if self.query_failures > 0:
            self.query_failures -= 1
            raise BrokerQueryError("fake: 조회 실패")

    def query_orders(self, account: str) -> list[BrokerOrderRecord]:
        self._query()
        return [o.record for o in self._book.values()
                if o.account == account and o.day == self._day and self.queries > o.visible_after]

    def query_fills(self, account: str) -> list[FillRecord]:
        self._query()
        visible = {no for no, o in self._book.items() if self.queries > o.visible_after and o.day == self._day}
        return [f for a, d, f in self._fills if a == account and d == self._day and f.order_no in visible]
