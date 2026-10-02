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
from decimal import Decimal

from trading_system.broker.base import BrokerAdapter, BrokerQueryError
from trading_system.domain.clock import Clock
from trading_system.domain.enums import HaltType, IntentState, Mode
from trading_system.domain.sinks import AlertSink, HaltControl
from trading_system.storage.repository import Repository

OPEN = ("ACCEPTED", "PARTIAL")
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


class Reconciler:
    def __init__(self, repo: Repository, broker: BrokerAdapter, clock: Clock, halts: HaltControl | None,
                 alerts: AlertSink) -> None:
        self._repo = repo
        self._broker = broker
        self._clock = clock
        self._halts = halts
        self._alerts = alerts

    def run(self, account: str, trigger: str) -> ReconcileResult:
        if self._repo.in_transaction:
            raise RuntimeError("대조 조회는 저장 단위(tx) 밖에서만 한다")
        try:
            positions = self._broker.query_positions(account)
            orders = self._broker.query_orders(account)
            fills = self._broker.query_fills(account)
        except BrokerQueryError as exc:
            with self._repo.tx():
                rid = self._repo.insert_reconciliation(account, trigger, "QUERY_FAILED",
                                                       {"error": str(exc)}, self._clock.now())
            self._alerts.alert("WARNING", f"대조 조회 실패 ({trigger}): {exc}", rid)
            return ReconcileResult(rid, "QUERY_FAILED")

        now = self._clock.now()
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
                              f"{o.side} {o.quantity} {o.state}", {"side": o.side, "quantity": str(o.quantity)}))
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
            if order is not None and order.intent_id is not None and                     not self._repo.fill_exists(order.order_key, f.fill_id):  # 외부 주문 체결은 수량 대조로
                found.append((ItemKind.MISSING_FILL, order.instrument, f.order_no, None, f"{f.quantity}@{f.price}",
                              {"order_key": order.order_key, "fill_id": f.fill_id, "quantity": str(f.quantity),
                               "price": str(f.price), "at": f.at.isoformat()}))

        result = "MISMATCH" if found else "MATCH"
        with self._repo.tx():
            rid = self._repo.insert_reconciliation(account, trigger, result, {
                "positions": {k: str(v) for k, v in broker_pos.items()}, "orders": len(orders), "fills": len(fills),
                "items": len(found)}, now)
            ids = tuple(self._repo.insert_recon_item(rid, account, kind, inst, ref, iv, bv, json.dumps(detail))
                        for kind, inst, ref, iv, bv, detail in found)
            self._repo.audit(now, "RECONCILE", "reconciler", rid, {"trigger": trigger, "result": result,
                                                                 "items": len(found)})
        if found:
            if self._halts is not None:
                self._halts.activate(HaltType.K4, "reconciler", f"대조 불일치 {len(found)}건 ({trigger})", account)
                self._halts.report_condition(HaltType.K4, account, "reconciled", False, rid)
            self._alerts.alert("EMERGENCY", f"대조 불일치 {len(found)}건 — 사람 확인 필요 (reconcile-resolve)", rid)
        elif self._halts is not None:
            for k in (HaltType.K3, HaltType.K4, HaltType.K5):
                self._halts.report_condition(k, account, "reconciled", True, rid)
        return ReconcileResult(rid, result, ids)


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
        if not self._repo.integrity_ok():
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
        if rep.ok:
            self._halts.report_condition(HaltType.K8, None, "recovery_ok", True, f"대조 {r.recon_id}")
        else:
            self._halts.report_condition(HaltType.K8, None, "recovery_ok", False, "; ".join(rep.failures))
        return rep
