"""대조와 재시작 복구 판정 (R-08, R-18, 01 §2.11, 02 ST-07·D-034·D-035, 03 §6.3·D-094).

- 내부 원장(체결 누적)을 브로커 조회에 맞춰 **자동으로 고치지 않는다**. 차이는 불일치 항목으로 기록하고 K4 (D-035).
- 비교: 보유 수량, 브로커에만 있는 주문(외부 거래·미확인), 내부에만 열린 주문, 상태 차이(브로커 종결·내부 열림),
  지난 거래일에 열린 채 남은 주문, 누락 체결(체결번호가 있는 조회 체결만, D-094).
- 조회 실패는 불일치가 아니다(결과를 모름). 기록만 하고 판단은 호출자(K5 정책)에 맡긴다.
- 일치하면 K3·K4·K5의 재개 조건 `reconciled`를 충족으로 보고한다.
- 재시작 복구(01 §2.11): 모드·환경, 저장 무결성, 시계, 브로커 조회, 대조 일치·결과 불명 없음을 모두 확인한다.
  하나라도 실패하면 해당 정지(K7·K6·K5·K4·K3)를 발동하고, 모두 충족하면 K8 `recovery_ok`를 보고한다.
  K8 해제는 사용자 재개로만 한다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from trading_system.broker.base import BrokerAdapter, BrokerQueryError
from trading_system.broker.ratelimit import Priority, call_priority
from trading_system.domain.clock import Clock
from trading_system.domain.enums import HaltType, IntentState, Mode
from trading_system.domain.sinks import AlertSink, HaltControl
from trading_system.storage.jsonlog import Masker
from trading_system.storage.repository import Repository

OPEN = ("ACCEPTED", "PARTIAL")
K5_LINK_REST = "link:rest"   # halt/manager.py K5_LINK_REST와 같은 값 (K5 조회 경로, RV-02)
_ALLOWED_ENV = {Mode.BACKTEST: {"fake"}, Mode.PAPER: {"mock"}, Mode.LIVE: {"real"}}


class ItemKind:
    QTY_MISMATCH = "QTY_MISMATCH"                  # 수량 차이
    UNKNOWN_BROKER_ORDER = "UNKNOWN_BROKER_ORDER"  # 미확인 브로커 주문 (외부 거래 의심)
    MISSING_FILL = "MISSING_FILL"                  # 누락 체결
    INTERNAL_ONLY_ORDER = "INTERNAL_ONLY_ORDER"    # 내부에만 있는 주문
    STATE_MISMATCH = "STATE_MISMATCH"              # 브로커는 종결, 내부는 열림
    STALE_ORDER = "STALE_ORDER"                    # 지난 거래일 주문이 내부에서 열린 채 남음


@dataclass(frozen=True)
class ReconcileResult:
    recon_id: str
    result: str                 # MATCH / MISMATCH / QUERY_FAILED
    items: tuple[str, ...] = ()  # 불일치 항목 ID


@dataclass
class RecoveryReport:
    ok: bool
    failures: list[str] = field(default_factory=list)
    halts: list[str] = field(default_factory=list)


QUERY_FAILURES_TO_K5 = 3   # 연속 대조 조회 실패 → K5 (03 타임아웃 표 "최대 3회 → K5", e2e 결함 후보 2)


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
        # 재조회 전 대기: 01 "재조회 1회 (체결 반영 지연 확인)" — 실시간 체결 통보가 조회보다 늦게 오는 지연을 기다린다.
        # 실운영은 main이 짧은 실제 대기를 넘긴다(판정·실행 스레드만 멈춤, 감시 스레드는 계속). 시험 기본은 대기 없음
        self._requery_wait = requery_wait

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
        found, broker_pos = self._compare(account, positions, orders, fills, now)
        requeried = False
        if found:
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
                found, broker_pos = self._compare(account, positions, orders, fills, now)

        with self._repo.tx():
            # 같은 내용의 열린 항목은 다시 만들지 않는다. 이번에 안 보여도 열린 항목은 사람이 해소하기 전까지 남는다
            # (예: 외부 주문이 다음 날 조회에서 사라짐) — 열린 항목이 있으면 일치로 보지 않는다
            open_before = {(i["kind"], i["instrument"], i["ref"], i["internal_value"], i["broker_value"]): i["item_id"]
                           for i in self._repo.open_recon_items(account)}
            result = "MISMATCH" if found or open_before else "MATCH"
            rid = self._repo.insert_reconciliation(account, trigger, result, {
                "positions": {k: str(v) for k, v in broker_pos.items()}, "orders": len(orders), "fills": len(fills),
                "items": len(found), "open_items_before": len(open_before), "requeried": requeried}, now)
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
            self._alerts.alert("EMERGENCY", f"대조 불일치 {n}건 — 사람 확인 필요 (reconcile-resolve)", rid)
        elif self._halts is not None:
            for k in (HaltType.K3, HaltType.K4, HaltType.K5):
                self._halts.report_condition(k, account, "reconciled", True, rid)
        return ReconcileResult(rid, result, ids)

    def _drain(self) -> None:
        if self._drain_events is not None:
            self._drain_events()

    def _query(self, account: str) -> tuple[list, list, list]:  # type: ignore[type-arg]
        with call_priority(Priority.RECONCILE):  # 03 D-048
            return (self._broker.query_positions(account), self._broker.query_orders(account),
                    self._broker.query_fills(account))

    def _compare(self, account: str, positions: list, orders: list, fills: list,  # type: ignore[type-arg]
                 now: datetime) -> tuple[list[tuple[str, str | None, str | None, str | None, str | None, dict]],
                                         dict[str, Decimal]]:
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
        for o in orders:
            if o.order_no not in known and o.kind != "CANCEL":
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
            if b is None:
                found.append((ItemKind.INTERNAL_ONLY_ORDER, io.instrument, io.order_no, io.state, None,
                              {"order_key": io.order_key}))
            elif b.state not in OPEN:
                found.append((ItemKind.STATE_MISMATCH, io.instrument, io.order_no, io.state, b.state,
                              {"order_key": io.order_key, "broker_state": b.state}))
        for f in fills:
            if not f.fill_id:
                continue  # 체결번호 없는 조회 체결은 반영 후보로 쓰지 않는다 (D-094)
            order = self._repo.broker_order_by_no(f.order_no, today, account)
            # 외부 주문 체결은 수량 대조로
            if order is not None and order.intent_id is not None and                     not self._repo.fill_exists(order.order_key, f.fill_id):
                found.append((ItemKind.MISSING_FILL, order.instrument, f.order_no, None, f"{f.quantity}@{f.price}",
                              {"order_key": order.order_key, "fill_id": f.fill_id, "quantity": str(f.quantity),
                               "price": str(f.price), "at": f.at.isoformat()}))
        return found, broker_pos


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
