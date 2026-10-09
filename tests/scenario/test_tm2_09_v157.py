"""T-M2-09 공통 V의 M2 재실행 (04 2-M2.4 V-157, CP-024 §4, 05 T-M2-09).

- M2 설정(tests/support/m2_fakes: 가상 크립토 심볼·소수 수량·UTC 시간·USDT·거래 규칙·FakeSpotBroker·식별 SYMBOL)으로 공통 V를
  다시 실행한다. 이미 M2 시험이 있는 V는 COVERAGE에 그 시험을 적고, 없던 V는 이 파일에 새로 쓴다.
- 이 작업 범위에서 실행할 수 없는 V는 NOT_RUN에 사유와 함께 적는다(미실행 — 통과로 세지 않는다. 04 D-068: 하나라도
  미실행이면 M2 전환 불가). 시험 test_v157_list_is_fully_accounted가 V-157 목록 전체가 둘 중 하나에 있고, 적은 시험이
  실제로 있는지 확인한다.
- 기대값 확인 필요(04 §6, 사용자 확인 전 [제안]): V-41(M2 = 5회 없음 → K3, CP-031로 M1도 같음), V-48(M2 정정 미지원 →
  정정 요청 G10 거부), V-125·V-126(M2 보류 기준 = 전송 후 24시간, 고객 주문 ID로 우리 주문과 외부 주문을 구분).
모든 값은 가상이다. 네트워크 없음. 매 시험 끝에 불변 조건을 검사한다 (04 D-069).
"""

from __future__ import annotations

import importlib
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.invariants import check_invariants
from tests.support.m2_fakes import (
    ACCOUNT, PAIRS, QUOTE_ASSET, RULES, T0, M2Env, asset_pair, build_m2_env, ioc_buy_price, m2_limits,
)
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import (
    Check, GateResult, HaltType, IntentState, Mode, OrderType, RequestSource, RequestType, Side,
)
from trading_system.domain.market import DailyRisk
from trading_system.domain.models import GateRequest, OrderRequest, Signal, client_order_id_of
from trading_system.execution.executor import ExecutionBlocked, Executor
from trading_system.execution.resolver import ResolveOutcome
from trading_system.execution.spot_resolver import SpotUnknownResolver
from trading_system.halt.manager import Channel, HaltManager
from trading_system.reconciliation.reconciler import RecoveryChecker
from trading_system.reconciliation.spot import PERIODIC, SpotItemKind, SpotReconciler
from trading_system.storage.alerts import AuditAlertSink
from trading_system.storage.maintenance import backup, restore_test
from trading_system.storage.repository import StorageFailure

D = Decimal
S1, S2 = "tm209-a", "tm209-b"   # 테스트 전용 고정 전략 (실제 매매전략 아님)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[M2Env]:
    e = build_m2_env(tmp_path)
    yield e
    violations = check_invariants(e.repo.connection, Mode.BACKTEST.value)
    e.repo.close()
    assert violations == [], violations


def sig(kind: RequestType, inst: str = "TSTA-USDT", qty: str = "0.1000", ev: str = "e1", strategy: str = S1,
        stop: str | None = None, env: M2Env | None = None) -> Signal:
    r = RULES[inst]
    buying = kind in (RequestType.ENTRY, RequestType.INCREASE)
    now = env.clock.now() if env is not None else T0
    return Signal(strategy_id=strategy, strategy_version=1, evaluation_id=ev, account=ACCOUNT, instrument=inst,
                  rule_id="R1", kind=kind, quantity=D(qty), order_type=OrderType.LIMIT if buying else OrderType.MARKET,
                  price=ioc_buy_price(r.last, r.tick_size) if buying else r.last, as_of=now - timedelta(minutes=1),
                  valid_until=now + timedelta(minutes=30), stop_price=None if stop is None else D(stop))


def op_sell(kind: RequestType, qty: str, key: str, inst: str = "TSTA-USDT") -> GateRequest:
    return GateRequest(source=RequestSource.OPERATOR, request_type=kind, account=ACCOUNT, instrument=inst,
                       side=Side.SELL, quantity=D(qty), order_type=OrderType.MARKET, price=RULES[inst].last,
                       idem_key=key)


def run(env: M2Env, req: GateRequest | Signal):  # type: ignore[no-untyped-def]
    r = GateRequest.from_signal(req) if isinstance(req, Signal) else req
    d = env.gate.decide(r)
    if d.approved is not None:
        env.executor.submit(d.approved)
        env.executor.process_events()
    return d


def halts(env: M2Env) -> list[str]:
    return env.repo.active_halts(ACCOUNT)


def spot_reconciler(env: M2Env, expected: Decimal = D("10000")) -> SpotReconciler:
    return SpotReconciler(env.repo, env.broker, env.clock, env.halts, AuditAlertSink(env.repo, env.clock),
                          universe=PAIRS, quote_asset=QUOTE_ASSET, expected_quote=lambda a: expected,
                          drain_events=env.executor.process_events)


def resolver(env: M2Env) -> SpotUnknownResolver:
    return SpotUnknownResolver(env.repo, env.broker, env.executor, env.clock,
                               lambda s: env.clock.advance(timedelta(seconds=s)))


def alerts(env: M2Env) -> list[str]:
    import json
    return [json.loads(e["summary"])["message"] for e in env.repo.audit_events() if e["kind"] == "ALERT"]


# --- 한도·게이트 -----------------------------------------------------------------------------------------------

def test_v01_l5_l6_l7_boundaries(env: M2Env) -> None:
    env.gate._in.limits.by_account[ACCOUNT] = m2_limits(l5_daily_new_orders=2, l6_max_positions=2,   # noqa: SLF001
                                                       l7_max_order_amount=D("1005.00"))
    assert run(env, sig(RequestType.ENTRY, qty="0.1000")).result is GateResult.APPROVED          # 1005.00 = L7 경계
    over = run(env, sig(RequestType.ENTRY, "TSTB-USDT", "400.1", "e2"))                           # 1005.25 > L7
    assert over.failed_check is Check.G7 and "L7" in (over.reason or "")
    assert run(env, sig(RequestType.ENTRY, "TSTB-USDT", "100.0", "e3")).result is GateResult.APPROVED  # L5 2건째
    l5 = run(env, sig(RequestType.ENTRY, "TSTC-USDT", "10.0", "e4"))
    assert l5.failed_check is Check.G7 and "L5" in (l5.reason or "")


def test_v05_unknown_and_open_buys_count_as_filled(env: M2Env) -> None:
    env.broker.behavior = FakeBehavior.UNKNOWN
    assert run(env, sig(RequestType.ENTRY, qty="0.1990")).result is GateResult.APPROVED   # 결과 불명 1,999.95 예약 유지
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    assert run(env, sig(RequestType.ENTRY, "TSTB-USDT", "700.0", "e2")).result is GateResult.APPROVED  # 미체결 1,758.75
    over = run(env, sig(RequestType.ENTRY, "TSTC-USDT", "500.0", "e3"))                   # 합 5,014.95 > L3 5,000
    assert over.failed_check is Check.G7 and "L3" in (over.reason or "")


def test_v06_reduction_ignores_limits(env: M2Env) -> None:
    run(env, sig(RequestType.ENTRY))
    env.gate._in.limits.by_account[ACCOUNT] = m2_limits(l2_max_symbol_ratio=D("0.01"), l3_max_total_ratio=D("0.01"))  # noqa: SLF001
    assert run(env, sig(RequestType.EXIT, qty="0.0999", ev="x")).result is GateResult.APPROVED


def test_v13_operator_cannot_enter(env: M2Env) -> None:
    req = GateRequest(source=RequestSource.OPERATOR, request_type=RequestType.ENTRY, account=ACCOUNT,
                      instrument="TSTA-USDT", side=Side.BUY, quantity=D("0.1000"), order_type=OrderType.LIMIT,
                      price=D("10050.00"), idem_key="op-buy", owner="op")
    d = run(env, req)
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G3 and env.broker.sent == []


def test_v121_stop_price_l1(env: M2Env) -> None:
    env.gate._in.limits.by_account[ACCOUNT] = m2_limits(l1_max_loss_ratio=D("0.01"))   # noqa: SLF001 — 100 USDT
    no_stop = run(env, sig(RequestType.ENTRY))                                          # 위험 = 주문 금액 1,005
    assert no_stop.failed_check is Check.G7 and "L1" in (no_stop.reason or "")
    ok = run(env, sig(RequestType.ENTRY, ev="e2", stop="9500.00"))                      # (10050 − 9500) × 0.1 = 55
    assert ok.result is GateResult.APPROVED
    bad = run(env, sig(RequestType.ENTRY, "TSTB-USDT", "10.0", "e3", stop="3.0000"))    # 손절가 ≥ 진입가
    assert bad.failed_check is Check.G3


# --- 정지 유형 (V-21~V-32, V-119) ---------------------------------------------------------------------------------

def test_v21_k2_allows_strategy_reduction_blocks_entry(env: M2Env) -> None:
    run(env, sig(RequestType.ENTRY))
    env.halts.check_daily_loss(ACCOUNT, DailyRisk(D("10000"), D("-300"), D("0"), D("0")), D("0.02"))
    assert "K2" in halts(env)
    assert run(env, sig(RequestType.ENTRY, "TSTB-USDT", "10.0", "e2")).failed_check is Check.G2
    assert run(env, sig(RequestType.EXIT, qty="0.0999", ev="x")).result is GateResult.APPROVED
    env.clock.advance(timedelta(days=1))
    assert "K2" in halts(env)   # 다음 기준 시각에도 자동 해제 없음


def test_v22_k3_blocks_account_risk_increase_local_resume_only(env: M2Env) -> None:
    env.broker.behavior = FakeBehavior.UNKNOWN
    iid = run(env, sig(RequestType.ENTRY)).approved.intent_id
    assert resolver(env).resolve(iid) is ResolveOutcome.AMBIGUOUS and "K3" in halts(env)
    env.broker.behavior = FakeBehavior.FILL
    assert run(env, sig(RequestType.ENTRY, "TSTB-USDT", "10.0", "e2")).failed_check is Check.G2   # 다른 종목도
    assert not env.halts.resume(HaltType.K3, ACCOUNT, Channel.REMOTE, "user").ok


def test_v24_v67_k5_link_down_then_auto_release_after_reconcile(env: M2Env) -> None:
    rec = spot_reconciler(env)
    env.broker.query_failures = 3
    for _ in range(3):
        rec.run(ACCOUNT, PERIODIC)
    assert "K5" in halts(env)
    assert run(env, sig(RequestType.ENTRY)).failed_check is Check.G2   # 위험 확대 거부
    assert rec.run(ACCOUNT, PERIODIC).result == "MATCH"
    assert "K5" not in halts(env)                                      # 대조 일치 → 자동 해제


def test_v25_k6_operator_reduction_only(env: M2Env) -> None:
    run(env, sig(RequestType.ENTRY))
    env.halts.activate(HaltType.K6, "clock", "시계 오차", ACCOUNT)
    assert run(env, sig(RequestType.EXIT, qty="0.0999", ev="x")).failed_check is Check.G2
    assert run(env, op_sell(RequestType.REDUCE, "0.0500", "op1")).result is GateResult.APPROVED


def test_v27_k8_needs_user_resume_even_when_recovered(env: M2Env) -> None:
    env.halts.on_start()
    env.halts.report_condition(HaltType.K8, None, "recovery_ok", True, "test")
    assert run(env, sig(RequestType.ENTRY)).failed_check is Check.G2 and env.broker.sent == []
    assert env.halts.resume(HaltType.K8, None, Channel.LOCAL, "user").ok
    assert run(env, sig(RequestType.ENTRY, ev="e2")).result is GateResult.APPROVED


def test_v28_v84_k9_user_vs_replace(env: M2Env) -> None:
    run(env, sig(RequestType.ENTRY))
    env.halts.activate(HaltType.K9, "user", "중지", ACCOUNT, owner=f"{S1}:1", variant="USER")
    assert run(env, sig(RequestType.EXIT, qty="0.0999", ev="x1")).failed_check is Check.G2
    assert run(env, op_sell(RequestType.REDUCE, "0.0100", "op1")).result is GateResult.APPROVED
    env.halts.report_condition(HaltType.K9, ACCOUNT, "strategy_resumed", True, "시험: 전략 재개", owner=f"{S1}:1")
    assert env.halts.resume(HaltType.K9, ACCOUNT, Channel.LOCAL, "user", owner=f"{S1}:1").ok
    env.halts.activate(HaltType.K9, "user", "교체", ACCOUNT, owner=f"{S1}:1", variant="REPLACE")
    v2 = run(env, sig(RequestType.ENTRY, ev="v2", strategy=S2))        # v2는 v1 소유 종목 진입 거부
    assert v2.failed_check is Check.G6
    assert run(env, sig(RequestType.ENTRY, ev="e9")).failed_check is Check.G2   # 중지된 전략의 위험 확대
    assert run(env, sig(RequestType.EXIT, qty="0.0899", ev="x2")).result is GateResult.APPROVED   # 교체: 감액·청산 허용
    other = run(env, sig(RequestType.ENTRY, "TSTB-USDT", "10.0", "e3", strategy=S2))
    assert other.result is GateResult.APPROVED   # 다른 전략·다른 종목은 영향 없음


def test_v29_k10_instrument_scope(env: M2Env) -> None:
    env.halts.activate(HaltType.K10, "data", "품질", ACCOUNT, instrument="TSTA-USDT")
    assert run(env, sig(RequestType.ENTRY)).failed_check is Check.G2
    assert run(env, sig(RequestType.ENTRY, "TSTB-USDT", "10.0", "e2")).result is GateResult.APPROVED


def test_v30_k11_blocks_risk_increase_allows_reduction(env: M2Env) -> None:
    run(env, sig(RequestType.ENTRY))
    env.halts.activate(HaltType.K11, "notify", "알림 장애", ACCOUNT)
    assert run(env, sig(RequestType.ENTRY, "TSTB-USDT", "10.0", "e2")).failed_check is Check.G2
    assert run(env, sig(RequestType.EXIT, qty="0.0999", ev="x")).result is GateResult.APPROVED


def test_v31_k2_and_k4_most_strict(env: M2Env) -> None:
    run(env, sig(RequestType.ENTRY))
    env.halts.activate(HaltType.K2, "monitor", "손실", ACCOUNT)
    env.halts.activate(HaltType.K4, "reconciler", "불일치", ACCOUNT)
    assert run(env, sig(RequestType.EXIT, qty="0.0999", ev="x")).failed_check is Check.G2   # K4: 취소만


def test_v32_remote_resume_refused_for_k3_k4_k7_k8(env: M2Env) -> None:
    for k in (HaltType.K3, HaltType.K4, HaltType.K7, HaltType.K8):
        env.halts.activate(k, "test", "시험", ACCOUNT if k is not HaltType.K8 else None)
        assert not env.halts.resume(k, ACCOUNT if k is not HaltType.K8 else None, Channel.REMOTE, "user").ok


def test_v119_k1_remote_resume_kill_file_still_blocks_send(env: M2Env) -> None:
    env.halts.activate(HaltType.K1, "local", "kill", ACCOUNT)
    env.kill_file.write_text("KILL")
    assert env.halts.resume(HaltType.K1, ACCOUNT, Channel.REMOTE, "user").ok
    d = env.gate.decide(GateRequest.from_signal(sig(RequestType.ENTRY)))
    assert d.result is GateResult.APPROVED
    assert env.executor.submit(d.approved) is IntentState.NOT_SENT and env.broker.sent == []


# --- 결과 불명·주문 상태 (V-41·V-43·V-45·V-47·V-48·V-58·V-59·V-122·V-124) ----------------------------------------------

def test_v43_new_signal_during_unknown_rejected(env: M2Env) -> None:
    env.broker.behavior = FakeBehavior.UNKNOWN
    run(env, sig(RequestType.ENTRY))
    d = run(env, sig(RequestType.ENTRY, ev="e2"))
    assert d.result is GateResult.REJECTED and d.failed_check in (Check.G4, Check.G5) and len(env.broker.sent) == 1


def test_v45_reject_streak_alert(env: M2Env) -> None:
    env.broker.behavior, env.broker.reject_code = FakeBehavior.REJECT, -2010
    for i, inst in enumerate(("TSTA-USDT", "TSTB-USDT", "TSTC-USDT")):
        run(env, sig(RequestType.ENTRY, inst, "0.1000" if inst == "TSTA-USDT" else "10.0", f"e{i}"))
    assert any("연속 거부 3회" in m for m in alerts(env))


def test_v47_pre_send_failure_closes_not_sent(env: M2Env) -> None:
    env.broker.behavior = FakeBehavior.PRE_SEND_FAIL
    d = run(env, sig(RequestType.ENTRY))
    assert env.repo.intent(d.approved.intent_id).state is IntentState.NOT_SENT
    assert env.broker.sent == [] and env.repo.active_reservations(ACCOUNT) == []


def test_v48_m2_modify_rejected_cancel_confirmed(env: M2Env) -> None:
    """[확인 필요 04 §6] M2 기대값(제안): 정정은 미지원 → 게이트 G10 거부, 취소는 대상 주문 종결로 확인."""
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    run(env, sig(RequestType.ENTRY))
    [o] = env.repo.open_orders(ACCOUNT)
    mod = run(env, GateRequest(source=RequestSource.EXEC_POLICY, request_type=RequestType.MODIFY, account=ACCOUNT,
                               instrument=o.instrument, side=Side.BUY, quantity=o.quantity, order_type=OrderType.LIMIT,
                               price=D("10000.00"), idem_key="ep1", owner=f"{S1}:1", target_order_key=o.order_key))
    assert mod.failed_check is Check.G10
    cancel = run(env, GateRequest.cancel(RequestSource.OPERATOR, ACCOUNT, o.instrument, Side.BUY, o.order_key,
                                         idem_key="op-c", owner=None))
    assert cancel.result is GateResult.APPROVED
    assert env.repo.broker_order(o.order_key).state == "CANCELLED"


def test_v58_restart_with_awaiting_response_resolved_by_client_id(env: M2Env) -> None:
    d = env.gate.decide(GateRequest.from_signal(sig(RequestType.ENTRY)))
    intent = env.repo.intent(d.approved.intent_id)
    cid = client_order_id_of(intent.idem_key or "")
    with env.repo.tx():   # 전송 시도 기록 직후 프로세스 종료 (거래소는 받음)
        env.repo.insert_client_order(cid, intent.intent_id, ACCOUNT, intent.instrument, env.clock.now())
        env.repo.insert_attempt(intent.intent_id, env.clock.now())
        env.repo.set_intent_state(intent.intent_id, IntentState.SENT)
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    env.broker.place_order(OrderRequest("NEW", ACCOUNT, intent.instrument, "BUY", intent.quantity, OrderType.LIMIT,
                                        intent.price, client_order_id=cid))
    env.executor = Executor(env.repo, env.broker, env.clock, env.kill_file, asset_pair=asset_pair, client_ids=True)
    assert env.executor.recover_on_start() == [intent.intent_id]
    assert resolver(env).resolve(intent.intent_id) is ResolveOutcome.FOUND
    assert len(env.broker.sent) == 1


def test_v59_pending_send_closed_on_restart(env: M2Env) -> None:
    d = env.gate.decide(GateRequest.from_signal(sig(RequestType.ENTRY)))
    env.executor = Executor(env.repo, env.broker, env.clock, env.kill_file, asset_pair=asset_pair, client_ids=True)
    env.executor.recover_on_start()
    assert env.repo.intent(d.approved.intent_id).state is IntentState.NOT_SENT and env.broker.sent == []


def test_v56_same_evaluation_after_restart_no_new_intent(env: M2Env) -> None:
    run(env, sig(RequestType.ENTRY))
    env.executor = Executor(env.repo, env.broker, env.clock, env.kill_file, asset_pair=asset_pair, client_ids=True)
    again = run(env, sig(RequestType.ENTRY))
    assert again.failed_check is Check.G4 and len(env.repo.intents()) == 1


def test_v122_recheck_before_send(env: M2Env) -> None:
    d1 = env.gate.decide(GateRequest.from_signal(sig(RequestType.ENTRY)))
    env.halts.activate(HaltType.K1, "local", "kill", ACCOUNT)
    assert env.executor.submit(d1.approved) is IntentState.NOT_SENT
    env.halts.resume(HaltType.K1, ACCOUNT, Channel.LOCAL, "user")
    d2 = env.gate.decide(GateRequest.from_signal(sig(RequestType.ENTRY, "TSTB-USDT", "10.0", "e2")))
    env.clock.advance(timedelta(minutes=31))
    assert env.executor.submit(d2.approved) is IntentState.NOT_SENT and env.broker.sent == []


def test_v124_stepwise_resolution_lets_other_requests_proceed(env: M2Env) -> None:
    env.broker.behavior = FakeBehavior.LOST_ACCEPT
    iid = run(env, sig(RequestType.ENTRY)).approved.intent_id
    r = resolver(env)
    p = r.begin(iid)
    env.broker.behavior = FakeBehavior.FILL
    other = run(env, sig(RequestType.ENTRY, "TSTB-USDT", "10.0", "e2"))   # 해소 대기 중에도 다른 종목 처리
    assert other.result is GateResult.APPROVED
    env.clock.advance(timedelta(seconds=2))
    assert r.step(p) is ResolveOutcome.FOUND


# --- 소유권 (V-80~V-83) -------------------------------------------------------------------------------------------

def test_v80_two_strategies_same_instrument_one_wins(env: M2Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    a = run(env, sig(RequestType.ENTRY, strategy=S1))
    b = run(env, sig(RequestType.ENTRY, ev="e2", strategy=S2))
    assert a.result is GateResult.APPROVED and b.failed_check is Check.G4


def test_v81_ownership_release_cases(tmp_path: Path) -> None:
    e = build_m2_env(tmp_path, ioc=True)
    try:
        e.broker.ioc_liquidity["TSTA-USDT"] = D("0")       # 체결 0 → 해제
        run(e, sig(RequestType.ENTRY))
        rel = e.repo.select("SELECT released_reason FROM ownership WHERE state='RELEASED'")
        assert rel[-1]["released_reason"] == "체결 0주 종결"
        e.broker.ioc_liquidity.pop("TSTA-USDT")
        run(e, sig(RequestType.ENTRY, ev="e2"))
        run(e, sig(RequestType.EXIT, qty="0.0999", ev="x"))
        assert e.repo.select("SELECT released_reason FROM ownership WHERE state='RELEASED'")[-1]["released_reason"] \
            == "전량 청산"
        e.broker.behavior = FakeBehavior.UNKNOWN
        run(e, sig(RequestType.ENTRY, "TSTB-USDT", "10.0", "e3"))
        assert e.repo.active_ownership(ACCOUNT, "TSTB-USDT") is not None   # 결과 불명 → 유지
        assert check_invariants(e.repo.connection, Mode.BACKTEST.value) == []
    finally:
        e.repo.close()


def test_v82_non_owner_strategy_cannot_sell(env: M2Env) -> None:
    run(env, sig(RequestType.ENTRY, strategy=S1))
    d = run(env, sig(RequestType.REDUCE, qty="0.0500", ev="x", strategy=S2))
    assert d.failed_check is Check.G6


def test_v83_blocked_strategy_signal_rejected(env: M2Env) -> None:
    env.gate._in.strategies.blocked[f"{S1}:1"] = "중지"   # noqa: SLF001
    assert run(env, sig(RequestType.ENTRY)).failed_check is Check.G6


# --- 재시작 복구·저장 (V-60·V-73·V-106·V-108) ------------------------------------------------------------------------

def test_v60_recovery_conditions_with_spot_reconciler(env: M2Env) -> None:
    rec = spot_reconciler(env)
    ok = RecoveryChecker(env.repo, rec, env.halts, clock_ok=lambda: True).check(ACCOUNT)
    assert ok.ok
    bad_clock = RecoveryChecker(env.repo, rec, env.halts, clock_ok=lambda: False).check(ACCOUNT)
    assert not bad_clock.ok and "K6" in halts(env)
    env.broker.query_failures = 2
    failed = RecoveryChecker(env.repo, rec, env.halts, clock_ok=lambda: True).check(ACCOUNT)
    assert not failed.ok and "K5" in halts(env)


def test_v73_storage_failure_after_send_blocks_and_k7(env: M2Env, monkeypatch: pytest.MonkeyPatch) -> None:
    d = env.gate.decide(GateRequest.from_signal(sig(RequestType.ENTRY)))

    def boom(*a: object, **k: object) -> None:
        raise StorageFailure("주입: 결과 기록 실패")
    monkeypatch.setattr(env.repo, "finish_attempt", boom)
    with pytest.raises(StorageFailure):
        env.executor.submit(d.approved)
    monkeypatch.undo()
    assert "K7" in halts(env)
    d2 = env.gate.decide(GateRequest.from_signal(sig(RequestType.EXIT, qty="0.0999", ev="x")))
    if d2.approved is not None:
        with pytest.raises(ExecutionBlocked):
            env.executor.submit(d2.approved)
    assert len(env.broker.sent) == 1


def test_v106_audit_append_only_on_m2_db(env: M2Env) -> None:
    run(env, sig(RequestType.ENTRY))
    with pytest.raises(sqlite3.DatabaseError):
        env.repo.connection.execute("UPDATE audit_event SET kind='X' WHERE seq=1")
    with pytest.raises(sqlite3.DatabaseError):
        env.repo.connection.execute("DELETE FROM audit_event WHERE seq=1")


def test_v108_backup_restore_m2_db(env: M2Env, tmp_path: Path) -> None:
    run(env, sig(RequestType.ENTRY))
    saved = backup(tmp_path / "trading.db", tmp_path / "bk")
    rep = restore_test(saved, Mode.BACKTEST.value)
    assert rep.ok, rep.checks


# --- V-126 M2: 같은 조건 외부 주문 ------------------------------------------------------------------------------------

def test_v126_m2_external_same_terms_order_is_k4_ours_adopted_by_client_id(env: M2Env) -> None:
    """[확인 필요 04 §6] M1 기대값(K4 없이 해소기 모호 → K3)과 다르다: M2는 고객 주문 ID로 우리 주문을 구분하므로
    우리 주문은 채택하고, 같은 조건의 외부 주문(ID 없음)은 미확인 브로커 주문 → K4 (보수)."""
    env.broker.behavior = FakeBehavior.LOST_ACCEPT
    iid = run(env, sig(RequestType.ENTRY)).approved.intent_id
    i = env.repo.intent(iid)
    env.broker.add_external_order(ACCOUNT, "TSTA-USDT", "BUY", i.quantity, i.price,
                                  client_order_id="web_ext000001")   # 거래소 자동 부여 ID(가상)
    spot_reconciler(env).run(ACCOUNT, PERIODIC)
    assert SpotItemKind.UNKNOWN_BROKER_ORDER in [x["kind"] for x in env.repo.open_recon_items(ACCOUNT)]
    assert "K4" in halts(env)
    assert resolver(env).resolve(iid) is ResolveOutcome.FOUND


# --- V-157 목록 대응표 -----------------------------------------------------------------------------------------------

V157 = (["V-01"] + [f"V-{n:02d}" for n in range(3, 14)] + [f"V-{n}" for n in range(20, 33)]
        + [f"V-{n}" for n in range(40, 44)] + ["V-45", "V-47", "V-48"] + [f"V-{n}" for n in range(55, 62)]
        + [f"V-{n}" for n in range(65, 70)] + [f"V-{n}" for n in range(71, 74)] + ["V-76"]
        + [f"V-{n}" for n in range(80, 88)] + [f"V-{n}" for n in range(90, 96)] + ["V-100", "V-101", "V-102", "V-104"]
        + [f"V-{n}" for n in range(106, 120)] + ["V-121", "V-122"] + [f"V-{n}" for n in range(124, 129)])

_TM = "tests.scenario"
COVERAGE: dict[str, list[str]] = {
    "V-01": [f"{_TM}.test_tm2_01_flow::test_v01_symbol_limit_boundary_decimal",
             f"{_TM}.test_tm2_01_flow::test_v01_total_limit_boundary_decimal",
             f"{_TM}.test_tm2_09_v157::test_v01_l5_l6_l7_boundaries",
             f"{_TM}.test_tm2_07_cash::test_v140_l4_daily_loss_splits_at_utc_midnight_not_kst",
             f"{_TM}.test_tm2_02_time::test_v141_order_window_is_utc",
             f"{_TM}.test_tm2_09_v157::test_v121_stop_price_l1"],
    "V-03": [f"{_TM}.test_tm2_07_cash::test_v140_l4_daily_loss_splits_at_utc_midnight_not_kst"],
    "V-04": [f"{_TM}.test_tm2_01_flow::test_v04_concurrent_signals_respect_total_limit"],
    "V-05": [f"{_TM}.test_tm2_09_v157::test_v05_unknown_and_open_buys_count_as_filled"],
    "V-06": [f"{_TM}.test_tm2_09_v157::test_v06_reduction_ignores_limits"],
    "V-07": [f"{_TM}.test_tm2_01_flow::test_v07_expired_signal_rejected"],
    "V-08": [f"{_TM}.test_tm2_07_cash::test_v151_insufficient_exchange_free_rejected_g8"],
    "V-09": [f"{_TM}.test_tm2_01_flow::test_entry_net_received_position_then_market_exit"],
    "V-10": [f"{_TM}.test_tm2_01_flow::test_g10_step_and_tick_violation_rejected",
             f"{_TM}.test_tm2_03_rules::test_v133_buy_filter_violation_rejected_nothing_sent"],
    "V-11": [f"{_TM}.test_tm2_02_time::test_v141_order_window_is_utc"],
    "V-12": [f"{_TM}.test_tm2_01_flow::test_v12_oversized_signal_rejected_not_resized"],
    "V-13": [f"{_TM}.test_tm2_09_v157::test_v13_operator_cannot_enter"],
    "V-20": [f"{_TM}.test_tm2_01_flow::test_v20_k1_blocks_entry_allows_operator_reduce",
             f"{_TM}.test_tm2_01_flow::test_v20_kill_file_blocks_risk_increase_send"],
    "V-21": [f"{_TM}.test_tm2_09_v157::test_v21_k2_allows_strategy_reduction_blocks_entry"],
    "V-22": [f"{_TM}.test_tm2_09_v157::test_v22_k3_blocks_account_risk_increase_local_resume_only"],
    "V-23": [f"{_TM}.test_tm2_06_reconcile::test_v144_reset_k4_no_auto_rebaseline_then_reset_confirm"],
    "V-24": [f"{_TM}.test_tm2_09_v157::test_v24_v67_k5_link_down_then_auto_release_after_reconcile"],
    "V-25": [f"{_TM}.test_tm2_09_v157::test_v25_k6_operator_reduction_only"],
    "V-26": [f"{_TM}.test_tm2_01_flow::test_v26_storage_failure_sends_nothing"],
    "V-27": [f"{_TM}.test_tm2_09_v157::test_v27_k8_needs_user_resume_even_when_recovered"],
    "V-28": [f"{_TM}.test_tm2_09_v157::test_v28_v84_k9_user_vs_replace"],
    "V-29": [f"{_TM}.test_tm2_09_v157::test_v29_k10_instrument_scope"],
    "V-30": [f"{_TM}.test_tm2_09_v157::test_v30_k11_blocks_risk_increase_allows_reduction"],
    "V-31": [f"{_TM}.test_tm2_09_v157::test_v31_k2_and_k4_most_strict"],
    "V-32": [f"{_TM}.test_tm2_09_v157::test_v32_remote_resume_refused_for_k3_k4_k7_k8"],
    "V-40": [f"{_TM}.test_tm2_05_identity::test_v138_lost_accept_found_by_client_id_and_fills_applied"],
    "V-41": [f"{_TM}.test_tm2_05_identity::test_v138_five_rounds_nothing_goes_k3_without_auto_close"],
    "V-42": [f"{_TM}.test_tm2_05_identity::test_v138_query_failures_go_k3"],
    "V-43": [f"{_TM}.test_tm2_09_v157::test_v43_new_signal_during_unknown_rejected"],
    "V-45": [f"{_TM}.test_tm2_09_v157::test_v45_reject_streak_alert"],
    "V-47": [f"{_TM}.test_tm2_09_v157::test_v47_pre_send_failure_closes_not_sent"],
    "V-48": [f"{_TM}.test_tm2_09_v157::test_v48_m2_modify_rejected_cancel_confirmed",
             f"{_TM}.test_tm2_03_rules::test_m2_modify_not_supported"],
    "V-55": [f"{_TM}.test_tm2_01_flow::test_v55_duplicate_signal_creates_one_intent"],
    "V-56": [f"{_TM}.test_tm2_09_v157::test_v56_same_evaluation_after_restart_no_new_intent"],
    "V-58": [f"{_TM}.test_tm2_09_v157::test_v58_restart_with_awaiting_response_resolved_by_client_id"],
    "V-59": [f"{_TM}.test_tm2_09_v157::test_v59_pending_send_closed_on_restart"],
    "V-60": [f"{_TM}.test_tm2_09_v157::test_v60_recovery_conditions_with_spot_reconciler"],
    "V-65": [f"{_TM}.test_tm2_06_reconcile::test_v143_base_asset_mismatch_raises_k4"],
    "V-66": [f"{_TM}.test_tm2_09_faults::test_stream_drop_then_reconnect_reconcile_finds_missed_fill"],
    "V-67": [f"{_TM}.test_tm2_09_v157::test_v24_v67_k5_link_down_then_auto_release_after_reconcile"],
    "V-68": [f"{_TM}.test_tm2_03_rules::test_v154_stale_quote_rejects_buy",
             f"{_TM}.test_tm2_03_rules::test_v154_stale_quote_market_sell_rejected_then_limit_ioc_next_request"],
    "V-69": [f"{_TM}.test_tm2_09_faults::test_unknown_codes_not_received_go_k3_without_resend"],
    "V-71": ["tests.contract.test_binance_weights::test_priority_tiers_hold_back_risk_increase_and_market_first"],
    "V-73": [f"{_TM}.test_tm2_09_v157::test_v73_storage_failure_after_send_blocks_and_k7"],
    "V-80": [f"{_TM}.test_tm2_09_v157::test_v80_two_strategies_same_instrument_one_wins"],
    "V-81": [f"{_TM}.test_tm2_09_v157::test_v81_ownership_release_cases"],
    "V-82": [f"{_TM}.test_tm2_09_v157::test_v82_non_owner_strategy_cannot_sell"],
    "V-83": [f"{_TM}.test_tm2_09_v157::test_v83_blocked_strategy_signal_rejected"],
    "V-84": [f"{_TM}.test_tm2_09_v157::test_v28_v84_k9_user_vs_replace"],
    "V-90": [f"{_TM}.test_tm2_12_backtest::test_v90_strategy_only_sees_final_bars_during_run"],
    "V-91": [f"{_TM}.test_tm2_12_backtest::test_v91_perturbation_check_passes_for_causal_strategy",
             f"{_TM}.test_tm2_12_backtest::test_v91_perturbation_check_detects_in_progress_bar_leak"],
    "V-92": [f"{_TM}.test_tm2_12_backtest::test_v92_reproducible_and_sensitive_to_slippage"],
    "V-93": ["tests.architecture.test_imports::test_src_has_no_dependency_violations"],
    "V-94": ["tests.architecture.test_order_path::test_broker_order_calls_only_in_execution",
             "tests.architecture.test_order_path::test_executor_rejects_non_approved_input"],
    "V-95": [f"{_TM}.test_tm2_09_faults::test_v157_v95_m2_random_event_sequences_keep_invariants"],
    "V-106": [f"{_TM}.test_tm2_09_v157::test_v106_audit_append_only_on_m2_db"],
    "V-108": [f"{_TM}.test_tm2_09_v157::test_v108_backup_restore_m2_db"],
    "V-109": ["tests.unit.test_secret_files_policy::test_no_secret_files_tracked"],
    "V-119": [f"{_TM}.test_tm2_09_v157::test_v119_k1_remote_resume_kill_file_still_blocks_send"],
    "V-121": [f"{_TM}.test_tm2_09_v157::test_v121_stop_price_l1"],
    "V-122": [f"{_TM}.test_tm2_09_v157::test_v122_recheck_before_send"],
    "V-124": [f"{_TM}.test_tm2_09_v157::test_v124_stepwise_resolution_lets_other_requests_proceed"],
    "V-125": [f"{_TM}.test_tm2_06_reconcile::test_v155_unknown_order_deferred_across_utc_midnight"],
    "V-126": [f"{_TM}.test_tm2_09_v157::test_v126_m2_external_same_terms_order_is_k4_ours_adopted_by_client_id"],
}
# 이 작업 범위에서 M2 설정으로 실행하지 못한 V (미실행 — 통과 아님). 사유와 담당 작업
NOT_RUN: dict[str, str] = {
    "V-02": "M2에 레버리지·인버스 상품 없음(L10 none, CP-024 §2.4) — 해당 없음으로 볼지 사용자 확인",
    "V-57": "텔레그램 메시지 중복 — 운영 도구·텔레그램 M2 일반화 T-M2-13(다른 작업) 범위",
    "V-61": "절전·루프 정지(엔진 루프·시계 점프) — M2 실행 루프(엔진) 미구현, T-M2-14 이후",
    "V-72": "SNTP 시계 오차 K6 — 공통 감시 코드(M1 시험이 검증), M2 서버 시각 검사(V-146)는 T-M2-14",
    "V-76": "실시간 시세 품질 검사 — M2 시세 공급자 T-M2-11(다른 작업) 범위. 백테스트 일봉 품질 거부만 T-M2-12에 있음",
    "V-85": "전략 등록 검증 — M2 전략 등록·capabilities M2 열 미반영(T-M2-17, capabilities.md 수정 금지 구간)",
    "V-86": "파라미터 검증 — M2 설정 로더 config/m2 T-M2-08(다른 작업) 범위",
    "V-87": "설정 변경 규칙 — M2 설정 로더 T-M2-08(다른 작업) 범위 (UTC 완화 시점은 T-M2-02 시험 있음)",
    "V-100": "TRADE_MODE 검사 — 시작 점검·설정 T-M2-08 범위",
    "V-101": "모드·저장소·자격정보 불일치 — 시작 일치 표 T-M2-08 범위",
    "V-102": "모드 값만 바꾸기 — 시작 점검 T-M2-08·T-M2-14 범위",
    "V-104": "마스킹 — M2 로그 경로(어댑터 키·서명)는 T-M2-10 V-158 시험, 공통 Masker의 M2 재실행은 미작성",
    "V-107": "운영 요청 인증 — 텔레그램 T-M2-13 범위",
    "V-110": "일일 보고 — M2 일일 보고 미구현(일 경계 대조의 on_day_close 콜백만, T-M2-06). 보고 생성은 T-M2-14 이후",
    "V-111": "프로세스 감시(watchdog) — M2 실행 프로그램 미구성",
    "V-112": "전처리 동일성 — M2 실시간 시세 경로(T-M2-11) 연결 뒤",
    "V-113": "검증 전 전략의 PAPER 거부 — M2 전략 등록·상태(T-M2-17)",
    "V-114": "전략·버전별 집계 — 보고(reporting) M2 일반화 미작성",
    "V-115": "견고성 묶음 — 묶음 항목별 M2 재작성 미작성",
    "V-116": "한도 사용률 경고 — AlertWatcher 일 기준이 KST 고정(monitoring.watchers) — M2(UTC) 일반화 필요 [제안]",
    "V-117": "거부 급증 경고 — V-116과 같은 감시 모듈, UTC 일반화 뒤",
    "V-118": "위험 축소 거부 알림 — 운영 명령 경로(ops) M2 일반화 T-M2-13 범위",
    "V-127": "WS 재접속 한도(키움 실시간) — M2 사용자 스트림 연결 관리 실연결 T-M2-14",
    "V-128": "꺼져 있던 동안의 원격 명령 — 텔레그램 T-M2-13 범위",
}


def test_v157_list_is_fully_accounted() -> None:
    assert len(V157) == len(set(V157))
    missing = [v for v in V157 if v not in COVERAGE and v not in NOT_RUN]
    assert missing == [], missing
    assert not set(COVERAGE) & set(NOT_RUN)
    for v, refs in COVERAGE.items():
        assert v in V157, v
        for ref in refs:
            mod, fn = ref.split("::")
            assert callable(getattr(importlib.import_module(mod), fn, None)), ref
