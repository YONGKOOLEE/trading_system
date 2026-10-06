"""대조와 재시작 복구 판정 (R-08, R-18, 01 §2.11, 02 ST-07·D-034·D-035, 03 §6.3·D-094).

- 내부 원장(체결 누적)을 브로커 조회에 맞춰 **자동으로 고치지 않는다**. 차이는 불일치 항목으로 기록하고 K4 (D-035).
- 비교: 보유 수량, 브로커에만 있는 주문(외부 거래·미확인), 내부에만 열린 주문, 상태 차이(브로커 종결·내부 열림),
  지난 거래일에 열린 채 남은 주문, 누락 체결(체결번호가 있는 조회 체결만, D-094).
- 조회 실패는 불일치가 아니다(결과를 모름). 기록만 하고 판단은 호출자(K5 정책)에 맡긴다.
- 일치하면 K3·K4·K5의 재개 조건 `reconciled`를 충족으로 보고한다.
- 재시작 복구(01 §2.11): 모드·환경, 저장 무결성, 시계, 브로커 조회, 대조 일치·결과 불명 없음을 모두 확인한다.
  하나라도 실패하면 해당 정지(K7·K6·K5·K4·K3)를 발동하고, 모두 충족하면 K8 `recovery_ok`를 보고한다.
  K8 해제는 사용자 재개로만 한다.
- 실행 중 K8(D-064 루프 지연·시계 점프, 엔진 단계 오류): 대조가 끝날 때마다(tick마다가 아님) 같은 조건을 다시 판정해
  `recovery_ok`를 보고한다(RecoveryChecker.evaluate, 3차 검토 #1). 정지는 새로 걸지 않는다(각 조건의 정지는 해당
  경로가 이미 건다). 자동 해제 없음 — 사용자 재개.
- 결과 불명 의도와 조건이 같은 브로커 주문(계좌·종목·방향·수량·주문 종류, 전송 시도가 같은 거래일)은 그 의도가
  결과 불명인 동안 '미확인 브로커 주문'으로 잡지 않는다 — 결과 불명 해소(해소기, 모호하면 K3)에 맡긴다(3차 검토 #2).
  해소기가 채택하면 알려진 주문이 되고, 그전에 열린 같은 주문의 '미확인 브로커 주문' 항목은 다음 대조에서 자동 종결한다.
- 장 마감 후 대조(trigger CLOSE, 03 §6.4): 오늘 주문 중 내부는 열려 있으나 브로커가 소멸(EXPIRED)·미표시이고
  체결 수량이 같으면 실행기 경로로 만료 종결(예약 해제)한다 — 상태 차이(K4)로 보지 않는다.
  브로커에 열린 채 남은 오늘 주문은 경고 알림(마감 후 대조는 하루 1회).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from trading_system.broker.base import BrokerAdapter, BrokerQueryError
from trading_system.broker.ratelimit import Priority, call_priority
from trading_system.domain.clock import Clock
from trading_system.domain.enums import HaltType, IntentState, Mode
from trading_system.domain.sinks import AlertSink, HaltControl
from trading_system.storage.jsonlog import Masker
from trading_system.storage.repository import BrokerOrderRow, Repository

OPEN = ("ACCEPTED", "PARTIAL")
CLOSE_TRIGGER = "CLOSE"   # 장 마감 후 1회 대조 (03 §6.3 "마감 후", §6.4) — 엔진이 이 trigger로 부른다
_INTENT_KIND = {"CANCEL": "CANCEL", "MODIFY": "MODIFY"}   # 의도 요청 유형 → 브로커 주문 종류 (resolver._KIND와 같음)
K5_LINK_REST = "link:rest"   # halt/manager.py K5_LINK_REST와 같은 값 (K5 조회 경로, RV-02)
_ALLOWED_ENV = {Mode.BACKTEST: {"fake"}, Mode.PAPER: {"mock"}, Mode.LIVE: {"real"}}


class ItemKind:
    QTY_MISMATCH = "QTY_MISMATCH"                  # 수량 차이
    UNKNOWN_BROKER_ORDER = "UNKNOWN_BROKER_ORDER"  # 미확인 브로커 주문 (외부 거래 의심)
    MISSING_FILL = "MISSING_FILL"                  # 누락 체결
    EXCESS_FILL = "EXCESS_FILL"                    # 내부 체결이 브로커보다 많음 (T-017)
    INTERNAL_ONLY_ORDER = "INTERNAL_ONLY_ORDER"    # 내부에만 있는 주문
    STATE_MISMATCH = "STATE_MISMATCH"              # 브로커는 종결, 내부는 열림
    STALE_ORDER = "STALE_ORDER"                    # 지난 거래일 주문이 내부에서 열린 채 남음


@dataclass(frozen=True)
class ReconcileResult:
    recon_id: str
    result: str                 # MATCH / MISMATCH / QUERY_FAILED
    items: tuple[str, ...] = ()  # 불일치 항목 ID


@dataclass
class _Comparison:
    found: list[tuple[str, str | None, str | None, str | None, str | None, dict]]
    broker_pos: dict[str, Decimal]
    expirable: list[BrokerOrderRow] = field(default_factory=list)   # 마감 후: 만료 종결할 내부 주문 (§6.4)
    open_after_close: list[str] = field(default_factory=list)        # 마감 후: 브로커에 열린 채 남은 주문번호
    deferred: list[str] = field(default_factory=list)                # 결과 불명 해소에 맡긴 브로커 주문번호
    fill_totals: dict[str, Decimal] = field(default_factory=dict)  # 주문번호 → 브로커 체결 합 (T-017)


@dataclass
class RecoveryReport:
    ok: bool
    failures: list[str] = field(default_factory=list)
    halts: list[str] = field(default_factory=list)


OPEN_ITEM_REMINDER = timedelta(minutes=60)   # T-017: 미해소 불일치 상기 간격 (1일차 1분마다 EMERGENCY 361건)
QUERY_FAILURES_TO_K5 = 3   # 연속 대조 조회 실패 → K5 (03 타임아웃 표 "최대 3회 → K5", e2e 결함 후보 2)


def _avg_price(rows: list) -> Decimal:  # type: ignore[type-arg]
    """체결 행 평균가 — 행 가격 중 가장 긴 소수 자릿수로 반올림(ROUND_HALF_UP). M1은 원 단위 정수가 된다 (T-017)."""
    qty = sum((f.quantity for f in rows), Decimal("0"))
    avg = sum((f.quantity * f.price for f in rows), Decimal("0")) / qty
    places = max(max(-f.price.as_tuple().exponent, 0) for f in rows)  # type: ignore[operator]
    return avg.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


class Reconciler:
    def __init__(self, repo: Repository, broker: BrokerAdapter, clock: Clock, halts: HaltControl | None,
                 alerts: AlertSink, drain_events: Callable[[], object] | None = None,
                 requery_wait: Callable[[], None] | None = None) -> None:
        self._repo = repo
        self._broker = broker
        self._clock = clock
        self._halts = halts
        self._alerts = alerts
        self._drain_events = drain_events   # 대기 중인 브로커 통보 반영 (실행기 process_events, RV-24)
        self._failures: dict[str, int] = {}  # 계좌별 연속 조회 실패 수
        self._reminded: dict[str, datetime] = {}  # 계좌별 마지막 불일치 알림 시각 (T-017 반복 억제)
        # 재조회 전 대기: 01 "재조회 1회 (체결 반영 지연 확인)" — 실시간 체결 통보가 조회보다 늦게 오는 지연을 기다린다.
        # 실운영은 main이 짧은 실제 대기를 넘긴다(판정·실행 스레드만 멈춤, 감시 스레드는 계속). 시험 기본은 대기 없음
        self._requery_wait = requery_wait
        # 마감 후 만료 종결 (03 §6.4): (내부 주문, 거래일) → 실행기의 소멸 통보 반영 경로. 엔진이 연결한다(실행기 소유).
        # 없으면 만료 종결하지 않고 종전대로 상태 차이로 기록한다(보수적)
        self.expire_order: Callable[[BrokerOrderRow, str], None] | None = None
        # 대조 뒤 알림 (엔진: 실행 중 K8 복구 판정). 시작 대조(STARTUP)에도 불린다 — 받는 쪽이 거른다
        self.listeners: list[Callable[[str, str, ReconcileResult], None]] = []

    def run(self, account: str, trigger: str) -> ReconcileResult:
        if self._repo.in_transaction:
            raise RuntimeError("대조 조회는 저장 단위(tx) 밖에서만 한다")
        try:
            positions, orders, fills = self._query(account)
        except BrokerQueryError as exc:
            err = Masker().text(str(exc))  # 브로커 오류 문구에 URL 토큰·계좌번호가 섞일 수 있다
            with self._repo.tx():
                rid = self._repo.insert_reconciliation(account, trigger, "QUERY_FAILED",
                                                       {"error": err}, self._clock.now())
            self._alerts.alert("WARNING", f"대조 조회 실패 ({trigger}): {err}", rid)
            n = self._failures.get(account, 0) + 1
            self._failures[account] = n
            if n >= QUERY_FAILURES_TO_K5 and self._halts is not None:
                # 03 타임아웃 표 "일반 조회(대조·잔고) 최대 3회 → K5", 01 §2.7 "API 단절·연속 호출 실패 → K5".
                # 조회 경로(link:rest) 끊김으로 기록 → 다음 대조 성공(조회 3종)이 경로 정상·reconciled를 보고해 자동 해제
                self._halts.activate(HaltType.K5, "reconcile", f"대조 조회 연속 {n}회 실패: {err}", account)
            return ReconcileResult(rid, "QUERY_FAILED")
        self._failures[account] = 0

        if self._halts is not None:
            # 조회 3종 성공 = 브로커 조회 경로 연결 증거 (RV-02). 실시간 경로는 따로 보고된다(끊긴 경로는 안 풀림)
            self._halts.report_condition(HaltType.K5, account, K5_LINK_REST, True, f"대조 조회 성공 ({trigger})")
        self._drain()  # 조회 중 도착한 통보를 먼저 반영 (RV-24, A-043 (4))
        now = self._clock.now()
        closing = trigger == CLOSE_TRIGGER
        cmp = self._compare(account, positions, orders, fills, now, closing)
        requeried = False
        if cmp.found:
            # 01 장애 정책 "잔고 불일치": 재조회 1회(체결 반영 지연 확인) 후에도 불일치면 K4 (RV-24).
            # 재조회는 이 호출 안에서(판정·실행 스레드) 끝나므로 그동안 이 계좌의 새 주문은 나가지 않는다.
            # 재조회가 실패하면 처음 결과로 판정한다(실패는 일치 증거가 아님)
            if self._requery_wait is not None:
                self._requery_wait()
            self._drain()
            try:
                positions2, orders2, fills2 = self._query(account)
            except BrokerQueryError:
                pass
            else:
                requeried = True
                self._drain()
                positions, orders, fills = positions2, orders2, fills2
                now = self._clock.now()
                cmp = self._compare(account, positions, orders, fills, now, closing)
        expired = self._expire_at_close(account, cmp, now)
        found, broker_pos = cmp.found, cmp.broker_pos

        with self._repo.tx():
            # 같은 내용의 열린 항목은 다시 만들지 않는다. 이번에 안 보여도 열린 항목은 사람이 해소하기 전까지 남는다
            # (예: 외부 주문이 다음 날 조회에서 사라짐) — 열린 항목이 있으면 일치로 보지 않는다
            auto_closed = (self._close_known_order_items(account, now)
                           + self._close_matched_fill_items(account, now, cmp.fill_totals))
            open_before = {(i["kind"], i["instrument"], i["ref"], i["internal_value"], i["broker_value"]): i["item_id"]
                           for i in self._repo.open_recon_items(account)}
            result = "MISMATCH" if found or open_before else "MATCH"
            rid = self._repo.insert_reconciliation(account, trigger, result, {
                "positions": {k: str(v) for k, v in broker_pos.items()}, "orders": len(orders), "fills": len(fills),
                "items": len(found), "open_items_before": len(open_before), "requeried": requeried,
                "deferred_unknown": cmp.deferred, "expired_at_close": expired, "auto_closed_items": auto_closed,
                "open_after_close": cmp.open_after_close}, now)
            ids = tuple(open_before.get((kind, inst, ref, iv, bv)) or
                        self._repo.insert_recon_item(rid, account, kind, inst, ref, iv, bv, json.dumps(detail))
                        for kind, inst, ref, iv, bv, detail in found)
            self._repo.audit(now, "RECONCILE", "reconciler", rid, {"trigger": trigger, "result": result,
                                                                 "items": len(found), "open_before": len(open_before)})
        if result == "MISMATCH":
            n = len(set(ids) | set(open_before.values()))
            if self._halts is not None:
                self._halts.activate(HaltType.K4, "reconciler", f"대조 불일치 {n}건 ({trigger})", account)
                self._halts.report_condition(HaltType.K4, account, "reconciled", False, rid)
            # T-017: 새 항목이 생길 때만 긴급 알림. 같은 항목이 열려 있는 동안은 60분마다 WARNING으로 상기만 한다
            # (K4는 그대로 유지 — 알림 횟수만 줄인다)
            new = set(ids) - set(open_before.values())
            last = self._reminded.get(account)
            if new or last is None:   # 새 항목, 또는 이 프로세스에서 처음 보는 불일치(재시작 직후)는 긴급
                self._alerts.alert("EMERGENCY", f"대조 불일치 {n}건 — 사람 확인 필요 (reconcile-resolve)", rid)
                self._reminded[account] = now
            elif last is None or now - last >= OPEN_ITEM_REMINDER:
                self._alerts.alert("WARNING", f"대조 불일치 {n}건 미해소 — 사람 확인 필요 (reconcile-resolve)", rid)
                self._reminded[account] = now
        else:
            self._reminded.pop(account, None)
        if result != "MISMATCH" and self._halts is not None:
            for k in (HaltType.K3, HaltType.K4, HaltType.K5):
                self._halts.report_condition(k, account, "reconciled", True, rid)
        if cmp.open_after_close:
            # 03 §6.4 "남아 있으면 알림하고 다음 거래일 시작 전 사용자 확인 대상" (마감 후 대조는 하루 1회 → 알림 1회)
            self._alerts.alert("WARNING", f"장 마감 후 미체결 {len(cmp.open_after_close)}건이 브로커에 남아 있음 "
                                          f"(주문번호 {', '.join(cmp.open_after_close)}) — 다음 거래일 시작 전 사용자 확인",
                               rid)
        res = ReconcileResult(rid, result, ids)
        for fn in list(self.listeners):
            fn(account, trigger, res)
        return res

    def _expire_at_close(self, account: str, cmp: _Comparison, now: datetime) -> list[str]:
        """마감 후 대조: 브로커에서 소멸한 오늘 주문을 실행기 경로(소멸 통보 반영)로 만료 종결한다 (03 §6.4)."""
        if not cmp.expirable:
            return []
        today = self._repo.trade_date_of(now)
        done: list[str] = []
        for io in cmp.expirable:
            if self.expire_order is not None:
                self.expire_order(io, today)
            after = self._repo.broker_order(io.order_key)
            if after is not None and after.state not in OPEN:
                done.append(io.order_no)
            else:   # 만료 경로가 없거나 반영되지 않았으면 종전대로 상태 차이 (보수적)
                cmp.found.append((ItemKind.STATE_MISMATCH, io.instrument, io.order_no, io.state, "EXPIRED",
                                  {"order_key": io.order_key, "broker_state": "마감 후 소멸·미표시"}))
        if done:
            with self._repo.tx():
                self._repo.audit(now, "EXPIRED_AT_CLOSE", "reconciler", account,
                                 {"order_nos": done, "basis": "03 §6.4 마감 후 대조 — 브로커 미체결 없음"})
        return done

    def _close_known_order_items(self, account: str, now: datetime) -> list[str]:
        """열린 '미확인 브로커 주문' 항목 중 그 뒤 의도에 연결된(결과 불명 해소기가 채택한) 주문은 자동 종결한다.
        원장은 바꾸지 않는다 — 항목 상태만. 항목의 거래일 기준으로 찾는다(주문번호 유일 범위 = 거래일, D-046)."""
        closed: list[str] = []
        for item in self._repo.open_recon_items(account):
            if item["kind"] != ItemKind.UNKNOWN_BROKER_ORDER or not item["ref"]:
                continue
            try:
                trade_date = json.loads(item["detail"] or "{}").get("trade_date")
            except ValueError:
                continue
            if not trade_date:
                continue
            order = self._repo.broker_order_by_no(item["ref"], trade_date, account)
            if order is None or order.intent_id is None:
                continue
            self._repo.resolve_recon_item(item["item_id"], "AUTO_ADOPTED", "reconciler", now)
            self._repo.audit(now, "RECON_ITEM_AUTO_RESOLVED", "reconciler", item["item_id"],
                             {"order_no": item["ref"], "intent_id": order.intent_id, "trade_date": trade_date})
            closed.append(item["item_id"])
        return closed

    def _close_matched_fill_items(self, account: str, now: datetime, totals: dict[str, Decimal]) -> list[str]:
        """열린 체결 수량 항목(누락·초과) 중 주문의 내부 반영 수량이 브로커 체결 합과 **같아진** 것은 자동 종결한다 (T-017).

        1일차처럼 체결번호 차이로 생긴 오판 항목, 또는 뒤늦게 통보로 반영된 체결. 원장은 바꾸지 않는다 — 항목 상태만.
        브로커 합: 이번 조회에 그 주문이 있으면 그 값(totals), 없으면(지난 거래일 등) 항목의 broker_total.
        broker_total이 없는 이전 형식 항목은 주문이 전량 체결(FILLED)이고 수량이 모두 같을 때만 종결."""
        closed: list[str] = []
        for item in self._repo.open_recon_items(account):
            if item["kind"] not in (ItemKind.MISSING_FILL, ItemKind.EXCESS_FILL):
                continue
            try:
                detail = json.loads(item["detail"] or "{}")
                order_key = detail["order_key"]
                order = self._repo.broker_order(order_key)
                if order is None:
                    continue
                if order_key.split("|")[1] == self._repo.trade_date_of(now) and order.order_no in totals:
                    broker_total = totals[order.order_no]
                elif detail.get("broker_total"):
                    broker_total = Decimal(detail["broker_total"])
                elif item["kind"] == ItemKind.MISSING_FILL and order.state == "FILLED" \
                        and order.filled_quantity == order.quantity == Decimal(detail["quantity"]):
                    broker_total = order.quantity
                else:
                    continue
            except (ValueError, KeyError, ArithmeticError):
                continue
            if order.filled_quantity != broker_total:
                continue
            self._repo.resolve_recon_item(item["item_id"], "AUTO_MATCHED", "reconciler", now)
            self._repo.audit(now, "RECON_ITEM_AUTO_RESOLVED", "reconciler", item["item_id"],
                             {"order_key": order_key, "broker_total": str(broker_total),
                              "internal_filled": str(order.filled_quantity), "basis": "주문별 체결 수량 일치 (K-U14)"})
            closed.append(item["item_id"])
        return closed

    @staticmethod
    def _fill_totals(fills: list, orders: list) -> dict[str, Decimal]:  # type: ignore[type-arg]
        """주문별 브로커 체결 합 = max(조회 체결 행 합, 브로커 주문의 체결 수량). 두 근거 중 큰 값 (T-017 재검토 5):
        체결번호 없는 행(D-094)·행 단위 미확인(K-U14)이어도 미체결 조회(주문 − 미체결)가 체결을 보여 준다."""
        totals: dict[str, Decimal] = {}
        for f in fills:
            if f.fill_id:
                totals[f.order_no] = totals.get(f.order_no, Decimal("0")) + f.quantity
        for o in orders:
            if o.kind != "CANCEL" and o.filled_quantity > totals.get(o.order_no, Decimal("0")):
                totals[o.order_no] = o.filled_quantity
        return totals

    def _drain(self) -> None:
        if self._drain_events is not None:
            self._drain_events()

    def _query(self, account: str) -> tuple[list, list, list]:  # type: ignore[type-arg]
        with call_priority(Priority.RECONCILE):  # 03 D-048
            return (self._broker.query_positions(account), self._broker.query_orders(account),
                    self._broker.query_fills(account))

    def _unknown_intent_terms(self, account: str, today: str) -> set[tuple[str, str, str, Decimal, str | None]]:
        """결과 불명 의도의 (주문 종류, 종목, 방향, 수량, 대상 원주문번호). 전송 시도가 오늘 거래일인 것만.
        브로커 조회 주문에는 시각이 없어 '시도 이후'는 같은 거래일로만 좁힌다."""
        terms: set[tuple[str, str, str, Decimal, str | None]] = set()
        for i in self._repo.intents_in_state(IntentState.UNKNOWN):
            if i.account != account:
                continue
            attempt = self._repo.attempt_for(i.intent_id)
            if attempt is None or self._repo.trade_date_of(datetime.fromisoformat(attempt["created_at"])) != today:
                continue
            kind = _INTENT_KIND.get(i.request_type, "NEW")
            target_no = None
            if kind != "NEW":
                target = self._repo.broker_order(i.target_order_key or "")
                target_no = target.order_no if target else None
            terms.add((kind, i.instrument, i.side, i.quantity, target_no))
        return terms

    def _compare(self, account: str, positions: list, orders: list, fills: list,  # type: ignore[type-arg]
                 now: datetime, closing: bool = False) -> _Comparison:
        today = self._repo.trade_date_of(now)
        found: list[tuple[str, str | None, str | None, str | None, str | None, dict]] = []

        internal = {i: p.quantity for i, p in self._repo.positions(account).items() if p.quantity != 0}
        broker_pos = {p.instrument: p.quantity for p in positions if p.quantity != 0}
        for inst in sorted(set(internal) | set(broker_pos)):
            iq, bq = internal.get(inst, Decimal("0")), broker_pos.get(inst, Decimal("0"))
            if iq != bq:
                found.append((ItemKind.QTY_MISMATCH, inst, None, str(iq), str(bq), {}))

        known = self._repo.known_order_nos(account, today)
        by_no = {o.order_no: o for o in orders}
        unknown_terms = self._unknown_intent_terms(account, today)
        cmp = _Comparison(found, broker_pos)
        cmp.fill_totals = self._fill_totals(fills, orders)
        for o in orders:
            if o.order_no not in known and o.kind != "CANCEL":
                if (o.kind, o.instrument, o.side, o.quantity,
                        o.original_order_no if o.kind != "NEW" else None) in unknown_terms:
                    # 결과 불명 의도의 주문일 수 있다 → 해소기(조회 → 채택, 모호하면 K3)에 맡긴다 (CLAUDE.md §8-6)
                    cmp.deferred.append(o.order_no)
                    continue
                found.append((ItemKind.UNKNOWN_BROKER_ORDER, o.instrument, o.order_no, None,
                              f"{o.side} {o.quantity} {o.state}",
                              {"side": o.side, "quantity": str(o.quantity), "trade_date": today,
                               "detected_at": now.isoformat()}))
        for io in self._repo.open_orders(account):
            trade_date = io.order_key.split("|")[1]
            if trade_date != today:
                found.append((ItemKind.STALE_ORDER, io.instrument, io.order_no, io.state, None,
                              {"order_key": io.order_key}))
                continue
            b = by_no.get(io.order_no)
            if closing and self._expired_at_close(io, b, fills):
                cmp.expirable.append(io)
                continue
            if b is None:
                found.append((ItemKind.INTERNAL_ONLY_ORDER, io.instrument, io.order_no, io.state, None,
                              {"order_key": io.order_key}))
            elif b.state not in OPEN:
                found.append((ItemKind.STATE_MISMATCH, io.instrument, io.order_no, io.state, b.state,
                              {"order_key": io.order_key, "broker_state": b.state}))
        # T-017 (K-U14 실측 2026-10-06): 키움 모의 kt00009 cntr_no는 실시간 909 체결번호와 다른 값(주문번호와 같음)
        # → 체결번호로 짝지으면 같은 체결이 누락 체결로 잡힌다. 주문별 체결 수량 합(브로커 조회 vs 내부 반영)으로
        # 대조하고, 브로커 쪽이 많을 때만 그 차이를 누락 체결로 본다. 체결번호 없는 조회 행은 종전대로 쓰지 않는다(D-094)
        by_order: dict[str, list] = {}  # type: ignore[type-arg]
        for f in fills:
            if f.fill_id:
                by_order.setdefault(f.order_no, []).append(f)
        for order_no, broker_total in sorted(cmp.fill_totals.items()):
            order = self._repo.broker_order_by_no(order_no, today, account)
            # 외부 주문 체결은 수량 대조로
            if order is None or order.intent_id is None:
                continue
            internal_total = order.filled_quantity
            if broker_total == internal_total:
                continue
            if broker_total < internal_total:
                # 내부가 더 많다(이중 반영 등). 같은 날 매수·매도 초과가 상쇄돼 보유 수량 대조로 안 보일 수 있다 (T-017)
                found.append((ItemKind.EXCESS_FILL, order.instrument, order_no, str(internal_total),
                              str(broker_total), {"order_key": order.order_key, "broker_total": str(broker_total),
                                                  "internal_total": str(internal_total)}))
                continue
            rows = by_order.get(order_no, [])
            detail = {"order_key": order.order_key, "fill_id": f"Q{order_no}-{broker_total}",
                      "quantity": str(broker_total - internal_total), "broker_total": str(broker_total),
                      "internal_total": str(internal_total), "basis": "주문별 체결 수량 합 (K-U14)"}
            shown = str(broker_total)
            if rows:
                avg = _avg_price(rows)
                detail.update(price=str(avg), at=max(rows, key=lambda f: f.at).at.isoformat())
                shown = f"{broker_total}@{avg}"
            # APPLY_FILL이 반영할 체결: 차이 수량, 평균가(행 가격 자릿수로 반올림), 누계별 식별. 체결 행이 없으면
            # (브로커 주문 체결 수량만 있음) 가격을 몰라 APPLY_FILL 불가 → 사람이 증권사 확인(BROKER_ACTION)
            found.append((ItemKind.MISSING_FILL, order.instrument, order_no, str(internal_total), shown, detail))
        if closing:
            # 누락 체결이 있는 주문, 보유 수량이 다른 종목의 주문은 만료 종결하지 않는다 — 상태 차이로 사람 확인
            missing = {f[2] for f in found if f[0] == ItemKind.MISSING_FILL}
            qty_diff = {f[1] for f in found if f[0] == ItemKind.QTY_MISMATCH}
            for io in [io for io in cmp.expirable if io.order_no in missing or io.instrument in qty_diff]:
                cmp.expirable.remove(io)
                found.append((ItemKind.STATE_MISMATCH, io.instrument, io.order_no, io.state, "EXPIRED",
                              {"order_key": io.order_key, "broker_state": "마감 후 소멸·미표시 + 누락 체결"}))
            for io in self._repo.open_orders(account):
                b = by_no.get(io.order_no)
                if io.order_key.split("|")[1] == today and b is not None and b.state in OPEN:
                    cmp.open_after_close.append(io.order_no)
        return cmp

    @staticmethod
    def _expired_at_close(io: BrokerOrderRow, b: object, fills: list) -> bool:  # type: ignore[type-arg]
        """마감 후: 내부는 열림, 브로커가 명시적으로 소멸(EXPIRED)이라 하고 체결 수량이 같으면 만료 종결 대상.
        브로커 미표시(b None)는 만료 증거로 쓰지 않는다 — 조회 누락과 구별할 수 없다(4차 검토 F-C). 키움 소멸 표시
        (CLOSED_UNKNOWN 등)를 만료로 볼지는 K-U3·PV-03 실측 후 결정(DP-40). 그 밖은 종전대로 상태 차이(사람 확인)."""
        if b is None:
            return False
        return getattr(b, "state", None) == "EXPIRED" and getattr(b, "filled_quantity", None) == io.filled_quantity


class RecoveryChecker:
    """재시작 복구 조건 (01 §2.11). 통과해도 K8을 풀지 않는다 — 사용자 재개."""

    def __init__(self, repo: Repository, reconciler: Reconciler, halts: HaltControl,
                 clock_ok: Callable[[], bool | None]) -> None:
        self._repo = repo
        self._reconciler = reconciler
        self._halts = halts
        self._clock_ok = clock_ok

    def check(self, account: str) -> RecoveryReport:
        rep = RecoveryReport(ok=True)

        def fail(k: HaltType, why: str) -> None:
            rep.ok = False
            rep.failures.append(why)
            rep.halts.append(self._halts.activate(k, "recovery", why, account))

        acct = self._repo.account(account)
        if acct is None or acct.mode is not self._repo.mode or acct.broker_env not in _ALLOWED_ENV[self._repo.mode]:
            rep.ok = False
            rep.failures.append("1. 모드·브로커 환경 불일치 (R-01)")  # 시작 점검 실패 — 프로세스는 시작하지 않아야 함
            return rep
        storage_ok = self._repo.integrity_ok()
        if not storage_ok:
            fail(HaltType.K7, "2. 저장 상태 무결성 실패 (R-25)")
        if self._clock_ok() is not True:
            fail(HaltType.K6, "3. 시계 오차 정상 아님 (R-24)")
        r = self._reconciler.run(account, "STARTUP")
        if r.result == "QUERY_FAILED":
            fail(HaltType.K5, "4. 브로커 미체결·체결·잔고 조회 실패")
        elif r.result == "MISMATCH":
            rep.ok = False
            rep.failures.append(f"5. 대조 불일치 {len(r.items)}건 → K4")
        unknown = [i for i in self._repo.intents_in_state(IntentState.UNKNOWN) if i.account == account]
        if unknown:
            fail(HaltType.K3, f"5. 결과 불명 의도 {len(unknown)}건 미해소")
        # 재시작 정리(recover_on_start)를 거치지 않았다는 뜻 — 전송 결과를 모르는 의도가 남아 있다
        unsettled = [i for st in (IntentState.SENT, IntentState.PENDING_SEND)
                     for i in self._repo.intents_in_state(st) if i.account == account]
        if unsettled:
            fail(HaltType.K3, f"5. 전송 대기·응답 대기 의도 {len(unsettled)}건 — 재시작 정리 미실행")
        # K7 storage_ok (RV-03): 재시작 무결성 점검 통과 + 이번 점검의 대조 기록 쓰기 성공일 때만 충족 보고.
        # K7은 사용자 재개 유형이라 여기서는 재개 대기가 될 뿐 풀리지 않는다. 실행 중 저장 실패는 실행기가
        # 메모리에서 계속 차단하므로, 실행 중에는 보고하지 않는다(재시작 후 이 점검으로만 — 보수적)
        self._halts.report_condition(HaltType.K7, None, "storage_ok", storage_ok,
                                     f"무결성 점검 {'통과' if storage_ok else '실패'}, 대조 기록 {r.recon_id}")
        if rep.ok:
            self._halts.report_condition(HaltType.K8, None, "recovery_ok", True, f"대조 {r.recon_id}")
        else:
            self._halts.report_condition(HaltType.K8, None, "recovery_ok", False, "; ".join(rep.failures))
        return rep

    def evaluate(self, account: str, r: ReconcileResult) -> RecoveryReport:
        """실행 중 K8(D-064·엔진 단계 오류)의 복구 판정 (3차 검토 #1): 방금 끝난 대조 결과로 01 §2.11 조건을 다시 본다.

        대조를 다시 하지 않는다(호출자가 대조 직후에 부른다). 정지는 새로 걸지 않는다 — 실패 조건의 정지(K4·K5·K3·
        K6·K7)는 해당 경로(대조기·해소기·시계 감시·실행기)가 이미 건다. 같은 정지를 여기서 다시 발동하면 재개 대기 중인
        정지의 조건이 지워진다. K8은 풀지 않는다 — `recovery_ok`만 보고하고 해제는 사용자 재개.
        """
        rep = RecoveryReport(ok=True)

        def fail(why: str) -> None:
            rep.ok = False
            rep.failures.append(why)

        acct = self._repo.account(account)
        if acct is None or acct.mode is not self._repo.mode or acct.broker_env not in _ALLOWED_ENV[self._repo.mode]:
            fail("1. 모드·브로커 환경 불일치 (R-01)")
        if not self._repo.integrity_ok():
            fail("2. 저장 상태 무결성 실패 (R-25)")
        if self._clock_ok() is not True:
            fail("3. 시계 오차 정상 아님 (R-24)")
        if r.result == "QUERY_FAILED":
            fail("4. 브로커 미체결·체결·잔고 조회 실패")
        elif r.result != "MATCH":
            fail(f"5. 대조 불일치 {len(r.items)}건")
        unknown = [i for i in self._repo.intents_in_state(IntentState.UNKNOWN) if i.account == account]
        if unknown:
            fail(f"5. 결과 불명 의도 {len(unknown)}건 미해소")
        unsettled = [i for st in (IntentState.SENT, IntentState.PENDING_SEND)
                     for i in self._repo.intents_in_state(st) if i.account == account]
        if unsettled:
            fail(f"5. 전송 대기·응답 대기 의도 {len(unsettled)}건")
        self._halts.report_condition(HaltType.K8, None, "recovery_ok", rep.ok,
                                     f"실행 중 복구 판정, 대조 {r.recon_id}" if rep.ok else "; ".join(rep.failures))
        return rep
