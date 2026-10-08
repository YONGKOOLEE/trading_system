"""CP-031 (A-084, 2026-10-08): 결과 불명 조회 소진(5회 모두 주문 없음) → 자동 미접수 종결 대신 K3·사람 확인.

R-05·R-06, 03 D-047(M1) → D-111과 같은 규칙. 모든 값은 가상이다.
(a) 5회 빈 조회 → K3 활성, 의도 UNKNOWN 유지, 예약·소유권 유지, 같은 종목 재진입 G5 거부, 감사 UNKNOWN_ESCALATED
(b) 재시작 → 재조회·알림 반복 없음, K3 유지
(c) 사람 확정(미접수) → NOT_ACCEPTED, 예약·소유권 해제, K3 재개 가능
(d) 불변 조건 통과
(e) 접수됐지만 안 보이던 주문 → K3 → 맡긴 주문이 있는 대조 일치는 K3 재개 근거 아님 → 잘못된 미접수 확정 뒤에도
    새 대조 전 재개 거부, 새 대조는 미확인 브로커 주문(K4) → 재진입 거부 (CP-031 재검토, V-233)
(f) 2회차 뒤 재시작 → 1회차부터 다시 5회 → K3 1회 (V-234, DP-25)
(g) 늦게 보인 주문을 사람이 접수 채택 → 확정 뒤 새 대조 일치로만 K3 재개 (V-233)
"""

from __future__ import annotations

import json

from tests.scenario.conftest import ACCOUNT, Env, make_signal
from tests.scenario.test_rv_broker import engine_ticks, resolver, unknown_entry
from tests.support.invariants import check_invariants
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import HaltType, IntentState, Mode, RequestType
from trading_system.domain.models import GateRequest
from trading_system.execution.resolver import ESCALATED, ResolveOutcome, ResolveProgress
from trading_system.halt.manager import Channel
from trading_system.reconciliation.reconciler import Reconciler
from trading_system.storage.alerts import AuditAlertSink

NEEDLE = "조회 5회 주문 없음 → K3"


def _alerts(env: Env) -> list[str]:
    return [json.loads(e["summary"])["message"] for e in env.repo.audit_events()
            if e["kind"] == "ALERT" and json.loads(e["summary"])["level"] == "EMERGENCY"
            and NEEDLE in json.loads(e["summary"])["message"]]


def _escalations(env: Env, iid: str) -> list[dict]:
    return [e for e in env.repo.audit_events() if e["kind"] == ESCALATED and e["ref"] == iid]


def test_a_five_empty_queries_keep_unknown_and_raise_k3(env: Env) -> None:
    iid = unknown_entry(env)
    rsv_before = env.repo.active_reservations(ACCOUNT)
    assert rsv_before and env.repo.active_ownership(ACCOUNT, "TST-A") is not None
    assert resolver(env).resolve(iid) is ResolveOutcome.AMBIGUOUS
    assert env.broker.queries == 10                                    # 5회차 × (주문+체결 조회), 회차·간격 그대로
    assert env.repo.intent(iid).state is IntentState.UNKNOWN            # 자동 미접수 종결 없음
    assert env.repo.active_reservations(ACCOUNT) == rsv_before          # 예약 유지
    assert env.repo.active_ownership(ACCOUNT, "TST-A") is not None      # 소유권 유지
    assert "K3" in env.repo.active_halts(ACCOUNT)
    assert _alerts(env) == [f"결과 불명: {NEEDLE}, 사람 확인 필요"]
    assert len(_escalations(env, iid)) == 1
    assert len(env.broker.sent) == 1
    # 같은 종목 재진입 거부 (이중 포지션 방지): K3 정지(G2)가 먼저 막고, 결과 불명 의도(G5)가 두 번째 방어선
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, evaluation="e2")))
    assert d.approved is None and d.failed_check is not None and d.failed_check.value in ("G2", "G5"), (d.failed_check, d.reason)
    assert "K3" in (d.reason or "") or "결과 불명" in (d.reason or "")
    assert check_invariants(env.repo.connection, Mode.BACKTEST.value) == []


def test_b_restart_after_escalation_does_not_requery_or_realert(env: Env) -> None:
    iid = unknown_entry(env)
    r = resolver(env)
    progress: dict[str, ResolveProgress] = {}
    engine_ticks(env, r, progress, 30)                                 # 5분: 62초 뒤 5회 소진 → K3
    assert env.repo.intent(iid).state is IntentState.UNKNOWN and "K3" in env.repo.active_halts(ACCOUNT)
    q, n_alerts = env.broker.queries, len(_alerts(env))
    assert q == 10 and n_alerts == 1
    engine_ticks(env, resolver(env), {}, 200)                          # 재시작: 새 해소기·새 진행 표, 30여 분
    assert env.broker.queries == q and len(_alerts(env)) == n_alerts    # 조회·알림 반복 없음
    assert len(_escalations(env, iid)) == 1
    assert env.repo.intent(iid).state is IntentState.UNKNOWN and "K3" in env.repo.active_halts(ACCOUNT)
    assert env.repo.active_reservations(ACCOUNT)


def test_c_human_not_accepted_closes_and_releases_then_k3_resumable(env: Env) -> None:
    iid = unknown_entry(env)
    r = resolver(env)
    progress: dict[str, ResolveProgress] = {}
    engine_ticks(env, r, progress, 30)
    assert "K3" in env.repo.active_halts(ACCOUNT)
    assert not env.halts.resume(HaltType.K3, ACCOUNT, Channel.LOCAL, "u").ok     # 결과 불명 남아 있음
    env.halts.confirm_unknown(iid, False, "operator")
    assert env.repo.intent(iid).state is IntentState.NOT_ACCEPTED
    assert env.repo.active_reservations(ACCOUNT) == []
    assert env.repo.active_ownership(ACCOUNT, "TST-A") is None
    engine_ticks(env, r, progress, 3)
    assert progress == {} and env.broker.queries == 10
    assert not env.halts.resume(HaltType.K3, ACCOUNT, Channel.LOCAL, "u").ok     # 대조 일치 전
    env.halts.report_condition(HaltType.K3, ACCOUNT, "reconciled", True, "test")
    assert env.halts.resume(HaltType.K3, ACCOUNT, Channel.LOCAL, "u").ok
    assert env.repo.active_halts(ACCOUNT) == []
    assert check_invariants(env.repo.connection, Mode.BACKTEST.value) == []


def test_sync_resolve_twice_raises_k3_once_and_keeps_unknown(env: Env) -> None:
    """운영 명령의 동기 해소를 거듭해도 K3 중복 발동·의도 종결 없음 (알림은 판정마다 1회)."""
    iid = unknown_entry(env)
    assert resolver(env).resolve(iid) is ResolveOutcome.AMBIGUOUS
    assert resolver(env).resolve(iid) is ResolveOutcome.AMBIGUOUS
    k3 = [h for h in env.repo.halt_rows(None) if h["halt_type"] == "K3"]
    assert len(k3) == 1
    assert env.repo.intent(iid).state is IntentState.UNKNOWN and env.broker.queries == 20


# --- CP-031 재검토 (V-233·V-234): K3 재개는 사람 확정 뒤의 새 대조 일치만 인정 ------------------------

def _rec(env: Env) -> Reconciler:
    return Reconciler(env.repo, env.broker, env.clock, env.halts, AuditAlertSink(env.repo, env.clock),
                      drain_events=env.executor.process_events)


def _invisible_accepted_entry(env: Env) -> str:
    """브로커는 접수했지만 응답 유실 + 실시간 통보 없음 + 해소 조회 5회(10번 조회) 동안 조회에 안 보이는 주문."""
    env.broker.behavior = FakeBehavior.LOST_ACCEPT
    env.broker.query_delay = 10
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, evaluation="e1")))
    assert d.approved is not None, d.reason
    env.executor.submit(d.approved)
    env.executor.process_events()
    env.broker.behavior = FakeBehavior.FILL
    iid = d.approved.intent_id
    assert env.repo.intent(iid).state is IntentState.UNKNOWN
    assert resolver(env).resolve(iid) is ResolveOutcome.AMBIGUOUS        # 5회 모두 주문 없음 → K3
    assert env.repo.intent(iid).state is IntentState.UNKNOWN and "K3" in env.repo.active_halts(ACCOUNT)
    return iid


def _k3_reconciled(env: Env) -> dict:
    [row] = [h for h in env.repo.halt_rows(None) if h["halt_type"] == "K3"]
    return json.loads(row["conditions"]).get("reconciled", {})


def test_e_wrong_not_accepted_confirm_needs_fresh_reconcile_and_blocks_reentry(env: Env) -> None:
    """검토 지적(중간): 결과 불명 의도에 맡긴(deferred) 브로커 주문이 있는 대조 일치가 K3 reconciled를 채우고,
    사람이 '미접수'로 잘못 확정하면 새 대조 없이 K3 재개 → 같은 종목 재진입 승인 → 이중 주문."""
    iid = _invisible_accepted_entry(env)
    [order] = env.broker.query_orders_unchecked()
    r1 = _rec(env).run(ACCOUNT, "PERIODIC")                              # 이제 주문이 조회에 보임 → 결과 불명에 맡김
    assert r1.result == "MATCH"
    assert order.order_no in json.loads(env.repo.select(
        "SELECT summary FROM reconciliation WHERE recon_id=?", (r1.recon_id,))[0]["summary"])["deferred_unknown"]
    assert not _k3_reconciled(env).get("ok")                             # 맡긴 주문이 있는 일치는 K3 재개 근거가 아님
    env.halts.confirm_unknown(iid, False, "operator")                    # 사람의 잘못된 '미접수' 확정
    assert env.repo.intent(iid).state is IntentState.NOT_ACCEPTED
    res = env.halts.resume(HaltType.K3, ACCOUNT, Channel.LOCAL, "u")
    assert not res.ok and "reconciled" in res.reason                    # 확정 뒤 새 대조 전에는 재개 거부
    assert "K3" in env.repo.active_halts(ACCOUNT)
    r2 = _rec(env).run(ACCOUNT, "PERIODIC")                              # 새 대조: 그 주문은 이제 미확인 브로커 주문
    assert r2.result == "MISMATCH"
    kinds = [i["kind"] for i in env.repo.open_recon_items(ACCOUNT)]
    assert "UNKNOWN_BROKER_ORDER" in kinds
    assert "K4" in env.repo.active_halts(ACCOUNT)
    assert not env.halts.resume(HaltType.K3, ACCOUNT, Channel.LOCAL, "u").ok
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, evaluation="e2")))
    assert d.approved is None, d.reason                                  # 같은 종목 재진입 거부 (이중 주문 없음)
    assert len(env.broker.sent) == 1


def test_f_restart_mid_resolution_restarts_from_round_one_then_k3_once(env: Env) -> None:
    """V-234 / DP-25: 2회차까지 조회한 뒤 재시작 → 새 해소기가 1회차부터 다시 5회 → K3 1회."""
    iid = unknown_entry(env)
    engine_ticks(env, resolver(env), {}, 3)                              # 10초 간격: 2초·6초 뒤 1·2회차
    assert env.broker.queries == 4 and "K3" not in env.repo.active_halts(ACCOUNT)
    assert _escalations(env, iid) == []
    engine_ticks(env, resolver(env), {}, 30)                             # 재시작: 새 해소기·새 진행 표
    assert env.broker.queries == 4 + 10                                  # 1회차부터 다시 5회 (이어 하지 않음)
    k3 = [h for h in env.repo.halt_rows(None) if h["halt_type"] == "K3"]
    assert len(k3) == 1 and len(_alerts(env)) == 1 and len(_escalations(env, iid)) == 1
    assert env.repo.intent(iid).state is IntentState.UNKNOWN
    assert len(env.broker.sent) == 1


def test_g_late_order_adopted_by_human_resume_only_after_fresh_match(env: Env) -> None:
    """5회 미발견 → K3 → 주문이 늦게 보임 → 사람 '접수' 채택 → 새 대조 일치 뒤에만 K3 재개 (V-233)."""
    iid = _invisible_accepted_entry(env)
    [order] = env.broker.query_orders_unchecked()
    assert _rec(env).run(ACCOUNT, "PERIODIC").result == "MATCH"          # 맡긴 주문 → K3 근거 아님
    env.halts.confirm_unknown(iid, True, "operator", order.order_no)
    assert env.repo.intent(iid).broker_order_key is not None
    assert not _k3_reconciled(env).get("ok")
    assert not env.halts.resume(HaltType.K3, ACCOUNT, Channel.LOCAL, "u").ok
    r = _rec(env).run(ACCOUNT, "PERIODIC")
    assert r.result == "MATCH"
    assert _k3_reconciled(env).get("ok") and _k3_reconciled(env).get("recon") == r.recon_id
    assert env.halts.resume(HaltType.K3, ACCOUNT, Channel.LOCAL, "u").ok
    assert "K4" not in env.repo.active_halts(ACCOUNT)
    assert check_invariants(env.repo.connection, Mode.BACKTEST.value) == []
