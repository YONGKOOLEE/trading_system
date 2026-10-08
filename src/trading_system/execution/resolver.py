"""결과 불명 해소 (R-06, 01 §2.7, 02 ST-01·D-029, 03 D-047).

- 조회 5회, 각 조회 전에 2·4·8·16·32초 대기 (대기 함수 주입: 테스트·백테스트는 가짜 시계).
- 매칭(D-029): 내부에 없는 주문번호 중 (종목, 방향, 종류)가 같고 수량·가격이 의도와 같은 주문.
  취소·정정은 원주문번호가 대상 주문과 같아야 한다.
- 한 번이라도 유일 매칭 → 접수로 반영하고 조회된 체결·종결 상태를 반영한다 (재전송 없음).
- 5회 모두 조회에 성공했고 매칭 후보가 없고, 그동안 짝 없는 실시간 통보도 없어도 → **자동 미접수 종결 없음**
  (CP-031, A-084). 결과 불명 유지 + 정지 K3 + 긴급 알림 "조회 N회 주문 없음". 미접수 종결은 사람 확정(confirm_unknown)만.
  ResolveOutcome.NOT_ACCEPTED는 자동 경로에서 더 이상 만들지 않는다.
- 그 밖(후보 여럿, 부분 일치, 조회 실패 섞임, 짝 없는 통보) → 결과 불명 유지 + 정지 K3 + 긴급 알림.
- 전송 거래일 ≠ 오늘이면 미접수로 확정하지 않는다 — 조회(kt00009·ka10075)가 당일 주문만 보기 때문 (RV-09).
- K3로 넘긴 의도는 "사람 대기"(감사 UNKNOWN_ESCALATED로 영속)다. 단계형 begin()은 조회·알림을 다시 시작하지 않고
  통보 반영만 지켜본다. 사람 확정(confirm_unknown)·통보로 해소되거나, 새 짝 없는 통보(새 증거)가 오면 다시 판정한다
  (RV-08). 운영 명령의 동기 resolve()는 항상 새로 판정한다.
- 조회로 발견한 주문의 체결은 브로커가 조회 체결번호를 실시간 통보와 같은 식별자로 보장할 때만 반영한다
  (`query_fills_trusted`, 기본 False). 키움은 K-U14 미확인 → 주문만 채택하고 체결은 실시간 보류 통보·대조·사람 (RV-14).
호출 제한기 우선순위(D-048)는 T-014 어댑터에서 적용한다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from trading_system.broker.base import BrokerAdapter, BrokerQueryError
from trading_system.broker.ratelimit import Priority, call_priority
from trading_system.domain.clock import Clock, from_iso, to_iso
from trading_system.domain.enums import HaltType, IntentState, OrderType, RequestType
from trading_system.domain.models import BrokerOrderRecord, FillRecord, OrderEvent
from trading_system.storage.repository import IntentRow, Repository

from .executor import Executor

QUERY_WAITS_SECONDS = (2, 4, 8, 16, 32)  # 01 §2.7
AWAIT_RECHECK_SECONDS = 60  # 사람 대기 중 통보 반영 확인 주기 (조회 없음, RV-08)
ESCALATED = "UNKNOWN_ESCALATED"


@dataclass
class ResolveProgress:
    """의도 하나의 해소 진행 (03 D-092). 메모리에만 둔다 — 재시작하면 1회차부터 다시(더 오래 기다리는 쪽)."""

    intent_id: str
    since: str
    trade_date: str
    next_at: datetime
    round: int = 0
    clean: bool = True
    notes: list[str] = field(default_factory=list)
    awaiting: bool = False          # K3로 넘겨 사람 확인 대기 (RV-08)
    unmatched_seen: int = 0         # 넘길 때까지의 짝 없는 통보 수 — 늘면 새 증거


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
        """동기 해소: 회차마다 대기 함수를 부른다 (시험·백테스트·운영 명령). 운영 루프는 단계형(begin/step)."""
        p = self.begin(intent_id, fresh=True)
        while True:
            self._wait(self._waits[p.round])
            out = self.step(p)
            if out is not None:
                return out

    def begin(self, intent_id: str, *, fresh: bool = False) -> ResolveProgress:
        """진행 시작. 이미 K3로 넘긴 의도면(fresh 아님) 사람 대기 진행을 돌려준다 — 조회·알림 반복 없음 (RV-08)."""
        intent = self._repo.intent(intent_id)
        if intent.state is not IntentState.UNKNOWN:
            raise ValueError(f"결과 불명 의도만 해소한다: {intent.state}")
        attempt = self._repo.attempt_for(intent_id)
        since = attempt["created_at"] if attempt else to_iso(self._clock.now())
        p = ResolveProgress(intent_id, since, self._repo.trade_date_of(from_iso(since)),
                            self._clock.now() + timedelta(seconds=self._waits[0]))
        if not fresh:
            esc = self._last_escalation(intent_id)
            if esc is not None:
                p.awaiting, p.unmatched_seen, p.next_at = True, int(esc.get("unmatched", 0)), self._clock.now()
        return p

    def step(self, p: ResolveProgress) -> ResolveOutcome | None:
        """조회 한 회차 (03 D-092). 결론이 나면 결과, 아니면 None(다음 회차 시각은 p.next_at)."""
        if self._repo.in_transaction:
            raise RuntimeError("결과 불명 조회는 저장 단위(tx) 밖에서만 한다")
        intent = self._repo.intent(p.intent_id)
        self._trade_date = p.trade_date
        n = p.round + 1
        self._executor.process_events()  # 대기 중 도착한 실시간 통보를 반영해야 "통보 없음"을 판단할 수 있다 (D-047)
        if self._repo.intent(p.intent_id).state is not IntentState.UNKNOWN:
            return ResolveOutcome.FOUND  # 보류 통보 반영 등으로 이미 해소됨
        if p.awaiting:
            return self._await_step(p)
        if self._today() != p.trade_date:
            # 조회는 당일 주문만 본다 → 전일 전송분은 "없음"을 증명할 수 없다 (RV-09, D-047)
            return self._ambiguous(intent, f"전송 거래일 {p.trade_date} ≠ 오늘 — 당일 조회로 판정 불가", p)
        trusted = bool(getattr(self._broker, "query_fills_trusted", False))
        try:
            with call_priority(Priority.RESOLVE):  # 호출 제한기 최우선 (03 D-048)
                orders = self._broker.query_orders(intent.account)
                fills = self._broker.query_fills(intent.account) if trusted else []
        except BrokerQueryError as exc:
            p.clean = False
            p.notes.append(f"{n}회 조회 실패: {exc}")
            self._audit(intent, "QUERY_FAILED", {"round": n})
        else:
            if self._today() != p.trade_date:   # 조회 도중 날짜가 바뀜 — 어느 날의 목록인지 모름 (RV-09)
                return self._ambiguous(intent, f"{n}회 조회 중 거래일 경과", p)
            exact, partial = self._match(intent, orders)
            if len(exact) == 1 and not partial:
                self._adopt(intent, exact[0], fills)
                return ResolveOutcome.FOUND
            if exact or partial:
                return self._ambiguous(intent, f"{n}회 조회: 일치 {len(exact)}건, 부분 일치 {len(partial)}건", p)
        p.round = n
        if p.round < len(self._waits):
            p.next_at = self._clock.now() + timedelta(seconds=self._waits[p.round])
            return None
        if self._repo.audit_count_since("UNMATCHED_EVENT", p.since):
            p.clean = False
            p.notes.append("짝 없는 실시간 통보 있음")
        if not p.clean:
            return self._ambiguous(intent, "; ".join(p.notes), p)
        # [CP-031, A-084] 5회 모두 "주문 없음"이어도 자동 미접수 종결하지 않는다. 종결은 예약·소유권을 풀어 재진입을 허용하므로
        # 브로커 접수 반영이 늦으면 이중 포지션이 된다. 결과 불명 유지 + K3 + 사람 확정(confirm_unknown)으로만 종결한다 (D-111과 같은 규칙).
        n_q = len(self._waits)
        return self._escalate(intent, f"조회 {n_q}회 모두 주문 없음",
                              f"결과 불명: 조회 {n_q}회 주문 없음 → K3, 사람 확인 필요", p)

    # --- 매칭 -------------------------------------------------------------------
    def _match(self, intent: IntentRow, orders: list[BrokerOrderRecord]) -> tuple[list, list]:
        known = self._repo.known_order_nos(intent.account, self._trade_date)
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
            for f in fills:   # 신뢰 가능한 브로커(fake)만 — 키움은 빈 목록 (K-U14, RV-14)
                if f.order_no == rec.order_no:
                    self._executor.apply_event(OrderEvent(kind="FILL", order_no=f.order_no, fill_id=f.fill_id,
                                                          quantity=f.quantity, price=f.price, at=f.at))
            if rec.state in ("CANCELLED", "EXPIRED", "REJECTED"):
                self._executor.apply_event(OrderEvent(kind=rec.state, order_no=rec.order_no))

    def _ambiguous(self, intent: IntentRow, reason: str, p: ResolveProgress) -> ResolveOutcome:
        return self._escalate(intent, reason, f"결과 불명 미해소 → K3, 사람 확인 필요: {reason}", p)

    def _escalate(self, intent: IntentRow, reason: str, message: str, p: ResolveProgress) -> ResolveOutcome:
        """결과 불명 유지 + K3 + 긴급 알림 + 사람 대기 영속. 같은 범위의 K3가 이미 있으면 정지 관리자가 재사용한다."""
        self._executor.raise_halt(HaltType.K3, "resolver", reason, intent.account, instrument=intent.instrument)
        self._executor.alert("EMERGENCY", message, intent.intent_id)
        # 사람 대기 표시 (RV-08): 다음 begin()이 조회·알림을 되풀이하지 않게 영속 기록
        self._audit(intent, ESCALATED, {"unmatched": self._repo.audit_count_since("UNMATCHED_EVENT", p.since)})
        return ResolveOutcome.AMBIGUOUS

    # --- 사람 대기 (RV-08) ---------------------------------------------------------------
    def _await_step(self, p: ResolveProgress) -> None:
        """조회·알림 없이 기다린다. 새 짝 없는 통보(새 증거)가 오면 1회차부터 다시 판정한다."""
        now = self._clock.now()
        if self._repo.audit_count_since("UNMATCHED_EVENT", p.since) > p.unmatched_seen:
            p.awaiting, p.round, p.clean, p.notes = False, 0, True, []
            p.next_at = now + timedelta(seconds=self._waits[0])
            return None
        p.next_at = now + timedelta(seconds=AWAIT_RECHECK_SECONDS)
        return None

    def _last_escalation(self, intent_id: str) -> dict | None:
        # 저장소에 (종류, ref) 조회가 없어 감사 전체를 훑는다. begin()은 의도마다 엔진 수명에 한 번만 불린다.
        last = None
        for e in self._repo.audit_events():
            if e["kind"] == ESCALATED and e["ref"] == intent_id:
                last = e
        return json.loads(last["summary"]) if last else None

    def _today(self) -> str:
        return self._repo.trade_date_of(self._clock.now())

    def _audit(self, intent: IntentRow, kind: str, summary: dict) -> None:
        with self._repo.tx():
            self._repo.audit(self._clock.now(), kind, "resolver", intent.intent_id, summary)
