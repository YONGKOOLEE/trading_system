"""M2 결과 불명 해소 (CP-024 D-110·D-111, T-M2-05, V-138·V-155). M1 해소기(resolver.UnknownResolver)는 그대로 둔다.

회차·대기는 M1과 같다(5회, 2·4·8·16·32초, 03 D-047·D-092). 다른 점:
- 조회 키: 고객 주문 ID(origClientOrderId)로 단건 조회. 결과 불명 의도는 orderId를 모르므로 고객 주문 ID가 기본이다.
- 발견 → 접수 반영(실행기 adopt_order) + 그 주문의 myTrades 체결(수수료·보고 금액 포함, 같은 체결 키로 중복 제거) +
  종결 상태(EXPIRED 등) 반영. 재전송 없음.
- -2013(없음): 접수 직후 반영 지연일 수 있어(B-U3 미확인) 같은 회차에 allOrders(고객 주문 ID 일치)·myTrades(모르는
  orderId의 체결 여부)를 교차 확인한다. 창 = 전송 1분 전 ~ 지금(24시간 이내, BN §7).
- -2026(보관): 전송 90일 이내 주문에 오면 이상 → 바로 K3. 이 해소기는 24시간 이내만 조회하므로 받은 -2026은 모두 이상이다.
- 5회 모두 없음 → K3(사람 확인). **자동 미접수 종결 없음**(D-111). 모르는 orderId 체결·고객 주문 ID 중복 → 바로 K3.
- 거래일을 보지 않는다(D-110): UTC 자정 통과만으로 K3가 되지 않는다. 대신 전송 후 24시간 초과면 조회 창 밖 → K3(V-155).
- 취소 의도: 대상 주문을 orderId로 조회해 종결이면 그 종결을 반영하고 취소 의도를 완료로, 아직 열려 있으면 K3(보수).
- K3로 넘긴 뒤 사람 대기·새 증거 재판정은 M1 해소기와 같다(RV-08, 상속).
B-U3·B-U21(체결 0 EXPIRED 주문의 고객 주문 ID 조회 가능 여부)은 미확인 — 합성(fake) 시험으로만 고정했다(PV-M2-02·11).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from trading_system.broker.base import BrokerQueryError, LookupKind, SpotOrderQueries
from trading_system.broker.ratelimit import Priority, call_priority
from trading_system.domain.clock import from_iso
from trading_system.domain.enums import IntentState, RequestType
from trading_system.domain.models import BrokerOrderRecord, FillRecord, OrderEvent
from trading_system.storage.repository import IntentRow

from .resolver import ResolveOutcome, ResolveProgress, UnknownResolver

LOOKUP_WINDOW = timedelta(hours=24)   # allOrders·myTrades 조회 창 (BN §7) — 넘으면 K3 (D-111)
SEND_MARGIN = timedelta(minutes=1)    # 창 시작 = 전송 기록 1분 전 (거래소·로컬 시계 차이 여유, [제안])
_TERMINAL = ("CANCELLED", "EXPIRED", "REJECTED")


class SpotUnknownResolver(UnknownResolver):
    """M2 해소기. broker는 SpotOrderQueries를 구현해야 한다(fake: FakeSpotBroker, 실어댑터: T-M2-14 이후)."""

    def step(self, p: ResolveProgress) -> ResolveOutcome | None:
        if self._repo.in_transaction:
            raise RuntimeError("결과 불명 조회는 저장 단위(tx) 밖에서만 한다")
        n = p.round + 1
        self._executor.process_events()   # 대기 중 도착한 통보 먼저 (D-047)
        intent = self._repo.intent(p.intent_id)
        if intent.state is not IntentState.UNKNOWN:
            return ResolveOutcome.FOUND
        if p.awaiting:
            return self._await_step(p)
        sent, now = from_iso(p.since), self._clock.now()
        if now - sent > LOOKUP_WINDOW:
            return self._ambiguous(intent, f"전송 후 {LOOKUP_WINDOW} 초과 — 조회 창 밖, 사람 확인 (D-111)", p)
        broker: SpotOrderQueries = self._broker  # type: ignore[assignment]
        try:
            with call_priority(Priority.RESOLVE):   # 호출 예산 최우선 (D-113)
                if intent.request_type == RequestType.CANCEL.value:
                    return self._cancel_round(intent, broker, n, p)
                verdict, rec, trades = self._find(intent, broker, sent, now)
        except BrokerQueryError as exc:
            p.clean = False
            p.notes.append(f"{n}회 조회 실패: {exc}")
            self._audit(intent, "QUERY_FAILED", {"round": n})
        else:
            if verdict == "FOUND" and rec is not None:
                self._adopt_spot(intent, rec, trades)
                return ResolveOutcome.FOUND
            if verdict != "NONE":
                return self._ambiguous(intent, f"{n}회 조회: {verdict}", p)
        p.round = n
        if p.round < len(self._waits):
            p.next_at = now + timedelta(seconds=self._waits[p.round])
            return None
        if self._repo.audit_count_since("UNMATCHED_EVENT", p.since):
            p.clean = False
            p.notes.append("짝 없는 실시간 통보 있음")
        if not p.clean:
            return self._ambiguous(intent, "; ".join(p.notes), p)
        n_q = len(self._waits)
        return self._escalate(intent, f"조회 {n_q}회 모두 주문 없음 (M2, 자동 미접수 종결 없음 D-111)",
                              f"결과 불명: 조회 {n_q}회 주문 없음 → K3, 사람 확인 필요", p)

    # --- 신규 주문 ---------------------------------------------------------------------
    def _find(self, intent: IntentRow, broker: SpotOrderQueries, sent: datetime,
              now: datetime) -> tuple[str, BrokerOrderRecord | None, list[FillRecord]]:
        cid = self._repo.client_order_of(intent.intent_id)
        start = sent - SEND_MARGIN
        end = min(now, start + LOOKUP_WINDOW)
        if cid is not None:
            lk = broker.lookup_order(intent.account, intent.instrument, order_no=None, client_order_id=cid)
            if lk.kind is LookupKind.ARCHIVED:
                return "보관(-2026) — 전송 90일 이내 주문이라 이상", None, []
            if lk.kind is LookupKind.FOUND and lk.record is not None:
                return self._found(intent, broker, lk.record, start, end)
            # -2013: allOrders 교차 (같은 회차)
            mine = [o for o in broker.orders_between(intent.account, intent.instrument, start, end)
                    if o.client_order_id == cid]
            if len(mine) > 1:
                return f"같은 고객 주문 ID 주문 {len(mine)}건", None, []
            if mine:
                return self._found(intent, broker, mine[0], start, end)
        known = self._repo.order_nos_of(intent.account, intent.instrument)
        orphan = sorted({t.order_no for t in broker.trades_between(intent.account, intent.instrument, start, end)
                         if t.order_no not in known})
        if orphan:
            return f"출처를 모르는 체결(주문 {', '.join(orphan)}) — 이 의도의 것인지 판정 불가", None, []
        return "NONE", None, []

    def _found(self, intent: IntentRow, broker: SpotOrderQueries, rec: BrokerOrderRecord, start: datetime,
               end: datetime) -> tuple[str, BrokerOrderRecord | None, list[FillRecord]]:
        if rec.kind != "NEW" or rec.instrument != intent.instrument or rec.side != intent.side:
            return f"조회 주문의 조건이 의도와 다름 ({rec.order_no})", None, []
        if rec.order_no in self._repo.order_nos_of(intent.account, intent.instrument):
            return f"조회 주문이 이미 다른 의도에 연결됨 ({rec.order_no})", None, []
        trades = [t for t in broker.trades_between(intent.account, intent.instrument, start, end)
                  if t.order_no == rec.order_no]
        return "FOUND", rec, trades

    def _adopt_spot(self, intent: IntentRow, rec: BrokerOrderRecord, trades: list[FillRecord]) -> None:
        with self._repo.tx():
            now = self._clock.now()
            self._executor.adopt_order(intent, "NEW", rec.order_no, now)
            self._audit(intent, "UNKNOWN_RESOLVED", {"order_no": rec.order_no, "state": rec.state,
                                                     "trades": len(trades), "market": "M2"})
            for t in trades:   # myTrades 체결 = 사용자 스트림과 같은 체결 키 (B-U17) → 실행기가 중복 제거
                self._executor.apply_event(OrderEvent(
                    kind="FILL", order_no=t.order_no, fill_id=t.fill_id, quantity=t.quantity, price=t.price, at=t.at,
                    account=intent.account, commission=t.commission, commission_asset=t.commission_asset,
                    instrument=intent.instrument, quote_qty=t.quote_qty, quote_confirmed=t.quote_qty is not None))
            if rec.state in _TERMINAL:
                self._executor.apply_event(OrderEvent(kind=rec.state, order_no=rec.order_no, account=intent.account,
                                                      instrument=intent.instrument,
                                                      reason=f"조회 확인 {rec.state} (결과 불명 해소)"))

    # --- 취소 ---------------------------------------------------------------------------
    def _cancel_round(self, intent: IntentRow, broker: SpotOrderQueries, n: int,
                      p: ResolveProgress) -> ResolveOutcome:
        target = self._repo.broker_order(intent.target_order_key or "")
        if target is None:
            return self._ambiguous(intent, "취소 대상 주문 기록 없음", p)
        lk = broker.lookup_order(intent.account, intent.instrument, order_no=target.order_no, client_order_id=None)
        if lk.kind is LookupKind.FOUND and lk.record is not None and lk.record.state in _TERMINAL + ("FILLED",):
            with self._repo.tx():
                if lk.record.state in _TERMINAL:
                    self._executor.apply_event(OrderEvent(kind=lk.record.state, order_no=target.order_no,
                                                          account=intent.account, instrument=intent.instrument,
                                                          reason=f"조회 확인 {lk.record.state}"))
                cur = self._repo.intent(intent.intent_id)
                if not cur.state.is_terminal:
                    self._executor.close(cur, IntentState.DONE, f"취소 결과 불명 → 대상 주문 {lk.record.state} 확인")
                self._audit(intent, "UNKNOWN_RESOLVED", {"target": target.order_no, "state": lk.record.state})
            return ResolveOutcome.FOUND
        return self._ambiguous(intent, f"{n}회 조회: 취소 대상 주문이 아직 열림·확인 불가 ({lk.kind.value})", p)
