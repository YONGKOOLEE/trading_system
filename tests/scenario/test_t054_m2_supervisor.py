"""CP-037 T-054 — M2 감독 프로세스 (D-223·D-225~D-228·D-230, 승인 A-113 Q-1 (가)·Q-2 (나)). V-296~V-301.

run 프로세스는 같은 프로세스 안의 가짜(FakeM2Run: 실제 build_engine(market="M2", PAPER 저장소) + fake 현물 브로커)로
대신하고, 감독의 sleep이 가상 시계를 앞당기며 엔진 tick을 돌린다(결정적, 실제 대기 없음). 실제 프로세스를 띄우거나 끝내지
않는다 — 종료는 가짜의 종료 코드로만 흉내 낸다. 바이낸스·텔레그램 연결 없음(텔레그램은 기록용 가짜 notify).
M2 추가 재개 조건(D-226)은 M2ResumeConditions에 가짜 서버 시각·스트림 상태를 넣어 run 쪽 처리기에 연결한다.
모든 값은 가상이다(계좌 별칭 BN-PAPER-01, 가상 심볼 TSTA-USDT).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_tm2_08_market_split import make_m2_root
from tests.support.m2_engine_world import CAPS_M2, PAIRS, SETTINGS, SYM, ClockOk
from tests.support.m2_fakes import FEE_RATE, M2_ORDER_POLICY, QUOTE_ASSET, m2_limits, symbol_rules
from trading_system.app.instance_lock import acquire_instance_lock
from trading_system.app.m2_resume_conditions import M2ResumeConditions
from trading_system.app.m2_supervisor import (
    AUDIT_KIND, M2_SUPERVISOR_LOCK, SUPERVISOR_ACTOR, SUPERVISOR_STOP, M2Schedule, M2Supervisor, M2SupervisorDeps,
    M2SupervisorRefused, m2_supervisor_main, request_supervisor_stop,
)
from trading_system.app.main import STOP_REQUEST
from trading_system.app.wiring import SpotInputs, build_engine
from trading_system.broker.fake.broker import FakeSpotFees
from trading_system.broker.fake.spot import FakeSpotBroker
from trading_system.domain.clock import FixedClock
from trading_system.domain.enums import HaltType, Mode
from trading_system.domain.market import Quote
from trading_system.marketdata.spot_facts import SpotFactsStore
from trading_system.monitoring.liveness import write_heartbeat
from trading_system.storage.repository import OrderIdentity, Repository

ACCOUNT = "BN-PAPER-01"
T0 = datetime(2026, 10, 10, 10, 0, tzinfo=UTC)     # 토요일 UTC — 24/7
HOSTS = frozenset({"testnet.binance.vision", "ws-api.testnet.binance.vision"})


def utc(d: int, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(2026, 10, d, hh, mm, ss, tzinfo=UTC)


class FakeM2Run:
    """run 프로세스 대역: 실제 M2 엔진(PAPER 저장소, fake 현물 브로커). step()마다 정지 요청 파일 → 시작(첫 회) → 하트비트
    → tick. 실행 잠금(run.lock)을 실제 run처럼 쥔다. 종료는 finish(코드)로만(실제 프로세스 없음)."""

    def __init__(self, rig: "Rig") -> None:
        self.rig = rig
        self.lock = acquire_instance_lock(rig.base)
        self.repo = Repository(rig.base / "trading.db", Mode.PAPER, order_identity=OrderIdentity.SYMBOL)
        self.engine = build_engine(mode=Mode.PAPER, repo=self.repo, clock=rig.clock, broker=rig.broker, store=None,
                                   account=ACCOUNT, account_limits=m2_limits(), gate_settings=SETTINGS, caps=CAPS_M2,
                                   strategies=[], kill_file=rig.base / "KILL", clock_health=ClockOk(),
                                   alert_thresholds=(10, 5), market="M2",
                                   spot=SpotInputs(rig.facts, PAIRS, QUOTE_ASSET, M2_ORDER_POLICY,
                                                   cash_max_age=timedelta(minutes=3)))
        self.engine.ops.auto_resume_check = rig.conditions.problems   # D-226 (조립 지점 _run_m2와 같은 연결)
        if not rig.runs:   # 기준선 확정(D-103·D-210 — 실운영은 사람이 initial-baseline으로). 거래소 USDT와 같은 값
            self.engine.account_state.set_baseline(ACCOUNT, Decimal("10000"), rig.clock.now())  # type: ignore[attr-defined]
        self.started = False
        self.code: int | None = None
        self.frozen = False
        self.stopped_by_file = False

    def alive(self) -> bool:
        return self.code is None

    def exit_code(self) -> int | None:
        return self.code

    def step(self) -> None:
        if self.code is not None or self.frozen:
            return
        stop = self.rig.base / STOP_REQUEST
        if stop.exists():
            stop.unlink()
            self.stopped_by_file = True
            self.finish(0)
            return
        write_heartbeat(self.rig.base / "heartbeat", self.rig.clock.now())
        if not self.started:
            self.engine.start()
            self.started = True
            if self.rig.on_started is not None:
                self.rig.on_started(self)
            return
        self.engine.fast_kill()      # run 안 감시 스레드 몫(KILL 파일 → K1 즉시 기록, D-038)
        self.engine.tick()

    def finish(self, code: int) -> None:
        self.code = code
        self.repo.close()
        self.lock.release()


class Rig:
    def __init__(self, tmp: Path, *, start: datetime = T0, schedule: M2Schedule | None = None,
                 preflight_rc: int | list[int] = 0, review_rc: int = 0, backup_rc: int = 0) -> None:
        self.base = tmp / "var" / "paper"
        self.base.mkdir(parents=True)
        self.clock = FixedClock(start)
        self.broker = FakeSpotBroker(now=self.clock.now)
        self.broker.grant = {QUOTE_ASSET: Decimal("10000")}
        self.broker.spot_fees = FakeSpotFees(rate=FEE_RATE, pairs=PAIRS)
        self.broker.ioc_limit = True
        self.facts = SpotFactsStore(now=self.clock.now, universe=(SYM,))
        self.facts.load_rules({SYM: symbol_rules(SYM)})
        self.offset_ms = 10
        self.stream_online = True
        self.feed_ok = True
        self.conditions = M2ResumeConditions(server_offset_ms=lambda: self.offset_ms, warn_ms=500, facts=self.facts,
                                             symbols=(SYM,), stream_online=lambda: self.stream_online,
                                             now=self.clock.now, max_quote_age=timedelta(seconds=60))
        self.on_started: Callable[[FakeM2Run], None] | None = None
        self.runs: list[FakeM2Run] = []
        self.notes: list[tuple[str, str]] = []
        self.events: list[str] = []
        self.logs: list[str] = []
        self.hooks: list[tuple[datetime, Callable[[], None]]] = []
        self.preflight_rcs = list(preflight_rc) if isinstance(preflight_rc, list) else [preflight_rc]
        self.review_rc, self.backup_rc = review_rc, backup_rc
        self.sup = M2Supervisor(Mode.PAPER, self.base, ACCOUNT,
                                schedule or M2Schedule(poll_seconds=30, idle_poll_seconds=60),
                                M2SupervisorDeps(clock=self.clock, sleep=self.sleep, notify=self.notify,
                                                 preflight=self.preflight, launch=self.launch, review=self.review,
                                                 backup=self.backup, log=self.logs.append))
        self.quote()

    # --- 주입 ---
    def quote(self) -> None:
        if self.feed_ok:
            self.facts.update_quote(Quote(SYM, Decimal("10000.00"), self.clock.now()))

    def sleep(self, seconds: float) -> None:
        self.clock.advance(timedelta(seconds=seconds))
        self.quote()
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
        rc = self.preflight_rcs.pop(0) if len(self.preflight_rcs) > 1 else self.preflight_rcs[0]
        return rc, ("결과: 실패 (가상)" if rc else "결과: 통과")

    def launch(self) -> FakeM2Run:
        self.events.append("launch")
        r = FakeM2Run(self)
        self.runs.append(r)
        return r

    def review(self) -> tuple[int, str]:
        self.events.append("review")
        return self.review_rc, "집계 완료 (가상)"

    def backup(self) -> tuple[int, str]:
        self.events.append("backup")
        return self.backup_rc, "백업 완료 (가상)"

    # --- 조작·조회 ---
    def at(self, when: datetime, fn: Callable[[], None]) -> None:
        self.hooks.append((when, fn))

    def stop_supervisor_at(self, when: datetime) -> None:
        self.at(when, lambda: request_supervisor_stop(self.base, self.clock.now()))

    def q(self, sql: str, params: tuple = ()) -> list[dict]:
        repo = Repository(self.base / "trading.db", Mode.PAPER, order_identity=OrderIdentity.SYMBOL)
        try:
            return repo.select(sql, params)
        finally:
            repo.close()

    def active(self) -> set[str]:
        return {r["halt_type"] for r in self.q("SELECT halt_type FROM halt WHERE state<>'RELEASED'")}

    def auto_commands(self) -> list[dict]:
        return self.q("SELECT * FROM ops_command WHERE channel='AUTO' ORDER BY created_seq")

    def sup_events(self) -> list[str]:
        import json
        return [json.loads(r["summary"])["event"] for r in self.q("SELECT summary FROM audit_event WHERE kind=? "
                                                                 "ORDER BY seq", (AUDIT_KIND,))]

    def text(self) -> str:
        return "\n".join(f"{lv} {t}" for lv, t in self.notes)

    def levels(self, part: str) -> list[str]:
        return [lv for lv, t in self.notes if part in t]


def kill(r: Rig, code: int = 1) -> Callable[[], None]:
    return lambda: r.runs[-1].finish(code)


# --- V-296 시작·거부 -------------------------------------------------------------------------------------

def _root(tmp: Path, *, telegram: str = "\n[telegram]\nenabled = true\n", keys: bool = True,
          m2_keys: bool = True) -> Path:
    root = make_m2_root(tmp, Mode.PAPER, symbols='quote_asset = "USDT"\nsymbols = ["BTCUSDT"]\n')
    app = root / "config" / "m2" / "app.paper.toml"
    app.write_text(app.read_text(encoding="utf-8") + "\n[paper]\nconnection_approved = true\n" + telegram,
                   encoding="utf-8")
    lines = ["TRADE_MODE=PAPER"]
    if keys:
        lines += ["BINANCE_TESTNET_API_KEY=synthetic-k", "BINANCE_TESTNET_SECRET_KEY=synthetic-s"]
    if m2_keys:
        lines += ["M2_TELEGRAM_BOT_TOKEN=synthetic-token", "M2_TELEGRAM_CHAT_ID=12345"]
    (root / ".env.paper").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / ".env").write_text("TRADE_MODE=PAPER\n", encoding="utf-8")
    return root


def _nothing_created(root: Path) -> bool:
    return not (root / "var").exists()


@pytest.mark.parametrize("argv,env,expect", [
    (["--mode", "LIVE", "--market", "M2"], {}, "PAPER 전용"),
    (["--mode", "BACKTEST", "--market", "M2"], {}, "PAPER 전용"),
    (["--mode", "paper", "--market", "M2"], {}, "PAPER 전용"),
    (["--mode", "PAPER", "--market", "M1"], {}, "M2 전용"),
    (["--mode", "PAPER", "--market", "M3"], {}, "M2 전용"),
    (["--mode", "PAPER", "--market", "M2"], {"TRADE_MODE": "LIVE"}, "TRADE_MODE"),
])
def test_v296a_mode_or_market_mismatch_refused_creates_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                                               argv: list[str], env: dict, expect: str) -> None:
    root = _root(tmp_path)
    assert m2_supervisor_main([*argv, "--root", str(root)], environ=env, hosts=HOSTS) == 2
    assert expect in capsys.readouterr().out
    assert _nothing_created(root)


def test_v296a_root_env_trade_mode_must_be_paper(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _root(tmp_path)
    (root / ".env").write_text("TRADE_MODE=BACKTEST\n", encoding="utf-8")
    assert m2_supervisor_main(["--mode", "PAPER", "--market", "M2", "--root", str(root)], environ={},
                              hosts=HOSTS) == 2
    assert "TRADE_MODE" in capsys.readouterr().out and _nothing_created(root)


def test_v296b_m1_checkout_refused_creates_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tests.scenario.test_t020_main import make_root
    root = make_root(tmp_path, Mode.PAPER)        # M1 체크아웃 흉내(config/m2 없음)
    (root / ".env").write_text("TRADE_MODE=PAPER\n", encoding="utf-8")
    assert m2_supervisor_main(["--mode", "PAPER", "--market", "M2", "--root", str(root)], environ={},
                              hosts=HOSTS) == 2
    assert "설정" in capsys.readouterr().out
    assert not (root / "var" / "paper" / M2_SUPERVISOR_LOCK).exists()
    assert not (root / "var" / "paper" / "trading.db").exists()


def test_v296b_hosts_outside_testnet_table_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _root(tmp_path)
    for hosts in (None, frozenset({"api.binance.com"})):
        assert m2_supervisor_main(["--mode", "PAPER", "--market", "M2", "--root", str(root)], environ={},
                                  hosts=hosts) == 2
    out = capsys.readouterr().out
    assert "접속 도메인" in out and _nothing_created(root)


@pytest.mark.parametrize("telegram,m2_keys,expect", [
    ("", True, "텔레그램"),                                        # [telegram] 없음 = 꺼짐
    ("\n[telegram]\nenabled = false\n", True, "텔레그램"),
    ("\n[telegram]\nenabled = true\n", False, "M2_TELEGRAM_BOT_TOKEN"),
])
def test_v296c_telegram_off_refused_creates_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                                    telegram: str, m2_keys: bool, expect: str) -> None:
    root = _root(tmp_path, telegram=telegram, m2_keys=m2_keys)
    assert m2_supervisor_main(["--mode", "PAPER", "--market", "M2", "--root", str(root)], environ={},
                              hosts=HOSTS) == 2
    out = capsys.readouterr().out
    assert expect in out and _nothing_created(root)
    assert "synthetic-token" not in out


def test_v296_main_valid_settings_reach_supervisor(tmp_path: Path) -> None:
    """설정이 모두 맞으면 감독 본체까지 간다 — 시험은 가짜 의존성으로 바꿔 끼운다(실제 run·텔레그램 없음)."""
    root = _root(tmp_path)
    seen: dict = {}

    def make_deps(root_: Path, base: Path, cfg, log):  # type: ignore[no-untyped-def]
        seen["account"] = cfg.account
        seen["base"] = base
        return M2SupervisorDeps(clock=FixedClock(T0), sleep=lambda s: None, notify=lambda lv, t: seen.setdefault(
            "notes", []).append(t), preflight=lambda: (1, "결과: 실패 (가상)"), launch=lambda: None,  # type: ignore
                                review=lambda: (0, ""), backup=lambda: (0, ""), log=log)
    assert m2_supervisor_main(["--mode", "PAPER", "--market", "M2", "--root", str(root)], environ={},
                              hosts=HOSTS, make_deps=make_deps) == 2      # 가짜 시작 점검 실패 → 코드 2
    assert seen["account"] == ACCOUNT and seen["base"] == root.resolve() / "var" / "paper"
    assert any("시작 점검 실패" in t for t in seen["notes"])


@pytest.mark.parametrize("mode", [Mode.LIVE, Mode.BACKTEST])
def test_v296_class_refuses_non_paper(tmp_path: Path, mode: Mode) -> None:
    with pytest.raises(M2SupervisorRefused):
        M2Supervisor(mode, tmp_path, ACCOUNT, M2Schedule(),
                     M2SupervisorDeps(clock=FixedClock(T0), sleep=lambda s: None, notify=lambda lv, t: None,
                                      preflight=lambda: (0, ""), launch=lambda: None,  # type: ignore[arg-type,return-value]
                                      review=lambda: (0, ""), backup=lambda: (0, "")))


def test_v296d_supervisor_lock_held_exits_immediately(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    held = acquire_instance_lock(r.base, M2_SUPERVISOR_LOCK)
    try:
        assert r.sup.run() == 0                      # 매일 보조 트리거의 정상 경우 — 조용히 끝남
    finally:
        held.release()
    assert r.events == [] and r.notes == []


def test_v296e_run_already_running_warns_and_starts_nothing(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    manual = acquire_instance_lock(r.base)           # 수동 run이 실행 잠금을 쥠
    try:
        assert r.sup.run() == 2
    finally:
        manual.release()
    assert "launch" not in r.events and "preflight" not in r.events
    assert r.levels("run이 이미 실행 중") == ["WARNING"]


def test_v296f_preflight_failure_emergency_no_run(tmp_path: Path) -> None:
    r = Rig(tmp_path, preflight_rc=1)
    assert r.sup.run() == 2
    assert r.events == ["preflight"]
    assert r.levels("시작 점검 실패") == ["EMERGENCY"]


# --- V-297 K8 자동 재개 M2 --------------------------------------------------------------------------------

def test_v297a_conditions_met_resume_with_this_reconcile_evidence(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.stop_supervisor_at(T0 + timedelta(minutes=10))
    assert r.sup.run() == 0
    [cmd] = r.auto_commands()
    assert cmd["status"] == "DONE" and cmd["actor"] == SUPERVISOR_ACTOR
    import json
    args = json.loads(cmd["args"])
    assert args["halt_type"] == "K8" and args["account"] == ACCOUNT
    assert datetime.fromisoformat(args["not_after"]) - datetime.fromisoformat(cmd["created_at"]) == \
        timedelta(minutes=10)                                          # D-226 not_after = 넣은 시각 + 10분
    [k8] = r.q("SELECT * FROM halt WHERE halt_type='K8'")
    assert k8["state"] == "RELEASED" and k8["released_by"] == f"AUTO:{SUPERVISOR_ACTOR}"
    recon = r.q("SELECT recon_id FROM reconciliation WHERE trigger='AUTO_RESUME'")
    assert len(recon) == 1 and recon[0]["recon_id"] in k8["conditions"]   # 이번 대조 ID가 증거
    assert r.levels("K8 자동 재개 완료") == ["INFO"]
    assert "K8_AUTO_RESUMED" in r.sup_events()


def test_v297b_human_halt_k4_active_no_command_and_warning(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.on_started = lambda run: run.engine.halts.activate(HaltType.K4, "reconciler", "가상 불일치", ACCOUNT)
    r.stop_supervisor_at(T0 + timedelta(minutes=30))
    assert r.sup.run() == 0
    assert r.auto_commands() == []
    assert "K4" in r.active() and "K8" in r.active()
    assert any("K4" in t and lv == "EMERGENCY" for lv, t in r.notes)


def _stale_feed(r: "Rig") -> None:
    r.feed_ok = False
    r.facts.update_quote(Quote(SYM, Decimal("10000.00"), r.clock.now() - timedelta(seconds=120)))


@pytest.mark.parametrize("setup,reason", [
    (lambda r: setattr(r, "offset_ms", 600), "서버 시각 오차 600ms"),
    (lambda r: setattr(r, "offset_ms", -600), "서버 시각 오차 -600ms"),
    (lambda r: setattr(r, "stream_online", False), "사용자 스트림 미연결"),
    (_stale_feed, "초 지남(기준 60초)"),
    (lambda r: r.facts.clear_rules(), "exchangeInfo 규칙"),
])
def test_v297cd_m2_conditions_rejected_with_reason_no_reconcile(tmp_path: Path, setup, reason: str) -> None:  # type: ignore[no-untyped-def]
    r = Rig(tmp_path)
    setup(r)
    r.stop_supervisor_at(T0 + timedelta(minutes=25))
    assert r.sup.run() == 0
    cmds = r.auto_commands()
    assert cmds and all(c["status"] == "REJECTED" and reason in c["result"] for c in cmds)
    assert r.q("SELECT 1 FROM reconciliation WHERE trigger='AUTO_RESUME'") == []   # 조건 미충족 → 새 대조 없음
    assert "K8" in r.active()
    assert r.levels("K8 자동 재개 실패") == ["WARNING"]                            # 첫 실패만 알림


def test_v297f_twelve_rejections_one_warning_then_stop(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.offset_ms = 900
    r.stop_supervisor_at(T0 + timedelta(hours=3))
    assert r.sup.run() == 0
    cmds = r.auto_commands()
    assert len(cmds) == 12 and all(c["status"] == "REJECTED" for c in cmds)
    times = [datetime.fromisoformat(c["created_at"]) for c in cmds]
    assert all(b - a >= timedelta(minutes=5) for a, b in zip(times, times[1:]))   # 5분 간격
    assert len(r.levels("K8 자동 재개 12회 실패")) == 1 and r.levels("K8 자동 재개 12회 실패") == ["WARNING"]
    assert "K8" in r.active()
    assert r.events.count("launch") == 1 and r.runs[-1].stopped_by_file    # run은 감독 정지 요청까지 그대로


def test_v297g_expired_not_after_rejected_without_reconcile(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.runs.append(run := FakeM2Run(r))
    run.step()                                       # 시작(K8)
    with run.repo.tx():
        run.repo.insert_command("auto-old", "AUTO", SUPERVISOR_ACTOR, "RESUME",
                                {"account": ACCOUNT, "halt_type": "K8",
                                 "not_after": (T0 - timedelta(minutes=1)).isoformat()}, T0)
    run.step()
    cmd = run.repo.command("auto-old")
    assert cmd is not None and cmd["status"] == "REJECTED" and "시한 경과" in cmd["result"]
    assert run.repo.select("SELECT 1 FROM reconciliation WHERE trigger='AUTO_RESUME'") == []
    run.finish(0)


def test_v297_resume_hook_only_for_auto_channel(tmp_path: Path) -> None:
    """M2 추가 조건은 AUTO 채널에만 — 원격·로컬 재개는 CP-026·기존 조건 그대로(조건 함수가 불리지 않음)."""
    r = Rig(tmp_path)
    r.runs.append(run := FakeM2Run(r))
    calls: list[int] = []
    run.engine.ops.auto_resume_check = lambda: calls.append(1) or "가상 거부"  # type: ignore[func-returns-value]
    run.step()
    with run.repo.tx():
        run.repo.insert_command("local-1", "LOCAL", "tester", "RESUME", {"account": None, "halt_type": "K8"}, T0)
    run.step()
    assert calls == [] and run.repo.command("local-1")["status"] == "DONE"  # type: ignore[index]
    run.finish(0)


# --- V-298 재시작 정책 ------------------------------------------------------------------------------------

def test_v298_abnormal_exit_restarts_after_5_then_15_minutes_then_cap(tmp_path: Path) -> None:
    r = Rig(tmp_path, schedule=M2Schedule(poll_seconds=30, idle_poll_seconds=30))
    r.at(T0 + timedelta(minutes=20), kill(r))                    # 1번째 사망
    r.at(T0 + timedelta(hours=1), kill(r))                       # 2번째 사망
    r.at(T0 + timedelta(hours=2), kill(r))                       # 3번째 사망 (24시간 안) → 상한
    r.stop_supervisor_at(T0 + timedelta(hours=3))
    assert r.sup.run() == 0
    assert r.events.count("launch") == 3                        # 처음 + 재시작 2회
    starts = [run for run in r.runs]
    assert len(starts) == 3
    ev = r.sup_events()
    assert ev.count("RESTART") == 2 and "RESTART_CAP" in ev
    assert any("5분 뒤 자동 재시작" in t for _, t in r.notes)
    assert any("15분 뒤 자동 재시작" in t for _, t in r.notes)
    assert r.levels("재시작 상한") == ["EMERGENCY"]
    assert any("M2 정지 — 사람 확인" in t for _, t in r.notes)
    restarts = [datetime.fromisoformat(x["at"]) for x in r.q("SELECT at, summary FROM audit_event WHERE kind=? "
                                                             "AND summary LIKE '%\"RESTART\"%' ORDER BY seq",
                                                             (AUDIT_KIND,))]
    assert restarts[0] - utc(10, 10, 20) >= timedelta(minutes=5) and restarts[0] - utc(10, 10, 20) < timedelta(minutes=6)
    assert restarts[1] - utc(10, 11, 0) >= timedelta(minutes=15) and restarts[1] - utc(10, 11, 0) < timedelta(minutes=16)


def test_v298_restarted_run_starts_with_k8_and_no_order_before_reconcile(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    seen: list[tuple[bool, int]] = []

    def started(run: FakeM2Run) -> None:   # 재시작 run의 시작 직후: K8이 걸려 있고 주문 없음
        seen.append(("K8" in run.repo.active_halts(ACCOUNT), len(r.broker.sent)))
    r.on_started = started
    r.at(T0 + timedelta(minutes=20), kill(r))
    r.stop_supervisor_at(T0 + timedelta(minutes=50))
    assert r.sup.run() == 0
    assert seen == [(True, 0), (True, 0)]
    assert len(r.auto_commands()) == 2 and all(c["status"] == "DONE" for c in r.auto_commands())
    assert r.broker.sent == []                                     # 같은 의도 재전송 0 (전략 없음 — 주문 자체 없음)


@pytest.mark.parametrize("code", [0, 2])
def test_v298_exit_code_0_or_2_no_restart_emergency(tmp_path: Path, code: int) -> None:
    r = Rig(tmp_path)
    r.at(T0 + timedelta(minutes=20), kill(r, code))
    r.stop_supervisor_at(T0 + timedelta(hours=2))
    rc = r.sup.run()
    assert r.events.count("launch") == 1
    assert any(lv == "EMERGENCY" and f"코드 {code}" in t for lv, t in r.notes)
    if code == 0:
        assert rc == 0 and "의도된 종료" in r.text()           # 감독도 끝난다(다음 로그온·사용자 시작)
        assert r.clock.now() < T0 + timedelta(hours=1)
    else:
        assert rc == 0 and "M2 정지 — 사람 확인" in r.text()    # run 없이 대기 → 감독 정지 요청으로 끝남
        assert r.clock.now() >= T0 + timedelta(hours=2)


@pytest.mark.parametrize("halt", [HaltType.K3, HaltType.K4, HaltType.K1, HaltType.K2, HaltType.K7])
def test_v298_human_halt_active_at_death_no_restart(tmp_path: Path, halt: HaltType) -> None:
    r = Rig(tmp_path)

    def die_with_halt() -> None:
        run = r.runs[-1]
        run.engine.halts.activate(halt, "tester", "가상 사람 확인 정지", ACCOUNT)
        run.finish(1)
    r.at(T0 + timedelta(minutes=20), die_with_halt)
    r.stop_supervisor_at(T0 + timedelta(hours=1))
    assert r.sup.run() == 0
    assert r.events.count("launch") == 1
    assert any(lv == "EMERGENCY" and "M2 정지 — 사람 확인" in t and halt.value in t for lv, t in r.notes)


def test_v298_restart_preflight_failure_waits_without_run(tmp_path: Path) -> None:
    r = Rig(tmp_path, preflight_rc=[0, 1])
    r.at(T0 + timedelta(minutes=20), kill(r))
    r.stop_supervisor_at(T0 + timedelta(hours=1))
    assert r.sup.run() == 0
    assert r.events.count("launch") == 1 and r.events.count("preflight") == 2
    assert any(lv == "EMERGENCY" and "시작 점검 실패" in t for lv, t in r.notes)


def test_v298_restart_count_survives_supervisor_restart(tmp_path: Path) -> None:
    """재시작 횟수는 감사 기록(DB)으로 센다 — 감독이 다시 떠도 24시간 상한이 초기화되지 않는다(보수, DP)."""
    r = Rig(tmp_path)
    r.at(T0 + timedelta(minutes=20), kill(r))
    r.at(T0 + timedelta(hours=1), kill(r))
    r.at(T0 + timedelta(hours=1, minutes=30), lambda: request_supervisor_stop(r.base, r.clock.now()))
    assert r.sup.run() == 0
    assert r.sup_events().count("RESTART") == 2
    r2 = Rig.__new__(Rig)
    r2.__dict__.update(r.__dict__)
    r2.runs, r2.events, r2.notes, r2.hooks = [], [], [], []
    r2.sup = M2Supervisor(Mode.PAPER, r.base, ACCOUNT, M2Schedule(poll_seconds=30, idle_poll_seconds=60),
                          M2SupervisorDeps(clock=r.clock, sleep=r2.sleep, notify=r2.notify, preflight=r2.preflight,
                                           launch=r2.launch, review=r2.review, backup=r2.backup))
    r2.at(r.clock.now() + timedelta(minutes=10), kill(r2))
    r2.stop_supervisor_at(r.clock.now() + timedelta(hours=1))
    assert r2.sup.run() == 0
    assert r2.events.count("launch") == 1 and r2.levels("재시작 상한") == ["EMERGENCY"]


# --- V-299 UTC 일 경계 일과 -------------------------------------------------------------------------------

def test_v299_utc_boundary_routine_after_day_reconcile_once(tmp_path: Path) -> None:
    r = Rig(tmp_path, start=utc(10, 23, 50))
    r.stop_supervisor_at(utc(11, 0, 40))
    assert r.sup.run() == 0
    assert r.events.count("launch") == 1                          # run 재시작 없음
    day = r.q("SELECT created_at FROM reconciliation WHERE trigger='DAY_BOUNDARY'")
    assert day, "일 경계 대조가 있어야 함"
    i = r.events.index("review")
    assert r.events[i:i + 2] == ["review", "backup"] and r.events.count("review") == 1
    summaries = [t for lv, t in r.notes if "일일 요약" in t]
    assert len(summaries) == 1 and "[M2" in summaries[0] and "2026-10-10" in summaries[0]
    assert "확인 필요" not in summaries[0]
    assert ACCOUNT not in summaries[0]                              # 계좌 식별 없음 (D-227)


def test_v299_kst_midnight_has_no_effect(tmp_path: Path) -> None:
    r = Rig(tmp_path, start=utc(10, 14, 50))                     # KST 23:50
    r.stop_supervisor_at(utc(10, 15, 40))                         # KST 00:40
    assert r.sup.run() == 0
    assert "review" not in r.events and not any("일일 요약" in t for _, t in r.notes)
    assert r.events.count("launch") == 1


def test_v299_backup_failure_and_missing_day_reconcile_marked(tmp_path: Path) -> None:
    r = Rig(tmp_path, start=utc(10, 23, 50), backup_rc=1)
    r.at(utc(10, 23, 58), lambda: setattr(r.runs[-1], "frozen", True))   # run 멈춤 → 일 경계 대조 없음
    r.stop_supervisor_at(utc(11, 0, 45))
    assert r.sup.run() == 0
    summaries = [t for lv, t in r.notes if "일일 요약" in t]
    assert len(summaries) == 1
    assert "확인 필요" in summaries[0] and "백업 실패" in summaries[0] and "일 경계 대조" in summaries[0]
    assert r.events.count("launch") == 1                          # 멈춘 run을 재시작·강제 종료하지 않는다
    assert any("하트비트 멈춤" in t for _, t in r.notes)


def test_v299_summary_lists_restarts_and_k8(tmp_path: Path) -> None:
    r = Rig(tmp_path, start=utc(10, 22, 0))
    r.at(utc(10, 22, 30), kill(r))
    r.stop_supervisor_at(utc(11, 0, 40))
    assert r.sup.run() == 0
    [summary] = [t for lv, t in r.notes if "일일 요약" in t]
    assert "자동 재시작" in summary and "K8 자동 재개" in summary


# --- V-300 리셋·사람 확인 정지 ----------------------------------------------------------------------------

def test_v300_testnet_reset_k4_no_resume_attempt_emergency(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.at(T0 + timedelta(minutes=10), lambda: r.broker.grant.update({QUOTE_ASSET: Decimal("15000")}))  # USDT 재지급
    r.stop_supervisor_at(T0 + timedelta(hours=1, minutes=30))
    assert r.sup.run() == 0
    assert "K4" in r.active()
    assert [c["status"] for c in r.auto_commands()] == ["DONE"]   # 시작 때 K8 재개 1건뿐 — K4 뒤 재개 시도 0
    assert any(lv == "EMERGENCY" and "리셋 의심" in t and "reset-confirm" in t for lv, t in r.notes)
    assert r.events.count("launch") == 1


@pytest.mark.parametrize("halt", [HaltType.K1, HaltType.K3])
def test_v300_k1_k3_not_auto_released(tmp_path: Path, halt: HaltType) -> None:
    r = Rig(tmp_path)
    r.at(T0 + timedelta(minutes=10),
         lambda: r.runs[-1].engine.halts.activate(halt, "tester", "가상", ACCOUNT))
    r.stop_supervisor_at(T0 + timedelta(hours=2))
    assert r.sup.run() == 0
    assert halt.value in r.active()
    assert len(r.auto_commands()) == 1                            # 시작 K8 한 건 — 사람 확인 정지는 건드리지 않음
    assert any(halt.value in t and "사람 확인 정지" in t for _, t in r.notes)


# --- V-301 정지 경로 ------------------------------------------------------------------------------------

def test_v301_ops_stop_run_exits_normally_no_restart(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.at(T0 + timedelta(minutes=20), lambda: (r.base / STOP_REQUEST).write_text("ops-stop 가상\n", encoding="utf-8"))
    assert r.sup.run() == 0                                        # 감독도 끝난다 (의도된 종료)
    assert r.runs[-1].stopped_by_file and r.events.count("launch") == 1
    assert "PLANNED_STOP" in r.sup_events()


def test_v301_kill_file_k1_run_kept_not_restart_target(tmp_path: Path) -> None:
    r = Rig(tmp_path)

    def kill_switch() -> None:   # scripts/kill.py와 같은 효과: KILL 파일 + KILL 명령(LOCAL) → run이 K1
        (r.base / "KILL").write_text("", encoding="utf-8")
        run = r.runs[-1]
        with run.repo.tx():
            run.repo.insert_command("local-kill-1", "LOCAL", "tester", "KILL", {"account": None}, r.clock.now())
    r.at(T0 + timedelta(minutes=20), kill_switch)
    r.stop_supervisor_at(T0 + timedelta(hours=1))
    assert r.sup.run() == 0
    assert "K1" in r.active()
    assert r.events.count("launch") == 1 and r.runs[-1].stopped_by_file   # 감독 정지 요청 전까지 run 유지
    assert any("K1" in t and "사람 확인 정지" in t for _, t in r.notes)


def test_v301_supervisor_stop_request_uses_run_stop_path(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.stop_supervisor_at(T0 + timedelta(minutes=20))
    assert r.sup.run() == 0
    assert r.runs[-1].stopped_by_file and r.runs[-1].exit_code() == 0
    assert not (r.base / SUPERVISOR_STOP).exists()
    assert r.events.count("launch") == 1 and "SUPERVISOR_STOP" in r.sup_events()
    assert any("감독 정지" in t for _, t in r.notes)


def test_v301_supervisor_stop_while_run_does_not_exit_no_force(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.at(T0 + timedelta(minutes=10), lambda: setattr(r.runs[-1], "frozen", True))
    r.stop_supervisor_at(T0 + timedelta(minutes=20))
    assert r.sup.run() == 0
    assert r.runs[-1].alive()                                      # 강제 종료 없음 — 사람이 PC에서 확인
    assert any(lv == "EMERGENCY" and "끝나지 않음" in t for lv, t in r.notes)


def test_v301_interrupt_writes_stop_for_launched_run(tmp_path: Path) -> None:
    r = Rig(tmp_path)

    def boom() -> None:
        raise KeyboardInterrupt
    r.at(T0 + timedelta(minutes=20), boom)
    assert r.sup.run() == 3
    assert (r.base / STOP_REQUEST).exists() and r.runs[-1].alive()   # 정지 요청 파일만 — 강제 종료 없음
    assert any(lv == "EMERGENCY" and "감독 예외 종료" in t for lv, t in r.notes)


def test_request_supervisor_stop_writes_file(tmp_path: Path) -> None:
    base = tmp_path / "var" / "paper"
    base.mkdir(parents=True)
    path = request_supervisor_stop(base, T0)
    assert path == base / SUPERVISOR_STOP and path.exists()


def test_m2_telegram_notify_prefix_and_paths_masked(tmp_path: Path) -> None:
    from trading_system.app.m2_supervisor import M2TelegramNotify
    sent: list[tuple[str, str]] = []

    class Api:
        def send_message(self, chat: str, text: str) -> None:
            sent.append((chat, text))
    logs: list[str] = []
    n = M2TelegramNotify(Api(), "12345", logs.append, fallback=tmp_path / "fb.log", request_allowance=0.0)
    n("EMERGENCY", r"run 하트비트 멈춤 C:\AI\trading_system\m2-binance\var\paper\heartbeat — 확인")
    [(chat, text)] = sent
    assert chat == "12345" and text.startswith("[긴급] [M2 감독] ")
    assert "m2-binance" not in text and "<경로>" in text


def test_v297e_unknown_intent_unresolved_auto_resume_rejected(tmp_path: Path) -> None:
    """결과 불명 의도가 남아 있으면 K8 자동 재개는 01 §2.11 조건 5로 거부 — 사람 확인으로 넘어간다(D-225). 결과 불명 의도는
    복구 판정(reconciliation.reconciler)이 보는 조회만 가짜로 바꿔 흉내 낸다(해소기는 손대지 않음)."""
    import inspect
    from types import SimpleNamespace

    from trading_system.domain.enums import IntentState
    r = Rig(tmp_path)

    def started(run: FakeM2Run) -> None:
        orig = run.repo.intents_in_state
        fake = SimpleNamespace(account=ACCOUNT, intent_id="intent-unknown-virtual")

        def patched(state: IntentState):  # type: ignore[no-untyped-def]
            caller = inspect.stack()[1].frame.f_globals.get("__name__", "")
            if state is IntentState.UNKNOWN and caller.endswith("reconciliation.reconciler"):
                return [fake]
            return orig(state)
        run.repo.intents_in_state = patched  # type: ignore[method-assign]
    r.on_started = started
    r.stop_supervisor_at(T0 + timedelta(minutes=12))
    assert r.sup.run() == 0
    cmds = r.auto_commands()
    assert cmds and all(c["status"] == "REJECTED" and "결과 불명" in c["result"] for c in cmds)
    assert "K8" in r.active()


# --- 정지 도구 (D-227·D-230): ops-stop --market M2, m2-supervisor --stop -------------------------------------------

def test_ops_stop_market_m2_writes_stop_only_in_m2_checkout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from trading_system.app.autopilot import ops_stop_main
    root = _root(tmp_path)
    base = root / "var" / "paper"
    run_lock = acquire_instance_lock(base)
    try:
        assert ops_stop_main(["--mode", "PAPER", "--market", "M2", "--root", str(root), "--wait", "0"], environ={}) == 0
        assert (base / STOP_REQUEST).exists()
    finally:
        run_lock.release()
    m1 = tmp_path / "m1"
    (m1 / "var" / "paper").mkdir(parents=True)
    lock2 = acquire_instance_lock(m1 / "var" / "paper")
    try:
        assert ops_stop_main(["--mode", "PAPER", "--market", "M2", "--root", str(m1), "--wait", "0"], environ={}) == 2
        assert not (m1 / "var" / "paper" / STOP_REQUEST).exists()          # M1 체크아웃의 run은 건드리지 않음
        assert ops_stop_main(["--mode", "PAPER", "--market", "M3", "--root", str(m1), "--wait", "0"], environ={}) == 2
    finally:
        lock2.release()
    assert "config/m2" in capsys.readouterr().out


def test_supervisor_stop_cli_requires_running_supervisor(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _root(tmp_path)
    argv = ["--mode", "PAPER", "--market", "M2", "--root", str(root), "--stop", "--wait", "0"]
    assert m2_supervisor_main(argv, environ={}, hosts=HOSTS) == 1
    assert not (root / "var" / "paper" / SUPERVISOR_STOP).exists()
    base = root / "var" / "paper"
    held = acquire_instance_lock(base, M2_SUPERVISOR_LOCK)
    try:
        assert m2_supervisor_main(argv, environ={}, hosts=HOSTS) == 0
        assert (base / SUPERVISOR_STOP).exists()
    finally:
        held.release()
    assert "감독 정지 요청 파일 작성" in capsys.readouterr().out
