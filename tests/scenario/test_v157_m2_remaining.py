"""V-157 잔여 4개(V-02·V-76·V-112·V-127)의 M2판 시험 (04 2-M2.4 V-157, CP-036 §8 "V-157 미실행 4개", 05 T-M2-09 잔여).

조립은 실운영과 같은 build_engine(market="M2")만 쓴다.
- V-02·V-76·V-112: tests/support/m2_engine_world.M2World(FakeSpotBroker·가상 심볼 TSTA-USDT·가상 계좌 BN-BT-01)에
  실제 시세 공급 BinanceFeed(1d klines → BinanceMarketFacts 품질 검사 → SpotFactsStore)를 붙인다. 거래소는 합성
  (tests/support/binance_exchange_sim, 주문 스위치 꺼짐 RunGuard — 주문 계열 전송 0).
- V-112 백테스트 쪽: 같은 원시 행을 A-110 CSV로 써서 app/backtest_m2_cli.load_binance_daily_csv → run_backtest_m2.
- V-127: tests/scenario/test_t047_m2_adapter.Run(실제 BinanceSpotAdapter + 합성 거래소 + build_engine M2) + 실제 UserStream +
  메모리 가짜 WS API(tests/support/binance_ws_fake) + M2Relay(실운영 run --market M2와 같은 중계).
네트워크 없음. 모든 값은 가상이다. 매 시험 끝에 불변 조건을 검사한다(04 D-069).
xfail(strict=True)은 제품 결함 또는 설계 기준 미정으로 기대 결과를 충족하지 못하는 항목이다 — 통과로 세지 않는다(D-068).
"""

from __future__ import annotations

import csv
import json
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_t047_m2_adapter import ACCOUNT as RUN_ACCOUNT
from tests.scenario.test_t047_m2_adapter import KEY, SECRET, Run, _ms
from tests.scenario.test_tm2_09_v157 import V157
from tests.support.binance_exchange_sim import ExchangeSim, RawReply, _reply
from tests.support.binance_ws_fake import FakeWsApi, wait_until
from tests.support.invariants import check_invariants
from tests.support.m2_engine_world import (
    ACCOUNT, BASELINE, DAY, SYM, FixedM2Strategy, M2World, fixed_spec, utc,
)
from tests.support.m2_fakes import m2_limits, symbol_rules
from trading_system.app.backtest_m2 import SpotBacktestConfig, run_backtest_m2
from trading_system.app.backtest_m2_cli import load_binance_daily_csv
from trading_system.app.m2_relay import M2Relay
from trading_system.app.wiring import StrategyInput
from trading_system.broker.binance.client import TESTNET_REST, BinanceSpotClient
from trading_system.broker.binance.feed import BinanceFeed
from trading_system.broker.binance.run_guard import RunGuard
from trading_system.broker.binance.signing import Credentials
from trading_system.broker.binance.user_stream import UserStream
from trading_system.broker.binance.weights import WeightBudget, parse_rate_limits
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.config.capabilities import load_capabilities
from trading_system.config.strategy_registry import validate_spec
from trading_system.domain.enums import Mode, OrderType, RequestType
from trading_system.domain.market import CostRates
from trading_system.domain.models import Signal
from trading_system.domain.strategy import Bar, EvaluationSnapshot
from trading_system.strategy.base import ParamValues, make_signal

D = Decimal
ROOT = Path(__file__).resolve().parents[2]
DAY_MS = 86_400_000
EVAL = time(1, 0)
QTY = "0.0100"
LSYM = "TSTAUSDT"     # 시세 공급(바이낸스 클라이언트)은 거래소 심볼 형식만 받는다 — 가상 심볼, 형식만 바이낸스와 같음
LRULES = replace(symbol_rules(SYM), instrument=LSYM)   # 가상 규칙(TSTA-USDT와 같은 값)


# --- 공통: 원시 1d 봉(같은 원시 데이터를 두 경로 형식으로) -------------------------------------------------------

Raw = tuple[date, str, str, str, str, str]     # (봉 시작 UTC 날짜, open, high, low, close, volume) — 원시 문자열 그대로


def raw_days(first: date, last: date, *, high_close: date | None = None, skip: frozenset[date] = frozenset(),
             dup: date | None = None, bad_ohlc: date | None = None) -> list[Raw]:
    """가상 1d 원시 봉 first~last(last = 판정 날의 진행 중 봉). 종가 10000.00, high_close 날만 10100.00(결정 기준 10050 초과).
    skip 날짜는 빠짐(결측), dup은 두 번, bad_ohlc는 고가 < 시가."""
    out: list[Raw] = []
    d = first
    while d <= last:
        c = "10100.00" if d == high_close else "10000.00"
        if d not in skip:
            hi = "9000.00" if d == bad_ohlc else "10200.00"
            out.append((d, "10000.00", hi, "9900.00", c, "1000.0"))
            if d == dup:
                out.append(out[-1])
        d += timedelta(days=1)
    return out


def _open_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


def kline_rows(raw: list[Raw]) -> list[list[object]]:
    """원시 → 바이낸스 GET /api/v3/klines 행 형식(BN §8)."""
    return [[_open_ms(d), o, h, lo, c, v, _open_ms(d) + DAY_MS - 1, "1000", 10, "5.0", "500", "0"]
            for d, o, h, lo, c, v in raw]


def write_csv(raw: list[Raw], path: Path) -> Path:
    """원시 → A-110 수집 형식 CSV(open_time_utc,…,close_time_utc, ISO UTC)."""
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["open_time_utc", "open", "high", "low", "close", "volume", "close_time_utc"])
        for d, o, h, lo, c, v in raw:
            opened = datetime(d.year, d.month, d.day, tzinfo=UTC)
            w.writerow([opened.isoformat(), o, h, lo, c, v,
                        (opened + timedelta(days=1) - timedelta(milliseconds=1)).isoformat()])
    return path


class DailySim(ExchangeSim):
    """합성 거래소: 1d klines만 주어진 원시 행(지금 UTC 날짜까지, 진행 중 봉 포함 — 거래소와 같음)으로 돌려준다."""

    def __init__(self, now_ms: Callable[[], int], raw: list[Raw]) -> None:
        super().__init__(now_ms, symbols=(LSYM,), price=D("10000.00"))
        self.raw = raw

    def _get_klines(self, p: dict) -> RawReply:
        if p.get("interval") != "1d":
            return super()._get_klines(p)
        today = datetime.fromtimestamp(self.now_ms() / 1000, UTC).date()
        rows = kline_rows([r for r in self.raw if r[0] <= today])
        return _reply(200, rows[-int(p.get("limit", "500")):])


def feed_step(world: M2World, raw: list[Raw]) -> tuple[RunGuard, list[tuple[str, str]]]:
    """실운영 시세 공급(BinanceFeed)의 첫 단계 한 번: 1d klines → 품질 검사(실패 = K10, 실제 정지 관리자) → SpotFactsStore.
    run 시작 직후처럼 공급 저장소의 1d 봉은 비어 있는 상태에서 시작한다."""
    world.facts.set_daily_bars(world.sym, [])
    sim = DailySim(lambda: _ms(world.clock), raw)
    budget = WeightBudget(parse_rate_limits([{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE",
                                              "intervalNum": 1, "limit": 1200}]), lambda: _ms(world.clock))
    guard = RunGuard(sim, orders_enabled=False)
    alerts: list[tuple[str, str]] = []
    client = BinanceSpotClient(TESTNET_REST, Credentials(KEY, SECRET), guard, budget, now_ms=lambda: _ms(world.clock),
                               sleep=lambda s: None, on_alert=lambda k, m: alerts.append((k, m)))
    feed = BinanceFeed(client, world.facts, [world.sym], now=world.clock.now, alert=lambda k, m: alerts.append((k, m)),
                       halts=world.engine.halts, account=ACCOUNT)   # type: ignore[arg-type]
    feed.step()
    assert guard.order_sent == 0
    return guard, alerts


class Recorder(FixedM2Strategy):
    """판정 입력(확정 1d 봉·data_ok)을 판정 시각별로 기록한다. 결정: 보유 없음 AND data_ok AND 마지막 확정 종가 > 10050."""

    THRESHOLD = D("10050")

    def __init__(self, *, emit: bool = True, need: int = 1) -> None:
        super().__init__(fixed_spec(universe=(LSYM,)))
        self.emit, self.need = emit, need      # need = 판정에 필요한 연속 확정 봉 수(실제 전략의 창 길이 확인과 같음)
        self.seen: dict[datetime, tuple[tuple[Bar, ...], bool, bool]] = {}

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
        out: list[Signal] = []
        for v in snapshot.instruments:
            decide = bool(v.data_ok and len(v.daily_bars) >= self.need and v.daily_bars[-1].close > self.THRESHOLD
                          and v.position is None)
            self.seen[snapshot.as_of] = (tuple(v.daily_bars), v.data_ok, decide)
            if decide and self.emit and v.last_price is not None:
                out.append(make_signal(self.spec, snapshot, v.instrument, "ENTRY-1", RequestType.ENTRY, D(QTY),
                                       OrderType.MARKET, v.last_price, timedelta(minutes=30)))
        return out


def w_strategy_seen(w: M2World) -> list[tuple[tuple[Bar, ...], bool, bool]]:
    return list(w.strategy_seen.values())   # type: ignore[attr-defined]


def live_world(tmp: Path, rec: Recorder, day: date, **kw: object) -> M2World:
    (tmp / "live").mkdir()
    w = M2World(tmp / "live", eval_times=(EVAL,), day=day, sym=LSYM, base_asset="TSTA", symbol_rule=LRULES,
                strategies=[StrategyInput(rec, {"qty": QTY}, (EVAL,))], **kw)  # type: ignore[arg-type]
    w.set_baseline()
    w.strategy_seen = rec.seen   # type: ignore[attr-defined]
    return w


def emergencies(repo) -> list[str]:  # type: ignore[no-untyped-def]
    out = []
    for e in repo.audit_events():
        if e["kind"] == "ALERT":
            s = json.loads(e["summary"])
            if s["level"] == "EMERGENCY":
                out.append(s["message"])
    return out


# === V-02 레버리지 배수 노출 (01 L9) — M2 해당 없음 근거 + 배수 1 경로 ==========================================

def test_v02_not_in_v157_rerun_list_and_m2_has_no_leverage_capability() -> None:
    """V-157 재실행 목록(04 2-M2.4)에 V-02가 없다 — "V-01, V-03~V-13". M2 열의 CAP-INS-04(레버리지·인버스)는 "해당 없음"이고
    M2 상품은 롱 전용 현물(CAP-INS-05)뿐이다. CAP-INS-04를 선언한 M2 전략은 어떤 모드로도 등록되지 않는다."""
    text = (ROOT / "docs" / "04-validation.md").read_text(encoding="utf-8")
    [row] = [ln for ln in text.splitlines() if ln.startswith("| V-157 |")]
    assert "V-01, V-03~V-13" in row and not re.search(r"V-02\b", row)
    assert "V-02" not in V157
    caps = load_capabilities(ROOT / "docs" / "capabilities.md")
    spec = fixed_spec(capabilities=("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-ORD-02", "CAP-INS-05", "CAP-INS-04"))
    for mode in (Mode.BACKTEST, Mode.PAPER):
        problems = validate_spec(spec, caps, m2_limits(), mode)
        assert any(p.startswith("CAP-INS-04: M2에서 해당 없음") for p in problems), problems
    assert m2_limits().l10_leverage_total_ratio is None


def test_v02_m2_engine_exposure_is_order_value_times_one(tmp_path: Path) -> None:
    """실제 M2 조립: 공급자 상품 배수 = 1, 신규 진입 예약 노출 = 주문 수량 × 주문 가격(배수 곱 없음)."""
    w = M2World(tmp_path, behavior=FakeBehavior.ACCEPT_ONLY)
    w.set_baseline()
    try:
        info = w.facts.instrument(SYM)
        assert info is not None and info.leverage == D("1")
        w.start_and_resume()
        w.tick_at(1, 0)
        [sent] = w.sent()
        [rsv] = w.repo.active_reservations(ACCOUNT)
        i = w.repo.intent(rsv.intent_id)
        assert rsv.amount == i.quantity * i.price == sent.quantity * sent.price
    finally:
        w.close()


# === V-76 데이터 품질 실패 → 신호 무효 (R-22) — 실제 공급 경로 + M2 엔진 ===========================================

def _v76_run(tmp: Path, raw: list[Raw], need: int = 1) -> M2World:
    rec = Recorder(need=need)
    w = live_world(tmp, rec, DAY)
    w.at(0, 30)
    feed_step(w, raw)
    w.start_and_resume()
    w.tick_at(EVAL.hour, EVAL.minute)
    return w


def test_v76_m2_control_clean_klines_signal_reaches_broker(tmp_path: Path) -> None:
    w = _v76_run(tmp_path, raw_days(DAY - timedelta(days=30), DAY, high_close=DAY - timedelta(days=1)))
    try:
        assert "K10" not in w.repo.active_halts(ACCOUNT) and len(w.sent()) == 1
    finally:
        w.close()


@pytest.mark.parametrize("case", ["duplicate", "ohlc"])
def test_v76_m2_duplicate_or_ohlc_kline_k10_and_no_order(tmp_path: Path, case: str) -> None:
    first, prev = DAY - timedelta(days=30), DAY - timedelta(days=5)
    hc = DAY - timedelta(days=1)
    raw = (raw_days(first, DAY, high_close=hc, dup=prev) if case == "duplicate"
           else raw_days(first, DAY, high_close=hc, bad_ohlc=prev))
    w = _v76_run(tmp_path, raw)
    try:
        assert "K10" in w.repo.active_halts(ACCOUNT)
        assert w.facts.daily_bars(LSYM) == ()                          # 품질 실패 봉은 판정 입력에 들어가지 않음
        assert w.sent() == []                                          # 신호 무효 = 주문 0
    finally:
        w.close()


def test_v76_m2_missing_latest_day_no_signal(tmp_path: Path) -> None:
    """어제(DAY−1) 봉이 없으면 판정 입력 data_ok False — 신호 없음, 주문 0 (결측 종목은 그 판정에서 신호 없음, 04 §2.1)."""
    raw = raw_days(DAY - timedelta(days=30), DAY, high_close=DAY - timedelta(days=2),
                   skip=frozenset({DAY - timedelta(days=1)}))                    # 그제 종가는 기준 초과, 어제 봉 없음
    w = _v76_run(tmp_path, raw)
    try:
        assert w.sent() == [] and w.intents() == []
    finally:
        w.close()


def test_v76_m2_gap_inside_window_invalidates_signal(tmp_path: Path) -> None:
    """확정 봉 사이 빈 날(DAY−3) → 진입용 판정 입력은 빈 날 뒤 연속 구간(DAY−2·DAY−1)만 → 창 5봉이 필요한 전략은 진입
    판정하지 않음 → 주문 0. 빈 날을 가로지르는 창으로 진입하지 않는다.
    [T-068 개정 — DP-82 확정 A-126 ②] 일봉이 불완전하면 판정 실행기가 전략을 한 번 더(청산용, 빈 날을 건너뛴 확정 봉 전체)
    부른다. 그 판정에서 나온 진입 신호는 실행기가 버린다(감액·청산만 씀). 그래서 전략 기록은 두 번이고, 두 번째 입력은
    DAY−3이 빠진 확정 봉 전체(진입 결정이 나와도 주문 0). 종전 기대값(기록 1회·연속 구간 2봉)은 DP-82 (가) 결정으로 바뀐 동작."""
    raw = raw_days(DAY - timedelta(days=30), DAY, high_close=DAY - timedelta(days=1),
                   skip=frozenset({DAY - timedelta(days=3)}))
    w = _v76_run(tmp_path, raw, need=5)
    try:
        assert w.sent() == [] and w.intents() == []
        [(bars, ok, decide)] = w_strategy_seen(w)       # 같은 판정 시각 키 — 마지막(청산용) 입력이 남는다
        assert DAY - timedelta(days=3) not in [b.at for b in bars] and len(bars) == 29 and ok and decide
        msgs = [json.loads(e["summary"])["message"] for e in w.audits("ALERT")]
        assert any("일봉 판정 입력 불완전" in m and "보유 없음" in m for m in msgs)
    finally:
        w.close()


def test_v76_m2_gap_outside_needed_window_does_not_block(tmp_path: Path) -> None:
    """대조군: 같은 빈 날이어도 전략이 어제 봉 1개만 쓰면(m2-verify 방식) 판정·주문은 정상 — 빈 날 뒤 연속 구간 안에서 판정."""
    raw = raw_days(DAY - timedelta(days=30), DAY, high_close=DAY - timedelta(days=1),
                   skip=frozenset({DAY - timedelta(days=3)}))
    w = _v76_run(tmp_path, raw, need=1)
    try:
        assert len(w.sent()) == 1
    finally:
        w.close()


# === V-112 전처리 동일성 (R-22, 03 D-088) — 같은 원시 → 백테스트·실시간 같은 전처리·신호 ==============================

def _backtest(tmp: Path, raw: list[Raw], start: date, end: date, rec: Recorder) -> object:
    data = load_binance_daily_csv(write_csv(raw, tmp / "klines.csv"), incomplete=[])
    cfg = SpotBacktestConfig(
        run_id="v112", strategy=rec, params={"qty": QTY}, account_limits=m2_limits(), rules={LSYM: LRULES},
        pairs={LSYM: ("TSTA", "USDT")}, cost_rates=CostRates(D("0.001"), D("0"), D("0.00000001")), data={LSYM: data},
        start=start, end=end, initial_cash=BASELINE, out_dir=tmp / "bt", slippage=D("0.001"), eval_times=(EVAL,),
        final_from=date(2100, 1, 1))
    return run_backtest_m2(cfg)


def _live(tmp: Path, raw: list[Raw], days: list[date], rec: Recorder) -> M2World:
    w = live_world(tmp, rec, days[0])
    w.at(0, 30)
    feed_step(w, raw)
    w.start_and_resume()
    for d in days:
        w.at(0, 30, day=d)
        feed_step(w, raw)                                              # UTC 날짜가 바뀐 뒤 첫 단계 = 1d klines 재적재
        w.tick_at(EVAL.hour, EVAL.minute, day=d)
    return w


def test_v112_m2_same_raw_klines_same_bars_and_same_signal(tmp_path: Path) -> None:
    raw = raw_days(DAY - timedelta(days=40), DAY, high_close=DAY - timedelta(days=1))
    bt_rec, live_rec = Recorder(), Recorder()
    result = _backtest(tmp_path, raw, DAY, DAY, bt_rec)
    w = _live(tmp_path, raw, [DAY], live_rec)
    try:
        at = utc(DAY, EVAL.hour, EVAL.minute)
        assert bt_rec.seen[at] == live_rec.seen[at]                     # 같은 확정 봉(값·날짜)·data_ok·결정
        bars, ok, decide = live_rec.seen[at]
        assert ok and decide and bars[-1].at == DAY - timedelta(days=1) and len(bars) == 40
        assert [f[3] for f in result.fills] == ["BUY"]                  # type: ignore[attr-defined]
        [sent] = w.sent()
        assert sent.side == "BUY" and sent.quantity == D(QTY)
    finally:
        w.close()


@pytest.mark.parametrize("gap_back", [1, 3])
def test_v112_m2_same_raw_with_missing_day_same_inputs_every_evaluation(tmp_path: Path, gap_back: int) -> None:
    gap = DAY - timedelta(days=gap_back)
    raw = raw_days(DAY - timedelta(days=40), DAY, high_close=gap - timedelta(days=1), skip=frozenset({gap}))
    # 결측 전날 종가가 기준 초과 → 어느 판정에서 신호(결정)가 나는지가 두 경로에서 같아야 한다
    days = [gap - timedelta(days=1) + timedelta(days=k) for k in range(gap_back + 2)]
    bt_rec, live_rec = Recorder(emit=False), Recorder(emit=False)
    _backtest(tmp_path, raw, days[0], days[-1], bt_rec)
    w = _live(tmp_path, raw, days, live_rec)
    try:
        assert set(bt_rec.seen) == set(live_rec.seen)
        for at in sorted(bt_rec.seen):
            assert bt_rec.seen[at][0] == live_rec.seen[at][0], at          # 같은 전처리(domain.daily_input) → 같은 확정 봉
        diff = {at.date() for at in bt_rec.seen if bt_rec.seen[at] != live_rec.seen[at]}
        # 남는 차이는 결측 당일 하나뿐: 백테스트는 그날 봉이 없어 가격도 없음(data_ok False) — 실시간은 그날 시세가 있다.
        # 결측은 다음 날에야 알 수 있어 피할 수 없는 차이(전처리 차이 아님). 다음 날부터는 두 경로 모두 무효(어제 봉 없음).
        assert diff <= {gap}, diff
        g = utc(gap, EVAL.hour, EVAL.minute)
        assert bt_rec.seen[g][1] is False
    finally:
        w.close()


# === V-127 WS 재접속 한도 초과 뒤 복구 (03 D-050·D-114, 01 §2.7) — 실제 UserStream + M2Relay + M2 엔진 ===============

@pytest.fixture
def started_run(tmp_path: Path) -> Iterator[Run]:
    r = Run(tmp_path)
    r.engine.account_state.set_baseline(RUN_ACCOUNT, D("10000"), r.clock.now())  # type: ignore[attr-defined]
    r.engine.start()
    with r.repo.tx():
        r.repo.insert_command("c1", "LOCAL", "tester", "RESUME", {"account": None, "halt_type": "K8"}, r.clock.now())
    r.at(0, 50)
    assert r.repo.active_halts(RUN_ACCOUNT) == []
    yield r
    assert check_invariants(r.repo.connection, Mode.BACKTEST.value) == []
    r.repo.close()


def test_v127_m2_reconnect_refused_past_limit_k5_one_emergency_thread_kept_then_recovers(started_run: Run) -> None:
    r = started_run
    relay = M2Relay(RUN_ACCOUNT)
    relay.attach(r.engine)
    api = FakeWsApi(r.sim)
    states: list[str] = []
    waits: list[float] = []
    release = threading.Event()
    held = 15                                   # M1 한도(10회, D-050)를 넘는 연속 실패 수

    def wait(stop: threading.Event, s: float) -> None:
        waits.append(s)
        if len(waits) >= held:
            release.wait(10)                    # "서버 복구" 전까지 재접속 스레드를 여기 붙잡아 둔다

    def on_state(st, m: str) -> None:  # type: ignore[no-untyped-def]
        states.append(st.value)
        relay.stream_state(st.value, m)

    stream = UserStream(api.connect, r.session.credentials, r.session.adapter, now_ms=lambda: _ms(r.clock),
                        on_state=on_state, recv_timeout_s=0.01, wait=wait)
    stop = threading.Event()
    t = threading.Thread(target=stream.run, args=(stop,), daemon=True)
    t.start()
    try:
        assert wait_until(lambda: stream.subscriptions == 1)
        r.at(0, 55)
        before = len(emergencies(r.repo))
        api.fail_connects = 10 ** 6             # 서버가 재접속을 계속 거부
        api.current.drop()
        assert wait_until(lambda: len(waits) == held)
        r.at(0, 56)                             # 중계 큐 처리 (판정·실행 스레드)
        assert t.is_alive()                                                # 한도를 넘어도 스레드 유지
        assert states.count("OFFLINE") == held and "REFUSED" not in states
        assert waits[:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]        # 같은 간격(30초)으로 계속 시도
        assert "K5" in r.repo.active_halts(RUN_ACCOUNT)
        new = emergencies(r.repo)[before:]
        assert len(new) == 1 and "K5" in new[0], new                       # 치명(긴급) 알림 1회
        r.at(1, 0)                                                         # 판정 시각: 진입 신호 → K5로 전송 0
        assert r.sim.order_posts() == []
        api.fail_connects = 0                   # 서버 복구
        release.set()
        assert wait_until(lambda: stream.subscriptions == 2)
        assert states[-1] == "RECONNECTED"
        r.at(1, 5)
        r.at(1, 6)
        triggers = [x["trigger"] for x in r.repo.select("SELECT trigger FROM reconciliation ORDER BY created_seq")]
        assert "STREAM_RECONNECT" in triggers                              # 복구 시 대조
        assert "K5" not in r.repo.active_halts(RUN_ACCOUNT)                # 재시작 없이 K5 해제
        assert t.is_alive() and len(emergencies(r.repo)[before:]) == 1
        r.at(4, 0)                                                         # 같은 엔진·같은 스레드로 매매 재개
        assert len(r.sim.order_posts()) == 1
    finally:
        release.set()
        stop.set()
        t.join(5)
