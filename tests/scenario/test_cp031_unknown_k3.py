"""CP-031 (A-084, 2026-10-08): 결과 불명 조회 소진(5회 모두 주문 없음) → 자동 미접수 종결 대신 K3·사람 확인.

R-05·R-06, 03 D-047(M1) → D-111과 같은 규칙. 모든 값은 가상이다.
(a) 5회 빈 조회 → K3 활성, 의도 UNKNOWN 유지, 예약·소유권 유지, 같은 종목 재진입 G5 거부, 감사 UNKNOWN_ESCALATED
(b) 재시작 → 재조회·알림 반복 없음, K3 유지
(c) 사람 확정(미접수) → NOT_ACCEPTED, 예약·소유권 해제, K3 재개 가능
(d) 불변 조건 통과
"""

from __future__ import annotations

import json

from tests.scenario.conftest import ACCOUNT, Env, make_signal
from tests.scenario.test_rv_broker import engine_ticks, resolver, unknown_entry
from tests.support.invariants import check_invariants
from trading_system.domain.enums import HaltType, IntentState, Mode, RequestType
from trading_system.domain.models import GateRequest
from trading_system.execution.resolver import ESCALATED, ResolveOutcome, ResolveProgress
from trading_system.halt.manager import Channel

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
