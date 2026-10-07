"""T-022 원격 운영 확장 (CP-026, A-068·A-069, 01 §2.9, 03 §6.6, 04 V-32·V-181~V-186).

- 원격 K4·K8 재개: 명령 처리 시점에 대조를 새로 1회 하고, 그 대조 기준으로 재개 조건을 다시 본다(A-069 ①②).
- 원격 대조(/reconcile), 오늘 요약(/review). K3·K5·K7은 여전히 로컬만.
모든 값은 가상이다. 텔레그램은 로컬 가짜 서버(test_t013_telegram.FakeTelegram)로만 시험한다.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, Env, make_signal
from tests.scenario.test_t013_telegram import CHAT, FakeTelegram, api, code_from
from tests.scenario.test_t020_app import ACCOUNT as APP_ACCOUNT, World
from trading_system.domain.enums import HaltType, RequestType
from trading_system.domain.halts import HaltState
from trading_system.domain.models import GateRequest
from trading_system.halt.manager import Channel
from trading_system.ops.commands import OpsProcessor
from trading_system.ops.telegram import CommandBot
from trading_system.reconciliation.reconciler import Reconciler, RecoveryChecker
from trading_system.storage.alerts import AuditAlertSink


@pytest.fixture
def tg() -> FakeTelegram:
    f = FakeTelegram()
    yield f
    f.close()


def rec(env: Env) -> Reconciler:
    r = Reconciler(env.repo, env.broker, env.clock, env.halts, AuditAlertSink(env.repo, env.clock))
    # 엔진(app/engine.py _after_reconcile)과 같은 배선: 시작 대조가 아닌 대조 뒤 K8이 있으면 복구 판정
    checker = RecoveryChecker(env.repo, r, env.halts, lambda: True)
    r.listeners.append(lambda acct, trig, res: checker.evaluate(acct, res) if trig != "STARTUP" and any(
        h["halt_type"] == "K8" for h in env.repo.halt_rows(acct)) else None)
    return r


def ops(env: Env, reconciler: Reconciler | None = None) -> OpsProcessor:
    return OpsProcessor(env.repo, env.gate, env.executor, env.halts, env.clock, kill_file=env.kill_file,
                        reconciler=reconciler or rec(env))


def remote(env: Env, cid: str, name: str, args: dict) -> None:
    with env.repo.tx():
        env.repo.insert_command(cid, Channel.REMOTE, "telegram", name, args, env.clock.now())


def enter(env: Env, inst: str = "TST-A", qty: str = "10", ev: str = "e1") -> None:
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, instrument=inst, quantity=qty,
                                                            evaluation=ev)))
    assert d.approved is not None, d.reason
    env.executor.submit(d.approved)
    env.executor.process_events()


def bot(env: Env, tg: FakeTelegram) -> CommandBot:
    sink = AuditAlertSink(env.repo, env.clock)
    return CommandBot(env.repo, api(tg), CHAT, ACCOUNT, env.clock, env.kill_file, lambda lvl, m: sink.alert(lvl, m, None))


def confirm(b: CommandBot, tg: FakeTelegram, uid: int, text: str) -> None:
    tg.push(uid, text)
    b.poll_once(timeout=0)
    tg.push(uid + 1, f"/confirm {code_from(tg)}")
    b.poll_once(timeout=0)


# --- V-181 원격 K4 재개: 새 대조 MATCH일 때만 -------------------------------------------------------

def test_v181_remote_k4_resume_runs_fresh_reconcile_and_resumes_on_match(env: Env) -> None:
    enter(env)
    env.halts.activate(HaltType.K4, "reconciler", "불일치", ACCOUNT)
    remote(env, "r1", "RESUME", {"account": ACCOUNT, "halt_type": "K4"})
    [o] = ops(env).process_pending()
    assert o.status == "DONE", o.result
    assert "K4" not in env.repo.active_halts(ACCOUNT)
    last = env.repo.latest_reconciliation(ACCOUNT)
    assert last["trigger"] == "REMOTE_RESUME" and last["result"] == "MATCH"


def test_v181_remote_k4_resume_rejected_when_fresh_reconcile_mismatches(env: Env) -> None:
    enter(env)
    no = env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("30"))   # 앱에서 직접 매수
    env.broker.fill(no)
    env.broker.drop_events()
    env.halts.activate(HaltType.K4, "reconciler", "불일치", ACCOUNT)
    remote(env, "r1", "RESUME", {"account": ACCOUNT, "halt_type": "K4"})
    [o] = ops(env).process_pending()
    assert o.status == "REJECTED" and "MISMATCH" in o.result
    assert "K4" in env.repo.active_halts(ACCOUNT)


def test_v181_stale_reconciled_condition_is_not_enough_for_remote_k4(env: Env) -> None:
    """이전 대조의 reconciled=충족만으로는 원격 재개 불가 — 이번 명령의 대조 ID여야 한다."""
    env.halts.activate(HaltType.K4, "reconciler", "불일치", ACCOUNT)
    env.halts.report_condition(HaltType.K4, ACCOUNT, "reconciled", True, "rc-old")
    r = env.halts.resume(HaltType.K4, ACCOUNT, Channel.REMOTE, "u")
    assert not r.ok and "새 대조" in r.reason
    r = env.halts.resume(HaltType.K4, ACCOUNT, Channel.REMOTE, "u", fresh_recon="rc-new")
    assert not r.ok
    assert env.halts.resume(HaltType.K4, ACCOUNT, Channel.LOCAL, "u").ok   # 로컬은 기존 규칙 그대로


def test_v181_remote_k4_rejected_when_query_fails(env: Env) -> None:
    env.halts.activate(HaltType.K4, "reconciler", "불일치", ACCOUNT)
    env.broker.query_failures = 1
    remote(env, "r1", "RESUME", {"account": ACCOUNT, "halt_type": "K4"})
    [o] = ops(env).process_pending()
    assert o.status == "REJECTED" and "QUERY_FAILED" in o.result
    assert "K4" in env.repo.active_halts(ACCOUNT)


def test_v181_remote_k4_needs_account(env: Env) -> None:
    env.halts.activate(HaltType.K4, "reconciler", "불일치", ACCOUNT)
    remote(env, "r1", "RESUME", {"account": None, "halt_type": "K4"})
    [o] = ops(env).process_pending()
    assert o.status == "REJECTED"
    assert "K4" in env.repo.active_halts(ACCOUNT)


# --- V-182 원격 K8 재개: 복구 조건 재검사 -------------------------------------------------------------

def test_v182_remote_k8_resume_rechecks_recovery_with_fresh_reconcile(env: Env) -> None:
    env.halts.on_start()
    env.halts.report_condition(HaltType.K8, None, "recovery_ok", True, "시작 대조 rc-start")   # 시작 판정 통과
    assert not env.halts.resume(HaltType.K8, None, Channel.REMOTE, "u").ok   # 새 대조 없이 원격 재개 불가
    remote(env, "r1", "RESUME", {"account": ACCOUNT, "halt_type": "K8"})
    [o] = ops(env).process_pending()
    assert o.status == "DONE", o.result
    assert "K8" not in env.repo.active_halts(ACCOUNT)


def test_v182_remote_k8_rejected_when_recovery_fails(env: Env) -> None:
    env.halts.on_start()
    env.halts.report_condition(HaltType.K8, None, "recovery_ok", True, "시작 대조 rc-start")
    enter_blocked = env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("5"))
    env.broker.fill(enter_blocked)
    env.broker.drop_events()
    remote(env, "r1", "RESUME", {"account": ACCOUNT, "halt_type": "K8"})
    [o] = ops(env).process_pending()
    assert o.status == "REJECTED"
    assert "K8" in env.repo.active_halts(ACCOUNT)


def test_v182_remote_k8_rejected_without_recovery_evaluation(env: Env) -> None:
    """대조 뒤 복구 판정이 돌지 않으면(배선 누락) recovery_ok가 이번 대조 기준이 아니므로 거부 — 안전한 실패."""
    env.halts.on_start()
    env.halts.report_condition(HaltType.K8, None, "recovery_ok", True, "시작 대조 rc-start")
    bare = Reconciler(env.repo, env.broker, env.clock, env.halts, AuditAlertSink(env.repo, env.clock))
    remote(env, "r1", "RESUME", {"account": ACCOUNT, "halt_type": "K8"})
    [o] = ops(env, bare).process_pending()
    assert o.status == "REJECTED" and "recovery_ok" in o.result
    assert "K8" in env.repo.active_halts(ACCOUNT)


# --- V-32 (CP-026 개정) K3·K5·K7은 여전히 로컬만 --------------------------------------------------------

@pytest.mark.parametrize("k", [HaltType.K3, HaltType.K5, HaltType.K7])
def test_v32_local_only_halts_still_refused_remotely_even_after_fresh_reconcile(env: Env, k: HaltType) -> None:
    env.halts.activate(k, "test", "x", ACCOUNT)
    remote(env, "r1", "RESUME", {"account": ACCOUNT, "halt_type": k.value})
    [o] = ops(env).process_pending()
    assert o.status == "REJECTED" and "로컬" in o.result
    assert k.value in env.repo.active_halts(ACCOUNT)
    assert env.repo.latest_reconciliation(ACCOUNT) is None   # 로컬 전용 정지에는 대조도 돌리지 않는다


# --- V-183 원격 대조 ---------------------------------------------------------------------------------

def test_v183_remote_reconcile_allowed_and_reports_result(env: Env) -> None:
    enter(env)
    remote(env, "r1", "RECONCILE", {"account": ACCOUNT})
    [o] = ops(env).process_pending()
    assert o.status == "DONE" and "MATCH" in o.result and "열린 불일치 0건" in o.result
    assert env.repo.latest_reconciliation(ACCOUNT)["trigger"] == "MANUAL"


def test_v183_remote_reconcile_resolve_still_local_only(env: Env) -> None:
    remote(env, "r1", "RECONCILE_RESOLVE", {"item_id": "x", "method": "EXTERNAL"})
    [o] = ops(env).process_pending()
    assert o.status == "REJECTED" and "로컬" in o.result


# --- V-184 텔레그램 명령 (확인 코드 유지) ------------------------------------------------------------------

def test_v184_telegram_resume_k4_and_reconcile_need_confirmation_and_carry_account(env: Env, tg: FakeTelegram) -> None:
    b = bot(env, tg)
    tg.push(1, "/resume K4")
    b.poll_once(timeout=0)
    assert env.repo.pending_commands() == [] and "/confirm" in tg.sent[-1]["text"]
    tg.push(2, f"/confirm {code_from(tg)}")
    b.poll_once(timeout=0)
    confirm(b, tg, 10, "/reconcile")
    confirm(b, tg, 20, "/resume K1")
    cmds = {c["command_id"]: c for c in env.repo.pending_commands()}
    import json
    assert json.loads(cmds["tg:1"]["args"]) == {"account": ACCOUNT, "halt_type": "K4"}
    assert cmds["tg:10"]["command"] == "RECONCILE" and json.loads(cmds["tg:10"]["args"]) == {"account": ACCOUNT}
    assert json.loads(cmds["tg:20"]["args"]) == {"account": None, "halt_type": "K1"}   # K1·K2는 기존 그대로


def test_v184_help_lists_new_commands(env: Env, tg: FakeTelegram) -> None:
    b = bot(env, tg)
    tg.push(1, "/help")
    b.poll_once(timeout=0)
    text = tg.sent[-1]["text"]
    assert "/resume K1|K2|K4|K8" in text and "/reconcile" in text and "/review" in text


# --- V-185 /review 요약 --------------------------------------------------------------------------------

def test_v185_review_summarises_today_without_amounts_or_account(env: Env, tg: FakeTelegram) -> None:
    enter(env, "TST-A", "10", "e1")
    env.halts.activate(HaltType.K4, "reconciler", "불일치", ACCOUNT)
    env.kill_file.write_text("KILL")
    b = bot(env, tg)
    tg.push(1, "/review")
    b.poll_once(timeout=0)
    text = tg.sent[-1]["text"]
    assert env.repo.pending_commands() == []          # 조회만 — 명령 테이블에 넣지 않는다
    assert "오늘 체결 1건" in text and "TST-A 매수 10" in text
    assert "K4(ACTIVE)" in text and "KILL 파일: 있음" in text
    assert "결과 불명: 0건" in text and "대조 불일치: 0건" in text
    assert ACCOUNT not in text
    assert "원" not in text.replace("원격", "")   # 금액 표시 없음


def test_v185_review_excludes_previous_days_fills(env: Env, tg: FakeTelegram) -> None:
    enter(env, "TST-A", "10", "e1")
    env.clock.advance(timedelta(days=1))
    b = bot(env, tg)
    tg.push(1, "/review")
    b.poll_once(timeout=0)
    assert "오늘 체결 0건" in tg.sent[-1]["text"]


# --- V-186 실제 배선(build_engine)으로 원격 K8 재개 ---------------------------------------------------------

def test_v186_remote_k8_resume_through_real_wiring(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        w.engine.start()
        assert "K8" in [h["halt_type"] for h in w.repo.halt_rows(None) if h["state"] != HaltState.RELEASED.value]
        with w.repo.tx():
            w.repo.insert_command("tg:1", "REMOTE", "telegram", "RESUME", {"account": APP_ACCOUNT, "halt_type": "K8"},
                                  w.clock.now())
        w.engine.tick()
        row = dict(w.repo.select("SELECT status, result FROM ops_command WHERE command_id='tg:1'")[0])
        assert row["status"] == "DONE", row
        assert not any(h["halt_type"] == "K8" and h["state"] != HaltState.RELEASED.value
                       for h in w.repo.halt_rows(None))
    finally:
        w.close()


# --- CP-026 재검토 반영 ---------------------------------------------------------------------------

def test_review1_executor_raised_k4_not_resumable_remotely(env: Env) -> None:
    """실행기가 건 K4(비정상 통보 등)는 대조 일치만으로 원인이 설명되지 않는다 → 원격 거부, 로컬은 가능."""
    env.halts.activate(HaltType.K4, "tracker", "비정상 통보: 모르는 주문번호", ACCOUNT)
    remote(env, "r1", "RESUME", {"account": ACCOUNT, "halt_type": "K4"})
    [o] = ops(env).process_pending()
    assert o.status == "REJECTED" and "실행기" in o.result
    assert "K4" in env.repo.active_halts(ACCOUNT)
    assert env.halts.resume(HaltType.K4, ACCOUNT, Channel.LOCAL, "u").ok


def test_review1_reconciler_k4_reraised_by_executor_not_resumable_remotely(env: Env) -> None:
    env.halts.activate(HaltType.K4, "reconciler", "대조 불일치 1건", ACCOUNT)
    env.halts.activate(HaltType.K4, "tracker", "종결 후 체결", ACCOUNT)   # 같은 행 재발동
    remote(env, "r1", "RESUME", {"account": ACCOUNT, "halt_type": "K4"})
    [o] = ops(env).process_pending()
    assert o.status == "REJECTED" and "실행기" in o.result


def test_review2_global_k8_not_resumed_remotely_with_multiple_accounts(env: Env) -> None:
    from trading_system.domain.models import Account
    other = Account(alias="KR-TEST-02", mode=env.repo.mode, broker_env=env.repo.account(ACCOUNT).broker_env)
    with env.repo.tx():
        env.repo.register_account(other)
    env.halts.on_start()
    remote(env, "r1", "RESUME", {"account": ACCOUNT, "halt_type": "K8"})
    [o] = ops(env).process_pending()
    assert o.status == "REJECTED" and "여러 계좌" in o.result
    assert "K8" in env.repo.active_halts(ACCOUNT)
