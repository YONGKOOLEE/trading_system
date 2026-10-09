"""운영 보강 (사용자 결정 A-084, 2026-10-08 — 첫 무인 자동 운영 2026-10-12 전). 04 V-201~V-212 보강, R-18·R-21, X-U3.

1. SNTP 시계 점검: 서버당 표본 3개(약 0.2초 간격) 중 왕복 지연이 가장 짧은 표본으로 판정. 허용 오차 의미는 그대로.
2. 자동 운영 거부 사유: DayLog·텔레그램 전에 거부되던 것을 일 로그에 남기고, 비밀값이 있으면 경고 1건.
3. 자동 운영 알림 유실 방지: 최종 실패는 alerts-fallback-autopilot.log, 429 Retry-After 존중(상한 60초), 종료 전 1회 재전송.
4. 다음 해 달력 사전 경고: 오늘+30일이 달력 없는 해로 넘어가면 경고(실패 아님).
5. 종료 코드: 0 정상, 1 문제 있음, 2 시작 전 거부, 3 run 시작 뒤 중단. 하트비트 회복은 문제로 세지 않음.

실제 네트워크·실제 자격정보 없음. 텔레그램은 로컬 가짜 HTTP 서버 또는 가짜 API 객체. 모든 값은 가상이다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT as ENV_ACCOUNT, Env
from tests.scenario.test_t012_monitoring import _response
from tests.scenario.test_t013_telegram import CHAT, TOKEN, FakeTelegram, tg  # noqa: F401 — 픽스처 재사용
from tests.scenario.test_t015_preflight import (  # noqa: F401 — 픽스처 재사용
    CODE, ENV_OK, clock, make_root, sim)
from tests.scenario.test_t025_autopilot import DAY, Rig, calendar, kst
from tests.support.kiwoom_sim import Sim
from trading_system.app.autopilot import (EXIT_ABORTED, EXIT_OK, EXIT_PROBLEMS, EXIT_REFUSED, TelegramNotify,
                                          autopilot_main)
from trading_system.app.preflight import preflight_main
from trading_system.domain.clock import FixedClock
from trading_system.marketdata.calendar import KrxCalendar, parse_calendar
from trading_system.monitoring.sntp import (ClockMonitor, ClockResult, SntpError, SntpSample, build_request,
                                            parse_sample, query_sample)
from trading_system.ops.telegram import TelegramApi, TelegramError


# --- 1. SNTP 표본 3개 -----------------------------------------------------------------------------------------

def test_a084_parse_sample_reports_offset_and_round_trip_delay() -> None:
    t1 = datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC)
    req = build_request(t1)
    resp = _response(req, t1 + timedelta(seconds=1, milliseconds=600))    # t2 = t3
    s = parse_sample(resp, t1, t1 + timedelta(milliseconds=200))
    assert isinstance(s, SntpSample)
    assert s.offset == Decimal("1.500000")                                  # ((1.6) + (1.6 − 0.2)) / 2
    assert s.delay == Decimal("0.200000")                                   # (t4 − t1) − (t3 − t2) = 0.2 − 0


class SampleQuery:
    """서버별 표본 순서 주입. 값은 SntpSample, Decimal(오차만), 또는 SntpError(실패)."""

    def __init__(self, plan: dict[str, list]) -> None:
        self.plan = {h: iter(v) for h, v in plan.items()}
        self.calls: list[str] = []

    def __call__(self, host: str, port: int):  # type: ignore[no-untyped-def]
        self.calls.append(host)
        v = next(self.plan[host])
        if isinstance(v, Exception):
            raise v
        return v


def S(offset: str, delay: str) -> SntpSample:
    return SntpSample(Decimal(offset), Decimal(delay))


def monitor(env: Env, q: SampleQuery, servers: list[tuple[str, int]], sleeps: list[float], **kw) -> ClockMonitor:  # type: ignore[no-untyped-def]
    return ClockMonitor(env.repo, env.clock, servers, env.halts, None, q, sleep=sleeps.append, **kw)


def test_a084_three_samples_pick_smallest_delay_not_first(env: Env) -> None:
    # 10-08 실측: 표본 1개 0.976초(허용 1초 직전) — 지연이 큰 표본의 오차는 믿지 않는다
    q = SampleQuery({"a": [S("0.976", "0.900"), S("0.310", "0.080"), S("0.520", "0.400")]})
    sleeps: list[float] = []
    res = monitor(env, q, [("a", 1)], sleeps).check()
    assert res.ok is True and res.offset == Decimal("0.310") and res.delay == Decimal("0.080")
    assert res.samples == 3 and res.source == "a:1"
    assert q.calls == ["a", "a", "a"]
    assert sleeps == [0.2, 0.2]                                             # 표본 사이 간격, 첫 표본 전에는 없음
    assert "지연 0.080" in res.detail and "표본 3" in res.detail
    assert "K6" not in env.repo.active_halts(ENV_ACCOUNT)
    assert env.repo.select("SELECT offset_seconds, result FROM clock_check") == [{"offset_seconds": "0.310",
                                                                                 "result": "OK"}]


def test_a084_samples_configurable_and_tolerance_unchanged(env: Env) -> None:
    q = SampleQuery({"a": [S("1.000000", "0.1"), S("1.000001", "0.05")], "b": [S("0", "0")]})
    res = monitor(env, q, [("a", 1), ("b", 2)], [], samples=1).check()
    assert q.calls == ["a"] and res.samples == 1
    assert res.ok is True and res.offset == Decimal("1.000000")           # 경계 포함 (R-24 허용 1초)
    q2 = SampleQuery({"a": [S("1.000001", "0.05"), S("1.5", "0.3")]})
    res2 = monitor(env, q2, [("a", 1)], [], samples=2).check()
    assert res2.ok is False and res2.offset == Decimal("1.000001")
    assert "K6" in env.repo.active_halts(ENV_ACCOUNT)


def test_a084_server_failing_midway_uses_collected_samples(env: Env) -> None:
    q = SampleQuery({"a": [S("0.4", "0.5"), SntpError("a:1 응답 없음: TimeoutError")], "b": [S("0", "0")] * 3})
    res = monitor(env, q, [("a", 1), ("b", 2)], []).check()
    assert res.ok is True and res.offset == Decimal("0.4") and res.samples == 1 and res.source == "a:1"
    assert q.calls == ["a", "a"]                                            # 표본 1개 확보 → 다음 서버로 안 넘어감


def test_a084_server_failing_first_sample_falls_to_next_server(env: Env) -> None:
    q = SampleQuery({"a": [SntpError("a:1 응답 없음: TimeoutError")], "b": [S("0.1", "0.3"), S("0.2", "0.1"),
                                                                          S("0.9", "0.9")]})
    res = monitor(env, q, [("a", 1), ("b", 2)], []).check()
    assert res.ok is True and res.offset == Decimal("0.2") and res.source == "b:2" and res.samples == 3
    q_all = SampleQuery({"a": [SntpError("a 실패")], "b": [SntpError("b 실패")]})
    res2 = monitor(env, q_all, [("a", 1), ("b", 2)], []).check()
    assert res2.ok is None and res2.samples == 0 and "a 실패" in res2.source and "b 실패" in res2.source


def test_a084_legacy_decimal_query_still_works(env: Env) -> None:
    q = SampleQuery({"a": [Decimal("0.3"), Decimal("0.2"), Decimal("0.4")]})
    res = monitor(env, q, [("a", 1)], []).check()
    assert res.ok is True and res.offset == Decimal("0.3") and res.delay is None and res.samples == 3


def test_a084_query_sample_against_local_fake_server() -> None:
    import socket
    import threading
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]

    def serve() -> None:
        data, addr = srv.recvfrom(512)
        srv.sendto(_response(data, datetime.now(UTC) + timedelta(seconds=3)), addr)

    t = threading.Thread(target=serve)
    t.start()
    s = query_sample("127.0.0.1", port, timeout=2)
    t.join()
    srv.close()
    assert Decimal("2.5") < s.offset < Decimal("3.5") and s.delay is not None and Decimal("0") <= s.delay < Decimal("1")


# --- 4. 다음 해 달력 사전 경고 (달력 모듈·시작 점검·자동 운영) -----------------------------------------------------

def two_years() -> KrxCalendar:
    years = {y: parse_calendar({"market": "KRX", "year": y, "source": "가상", "source_version": "t", "holidays": []}, y)
             for y in (2026, 2027)}
    return KrxCalendar(years)


@pytest.mark.parametrize("today,cal,expect", [
    (date(2026, 12, 2), "one", 2027),      # +30일 = 2027-01-01 → 2027 없음
    (date(2026, 11, 30), "one", None),     # +30일 = 2026-12-30 → 아직 같은 해
    (date(2026, 12, 2), "two", None),      # 2027 달력 있음
    (date(2027, 1, 4), "two", None),       # 새해, 두 달력 모두 있음
    (date(2027, 1, 4), "one", None),       # 올해(2027)가 없는 것은 기존 "올해 달력 없음" 경고·거부 몫 — 여기서는 아님
])
def test_a084_calendar_missing_next_year(today: date, cal: str, expect: int | None) -> None:
    c = calendar() if cal == "one" else two_years()
    assert c.missing_year_within(today, 30) == expect


def test_a084_preflight_warns_next_year_calendar_missing(tmp_path: Path, sim: Sim, clock: FixedClock,
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path, env_lines=ENV_OK, approved=True)
    (root / "config" / "calendar" / "krx-2026.toml").write_text(
        'market = "KRX"\nyear = 2026\nsource = "가상"\nsource_version = "t"\nholidays = []\n', encoding="utf-8")
    clock._now = datetime(2026, 12, 2, 1, 0, tzinfo=UTC)  # noqa: SLF001 — 10:00 KST, 2026-12-02
    preflight_main(root, {"TRADE_MODE": "PAPER"}, rest_url=sim.url, ws_url=sim.ws_url, clock=clock, probe_code=CODE,
                   per_tr_interval=0.0, sntp_query=lambda h, p: Decimal("0"))
    out = capsys.readouterr().out
    assert "다음 해 달력 없음" in out and "2027" in out and "X-U3" in out, out
    assert "실패:" not in out and "결과: 통과" in out                       # 경고이지 실패가 아님


def test_a084_preflight_no_next_year_warning_when_not_within_30_days(tmp_path: Path, sim: Sim, clock: FixedClock,
                                                                       capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path, env_lines=ENV_OK, approved=True)
    (root / "config" / "calendar" / "krx-2026.toml").write_text(
        'market = "KRX"\nyear = 2026\nsource = "가상"\nsource_version = "t"\nholidays = []\n', encoding="utf-8")
    clock._now = datetime(2026, 11, 30, 1, 0, tzinfo=UTC)  # noqa: SLF001
    preflight_main(root, {"TRADE_MODE": "PAPER"}, rest_url=sim.url, ws_url=sim.ws_url, clock=clock, probe_code=CODE,
                   per_tr_interval=0.0, sntp_query=lambda h, p: Decimal("0"))
    assert "다음 해 달력" not in capsys.readouterr().out


def test_a084_preflight_reports_sntp_delay(tmp_path: Path, sim: Sim, clock: FixedClock,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path, env_lines=ENV_OK, approved=True)
    plan = iter([S("0.9", "0.8"), S("0.1", "0.05"), S("0.5", "0.4")])
    assert preflight_main(root, {"TRADE_MODE": "PAPER"}, rest_url=sim.url, ws_url=sim.ws_url, clock=clock,
                          probe_code=CODE, per_tr_interval=0.0, sntp_query=lambda h, p: next(plan)) == 0
    out = capsys.readouterr().out
    assert "오차 0.1초" in out and "지연 0.05초" in out, out


def test_a084_autopilot_warns_next_year_calendar_missing(tmp_path: Path) -> None:
    r = Rig(tmp_path, start=kst(6, 45, d=date(2026, 12, 2)), preflight_rc=1)   # 수요일. 시작 점검 실패로 빨리 끝남
    assert r.ap.run() == EXIT_REFUSED
    warns = [t for lv, t in r.notes if lv == "WARNING" and "다음 해 달력 없음" in t]
    assert len(warns) == 1 and "2027" in warns[0] and "X-U3" in warns[0], r.notes


def test_a084_autopilot_no_next_year_warning_otherwise(tmp_path: Path) -> None:
    r = Rig(tmp_path, start=kst(6, 45, d=date(2026, 11, 30)), preflight_rc=1)
    assert r.ap.run() == EXIT_REFUSED
    assert not any("다음 해 달력" in t for _, t in r.notes), r.notes
    r2 = Rig(tmp_path / "b", start=kst(6, 45, d=date(2026, 12, 2)), preflight_rc=1)
    r2.ap.calendar = two_years()
    assert r2.ap.run() == EXIT_REFUSED
    assert not any("다음 해 달력" in t for _, t in r2.notes), r2.notes


# --- 5. 종료 코드 · 하트비트 회복 -----------------------------------------------------------------------------

def test_a084_exit_code_constants_documented() -> None:
    import trading_system.app.autopilot as m
    assert (EXIT_OK, EXIT_PROBLEMS, EXIT_REFUSED, EXIT_ABORTED) == (0, 1, 2, 3)
    assert m.__doc__ is not None and "종료 코드" in m.__doc__ and "3" in m.__doc__


def test_a084_heartbeat_stall_recovered_same_day_is_not_a_problem(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.hooks.append((kst(10, 0), lambda: setattr(r.runs[-1], "frozen", True)))     # run이 멈춘 듯(하트비트 안 씀)
    r.hooks.append((kst(11, 0), lambda: setattr(r.runs[-1], "frozen", False)))    # 회복
    assert r.ap.run() == EXIT_OK, r.notes
    assert any(lv == "EMERGENCY" and "하트비트 멈춤" in t for lv, t in r.notes), r.notes
    assert any(lv == "INFO" and "하트비트 회복" in t for lv, t in r.notes), r.notes
    summary = r.notes[-1][1]
    assert "일일 요약" in summary and "확인 필요" not in summary, summary
    assert "회복" in summary, summary                                       # 요약(진행 메모)에는 남긴다


def test_a084_heartbeat_stall_not_recovered_is_a_problem(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.hooks.append((kst(10, 0), lambda: setattr(r.runs[-1], "frozen", True)))
    r.hooks.append((kst(15, 50), lambda: setattr(r.runs[-1], "frozen", False)))   # 장중 감시가 끝난 뒤에야 회복
    assert r.ap.run() == EXIT_PROBLEMS, r.notes
    summary = r.notes[-1][1]
    assert "확인 필요" in summary and "하트비트 멈춤" in summary, summary


# --- 2. 거부 사유 기록 · 거부 알림 ------------------------------------------------------------------------------

def _day_log(root: Path) -> str:
    logs = sorted((root / "var" / "paper" / "logs").glob("autopilot-*.log"))
    assert len(logs) == 1, logs
    return logs[0].read_text(encoding="utf-8")


def test_a084_mode_refusal_is_written_to_day_log(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert autopilot_main(["--mode", "LIVE", "--root", str(tmp_path)], environ={}) == EXIT_REFUSED
    text = _day_log(tmp_path)
    assert "자동 운영 거부" in text and "PAPER 전용" in text, text
    assert "텔레그램" in text                                                # 알림을 보냈는지/못 보냈는지도 기록
    assert not (tmp_path / "var" / "paper" / "trading.db").exists()        # 저장소·잠금은 만들지 않음
    assert not (tmp_path / "var" / "paper" / "autopilot.lock").exists()


def _telegram_on(root: Path) -> None:
    """재검토 잔여(T-025): PAPER 거부 알림은 config/app.paper.toml [telegram] enabled = true일 때만 보낸다."""
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "app.paper.toml").write_text("[telegram]\nenabled = true\n", encoding="utf-8")


def test_a084_refusal_sends_one_telegram_warning_when_secrets_available(tmp_path: Path, tg: FakeTelegram) -> None:
    # 재검토 잔여(T-025)로 변경: --mode LIVE 거부는 PAPER 채널에 보내지 않는다(tests/scenario/test_ops_low_items.py).
    # 알림 경로는 PAPER 요청의 모드 거부(환경 변수 TRADE_MODE=LIVE) + [telegram] enabled로 시험한다.
    (tmp_path / ".env.paper").write_text(f"TRADE_MODE=PAPER\nTELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID={CHAT}\n",
                                         encoding="utf-8")
    _telegram_on(tmp_path)
    assert autopilot_main(["--mode", "PAPER", "--root", str(tmp_path)], environ={"TRADE_MODE": "LIVE"},
                          telegram_base_url=tg.url) == 2
    assert len(tg.sent) == 1 and tg.sent[0]["text"].startswith("[M1] [경고] [자동 운영] 자동 운영 거부"), tg.sent  # [M1] 표시: T-M2-13 D-117 A-065 Q5
    assert "PAPER가 아님" in tg.sent[0]["text"]
    text = _day_log(tmp_path)
    assert TOKEN not in text and "virtual-token" not in text


def test_a084_config_refusal_logged_and_notified(tmp_path: Path, tg: FakeTelegram) -> None:
    (tmp_path / ".env").write_text("TRADE_MODE=PAPER\n", encoding="utf-8")
    (tmp_path / ".env.paper").write_text(f"TRADE_MODE=PAPER\nTELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID={CHAT}\n",
                                         encoding="utf-8")
    # 한도 파일 등 없음 → check_startup/설정 적재가 ConfigError → 거부(코드 2). 재검토 잔여(T-025): 알림은 [telegram]
    # enabled = true를 읽을 수 있을 때만 — config/app.paper.toml에 그 값만 둔다
    _telegram_on(tmp_path)
    assert autopilot_main(["--mode", "PAPER", "--root", str(tmp_path)], environ={}, telegram_base_url=tg.url) == 2
    text = _day_log(tmp_path)
    assert "자동 운영 거부(설정)" in text, text
    assert len(tg.sent) == 1 and "자동 운영 거부" in tg.sent[0]["text"], tg.sent


def test_a084_refusal_without_secrets_logs_only(tmp_path: Path, tg: FakeTelegram) -> None:
    # 재검토 잔여(T-025)로 변경: BACKTEST 요청은 비밀값 확인 전에 로그만 → "키 없음" 경로는 PAPER 요청 거부로 시험한다
    (tmp_path / ".env.paper").write_text("TRADE_MODE=PAPER\n", encoding="utf-8")   # 텔레그램 키 없음
    _telegram_on(tmp_path)
    assert autopilot_main(["--mode", "PAPER", "--root", str(tmp_path)], environ={"TRADE_MODE": "BACKTEST"},
                          telegram_base_url=tg.url) == 2
    assert tg.sent == [] and tg.paths == []
    assert "거부 알림 못 보냄" in _day_log(tmp_path)


# --- 3. 자동 운영 알림 유실 방지 ---------------------------------------------------------------------------------

class FakeApi:
    def __init__(self, replies: list) -> None:
        self.replies = list(replies)
        self.sent: list[str] = []

    def send_message(self, chat: str, text: str) -> None:
        r = self.replies.pop(0) if self.replies else None
        if isinstance(r, Exception):
            raise r
        self.sent.append(text)


def _notify(tmp_path: Path, api, sleeps: list[float]):  # type: ignore[no-untyped-def]
    logs: list[str] = []
    n = TelegramNotify(api, CHAT, logs.append, sleep=sleeps.append, fallback=tmp_path / "logs" / "alerts-fallback-autopilot.log")
    return n, logs


def test_a084_retry_after_honored_with_cap(tmp_path: Path) -> None:
    api = FakeApi([TelegramError("sendMessage: 거부 (HTTP 429)", retry_after=7.0), None,
                   TelegramError("sendMessage: 거부 (HTTP 429)", retry_after=600.0), None])
    sleeps: list[float] = []
    n, _ = _notify(tmp_path, api, sleeps)
    n("INFO", "하나")
    n("INFO", "둘")
    # 2초 고정 대기 대신 Retry-After. 재검토 잔여(T-025)로 변경: 메시지 1건 30초 상한 — 상한 60초 대기(600→60)는 30초를
    # 넘으므로 기다리지 않고 fallback + 종료 전 재전송 큐로 (전에는 [7.0, 60.0]·2건 즉시 전송)
    assert sleeps == [7.0] and api.sent == ["[정보] [자동 운영] 하나"]
    assert len(n.failed) == 1 and "둘" in n.failed[0]
    assert "둘" in (tmp_path / "logs" / "alerts-fallback-autopilot.log").read_text(encoding="utf-8")
    assert n.flush() == 1 and n.failed == [] and len(api.sent) == 2


def test_a084_final_failure_goes_to_fallback_file_and_is_resent_once_at_exit(tmp_path: Path) -> None:
    err = TelegramError("sendMessage: 연결 실패 (ConnectionError)")
    api = FakeApi([err, err, err, err, err, err])
    sleeps: list[float] = []
    n, logs = _notify(tmp_path, api, sleeps)
    n("EMERGENCY", "첫 번째")
    n("WARNING", "두 번째")
    fb = (tmp_path / "logs" / "alerts-fallback-autopilot.log").read_text(encoding="utf-8")
    assert "[긴급] [자동 운영] 첫 번째" in fb and "[경고] [자동 운영] 두 번째" in fb
    assert len(n.failed) == 2 and sleeps == [2.0] * 4 and api.sent == []   # 시도 3회(대기 2회) × 2건
    api.replies = [None, err]
    assert n.flush() == 1                                                   # 종료 전 1회 재전송: 하나 성공
    assert api.sent == ["[긴급] [자동 운영] 첫 번째"] and len(n.failed) == 1 and "두 번째" in n.failed[0]
    assert any("재전송" in m for m in logs), logs
    api.replies = [None]
    assert n.flush() == 1 and n.failed == []
    assert n.flush() == 0                                                   # 큐가 비면 아무것도 안 함


def test_a084_permanent_rejection_is_recorded_but_not_requeued(tmp_path: Path) -> None:
    api = FakeApi([TelegramError("sendMessage: 거부 (HTTP 400) message is too long", permanent=True)])
    sleeps: list[float] = []
    n, _ = _notify(tmp_path, api, sleeps)
    n("INFO", "거부될 문구")
    assert "거부될 문구" in (tmp_path / "logs" / "alerts-fallback-autopilot.log").read_text(encoding="utf-8")
    assert n.failed == [] and sleeps == []


def test_a084_fallback_with_real_http_client(tmp_path: Path, tg: FakeTelegram) -> None:
    api = TelegramApi(lambda: TOKEN, tg.url)
    sleeps: list[float] = []
    n, logs = _notify(tmp_path, api, sleeps)
    tg.fail = True
    n("WARNING", "서버 장애 중")
    assert len(tg.paths) == 3 and tg.sent == [] and len(n.failed) == 1
    assert "서버 장애 중" in (tmp_path / "logs" / "alerts-fallback-autopilot.log").read_text(encoding="utf-8")
    tg.fail = False
    assert n.flush() == 1 and len(tg.sent) == 1 and "서버 장애 중" in tg.sent[0]["text"]
    assert all(TOKEN not in m for m in logs)


def test_a084_429_with_real_http_client_waits_retry_after(tmp_path: Path, tg: FakeTelegram) -> None:
    api = TelegramApi(lambda: TOKEN, tg.url)
    sleeps: list[float] = []
    n, _ = _notify(tmp_path, api, sleeps)
    once = {"done": False}

    def reply(body: dict):  # type: ignore[no-untyped-def]
        if not once["done"]:
            once["done"] = True
            return 429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 3}}
        return None
    tg.send_reply = reply
    n("INFO", "제한 뒤 성공")
    assert sleeps == [3.0] and len(tg.sent) == 1 and n.failed == []


# --- 재검토: SNTP 음수 왕복 지연 표본 제외 ---------------------------------------------------------------------

def test_a084_negative_delay_samples_excluded(env: Env) -> None:
    # 왕복 지연 음수 = 측정 중 시계 점프·쓰레기 응답 — "가장 짧은 지연"으로 뽑히면 안 된다
    q = SampleQuery({"a": [S("0.900", "-0.500"), S("0.300", "0.100"), S("0.200", "0.200")]})
    res = monitor(env, q, [("a", 1)], []).check()
    assert res.ok is True and res.offset == Decimal("0.300") and res.delay == Decimal("0.100"), res
    assert res.samples == 2 and res.source == "a:1"                        # 유효 표본 수 (음수 제외)


def test_a084_all_negative_delay_samples_mean_no_valid_sample(env: Env) -> None:
    neg = [S("0.1", "-0.01"), S("0.2", "-0.02"), S("0.3", "-0.03")]
    q = SampleQuery({"a": list(neg), "b": [S("0.4", "0.05")] * 3})
    res = monitor(env, q, [("a", 1), ("b", 2)], []).check()
    assert res.ok is True and res.source == "b:2" and res.offset == Decimal("0.4")   # 다음 서버로
    q_all = SampleQuery({"a": list(neg), "b": list(neg)})
    res2 = monitor(env, q_all, [("a", 1), ("b", 2)], []).check()
    assert res2.ok is None and res2.offset is None and res2.samples == 0, res2
    assert "a:1" in res2.source and "b:2" in res2.source and "음수" in res2.source
    assert "K6" in env.repo.active_halts(ENV_ACCOUNT)                       # 판정 불가 = 모두 실패와 같음


# --- 재검토: 처리되지 않은 예외 → 종료 코드 3 · traceback 일 로그 · 비밀값 없음 ------------------------------------

FAKE_APP_KEY = "virtual-appkey-Q7Z9X1"


def _secrets_root(root: Path) -> None:
    (root / ".env").write_text("TRADE_MODE=PAPER\n", encoding="utf-8")
    (root / ".env.paper").write_text(f"TRADE_MODE=PAPER\nTELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID={CHAT}\n",
                                     encoding="utf-8")


def _assert_no_secrets(text: str) -> None:
    assert TOKEN not in text and "virtual-token" not in text and FAKE_APP_KEY not in text, text


def test_a084_unhandled_exception_exits_3_with_traceback_in_day_log(tmp_path: Path, tg: FakeTelegram,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    _secrets_root(tmp_path)

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError(f"주입 결함 {TOKEN}")
    monkeypatch.setattr("trading_system.app.startup.check_startup", boom)   # 설정 단계에서 예상 밖 예외
    rc = autopilot_main(["--mode", "PAPER", "--root", str(tmp_path)], environ={}, telegram_base_url=tg.url)
    assert rc == EXIT_ABORTED                                                # 1(문제 있음)이 아니라 3
    text = _day_log(tmp_path)
    assert "Traceback" in text and "RuntimeError" in text and "주입 결함" in text and "boom" in text, text
    assert "자동 운영 예외 종료" in text
    _assert_no_secrets(text)
    assert len(tg.sent) == 1 and tg.sent[0]["text"].startswith("[M1] [긴급] [자동 운영] 자동 운영 예외 종료"), tg.sent  # [M1] 표시: T-M2-13 D-117 A-065 Q5
    _assert_no_secrets(tg.sent[0]["text"])


def test_a084_unhandled_exception_without_secrets_still_logged(tmp_path: Path, tg: FakeTelegram,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("TRADE_MODE=PAPER\n", encoding="utf-8")

    def boom(*a: object, **k: object) -> None:
        raise KeyError("주입")
    monkeypatch.setattr("trading_system.app.autopilot.mode_refusal", boom)
    assert autopilot_main(["--mode", "PAPER", "--root", str(tmp_path)], environ={},
                          telegram_base_url=tg.url) == EXIT_ABORTED
    text = _day_log(tmp_path)
    assert "Traceback" in text and "KeyError" in text, text
    assert tg.sent == []                                                     # 비밀값 없음 → 로그만


def test_a084_system_exit_code_passes_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _secrets_root(tmp_path)

    def bye(*a: object, **k: object) -> None:
        raise SystemExit(5)
    monkeypatch.setattr("trading_system.app.startup.check_startup", bye)
    with pytest.raises(SystemExit) as e:
        autopilot_main(["--mode", "PAPER", "--root", str(tmp_path)], environ={})
    assert e.value.code == 5


def test_a084_refusal_notify_exception_does_not_escape(tmp_path: Path, tg: FakeTelegram,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    _secrets_root(tmp_path)

    def broken(self: TelegramNotify, level: str, text: str) -> None:
        raise RuntimeError(f"전송 결함 {TOKEN}")
    monkeypatch.setattr(TelegramNotify, "__call__", broken)
    _telegram_on(tmp_path)   # 재검토 잔여(T-025): LIVE 요청은 알림 자체가 없으므로 PAPER 요청 거부로 전송 경로를 탄다
    assert autopilot_main(["--mode", "PAPER", "--root", str(tmp_path)], environ={"TRADE_MODE": "LIVE"},
                          telegram_base_url=tg.url) == EXIT_REFUSED              # 거부는 거부 그대로
    text = _day_log(tmp_path)
    assert "자동 운영 거부" in text and "RuntimeError" in text, text
    _assert_no_secrets(text)


def _stub_main_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """autopilot_main이 Autopilot.run까지 가도록 설정 적재를 가상 값으로 대체 (네트워크·실제 자격정보 없음)."""
    from types import SimpleNamespace

    from trading_system.config.envfile import parse_env_text
    secrets = parse_env_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID={CHAT}\n"
                             f"KIWOOM_PAPER_APP_KEY={FAKE_APP_KEY}\n")
    startup = SimpleNamespace(account="KR-PAPER-01", secrets=secrets,
                              limits=SimpleNamespace(daily_reference_time=datetime(2026, 1, 1, 9).time()))
    monkeypatch.setattr("trading_system.app.startup.check_startup", lambda *a, **k: startup)
    monkeypatch.setattr("trading_system.config.app_file.load_app_settings",
                        lambda *a, **k: SimpleNamespace(account="KR-PAPER-01", telegram_enabled=True))
    monkeypatch.setattr(KrxCalendar, "load_dir", staticmethod(lambda p: None))
    monkeypatch.setattr("trading_system.config.market_file.load_market_settings",
                        lambda *a, **k: SimpleNamespace(cost_rates=None))


def test_a084_autopilot_exception_then_notify_exception_does_not_escape(tmp_path: Path, tg: FakeTelegram,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_system.app.autopilot import Autopilot
    _secrets_root(tmp_path)
    _stub_main_setup(monkeypatch)

    def fault(self: Autopilot) -> int:
        raise ValueError(f"결함 {FAKE_APP_KEY}")

    def broken(self: TelegramNotify, level: str, text: str) -> None:
        raise RuntimeError(f"전송 결함 {TOKEN}")
    monkeypatch.setattr(Autopilot, "run", fault)
    monkeypatch.setattr(TelegramNotify, "__call__", broken)
    rc = autopilot_main(["--mode", "PAPER", "--root", str(tmp_path)], environ={}, telegram_base_url=tg.url)
    assert rc == EXIT_REFUSED                                                # run 시작 전 결함 (기존 분류 유지)
    text = _day_log(tmp_path)
    assert "Traceback" in text and "ValueError" in text and "RuntimeError" in text, text
    assert "자동 운영 끝 (코드 2)" in text
    _assert_no_secrets(text)                                                 # 시작 설정의 비밀값을 일 로그 가림에 등록


def test_a084_autopilot_base_exception_exits_3(tmp_path: Path, tg: FakeTelegram,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_system.app.autopilot import Autopilot
    _secrets_root(tmp_path)
    _stub_main_setup(monkeypatch)

    def interrupted(self: Autopilot) -> int:
        raise KeyboardInterrupt
    monkeypatch.setattr(Autopilot, "run", interrupted)
    rc = autopilot_main(["--mode", "PAPER", "--root", str(tmp_path)], environ={}, telegram_base_url=tg.url)
    assert rc == EXIT_ABORTED
    text = _day_log(tmp_path)
    assert "KeyboardInterrupt" in text and "자동 운영 예외 종료" in text, text
    assert len(tg.sent) == 1 and "자동 운영 예외 종료" in tg.sent[0]["text"], tg.sent
