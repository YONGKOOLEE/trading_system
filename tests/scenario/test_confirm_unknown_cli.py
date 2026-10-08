"""결과 불명 확정 로컬 도구 confirm-unknown (CP-031 V-233 사람 확정 경로, 03 §6.6, LOCAL_ONLY CONFIRM_UNKNOWN).

CP-031 이후 조회 5회로 해소되지 않은 결과 불명 의도는 K3로 올라가고 사람만 확정할 수 있다. 이 도구는 그 확정을
명령 테이블에 기록만 한다 (판정·반영은 본 프로세스의 명령 처리기 → halt.confirm_unknown). 모든 값은 가상이다.
(a) --not-accepted 기록 → 처리기 → NOT_ACCEPTED, K3는 확정 뒤 새 대조 일치로만 재개
(b) --accepted --order-no 기록 → 처리기 → 늦게 보인 주문 채택, K3는 새 대조 일치 뒤에만 재개
(c) --mode LIVE는 저장소를 열기 전에 거부
(d) 없는 의도·결과 불명이 아닌 의도·DB 없음 → 종료 코드 2, 기록 없음
(e) --list: 결과 불명 의도 목록 (읽기 전용)
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, Env, make_signal
from tests.scenario.test_cp031_unknown_k3 import _invisible_accepted_entry, _k3_reconciled, _rec
from tests.scenario.test_rv_broker import engine_ticks, resolver, unknown_entry
from trading_system.domain.enums import HaltType, IntentState, Mode, RequestType
from trading_system.domain.models import GateRequest
from trading_system.execution.resolver import ResolveProgress
from trading_system.halt.manager import Channel
from trading_system.ops.cli import confirm_unknown_main
from trading_system.ops.commands import OpsProcessor
from trading_system.storage.repository import Repository

REPO = Path(__file__).resolve().parents[2]


def _db(tmp_path: Path) -> list[str]:
    return ["--mode", "BACKTEST", "--db", str(tmp_path / "trading.db")]


def _commands(env: Env) -> list[dict]:
    return env.repo.select("SELECT * FROM ops_command WHERE command='CONFIRM_UNKNOWN' ORDER BY created_seq")


def _k3_escalated(env: Env) -> str:
    iid = unknown_entry(env)
    engine_ticks(env, resolver(env), {}, 30)                              # 5회 빈 조회 → K3 (CP-031)
    assert env.repo.intent(iid).state is IntentState.UNKNOWN and "K3" in env.repo.active_halts(ACCOUNT)
    return iid


def _process(env: Env) -> list:
    return OpsProcessor(env.repo, env.gate, env.executor, env.halts, env.clock).process_pending()


# --- (a) 미접수 확정 ------------------------------------------------------------------------------

def test_a_not_accepted_recorded_processed_then_k3_needs_fresh_reconcile(
        env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    iid = _k3_escalated(env)
    assert confirm_unknown_main(_db(tmp_path) + [iid, "--not-accepted"]) == 0
    out = capsys.readouterr().out
    [cmd] = _commands(env)
    assert cmd["command_id"] in out and "다음 tick" in out
    assert cmd["channel"] == "LOCAL" and cmd["status"] == "PENDING"
    assert json.loads(cmd["args"]) == {"intent_id": iid, "accepted": False, "order_no": None}
    assert json.loads(cmd["args"])["accepted"] is False                   # 불리언 (문자열 아님)
    assert env.repo.intent(iid).state is IntentState.UNKNOWN              # 기록만, 판정은 본 프로세스
    [o] = _process(env)
    assert o.status == "DONE", o.result
    assert env.repo.intent(iid).state is IntentState.NOT_ACCEPTED
    assert env.repo.active_reservations(ACCOUNT) == []
    assert not env.halts.resume(HaltType.K3, ACCOUNT, Channel.LOCAL, "u").ok      # 확정 뒤 새 대조 전
    r = _rec(env).run(ACCOUNT, "PERIODIC")
    assert r.result == "MATCH"
    assert _k3_reconciled(env).get("recon") == r.recon_id
    assert env.halts.resume(HaltType.K3, ACCOUNT, Channel.LOCAL, "u").ok
    assert env.repo.active_halts(ACCOUNT) == []


# --- (b) 접수 확정 (늦게 보인 주문 채택) ----------------------------------------------------------

def test_b_accepted_with_order_no_adopts_then_resume_after_fresh_match(
        env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    iid = _invisible_accepted_entry(env)
    [order] = env.broker.query_orders_unchecked()
    assert _rec(env).run(ACCOUNT, "PERIODIC").result == "MATCH"           # 맡긴 주문 → K3 근거 아님
    assert confirm_unknown_main(_db(tmp_path) + [iid, "--accepted", "--order-no", order.order_no]) == 0
    capsys.readouterr()
    [cmd] = _commands(env)
    assert json.loads(cmd["args"]) == {"intent_id": iid, "accepted": True, "order_no": order.order_no}
    [o] = _process(env)
    assert o.status == "DONE", o.result
    assert env.repo.intent(iid).broker_order_key is not None
    assert not env.halts.resume(HaltType.K3, ACCOUNT, Channel.LOCAL, "u").ok
    r = _rec(env).run(ACCOUNT, "PERIODIC")
    assert r.result == "MATCH"
    assert env.halts.resume(HaltType.K3, ACCOUNT, Channel.LOCAL, "u").ok
    assert "K4" not in env.repo.active_halts(ACCOUNT)
    assert len(env.broker.sent) == 1


def test_b_accepted_without_order_no_is_refused(env: Env, tmp_path: Path,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    iid = _k3_escalated(env)
    assert confirm_unknown_main(_db(tmp_path) + [iid, "--accepted"]) == 2
    assert "--order-no" in capsys.readouterr().out
    assert _commands(env) == []


def test_b_order_no_with_not_accepted_is_refused(env: Env, tmp_path: Path,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    iid = _k3_escalated(env)
    assert confirm_unknown_main(_db(tmp_path) + [iid, "--not-accepted", "--order-no", "0000001"]) == 2
    assert "거부" in capsys.readouterr().out
    assert _commands(env) == []


@pytest.mark.parametrize("flags", [[], ["--accepted", "--not-accepted"]])
def test_b_exactly_one_of_accepted_or_not(env: Env, tmp_path: Path, flags: list[str]) -> None:
    iid = _k3_escalated(env)
    with pytest.raises(SystemExit) as exc:
        confirm_unknown_main(_db(tmp_path) + [iid] + flags + (["--order-no", "1"] if flags else []))
    assert exc.value.code == 2
    assert _commands(env) == []


# --- (c) LIVE 거부 -------------------------------------------------------------------------------

@pytest.mark.parametrize("extra", [["intent-x", "--not-accepted"], ["intent-x", "--accepted", "--order-no", "1"],
                                   ["--list"]])
def test_c_live_refused_before_db_open(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                       extra: list[str]) -> None:
    assert confirm_unknown_main(["--mode", "LIVE", "--root", str(tmp_path)] + extra) == 2
    out = capsys.readouterr().out
    assert out.startswith("거부") and "LIVE" in out
    assert not (tmp_path / "var").exists()                                # 저장소 경로를 만들지도 않음


# --- (d) 대상 검증 -------------------------------------------------------------------------------

def test_d_missing_intent_exit2_nothing_recorded(env: Env, tmp_path: Path,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    _k3_escalated(env)
    assert confirm_unknown_main(_db(tmp_path) + ["intent-does-not-exist", "--not-accepted"]) == 2
    out = capsys.readouterr().out
    assert "거부" in out and "intent-does-not-exist" in out
    assert _commands(env) == []
    assert env.repo.select("SELECT COUNT(*) AS n FROM ops_command")[0]["n"] == 0


def test_d_intent_not_unknown_exit2_nothing_recorded(env: Env, tmp_path: Path,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, evaluation="e1")))
    assert d.approved is not None
    env.executor.submit(d.approved)
    env.executor.process_events()
    state = env.repo.intent(d.approved.intent_id).state
    assert state is not IntentState.UNKNOWN
    assert confirm_unknown_main(_db(tmp_path) + [d.approved.intent_id, "--not-accepted"]) == 2
    out = capsys.readouterr().out
    assert "결과 불명이 아님" in out and state.value in out
    assert _commands(env) == []


def test_d_pending_confirm_for_same_intent_refused(env: Env, tmp_path: Path,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    iid = _k3_escalated(env)
    assert confirm_unknown_main(_db(tmp_path) + [iid, "--not-accepted"]) == 0
    assert confirm_unknown_main(_db(tmp_path) + [iid, "--not-accepted"]) == 2
    assert "이미" in capsys.readouterr().out
    assert len(_commands(env)) == 1


def test_d_missing_db_exit2_and_not_created(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "none" / "trading.db"
    assert confirm_unknown_main(["--mode", "PAPER", "--db", str(db), "intent-x", "--not-accepted"]) == 2
    assert "DB 없음" in capsys.readouterr().out
    assert not db.exists() and not db.parent.exists()
    assert confirm_unknown_main(["--mode", "PAPER", "--db", str(db), "--list"]) == 2
    assert not db.exists()


def test_d_mode_mismatch_exit2(env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    iid = _k3_escalated(env)
    assert confirm_unknown_main(["--mode", "PAPER", "--db", str(tmp_path / "trading.db"), iid,
                                 "--not-accepted"]) == 2
    assert "거부" in capsys.readouterr().out
    assert _commands(env) == []


def test_d_intent_id_required_without_list(env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert confirm_unknown_main(_db(tmp_path) + ["--not-accepted"]) == 2
    assert "의도 ID" in capsys.readouterr().out


# --- (e) --list ---------------------------------------------------------------------------------

def test_e_list_shows_unknown_intents_read_only(env: Env, tmp_path: Path,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    assert confirm_unknown_main(_db(tmp_path) + ["--list"]) == 0
    assert "결과 불명 의도: 0건" in capsys.readouterr().out
    iid = unknown_entry(env)                                              # 아직 K3 전 (조회 중)
    assert confirm_unknown_main(_db(tmp_path) + ["--list", "--account", ACCOUNT]) == 0
    out = capsys.readouterr().out
    assert "결과 불명 의도: 1건" in out and iid in out and "TST-A" in out and "BUY" in out and "100" in out
    assert "K3 아님" in out
    progress: dict[str, ResolveProgress] = {}
    engine_ticks(env, resolver(env), progress, 30)                        # → K3
    assert confirm_unknown_main(_db(tmp_path) + ["--list"]) == 0
    out = capsys.readouterr().out
    assert iid in out and "K3 활성" in out and "사람 확인 요청됨" in out
    assert "전송 10-01" in out                                            # 전송 시각 KST
    assert confirm_unknown_main(_db(tmp_path) + ["--list", "--account", "KR-OTHER"]) == 0
    assert "결과 불명 의도: 0건" in capsys.readouterr().out
    assert env.repo.select("SELECT COUNT(*) AS n FROM ops_command")[0]["n"] == 0   # 기록 없음


def test_e_list_ignores_confirm_args(env: Env, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        confirm_unknown_main(_db(tmp_path) + ["--list", "--not-accepted"])
    assert exc.value.code == 2


# --- 재검토 (M-1, L-1, L-3, L-4) -----------------------------------------------------------------

DIGIT_ID = "int-ab12345678cd"   # 8자리 숫자열 → 계좌번호 형태 가림(ACCOUNT_LIKE)에 걸리는 ID


def _digit_id_unknown(env: Env, monkeypatch: pytest.MonkeyPatch) -> str:
    import trading_system.gate.gate as gate_mod
    orig = gate_mod.new_id
    monkeypatch.setattr(gate_mod, "new_id", lambda prefix: DIGIT_ID if prefix == "int" else orig(prefix))
    iid = unknown_entry(env)
    assert iid == DIGIT_ID
    return iid


def test_m1_list_never_masks_intent_id(env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    """M-1: 목록의 의도 ID를 가리면 운영자가 가린 ID를 복사해 '의도 없음' → K3를 풀 수 없다."""
    iid = _digit_id_unknown(env, monkeypatch)
    engine_ticks(env, resolver(env), {}, 30)                              # → K3
    assert confirm_unknown_main(_db(tmp_path) + ["--list"]) == 0
    out = capsys.readouterr().out
    assert DIGIT_ID in out and "****" not in out
    assert confirm_unknown_main(_db(tmp_path) + [iid, "--not-accepted"]) == 0   # 목록의 ID 그대로 확정 가능
    capsys.readouterr()
    assert json.loads(_commands(env)[0]["args"])["intent_id"] == DIGIT_ID
    [o] = _process(env)
    assert o.status == "DONE" and env.repo.intent(iid).state is IntentState.NOT_ACCEPTED


def test_l1_order_no_is_stripped(env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    iid = _invisible_accepted_entry(env)
    [order] = env.broker.query_orders_unchecked()
    assert confirm_unknown_main(_db(tmp_path) + [iid, "--accepted", "--order-no", f"  {order.order_no} "]) == 0
    capsys.readouterr()
    [cmd] = _commands(env)
    assert json.loads(cmd["args"])["order_no"] == order.order_no
    [o] = _process(env)
    assert o.status == "DONE", o.result
    assert env.repo.intent(iid).broker_order_key is not None


def test_l1_blank_order_no_refused(env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    iid = _k3_escalated(env)
    assert confirm_unknown_main(_db(tmp_path) + [iid, "--accepted", "--order-no", "   "]) == 2
    assert "--order-no" in capsys.readouterr().out
    assert _commands(env) == []


def _closed_db(tmp_path: Path, mode: Mode = Mode.PAPER) -> Path:
    db = tmp_path / "ro" / "trading.db"
    Repository(db, mode).close()
    assert not db.with_name("trading.db-wal").exists() and not db.with_name("trading.db-shm").exists()
    return db


def test_l3_list_is_truly_read_only_no_wal_shm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    db = _closed_db(tmp_path)
    before = db.read_bytes()

    def no_writer(*a: object, **k: object) -> None:
        raise AssertionError("--list는 쓰기 저장소(Repository)를 열지 않는다")
    monkeypatch.setattr("trading_system.ops.cli.Repository", no_writer)
    assert confirm_unknown_main(["--mode", "PAPER", "--db", str(db), "--list"]) == 0
    assert "결과 불명 의도: 0건" in capsys.readouterr().out
    assert db.read_bytes() == before
    assert sorted(p.name for p in db.parent.iterdir()) == ["trading.db"]   # -wal·-shm 만들지 않음


def test_l3_list_mode_mismatch_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _closed_db(tmp_path, Mode.BACKTEST)
    assert confirm_unknown_main(["--mode", "PAPER", "--db", str(db), "--list"]) == 2
    out = capsys.readouterr().out
    assert out.startswith("거부") and "BACKTEST" in out


def test_l3_list_no_schema_creation_on_foreign_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "other.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE unrelated(a)")
    c.commit()
    c.close()
    assert confirm_unknown_main(["--mode", "PAPER", "--db", str(db), "--list"]) == 2
    assert capsys.readouterr().out.startswith("거부")
    c = sqlite3.connect(str(db))
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    c.close()
    assert tables == ["unrelated"]                                        # 스키마·모드 기록 없음


def test_l3_list_unreadable_file_exit2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "trading.db"
    db.write_bytes(b"not a database file at all" * 100)
    assert confirm_unknown_main(["--mode", "PAPER", "--db", str(db), "--list"]) == 2
    assert capsys.readouterr().out.startswith("거부")


def test_l3_list_while_writer_open_sees_committed_rows(env: Env, tmp_path: Path,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    iid = unknown_entry(env)                                              # env.repo 연결이 열린 채(WAL 사용 중)
    assert confirm_unknown_main(_db(tmp_path) + ["--list"]) == 0
    assert iid in capsys.readouterr().out


def test_l4_confirm_before_escalation_warns_but_records(env: Env, tmp_path: Path,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    iid = unknown_entry(env)                                              # 자동 조회 해소 중 (K3 전)
    assert confirm_unknown_main(_db(tmp_path) + [iid, "--not-accepted"]) == 0
    out = capsys.readouterr().out
    assert "주의" in out and "조회 해소 중" in out
    assert len(_commands(env)) == 1


def test_l4_no_warning_after_escalation(env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    iid = _k3_escalated(env)
    assert confirm_unknown_main(_db(tmp_path) + [iid, "--not-accepted"]) == 0
    assert "주의" not in capsys.readouterr().out


# --- 래퍼 ----------------------------------------------------------------------------------------

def test_wrapper_script_help_runs() -> None:
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "confirm-unknown.py"), "--help"],
                       capture_output=True, text=True, encoding="utf-8", timeout=60,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert r.returncode == 0, r.stderr
    assert "결과 불명" in r.stdout and "--not-accepted" in r.stdout and "--order-no" in r.stdout
