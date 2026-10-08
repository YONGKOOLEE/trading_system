"""운영 낮음 항목 정리 (T-025 재검토 잔여, docs/handoff.md "진행 중", 독립 재검토 2026-10-08·09).

1. 다음 해 달력 경고 — 12월 매 거래일 반복하지 않고 텔레그램은 7일에 1회(마지막 전송일을 var/paper 상태 파일에 기록).
   시작 점검(preflight) 콘솔 출력은 매번 보여도 된다.
2. 자동 운영 거부 알림 — 요청 모드가 PAPER가 아니면(LIVE 등) 로그만(PAPER 텔레그램 채널을 쓰지 않음). PAPER 설정 거부는
   config/app.paper.toml [telegram] enabled = true를 읽을 수 있을 때만 보낸다. 텔레그램 문구에 파일 시스템 경로를 넣지 않는다
   (일 로그에는 남김).
3. 자동 운영 텔레그램 429·연결 실패 — 메시지 1건에 쓰는 시간 상한 30초(Retry-After는 상한 안에서만 따름), 넘으면 fallback
   파일 + 종료 전 재전송 큐.
4. paper-review — 손상된 DAILY_BASE 행은 "손상"으로 표시하고 집계를 계속한다(domain.daily_base 사용).
5. run 시작 뒤 특수 예외(KeyboardInterrupt 등)로 자동 운영이 끝나면, 이 자동 운영이 띄운 run이 실행 잠금을 쥐고 있을 때
   정지 요청 파일(ops-stop과 같은 파일)을 최선 노력으로 쓴 뒤 코드 3.
6. 미래 시각 하트비트 — status·preflight·자동 운영 감시가 모두 "시계 이상 의심"으로 보인다.

모든 값은 가상이다. 실제 네트워크·자격정보 없음(텔레그램은 로컬 가짜 서버 또는 가짜 API).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tests.scenario.test_a084_ops_hardening import (FAKE_APP_KEY, FakeApi, _day_log, _secrets_root,  # noqa: F401
                                                    _stub_main_setup)
from tests.scenario.test_t013_telegram import CHAT, TOKEN, FakeTelegram, tg  # noqa: F401 — 픽스처 재사용
from tests.scenario.test_t025_autopilot import Rig, kst
from trading_system.app.autopilot import (EXIT_ABORTED, EXIT_REFUSED, Autopilot, TelegramNotify, autopilot_main)
from trading_system.app.instance_lock import acquire_instance_lock
from trading_system.app.main import STOP_REQUEST
from trading_system.domain.clock import KST
from trading_system.domain.enums import Mode
from trading_system.ops.telegram import TelegramError
from trading_system.reporting.paper_review import render_markdown, review
from trading_system.storage.repository import Repository

# --- 1. 다음 해 달력 경고: 텔레그램 7일에 1회 ------------------------------------------------------------------


def _december_trading_days() -> list[date]:
    d, out = date(2026, 12, 2), []
    while d <= date(2026, 12, 31):
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_calendar_warning_sent_at_most_once_per_7_days_through_december(tmp_path: Path) -> None:
    r = Rig(tmp_path, start=kst(6, 45, d=date(2026, 12, 2)), preflight_rc=1)   # 시작 점검 실패로 빨리 끝나는 하루
    sent_days: list[date] = []
    for day in _december_trading_days():
        r.clock._now = kst(6, 45, d=day)  # noqa: SLF001
        before = len(r.notes)
        assert r.ap.run() == EXIT_REFUSED
        if any(lv == "WARNING" and "다음 해 달력 없음" in t for lv, t in r.notes[before:]):
            sent_days.append(day)
    assert sent_days == [date(2026, 12, 2), date(2026, 12, 9), date(2026, 12, 16), date(2026, 12, 23),
                         date(2026, 12, 30)], sent_days
    for a, b in zip(sent_days, sent_days[1:]):
        assert (b - a).days >= 7
    # 보내지 않은 날도 일 로그에는 남긴다(매일 확인 가능)
    assert sum("다음 해 달력 없음" in m for m in r.logs) == len(_december_trading_days()), r.logs


def test_calendar_warning_state_unreadable_or_future_sends_again(tmp_path: Path) -> None:
    r = Rig(tmp_path, start=kst(6, 45, d=date(2026, 12, 2)), preflight_rc=1)
    assert r.ap.run() == EXIT_REFUSED
    state = [p for p in r.base.iterdir() if "calendar" in p.name]
    assert len(state) == 1, list(r.base.iterdir())                         # var/paper 상태 파일 1개
    state[0].write_text("깨짐", encoding="utf-8")
    r.clock._now = kst(6, 45, d=date(2026, 12, 3))  # noqa: SLF001
    before = len(r.notes)
    r.ap.run()
    assert any("다음 해 달력 없음" in t for _, t in r.notes[before:])        # 상태를 못 읽으면 보낸다(보수적)
    state[0].write_text("2027-06-01", encoding="utf-8")                     # 미래 날짜(시계 이상) — 영구 억제 안 함
    r.clock._now = kst(6, 45, d=date(2026, 12, 4))  # noqa: SLF001
    before = len(r.notes)
    r.ap.run()
    assert any("다음 해 달력 없음" in t for _, t in r.notes[before:])


# --- 2. 거부 알림: LIVE 등은 로그만, PAPER는 [telegram] enabled일 때만, 경로 없음 -----------------------------------

def _app_toml(root: Path, enabled: bool | None) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    if enabled is not None:
        (root / "config" / "app.paper.toml").write_text(
            f"[telegram]\nenabled = {'true' if enabled else 'false'}\n", encoding="utf-8")


@pytest.mark.parametrize("mode", ["LIVE", "BACKTEST", "paper"])
def test_non_paper_mode_refusal_never_uses_paper_telegram(tmp_path: Path, tg: FakeTelegram, mode: str) -> None:
    _secrets_root(tmp_path)
    _app_toml(tmp_path, True)
    assert autopilot_main(["--mode", mode, "--root", str(tmp_path)], environ={},
                          telegram_base_url=tg.url) == EXIT_REFUSED
    assert tg.sent == [] and tg.paths == []                                  # PAPER 채널에 보내지 않음
    text = _day_log(tmp_path)
    assert "자동 운영 거부" in text and "PAPER 전용" in text, text
    assert "텔레그램" in text and "로그만" in text, text


def test_paper_mode_refusal_notifies_when_telegram_enabled(tmp_path: Path, tg: FakeTelegram) -> None:
    _secrets_root(tmp_path)
    _app_toml(tmp_path, True)
    assert autopilot_main(["--mode", "PAPER", "--root", str(tmp_path)], environ={"TRADE_MODE": "LIVE"},
                          telegram_base_url=tg.url) == EXIT_REFUSED
    assert len(tg.sent) == 1 and tg.sent[0]["text"].startswith("[경고] [자동 운영] 자동 운영 거부"), tg.sent
    assert "TRADE_MODE" in tg.sent[0]["text"]


def test_paper_refusal_with_telegram_explicitly_disabled_logs_only(tmp_path: Path, tg: FakeTelegram) -> None:
    _secrets_root(tmp_path)
    _app_toml(tmp_path, False)                                               # 읽히고 명시적으로 enabled = false
    assert autopilot_main(["--mode", "PAPER", "--root", str(tmp_path)], environ={"TRADE_MODE": "LIVE"},
                          telegram_base_url=tg.url) == EXIT_REFUSED
    assert tg.sent == [] and tg.paths == []
    text = _day_log(tmp_path)
    assert "자동 운영 거부" in text and "로그만" in text, text


# 재검토(c7e16da 후속, A-084 위임 결정 L-2): 무인 운영에서 침묵이 더 나쁘다 — 설정을 못 읽거나 깨졌으면 PAPER 거부
# 알림을 그대로 보낸다(fail-loud). 로그만은 "읽히고 명시적으로 enabled = false"일 때뿐.
@pytest.mark.parametrize("content", [
    None,                                       # 파일 없음 (OSError)
    b"[telegram\nenabled = true\n",             # TOML 형식 오류
    b"[telegram]\nenabled = \xff\xfe\n",        # UTF-8 아님 (UnicodeDecodeError)
])
def test_paper_refusal_with_unreadable_app_toml_still_notifies(tmp_path: Path, tg: FakeTelegram,
                                                               content: bytes | None) -> None:
    _secrets_root(tmp_path)
    (tmp_path / "config").mkdir()
    if content is not None:
        (tmp_path / "config" / "app.paper.toml").write_bytes(content)
    assert autopilot_main(["--mode", "PAPER", "--root", str(tmp_path)], environ={"TRADE_MODE": "LIVE"},
                          telegram_base_url=tg.url) == EXIT_REFUSED
    assert len(tg.sent) == 1 and "자동 운영 거부" in tg.sent[0]["text"], tg.sent
    assert "확인 불가" in _day_log(tmp_path)


def test_non_paper_refusal_with_unreadable_app_toml_still_log_only(tmp_path: Path, tg: FakeTelegram) -> None:
    _secrets_root(tmp_path)                                                  # 설정 파일 없음 — 그래도 LIVE는 로그만
    assert autopilot_main(["--mode", "LIVE", "--root", str(tmp_path)], environ={},
                          telegram_base_url=tg.url) == EXIT_REFUSED
    assert tg.sent == [] and tg.paths == []


def test_config_refusal_telegram_has_no_filesystem_path(tmp_path: Path, tg: FakeTelegram,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_system.config.errors import ConfigError
    _secrets_root(tmp_path)
    _app_toml(tmp_path, True)
    win = r"C:\Users\someone\trading\config\limits.paper.toml"
    posix = "/home/someone/trading/var/paper/trading.db"

    def bad(*a: object, **k: object) -> None:
        raise ConfigError(f"한도 파일 없음: {tmp_path / 'config' / 'limits.paper.toml'} ({win}, {posix})")
    monkeypatch.setattr("trading_system.app.startup.check_startup", bad)
    assert autopilot_main(["--mode", "PAPER", "--root", str(tmp_path)], environ={},
                          telegram_base_url=tg.url) == EXIT_REFUSED
    assert len(tg.sent) == 1, tg.sent
    msg = tg.sent[0]["text"]
    assert "자동 운영 거부(설정)" in msg and "한도 파일 없음" in msg, msg
    for p in (str(tmp_path), "someone", "C:\\", "/home/"):
        assert p not in msg, msg
    log = _day_log(tmp_path)
    assert str(tmp_path / "config" / "limits.paper.toml") in log and posix in log   # 일 로그에는 경로 그대로


@pytest.mark.parametrize("text,kept", [
    ("원격 /status·/resume K8 확인", True),           # 명령 안내는 경로가 아님
    ("config/app.paper.toml [telegram]", True),        # 저장소 상대 안내는 둔다
    ("K2/K4 08:55/09:00", True),
    ("https://api.telegram.org 응답 없음", True),
    ("없음: \\\\srv\\share\\a.toml", False),             # UNC
    ("없음: D:/data/x.toml", False),
])
def test_strip_paths_only_masks_absolute_paths(text: str, kept: bool) -> None:
    from trading_system.app.autopilot import strip_paths
    assert (strip_paths(text) == text) is kept, strip_paths(text)


@pytest.mark.parametrize("text,gone", [
    ("C:\\AI\\x y\\파일 없음", ("AI", "x y", "파일")),                       # 공백 든 경로 — 조각이 남지 않음
    ("없음: C:\\Program Files\\app\\a.toml (확인)", ("Program", "Files", "app", "a.toml")),
    ("없음: /home/my docs/x.db — 다시 실행", ("home", "my docs", "x.db")),
    ("없음: \\\\srv\\my share\\a.toml\n다음 줄", ("srv", "my share", "a.toml")),
])
def test_strip_paths_masks_paths_with_spaces_to_delimiter(text: str, gone: tuple[str, ...]) -> None:
    from trading_system.app.autopilot import strip_paths
    out = strip_paths(text)
    assert "<경로>" in out, out
    for frag in gone:
        assert frag not in out, out
    if "(확인)" in text:
        assert out.endswith(" (확인)"), out                                 # 구분자 뒤 문구는 남김
    if " — 다시 실행" in text:
        assert out.endswith("<경로> — 다시 실행"), out
    if "\n다음 줄" in text:
        assert out.endswith("\n다음 줄"), out


# --- 3. 429·연결 실패: 메시지 1건 30초 상한 ---------------------------------------------------------------------

class Mono:
    """가짜 단조 시계: sleep과 가짜 HTTP 호출이 시간을 앞당긴다."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


class SlowApi(FakeApi):
    def __init__(self, mono: Mono, replies: list, cost: float) -> None:
        super().__init__(replies)
        self.mono, self.cost = mono, cost
        self.calls = 0

    def send_message(self, chat: str, text: str) -> None:
        self.calls += 1
        self.mono.t += self.cost                                             # 요청 소요 시간 (연결 실패면 시간 초과까지)
        super().send_message(chat, text)


def _timed(tmp_path: Path, api: FakeApi, mono: Mono) -> tuple[TelegramNotify, list[str]]:
    logs: list[str] = []
    n = TelegramNotify(api, CHAT, logs.append, sleep=mono.sleep, monotonic=mono.now,
                       fallback=tmp_path / "logs" / "alerts-fallback-autopilot.log")
    return n, logs


def test_429_long_retry_after_does_not_block_beyond_30s(tmp_path: Path) -> None:
    mono = Mono()
    err = TelegramError("sendMessage: 거부 (HTTP 429)", retry_after=60.0)
    api = SlowApi(mono, [err, err, err], cost=0.2)
    n, logs = _timed(tmp_path, api, mono)
    start = mono.t
    n("WARNING", "제한 중")
    assert mono.t - start <= 30.0, (mono.t - start, mono.sleeps)
    assert mono.sleeps == [] and api.calls == 1                              # 60초 대기는 상한을 넘으므로 기다리지 않음
    assert "제한 중" in (tmp_path / "logs" / "alerts-fallback-autopilot.log").read_text(encoding="utf-8")
    assert len(n.failed) == 1                                                # 종료 전 재전송 큐
    api.replies = [None]
    assert n.flush() == 1 and n.failed == []


def test_429_short_retry_after_honored_within_cap(tmp_path: Path) -> None:
    mono = Mono()
    api = SlowApi(mono, [TelegramError("sendMessage: 거부 (HTTP 429)", retry_after=5.0), None], cost=0.2)
    n, _ = _timed(tmp_path, api, mono)
    n("INFO", "잠시 뒤 성공")
    assert mono.sleeps == [5.0] and api.sent and n.failed == []
    assert not (tmp_path / "logs").exists()


def test_connection_timeouts_total_time_capped_at_30s(tmp_path: Path) -> None:
    mono = Mono()
    err = TelegramError("sendMessage: 연결 실패 (ReadTimeout)")
    api = SlowApi(mono, [err, err, err], cost=13.0)                          # 연결 3초 + 읽기 10초 시간 초과
    n, _ = _timed(tmp_path, api, mono)
    start = mono.t
    n("EMERGENCY", "느린 채널")
    assert mono.t - start <= 30.0, (mono.t - start, mono.sleeps, api.calls)  # 전에는 3회 × 13초 + 대기 = 43초
    assert api.calls == 2 and mono.sleeps == [2.0]
    assert len(n.failed) == 1


def test_repeated_429_each_message_capped(tmp_path: Path) -> None:
    mono = Mono()
    err = TelegramError("sendMessage: 거부 (HTTP 429)", retry_after=12.0)
    api = SlowApi(mono, [err] * 9, cost=0.5)
    n, _ = _timed(tmp_path, api, mono)
    for i in range(3):
        start = mono.t
        n("INFO", f"메시지 {i}")
        assert mono.t - start <= 30.0, (i, mono.t - start)
    assert len(n.failed) == 3


def test_flush_total_time_capped_at_60s(tmp_path: Path) -> None:
    # 재검토(L-4): 종료 전 재전송도 전체 60초 이내 — 남은 것은 큐·fallback 파일에 그대로
    mono = Mono()
    err = TelegramError("sendMessage: 연결 실패 (ReadTimeout)")
    api = SlowApi(mono, [], cost=13.0)
    n, logs = _timed(tmp_path, api, mono)
    n.failed = [f"[정보] [자동 운영] 밀린 {i}" for i in range(10)]
    api.replies = [err] * 10
    start = mono.t
    assert n.flush() == 0
    assert mono.t - start <= 60.0, mono.t - start
    assert api.calls == 4 and len(n.failed) == 10                           # 시도 못 한 6건도 큐에 남김
    assert any("60초" in m for m in logs), logs
    api.replies = []
    mono.t += 1
    api.cost = 0.1
    assert n.flush() == 10 and n.failed == []


def test_retry_after_cap_constant_removed() -> None:
    import trading_system.app.autopilot as m
    assert not hasattr(m, "RETRY_AFTER_CAP_SECONDS")                        # 메시지당 30초 상한으로 대체
    assert m.MESSAGE_BUDGET_SECONDS == 30.0 and m.FLUSH_BUDGET_SECONDS == 60.0
    assert m.__doc__ is not None and "메시지당 총 30초" in m.__doc__


# --- 4. paper-review: 손상된 DAILY_BASE 행 ---------------------------------------------------------------------

def test_paper_review_renders_corrupt_daily_base_rows(tmp_path: Path) -> None:
    db = tmp_path / "var" / "paper" / "trading.db"
    db.parent.mkdir(parents=True)
    repo = Repository(db, Mode.PAPER)
    at = datetime(2026, 10, 12, 0, 0, tzinfo=UTC)
    try:
        repo.audit(at, "DAILY_BASE", "monitor", "KR-PAPER-01",
                   {"trade_date": "2026-10-12", "cash": "10000000", "value": "0", "at": at.isoformat()})
        repo.audit(at + timedelta(days=1), "DAILY_BASE", "monitor", "KR-PAPER-01",
                   {"trade_date": "2026-10-13", "cash": "abc", "value": "0", "at": at.isoformat()})
        repo.audit(at + timedelta(days=2), "DAILY_BASE", "monitor", "KR-PAPER-01", {"trade_date": "2026-10-14"})
    finally:
        repo.close()
    conn = sqlite3.connect(db)   # 감사 표는 추가 전용 — 깨진 요약 행을 그대로 추가해 손상을 흉내 낸다
    try:
        conn.execute("INSERT INTO audit_event(mode, at, kind, actor, ref, summary) VALUES(?,?,?,?,?,?)",
                     ("PAPER", (at + timedelta(days=3)).isoformat(), "DAILY_BASE", "monitor", "KR-PAPER-01",
                      "{깨진 json"))
        conn.commit()
    finally:
        conn.close()
    r = review(db)                                                           # 전에는 json.loads·Decimal 오류로 실패
    assert len(r.daily_bases) == 4, r.daily_bases
    good, bad_cash, missing, broken = r.daily_bases
    assert good["cash"] == "10000000" and good["trade_date"] == "2026-10-12"
    for b in (bad_cash, missing, broken):
        assert b["cash"] == "손상" and b["value"] == "손상", b
    assert bad_cash["trade_date"] == "2026-10-13" and missing["trade_date"] == "2026-10-14"
    assert broken["trade_date"] == "-"
    text = render_markdown(r)
    assert "## 일일 기준" in text and text.count("손상") >= 6, text


# --- 5. run 시작 뒤 특수 예외 → 정지 요청 파일 ------------------------------------------------------------------

class FakeChildRun:
    """SubprocessRun 대역 — 실제 run처럼 실행 잠금(run.lock)을 쥔다. 프로세스는 띄우지 않는다."""
    made: list["FakeChildRun"] = []

    def __init__(self, argv: object, cwd: Path, env: object, log_path: Path) -> None:
        self.lock = acquire_instance_lock(log_path.parent.parent)          # var/paper
        self.exited = False
        FakeChildRun.made.append(self)

    def alive(self) -> bool:
        return not self.exited

    def exit_code(self) -> int | None:
        return 2 if self.exited else None


@pytest.fixture
def child(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    FakeChildRun.made = []
    monkeypatch.setattr("trading_system.app.autopilot.SubprocessRun", FakeChildRun)
    yield FakeChildRun
    for c in FakeChildRun.made:
        c.lock.release()


def test_base_exception_after_run_started_writes_stop_request(tmp_path: Path, tg: FakeTelegram,
                                                              monkeypatch: pytest.MonkeyPatch, child) -> None:  # type: ignore[no-untyped-def]
    _secrets_root(tmp_path)
    _stub_main_setup(monkeypatch)

    def started_then_interrupted(self: Autopilot) -> int:
        self.d.launch()
        raise KeyboardInterrupt
    monkeypatch.setattr(Autopilot, "run", started_then_interrupted)
    rc = autopilot_main(["--mode", "PAPER", "--root", str(tmp_path)], environ={}, telegram_base_url=tg.url)
    assert rc == EXIT_ABORTED
    stop = tmp_path / "var" / "paper" / STOP_REQUEST
    assert stop.exists() and "autopilot" in stop.read_text(encoding="utf-8")
    text = _day_log(tmp_path)
    assert "KeyboardInterrupt" in text and "정지 요청 파일 작성" in text, text
    assert len(tg.sent) == 1 and "정지 요청" in tg.sent[0]["text"], tg.sent


def test_base_exception_before_launch_does_not_stop_manual_run(tmp_path: Path, tg: FakeTelegram,
                                                               monkeypatch: pytest.MonkeyPatch, child) -> None:  # type: ignore[no-untyped-def]
    _secrets_root(tmp_path)
    _stub_main_setup(monkeypatch)
    base = tmp_path / "var" / "paper"
    base.mkdir(parents=True)
    manual = acquire_instance_lock(base)                                     # 사용자가 수동으로 켠 run

    def interrupted(self: Autopilot) -> int:
        raise KeyboardInterrupt
    monkeypatch.setattr(Autopilot, "run", interrupted)
    try:
        assert autopilot_main(["--mode", "PAPER", "--root", str(tmp_path)], environ={},
                              telegram_base_url=tg.url) == EXIT_ABORTED
    finally:
        manual.release()
    assert not (base / STOP_REQUEST).exists()                                # 이 자동 운영이 띄운 run이 아니면 건드리지 않음


def test_base_exception_after_child_already_exited_writes_nothing(tmp_path: Path, tg: FakeTelegram,
                                                                  monkeypatch: pytest.MonkeyPatch, child) -> None:  # type: ignore[no-untyped-def]
    _secrets_root(tmp_path)
    _stub_main_setup(monkeypatch)

    def launched_exited_interrupted(self: Autopilot) -> int:
        self.d.launch()
        FakeChildRun.made[-1].lock.release()                                 # 자식 run은 이미 끝남(잠금 해제)
        raise KeyboardInterrupt
    monkeypatch.setattr(Autopilot, "run", launched_exited_interrupted)
    assert autopilot_main(["--mode", "PAPER", "--root", str(tmp_path)], environ={},
                          telegram_base_url=tg.url) == EXIT_ABORTED
    assert not (tmp_path / "var" / "paper" / STOP_REQUEST).exists()


def test_base_exception_child_exited_and_manual_run_holds_lock_no_stop(tmp_path: Path, tg: FakeTelegram,
                                                                        monkeypatch: pytest.MonkeyPatch,
                                                                        child) -> None:  # type: ignore[no-untyped-def]
    # 재검토 M-1: 자식 run이 끝난 뒤(예: 잠금 경합으로 코드 2, 또는 종료 절차 중) 사용자가 수동 run을 켜 잠금을 잡았으면
    # 실행 잠금만 보고 정지 요청을 쓰면 수동 run을 멈춘다 — 자기 자식이 살아 있을 때만 쓴다
    _secrets_root(tmp_path)
    _stub_main_setup(monkeypatch)
    held: list = []

    def child_exited_manual_started(self: Autopilot) -> int:
        self.d.launch()
        c = FakeChildRun.made[-1]
        c.lock.release()
        c.exited = True                                                      # 자식 프로세스 종료
        held.append(acquire_instance_lock(self.base))                        # 수동 run이 잠금을 잡음
        raise KeyboardInterrupt
    monkeypatch.setattr(Autopilot, "run", child_exited_manual_started)
    try:
        assert autopilot_main(["--mode", "PAPER", "--root", str(tmp_path)], environ={},
                              telegram_base_url=tg.url) == EXIT_ABORTED
    finally:
        for lk in held:
            lk.release()
    assert not (tmp_path / "var" / "paper" / STOP_REQUEST).exists()
    assert "정지 요청 파일 안 씀" in _day_log(tmp_path)


# --- 6. 미래 시각 하트비트: status·preflight·자동 운영 모두 "시계 이상 의심" -----------------------------------------

def test_future_heartbeat_shows_clock_suspect_everywhere(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from trading_system.app.preflight import engine_alive
    from trading_system.domain.heartbeat import watchdog_check
    from trading_system.ops.cli import _print_heartbeat
    now = datetime(2026, 10, 12, 1, 0, tzinfo=UTC)
    hb = tmp_path / "heartbeat"
    hb.write_text((now + timedelta(seconds=120)).isoformat(), encoding="utf-8")
    _print_heartbeat(hb, now)
    out = capsys.readouterr().out
    assert "시계 이상 의심" in out and "120초 미래" in out and "-120초 전" not in out, out
    reason = engine_alive(hb, now)
    assert reason is not None and "시계 이상 의심" in reason and "120초 미래" in reason
    st = watchdog_check(hb, now, timedelta(seconds=90))                      # 자동 운영 장중 감시 기준
    assert not st.alive and "시계 이상 의심" in st.detail
    assert now.astimezone(KST).hour == 10
