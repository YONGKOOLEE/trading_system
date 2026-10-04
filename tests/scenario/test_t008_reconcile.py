"""T-008 대조 (05-plan T-008 + CP-018 추가, 04 V-27·V-60·V-65·V-66, 02 ST-07·D-034·D-035, INV-16).

모든 값은 가상이다. fake 브로커의 체결 누적이 "브로커 잔고"다.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, Env, make_signal
from tests.support.invariants import check_invariants
from trading_system.broker.base import BrokerQueryError
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import Check, GateResult, HaltType, IntentState, Mode, OrderType, RequestSource, RequestType, Side
from trading_system.domain.halts import HaltState
from trading_system.domain.models import MANUAL_OWNER, FillRecord, GateRequest, OrderEvent
from trading_system.halt.manager import Channel
from trading_system.ops.commands import OpsProcessor
from trading_system.ops.reconcile import ReconcileResolver, ResolveError
from trading_system.reconciliation.reconciler import ItemKind, Reconciler, RecoveryChecker
from trading_system.storage.alerts import AuditAlertSink
from trading_system.storage.repository import Repository


def rec(env: Env) -> Reconciler:
    return Reconciler(env.repo, env.broker, env.clock, env.halts, AuditAlertSink(env.repo, env.clock))


def resolver(env: Env) -> ReconcileResolver:
    return ReconcileResolver(env.repo, env.executor, env.clock)


def enter(env: Env, inst: str = "TST-A", qty: str = "10", ev: str = "e1"):  # type: ignore[no-untyped-def]
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, instrument=inst, quantity=qty,
                                                            evaluation=ev)))
    assert d.approved is not None, d.reason
    env.executor.submit(d.approved)
    env.executor.process_events()
    return d


def kinds(env: Env) -> list[str]:
    return sorted(i["kind"] for i in env.repo.open_recon_items(ACCOUNT))


def item(env: Env, kind: str) -> dict:
    return next(i for i in env.repo.open_recon_items(ACCOUNT) if i["kind"] == kind)


def ops(env: Env) -> OpsProcessor:
    return OpsProcessor(env.repo, env.gate, env.executor, env.halts, env.clock, reconciler=rec(env),
                        resolver=resolver(env))


def cmd(env: Env, cid: str, name: str, args: dict, channel: str = Channel.LOCAL) -> None:
    with env.repo.tx():
        env.repo.insert_command(cid, channel, "user", name, args, env.clock.now())


# --- 일치 ------------------------------------------------------------------------------------

def test_match_reports_reconciled_and_raises_no_halt(env: Env) -> None:
    enter(env)
    env.halts.activate(HaltType.K5, "monitor", "단절", ACCOUNT)
    env.halts.report_condition(HaltType.K5, ACCOUNT, "broker_connected", True)
    r = rec(env).run(ACCOUNT, "PERIODIC")
    assert r.result == "MATCH" and r.items == ()
    assert env.repo.active_halts(ACCOUNT) == []  # K5는 연결 + 대조 일치로 자동 해제


# --- V-65 외부 수동 거래 -------------------------------------------------------------------------

def test_v65_external_trade_k4_then_manual_ownership(env: Env) -> None:
    enter(env, "TST-A")
    no = env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("30"))  # 증권사 앱에서 직접 매수
    env.broker.fill(no)
    env.broker.drop_events()
    r = rec(env).run(ACCOUNT, "PERIODIC")
    assert r.result == "MISMATCH"
    assert kinds(env) == [ItemKind.QTY_MISMATCH, ItemKind.UNKNOWN_BROKER_ORDER]
    assert "K4" in env.repo.active_halts(ACCOUNT)
    assert env.repo.position(ACCOUNT, "TST-C").quantity == 0  # 자동 반영 없음 (D-035)
    cmd(env, "c1", "RECONCILE_RESOLVE", {"item_id": item(env, ItemKind.QTY_MISMATCH)["item_id"], "method": "EXTERNAL"})
    cmd(env, "c2", "RECONCILE_RESOLVE", {"item_id": item(env, ItemKind.UNKNOWN_BROKER_ORDER)["item_id"],
                                         "method": "EXTERNAL"})
    assert [o.status for o in ops(env).process_pending()] == ["DONE", "DONE"]
    assert env.repo.position(ACCOUNT, "TST-C").quantity == Decimal("30")
    own = env.repo.active_ownership(ACCOUNT, "TST-C")
    assert own is not None and own.owner == MANUAL_OWNER and own.state == "CONFIRMED"
    assert not env.halts.resume(HaltType.K4, ACCOUNT, Channel.LOCAL, "u").ok  # 재대조 전
    assert rec(env).run(ACCOUNT, "MANUAL").result == "MATCH"
    # 수동 보유 종목: 전략 신호 거부, 운영 요청 청산은 허용 (D-034, INV-16)
    assert not env.halts.resume(HaltType.K4, ACCOUNT, Channel.REMOTE, "u").ok
    assert env.halts.resume(HaltType.K4, ACCOUNT, Channel.LOCAL, "u").ok
    sig = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, instrument="TST-C", evaluation="x")))
    assert sig.result is GateResult.REJECTED and (Check.G6, False) in sig.checks
    op = env.gate.decide(GateRequest(source=RequestSource.OPERATOR, request_type=RequestType.EXIT, account=ACCOUNT,
                                     instrument="TST-C", side=Side.SELL, quantity=Decimal("30"),
                                     order_type=OrderType.MARKET, price=Decimal("10000"), idem_key="op-c"))
    assert op.result is GateResult.APPROVED


def test_external_buy_on_strategy_owned_symbol_cannot_be_absorbed(env: Env) -> None:
    enter(env, "TST-A")
    no = env.broker.add_external_order(ACCOUNT, "TST-A", "BUY", Decimal("5"))
    env.broker.fill(no)
    env.broker.drop_events()
    rec(env).run(ACCOUNT, "PERIODIC")
    with pytest.raises(ResolveError, match="전략 소유"):
        resolver(env).resolve(item(env, ItemKind.QTY_MISMATCH)["item_id"], "EXTERNAL", "u")


# --- V-66 누락 체결 ---------------------------------------------------------------------------

def test_v66_missing_fill_recorded_not_applied_until_resolved(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = enter(env, "TST-A", "10")
    order = env.repo.broker_order(env.repo.intent(d.approved.intent_id).broker_order_key)
    env.broker.fill(order.order_no)
    env.broker.drop_events()  # 실시간 체결 통보 유실
    rec(env).run(ACCOUNT, "PERIODIC")
    assert kinds(env) == [ItemKind.MISSING_FILL, ItemKind.QTY_MISMATCH, ItemKind.STATE_MISMATCH]
    assert env.repo.position(ACCOUNT, "TST-A").quantity == 0
    resolver(env).resolve(item(env, ItemKind.MISSING_FILL)["item_id"], "APPLY_FILL", "u")
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("10")
    assert env.repo.intent(d.approved.intent_id).state is IntentState.FILLED
    for k in (ItemKind.QTY_MISMATCH, ItemKind.STATE_MISMATCH):
        resolver(env).resolve(item(env, k)["item_id"], "BROKER_ACTION", "u")
    assert rec(env).run(ACCOUNT, "MANUAL").result == "MATCH"


def test_fill_without_fill_number_is_not_used(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = enter(env, "TST-A", "10")
    order = env.repo.broker_order(env.repo.intent(d.approved.intent_id).broker_order_key)
    env.broker.query_fills = lambda account: [FillRecord(order.order_no, "", Decimal("10"), Decimal("10000"),  # type: ignore[method-assign]
                                                         env.clock.now())]
    rec(env).run(ACCOUNT, "PERIODIC")
    assert ItemKind.MISSING_FILL not in kinds(env)  # 체결번호 없는 조회 체결은 반영 후보가 아님 (D-094)


# --- 주문 상태 차이 --------------------------------------------------------------------------

def test_state_mismatch_close_order(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = enter(env, "TST-A", "10")
    order = env.repo.broker_order(env.repo.intent(d.approved.intent_id).broker_order_key)
    env.broker.expire_open()
    env.broker.drop_events()
    rec(env).run(ACCOUNT, "CLOSE")
    it = item(env, ItemKind.STATE_MISMATCH)
    assert it["broker_value"] == "EXPIRED"
    resolver(env).resolve(it["item_id"], "CLOSE_ORDER", "u")
    assert env.repo.intent(d.approved.intent_id).state is IntentState.EXPIRED
    assert env.repo.active_reservations(ACCOUNT) == []
    assert order.order_no


def test_internal_only_and_stale_orders(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d1 = enter(env, "TST-A", "10", ev="a")
    o1 = env.repo.broker_order(env.repo.intent(d1.approved.intent_id).broker_order_key)
    env.broker._book.pop(o1.order_no)  # 브로커 조회에 없는 주문 (시험용 조작)
    rec(env).run(ACCOUNT, "PERIODIC")
    assert ItemKind.INTERNAL_ONLY_ORDER in kinds(env)
    resolver(env).resolve(item(env, ItemKind.INTERNAL_ONLY_ORDER)["item_id"], "CLOSE_ORDER", "u")
    assert env.repo.intent(d1.approved.intent_id).state is IntentState.EXPIRED
    assert rec(env).run(ACCOUNT, "MANUAL").result == "MATCH"
    assert env.halts.resume(HaltType.K4, ACCOUNT, Channel.LOCAL, "u").ok
    d2 = enter(env, "TST-B", "10", ev="b")
    env.clock.advance(timedelta(days=1))
    env.broker.new_day()
    rec(env).run(ACCOUNT, "STARTUP")
    it = item(env, ItemKind.STALE_ORDER)
    resolver(env).resolve(it["item_id"], "CLOSE_ORDER", "u")
    assert env.repo.intent(d2.approved.intent_id).state is IntentState.EXPIRED


def test_wrong_method_and_remote_resolve_refused(env: Env) -> None:
    enter(env)
    no = env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("3"))
    env.broker.fill(no)
    env.broker.drop_events()
    rec(env).run(ACCOUNT, "PERIODIC")
    it = item(env, ItemKind.QTY_MISMATCH)
    with pytest.raises(ResolveError):
        resolver(env).resolve(it["item_id"], "APPLY_FILL", "u")
    cmd(env, "r1", "RECONCILE_RESOLVE", {"item_id": it["item_id"], "method": "EXTERNAL"}, Channel.REMOTE)
    [o] = ops(env).process_pending()
    assert o.status == "REJECTED" and "로컬" in o.result


# --- 조회 실패, 비정상 통보 → K4 -------------------------------------------------------------

def test_query_failure_is_not_a_mismatch(env: Env) -> None:
    env.broker.query_failures = 1
    r = rec(env).run(ACCOUNT, "PERIODIC")
    assert r.result == "QUERY_FAILED" and env.repo.active_halts(ACCOUNT) == []


@pytest.mark.parametrize("bad", [
    OrderEvent(kind="FILL", order_no="0000001", fill_id="Z1", quantity=Decimal("0"), price=Decimal("1")),
    OrderEvent(kind="FILL", order_no="0000001", fill_id="Z2", quantity=Decimal("999"), price=Decimal("1")),
    OrderEvent(kind="WEIRD", order_no="0000001"),
])
def test_anomalous_events_raise_k4(env: Env, bad: OrderEvent) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    enter(env, "TST-A", "10")
    env.broker.inject(bad)
    env.executor.process_events()
    assert "K4" in env.repo.active_halts(ACCOUNT)


def test_late_fill_raises_k4(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = enter(env, "TST-A", "10")
    order = env.repo.broker_order(env.repo.intent(d.approved.intent_id).broker_order_key)
    c = env.gate.decide(GateRequest.cancel(RequestSource.OPERATOR, ACCOUNT, "TST-A", Side.BUY, order.order_key,
                                           idem_key="c", owner=None))
    env.executor.submit(c.approved)
    env.executor.process_events()
    env.broker.inject(OrderEvent(kind="FILL", order_no=order.order_no, fill_id="LATE", quantity=Decimal("4"),
                                 price=Decimal("10000")))
    env.executor.process_events()
    assert "K4" in env.repo.active_halts(ACCOUNT)


# --- V-60·V-27 재시작 복구 ---------------------------------------------------------------------

def _checker(env: Env, clock_ok=lambda: True) -> RecoveryChecker:  # type: ignore[no-untyped-def]
    return RecoveryChecker(env.repo, rec(env), env.halts, clock_ok)


@pytest.mark.parametrize(("setup", "halt"), [
    ("integrity", "K7"), ("clock", "K6"), ("query", "K5"), ("mismatch", "K4"), ("unknown", "K3"),
])
def test_v60_each_recovery_condition_failure_switches_halt(env: Env, setup: str, halt: str) -> None:
    enter(env)
    k8 = env.halts.on_start()
    clock_ok = (lambda: False) if setup == "clock" else (lambda: True)
    if setup == "integrity":
        env.repo.integrity_ok = lambda: False  # type: ignore[method-assign]
    if setup == "query":
        env.broker.query_failures = 3
    if setup == "mismatch":
        no = env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("1"))
        env.broker.fill(no)
        env.broker.drop_events()
    if setup == "unknown":
        env.broker.behavior = FakeBehavior.UNKNOWN
        with env.repo.tx():
            env.repo.update_halt(k8, state=HaltState.RELEASED.value)  # 시험: 결과 불명 주문을 먼저 만들기 위해
        enter(env, "TST-B", ev="u")
        k8 = env.halts.on_start()
    rep = _checker(env, clock_ok).check(ACCOUNT)
    assert not rep.ok
    assert halt in env.repo.active_halts(ACCOUNT)
    assert env.repo.halt(k8)["state"] == HaltState.ACTIVE.value  # K8은 풀리지 않음


def test_v27_recovery_ok_then_no_orders_until_user_resume(env: Env) -> None:
    enter(env)
    k8 = env.halts.on_start()
    rep = _checker(env).check(ACCOUNT)
    assert rep.ok, rep.failures
    assert env.repo.halt(k8)["state"] == HaltState.AWAITING_RESUME.value
    sent = len(env.broker.sent)
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, instrument="TST-B", evaluation="z")))
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G2
    assert len(env.broker.sent) == sent
    assert env.halts.resume(HaltType.K8, None, Channel.LOCAL, "u").ok


# --- INV-16 검사기 ------------------------------------------------------------------------------

def test_inv16_checker_detects_signal_on_manual_owned_symbol(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "m.db", Mode.BACKTEST)
    c = repo.connection
    c.execute("PRAGMA foreign_keys=OFF")
    c.execute("INSERT INTO ownership(ownership_id, mode, account, instrument, owner, state, created_at) "
              "VALUES('o','BACKTEST','A','X','manual','CONFIRMED','2026-10-01T00:00:00.000000Z')")
    c.execute("""INSERT INTO gate_decision(decision_id, mode, created_at, seq, source, request_type, account, instrument,
                 side, quantity, price, idem_key, result, checks) VALUES('d','BACKTEST','2026-10-01T01:00:00.000000Z',
                 1,'SIGNAL','REDUCE','A','X','SELL','1','1','k','APPROVED','[]')""")
    c.execute("""INSERT INTO intent(intent_id, mode, decision_id, idem_key, source, request_type, account, instrument,
                 side, quantity, order_type, price, state, filled_quantity, created_at, created_seq)
                 VALUES('i','BACKTEST','d','k','SIGNAL','REDUCE','A','X','SELL','1','MARKET','1','REJECTED','0',
                 '2026-10-01T01:00:00.000000Z',1)""")
    assert any(v.startswith("INV-16") for v in check_invariants(c, "BACKTEST"))
    repo.close()


def test_broker_query_error_type_is_exposed() -> None:
    assert issubclass(BrokerQueryError, RuntimeError)


def test_unknown_broker_order_cannot_be_called_external_while_our_order_is_unknown(env: Env) -> None:
    env.broker.behavior = FakeBehavior.LOST_ACCEPT  # 우리 주문이 접수됐지만 응답 유실
    enter(env, "TST-A", "10")
    # 3차 검토 F-3 이후: 결과 불명 의도와 같은 조건의 주문은 대조가 K4 항목으로 잡지 않고 해소기에 맡긴다.
    # 보호 의도(결과 불명 중 같은 종목·방향 주문을 EXTERNAL로 처리 금지)를 계속 시험하려고 수량이 다른 외부 주문을 둔다
    env.broker.add_external_order(ACCOUNT, "TST-A", "BUY", Decimal("3"))
    rec(env).run(ACCOUNT, "PERIODIC")
    it = item(env, ItemKind.UNKNOWN_BROKER_ORDER)
    with pytest.raises(ResolveError, match="결과 불명"):
        resolver(env).resolve(it["item_id"], "EXTERNAL", "u")
