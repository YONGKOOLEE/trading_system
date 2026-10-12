"""CP-025 §7.4·§8.4 검증 항목 중 대응 시험이 없던 부분 (04 §6 "확인 필요 [CP-025]" 행).

- V-171: 채널 장애 401 / 403 / 타임아웃 연속 3회 → K11, 대기열 유지(복구 시 전송, V-30)  (03 D-128)
- V-173: 429 retry_after 없음(·형식 오류) → 5초 대기, 그동안 전송 0, 연속 실패 수 불변, K11 없음  (03 D-129)
- V-177(c): 저장 실패가 아닌 예외 → 건너뜀(offset 진행), 기록  (03 D-131)
- V-179: app/main 시작 경로에서 배포 검사·파라미터 로드 거부 → 시작 거부(설정 오류), 사유 표시  (03 D-134)
- V-180: v1 → v2 교체 — 보유 중이면 소유권 승계 없음(00 D-011), 무보유면 v2 등록·판정 정상  (03 D-135)

네트워크 없음: 로컬 가짜 텔레그램 서버(test_t013_telegram.FakeTelegram)·fake 브로커·가짜 시계만 쓴다.
토큰·채팅·계좌 별칭·승인 A-ID는 기존 시험의 가상값이다(A-9xx는 가상 승인).
"""

from __future__ import annotations

import sqlite3
import time as _time
from datetime import timedelta
from pathlib import Path

import pytest

import trading_system.ops.telegram as telegram_mod
from tests.scenario.conftest import Env
from tests.scenario.test_e2e_paper0_days import (
    ACCOUNT as PAPER_ACCOUNT, CODE, DAYS, PaperWorld, buy_sell_of_day, kst, run_trading_day, start_and_resume,
)
from tests.scenario.test_t013_telegram import CHAT, FakeTelegram, api, bot, tg  # noqa: F401  (fixture)
from tests.scenario.test_t017_telegram import alert, halts, notifier
from trading_system.app.main import run_main
from trading_system.app.strategy_tool import strategy_main
from trading_system.app.telegram_run import _guarded
from trading_system.domain.enums import Mode
from trading_system.ops.telegram import Notifier
from trading_system.storage.repository import Repository, StorageFailure

ROOT = Path(__file__).resolve().parents[2]


def _send_paths(t: FakeTelegram) -> int:
    return sum(1 for p in t.paths if p.endswith("/sendMessage"))


# --- V-171 채널 장애 401 / 403 / 타임아웃 → K11 ---------------------------------------------------------

def _assert_k11_then_recovers(env: Env, t: FakeTelegram, n: Notifier, heal) -> None:  # type: ignore[no-untyped-def]
    for _ in range(3):
        n.pump()
    assert env.repo.notification_fail_streak() == 3
    assert "K11" in halts(env)
    assert len(env.repo.pending_notifications()) == 1          # 버리지 않는다 (V-30)
    assert not any(e["kind"] == "NOTIFY_DROPPED" for e in env.repo.audit_events())
    heal()
    sent, failed = n.pump()
    assert sent >= 1 and failed == 0                            # 밀린 알림 + K11 발동 알림(정지가 남긴 ALERT)
    assert t.sent[0]["text"] == "[M1] [경고] a"                  # [M1]: T-M2-13 D-117 / 복구 시 밀린 알림 전송, 순번 순 (V-30)
    assert env.repo.pending_notifications() == []
    assert env.repo.notification_fail_streak() == 0


@pytest.mark.parametrize("code,desc", [
    (401, "Unauthorized"),
    (403, "Forbidden: bot was blocked by the user"),
])
def test_v171_auth_errors_are_channel_failures_k11(env: Env, tg: FakeTelegram, code: int, desc: str) -> None:  # noqa: F811
    broken = {"on": True}
    tg.send_reply = lambda body: (code, {"ok": False, "error_code": code, "description": desc}) if broken["on"] else None
    alert(env, "a")
    _assert_k11_then_recovers(env, tg, notifier(env, tg), lambda: broken.update(on=False))


def test_v171_wrong_token_401_from_server_is_channel_failure_k11(env: Env, tg: FakeTelegram) -> None:  # noqa: F811
    """가짜 서버의 실제 401 경로(경로의 토큰 불일치). 복구 = 올바른 토큰의 전송기."""
    alert(env, "a")
    n_bad = Notifier(env.repo, api(tg, token="999:virtual-wrong"), CHAT, env.clock, env.halts, sleep=lambda s: None)
    for _ in range(3):
        n_bad.pump()
    assert "K11" in halts(env) and len(env.repo.pending_notifications()) == 1
    assert tg.sent == []
    sent, _ = notifier(env, tg).pump()
    assert sent >= 1 and tg.sent[0]["text"] == "[M1] [경고] a"  # [M1] 표시: T-M2-13 D-117 A-065 Q5
    assert env.repo.pending_notifications() == []


def test_v171_timeout_is_channel_failure_k11(env: Env, tg: FakeTelegram, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(telegram_mod, "READ_TIMEOUT", 0.2)     # 응답 대기 시간만 줄인다(판정 경로는 그대로)
    slow = {"on": True}

    def reply(body: dict):  # type: ignore[no-untyped-def]
        if not slow["on"]:
            return None
        _time.sleep(0.8)                                       # 읽기 타임아웃보다 늦게 응답 → 클라이언트는 타임아웃
        return 502, {"ok": False, "description": "late"}       # (이미 끊긴 연결 — sent에 넣지 않음)
    tg.send_reply = reply
    alert(env, "a")
    _assert_k11_then_recovers(env, tg, notifier(env, tg), lambda: slow.update(on=False))


# --- V-173 429 retry_after 없음 → 5초 ---------------------------------------------------------------

@pytest.mark.parametrize("params", [None, {}, {"retry_after": "abc"}], ids=["no-parameters", "no-retry_after",
                                                                         "malformed"])
def test_v173_429_without_retry_after_waits_5s(env: Env, tg: FakeTelegram, params: dict | None) -> None:  # noqa: F811
    limited = {"on": True}
    body429: dict = {"ok": False, "error_code": 429, "description": "Too Many Requests"}
    if params is not None:
        body429["parameters"] = params
    tg.send_reply = lambda body: (429, body429) if limited["on"] else None
    alert(env, "x")
    n = notifier(env, tg)
    assert n.pump() == (0, 1)
    assert _send_paths(tg) == 1
    limited["on"] = False                                      # 서버는 이미 풀렸어도 5초 동안은 보내지 않는다
    for _ in range(4):
        env.clock.advance(timedelta(seconds=1))                # 1~4초
        assert n.pump() == (0, 0)
    assert _send_paths(tg) == 1                                # 대기 중 전송 0
    assert env.repo.notification_fail_streak() == 0 and "K11" not in halts(env)
    env.clock.advance(timedelta(seconds=1))                    # 5초
    assert n.pump() == (1, 0)
    assert [m["text"] for m in tg.sent] == ["[M1] [경고] x"]  # [M1] 표시: T-M2-13 D-117 A-065 Q5
    assert env.repo.notification_fail_streak() == 0 and "K11" not in halts(env)


# --- V-177(c) 저장 실패가 아닌 예외 → 건너뜀·기록 -----------------------------------------------------

def test_v177c_non_storage_exception_skips_update_and_is_logged(env: Env, tg: FakeTelegram,  # noqa: F811
                                                                 monkeypatch: pytest.MonkeyPatch,
                                                                 tmp_path: Path) -> None:
    b = bot(env, tg)

    def defect(*a, **k):  # type: ignore[no-untyped-def]
        raise KeyError("가상 결함")                            # StorageFailure가 아닌 예외
    monkeypatch.setattr(env.repo, "insert_command", defect)
    tg.push(1, "/kill 결함 경로")
    log_dir = tmp_path / "logs"
    assert _guarded(lambda: b.poll_once(timeout=0), "telegram.poll", log_dir) is False   # 폴링 스레드는 죽지 않음
    monkeypatch.undo()
    tg.push(2, "/status")
    assert b.poll_once(timeout=0) == 1                         # update 1은 다시 처리하지 않는다(offset 진행)
    assert env.repo.pending_commands() == []                   # /kill은 재처리되지 않음 (건너뜀)
    assert tg.sent[-1]["text"].startswith("[M1] 정지")         # [M1]: T-M2-13 D-117 / 뒤 update(/status)는 정상 처리
    getup = [p for p in tg.paths if p.endswith("/getUpdates")]
    assert len(getup) == 2
    logged = "".join(f.read_text(encoding="utf-8") for f in log_dir.rglob("*") if f.is_file())
    assert "EXCEPTION" in logged and "telegram.poll" in logged and "KeyError" in logged   # 기록


def test_v177c_skip_only_on_non_storage_failure(env: Env, tg: FakeTelegram,  # noqa: F811
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """대조: 저장 실패(StorageFailure)는 건너뛰지 않고 다음 폴링에서 다시 처리한다(V-175) — (c)와 구분."""
    b = bot(env, tg)
    real = env.repo.insert_command
    calls = {"n": 0}

    def flaky(*a, **k):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)
    monkeypatch.setattr(env.repo, "insert_command", flaky)
    tg.push(1, "/kill")
    with pytest.raises(StorageFailure):  # 저장소가 StorageFailure로 감싼다 — V-177(c)(그 밖 예외)와 대조
        b.poll_once(timeout=0)
    b.poll_once(timeout=0)
    assert [c["command"] for c in env.repo.pending_commands()] == ["KILL"]


# --- V-179 시작 경로(app/main) 거부 -----------------------------------------------------------------

V2_APP_STRATEGY = """[[strategies]]
module = "trading_system.strategy.paper0_verify:Paper0VerifyV2"
params = "config/strategies/paper0-verify-v2.toml"
eval_times = [{times}]
"""


def _v2_root(tmp: Path, split: str = "12:00", hold_days: int = 0, times: str = '"09:30", "14:30"') -> Path:
    from tests.scenario.test_t020_main import make_root
    root = make_root(tmp, Mode.BACKTEST)
    app = root / "config" / "app.backtest.toml"
    text = app.read_text(encoding="utf-8")
    head = text[:text.index("[[strategies]]")]
    app.write_text(head + V2_APP_STRATEGY.format(times=times), encoding="utf-8")
    (root / "config" / "strategies" / "paper0-verify-v2.toml").write_text(
        f'strategy = "paper0-verify"\nversion = 2\n[params]\nqty = 1\nhold_days = {hold_days}\n'
        f'split_time = "{split}"\n', encoding="utf-8")
    return root


def test_v179_main_starts_with_valid_v2_deploy(tmp_path: Path) -> None:
    """대조군: 올바른 v2 배포 설정이면 시작·등록된다 (거부 시험이 다른 원인으로 거부되는 것이 아님을 확인)."""
    root = _v2_root(tmp_path)
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.2) == 0
    repo = Repository(root / "var" / "backtest" / "trading.db", Mode.BACKTEST)
    try:
        assert repo.strategy_state("paper0-verify", 2, "M1") is not None
    finally:
        repo.close()


@pytest.mark.parametrize("kw,reason", [
    ({"split": "12"}, "split_time"),
    ({"split": "1200"}, "split_time"),
    ({"split": "25:00"}, "split_time"),
    ({"split": "16:00"}, "전·후"),                              # 판정 시각이 모두 split 전
    ({"split": "09:00"}, "전·후"),                              # 판정 시각이 모두 split 후
    ({"split": "14:30"}, "판정 시각과 같음"),                    # 판정 시각 = split_time
    ({"hold_days": 1}, "hold_days"),                           # 파라미터 로드 거부
], ids=["split-12", "split-1200", "split-25:00", "all-before", "all-after", "equal", "hold_days-1"])
def test_v179_main_refuses_start_on_bad_v2_deploy(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                                  kw: dict, reason: str) -> None:
    root = _v2_root(tmp_path, **kw)
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.2) == 2
    out = capsys.readouterr().out
    assert "시작 거부" in out and reason in out, out           # 설정 오류로 시작 거부, 사유 표시
    assert not (root / "var" / "backtest" / "heartbeat").exists()   # 엔진은 돌지 않았다
    db = root / "var" / "backtest" / "trading.db"
    if db.exists():
        repo = Repository(db, Mode.BACKTEST)
        try:
            assert repo.strategy_state("paper0-verify", 2, "M1") is None   # 등록·상태 생성 전 거부
        finally:
            repo.close()


# --- V-180 v1 → v2 교체 ----------------------------------------------------------------------------

V2_PAPER_STRATEGY = """[[strategies]]
module = "trading_system.strategy.paper0_verify:Paper0VerifyV2"
params = "config/strategies/paper0-verify-v2.toml"
eval_times = ["09:30", "14:30"]
verification_approval = "A-067"
"""


def _switch_to_v2(w: PaperWorld) -> None:
    """00 §2.11 교체 절차를 사용자 명령 순서대로 (D-135): 프로세스 정지 → 배포 설정 v2로 → v2 등록 → v2 백테스트 통과·PAPER
    → v1 중지(교체) → 재시작 → K8 재개. 승인 A-ID는 가상(A-9xx), 검증 승인은 배포 설정의 A-067."""
    root = w.root
    w.repo.close()
    (root / "config" / "strategies" / "paper0-verify-v2.toml").write_text(
        (ROOT / "config" / "strategies" / "paper0-verify-v2.toml").read_text(encoding="utf-8"), encoding="utf-8")
    app = root / "config" / "app.paper.toml"
    text = app.read_text(encoding="utf-8")
    app.write_text(text[:text.index("[[strategies]]")] + V2_PAPER_STRATEGY, encoding="utf-8")
    base = ["--mode", "PAPER", "--root", str(root)]
    assert strategy_main(["register", *base]) == 0
    ev = root / "var" / "backtest" / "paper0-verify-v2" / "report.md"
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text("가상 증거 (시험용)\n", encoding="utf-8")
    assert strategy_main(["transition", *base, "paper0-verify", "2", "BACKTEST_PASSED", "--approval", "A-921",
                          "--evidence", "var/backtest/paper0-verify-v2/report.md"]) == 0
    assert strategy_main(["transition", *base, "paper0-verify", "2", "PAPER", "--approval", "A-922"]) == 0
    assert strategy_main(["transition", *base, "paper0-verify", "1", "STOPPED", "--approval", "A-923",
                          "--stop-reason", "REPLACE"]) == 0
    w.build()
    assert w.engine.start().ok
    w.command("RESUME", {"account": None, "halt_type": "K8"})
    w.step()
    assert "K8" not in w.active(), w.active()


def _owners_of_intents(w: PaperWorld) -> list[tuple[str, str]]:
    return [(r["owner"], r["request_type"]) for r in w.repo.select(
        "SELECT owner, request_type FROM intent ORDER BY created_seq")]


def test_v180_switch_while_v1_holds_does_not_transfer_ownership(tmp_path: Path) -> None:
    """보유 중 교체: v1 소유 1주는 v2로 넘어가지 않는다(00 D-011). v2는 그 보유를 청산하지도, 그 종목에 진입하지도 않는다
    → 보유가 v1 소유로 남는다(교체 절차 미충족 — 사람이 정리해야 하는 상태).
    교체 뒤 v1 판정이 돌지 않는 것은 배포 설정에서 v1이 빠졌기 때문이며(01 K9 "교체"는 v1 감액·청산 신호 허용), 이 상태를
    시스템이 거부·경고해야 하는지는 결정 필요(재검토 #4) — 그래서 "교체 뒤 주문 0"은 단언하지 않는다."""
    w = PaperWorld(tmp_path)
    try:
        start_and_resume(w)
        w.run_until(kst(DAYS[0], 9, 31))
        assert w.position() == 1
        assert w.repo.active_ownership(PAPER_ACCOUNT, CODE).owner == "paper0-verify:1"
        _switch_to_v2(w)
        w.run_until(kst(DAYS[0], 14, 31))                      # v2 오후 판정: v1 소유 → 청산 없음
        w.run_until(kst(DAYS[1], 14, 31))                      # 다음 날 오전(보유라 진입 없음)·오후(소유 아님 → 청산 없음)
        assert w.position() == 1
        own = w.repo.active_ownership(PAPER_ACCOUNT, CODE)
        assert own is not None and own.owner == "paper0-verify:1"   # 소유권 승계 없음
        assert [o for o, _ in _owners_of_intents(w)] == ["paper0-verify:1"]   # v2 의도 0건
        v2_evals = [e for e in w.evaluations() if e["kind"] == "EVALUATION" and
                    e["s"].get("strategy") == "paper0-verify:2"]
        assert v2_evals and all(e["s"]["signals"] == 0 for e in v2_evals)
        rows = {(r["strategy_id"], r["version"]): r for r in w.repo.strategy_states()}
        assert rows[("paper0-verify", 1)]["state"] == "STOPPED" and rows[("paper0-verify", 2)]["state"] == "PAPER"
    finally:
        w.close()


def test_v180_v1_cannot_be_retired_while_it_still_owns_the_holding(tmp_path: Path,
                                                                   capsys: pytest.CaptureFixture[str]) -> None:
    """보유 중 교체의 정리 경로: v1 소유 보유가 남아 있으면 v1 폐기도 거부된다(00 §2.11 4, 사람 정리 필요)."""
    w = PaperWorld(tmp_path)
    try:
        start_and_resume(w)
        w.run_until(kst(DAYS[0], 9, 31))
        assert w.position() == 1
        _switch_to_v2(w)
        w.repo.close()
        rc = strategy_main(["transition", "--mode", "PAPER", "--root", str(w.root), "paper0-verify", "1", "RETIRED",
                            "--approval", "A-924"])
        assert rc == 1 and "소유" in capsys.readouterr().out
        w.build()
    finally:
        w.close()


def test_v180_switch_when_flat_v2_registers_and_trades_normally(tmp_path: Path) -> None:
    """무보유 교체: v1이 같은 날 왕복을 마친 뒤 교체 → 다음 거래일 v2가 09:30 진입·14:30 청산 (D-133)."""
    w = PaperWorld(tmp_path)
    try:
        start_and_resume(w)
        run_trading_day(w, DAYS[0])                            # v1: 매수·매도, 무보유로 끝남
        w.run_until(kst(DAYS[0], 16, 1))
        assert w.position() == 0 and w.repo.active_ownership(PAPER_ACCOUNT, CODE) is None
        w.run_until(kst(DAYS[1], 8, 30))
        _switch_to_v2(w)
        w.run_until(kst(DAYS[1], 9, 31))
        assert buy_sell_of_day(w, DAYS[1]) == [("ENTRY", "FILLED")]
        assert w.repo.active_ownership(PAPER_ACCOUNT, CODE).owner == "paper0-verify:2"
        w.run_until(kst(DAYS[1], 14, 31))
        assert buy_sell_of_day(w, DAYS[1]) == [("ENTRY", "FILLED"), ("EXIT", "FILLED")]
        assert w.position() == 0
        assert _owners_of_intents(w)[-2:] == [("paper0-verify:2", "ENTRY"), ("paper0-verify:2", "EXIT")]
        evs = [e for e in w.evaluations() if e["s"].get("strategy") == "paper0-verify:2"]
        assert [(e["kind"], e["s"]["signals"]) for e in evs] == [("EVALUATION", 1), ("EVALUATION", 1)]
    finally:
        w.close()
