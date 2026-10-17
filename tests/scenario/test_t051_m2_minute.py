"""CP-037 T-051 (A-113) — M2 분봉 공급·저장·품질 (D-220). V-292(확정·품질·예산), V-293(저장·재현).

네트워크 없음: 합성 거래소(tests/support/binance_exchange_sim — 1m klines 행은 BN §8 필드 정의, D-084 방식)·가짜 시계·
임시 SQLite. 테스트넷 1분봉 주기 조회는 별도 승인 전이라 설정 기본값 꺼짐(이 시험은 주입한 계획으로만 켠다).
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_t047_m2_adapter import ACCOUNT, KEY, SECRET, SYM, _ms
from tests.scenario.test_tm2_08_market_split import M2_MARKET, make_m2_root
from tests.support.binance_exchange_sim import RATE_LIMITS, ExchangeSim
from tests.support.binance_ws_fake import FakeWsApi
from tests.support.m2_engine_world import CAPS_M2, M2World, fixed_spec
from trading_system.app.m2_minutes import (
    MINUTE_CAPABILITY, MINUTE_INPUT_AUDIT, MINUTE_QUALITY_AUDIT, MINUTE_SOURCE, MINUTE_STORED_AUDIT, M2MinuteInput,
)
from trading_system.app.main import run_main
from trading_system.app.wiring import StrategyInput
from trading_system.broker.binance.client import TESTNET_REST, BinanceSpotClient
from trading_system.broker.binance.feed import (
    MAX_MINUTE_LOOKBACK, BinanceFeed, FeedPlan, MinutePlan, minute_budget_problem,
)
from trading_system.broker.binance.run_guard import RunGuard
from trading_system.broker.binance.run_start import M2RunConnector
from trading_system.broker.binance.signing import Credentials
from trading_system.broker.binance.weights import WeightBudget, parse_rate_limits
from trading_system.config.errors import ConfigError
from trading_system.config.m2_files import parse_m2_market
from trading_system.domain.clock import FixedClock
from trading_system.domain.enums import Mode
from trading_system.domain.minute_bar import MinuteBar
from trading_system.domain.minute_series import NO_ADJUSTMENT, bar_fingerprint
from trading_system.domain.strategy import EvaluationSnapshot
from trading_system.marketdata.spot_facts import SpotFactsStore
from trading_system.marketdata.spot_minute import SpotMinuteBook
from trading_system.storage.repository import Repository

D = Decimal
REPO = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 10, 0, 30, tzinfo=UTC)
PLAN = MinutePlan(period_minutes=5, lookback_bars=10)


def utc(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 10, 10, hh, mm, ss, tzinfo=UTC)


class Alerts:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str | None]] = []

    def alert(self, level: str, message: str, ref: str | None) -> None:
        self.items.append((level, message, ref))

    def texts(self) -> list[str]:
        return [m for _, m, _ in self.items]


class Halts:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str | None]] = []

    def activate(self, halt_type, actor, reason, account=None, **kw):  # type: ignore[no-untyped-def]
        self.items.append((halt_type.value, reason, kw.get("instrument")))
        return "h"

    def report_condition(self, *a, **kw) -> list[str]:  # type: ignore[no-untyped-def]
        return []


class MinuteRig:
    """시세 공급(binance-feed) + 분봉 버퍼 + 판정 입력(저장 → 창 → 지문). 판정·실행 스레드 몫은 직접 부른다."""

    def __init__(self, tmp: Path, plan: MinutePlan | None = PLAN, *, limit: int = 6000) -> None:
        self.clock = FixedClock(T0)
        self.sim = ExchangeSim(lambda: _ms(self.clock))
        self.feed_alerts: list[tuple[str, str]] = []
        self.halts = Halts()
        self.budget = WeightBudget(parse_rate_limits([{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE",
                                                        "intervalNum": 1, "limit": limit}]), lambda: _ms(self.clock))
        self.guard = RunGuard(self.sim, orders_enabled=False)
        self.client = BinanceSpotClient(TESTNET_REST, Credentials(KEY, SECRET), self.guard, self.budget,
                                        now_ms=lambda: _ms(self.clock), sleep=lambda s: None,
                                        on_alert=lambda k, m: self.feed_alerts.append((k, m)))
        self.facts = SpotFactsStore(now=self.clock.now, universe=(SYM,))
        self.book = SpotMinuteBook((SYM,), account=ACCOUNT)
        self.feed = BinanceFeed(self.client, self.facts, [SYM], now=self.clock.now,
                                alert=lambda k, m: self.feed_alerts.append((k, m)), account=ACCOUNT,
                                minute=plan, minute_sink=self.book if plan is not None else None)
        self.repo = Repository(tmp / "trading.db", Mode.PAPER)
        self.alerts = Alerts()
        self.input = M2MinuteInput(self.book, lookback_bars=plan.lookback_bars if plan else 10)

    def at(self, t: datetime) -> None:
        self.clock._now = t  # noqa: SLF001
        self.feed.step()

    def decide(self, t: datetime, ev: str = "ev") -> dict:
        self.clock._now = t  # noqa: SLF001
        return self.input.inputs(self.repo, self.alerts, [SYM], t, t, ev)

    def minute_calls(self) -> list[dict[str, str]]:
        return [prm for m, p, prm in self.sim.sent if p == "/api/v3/klines" and prm.get("interval") == "1m"]

    def audits(self, kind: str) -> list[dict]:
        return [e for e in self.repo.audit_events() if e["kind"] == kind]

    def close(self) -> None:
        self.repo.close()


@pytest.fixture
def rig(tmp_path: Path):  # type: ignore[no-untyped-def]
    r = MinuteRig(tmp_path)
    yield r
    r.close()


def _ms_of(t: datetime) -> int:
    return int(t.timestamp() * 1000)


# --- 공급 일정·전송 경계 ---------------------------------------------------------------------------------------

def test_v292_feed_polls_1m_on_grid_only_through_run_guard(rig: MinuteRig) -> None:
    rig.at(utc(0, 30, 1))                                        # 직전 격자 00:25:02 + 유예 60초 지남 → 조회 없음
    assert rig.minute_calls() == []
    rig.at(utc(0, 30, 2))
    rig.at(utc(0, 30, 3))                                        # 같은 격자 시각은 한 번만
    rig.at(utc(0, 34, 59))
    assert len(rig.minute_calls()) == 1
    rig.at(utc(0, 35, 2))
    calls = rig.minute_calls()
    assert len(calls) == 2 and calls[0]["symbol"] == SYM and calls[0]["limit"] == str(PLAN.lookback_bars + 3)
    assert rig.guard.blocked_total == 0 and rig.guard.order_sent == 0
    assert all(p != "/api/v3/order" for _, p, _ in rig.sim.sent)
    assert ("GET", "/api/v3/klines") in rig.guard.calls


def test_minute_polling_off_by_default(tmp_path: Path) -> None:
    r = MinuteRig(tmp_path, plan=None)
    try:
        for t in (utc(0, 30, 2), utc(0, 35, 2), utc(0, 40, 2)):
            r.at(t)
        assert r.minute_calls() == [] and FeedPlan() == FeedPlan()       # FeedPlan 기본값 그대로
    finally:
        r.close()


def test_minute_plan_requires_sink_and_lookback_limit() -> None:
    with pytest.raises(ValueError):
        MinutePlan(period_minutes=5, lookback_bars=MAX_MINUTE_LOOKBACK + 1)
    with pytest.raises(ValueError):
        MinutePlan(period_minutes=7, lookback_bars=10)
    clock = FixedClock(T0)
    sim = ExchangeSim(lambda: _ms(clock))
    budget = WeightBudget(parse_rate_limits(RATE_LIMITS), lambda: _ms(clock))
    client = BinanceSpotClient(TESTNET_REST, Credentials(KEY, SECRET), RunGuard(sim, orders_enabled=False), budget,
                               now_ms=lambda: _ms(clock), sleep=lambda s: None)
    with pytest.raises(ValueError):
        BinanceFeed(client, SpotFactsStore(now=clock.now, universe=(SYM,)), [SYM], now=clock.now,
                    alert=lambda k, m: None, minute=PLAN)


def test_v292_late_feed_step_skips_grid_time(rig: MinuteRig) -> None:
    rig.at(utc(0, 31, 3))                                        # 00:30:02 + 61초 → 늦은 조회 안 함
    assert rig.minute_calls() == [] and rig.feed.minute_skipped == 1


def test_fetch_failures_alert_throttled_dp67(rig: MinuteRig) -> None:
    rig.sim.market_down = True
    for t in (utc(0, 30, 2), utc(0, 35, 2), utc(0, 40, 2)):
        rig.at(t)
    one_m = [m for k, m in rig.feed_alerts if k == "FEED" and "1m" in m]
    assert len(one_m) == 1                                        # 같은 조건 반복 → 시작 1건 (DP-67)


# --- 닫힌 봉·저장 (V-292·V-293) ---------------------------------------------------------------------------------

def test_v292_in_progress_bar_not_stored_or_used(rig: MinuteRig) -> None:
    rig.at(utc(0, 30, 2))
    res = rig.decide(utc(0, 30, 2))[SYM]
    stored = rig.repo.minute_bars(SYM, 1, NO_ADJUSTMENT)
    assert stored and stored[-1].start == utc(0, 29)               # 진행 중 00:30 봉은 저장 안 함
    assert res.ok and [b.at for b in res.bars] == [utc(0, m) for m in range(20, 30)]
    assert rig.audits(MINUTE_INPUT_AUDIT)


def test_v293_rereceived_bar_with_other_values_keeps_stored_and_counts(rig: MinuteRig) -> None:
    rig.at(utc(0, 30, 2))
    assert rig.input.flush(rig.repo, rig.alerts, utc(0, 30, 2)) == (12, 0, 0)     # 00:18~00:29
    rig.sim.minute_close_delta = D("5")
    rig.at(utc(0, 35, 2))                                          # 00:23~00:34 수신, 00:23~00:29 값 다름
    assert rig.input.flush(rig.repo, rig.alerts, utc(0, 35, 2)) == (5, 0, 7)
    old = [b for b in rig.repo.minute_bars(SYM, 1, NO_ADJUSTMENT) if b.start <= utc(0, 29)]
    assert {b.close for b in old} == {D("60000.00")}               # 기존 값 유지 (추가 전용)
    [rec] = rig.audits(MINUTE_STORED_AUDIT)
    assert '"differing": 7' in rec["summary"] and SYM in rec["summary"]
    row = rig.repo.connection.execute("SELECT source, adjusted, mode FROM minute_bar LIMIT 1").fetchone()
    assert tuple(row) == (MINUTE_SOURCE, NO_ADJUSTMENT, "PAPER")


def test_v293_snapshot_fingerprint_equals_stored_bars_and_replay_same_signal(rig: MinuteRig) -> None:
    rig.at(utc(0, 30, 2))
    first = rig.decide(utc(0, 30, 2), "ev-1")[SYM]
    window = rig.repo.minute_bars(SYM, 1, NO_ADJUSTMENT, start=utc(0, 20), end=utc(0, 30))
    assert first.record["fingerprint"] == bar_fingerprint(window)
    assert first.record["first"] == utc(0, 20).isoformat() and first.record["last"] == utc(0, 29).isoformat()
    rig.sim.minute_close_delta = D("-7")                          # 재수신 값이 달라도 저장 봉이 판정 입력
    rig.at(utc(0, 35, 2))
    again = rig.decide(utc(0, 30, 2), "ev-1-replay")[SYM]
    assert again.bars == first.bars and again.record["fingerprint"] == first.record["fingerprint"]

    def toy_signal(bars) -> str:  # type: ignore[no-untyped-def]
        return "UP" if bars[-1].close >= bars[0].close else "DOWN"
    assert toy_signal(first.bars) == toy_signal(again.bars)


# --- 누락·결측·품질 (V-292) ---------------------------------------------------------------------------------------

def test_v292_last_bar_missing_invalid_recorded_three_in_row_warns_once_then_recovers(rig: MinuteRig) -> None:
    rig.at(utc(0, 30, 2))
    assert rig.decide(utc(0, 30, 2), "e0")[SYM].ok
    times = [utc(0, 35, 2), utc(0, 40, 2), utc(0, 45, 2), utc(0, 50, 2)]
    for i, t in enumerate(times):
        rig.sim.minute_skip.add(_ms_of(t - timedelta(minutes=1, seconds=2)))   # 판정 직전 닫힌 봉 누락
        rig.at(t)
        res = rig.decide(t, f"e{i + 1}")[SYM]
        assert not res.ok and res.bars == () and res.record["status"] == "LAST_MISSING"
    warns = [m for m in rig.alerts.texts() if "누락" in m]
    assert len(warns) == 1 and "3회" in warns[0]                    # 3회째 1건, 4회째는 묶음(30분 안)
    recs = rig.audits(MINUTE_INPUT_AUDIT)
    assert len(recs) == 5 and all("LAST_MISSING" in r["summary"] for r in recs[1:])
    rig.sim.minute_skip.clear()                                   # 늦게라도 봉이 오면(재수신) 창이 다시 채워진다
    rig.at(utc(0, 55, 2))
    assert rig.decide(utc(0, 55, 2), "e9")[SYM].ok
    assert any("회복" in m for m in rig.alerts.texts())


def test_v292_missing_alert_summary_after_30_minutes(rig: MinuteRig) -> None:
    t = utc(1, 0, 2)
    for i in range(40):                                           # 5분마다 계속 누락 — 30분마다 요약 1건
        rig.decide(t + timedelta(minutes=5 * i), f"m{i}")
    warns = [m for m in rig.alerts.texts() if "누락" in m]
    assert 2 <= len(warns) <= 8 and all("판정 신호 무효" in w for w in warns)


def test_v292_gap_inside_window_invalid(rig: MinuteRig) -> None:
    rig.sim.minute_skip.add(_ms_of(utc(0, 24)))
    rig.at(utc(0, 30, 2))
    res = rig.decide(utc(0, 30, 2))[SYM]
    assert not res.ok and res.record["status"] == "GAP" and res.record["missing"] == 1


def _rows(*specs: tuple[datetime, str, str, str, str, str]) -> list[list[object]]:
    return [[_ms_of(t), o, h, lo, c, v, _ms_of(t) + 59_999, "1", 1, "0", "0", "0"] for t, o, h, lo, c, v in specs]


GOOD = "100", "101", "99", "100", "1"


@pytest.mark.parametrize("body", [
    _rows((utc(0, 28), *GOOD), (utc(0, 28), *GOOD)),                               # 같은 시각 중복
    _rows((utc(0, 29), *GOOD), (utc(0, 28), *GOOD)),                               # 역순
    _rows((utc(0, 29), "100", "101", "99", "100", "-1")),                          # 음수
    _rows((utc(0, 29), "100", "99", "98", "100", "1")),                            # 고가 < 시가 (고저 모순)
    _rows((utc(0, 29), "0", "101", "0", "100", "1")),                              # 0 이하 가격
], ids=["duplicate", "reverse", "negative", "high-low", "zero-price"])
def test_v292_quality_violation_symbol_invalid_no_halt(rig: MinuteRig, body: list) -> None:
    # 재검토(DP-72 개정): 이전 기대값 "K10 1건" → K10 없음. K10은 종목 범위라 분봉을 쓰지 않는 전략(m2-verify)의 진입까지
    # 막고 일봉 data_ok 보고(UTC 하루 1회)로만 풀렸다. 이제 CAP-DAT-02 전략의 그 심볼 분봉 입력만 무효 + 감사·묶음 경고.
    rig.at(utc(0, 25, 2))                                        # 정상 수신 먼저
    rig.sim.minute_body = body
    rig.at(utc(0, 30, 2))
    res = rig.decide(utc(0, 30, 2))[SYM]
    assert not res.ok and res.record["status"] == "QUALITY" and res.bars == ()
    assert rig.halts.items == [] and rig.audits(MINUTE_QUALITY_AUDIT)
    rig.sim.minute_body = None                                   # 다음 정상 수신 → 판정 입력은 다시 유효
    rig.at(utc(0, 35, 2))
    assert rig.decide(utc(0, 35, 2), "ev-next")[SYM].ok


def test_book_ignores_symbols_outside_universe_and_bounds_pending() -> None:
    book = SpotMinuteBook((SYM,), max_pending=5)
    bars = [MinuteBar(SYM, 1, utc(0, m), D(1), D(1), D(1), D(1), D(1), NO_ADJUSTMENT) for m in range(10)]
    book.put_minute_bars("ETHUSDT", bars, utc(1, 0))
    book.put_minute_bars(SYM, bars, utc(1, 0))
    drained = book.drain()
    assert [b.start for b in drained] == [utc(0, m) for m in range(5, 10)] and book.dropped == 5
    assert book.drain() == []


# --- 호출 예산 시작 검사 (V-292 예산 초과 → 시작 거부, 브로커 주문 0) -----------------------------------------------

def test_v292_budget_problem_formula() -> None:
    limits = parse_rate_limits([{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1,
                                 "limit": 200}])                  # 내부 예산 100, 분봉 몫 10%= 10
    assert minute_budget_problem(limits, 5, MinutePlan(1, 60)) is None          # 5 × 1쪽 × 2 = 10
    problem = minute_budget_problem(limits, 6, MinutePlan(1, 60))
    assert problem is not None and "10%" in problem
    assert minute_budget_problem(parse_rate_limits(RATE_LIMITS), 1, MinutePlan(1, 997)) is None


def test_v292_connector_refuses_start_when_minute_budget_exceeded() -> None:
    clock = FixedClock(T0)
    sim = ExchangeSim(lambda: _ms(clock))
    from tests.contract.test_binance_rules import _symbol
    sim.exchange_body = {"timezone": "UTC", "serverTime": _ms(clock), "exchangeFilters": [],
                         "rateLimits": [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1,
                                         "limit": 20}], "symbols": [_symbol(SYM)]}
    conn = M2RunConnector(sim.factory(), FakeWsApi(sim).connect)
    out = conn(api_key=KEY, secret=SECRET, symbols=(SYM,), account=ACCOUNT, now=clock.now, sleep=lambda s: None,
               orders_enabled=False, sink=SpotFactsStore(now=clock.now, universe=(SYM,)),
               on_alert=lambda k, m: None, on_stream_state=lambda s, m: None, halts=Halts(),
               minute_plan=(1, 60, 2), minute_sink=SpotMinuteBook((SYM,)))
    assert isinstance(out, str) and "분봉" in out and "예산" in out
    assert sim.order_posts() == [] and ("GET", "/api/v3/klines") not in sim.paths()


# --- 설정 (기본 꺼짐, 켜려면 승인 A-ID) ------------------------------------------------------------------------------

def _market(extra: str) -> dict:
    import tomllib
    return tomllib.loads(M2_MARKET + extra)


def test_minute_settings_default_off() -> None:
    m = parse_m2_market(_market("")).minute
    assert m.enabled is False


def test_minute_settings_enabled_parsed() -> None:
    m = parse_m2_market(_market('[minute]\nenabled = true\nperiod_minutes = 5\nlookback_bars = 60\n'
                                'confirm_margin_seconds = 2\napproval = "A-999"\n')).minute
    assert (m.enabled, m.period_minutes, m.lookback_bars, m.confirm_margin_seconds, m.approval) == \
        (True, 5, 60, 2, "A-999")


@pytest.mark.parametrize("extra", [
    '[minute]\nenabled = true\nperiod_minutes = 5\nlookback_bars = 60\n',                       # 승인 A-ID 없음
    '[minute]\nenabled = true\nperiod_minutes = 7\nlookback_bars = 60\napproval = "A-999"\n',    # 1440 약수 아님
    '[minute]\nenabled = true\nperiod_minutes = 5\nlookback_bars = 998\napproval = "A-999"\n',  # 1쪽 초과
    '[minute]\nenabled = true\nperiod_minutes = 5\napproval = "A-999"\n',                       # 창 길이 없음
    '[minute]\nenabled = "yes"\n',                                                             # bool 아님
    '[minute]\nenabled = false\npolling = 1\n',                                                # 알 수 없는 키
    '[minute]\nenabled = true\nperiod_minutes = 5\nlookback_bars = 60\nconfirm_margin_seconds = 60\n'
    'approval = "A-999"\n',
])
def test_minute_settings_invalid_refused(extra: str) -> None:
    with pytest.raises(ConfigError):
        parse_m2_market(_market(extra))


MINUTE_ON = '\n[minute]\nenabled = true\nperiod_minutes = 1\nlookback_bars = 60\napproval = "A-999"\n'


def _paper_root(tmp: Path, *, approvals: str) -> Path:
    root = make_m2_root(tmp, Mode.PAPER, symbols='quote_asset = "USDT"\nsymbols = ["BTCUSDT"]\n')
    d = root / "config" / "m2"
    (d / "app.paper.toml").write_text((d / "app.paper.toml").read_text(encoding="utf-8")
                                      + "\n[paper]\nconnection_approved = true\n", encoding="utf-8")
    (d / "market.paper.toml").write_text(M2_MARKET + MINUTE_ON, encoding="utf-8")
    (root / ".env.paper").write_text("TRADE_MODE=PAPER\nBINANCE_TESTNET_API_KEY=synthetic-k\n"
                                     "BINANCE_TESTNET_SECRET_KEY=synthetic-s\n", encoding="utf-8")
    (root / "docs").mkdir()
    shutil.copy(REPO / "docs" / "capabilities.md", root / "docs" / "capabilities.md")
    (root / "docs" / "approvals.md").write_text(approvals, encoding="utf-8")
    return root


def _wall() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def test_run_m2_minute_on_without_recorded_approval_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _paper_root(tmp_path, approvals="| A-001 | x |\n")
    sim = ExchangeSim(_wall)
    assert run_main(root, {"TRADE_MODE": "PAPER"}, max_seconds=0.3, market="M2",
                    m2_connector=M2RunConnector(sim.factory(), FakeWsApi(sim).connect)) == 2
    out = capsys.readouterr().out
    assert "minute" in out and "A-999" in out and sim.sent == []


def test_run_m2_minute_on_budget_exceeded_refused_no_orders(tmp_path: Path,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    root = _paper_root(tmp_path, approvals="| A-999 | 시험용 가상 승인 |\n")
    sim = ExchangeSim(_wall)
    from tests.contract.test_binance_rules import _symbol
    sim.exchange_body = {"timezone": "UTC", "serverTime": _wall(), "exchangeFilters": [], "symbols": [_symbol(SYM)],
                         "rateLimits": [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1,
                                         "limit": 20}]}
    assert run_main(root, {"TRADE_MODE": "PAPER"}, max_seconds=0.3, market="M2",
                    m2_connector=M2RunConnector(sim.factory(), FakeWsApi(sim).connect)) == 2
    out = capsys.readouterr().out
    assert "예산" in out and sim.order_posts() == []


def test_run_m2_minute_on_starts_with_minute_feed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _paper_root(tmp_path, approvals="| A-999 | 시험용 가상 승인 |\n")
    sim = ExchangeSim(_wall)
    assert run_main(root, {"TRADE_MODE": "PAPER"}, max_seconds=0.3, market="M2",
                    m2_connector=M2RunConnector(sim.factory(), FakeWsApi(sim).connect,
                                                feed_plan=FeedPlan(loop_s=0.05))) == 0
    out = capsys.readouterr().out
    assert "분봉 조회 켜짐" in out and "A-999" in out and sim.order_posts() == []


# --- 판정 입력 (SpotProfile — CAP-DAT-02 선언 전략만) ------------------------------------------------------------

class Recorder:
    def __init__(self, spec) -> None:  # type: ignore[no-untyped-def]
        self.spec = spec
        self.snaps: list[EvaluationSnapshot] = []

    def evaluate(self, snapshot: EvaluationSnapshot, params) -> list:  # type: ignore[no-untyped-def]
        self.snaps.append(snapshot)
        return []


CAPS_MIN = {**CAPS_M2, MINUTE_CAPABILITY: {"M1": "미지원", "M2": "지원", "M3": "미지원"}}
WSYM = "TSTA-USDT"


def _world(tmp: Path, *, declare: bool, minutes: bool) -> tuple[M2World, Recorder, SpotMinuteBook]:
    caps = ("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-ORD-02", "CAP-INS-X1") + \
        ((MINUTE_CAPABILITY,) if declare else ())
    rec = Recorder(fixed_spec(strategy_id="t-m2min", capabilities=caps))
    book = SpotMinuteBook((WSYM,))
    w = M2World(tmp, strategies=[StrategyInput(rec, {"qty": "0.0100"}, (time(1, 0, 2),))], caps=CAPS_MIN,
                minutes=M2MinuteInput(book, lookback_bars=10) if minutes else None)
    bars = [MinuteBar(WSYM, 1, utc(0, 40) + timedelta(minutes=i), D(100), D(101), D(99), D(100), D(1), NO_ADJUSTMENT)
            for i in range(21)]                                   # 00:40~01:00 (01:00 진행 중)
    book.put_minute_bars(WSYM, bars, utc(1, 0, 2))
    w.set_baseline()
    return w, rec, book


def test_profile_fills_minute_bars_for_declared_strategy(tmp_path: Path) -> None:
    w, rec, _ = _world(tmp_path, declare=True, minutes=True)
    try:
        w.start_and_resume()
        w.tick_at(1, 0, 3)
        [snap] = rec.snaps
        v = snap.view(WSYM)
        assert v is not None and v.data_ok and [b.at for b in v.minute_bars] == [utc(0, m) for m in range(50, 60)]
        [a] = [e for e in w.repo.audit_events() if e["kind"] == MINUTE_INPUT_AUDIT]
        assert a["ref"] == snap.evaluation_id and bar_fingerprint(
            w.repo.minute_bars(WSYM, 1, NO_ADJUSTMENT, start=utc(0, 50), end=utc(1, 0))) in a["summary"]
    finally:
        w.close()


def test_profile_undeclared_strategy_unchanged(tmp_path: Path) -> None:
    w, rec, _ = _world(tmp_path, declare=False, minutes=True)
    try:
        w.start_and_resume()
        w.tick_at(1, 0, 3)
        v = rec.snaps[0].view(WSYM)
        assert v is not None and v.minute_bars == () and v.data_ok
        assert not [e for e in w.repo.audit_events() if e["kind"] == MINUTE_INPUT_AUDIT]
        assert w.repo.minute_bars(WSYM, 1, NO_ADJUSTMENT)              # 저장은 판정과 무관하게 tick마다
    finally:
        w.close()


def test_profile_declared_but_minutes_off_is_invalid(tmp_path: Path) -> None:
    w, rec, _ = _world(tmp_path, declare=True, minutes=False)
    try:
        w.start_and_resume()
        w.tick_at(1, 0, 3)
        v = rec.snaps[0].view(WSYM)
        # [T-052 DP-78] 기대값 변경: 분봉 무효는 data_ok가 아니라 minute_ok False (청산·감액 신호 보존, CLAUDE.md §8-3)
        assert v is not None and not v.minute_ok and v.data_ok and v.minute_bars == ()
        [a] = [e for e in w.repo.audit_events() if e["kind"] == MINUTE_INPUT_AUDIT]
        assert "DISABLED" in a["summary"]
    finally:
        w.close()
