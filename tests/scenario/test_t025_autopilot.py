"""T-025 M1 PAPER 무인 자동 운영 (CP-029 draft, A-078, 04 V-201~V-212 초안).

- 자동 운영 진입점(app/autopilot.py): PAPER 외 거부, 휴장일 종료, 시작 점검 → run, K8 자동 재개(원격 K8과 같은 새 대조 +
  §2.11 조건 1~5 기준, 채널 AUTO), 08:55 시한, 사람 확인 정지(K1·K4 등)면 자동 재개 안 함, 15:45 종료 절차
  (마감 대조 확인 → 정지 요청 파일 → paper-review → 백업 → 일일 요약), 중복 실행 거부.
- run 쪽 정지 요청 파일(var/<mode>/STOP) — Ctrl+C와 같은 종료 경로 (실제 run_main, BACKTEST 연습 실행).

run 프로세스는 같은 프로세스 안의 가짜(FakeRun: 실제 build_engine + fake 브로커 + PAPER 저장소)로 대신하고, 자동 운영의
sleep이 가상 시계를 앞당기며 엔진 tick을 한 번 돌린다(결정적, 실제 대기 없음). 키움·텔레그램 실제 연결 없음 — 텔레그램은
기록용 가짜 notify. 모든 값은 가상이다.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, time as dtime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT as ENV_ACCOUNT, Env
from tests.scenario.test_t020_main import make_root as make_bt_root
from tests.scenario.test_t022_remote_ops import enter
from trading_system.app.autopilot import (AUTO_ACTOR, Autopilot, AutopilotDeps, AutopilotRefused, Schedule,
                                          autopilot_main, mode_refusal, ops_stop_main)
from trading_system.app.instance_lock import AUTOPILOT_LOCK, acquire_instance_lock
from trading_system.app.main import STOP_REQUEST, run_main
from trading_system.app.wiring import build_engine
from trading_system.broker.fake.broker import FakeBroker
from trading_system.domain.clock import KST, FixedClock
from trading_system.domain.enums import HaltType, Mode
from trading_system.domain.market import CashView, CostRates, GateSettings
from trading_system.halt.manager import Channel
from trading_system.marketdata.calendar import KrxCalendar, parse_calendar
from trading_system.marketdata.store import MarketDataStore
from trading_system.monitoring.liveness import write_heartbeat
from trading_system.reporting.day_summary import build_day_summary
from trading_system.storage.repository import Repository

from tests.support.gate_fakes import scenario_limits
from tests.support.sample_strategies import CAPS_ALL_SUPPORTED

ACCOUNT = "KR-PAPER-01"
DAY = date(2026, 10, 12)          # 월요일 (가상 달력: 휴장 없음)
SETTINGS = GateSettings(60, Decimal("0.3"), CostRates(Decimal("0"), Decimal("0")))
REF = dtime(8, 30)


def kst(hh: int, mm: int, ss: int = 0, d: date = DAY) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=KST).astimezone(UTC)


def calendar(holidays: list[date] | None = None) -> KrxCalendar:
    return KrxCalendar({2026: parse_calendar({"market": "KRX", "year": 2026, "source": "가상", "source_version": "t",
                                              "holidays": holidays or []}, 2026)})


class ClockOk:
    def clock_ok(self) -> bool | None:
        return True


class FakeRun:
    """run 프로세스 대역: 실제 엔진(build_engine, PAPER 저장소, fake 브로커). step()마다 정지 요청 파일 확인 → 시작(첫 회)
    → 하트비트 → tick. 실행 잠금(run.lock)을 실제 run처럼 쥔다."""

    def __init__(self, base: Path, clock: FixedClock, broker: FakeBroker, cal: KrxCalendar,
                 on_started: Callable[["FakeRun"], None] | None = None) -> None:
        self.base, self.clock, self.broker = base, clock, broker
        self.lock = acquire_instance_lock(base)
        self.repo = Repository(base / "trading.db", Mode.PAPER)
        self.engine = build_engine(mode=Mode.PAPER, repo=self.repo, clock=clock, broker=broker,
                                   store=MarketDataStore(cal, None), account=ACCOUNT, account_limits=scenario_limits(),
                                   gate_settings=SETTINGS, caps=CAPS_ALL_SUPPORTED, kill_file=base / "KILL",
                                   clock_health=ClockOk(), strategies=[], alert_thresholds=(10, 5))
        self.on_started = on_started
        self.started = False
        self.code: int | None = None
        self.stopped_by_file = False

    def alive(self) -> bool:
        return self.code is None

    def exit_code(self) -> int | None:
        return self.code

    def step(self) -> None:
        if self.code is not None:
            return
        stop = self.base / STOP_REQUEST
        if stop.exists():
            stop.unlink()
            self.stopped_by_file = True
            self.finish(0)
            return
        write_heartbeat(self.base / "heartbeat", self.clock.now())
        if not self.started:
            self.engine.start()
            self.started = True
            if self.on_started is not None:
                self.on_started(self)
            return
        self.engine.tick()

    def finish(self, code: int) -> None:
        self.code = code
        self.repo.close()
        self.lock.release()


class DeadRun:
    def __init__(self, code: int) -> None:
        self.code = code

    def alive(self) -> bool:
        return False

    def exit_code(self) -> int | None:
        return self.code


class Rig:
    def __init__(self, tmp: Path, *, start: datetime | None = None, holidays: list[date] | None = None,
                 preflight_rc: int = 0, on_started: Callable[[FakeRun], None] | None = None,
                 schedule: Schedule | None = None) -> None:
        self.base = tmp / "var" / "paper"
        self.base.mkdir(parents=True)
        self.clock = FixedClock(start or kst(6, 45))
        self.cal = calendar(holidays)
        self.broker = FakeBroker()
        self.broker.cash[ACCOUNT] = CashView(Decimal("10000000"), Decimal("10000000"))
        self.events: list[str] = []
        self.notes: list[tuple[str, str]] = []
        self.runs: list[FakeRun] = []
        self.preflight_rc = preflight_rc
        self.on_started = on_started
        self.hooks: list[tuple[datetime, Callable[[], None]]] = []
        self.logs: list[str] = []
        self.preflight_text = ("  실패: 시계 오차 1.4초 > 허용 1초 (가상)\n결과: 실패" if preflight_rc
                               else "결과: 통과")
        self.manual_race = False
        self.race_lock = None
        self.ap = Autopilot(Mode.PAPER, self.base, ACCOUNT, self.cal, schedule or Schedule(poll_seconds=30, day_poll_seconds=1800),
                            AutopilotDeps(clock=self.clock, sleep=self.sleep, notify=self.notify,
                                          preflight=self.preflight, launch=self.launch, review=self.review,
                                          backup=self.backup, log=self.logs.append),
                            reference_time=REF, rates=CostRates(Decimal("0"), Decimal("0")))

    # --- 주입 ------------------------------------------------------------------------------------
    def sleep(self, seconds: float) -> None:
        self.clock.advance(timedelta(seconds=seconds))
        for at, fn in list(self.hooks):
            if self.clock.now() >= at:
                self.hooks.remove((at, fn))
                fn()
        for r in self.runs:
            r.step()

    def notify(self, level: str, text: str) -> None:
        self.notes.append((level, text))

    def preflight(self) -> tuple[int, str]:
        self.events.append("preflight")
        return self.preflight_rc, self.preflight_text

    def launch(self):  # type: ignore[no-untyped-def]
        self.events.append("launch")
        if self.manual_race:   # 자동 운영 확인 직후 사용자가 수동 run을 켬 → 자식 run은 잠금에 막혀 코드 2로 바로 끝남
            self.race_lock = acquire_instance_lock(self.base)
            return DeadRun(2)
        r = FakeRun(self.base, self.clock, self.broker, self.cal, self.on_started)
        self.runs.append(r)
        return r

    def review(self) -> tuple[int, str]:
        self.events.append(f"review alive={self.runs[-1].alive() if self.runs else None}")
        return 0, "집계 완료: (가상)"

    def backup(self) -> tuple[int, str]:
        self.events.append("backup")
        return 0, "백업 완료: (가상)"

    # --- 조회 ------------------------------------------------------------------------------------
    def q(self, sql: str, params: tuple = ()) -> list[dict]:
        repo = Repository(self.base / "trading.db", Mode.PAPER)
        try:
            return repo.select(sql, params)
        finally:
            repo.close()

    def active(self) -> set[str]:
        return {r["halt_type"] for r in self.q("SELECT halt_type FROM halt WHERE state<>'RELEASED'")}

    def text(self) -> str:
        return "\n".join(f"{lv} {t}" for lv, t in self.notes)

    def auto_commands(self) -> list[dict]:
        return self.q("SELECT * FROM ops_command WHERE channel='AUTO' ORDER BY created_seq")


def kill_now(r: Rig) -> None:
    r.runs[-1].finish(1)


# --- V-201 모드 거부 (PAPER 외 자동 시작 없음) ------------------------------------------------------------

@pytest.mark.parametrize("argv,env,expect", [
    (["--mode", "LIVE"], {}, "PAPER 전용"),
    (["--mode", "BACKTEST"], {}, "PAPER 전용"),
    (["--mode", "paper"], {}, "PAPER 전용"),
    (["--mode", "PAPER"], {"TRADE_MODE": "LIVE"}, "TRADE_MODE"),
    (["--mode", "PAPER"], {"TRADE_MODE": "BACKTEST"}, "TRADE_MODE"),
])
def test_v201_non_paper_refused_before_anything(tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str],
                                                env: dict, expect: str) -> None:
    assert autopilot_main([*argv, "--root", str(tmp_path)], environ=env) == 2
    assert expect in capsys.readouterr().out
    assert not (tmp_path / "var").exists()           # 저장소·로그·잠금 어느 것도 만들지 않음


def test_v201_missing_mode_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as e:
        autopilot_main(["--root", str(tmp_path)], environ={})
    assert e.value.code == 2
    assert mode_refusal(None, {}) is not None
    assert not (tmp_path / "var").exists()


@pytest.mark.parametrize("mode", [Mode.LIVE, Mode.BACKTEST])
def test_v201_autopilot_class_refuses_non_paper(tmp_path: Path, mode: Mode) -> None:
    with pytest.raises(AutopilotRefused):
        Autopilot(mode, tmp_path, ACCOUNT, calendar(), Schedule(),
                  AutopilotDeps(clock=FixedClock(kst(6, 45)), sleep=lambda s: None, notify=lambda lv, t: None,
                                preflight=lambda: (0, ""), launch=lambda: None, review=lambda: (0, ""),  # type: ignore
                                backup=lambda: (0, "")), reference_time=REF, rates=None)


# --- V-202 휴장일·늦은 시작·달력 없음 ---------------------------------------------------------------------

def test_v202_holiday_exits_with_one_line(tmp_path: Path) -> None:
    r = Rig(tmp_path, holidays=[DAY])
    assert r.ap.run() == 0
    assert r.events == []                                   # 시작 점검·run 없음
    assert len(r.notes) == 1 and "휴장일" in r.notes[0][1] and "\n" not in r.notes[0][1]


def test_v202_late_start_after_deadline_does_nothing(tmp_path: Path) -> None:
    r = Rig(tmp_path, start=kst(9, 10))
    assert r.ap.run() == 1
    assert r.events == [] and "08:55" in r.text()


def test_v202_missing_calendar_refused(tmp_path: Path) -> None:
    r = Rig(tmp_path, start=kst(6, 45, d=date(2027, 1, 4)))
    assert r.ap.run() == 1
    assert r.events == [] and "달력" in r.text()


# --- V-203 시작 점검 실패 → run 미시작·알림 ---------------------------------------------------------------

def test_v203_preflight_failure_no_run_and_alert(tmp_path: Path) -> None:
    r = Rig(tmp_path, preflight_rc=1)
    assert r.ap.run() == 1
    assert r.events == ["preflight"]
    assert any(lv == "EMERGENCY" and "시작 점검 실패" in t and "시계 오차" in t for lv, t in r.notes), r.notes
    assert not (r.base / "trading.db").exists() or r.auto_commands() == []


# --- V-204 K8 자동 재개 성공 → 15:45 종료 절차 --------------------------------------------------------------

def test_v204_full_day_auto_resume_then_close_sequence(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    assert r.ap.run() == 0, r.notes
    [cmd] = r.auto_commands()
    assert cmd["actor"] == AUTO_ACTOR and cmd["command"] == "RESUME" and cmd["status"] == "DONE", cmd
    k8 = r.q("SELECT released_by, released_at FROM halt WHERE halt_type='K8'")
    assert k8 and k8[0]["released_by"] == f"{Channel.AUTO}:{AUTO_ACTOR}", k8
    assert datetime.fromisoformat(k8[0]["released_at"]) < kst(7, 0)
    recon = r.q("SELECT trigger, result FROM reconciliation ORDER BY created_seq")
    assert ("AUTO_RESUME", "MATCH") in {(x["trigger"], x["result"]) for x in recon}, recon
    # 마감 대조(CLOSE) 뒤에만 정지 요청, run이 그 파일로 정상 종료 → 그 뒤 paper-review → 백업
    close = [x for x in r.q("SELECT trigger, created_at FROM reconciliation WHERE trigger='CLOSE'")]
    assert close, recon
    run = r.runs[0]
    assert run.stopped_by_file and run.exit_code() == 0 and not (r.base / STOP_REQUEST).exists()
    assert r.events == ["preflight", "launch", "review alive=False", "backup"], r.events
    assert any("K8 자동 재개 완료" in t for _, t in r.notes), r.notes
    summary = r.notes[-1][1]
    assert "일일 요약" in summary and "정지" in summary and "불일치" in summary and "실현 손익" in summary
    assert "슬리피지" in summary and ACCOUNT not in summary
    assert r.clock.now() >= kst(15, 45)
    # 감사: 재개 기록이 자동 운영 채널로 남음 (원격·로컬과 구분)
    resumes = [e for e in r.q("SELECT actor, summary FROM audit_event WHERE kind='HALT_RESUME'")]
    assert resumes and '"channel": "AUTO"' in resumes[-1]["summary"], resumes


# --- V-205 K8 자동 재개 실패: 대조 불일치 → K4 → 자동 재개 중단 ------------------------------------------------

def test_v205_fresh_reconcile_mismatch_rejects_and_stops_retrying(tmp_path: Path) -> None:
    def external(run: FakeRun) -> None:     # 시작 대조 뒤 외부 체결 (자동 재개 직전 새 대조가 잡아야 함)
        no = run.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("5"))
        run.broker.fill(no)
        run.broker.drop_events()

    r = Rig(tmp_path, on_started=external)
    assert r.ap.run() == 1
    cmds = r.auto_commands()
    assert len(cmds) == 1 and cmds[0]["status"] == "REJECTED", cmds
    assert {"K8", "K4"} <= r.active()
    assert any("K4" in t and "자동 재개하지 않음" in t for _, t in r.notes), r.notes
    assert any("08:55" in t for _, t in r.notes), r.notes
    assert r.runs[0].stopped_by_file                    # 그날 매매 없음이어도 종료 절차는 그대로


# --- V-206 K8 자동 재개 실패: 조회 실패 → 5분 간격 재시도 → 회복 시 재개 / 08:55 시한 ------------------------

def test_v206_query_failure_retries_then_succeeds(tmp_path: Path) -> None:
    r = Rig(tmp_path, on_started=lambda run: setattr(run.broker, "disconnected", True))
    r.hooks.append((kst(6, 53), lambda: setattr(r.broker, "disconnected", False)))
    assert r.ap.run() == 0, r.notes
    cmds = r.auto_commands()
    assert [c["status"] for c in cmds][-1] == "DONE" and all(c["status"] == "REJECTED" for c in cmds[:-1]), cmds
    assert 2 <= len(cmds) <= 4, cmds
    times = [datetime.fromisoformat(c["created_at"]) for c in cmds]
    assert all(b - a >= timedelta(minutes=5) for a, b in zip(times, times[1:])), times
    fails = [t for lv, t in r.notes if "K8 자동 재개 실패" in t]
    assert len(fails) == 1, r.notes                     # 첫 실패만 알림 (재시도마다 보내지 않음)
    assert "K8" not in r.active()


def test_v206_deadline_0855_stops_retrying_and_alerts(tmp_path: Path) -> None:
    r = Rig(tmp_path, on_started=lambda run: setattr(run.broker, "disconnected", True))
    assert r.ap.run() == 1
    cmds = r.auto_commands()
    assert cmds and all(c["status"] == "REJECTED" for c in cmds), cmds
    assert len(cmds) <= Schedule().resume_max
    last = max(datetime.fromisoformat(c["created_at"]) for c in cmds)
    assert last < kst(8, 55)
    assert "K8" in r.active()
    assert any(lv == "WARNING" and "08:55" in t and "매매 없음" in t for lv, t in r.notes), r.notes
    # 실패한 자동 재개마다 정지 관리자 경고(텔레그램 대상 ALERT)를 쌓지 않는다
    alerts = r.q("SELECT summary FROM audit_event WHERE kind='ALERT' AND summary LIKE '%K8 재개 거부%'")
    assert alerts == []


def test_v206_auto_command_after_deadline_is_rejected_by_engine(tmp_path: Path) -> None:
    """시한(not_after)이 지난 자동 재개 명령은 처리 시점에 거부된다 — 08:55 뒤 늦게 처리돼 풀리지 않게."""
    r = Rig(tmp_path)
    run = r.launch()
    run.step()                                           # 시작 (K8)
    with run.repo.tx():
        run.repo.insert_command("auto-late", Channel.AUTO, AUTO_ACTOR, "RESUME",
                                {"account": ACCOUNT, "halt_type": "K8", "not_after": kst(6, 40).isoformat()},
                                r.clock.now())
    run.step()
    row = r.q("SELECT status, result FROM ops_command WHERE command_id='auto-late'")[0]
    assert row["status"] == "REJECTED" and "시한" in row["result"], row
    assert "K8" in r.active()
    assert r.q("SELECT 1 FROM reconciliation WHERE trigger='AUTO_RESUME'") == []   # 대조도 하지 않음
    run.finish(0)


# --- V-207 사람 확인 정지(K1·K4)가 있으면 자동 재개 안 함 --------------------------------------------------

@pytest.mark.parametrize("k,actor", [(HaltType.K1, "LOCAL:user"), (HaltType.K4, "reconciler")])
def test_v207_human_halts_block_auto_resume(tmp_path: Path, k: HaltType, actor: str) -> None:
    r = Rig(tmp_path, on_started=lambda run: run.engine.halts.activate(k, actor, "가상: 전날 정지", ACCOUNT))
    assert r.ap.run() == 1
    assert r.auto_commands() == []                       # 명령 자체를 넣지 않음
    assert {"K8", k.value} <= r.active()
    hits = [t for lv, t in r.notes if k.value in t and "자동 재개하지 않음" in t]
    assert len(hits) == 1, r.notes                       # 한 번만 알림
    assert any("08:55" in t for _, t in r.notes)


# --- V-208 정상 종료 장치: 마감 대조 미완료·전송 중 의도 → 시한까지 기다린 뒤 경고와 함께 종료 ------------------------

def test_v208_close_reconcile_missing_waits_then_stops_with_warning(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.hooks.append((kst(15, 0), lambda: setattr(r.broker, "disconnected", True)))   # 마감 대조 조회 실패
    assert r.ap.run() == 1
    sched = Schedule()
    assert r.clock.now() >= kst(15, 45) + sched.close_wait
    assert any(lv == "WARNING" and "마감 대조 미완료" in t for lv, t in r.notes), r.notes
    assert r.runs[0].stopped_by_file
    assert r.events[-2:] == ["review alive=False", "backup"]


def test_v208_run_dies_midday_alerts_and_still_reviews(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.hooks.append((kst(11, 0), lambda: kill_now(r)))
    assert r.ap.run() == 1
    assert any(lv == "EMERGENCY" and "run 프로세스가 종료" in t for lv, t in r.notes), r.notes
    assert r.events[-2:] == ["review alive=False", "backup"]
    assert r.clock.now() < kst(12, 0)                    # 15:45를 기다리지 않음
    assert "일일 요약" in r.notes[-1][1]


def test_v208_run_main_stop_request_file_same_path_as_ctrl_c(tmp_path: Path) -> None:
    """실제 run_main: 정지 요청 파일 → 루프 종료 → Ctrl+C와 같은 정리(스레드 join·저장소 닫기·잠금 해제)."""
    root = make_bt_root(tmp_path, Mode.BACKTEST)
    base = root / "var" / "backtest"
    base.mkdir(parents=True)
    (base / STOP_REQUEST).write_text("stale", encoding="utf-8")   # 이전 정지 요청이 남아 있어도 시작은 한다

    def writer() -> None:
        end = time.monotonic() + 20
        while time.monotonic() < end and not (base / "heartbeat").exists():
            time.sleep(0.05)
        time.sleep(0.3)
        (base / STOP_REQUEST).write_text("STOP\n", encoding="utf-8")

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    started = time.monotonic()
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=25) == 0
    t.join(5)
    assert time.monotonic() - started < 20, "정지 요청 파일로 멈추지 않음(max_seconds까지 돎)"
    assert (base / "heartbeat").exists()
    assert not (base / STOP_REQUEST).exists()            # 처리한 요청은 지운다(다음 시작이 바로 멈추지 않게)
    acquire_instance_lock(base).release()                # 잠금 해제됨


# --- V-209 중복 실행 거부 -----------------------------------------------------------------------------

def test_v209_refused_when_manual_run_holds_lock(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    lock = acquire_instance_lock(r.base)                 # 수동 run 실행 중
    try:
        assert r.ap.run() == 2
    finally:
        lock.release()
    assert r.events == [] and any("이미 실행 중" in t for _, t in r.notes), r.notes


def test_v209_second_autopilot_refused(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    lock = acquire_instance_lock(r.base, AUTOPILOT_LOCK)
    try:
        assert r.ap.run() == 2
    finally:
        lock.release()
    assert r.events == []


# --- V-210 일일 요약 내용 (민감값 없음) ----------------------------------------------------------------------

def test_v210_day_summary_has_counts_without_account_or_amounts(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    run = r.launch()
    run.step()
    with run.repo.tx():
        run.repo.audit(r.clock.now(), "DAILY_BASE", "monitor", ACCOUNT,
                       {"account": ACCOUNT, "trade_date": DAY.isoformat(), "cash": "10000000", "value": "10000000",
                        "at": r.clock.now().isoformat()})
    run.engine.halts.activate(HaltType.K5, "broker", "가상 끊김", ACCOUNT)
    run.finish(0)
    text = build_day_summary(r.base / "trading.db", Mode.PAPER, ACCOUNT, DAY, REF, CostRates(Decimal("0"), Decimal("0")),
                             ["06:52 K8 자동 재개"])
    assert f"[{DAY} 일일 요약" in text
    assert "체결 0건" in text and "K5" in text and "K8" in text
    assert "결과 불명 0건" in text and "열린 불일치 0건" in text
    assert "실현 손익" in text and "슬리피지" in text
    assert "06:52 K8 자동 재개" in text
    assert ACCOUNT not in text and "10000000" not in text and "원" not in text.replace("원인", "")


def test_v210_day_summary_lists_fills_by_instrument_side_qty(env: Env, tmp_path: Path) -> None:
    enter(env, "TST-A", "10", "e1")
    day = env.clock.now().astimezone(KST).date()
    text = build_day_summary(tmp_path / "trading.db", Mode.BACKTEST, ENV_ACCOUNT, day, REF, None, [])
    assert "체결 1건" in text and "TST-A 매수 10" in text, text
    assert ENV_ACCOUNT not in text
    assert "비용 미반영" in text                          # 비용 요율을 못 읽으면 그렇게 표시


# --- V-211 운영 명령: 자동 운영 채널은 K8 재개만 ---------------------------------------------------------

@pytest.mark.parametrize("command,args", [
    ("KILL", {"account": ACCOUNT}),
    ("RESUME", {"account": ACCOUNT, "halt_type": "K4"}),
    ("RESUME", {"account": ACCOUNT, "halt_type": "K1"}),
    ("RECONCILE", {"account": ACCOUNT}),
    ("EXIT", {"account": ACCOUNT, "instrument": "TST-A", "quantity": "1"}),
])
def test_v211_auto_channel_only_resumes_k8(tmp_path: Path, command: str, args: dict) -> None:
    r = Rig(tmp_path)
    run = r.launch()
    run.step()
    run.engine.halts.activate(HaltType.K4, "reconciler", "가상", ACCOUNT)
    run.engine.halts.activate(HaltType.K1, "LOCAL:user", "가상", ACCOUNT)
    with run.repo.tx():
        run.repo.insert_command("auto-x", Channel.AUTO, AUTO_ACTOR, command,
                                {**args, "not_after": kst(8, 55).isoformat()}, r.clock.now())
    run.step()
    row = r.q("SELECT status, result FROM ops_command WHERE command_id='auto-x'")[0]
    assert row["status"] == "REJECTED" and "K8" in row["result"], row
    assert {"K1", "K4", "K8"} <= r.active()
    run.finish(0)


def test_v211_halt_manager_auto_k8_needs_fresh_reconcile(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    run = r.launch()
    run.step()
    res = run.engine.halts.resume(HaltType.K8, ACCOUNT, Channel.AUTO, AUTO_ACTOR)
    assert not res.ok and "새 대조" in res.reason
    res = run.engine.halts.resume(HaltType.K4, ACCOUNT, Channel.AUTO, AUTO_ACTOR, fresh_recon="rc-x")
    assert not res.ok and "K8" in res.reason
    run.finish(0)


# === 재검토 반영 (M1·M2·L3~L6) · A-079 =====================================================================

# --- V-213 자동 운영 예외 → run을 고아로 두지 않음 / 어제 run이 남아 있음 -------------------------------------

def test_v213_autopilot_crash_alerts_writes_stop_and_logs_traceback(tmp_path: Path) -> None:
    r = Rig(tmp_path)

    def boom() -> None:
        raise RuntimeError("가상 결함")

    r.hooks.append((kst(10, 0), boom))
    assert r.ap.run() == 1
    assert any(lv == "EMERGENCY" and "자동 운영 오류" in t and "RuntimeError" in t for lv, t in r.notes), r.notes
    assert (r.base / STOP_REQUEST).exists()               # run에 정지 요청
    assert any("Traceback" in m for m in r.logs)          # pythonw에는 표준 오류가 없으므로 일 로그에
    run = r.runs[0]
    run.step()
    assert run.stopped_by_file and not run.alive()


def test_v213_yesterdays_run_still_running_is_stopped(tmp_path: Path) -> None:
    r = Rig(tmp_path, start=kst(6, 45, d=date(2026, 10, 9)))
    run = r.launch()
    run.step()                                            # 어제 시작한 run (STARTUP 대조가 어제 날짜)
    r.clock._now = kst(6, 45)  # noqa: SLF001
    assert r.ap.run() == 2
    assert any(lv == "EMERGENCY" and "어제 run이 아직 실행 중" in t for lv, t in r.notes), r.notes
    assert (r.base / STOP_REQUEST).exists()
    assert "preflight" not in r.events
    run.step()
    assert not run.alive()


# --- V-214 시한(not_after)을 재개 저장 단위 안에서 다시 확인 ---------------------------------------------------

def test_v214_deadline_rechecked_inside_resume_transaction(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    run = r.launch()
    run.step()
    ops = run.engine.ops
    orig = ops._reconciler.run  # noqa: SLF001

    def slow(account: str, trigger: str):  # type: ignore[no-untyped-def]
        r.clock.advance(timedelta(seconds=30))            # 새 대조가 오래 걸려 시한을 넘김
        return orig(account, trigger)

    ops._reconciler.run = slow  # type: ignore[method-assign]  # noqa: SLF001
    with run.repo.tx():
        run.repo.insert_command("auto-slow", Channel.AUTO, AUTO_ACTOR, "RESUME",
                                {"account": ACCOUNT, "halt_type": "K8",
                                 "not_after": (r.clock.now() + timedelta(seconds=5)).isoformat()}, r.clock.now())
    run.step()
    row = r.q("SELECT status, result FROM ops_command WHERE command_id='auto-slow'")[0]
    assert row["status"] == "REJECTED" and "시한" in row["result"], row
    assert "K8" in r.active()
    run.finish(0)


# --- V-215 루트 .env의 TRADE_MODE도 PAPER여야 함 ----------------------------------------------------------

@pytest.mark.parametrize("dotenv,expect", [("TRADE_MODE=BACKTEST\n", ".env"), ("TRADE_MODE=LIVE\n", ".env"),
                                           (None, "TRADE_MODE")])
def test_v215_dotenv_trade_mode_must_be_paper(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                              dotenv: str | None, expect: str) -> None:
    if dotenv is not None:
        (tmp_path / ".env").write_text(dotenv, encoding="utf-8")
    assert autopilot_main(["--mode", "PAPER", "--root", str(tmp_path)], environ={}) == 2
    out = capsys.readouterr().out
    assert expect in out and "PAPER" in out, out
    assert not (tmp_path / "var").exists()


# --- V-216 자동 운영 알림 가림 처리 --------------------------------------------------------------------------

def test_v216_notifications_are_masked(tmp_path: Path) -> None:
    r = Rig(tmp_path, preflight_rc=1)
    r.preflight_text = "  실패: 계좌 12345678 조회 거부 token=abcd1234efgh\n결과: 실패"
    assert r.ap.run() == 1
    text = r.text()
    assert "12345678" not in text and "abcd1234efgh" not in text and "실패" in text, text


# --- V-217 마감 대조 불일치는 "완료"가 아니라 확인 필요 -------------------------------------------------------

def test_v217_close_mismatch_flagged_in_summary(tmp_path: Path) -> None:
    r = Rig(tmp_path)

    def external() -> None:
        no = r.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("5"))
        r.broker.fill(no)
        r.broker.drop_events()

    r.hooks.append((kst(12, 0), external))
    assert r.ap.run() == 1
    summary = r.notes[-1][1]
    assert "확인 필요" in summary and "마감 대조 불일치" in summary, summary
    assert not any("마감 대조 완료" in t for _, t in r.notes), r.notes
    assert r.runs[0].stopped_by_file


# --- V-218 수동 run과 경합 → 경고, review·백업 없음 -------------------------------------------------------------

def test_v218_manual_run_race_reports_warning_not_death(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.manual_race = True
    try:
        assert r.ap.run() == 2
    finally:
        r.race_lock.release()
    assert any(lv == "WARNING" and "수동 run이 이미 실행 중" in t for lv, t in r.notes), r.notes
    assert not any(lv == "EMERGENCY" for lv, _ in r.notes), r.notes
    assert r.events == ["preflight", "launch"]


# --- V-219 A-079 운영 정지 진입점 (PAPER 전용, STOP 파일) -------------------------------------------------------

@pytest.mark.parametrize("mode", ["LIVE", "BACKTEST", "paper"])
def test_v219_ops_stop_refuses_non_paper(tmp_path: Path, mode: str) -> None:
    assert ops_stop_main(["--mode", mode, "--root", str(tmp_path)], environ={}) == 2
    assert not (tmp_path / "var").exists()


def test_v219_ops_stop_only_when_run_is_running(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = tmp_path / "var" / "paper"
    base.mkdir(parents=True)
    assert ops_stop_main(["--mode", "PAPER", "--root", str(tmp_path), "--wait", "0"], environ={}) == 1
    assert not (base / STOP_REQUEST).exists() and "실행 중이 아님" in capsys.readouterr().out
    lock = acquire_instance_lock(base)                  # run 실행 중
    try:
        assert ops_stop_main(["--mode", "PAPER", "--root", str(tmp_path), "--wait", "0"], environ={}) == 0
    finally:
        lock.release()
    assert (base / STOP_REQUEST).exists() and "정지 요청" in capsys.readouterr().out


def test_day3_summary_base_is_cash_plus_value(tmp_path: Path) -> None:
    """3일차(2026-10-08) 결함: 일일 요약이 기준 금액 대신 보유 평가액(value)만 읽어, 보유 0인 날 "기록 없음"으로 표시.
    기준 금액 = cash + value (watchers DailyRiskTracker와 같은 정의)."""
    from trading_system.reporting.day_summary import _base_value

    r = Rig(tmp_path)
    run = r.launch()
    run.step()
    with run.repo.tx():
        run.repo.audit(r.clock.now(), "DAILY_BASE", "monitor", ACCOUNT,
                       {"trade_date": DAY.isoformat(), "cash": "99721280", "value": "0", "at": r.clock.now().isoformat()})
    assert _base_value(run.repo, ACCOUNT, DAY) == Decimal("99721280")
    with run.repo.tx():
        run.repo.audit(r.clock.now(), "DAILY_BASE", "monitor", ACCOUNT,
                       {"trade_date": DAY.isoformat(), "cash": "1000", "value": "500", "at": r.clock.now().isoformat()})
    assert _base_value(run.repo, ACCOUNT, DAY) == Decimal("1500")  # 같은 날 마지막 기록
    run.finish(0)
