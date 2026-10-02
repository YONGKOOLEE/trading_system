"""결과 불명 해소 (R-06, 01 §2.7, 02 ST-01·D-029, 03 D-047).

- 조회 5회, 각 조회 전에 2·4·8·16·32초 대기 (대기 함수 주입: 테스트·백테스트는 가짜 시계).
- 매칭(D-029): 내부에 없는 주문번호 중 (종목, 방향, 종류)가 같고 수량·가격이 의도와 같은 주문.
  취소·정정은 원주문번호가 대상 주문과 같아야 한다.
- 한 번이라도 유일 매칭 → 접수로 반영하고 조회된 체결·종결 상태를 반영한다 (재전송 없음).
- 5회 모두 조회에 성공했고 매칭 후보가 없고, 그동안 짝 없는 실시간 통보도 없으면 → 미접수 종결.
- 그 밖(후보 여럿, 부분 일치, 조회 실패 섞임, 짝 없는 통보) → 결과 불명 유지 + 정지 K3 + 긴급 알림.
호출 제한기 우선순위(D-048)는 T-014 어댑터에서 적용한다.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

from trading_system.broker.base import BrokerAdapter, BrokerQueryError
from trading_system.domain.clock import Clock, to_iso
from trading_system.domain.enums import IntentState, OrderType, RequestType
from trading_system.domain.models import BrokerOrderRecord, FillRecord, OrderEvent
from trading_system.storage.repository import IntentRow, Repository

from .executor import Executor

QUERY_WAITS_SECONDS = (2, 4, 8, 16, 32)  # 01 §2.7


class ResolveOutcome(StrEnum):
    FOUND = "FOUND"
    NOT_ACCEPTED = "NOT_ACCEPTED"
    AMBIGUOUS = "AMBIGUOUS"


_KIND = {RequestType.CANCEL.value: "CANCEL", RequestType.MODIFY.value: "MODIFY"}


class UnknownResolver:
    def __init__(self, repo: Repository, broker: BrokerAdapter, executor: Executor, clock: Clock,
                 wait: Callable[[float], None], waits: tuple[float, ...] = QUERY_WAITS_SECONDS) -> None:
        self._repo = repo
        self._broker = broker
        self._executor = executor
        self._clock = clock
        self._wait = wait
        self._waits = waits

    def resolve(self, intent_id: str) -> ResolveOutcome:
        intent = self._repo.intent(intent_id)
        if intent.state is not IntentState.UNKNOWN:
            raise ValueError(f"결과 불명 의도만 해소한다: {intent.state}")
        attempt = self._repo.attempt_for(intent_id)
        since = attempt["created_at"] if attempt else to_iso(self._clock.now())
        clean = True
        notes: list[str] = []
        for n, w in enumerate(self._waits, start=1):
            self._wait(w)
            try:
                orders = self._broker.query_orders(intent.account)
                fills = self._broker.query_fills(intent.account)
            except BrokerQueryError as exc:
                clean = False
                notes.append(f"{n}회 조회 실패: {exc}")
                self._audit(intent, "QUERY_FAILED", {"round": n})
                continue
            exact, partial = self._match(intent, orders)
            if len(exact) == 1 and not partial:
                self._adopt(intent, exact[0], fills)
                return ResolveOutcome.FOUND
            if exact or partial:
                return self._ambiguous(intent, f"{n}회 조회: 일치 {len(exact)}건, 부분 일치 {len(partial)}건")
        if self._repo.audit_count_since("UNMATCHED_EVENT", since):
            clean = False
            notes.append("짝 없는 실시간 통보 있음")
        if not clean:
            return self._ambiguous(intent, "; ".join(notes))
        with self._repo.tx():
            self._executor.close(self._repo.intent(intent_id), IntentState.NOT_ACCEPTED,
                                 f"조회 {len(self._waits)}회 모두 주문 없음 (D-047)")
        return ResolveOutcome.NOT_ACCEPTED

    # --- 매칭 -------------------------------------------------------------------
    def _match(self, intent: IntentRow, orders: list[BrokerOrderRecord]) -> tuple[list, list]:
        known = self._repo.known_order_nos(intent.account)
        kind = _KIND.get(intent.request_type, "NEW")
        target_no = None
        if kind != "NEW":
            target = self._repo.broker_order(intent.target_order_key or "")
            target_no = target.order_no if target else None
        exact: list[BrokerOrderRecord] = []
        partial: list[BrokerOrderRecord] = []
        for o in orders:
            if o.order_no in known or o.kind != kind or o.instrument != intent.instrument or o.side != intent.side:
                continue
            if kind != "NEW" and o.original_order_no != target_no:
                continue
            same_terms = kind == "CANCEL" or (
                o.quantity == intent.quantity
                and (intent.order_type != OrderType.LIMIT.value or o.price == intent.price))
            (exact if same_terms else partial).append(o)
        return exact, partial

    def _adopt(self, intent: IntentRow, rec: BrokerOrderRecord, fills: list[FillRecord]) -> None:
        with self._repo.tx():
            now = self._clock.now()
            self._executor.adopt_order(intent, rec.kind, rec.order_no, now)
            self._audit(intent, "UNKNOWN_RESOLVED", {"order_no": rec.order_no, "state": rec.state})
            # 조회로 알게 된 결과를 통보와 같은 경로로 반영한다 (체결번호로 중복 제거)
            if rec.kind == "MODIFY" and rec.original_order_no:
                self._executor.apply_event(OrderEvent(kind="MODIFIED", order_no=rec.original_order_no,
                                                      new_order_no=rec.order_no, quantity=rec.quantity,
                                                      price=rec.price))
            if rec.kind == "CANCEL" and rec.original_order_no:
                self._executor.apply_event(OrderEvent(kind="CANCELLED", order_no=rec.original_order_no))
            for f in fills:
                if f.order_no == rec.order_no:
                    self._executor.apply_event(OrderEvent(kind="FILL", order_no=f.order_no, fill_id=f.fill_id,
                                                          quantity=f.quantity, price=f.price, at=f.at))
            if rec.state in ("CANCELLED", "EXPIRED", "REJECTED"):
                self._executor.apply_event(OrderEvent(kind=rec.state, order_no=rec.order_no))

    def _ambiguous(self, intent: IntentRow, reason: str) -> ResolveOutcome:
        with self._repo.tx():
            now = self._clock.now()
            halt_id = self._repo.insert_halt("K3", intent.account, "resolver", reason, now,
                                             instrument=intent.instrument)
            self._repo.audit(now, "HALT_ACTIVATED", "resolver", halt_id, {"type": "K3", "account": intent.account,
                                                                         "intent_id": intent.intent_id})
            self._executor.alert("EMERGENCY", f"결과 불명 미해소 → K3, 사람 확인 필요: {reason}", intent.intent_id)
        return ResolveOutcome.AMBIGUOUS

    def _audit(self, intent: IntentRow, kind: str, summary: dict) -> None:
        with self._repo.tx():
            self._repo.audit(self._clock.now(), kind, "resolver", intent.intent_id, summary)
