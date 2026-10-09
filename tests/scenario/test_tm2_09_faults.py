"""T-M2-09 fake 장애 주입 M2판 + 무작위 사건열 (05 T-M2-09, 04 V-157(V-95 M2판)·D-070, CP-024 D-099·D-104·D-112·D-113).

- 지정 장애: IOC EXPIRED, 합성 응답 코드(-1013·-1021·-2010·-2010 중복·429·418·5XX·403·-1022), 응답 유실, 테스트넷 리셋,
  사용자 스트림 끊김(serverShutdown 포함 — 재연결 뒤 대조). 기대값은 D-112 4분류·D-111·D-099.
- fake 합성 응답의 4분류가 어댑터 분류(broker/binance/classify, T-M2-10)와 같은지 계약 시험으로 대조한다.
- 무작위 사건열: 시드 200 × 사건 50 (A-026과 같은 규모, M2 설정). 매 실행 끝에 INV와 안전 속성(재전송 없음, 정지·KILL 중 위험
  확대 없음, M2 시장가 매수·정정 전송 없음)을 검사한다. 모든 값은 가상이다. 네트워크 없음.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.invariants import check_invariants
from tests.support.m2_fakes import ACCOUNT, M2Env, build_m2_env
from tests.support.scenario_dsl_m2 import M2Runner, random_script_m2
from trading_system.broker.binance.classify import classify_order_reply
from trading_system.broker.fake.spot import REPLY_TABLE
from trading_system.domain.enums import HaltType, IntentState, Mode

SEEDS = 200
EVENTS = 50
D = Decimal


@pytest.fixture
def env(tmp_path: Path) -> Iterator[M2Env]:
    e = build_m2_env(tmp_path, ioc=True)
    yield e
    violations = check_invariants(e.repo.connection, Mode.BACKTEST.value)
    e.repo.close()
    assert violations == [], violations


def intents(env: M2Env) -> list:  # type: ignore[type-arg]
    return env.repo.intents()


def k(env: M2Env, halt: str) -> bool:
    return halt in env.repo.active_halts(ACCOUNT)


# --- fake 합성 응답 = 어댑터 4분류 (D-112) ----------------------------------------------------------------------

@pytest.mark.parametrize("code", sorted(REPLY_TABLE))
def test_fake_reply_classes_match_adapter_classification(code: str) -> None:
    spec = REPLY_TABLE[code]
    verdict = classify_order_reply(spec.status, spec.body, cancel=False)
    assert verdict.outcome is spec.outcome, code
    assert verdict.code == spec.code


# --- 지정 장애 ---------------------------------------------------------------------------------------------------

def test_ioc_partial_then_expired(env: M2Env) -> None:
    r = M2Runner(env)
    r.run("""
        liquidity TSTA-USDT 0.0400
        entry TSTA-USDT 0.1000
        process
    """)
    [i] = intents(env)
    assert i.state is IntentState.EXPIRED and i.filled_quantity == D("0.0400")
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == D("0.0400") - D("0.0000400")
    assert env.repo.active_reservations(ACCOUNT) == []


@pytest.mark.parametrize("code", ["-1013", "-1021", "-2010"])
def test_explicit_reject_codes_close_without_resend(env: M2Env, code: str) -> None:
    M2Runner(env).run(f"reply {code}\nentry TSTA-USDT 0.1000\nresolve")
    [i] = intents(env)
    assert i.state is IntentState.REJECTED and len(env.broker.sent) == 1
    assert env.repo.active_reservations(ACCOUNT) == [] and env.repo.active_ownership(ACCOUNT, "TSTA-USDT") is None


@pytest.mark.parametrize("code", ["429", "5xx", "403", "-1022", "-2010dup"])
def test_unknown_codes_not_received_go_k3_without_resend(env: M2Env, code: str) -> None:
    M2Runner(env).run(f"reply {code}\nentry TSTA-USDT 0.1000\nresolve")
    [i] = intents(env)
    assert i.state is IntentState.UNKNOWN and k(env, "K3") and len(env.broker.sent) == 1


def test_5xx_executed_is_found_by_client_id(env: M2Env) -> None:
    M2Runner(env).run("reply 5xx+\nentry TSTA-USDT 0.1000\ndrop\nresolve")
    [i] = intents(env)
    assert i.state is IntentState.FILLED and not k(env, "K3") and len(env.broker.sent) == 1
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == D("0.0999")


def test_418_stops_calls_and_reconcile_failures_raise_k5(env: M2Env) -> None:
    r = M2Runner(env)
    r.run("reply 418\nentry TSTA-USDT 0.1000")
    sent = len(env.broker.sent)
    r.run("entry TSTB-USDT 10.0")                    # 차단 중: 전송 전 실패 (거래소로 보내지 않음)
    assert len(env.broker.sent) == sent
    assert [i.state for i in intents(env)][-1] is IntentState.NOT_SENT
    r.run("reconcile\nreconcile\nreconcile")         # 조회도 실패 → 연속 3회 → K5
    assert k(env, "K5")


def test_testnet_reset_is_detected_k4(tmp_path: Path) -> None:
    e = build_m2_env(tmp_path, ioc=True, spot_cash=True)
    try:
        f = e.account
        f.set_baseline(ACCOUNT, D("10000"), e.clock.now())
        from trading_system.monitoring.spot_cash import QuoteBalance
        f.update_balance(ACCOUNT, QuoteBalance(D("10000"), D("0"), e.clock.now()))
        f.start_day(ACCOUNT)
        e.clock.advance(timedelta(seconds=1))
        M2Runner(e).run("entry TSTA-USDT 0.1000\nreconcile")
        assert not k(e, "K4")
        M2Runner(e).run("reset\nreconcile")
        assert k(e, "K4")
        assert check_invariants(e.repo.connection, Mode.BACKTEST.value) == []
    finally:
        e.repo.close()


def test_stream_drop_then_reconnect_reconcile_finds_missed_fill(env: M2Env) -> None:
    r = M2Runner(env)
    r.run("behavior ACCEPT_ONLY\nioc off\nentry TSTA-USDT 0.1000\nfill 0\nstream-drop\nreconcile")
    trig = [json.loads(e["summary"])["trigger"] for e in env.repo.audit_events() if e["kind"] == "RECONCILE"]
    assert trig == ["STREAM_RECONNECT"]
    assert k(env, "K4")   # 놓친 체결은 대조 불일치 → 사람 확인 (자동 반영 없음, V-66과 같음)


def test_modify_is_rejected_in_m2(env: M2Env) -> None:
    r = M2Runner(env)
    r.run("behavior ACCEPT_ONLY\nioc off\nentry TSTA-USDT 0.1000\nmodify 0")
    assert r.violations == []
    assert not [x for x in env.broker.sent if x.kind == "MODIFY"]


# --- 무작위 사건열 (V-157 중 V-95, M2 설정) ------------------------------------------------------------------------

def _run_seed(seed: int, base: Path) -> list[str]:
    tmp = base / f"s{seed}"
    tmp.mkdir()
    env = build_m2_env(tmp, ioc=True)
    runner = M2Runner(env)
    script = random_script_m2(seed, EVENTS)
    problems: list[str] = []
    try:
        runner.run(script)
        runner.step("process")
    except Exception as exc:  # 예상하지 못한 예외도 위반
        problems.append(f"예외 {type(exc).__name__}: {exc}")
    env.repo.fail_next_transaction = False
    problems += runner.violations
    problems += check_invariants(env.repo.connection, Mode.BACKTEST.value)
    attempts = env.repo.connection.execute("SELECT COUNT(*) FROM attempt WHERE state <> 'NOT_SENT'").fetchone()[0]
    if len(env.broker.sent) > attempts:
        problems.append(f"재전송 의심: 브로커 수신 {len(env.broker.sent)} > 전송 시도 {attempts}")
    env.repo.close()
    if problems:
        return [f"seed={seed} 위반={problems}\n  명령열:\n    " + "\n    ".join(script)]
    return []


@pytest.mark.slow
def test_v157_v95_m2_random_event_sequences_keep_invariants(tmp_path: Path) -> None:
    failures: list[str] = []
    for seed in range(SEEDS):
        failures += _run_seed(seed, tmp_path)
    assert not failures, f"{len(failures)}/{SEEDS} 시드 위반\n" + "\n".join(failures[:3])


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_m2_random_runs_are_reproducible(seed: int) -> None:
    assert random_script_m2(seed, EVENTS) == random_script_m2(seed, EVENTS)
