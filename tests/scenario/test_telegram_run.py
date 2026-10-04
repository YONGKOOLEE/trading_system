"""텔레그램 run 연결 (T-013 후속, 사용자 지시 2026-10-04 "텔레그램 구축 오늘"): 설정 [telegram] enabled,
.env.paper TELEGRAM_BOT_TOKEN·TELEGRAM_CHAT_ID, 알림 전송·명령 수신 스레드, 첫 연결 시 지난 알림 건너뜀.

네트워크는 로컬 가짜 텔레그램 서버(test_t013_telegram.FakeTelegram)만 쓴다.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.scenario.test_e2e_paper0_days import PaperWorld, start_and_resume
from tests.scenario.test_t013_telegram import CHAT, TOKEN, FakeTelegram, tg  # noqa: F401 (fixture)
from trading_system.app.telegram_run import telegram_threads
from trading_system.config.app_file import parse_app_settings
from trading_system.config.envfile import Secret, SecretStore
from trading_system.config.errors import ConfigError
from trading_system.domain.enums import Mode

ROOT = Path(__file__).resolve().parents[2]


def _raw(**telegram: object) -> dict:
    raw = {"app": {"account": "KR-PAPER-01", "loop_seconds": "0.5", "reconcile_minutes": 1,
                   "sntp_servers": ["time.example:123"]},
           "paper": {"connection_approved": True}}
    if telegram:
        raw["telegram"] = telegram
    return raw


# --- 설정 -------------------------------------------------------------------------------------------

def test_telegram_disabled_by_default() -> None:
    assert parse_app_settings(_raw(), Mode.PAPER, ROOT).telegram_enabled is False


def test_telegram_enabled_true_parsed() -> None:
    assert parse_app_settings(_raw(enabled=True), Mode.PAPER, ROOT).telegram_enabled is True


@pytest.mark.parametrize("bad", [{"enabled": "true"}, {"enabled": True, "token": "x"}])
def test_telegram_bad_config_refused(bad: dict) -> None:
    with pytest.raises(ConfigError):
        parse_app_settings(_raw(**bad), Mode.PAPER, ROOT)


def test_telegram_not_allowed_in_backtest() -> None:
    raw = _raw(enabled=True)
    del raw["paper"]
    with pytest.raises(ConfigError):
        parse_app_settings(raw, Mode.BACKTEST, ROOT)


# --- 첫 연결: 지난 알림 건너뜀 ---------------------------------------------------------------------

@pytest.fixture
def world(tmp_path: Path) -> Iterator[PaperWorld]:
    w = PaperWorld(tmp_path)
    try:
        start_and_resume(w)
        yield w
    finally:
        w.close()


def test_first_enable_skips_old_alerts_then_new_ones_queue(world: PaperWorld) -> None:
    repo = world.repo
    old = repo.connection.execute("SELECT COUNT(*) FROM audit_event WHERE kind='ALERT'").fetchone()[0]
    assert old > 0                                         # 시작 K8 알림 등
    with repo.tx():
        assert repo.skip_old_alerts_on_first_enable() == old
        assert repo.enqueue_new_alerts() == 0
    world.engine.alerts.alert("WARNING", "새 알림", None)
    with repo.tx():
        assert repo.enqueue_new_alerts() == 1
        assert repo.skip_old_alerts_on_first_enable() == 0   # 이미 대기열이 있으면 건너뛰지 않음(밀린 알림은 보냄)


# --- 스레드 연결 -------------------------------------------------------------------------------------

def _secrets(**kv: str) -> SecretStore:
    return SecretStore({k: Secret(v) for k, v in kv.items()})


def _wait(cond, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_disabled_makes_no_threads(world: PaperWorld) -> None:
    assert telegram_threads(world.engine, world.repo, False, None, world.clock, world.root / "KILL",
                            threading.Event()) == []


def test_enabled_without_secrets_refuses_start(world: PaperWorld) -> None:
    with pytest.raises(ConfigError) as e:
        telegram_threads(world.engine, world.repo, True, _secrets(TELEGRAM_BOT_TOKEN=TOKEN), world.clock,
                         world.root / "KILL", threading.Event())
    assert "TELEGRAM_CHAT_ID" in str(e.value) and TOKEN not in str(e.value)


def test_enabled_sends_new_alerts_and_accepts_kill(world: PaperWorld, tg: FakeTelegram) -> None:  # noqa: F811
    stop = threading.Event()
    threads = telegram_threads(world.engine, world.repo, True,
                               _secrets(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT), world.clock,
                               world.root / "KILL", stop, base_url=tg.url, notify_every=0.05, poll_timeout=0)
    assert {t.name for t in threads} == {"telegram-notify", "telegram-poll"}
    for t in threads:
        t.start()
    try:
        world.engine.alerts.alert("WARNING", "연결 시험 알림", None)
        assert _wait(lambda: any("연결 시험 알림" in m["text"] for m in tg.sent))
        assert not any("K8" in m["text"] for m in tg.sent)          # 연결 전 지난 알림은 보내지 않음
        tg.push(1, "/kill 원격 시험")
        assert _wait(lambda: any(r["command"] == "KILL" for r in
                                 world.repo.connection.execute("SELECT command FROM ops_command")))
    finally:
        stop.set()
        for t in threads:
            t.join(5)
    assert not any(t.is_alive() for t in threads)


# --- run: 켰는데 비밀값이 없으면 브로커 접속 전에 시작 거부 ---------------------------------------------

def test_run_refuses_before_broker_when_telegram_secrets_missing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from tests.scenario.test_run_rehearsal import make_root
    from trading_system.app import main as app_main

    root = make_root(tmp_path, 9)                       # 가상 .env.paper(텔레그램 키 없음)
    app = root / "config" / "app.paper.toml"
    app.write_text(app.read_text(encoding="utf-8") + "\n[telegram]\nenabled = true\n", encoding="utf-8")

    def no_broker(*a: object, **k: object) -> object:
        raise AssertionError("시작 거부 전에 브로커 접속을 시도함")

    monkeypatch.setattr(app_main, "_paper_broker", no_broker)
    assert app_main.run_main(root, environ={"TRADE_MODE": "PAPER"}) == 2
    out = capsys.readouterr().out
    assert "시작 거부(브로커 연결 없음)" in out and "TELEGRAM_BOT_TOKEN" in out


# --- 연결 시험 도구 (사람 실행) ---------------------------------------------------------------------------

def _env_root(tmp_path: Path, text: str) -> Path:
    (tmp_path / ".env.paper").write_text(text, encoding="utf-8")
    return tmp_path


def test_telegram_test_tool_sends_one_message(tmp_path: Path, tg: FakeTelegram,  # noqa: F811
                                              capsys: pytest.CaptureFixture[str]) -> None:
    from trading_system.app.telegram_tool import telegram_test_main
    root = _env_root(tmp_path, f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID={CHAT}\n")
    assert telegram_test_main(["--mode", "PAPER", "--root", str(root)], base_url=tg.url) == 0
    assert len(tg.sent) == 1 and tg.sent[0]["chat_id"] == CHAT and "연결 시험" in tg.sent[0]["text"]
    assert TOKEN not in capsys.readouterr().out


def test_telegram_test_tool_missing_key_and_bad_token(tmp_path: Path, tg: FakeTelegram,  # noqa: F811
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    from trading_system.app.telegram_tool import telegram_test_main
    root = _env_root(tmp_path, f"TELEGRAM_BOT_TOKEN={TOKEN}\n")
    assert telegram_test_main(["--mode", "PAPER", "--root", str(root)], base_url=tg.url) == 2
    root = _env_root(tmp_path, f"TELEGRAM_BOT_TOKEN=999:wrong-token\nTELEGRAM_CHAT_ID={CHAT}\n")
    assert telegram_test_main(["--mode", "PAPER", "--root", str(root)], base_url=tg.url) == 1
    out = capsys.readouterr().out
    assert "wrong-token" not in out and tg.sent == []
