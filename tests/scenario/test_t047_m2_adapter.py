"""CP-036 T-047 — 바이낸스 어댑터·주문 전송 스위치·시작 시 rateLimits 예산 적재 (D-203·D-204·D-209 일부).

검증: V-283 (b) rateLimits 없음 → 시작 거부(주문 0), V-284 엔진 경로(실운영 조립 build_engine(M2) + 실어댑터 + 합성 거래소),
V-285 (a)(c)(d)(e) 설정·시작 판정. 네트워크 없음 — 합성 거래소(tests/support/binance_exchange_sim)만. 키는 가상 값.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.contract.test_binance_rules import _symbol
from tests.scenario.test_tm2_08_market_split import M2_APP, make_m2_root
from tests.support.binance_exchange_sim import RATE_LIMITS, ExchangeSim
from tests.support.invariants import check_invariants
from tests.support.m2_engine_world import CAPS_M2, ClockOk, FixedM2Strategy, fixed_spec, m2_bars
from tests.support.m2_fakes import FEE_RATE, M2_ORDER_POLICY, QUOTE_QUANTUM, m2_limits
from trading_system.app.main import run_main
from trading_system.app.startup import check_order_switch
from trading_system.app.wiring import SpotInputs, StrategyInput, build_engine
from trading_system.broker.binance.client import TESTNET_REST
from trading_system.broker.binance.run_start import SpotStartRefused, start_spot_session
from trading_system.config.app_file import parse_app_settings
from trading_system.config.errors import ConfigError
from trading_system.domain.clock import FixedClock
from trading_system.domain.enums import Mode
from trading_system.domain.market import CostRates, GateSettings, Quote
from trading_system.marketdata.spot_facts import SpotFactsStore
from trading_system.storage.repository import OrderIdentity, Repository

D = Decimal
SYM = "BTCUSDT"
ACCOUNT = "BN-PAPER-01"
PAIRS = {SYM: ("BTC", "USDT")}
DAY = date(2026, 10, 10)
SETTINGS = GateSettings(60, D("0.05"), CostRates(FEE_RATE, D("0"), QUOTE_QUANTUM))
REPO = Path(__file__).resolve().parents[2]


class _Secret:
    def __init__(self, v: str) -> None:
        self._v = v

    def reveal(self) -> str:
        return self._v


KEY, SECRET = _Secret("synthetic-run-key-31f0"), _Secret("synthetic-run-secret-a84c")


def _ms(clock: FixedClock) -> int:
    return int(clock.now().timestamp() * 1000)


def _start(sim: ExchangeSim, clock: FixedClock, *, orders: bool = True, facts: SpotFactsStore | None = None,
           base_url: str = TESTNET_REST, alerts: list | None = None):  # type: ignore[no-untyped-def]
    return start_spot_session(base_url, KEY, SECRET, sim.factory(), symbols=[SYM], account=ACCOUNT,
                              now_ms=lambda: _ms(clock), sleep=lambda s: None, orders_enabled=orders,
                              units=facts.units if facts is not None else None,
                              on_alert=((lambda k, m: alerts.append((k, m))) if alerts is not None else lambda *a: None))


# --- 시작: exchangeInfo·rateLimits 적재 (D-209, D-113, V-283 b) ---------------------------------------------

def test_start_loads_rules_and_budget_from_exchange_info_queries_only() -> None:
    clock = FixedClock(datetime(2026, 10, 10, 0, 30, tzinfo=UTC))
    sim = ExchangeSim(lambda: _ms(clock))
    s = _start(sim, clock)
    assert s.rules[SYM].lot_step == D("0.00001") and s.rules[SYM].min_notional == D("5")
    assert s.budget.weight_budget(60_000) == 3000                 # 6000의 50%
    assert sim.paths() == [("GET", "/api/v3/time"), ("GET", "/api/v3/exchangeInfo")]
    assert sim.order_posts() == [] and s.guard.order_sent == 0
    assert s.server_offset_ms == 0


@pytest.mark.parametrize("body,needle", [
    ({"timezone": "UTC", "symbols": [_symbol()]}, "rateLimits"),
    ({"timezone": "UTC", "rateLimits": [], "symbols": [_symbol()]}, "rateLimits"),
    ({"timezone": "UTC", "rateLimits": RATE_LIMITS, "symbols": []}, SYM),
    ({"timezone": "UTC", "rateLimits": RATE_LIMITS, "symbols": [_symbol(status="BREAK")]}, "TRADING"),
])
def test_v283b_start_refused_without_rate_limits_or_rules(body: dict, needle: str) -> None:
    clock = FixedClock(datetime(2026, 10, 10, 0, 30, tzinfo=UTC))
    sim = ExchangeSim(lambda: _ms(clock), exchange_body=body)
    with pytest.raises(SpotStartRefused) as e:
        _start(sim, clock)
    assert needle in str(e.value)
    assert all(p != "/api/v3/account" for _, p in sim.paths()) and sim.order_posts() == []   # 서명 조회·주문 0


def test_start_refused_when_exchange_info_query_fails() -> None:
    clock = FixedClock(datetime(2026, 10, 10, 0, 30, tzinfo=UTC))
    sim = ExchangeSim(lambda: _ms(clock))
    sim._get_exchangeInfo = lambda p: __import__("tests.support.binance_exchange_sim",  # type: ignore[method-assign]
                                                 fromlist=["_reply"])._reply(503, None)
    with pytest.raises(SpotStartRefused, match="exchangeInfo"):
        _start(sim, clock)


@pytest.mark.parametrize("url", ["https://api.binance.com", "https://demo-api.binance.com",
                                 "https://testnet.binance.vision:444", "http://testnet.binance.vision"])
def test_v285e_start_refused_for_hosts_outside_start_table_before_transport(url: str) -> None:
    clock = FixedClock(datetime(2026, 10, 10, 0, 30, tzinfo=UTC))
    sim = ExchangeSim(lambda: _ms(clock))
    made: list[int] = []
    factory = sim.factory()

    def counting(pre, after):  # type: ignore[no-untyped-def]
        made.append(1)
        return factory(pre, after)
    with pytest.raises(SpotStartRefused):
        start_spot_session(url, KEY, SECRET, counting, symbols=[SYM], account=ACCOUNT, now_ms=lambda: _ms(clock),
                           sleep=lambda s: None, orders_enabled=False, units=None, on_alert=lambda *a: None)
    assert made == [] and sim.sent == []


def test_server_time_offset_warning_alert() -> None:
    clock = FixedClock(datetime(2026, 10, 10, 0, 30, tzinfo=UTC))
    sim = ExchangeSim(lambda: _ms(clock))
    sim.server_offset_ms = 600
    alerts: list = []
    s = _start(sim, clock, alerts=alerts)
    assert s.server_offset_ms == 600 and any(k == "CLOCK" for k, _ in alerts)


# --- V-285 주문 전송 스위치 설정·시작 판정 (D-204) ----------------------------------------------------------

def _app(paper: str, market: str = "M2"):  # type: ignore[no-untyped-def]
    raw = M2_APP.format(acct=ACCOUNT, broker="binance-spot-testnet") + paper
    if market == "M1":
        raw = raw.replace('market = "M2"\n', "").replace('broker = "binance-spot-testnet"\n', "")
        raw = raw.replace(ACCOUNT, "KR-PAPER-01")
    import tomllib
    return parse_app_settings(tomllib.loads(raw), Mode.PAPER, REPO)


APPROVALS = REPO / "docs" / "approvals.md"   # 재검토 DP-66: 스위치가 켜지면 A-ID가 승인 기록 표에 행으로 있어야 한다


def test_order_switch_keys_default_off() -> None:
    app = _app("[paper]\nconnection_approved = true\n")
    assert (app.paper_orders_approved, app.paper_orders_approval) == (False, None)
    sw = check_order_switch(app, deployed_strategies=0, approvals_file=APPROVALS)
    assert sw.enabled is False and "주문 차단" in sw.describe()


def test_v285a_switch_off_with_strategy_refuses_start() -> None:
    app = _app("[paper]\nconnection_approved = true\n")
    with pytest.raises(ConfigError, match="orders_approved"):
        check_order_switch(app, deployed_strategies=1, approvals_file=APPROVALS)


@pytest.mark.parametrize("extra", ["", 'orders_approval = "A1"\n', 'orders_approval = "approved"\n'])
def test_v285c_switch_on_without_valid_approval_refuses(extra: str) -> None:
    app = _app("[paper]\nconnection_approved = true\norders_approved = true\n" + extra)
    with pytest.raises(ConfigError, match="orders_approval"):
        check_order_switch(app, deployed_strategies=1, approvals_file=APPROVALS)


def test_v285d_switch_on_with_approval_id(tmp_path: Path) -> None:
    app = _app('[paper]\nconnection_approved = true\norders_approved = true\norders_approval = "A-123"\n')
    table = tmp_path / "approvals.md"                                     # 재검토 DP-66: 표에 행이 있는 A-ID
    table.write_text("| A-ID | 일자 |\n|---|---|\n| A-123 | 2026-10-16 |\n", encoding="utf-8")
    sw = check_order_switch(app, deployed_strategies=1, approvals_file=table)
    assert (sw.enabled, sw.approval) == (True, "A-123") and "A-123" in sw.describe()


def test_m1_paper_section_still_rejects_order_keys() -> None:
    with pytest.raises(ConfigError, match="connection_approved만"):
        _app("[paper]\nconnection_approved = true\norders_approved = true\n", market="M1")


def test_orders_approved_must_be_bool() -> None:
    with pytest.raises(ConfigError, match="orders_approved"):
        _app('[paper]\norders_approved = "yes"\n')


def test_run_m2_paper_default_refused_listing_what_is_missing(tmp_path: Path,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    root = make_m2_root(tmp_path, Mode.PAPER)
    assert run_main(root, {"TRADE_MODE": "PAPER"}, market="M2") == 2
    out = capsys.readouterr().out
    assert "connection_approved" in out and "BINANCE_TESTNET_API_KEY" in out
    assert "placeholder" not in out and not (root / "var").exists()


# --- V-284 엔진 경로: build_engine(M2) + BinanceSpotAdapter + 합성 거래소 ----------------------------------

class Run:
    def __init__(self, tmp: Path, *, orders: bool = True) -> None:
        self.clock = FixedClock(datetime(2026, 10, 10, 0, 30, tzinfo=UTC))
        self.sim = ExchangeSim(lambda: _ms(self.clock), balances={"USDT": D("10000")}, price=D("60000.00"),
                               fee_rate=FEE_RATE)
        self.facts = SpotFactsStore(now=self.clock.now, universe=(SYM,))
        self.alerts: list = []
        self.session = _start(self.sim, self.clock, orders=orders, facts=self.facts, alerts=self.alerts)
        assert self.facts.load_rules(self.session.rules) == []
        self.facts.set_daily_bars(SYM, m2_bars(DAY - timedelta(days=60), DAY + timedelta(days=5), "60000"))
        self.quote()
        # 모드 BACKTEST: 전략 상태 DRAFT로 신호를 낼 수 있는 모드(PAPER는 등록·상태 전이 필요 — T-049 범위). 어댑터·합성 거래소는 같다
        self.repo = Repository(tmp / "trading.db", Mode.BACKTEST, order_identity=OrderIdentity.SYMBOL)
        spec = fixed_spec(universe=(SYM,))
        strategies = [StrategyInput(FixedM2Strategy(spec), {"qty": "0.00100"}, (time(1, 0), time(4, 0)))]
        self.engine = build_engine(mode=Mode.BACKTEST, repo=self.repo, clock=self.clock, broker=self.session.adapter,
                                   store=None, account=ACCOUNT, account_limits=m2_limits(), gate_settings=SETTINGS,
                                   caps=CAPS_M2, strategies=strategies, kill_file=tmp / "KILL",
                                   clock_health=ClockOk(), alert_thresholds=(10, 5), market="M2",
                                   spot=SpotInputs(self.facts, PAIRS, "USDT", M2_ORDER_POLICY))

    def quote(self) -> None:
        self.facts.update_quote(Quote(SYM, self.sim.price[SYM], self.clock.now()))

    def at(self, hh: int, mm: int, day: date = DAY) -> None:
        self.clock._now = datetime(day.year, day.month, day.day, hh, mm, tzinfo=UTC)  # noqa: SLF001
        self.quote()
        self.engine.tick()

    def last_recon(self) -> str:
        return self.repo.select("SELECT result FROM reconciliation ORDER BY created_seq DESC LIMIT 1")[0]["result"]


def test_v284_engine_round_trip_through_real_adapter(tmp_path: Path) -> None:
    r = Run(tmp_path)
    try:
        r.engine.account_state.set_baseline(ACCOUNT, D("10000"), r.clock.now())  # type: ignore[attr-defined]
        r.engine.start()
        with r.repo.tx():
            r.repo.insert_command("c1", "LOCAL", "tester", "RESUME", {"account": None, "halt_type": "K8"},
                                  r.clock.now())
        r.at(0, 59)
        assert "K8" not in r.repo.active_halts(ACCOUNT), r.repo.halt_rows(ACCOUNT)
        r.at(1, 0)
        [p] = r.sim.order_posts()
        assert (p["type"], p["timeInForce"], p["side"], p["quantity"], p["price"]) == \
            ("LIMIT", "IOC", "BUY", "0.00100", "60300.00")
        r.at(1, 2)
        assert r.repo.position(ACCOUNT, SYM).quantity == D("0.000999")      # 순 수령 (수수료 = BTC)
        assert r.last_recon() == "MATCH"
        r.at(4, 0)
        sell = r.sim.order_posts()[-1]
        assert (sell["type"], sell["side"], sell["quantity"]) == ("MARKET", "SELL", "0.00099")   # LOT 단위 내림 (D-208)
        r.at(4, 2)
        assert r.repo.position(ACCOUNT, SYM).quantity == D("0.000009")      # 잔량 dust
        assert r.repo.active_ownership(ACCOUNT, SYM) is None
        assert r.last_recon() == "MATCH" and r.repo.active_halts(ACCOUNT) == []
        assert len(r.sim.order_posts()) == 2                                 # 재전송 없음
        assert check_invariants(r.repo.connection, Mode.BACKTEST.value) == []
    finally:
        r.repo.close()


def test_v284_unknown_result_resolved_by_client_id_lookup_without_resend(tmp_path: Path) -> None:
    r = Run(tmp_path)
    try:
        r.engine.account_state.set_baseline(ACCOUNT, D("10000"), r.clock.now())  # type: ignore[attr-defined]
        r.engine.start()
        with r.repo.tx():
            r.repo.insert_command("c1", "LOCAL", "tester", "RESUME", {"account": None, "halt_type": "K8"},
                                  r.clock.now())
        r.at(0, 59)
        r.sim.order_replies = ["timeout+"]                                     # 접수됐지만 응답 유실
        r.at(1, 0)
        [intent] = r.repo.select("SELECT state FROM intent")
        assert intent["state"] == "UNKNOWN"
        for m in range(1, 4):
            r.at(1, m)
        [intent] = r.repo.select("SELECT state FROM intent")
        assert intent["state"] == "FILLED" and len(r.sim.order_posts()) == 1   # 조회로 해소, 같은 의도 재전송 0
        assert any(p == "/api/v3/order" and m == "GET" for m, p in r.sim.paths())
        assert r.repo.position(ACCOUNT, SYM).quantity == D("0.000999")
    finally:
        r.repo.close()


def test_v285b_engine_orders_off_entry_not_sent(tmp_path: Path) -> None:
    r = Run(tmp_path, orders=False)
    try:
        r.engine.account_state.set_baseline(ACCOUNT, D("10000"), r.clock.now())  # type: ignore[attr-defined]
        r.engine.start()
        with r.repo.tx():
            r.repo.insert_command("c1", "LOCAL", "tester", "RESUME", {"account": None, "halt_type": "K8"},
                                  r.clock.now())
        r.at(0, 59)
        r.at(1, 0)
        assert r.sim.order_posts() == [] and r.session.guard.order_sent == 0
        [intent] = r.repo.select("SELECT state, closed_reason FROM intent")
        assert intent["state"] == "NOT_SENT" and "D-204" in intent["closed_reason"]
        assert any(k == "ORDER_BLOCKED" for k, _ in r.alerts)
    finally:
        r.repo.close()
