"""T-010 시장 데이터·품질·달력·호가 (05-plan T-010 + CP-018 추가, 04 V-11·V-74·V-75·V-76·V-90·V-112, 03 D-055·D-056·D-088·D-095).

달력·호가 표의 값은 형식 시험용 가상값이다 (실제 KRX 휴장일·호가 단위 아님, X-U1·X-U3).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, PRICES, T0, Env, make_signal
from tests.support.gate_fakes import TEST_POLICY
from trading_system.config.errors import ConfigError
from trading_system.config.market_file import load_market_settings, parse_market_settings
from trading_system.domain.clock import KST
from trading_system.domain.enums import Check, GateResult, HaltType, OrderType, RequestSource, RequestType, Side
from trading_system.domain.market import InstrumentInfo, InstrumentKind, Quote, TradingStatus
from trading_system.domain.models import GateRequest
from trading_system.gate.gate import Gate
from trading_system.gate.ports import GateInputs, GatePolicy
from trading_system.marketdata.calendar import KrxCalendar, parse_calendar
from trading_system.marketdata.errors import MarketDataError
from trading_system.marketdata.quality import PREPROCESS_VERSION, RawDaily, preprocess_daily
from trading_system.marketdata.store import MarketDataStore
from trading_system.marketdata.ticks import load_ticks, parse_ticks
from trading_system.storage.alerts import AuditAlertSink

ROOT = Path(__file__).resolve().parents[2]
VIRTUAL_HOLIDAY = date(2026, 10, 2)        # 가상 휴장일 (실제 공휴일 여부와 무관)
VIRTUAL_SHORT_DAY = date(2026, 10, 5)      # 가상 단축 개장일
CAL_RAW = {"market": "KRX", "year": 2026, "source": "시험용 가상 달력", "source_version": "test",
           "holidays": [VIRTUAL_HOLIDAY], "special_sessions": [{"date": VIRTUAL_SHORT_DAY, "open": "10:00",
                                                               "close": "13:00"}]}
TICKS_RAW = {"source": "시험용 가상 표", "version": "test",
             "bands": {"STOCK": [{"below": "5000", "tick": "5"}, {"below": "none", "tick": "10"}],
                       "ETF": [{"below": "none", "tick": "5"}]}}


def calendar() -> KrxCalendar:
    return KrxCalendar({2026: parse_calendar(CAL_RAW, 2026)})


def store(env: Env | None = None, ticks: bool = True) -> MarketDataStore:
    s = MarketDataStore(calendar(), parse_ticks(TICKS_RAW) if ticks else None,
                        halts=env.halts if env else None,
                        alerts=AuditAlertSink(env.repo, env.clock) if env else None, account=ACCOUNT)
    for inst, price in PRICES.items():
        s.register(InstrumentInfo(inst, InstrumentKind.STOCK, Decimal("1"), Decimal("1")))
        s.update_status(inst, frozenset())
        s.update_quote(Quote(inst, price, env.clock.now() if env else T0, upper_limit=price))
    return s


def wire(env: Env, s: MarketDataStore) -> None:
    f = env.facts
    env.gate = Gate(env.repo, env.clock, GateInputs(f.limits, s, f.account, f.strategies, f.clock_health), TEST_POLICY)


def raw_days(days: list[date], closes: list[str], ca: set[date] | None = None) -> list[RawDaily]:
    return [RawDaily(d, Decimal(c), Decimal(c), Decimal(c), Decimal(c), Decimal("100"), d in (ca or set()))
            for d, c in zip(days, closes, strict=True)]


def trading_days(a: date, b: date) -> list[date]:
    cal = calendar()
    out, d = [], a
    while d <= b:
        if cal.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


# --- 달력 (03 D-055) -----------------------------------------------------------------------------

def test_calendar_sessions() -> None:
    cal = calendar()
    assert cal.session(date(2026, 10, 1)).is_open
    assert cal.session(date(2026, 10, 1)).regular_close == datetime(2026, 10, 1, 15, 30, tzinfo=KST)
    assert not cal.session(date(2026, 10, 3)).is_open          # 토요일
    assert not cal.session(VIRTUAL_HOLIDAY).is_open
    short = cal.session(VIRTUAL_SHORT_DAY)
    assert short.regular_open.astimezone(KST).time() == time(10, 0)
    assert cal.session(date(2027, 1, 4)) is None               # 달력 파일 없는 연도 = 모름


@pytest.mark.parametrize("bad", [
    {**CAL_RAW, "market": "NYSE"}, {**CAL_RAW, "source": ""}, {**CAL_RAW, "holidays": ["2026-10-09"]},
    {**CAL_RAW, "holidays": [date(2025, 1, 1)]}, {**CAL_RAW, "special_sessions": [{"date": date(2026, 1, 2),
                                                                                 "open": "13:00", "close": "10:00"}]},
    {**CAL_RAW, "extra": 1},
])
def test_calendar_rejects_bad_files(bad: dict) -> None:
    with pytest.raises(MarketDataError):
        parse_calendar(bad, 2026)


def test_calendar_dir_ignores_examples() -> None:
    cal = KrxCalendar.load_dir(ROOT / "config" / "calendar")
    assert cal.session(date(2026, 10, 1)) is None  # 저장소에는 실제 달력이 없다 (X-U3: 사용자가 작성)


def test_v75_live_market_status_conflict_blocks_day(env: Env) -> None:
    s = store(env)
    wire(env, s)
    assert s.calendar.observe_live(T0.astimezone(KST).date(), market_open=False) is False  # 달력은 개장, 0s는 휴장
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY)))
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G12


# --- V-11 거래 시간: 실제 달력 연결 ----------------------------------------------------------------

def test_v11_gate_uses_calendar_holiday_and_short_session(env: Env) -> None:
    s = store(env)
    wire(env, s)
    env.clock._now = datetime(2026, 10, 2, 10, 0, tzinfo=KST).astimezone(UTC)  # 가상 휴장일
    s.update_quote(Quote("TST-A", Decimal("10000"), env.clock.now(), upper_limit=Decimal("10000")))
    sig = make_signal(RequestType.ENTRY)
    sig = sig.__class__(**{**sig.__dict__, "as_of": env.clock.now(), "valid_until": env.clock.now() + timedelta(minutes=5)})
    assert env.gate.decide(GateRequest.from_signal(sig)).failed_check is Check.G12
    env.clock._now = datetime(2026, 10, 5, 13, 30, tzinfo=KST).astimezone(UTC)  # 단축일 마감(13:00) 이후
    op = GateRequest(source=RequestSource.OPERATOR, request_type=RequestType.EXIT, account=ACCOUNT, instrument="TST-A",
                     side=Side.SELL, quantity=Decimal("1"), order_type=OrderType.MARKET, price=Decimal("10000"),
                     idem_key="op")
    assert env.gate.decide(op).failed_check in (Check.G9, Check.G12)
    assert (Check.G12, False) in env.gate.decide(GateRequest(**{**op.__dict__, "idem_key": "op2"})).checks


# --- 호가 단위 (03 D-056) ---------------------------------------------------------------------------

def test_ticks_valid_and_align() -> None:
    t = parse_ticks(TICKS_RAW)
    assert t.valid(InstrumentKind.STOCK, Decimal("4995")) is True
    assert t.valid(InstrumentKind.STOCK, Decimal("4997")) is False
    assert t.valid(InstrumentKind.STOCK, Decimal("10005")) is False
    assert t.valid(InstrumentKind.ETN, Decimal("100")) is None          # 표 없는 상품 유형 = 모름
    assert t.align(InstrumentKind.STOCK, Decimal("10007"), is_buy=True) == Decimal("10000")
    assert t.align(InstrumentKind.STOCK, Decimal("10001"), is_buy=False) == Decimal("10010")


@pytest.mark.parametrize("bad", [
    {**TICKS_RAW, "source": ""},
    {**TICKS_RAW, "bands": {"STOCK": [{"below": "none", "tick": "5"}, {"below": "100", "tick": "1"}]}},
    {**TICKS_RAW, "bands": {"STOCK": [{"below": "100", "tick": "1"}]}},
    {**TICKS_RAW, "bands": {"STOCK": [{"below": "none", "tick": 0.5}]}},
    {**TICKS_RAW, "bands": {"STOCK": [{"below": "200", "tick": "1"}, {"below": "100", "tick": "1"},
                                      {"below": "none", "tick": "5"}]}},
    {**TICKS_RAW, "bands": {}},
])
def test_ticks_reject_bad_tables(bad: dict) -> None:
    with pytest.raises(MarketDataError):
        parse_ticks(bad)


def test_no_tick_file_means_limit_orders_rejected(env: Env) -> None:
    assert load_ticks(ROOT / "config" / "ticks" / "krx.toml") is None  # X-U1 확인 전 실제 파일 없음
    s = store(env, ticks=False)
    wire(env, s)
    sig = make_signal(RequestType.ENTRY)
    req = GateRequest.from_signal(sig.__class__(**{**sig.__dict__, "order_type": OrderType.LIMIT}))
    d = env.gate.decide(req)
    assert d.failed_check is Check.G10 and "호가 단위 규칙 없음" in d.reason


# --- V-76 품질 검사 ------------------------------------------------------------------------------

DAYS = [date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1)]


@pytest.mark.parametrize(("days", "closes", "ca", "needle"), [
    (DAYS[:2] + DAYS[3:], ["100", "101", "102"], set(), "결측"),
    (DAYS + DAYS[3:], ["100", "101", "102", "103", "103"], set(), "중복"),
    (DAYS, ["100", "101", "140", "141"], set(), "이상치"),
    (DAYS, ["100", "0", "100", "100"], set(), "0 이하"),
    (DAYS[:2] + [date(2026, 10, 3)], ["100", "100", "100"], set(), "휴장일 봉"),
])
def test_v76_quality_failures(days: list[date], closes: list[str], ca: set[date], needle: str) -> None:
    p = preprocess_daily(raw_days(days, closes, ca), trading_days)
    assert not p.report.ok and any(needle in i for i in p.report.issues), p.report.issues


def test_v76_quality_ok_and_corporate_action_allowed() -> None:
    assert preprocess_daily(raw_days(DAYS, ["100", "101", "102", "103"]), trading_days).report.ok
    p = preprocess_daily(raw_days(DAYS, ["100", "101", "50", "51"], ca={DAYS[2]}), trading_days)
    assert p.report.ok, p.report.issues


def test_v76_ohlc_contradiction() -> None:
    bad = [RawDaily(DAYS[0], Decimal("100"), Decimal("90"), Decimal("80"), Decimal("95"), Decimal("1"))]
    assert not preprocess_daily(bad).report.ok


def test_quality_failure_raises_k10_for_instrument_and_recovers(env: Env) -> None:
    s = store(env)
    wire(env, s)
    s.update_daily("TST-A", raw_days(DAYS, ["100", "101", "140", "141"]))
    assert "K10" in env.repo.active_halts(ACCOUNT)
    assert env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY))).failed_check is Check.G2
    assert env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, instrument="TST-B",
                                                               evaluation="b"))).result is GateResult.APPROVED
    s.update_daily("TST-A", raw_days(DAYS, ["100", "101", "102", "103"]))
    assert "K10" not in env.repo.active_halts(ACCOUNT)


# --- V-112 전처리 동일성, V-90 룩어헤드 -------------------------------------------------------------

def test_v112_backtest_and_live_paths_share_preprocessing() -> None:
    raw = raw_days(DAYS, ["100", "101", "102", "103"])

    def backtest_loader(rows: list[RawDaily]):  # type: ignore[no-untyped-def]
        return preprocess_daily(rows, trading_days, data_version="csv-v1")

    def live_loader(rows: list[RawDaily]):  # type: ignore[no-untyped-def]
        return preprocess_daily(list(reversed(rows)), trading_days, data_version="csv-v1")  # 받는 순서가 달라도

    a, b = backtest_loader(raw), live_loader(raw)
    assert a == b and a.version == PREPROCESS_VERSION


def test_v90_snapshot_excludes_bars_not_yet_closed() -> None:
    s = store()
    s.update_daily("TST-A", raw_days(DAYS, ["100", "101", "102", "103"]))
    no_pos = lambda i: None  # noqa: E731
    morning = datetime(2026, 10, 1, 9, 5, tzinfo=KST)
    snap = s.snapshot("ev", "M1", ACCOUNT, morning, Decimal("1"), ["TST-A"], no_pos, lambda i: False)
    assert [b.at for b in snap.view("TST-A").daily_bars] == DAYS[:3]
    after_close = datetime(2026, 10, 1, 15, 30, tzinfo=KST)
    snap2 = s.snapshot("ev", "M1", ACCOUNT, after_close, Decimal("1"), ["TST-A"], no_pos, lambda i: False)
    assert [b.at for b in snap2.view("TST-A").daily_bars] == DAYS
    # 미래 데이터를 바꿔도 아침 스냅샷은 같다 (V-91 성격)
    s.update_daily("TST-A", raw_days(DAYS, ["100", "101", "102", "999"]))
    snap3 = s.snapshot("ev", "M1", ACCOUNT, morning, Decimal("1"), ["TST-A"], no_pos, lambda i: False)
    assert snap3.view("TST-A").daily_bars == snap.view("TST-A").daily_bars


def test_snapshot_marks_missing_data_not_ok() -> None:
    s = store()
    snap = s.snapshot("ev", "M1", ACCOUNT, T0, Decimal("1"), ["TST-B", "NOPE"], lambda i: None, lambda i: False)
    assert not snap.view("TST-B").data_ok and not snap.view("NOPE").data_ok


# --- V-74 거래 상태 갱신 --------------------------------------------------------------------------

def test_v74_trading_status_update_blocks_and_alerts_when_held(env: Env) -> None:
    s = store(env)
    wire(env, s)
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, quantity="10")))
    env.executor.submit(d.approved)
    env.executor.process_events()
    s.update_status("TST-A", frozenset({TradingStatus.VI}), held=True)
    assert env.gate.decide(GateRequest.from_signal(make_signal(RequestType.EXIT, quantity="10",
                                                               evaluation="x"))).failed_check is Check.G13
    alerts = [json.loads(e["summary"]) for e in env.repo.audit_events() if e["kind"] == "ALERT"]
    assert any("VI" in a["message"] for a in alerts)
    s.update_status("TST-A", frozenset({TradingStatus.HALTED}), held=True)
    alerts = [json.loads(e["summary"]) for e in env.repo.audit_events() if e["kind"] == "ALERT"]
    assert any(a["level"] == "EMERGENCY" and "HALTED" in a["message"] for a in alerts)


# --- 설정 (03 D-095) ------------------------------------------------------------------------------

def test_market_settings_file() -> None:
    ok = {"gate": {"quote_max_age_seconds": 60, "price_limit_ratio": "0.30"},
          "costs": {"commission_rate": "0.00015", "sell_tax_rate": "0.0018"}}
    st = parse_market_settings(ok)
    pol = GatePolicy.from_settings(st)
    assert pol.quote_max_age == timedelta(seconds=60) and pol.price_limit_ratio == Decimal("0.30")
    for bad in ({**ok, "gate": {"price_limit_ratio": "0.30"}},
                {**ok, "gate": {"quote_max_age_seconds": 60, "price_limit_ratio": 0.3}},
                {**ok, "costs": {"commission_rate": "0.00015"}},
                {**ok, "x": {}},
                {**ok, "gate": {"quote_max_age_seconds": True, "price_limit_ratio": "0.30"}}):
        with pytest.raises(ConfigError):
            parse_market_settings(bad)
    with pytest.raises(ConfigError):
        load_market_settings(ROOT / "config" / "market.example.toml")  # 자리표시자로는 시작 불가
    assert HaltType.K10  # 표시용


def test_snapshot_marks_quality_failed_instrument_not_ok() -> None:
    s = store()
    s.update_daily("TST-A", raw_days(DAYS, ["100", "101", "140", "141"]))
    s.update_daily("TST-B", raw_days(DAYS, ["100", "101", "102", "103"]))
    snap = s.snapshot("ev", "M1", ACCOUNT, datetime(2026, 10, 2, 9, 0, tzinfo=KST), Decimal("1"), ["TST-A", "TST-B"],
                      lambda i: None, lambda i: False)
    assert not snap.view("TST-A").data_ok and snap.view("TST-B").data_ok
