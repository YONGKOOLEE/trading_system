"""PAPER-0 다일(多日) 종단 간 시나리오 — 검토 F-4 "paper0-verify의 엔진 수준 다일 시험 없음" 대응.

실제 조립(app/wiring.build_engine, Mode.PAPER) + 실제 설정(config/calendar·market.paper·limits.paper·app.paper·
paper0-verify-v1 파라미터) + 실제 전략(Paper0Verify, 상태 PAPER는 strategy_main 등록·전이로) + 실제 시세 스레드 로직
(MarketPoller, 일봉 갱신 RV-04) + 실제 실시간 상태 처리기(ws_state_handler, RV-02)로 3거래일(2026-10-06·07·08)을 돈다.
브로커는 fake(FakeBroker), 시세 공급자는 가상(PaperFeed: 키움 응답 대신 store에 직접 넣음). 네트워크 없음,
가짜 시계(FixedClock)만 쓴다. 감시 스레드(Supervisor)는 시계를 분 단위로 건너뛰므로 루프 감시(K8 오탐)와
맞지 않아 주 경로에 넣지 않는다 (공백, 보고서 참조).
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e   # A-084 ⑥: 빠른 확인은 -m "not rehearsal and not e2e"

from tests.support.invariants import check_invariants
from trading_system.app.main import ws_state_handler
from trading_system.app.strategy_tool import strategy_main
from trading_system.app.wiring import StrategyInput, build_engine, load_strategy
from trading_system.broker.fake.broker import FakeBehavior, FakeBroker
from trading_system.broker.kiwoom.realtime import WsState
from trading_system.config.app_file import load_app_settings
from trading_system.config.capabilities import load_capabilities
from trading_system.config.limits_file import load_limits_file
from trading_system.config.market_file import load_market_settings, parse_alert_settings
from trading_system.domain.clock import KST, FixedClock
from trading_system.domain.enums import Mode
from trading_system.domain.market import CashView, Quote
from trading_system.marketdata.calendar import KrxCalendar
from trading_system.marketdata.kiwoom_feed import MarketPoller
from trading_system.marketdata.quality import RawDaily
from trading_system.marketdata.store import MarketDataStore, latest_complete_day
from trading_system.marketdata.ticks import load_ticks
from trading_system.storage.repository import Repository

ROOT = Path(__file__).resolve().parents[2]
ACCOUNT = "KR-PAPER-01"
CODE = "005930"
DAYS = (date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8))   # 10-05(대체공휴일)·10-09(한글날) 휴장
PRICE = Decimal("70000")
CASH = Decimal("100000000")   # strategy.md: 모의 예수금 1억 원 기준 (L1 = 주문 금액 전체)


def kst(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=KST)


class ClockOk:
    def clock_ok(self) -> bool | None:
        return True


class PaperFeed:
    """KiwoomMarketFeed 대역: MarketPoller가 부르는 메서드만. 키움 응답 대신 가상 값을 store에 넣는다."""

    def __init__(self, clock: FixedClock, store: MarketDataStore, infos: dict) -> None:
        self.clock, self.store, self.infos, self.alerts = clock, store, infos, None
        self.daily_calls: list[datetime] = []
        self.price = PRICE

    def refresh_daily(self, code: str):  # type: ignore[no-untyped-def]
        self.daily_calls.append(self.clock.now())
        last = latest_complete_day(self.clock.now(), self.store.calendar)
        rows, d = [], last
        while len(rows) < 60:
            if self.store.calendar.is_trading_day(d):
                rows.append(RawDaily(d, PRICE, PRICE + 500, PRICE - 500, PRICE, Decimal("1000")))
            d -= timedelta(days=1)
        return self.store.update_daily(code, sorted(rows, key=lambda r: r.day))

    def refresh_master(self) -> bool:
        for c in self.infos:
            self.store.update_status(c, frozenset())
        return True

    def refresh_vi(self) -> bool:
        return True

    def refresh_quote(self, code: str):  # type: ignore[no-untyped-def]
        q = Quote(code, self.price, self.clock.now(), upper_limit=Decimal("91000"), lower_limit=Decimal("49000"))
        self.store.update_quote(q)
        return q

    def tick(self) -> None:
        pass


def make_root(tmp: Path) -> Path:
    """임시 저장소 루트: 실제 설정 파일 복사 + 전략 등록 → BACKTEST_PASSED → PAPER (가상 승인 A-9xx)."""
    root = tmp / "repo"
    for rel in ("config/app.paper.toml", "config/limits.paper.toml", "config/market.paper.toml",
                "config/strategies/paper0-verify-v1.toml", "docs/capabilities.md"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text((ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
    # 배포 설정은 2026-10-07 v2로 교체됨(A-067 B-5). 이 시험들은 v1 기준 엔진 동작을 보므로 복사본의 전략 항목만 v1로
    # 고정한다(시험 변경 사유: 배포 설정 교체 — 시험 대상 동작은 그대로. v2 시험은 test_t017_paper0_v2·test_t017_gaps)
    app = root / "config" / "app.paper.toml"
    text = app.read_text(encoding="utf-8")
    for v2, v1 in (("paper0_verify:Paper0VerifyV2", "paper0_verify:Paper0Verify"),
                   ("paper0-verify-v2.toml", "paper0-verify-v1.toml"),
                   ('verification_approval = "A-067"', 'verification_approval = "A-037"')):
        text = text.replace(v2, v1)
    app.write_text(text, encoding="utf-8")
    assert strategy_main(["register", "--mode", "PAPER", "--root", str(root)]) == 0
    evidence = root / "var" / "backtest" / "paper0-verify-v1" / "report.md"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("가상 증거 (시험용)\n", encoding="utf-8")
    assert strategy_main(["transition", "--mode", "PAPER", "--root", str(root), "paper0-verify", "1",
                          "BACKTEST_PASSED", "--approval", "A-901", "--evidence",
                          "var/backtest/paper0-verify-v1/report.md"]) == 0
    assert strategy_main(["transition", "--mode", "PAPER", "--root", str(root), "paper0-verify", "1",
                          "PAPER", "--approval", "A-902"]) == 0
    return root


class PaperWorld:
    requery_wait = None   # 대조 재조회 전 대기 (fake 브로커는 통보가 동기라 불필요)
    def __init__(self, tmp: Path) -> None:
        self.root = make_root(tmp)
        self.clock = FixedClock(kst(DAYS[0], 8, 20))
        self.calendar = KrxCalendar.load_dir(ROOT / "config" / "calendar")
        assert not self.calendar.is_trading_day(date(2026, 10, 5))
        assert not self.calendar.is_trading_day(date(2026, 10, 9))
        assert all(self.calendar.is_trading_day(d) for d in DAYS)
        self.broker = self.make_broker()
        self.reconciles: list[datetime] = []
        self._broker_day = DAYS[0]
        self.build()

    def make_broker(self):  # type: ignore[no-untyped-def]
        b = FakeBroker(FakeBehavior.FILL)
        b.cash[ACCOUNT] = CashView(CASH, CASH)
        return b

    def new_broker_day(self) -> None:
        self.broker.new_day()                        # 키움 주문번호 일별 재시작 가정 (fake, K-U2)

    def settle(self) -> None:
        """비동기 통보를 기다린다 (fake는 동기라 할 일 없음)."""

    def sent_count(self) -> int:
        return len(self.broker.sent)

    def build(self) -> None:
        """main._run의 조립 순서를 따른다: 설정 로드 → store·종목 등록 → build_engine → 시작 시 일봉 1회."""
        import tomllib
        app = load_app_settings(self.root / "config" / "app.paper.toml", Mode.PAPER, self.root)
        assert app.account == ACCOUNT
        market = self.root / "config" / "market.paper.toml"
        gate_settings = load_market_settings(market)
        with market.open("rb") as f:
            alert_thresholds = parse_alert_settings(tomllib.load(f))
        limits = load_limits_file(self.root / "config" / "limits.paper.toml").limits
        self.store = MarketDataStore(self.calendar, load_ticks(ROOT / "config" / "ticks" / "krx.toml"))
        for info in app.instruments.values():
            self.store.register(info)
        self.repo = Repository(self.root / "var" / "paper" / "trading.db", Mode.PAPER)
        self.feed = PaperFeed(self.clock, self.store, app.instruments)
        self.poller = MarketPoller(self.feed, alerts=None)  # type: ignore[arg-type]
        strategies = []
        for dep in app.strategies:
            strat, params = load_strategy(dep)
            strategies.append(StrategyInput(strat, params, dep.eval_times, dep.verification_approval))
        assert [s.eval_times for s in strategies] == [(time(9, 30), time(14, 30))]
        self.engine = build_engine(mode=Mode.PAPER, repo=self.repo, clock=self.clock, broker=self.broker,
                                   store=self.store, account=ACCOUNT, account_limits=limits,
                                   gate_settings=gate_settings, caps=load_capabilities(ROOT / "docs" / "capabilities.md"),
                                   strategies=strategies, kill_file=self.root / "var" / "paper" / "KILL",
                                   clock_health=ClockOk(), alert_thresholds=alert_thresholds,
                                   requery_wait=self.requery_wait,
                                   reconcile_every=timedelta(minutes=app.reconcile_minutes))
        # 대조 실행 기록 (원래 동작은 그대로)
        real_run = self.engine.reconciler.run

        def counted(account: str, trigger: str):  # type: ignore[no-untyped-def]
            self.reconciles.append(self.clock.now())
            return real_run(account, trigger)

        self.engine.reconciler.run = counted  # type: ignore[method-assign]
        self.on_state = ws_state_handler(self.engine, ACCOUNT)
        self.after_build()
        self.poller.poll_once()                      # main._paper_broker의 시작 시 일봉 1회 + 첫 시세

    def after_build(self) -> None:
        pass

    def restart(self) -> None:
        """프로세스 재시작: 메모리 상태(엔진·store·시세 스레드)는 버리고 같은 DB·같은 브로커로 다시 조립."""
        self.repo.close()
        self.build()

    # --- 조작 -----------------------------------------------------------------------------------
    def step(self) -> None:
        """한 주기: 시세 스레드 1회 → 판정·실행 스레드 1회 (main._run 순서와 같은 관계)."""
        d = self.clock.now().astimezone(KST).date()
        if d != self._broker_day and self.clock.now().astimezone(KST).time() >= time(7, 0):
            self.new_broker_day()
            self._broker_day = d
        self.poller.poll_once()
        self.engine.tick()
        self.settle()

    def run_until(self, target: datetime) -> None:
        """target까지 시계를 진행하며 주기 실행. 장중(08:50~15:40)은 30초, 그 밖은 10분 간격."""
        while self.clock.now() < target:
            t = self.clock.now().astimezone(KST).time()
            dt = timedelta(seconds=30) if time(8, 50) <= t <= time(15, 40) else timedelta(minutes=10)
            self.clock._now = min(self.clock.now() + dt, target)  # noqa: SLF001
            self.step()

    def command(self, cmd: str, args: dict) -> None:
        with self.repo.tx():
            self.repo.insert_command(f"local-{cmd}-{len(self.repo.select('SELECT 1 FROM ops_command'))}",
                                     "LOCAL", "tester", cmd, args, self.clock.now())

    # --- 조회 -----------------------------------------------------------------------------------
    def active(self) -> set[str]:
        return set(self.repo.active_halts(ACCOUNT))

    def intents(self) -> list[dict]:
        return self.repo.select("SELECT intent_id, state, request_type, side, quantity, idem_key, created_at "
                                "FROM intent ORDER BY created_seq")

    def decisions(self) -> list[dict]:
        return self.repo.select("SELECT request_type, result, failed_check, reason, created_at FROM gate_decision "
                                "ORDER BY seq")

    def position(self) -> Decimal:
        return self.repo.position(ACCOUNT, CODE).quantity

    def evaluations(self) -> list[dict]:
        return [dict(e, s=json.loads(e["summary"])) for e in self.repo.audit_events()
                if e["kind"] in ("EVALUATION", "EVALUATION_SKIPPED")]

    def close(self) -> None:
        try:
            assert check_invariants(self.repo.connection, Mode.PAPER.value) == []
        finally:
            self.repo.close()


@pytest.fixture
def world(tmp_path: Path):  # type: ignore[no-untyped-def]
    w = PaperWorld(tmp_path)
    yield w
    w.close()


def start_and_resume(w: PaperWorld) -> None:
    rep = w.engine.start()
    assert rep.ok, rep.failures
    assert "K8" in w.active()
    w.run_until(kst(DAYS[0], 8, 40))
    assert w.sent_count() == 0                                # K8 중 주문 없음
    w.command("RESUME", {"account": None, "halt_type": "K8"})
    w.step()
    assert "K8" not in w.active(), w.active()


def buy_sell_of_day(w: PaperWorld, d: date) -> list[tuple[str, str]]:
    day = d.isoformat()
    return [(i["request_type"], i["state"]) for i in w.intents()
            if datetime.fromisoformat(i["created_at"]).astimezone(KST).date().isoformat() == day]


def run_trading_day(w: PaperWorld, d: date) -> None:
    w.run_until(kst(d, 9, 29, 30))
    assert w.position() == 0
    w.run_until(kst(d, 9, 31))
    assert buy_sell_of_day(w, d) == [("ENTRY", "FILLED")], (d, w.decisions()[-3:])
    assert w.position() == 1
    w.run_until(kst(d, 14, 31))
    assert buy_sell_of_day(w, d) == [("ENTRY", "FILLED"), ("EXIT", "FILLED")], (d, w.decisions()[-3:])
    assert w.position() == 0


# --- 1~4: 3거래일 정상 흐름 ---------------------------------------------------------------------------

def test_three_days_entry_exit_daily_bars_reports_and_reconcile_cadence(world: PaperWorld) -> None:
    w = world
    start_and_resume(w)
    for d in DAYS:
        run_trading_day(w, d)
        assert w.active() == set(), (d, w.active())          # 장중 주기 대조가 K4 등을 걸지 않음
        w.run_until(kst(d, 16, 1))
        w.run_until(kst(d + timedelta(days=1), 8, 45) if d != DAYS[-1] else kst(d, 21, 0))

    # 주문: 하루 매수 1·매도 1, 총 6건, 중복 없음
    its = w.intents()
    assert [(i["request_type"], i["state"]) for i in its] == [("ENTRY", "FILLED"), ("EXIT", "FILLED")] * 3
    assert len({i["idem_key"] for i in its}) == 6
    assert len(w.broker.sent) == 6
    assert [r.side for r in w.broker.sent] == ["BUY", "SELL"] * 3
    assert w.position() == 0
    assert w.repo.active_reservations(ACCOUNT) == []

    # 판정: 하루 2회 × 3일 = 6회, 모두 신호 1개 (일봉 품질 문제로 신호가 빠지지 않음, RV-04)
    evs = w.evaluations()
    assert [e["kind"] for e in evs] == ["EVALUATION"] * 6, [e["s"] for e in evs]
    assert all(e["s"]["signals"] == 1 and not e["s"]["error"] for e in evs)

    # RV-04: 확정일이 바뀔 때마다(시작 1회 + 각 거래일 20:00 이후) 일봉 재조회, 그 외 반복 조회 없음
    days_fetched = [c.astimezone(KST) for c in w.feed.daily_calls]
    assert len(days_fetched) == 1 + 3, days_fetched          # 시작 1회 + 10-06·07·08 각 20:00 뒤
    assert days_fetched[0].date() == DAYS[0] and days_fetched[0].time() < time(9, 0)
    assert [c.date() for c in days_fetched[1:]] == list(DAYS)
    assert all(c.time() >= time(20, 0) for c in days_fetched[1:])
    assert w.store.last_daily_day(CODE) == DAYS[2]

    # 게이트 판정: 위험 확대 거부(특히 G8 현금 모름) 없음 (RV-23)
    rejected = [x for x in w.decisions() if x["result"] != "APPROVED"]
    assert rejected == [], rejected

    # RV-21: 거래일마다 16:00 보고서
    reports = [r["trade_day"] for r in w.repo.daily_reports(ACCOUNT)]
    for d in DAYS:
        assert d.isoformat() in [str(x) for x in reports], reports


def test_rv25_no_overnight_periodic_reconcile(world: PaperWorld) -> None:
    w = world
    start_and_resume(w)
    run_trading_day(w, DAYS[0])
    w.run_until(kst(DAYS[0], 15, 30))                        # 15:30:00 = 정규장 안(<=) 마지막 주기 대조
    w.reconciles.clear()
    w.run_until(kst(DAYS[1], 8, 59, 30))                     # 장 마감 ~ 다음 날 장 시작 전
    runs = [r.astimezone(KST) for r in w.reconciles]
    assert len(runs) == 1, runs                               # 장 마감 후 1회만
    assert runs[0].date() == DAYS[0] and runs[0].time() > time(15, 30)
    w.run_until(kst(DAYS[1], 9, 5))
    assert len(w.reconciles) > 1                              # 장중 주기 대조 재개


def test_rv23_entry_not_blocked_by_cash_freshness_each_morning(world: PaperWorld) -> None:
    """09:30 판정 직전 현금 조회 시각 확인: 밤사이 주기 대조가 없어도 09:00 이후 장중 대조가 현금을 갱신해야 한다."""
    w = world
    start_and_resume(w)
    for d in DAYS:
        w.run_until(kst(d, 9, 29, 30))
        at = w.engine.account_state.tracker.cash_at(ACCOUNT)
        assert at is not None and w.clock.now() - at <= timedelta(minutes=1), (d, at)
        assert w.engine.gate._in.account.cash(ACCOUNT) is not None  # noqa: SLF001 — G8 입력 = 신선
        w.run_until(kst(d, 9, 31))
        g8 = [x for x in w.decisions() if x["failed_check"] == "G8"]
        assert g8 == [], g8
        w.run_until(kst(d + timedelta(days=1), 8, 45) if d != DAYS[-1] else kst(d, 15, 0))


def test_rv23_startup_just_before_entry_after_overnight_downtime(tmp_path: Path) -> None:
    """프로세스를 장 시작 직전에 켜는 경우: start()가 현금을 조회해 09:30까지 신선하다."""
    w = PaperWorld(tmp_path)
    try:
        w.clock._now = kst(DAYS[0], 8, 55)  # noqa: SLF001
        start_and_resume(w)
        run_trading_day(w, DAYS[0])
        assert [x for x in w.decisions() if x["result"] != "APPROVED"] == []
    finally:
        w.close()


# --- 5: 장중 실시간 끊김 → K5 → 재접속 → 대조 → K5 해제 → 14:30 청산 -------------------------------------

def test_rv02_realtime_disconnect_midday_k5_reconnect_release_then_exit(world: PaperWorld) -> None:
    w = world
    start_and_resume(w)
    d = DAYS[0]
    w.run_until(kst(d, 9, 31))
    assert w.position() == 1
    w.run_until(kst(d, 11, 0))
    w.on_state(WsState.OFFLINE, "가상 끊김")
    w.step()
    assert "K5" in w.active()
    sent_before = len(w.broker.sent)
    w.run_until(kst(d, 11, 10))                              # 끊긴 동안 주기 대조는 돌아도 K5 유지
    assert "K5" in w.active()
    assert len(w.broker.sent) == sent_before                  # 정지 중 새 주문 없음
    w.on_state(WsState.ONLINE, "가상 재접속")
    w.step()                                                  # calls(경로 정상 보고) → 대조(요청) 순서
    assert "K5" not in w.active(), json.dumps(
        [json.loads(r["conditions"]) for r in w.repo.halt_rows(None) if r["halt_type"] == "K5"], ensure_ascii=False)
    w.run_until(kst(d, 14, 31))
    assert buy_sell_of_day(w, d) == [("ENTRY", "FILLED"), ("EXIT", "FILLED")]
    assert w.position() == 0 and len(w.broker.sent) == 2


def test_rv02_disconnect_until_after_exit_time_holds_position_then_exits_next_morning(world: PaperWorld) -> None:
    """K5 중에는 "불확실할 때 허용: 없음"(01 §2.6) → 14:30 청산 판정도 건너뛴다(포지션 유지, 자동 청산 없음).
    재접속 전 대조로는 K5가 풀리지 않는다(RV-02). 재접속·대조 후 해제, 다음 날 09:30 판정이 보유 1주를 청산한다."""
    w = world
    start_and_resume(w)
    d = DAYS[0]
    w.run_until(kst(d, 11, 0))
    assert w.position() == 1
    w.on_state(WsState.OFFLINE, "가상 끊김")
    w.step()
    w.engine.request_reconcile()                              # 재접속 전 대조는 K5를 풀지 못한다
    w.step()
    assert "K5" in w.active()
    w.run_until(kst(d, 14, 31))
    assert w.position() == 1 and len(w.broker.sent) == 1      # 정지 중 주문 없음
    skipped = [e for e in w.evaluations() if e["kind"] == "EVALUATION_SKIPPED"]
    assert len(skipped) == 1 and "전역 정지" in skipped[0]["s"]["reason"]
    w.run_until(kst(d, 14, 40))
    w.on_state(WsState.ONLINE, "가상 재접속")
    w.step()
    assert "K5" not in w.active()
    w.run_until(kst(DAYS[1], 9, 31))                          # 보유를 안고 하루를 넘김
    assert w.position() == 0
    assert buy_sell_of_day(w, DAYS[1]) == [("EXIT", "FILLED")]
    assert [x for x in w.decisions() if x["result"] != "APPROVED"] == []
    assert len(w.broker.sent) == 2


# --- K8 중 판정·주문 없음 (시작 후 재개하지 않으면) --------------------------------------------------------

def test_no_orders_while_k8_unresumed(world: PaperWorld) -> None:
    w = world
    assert w.engine.start().ok
    w.run_until(kst(DAYS[0], 14, 31))
    assert "K8" in w.active()
    assert w.broker.sent == [] and w.intents() == []
    evs = w.evaluations()
    assert [e["kind"] for e in evs] == ["EVALUATION_SKIPPED"] * 2
    assert all("전역 정지" in e["s"]["reason"] for e in evs)


# --- 장중 재시작: K8 → 재개 → 중복 매수 없음 → 14:30 청산 ------------------------------------------------

def test_midday_restart_no_duplicate_entry_then_exit(world: PaperWorld) -> None:
    w = world
    start_and_resume(w)
    d = DAYS[1]
    w.run_until(kst(DAYS[0], 16, 1))
    w.run_until(kst(d, 9, 32))
    assert w.position() == 1
    w.restart()                                               # 09:32 재시작 (09:30 판정 유예 5분 안)
    assert w.engine.start().ok
    assert "K8" in w.active()
    w.step()                                                  # K8 중: 09:30 판정은 '전역 정지'로 건너뜀
    w.command("RESUME", {"account": None, "halt_type": "K8"})
    w.step()
    assert "K8" not in w.active()
    w.run_until(kst(d, 14, 31))
    assert buy_sell_of_day(w, d) == [("ENTRY", "FILLED"), ("EXIT", "FILLED")]
    assert w.position() == 0
    assert [r.side for r in w.broker.sent] == ["BUY", "SELL", "BUY", "SELL"]


# --- 감시 스레드(Supervisor)를 1초 간격으로 함께 돌린 진입 구간 -------------------------------------------

def test_supervisor_stepping_around_entry_no_false_k8(world: PaperWorld) -> None:
    from trading_system.app.supervisor import Supervisor

    class Mono:
        def __init__(self) -> None:
            self.t = 1000.0

        def __call__(self) -> float:
            return self.t

    w = world
    start_and_resume(w)
    w.run_until(kst(DAYS[0], 9, 29))
    mono = Mono()
    w.engine.mono = mono
    w.engine.last_progress = mono()
    sup = Supervisor(w.engine, heartbeat=w.root / "var" / "paper" / "heartbeat",
                     fallback=w.root / "var" / "paper" / "logs" / "fallback.log", mono=mono)
    sup.step()
    for _ in range(120):                                      # 09:29 ~ 09:31, 1초마다 감시, 2초마다 판정 주기
        mono.t += 1.0
        w.clock._now = w.clock.now() + timedelta(seconds=1)  # noqa: SLF001
        sup.step(waited=1.0)
        if int(mono.t) % 2 == 0:
            w.step()
    assert "K8" not in w.active() and sup.loop.events == []
    assert (w.root / "var" / "paper" / "heartbeat").exists()
    assert buy_sell_of_day(w, DAYS[0]) == [("ENTRY", "FILLED")]


# --- 키움 모의 시뮬레이터(로컬) 변형: 실제 KiwoomAdapter + KiwoomRealtime(00 통보) + ws_state_handler ---------
# 시세·일봉은 PaperFeed(가상) 그대로. 브로커 주문·조회·실시간 체결 통보·재접속만 시뮬레이터(127.0.0.1)로.

class KiwoomPaperWorld(PaperWorld):
    # 실시간 통보가 별도 스레드로 늦게 올 수 있다 — 실운영(main)처럼 재조회 전 짧게 기다린다 (01 "체결 반영 지연 확인")
    requery_wait = staticmethod(lambda: __import__("time").sleep(0.5))
    def make_broker(self):  # type: ignore[no-untyped-def]
        import itertools

        from tests.support.kiwoom_sim import ACCOUNT_NO, APP_KEY, SECRET, Sim
        from trading_system.broker.kiwoom.adapter import KiwoomAdapter, KiwoomConfig
        from trading_system.broker.kiwoom.http import KiwoomHttp
        self._itertools = itertools
        self.sim = Sim(now=lambda: self.clock.now().astimezone(KST).replace(tzinfo=None), auto_fill=True)
        self.sim.entr = CASH
        cfg = KiwoomConfig(ACCOUNT, lambda: ACCOUNT_NO, lambda: APP_KEY, lambda: SECRET, rest_url=self.sim.url,
                           ws_url=self.sim.ws_url, per_tr_interval=0.0, order_wait=1.0, query_wait=2.0)
        self.crit: list[tuple[str, str]] = []
        self.rt = None
        return KiwoomAdapter(cfg, self.clock, http=KiwoomHttp(self.sim.url, 1.0, 0.5),
                             on_critical=lambda k, m: self.crit.append((k, m)))

    def after_build(self) -> None:
        from trading_system.broker.kiwoom.realtime import KiwoomRealtime, order_handlers
        if self.rt is not None:
            self.rt.stop()
        a = self.broker
        self.states: list[WsState] = []

        def on_state(st: WsState, msg: str) -> None:
            self.states.append(st)
            self.on_state(st, msg)                   # main._paper_broker와 같은 처리기 (RV-02)

        self.rt = KiwoomRealtime(a.cfg.ws_url, a.tokens, self.clock, registrations=[("00", []), ("04", [])],
                                 handlers=order_handlers(a.push_event, ACCOUNT, self.clock,
                                                         lambda v: a._check_account(v, "00")),  # noqa: SLF001
                                 on_state=on_state, on_critical=lambda k, m: self.crit.append((k, m)),
                                 backoff_base=0.01, backoff_cap=0.05)
        self.rt.start()
        assert _wait(lambda: self.rt.state is WsState.ONLINE)

    def sent_count(self) -> int:
        return sum(1 for api, _, _ in self.sim.log if api in ("kt10000", "kt10001", "kt10002", "kt10003"))

    def new_broker_day(self) -> None:
        # 시뮬레이터에는 거래일 개념이 없다: 당일 주문·체결 조회(kt00009·ka10075)가 오늘 것만 주도록 비우고 번호 재시작
        with self.sim._lock:  # noqa: SLF001
            self.sim.orders.clear()
            self.sim.fills.clear()
            self.sim._seq = self._itertools.count(1)  # noqa: SLF001

    def settle(self) -> None:
        def done() -> bool:
            open_ = self.repo.select("SELECT 1 FROM intent WHERE state IN ('SENDING','SENT','ACCEPTED','PARTIAL')")
            if open_:
                self.engine.tick()
            return not open_
        assert _wait(done, 5.0), self.intents()[-2:]

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self.rt is not None:
                self.rt.stop()
            self.broker.close()
            self.sim.close()


def _wait(cond, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    import time as _t
    end = _t.monotonic() + timeout
    while _t.monotonic() < end:
        if cond():
            return True
        _t.sleep(0.01)
    return False


@pytest.fixture
def kworld(tmp_path: Path):  # type: ignore[no-untyped-def]
    w = KiwoomPaperWorld(tmp_path)
    yield w
    w.close()


def test_kiwoom_sim_three_days_entry_exit(kworld: KiwoomPaperWorld) -> None:
    w = kworld
    start_and_resume(w)
    for d in DAYS:
        run_trading_day(w, d)
        assert w.active() == set(), (d, w.active(), w.crit)
        w.run_until(kst(d, 16, 1))
        w.run_until(kst(d + timedelta(days=1), 8, 45) if d != DAYS[-1] else kst(d, 16, 30))
    its = w.intents()
    assert [(i["request_type"], i["state"]) for i in its] == [("ENTRY", "FILLED"), ("EXIT", "FILLED")] * 3
    assert [x for x in w.decisions() if x["result"] != "APPROVED"] == []
    assert w.sim.positions.get(CODE, (Decimal("0"),))[0] == 0 and w.position() == 0
    assert [str(r["trade_day"]) for r in w.repo.daily_reports(ACCOUNT)][-1] == DAYS[-1].isoformat()
    assert w.crit == []
    assert w.sent_count() == 6


def test_kiwoom_sim_ws_drop_midday_reconnect_releases_k5(kworld: KiwoomPaperWorld) -> None:
    w = kworld
    start_and_resume(w)
    d = DAYS[0]
    w.run_until(kst(d, 11, 0))
    assert w.position() == 1
    n = len(w.states)
    w.sim.drop_ws()                                          # 실시간 연결 끊김 → 자동 재접속
    assert _wait(lambda: WsState.RECONNECTED in w.states[n:]), w.states[n:]
    assert WsState.OFFLINE in w.states[n:]
    w.step()                                                 # 큐의 K5 발동·경로 정상 보고 → 요청 대조
    w.step()
    k5 = w.repo.select("SELECT state, released_by, conditions FROM halt WHERE halt_type='K5'")
    assert len(k5) == 1, "끊김이 K5로 기록되지 않음"
    assert "K5" not in w.active(), [json.loads(r["conditions"]) for r in k5]
    assert (k5[0]["state"], k5[0]["released_by"]) == ("RELEASED", "auto")
    assert w.reconciles[-1] >= kst(d, 11, 0)                 # 재접속 뒤 대조로 해제
    w.run_until(kst(d, 14, 31))
    assert buy_sell_of_day(w, d) == [("ENTRY", "FILLED"), ("EXIT", "FILLED")]
    assert w.position() == 0
