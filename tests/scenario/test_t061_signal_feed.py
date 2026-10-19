"""CP-039 T-061 (A-115, A-127) — 신호 출처 공급 스레드·판정 입력 구성·저장·DB 메타 (D-248·D-254).

V-318 DB 메타 신호 출처, V-319 판정 입력 구성, V-322 신호 출처 장애, V-326 기존 동작 불변(testnet 기본).
**네트워크 없음** — 합성 공개 시세 응답(BN §8 klines 필드 정의)·가짜 시계·임시 SQLite. 실서버 연결 관찰(T-064)은 별도 승인.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from tests.support.m2_engine_world import SYM, M2World, fixed_spec
from trading_system.app.m2_minutes import M2MinuteInput
from trading_system.app.wiring import SignalInputs, StrategyInput
from trading_system.domain.enums import Mode
from trading_system.domain.market import Quote
from trading_system.domain.market_time import Market
from trading_system.domain.signal_source import KIND_PUBLIC, KIND_TESTNET, PUBLIC_BAR_SOURCE
from trading_system.domain.strategy import EvaluationSnapshot
from trading_system.marketdata.binance_public import (
    INTERVAL_MS, KLINES_PATH, PUBLIC_HOSTS, TICKER_PATH, BinancePublicClient, PublicBudget, PublicDataGuard,
    PublicMinutePlan, PublicTransportError, SignalPlan, data_source_rule,
)
from trading_system.marketdata.signal_feed import BarsDroppedSink, SignalFacts, SignalFeed
from trading_system.marketdata.spot_facts import SpotFactsStore
from trading_system.marketdata.spot_minute import SpotMinuteBook
from trading_system.storage.market_meta import MarketMismatch, bind_market, bind_signal_source, read_signal_meta
from trading_system.storage.repository import OrderIdentity, Repository

D = Decimal
T0 = datetime(2026, 10, 10, 0, 30, 5, tzinfo=UTC)
PSYM = "BTCUSDT"


@dataclass
class Reply:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""


def kline_row(open_ms: int, step: int, o: str = "100", h: str = "101", lo: str = "99", c: str = "100.5") -> list:
    return [open_ms, o, h, lo, c, "10", open_ms + step - 1, "1000", 5, "4", "400", "0"]


class PublicSim:
    """실서버 공개 시세 흉내 — 경로별 합성 응답. fail[(경로, 간격)]에 넣은 응답을 먼저 돌려준다."""

    def __init__(self, now) -> None:  # type: ignore[no-untyped-def]
        self.now = now
        self.calls: list[tuple[str, str, dict]] = []
        self.fail: dict[tuple[str, str | None], list] = {}
        self.price = "100.00"
        self.bad_rows = False

    def send(self, method: str, url: str, headers):  # type: ignore[no-untyped-def]
        p = urlsplit(url)
        q = dict(parse_qsl(p.query))
        self.calls.append((p.hostname or "", p.path, q))
        key = (p.path, q.get("interval"))
        if self.fail.get(key):
            r = self.fail[key].pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        if p.path == TICKER_PATH:
            return Reply(200, {}, json.dumps({"symbol": q["symbol"], "price": self.price}))
        step = INTERVAL_MS[q["interval"]]
        now_ms = int(self.now().timestamp() * 1000)
        cur = now_ms // step * step                                  # 진행 중 봉 시작
        n = int(q["limit"])
        rows = [kline_row(cur - (n - 1 - i) * step, step) for i in range(n)]
        if self.bad_rows:
            rows[-2][2] = "1"                                          # high < low — OHLC 위반
        return Reply(200, {}, json.dumps(rows))

    def public_calls(self, path: str, interval: str | None = None) -> list:
        return [c for c in self.calls if c[1] == path and (interval is None or c[2].get("interval") == interval)]


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def now(self) -> datetime:
        return self.t


class Rig:
    def __init__(self, *, minute: PublicMinutePlan | None = None, symbols: tuple[str, ...] = (PSYM,)) -> None:
        self.clock = Clock(T0)
        self.sim = PublicSim(self.clock.now)
        self.notes: list[tuple[str, str]] = []
        self.guard = PublicDataGuard(self.sim, data_source_rule(Mode.PAPER, Market.M2, KIND_PUBLIC))
        self.client = BinancePublicClient(self.guard, PublicBudget(self.ms, cap_per_minute=1000), now_ms=self.ms,
                                          sleep=lambda s: None, on_alert=lambda k, m: self.notes.append((k, m)))
        self.facts = SpotFactsStore(now=self.clock.now, universe=symbols)
        self.ref = SignalFacts(symbols)
        self.book = SpotMinuteBook(symbols) if minute is not None else None
        self.feed = SignalFeed(self.client, symbols, now=self.clock.now, notify=lambda k, m: self.notes.append((k, m)),
                               daily_sink=self.facts, reference=self.ref, plan=SignalPlan(minute=minute),
                               minute_sink=self.book)

    def ms(self) -> int:
        return int(self.clock.t.timestamp() * 1000)

    def at(self, t: datetime) -> None:
        self.clock.t = t
        self.feed.step()


# --- V-319 공급: 봉 = 신호 출처(확정 봉만), 참조 시세 = 신호 출처 ----------------------------------------------------

def test_v319_first_step_closed_daily_bars_and_reference_quote() -> None:
    r = Rig()
    r.at(T0)
    bars = r.facts.daily_bars(PSYM)
    assert len(bars) == 199 and bars[-1].at == date(2026, 10, 9)          # 진행 중 10-10 봉 미사용
    q = r.ref.reference_quote(PSYM)
    assert q == Quote(PSYM, D("100.00"), T0) and r.ref.fill_reference(PSYM) == (D("100.00"), T0)
    assert {h for h, _p, _q in r.sim.calls} == set(PUBLIC_HOSTS) and r.guard.blocked == []
    assert r.facts.quote(PSYM) is None                                    # 실행 출처 최근가는 이 공급이 넣지 않는다


def test_reference_every_10s_daily_once_per_utc_day() -> None:
    r = Rig()
    r.at(T0)
    r.at(T0 + timedelta(seconds=5))
    r.at(T0 + timedelta(seconds=10))
    assert len(r.sim.public_calls(TICKER_PATH)) == 2 and len(r.sim.public_calls(KLINES_PATH, "1d")) == 1
    r.at(datetime(2026, 10, 11, 0, 0, 1, tzinfo=UTC))
    assert len(r.sim.public_calls(KLINES_PATH, "1d")) == 2
    assert r.facts.daily_bars(PSYM)[-1].at == date(2026, 10, 10)


def test_bars_dropped_sink_drops_only_daily_bars() -> None:
    facts = SpotFactsStore(now=lambda: T0, universe=(PSYM,))
    s = BarsDroppedSink(facts)
    s.set_daily_bars(PSYM, [object()])  # type: ignore[list-item]
    s.update_quote(Quote(PSYM, D("101"), T0))
    assert facts.daily_bars(PSYM) == () and facts.quote(PSYM) == Quote(PSYM, D("101"), T0)
    assert s.units == facts.units and s.dropped == 1


# --- V-322 장애: 입력 무효·정지 없음·대체 없음·묶음 경고 ---------------------------------------------------------------

def test_v322_daily_failure_keeps_input_invalid_retries_and_streak_warning() -> None:
    r = Rig()
    r.sim.fail[(KLINES_PATH, "1d")] = [Reply(500)] * 12
    r.at(T0)
    assert r.facts.daily_bars(PSYM) == ()                                 # 테스트넷 봉으로 대체하지 않음
    assert [k for k, _m in r.notes] == ["WARNING"]
    r.at(T0 + timedelta(seconds=30))                                      # 1분 전 — 다시 부르지 않음
    assert len(r.sim.public_calls(KLINES_PATH, "1d")) == 4
    r.at(T0 + timedelta(seconds=60))
    r.at(T0 + timedelta(seconds=120))
    assert [k for k, m in r.notes if "연속" in m] == ["WARNING"]
    r.at(T0 + timedelta(seconds=180))                                     # 회복
    assert len(r.facts.daily_bars(PSYM)) == 199 and r.notes[-1][0] == "INFO"
    assert all(k in ("WARNING", "INFO", "EMERGENCY") for k, _m in r.notes)   # 정지(K5·K8·K10) 경로 없음


def test_v322_daily_quality_violation_keeps_previous_bars() -> None:
    r = Rig()
    r.at(T0)
    before = r.facts.daily_bars(PSYM)
    r.sim.bad_rows = True
    r.at(datetime(2026, 10, 11, 0, 0, 1, tzinfo=UTC))
    assert r.facts.daily_bars(PSYM) == before and "실패" in r.notes[-1][1]


def test_v322_minute_failure_marks_symbol_input_invalid_then_recovers() -> None:
    r = Rig(minute=PublicMinutePlan(1, 10))
    assert r.book is not None
    r.sim.fail[(KLINES_PATH, "1m")] = [PublicTransportError("x")] * 4
    r.at(datetime(2026, 10, 10, 0, 31, 2, tzinfo=UTC))
    assert r.book.quality_problem(PSYM) is not None and r.book.drain() == []
    r.at(datetime(2026, 10, 10, 0, 32, 2, tzinfo=UTC))
    assert r.book.quality_problem(PSYM) is None
    bars = r.book.drain()
    assert bars and all(b.instrument == PSYM and b.start < datetime(2026, 10, 10, 0, 32, tzinfo=UTC) for b in bars)


def test_v322_429_stops_all_signal_calls_one_emergency_no_halt() -> None:
    r = Rig()
    r.at(T0)
    n = len(r.sim.calls)
    r.sim.fail[(TICKER_PATH, None)] = [Reply(429, {"Retry-After": "120"})]
    r.at(T0 + timedelta(seconds=10))
    r.at(T0 + timedelta(seconds=20))
    r.at(T0 + timedelta(seconds=60))
    assert len(r.sim.calls) == n + 1                                      # 중단 중 보내지 않음
    assert [k for k, _m in r.notes if k == "EMERGENCY"] == ["EMERGENCY"]
    q = r.ref.reference_quote(PSYM)
    assert q is not None and q.at == T0                                   # 늙어 간다 → 게이트 G11-S가 진입만 거부 (T-062)
    r.at(T0 + timedelta(seconds=135))
    assert r.ref.reference_quote(PSYM).at == T0 + timedelta(seconds=135)  # type: ignore[union-attr]


def test_v322_thread_exception_ends_thread_only() -> None:
    import threading
    r = Rig()

    def boom() -> object:
        raise RuntimeError("synthetic")
    r.feed._client.ticker_price = lambda s: boom()  # type: ignore[method-assign]  # noqa: SLF001
    stop = threading.Event()
    t = threading.Thread(target=r.feed.run, args=(stop,), name="signal-feed", daemon=True)
    hook, threading.excepthook = threading.excepthook, lambda a: None
    try:
        t.start()
        t.join(2)
    finally:
        threading.excepthook = hook
        stop.set()
    assert not t.is_alive()


# --- V-318 DB 메타 signal_source ---------------------------------------------------------------------------------

def _db(tmp: Path, kind: str | None) -> Path:
    path = tmp / "trading.db"
    repo = Repository(path, Mode.PAPER, order_identity=OrderIdentity.SYMBOL)
    try:
        bind_market(repo.connection, Market.M2, "binance-spot-testnet", created=True)
        if kind is not None:
            bind_signal_source(repo.connection, kind, created=True)
    finally:
        repo.close()
    return path


def test_v318_new_db_records_kind_and_mismatch_refused(tmp_path: Path) -> None:
    path = _db(tmp_path, KIND_PUBLIC)
    conn = sqlite3.connect(path)
    try:
        assert read_signal_meta(conn) == KIND_PUBLIC
        bind_signal_source(conn, KIND_PUBLIC, created=False)
        with pytest.raises(MarketMismatch, match="신호 출처"):
            bind_signal_source(conn, KIND_TESTNET, created=False)
    finally:
        conn.close()


def test_v318_legacy_db_testnet_allowed_unwritten_public_refused(tmp_path: Path) -> None:
    path = _db(tmp_path, None)
    conn = sqlite3.connect(path)
    try:
        bind_signal_source(conn, KIND_TESTNET, created=False)
        assert read_signal_meta(conn) is None                              # 기존 DB 바이트 불변 (V-326)
        with pytest.raises(MarketMismatch, match="기간 재시작"):
            bind_signal_source(conn, KIND_PUBLIC, created=False)
    finally:
        conn.close()


# --- V-319 판정 입력 구성 (SpotProfile) / V-326 testnet 불변 ---------------------------------------------------------

class Recorder:
    def __init__(self) -> None:
        self.spec = fixed_spec(strategy_id="t-sig")
        self.snaps: list[EvaluationSnapshot] = []

    def evaluate(self, snapshot: EvaluationSnapshot, params) -> list:  # type: ignore[no-untyped-def]
        self.snaps.append(snapshot)
        return []


def _world(tmp: Path, signal: SignalInputs | None) -> tuple[M2World, Recorder]:
    rec = Recorder()
    w = M2World(tmp, strategies=[StrategyInput(rec, {"qty": "0.0100"}, (time(1, 0),))], signal=signal)
    w.set_baseline()
    w.start_and_resume()
    return w, rec


def test_v319_profile_bars_signal_quote_testnet_reference_signal(tmp_path: Path) -> None:
    ref = SignalFacts((SYM,))
    w, rec = _world(tmp_path, SignalInputs(ref, D("0.01")))
    w.at(1, 0, 1)
    ref.update_reference(SYM, D("9950.00"), w.clock.now() - timedelta(seconds=59))
    w.engine.tick()
    v = rec.snaps[-1].instruments[0]
    assert v.last_price == D("10000.00") and v.reference_price == D("9950.00")
    ev = json.loads(w.audits("EVALUATION")[-1]["summary"])
    assert ev["signal_source"] == KIND_PUBLIC and ev["prices"][SYM]["signal"] == "9950.00"
    assert ev["prices"][SYM]["exec"] == "10000.00" and len(ev["daily"][SYM]["fingerprint"]) == 16
    w.close()


def test_v319_stale_or_missing_reference_is_none(tmp_path: Path) -> None:
    ref = SignalFacts((SYM,))
    w, rec = _world(tmp_path, SignalInputs(ref, D("0.01")))
    w.tick_at(1, 0, 1)
    assert rec.snaps[-1].instruments[0].reference_price is None
    w.close()


def test_v326_testnet_default_reference_equals_quote_and_no_new_audit_keys(tmp_path: Path) -> None:
    w, rec = _world(tmp_path, None)
    w.tick_at(1, 0, 1)
    v = rec.snaps[-1].instruments[0]
    assert v.reference_price == v.last_price == D("10000.00")
    ev = json.loads(w.audits("EVALUATION")[-1]["summary"])
    assert "signal_source" not in ev and "prices" not in ev
    w.close()


def test_minute_input_source_value(tmp_path: Path) -> None:
    from trading_system.domain.minute_bar import MinuteBar
    from trading_system.domain.minute_series import NO_ADJUSTMENT
    book = SpotMinuteBook((PSYM,))
    inp = M2MinuteInput(book, lookback_bars=5, source=PUBLIC_BAR_SOURCE)
    t = datetime(2026, 10, 10, 0, 30, tzinfo=UTC)
    book.put_minute_bars(PSYM, [MinuteBar(PSYM, 1, t, D(1), D(1), D(1), D(1), D(1), NO_ADJUSTMENT)],
                         t + timedelta(minutes=1, seconds=5))
    repo = Repository(tmp_path / "m.db", Mode.PAPER)
    try:
        inp.flush(repo, None, t + timedelta(minutes=2))
        assert [r["source"] for r in repo.select("SELECT source FROM minute_bar")] == [PUBLIC_BAR_SOURCE]
    finally:
        repo.close()
    assert M2MinuteInput(SpotMinuteBook((PSYM,)), lookback_bars=5).source == "binance-testnet"   # 기본 불변


# --- 운영 시작 (run 조립): 연결 전 거부·스레드·감시 -----------------------------------------------------------------

def _paper_root(tmp: Path, *, signal: str, approvals: str) -> Path:
    from tests.scenario.test_tm2_08_market_split import make_m2_root
    root = make_m2_root(tmp, Mode.PAPER, symbols='quote_asset = "USDT"\nsymbols = ["BTCUSDT"]\n')
    d = root / "config" / "m2"
    (d / "app.paper.toml").write_text((d / "app.paper.toml").read_text(encoding="utf-8")
                                      + "\n[paper]\nconnection_approved = true\n" + signal, encoding="utf-8")
    (root / ".env.paper").write_text("TRADE_MODE=PAPER\nBINANCE_TESTNET_API_KEY=synthetic-k\n"
                                     "BINANCE_TESTNET_SECRET_KEY=synthetic-s\n", encoding="utf-8")
    (root / "docs").mkdir()
    shutil.copy(Path(__file__).resolve().parents[2] / "docs" / "capabilities.md", root / "docs" / "capabilities.md")
    (root / "docs" / "approvals.md").write_text(approvals, encoding="utf-8")
    return root


PUBLIC_ON = '\n[signal_source]\nkind = "binance-public"\napproval = "A-901"\n'


def _wall() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _run(root: Path, public: PublicSim | None):  # type: ignore[no-untyped-def]
    from tests.support.binance_exchange_sim import ExchangeSim
    from tests.support.binance_ws_fake import FakeWsApi
    from trading_system.app.main import run_main
    from trading_system.broker.binance.feed import FeedPlan
    from trading_system.broker.binance.run_start import M2RunConnector
    sim = ExchangeSim(_wall)
    code = run_main(root, {"TRADE_MODE": "PAPER"}, max_seconds=0.4, market="M2",
                    m2_connector=M2RunConnector(sim.factory(), FakeWsApi(sim).connect, feed_plan=FeedPlan(loop_s=0.05)),
                    m2_signal_transport=public)
    return code, sim


def test_run_public_without_recorded_approval_refused_no_connection(tmp_path: Path,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    root = _paper_root(tmp_path, signal=PUBLIC_ON, approvals="| A-001 | x |\n")
    pub = PublicSim(lambda: datetime.now(UTC))
    code, sim = _run(root, pub)
    assert code == 2 and sim.sent == [] and pub.calls == []
    assert "A-901" in capsys.readouterr().out


def test_run_public_without_injected_transport_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _paper_root(tmp_path, signal=PUBLIC_ON, approvals="| A-901 | 시험용 가상 승인 |\n")
    code, sim = _run(root, None)
    assert code == 2 and sim.sent == []
    assert "신호 출처" in capsys.readouterr().out


def test_run_public_starts_signal_feed_and_records_db_meta(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _paper_root(tmp_path, signal=PUBLIC_ON, approvals="| A-901 | 시험용 가상 승인 |\n")
    pub = PublicSim(lambda: datetime.now(UTC))
    code, sim = _run(root, pub)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "신호 출처: binance-public" in out and sim.order_posts() == []
    assert pub.public_calls(TICKER_PATH) and pub.public_calls(KLINES_PATH, "1d")
    assert {h for h, _p, _q in pub.calls} == set(PUBLIC_HOSTS)
    assert ("GET", "/api/v3/klines") not in sim.paths()                # 테스트넷 1d·1m 봉은 조회하지 않음 (D-248)
    conn = sqlite3.connect(root / "var" / "paper" / "trading.db")
    try:
        assert read_signal_meta(conn) == KIND_PUBLIC
    finally:
        conn.close()
    # 같은 DB를 testnet 설정으로 열면 시작 거부 (V-318)
    d = root / "config" / "m2" / "app.paper.toml"
    d.write_text(d.read_text(encoding="utf-8").replace(PUBLIC_ON, ""), encoding="utf-8")
    code2, sim2 = _run(root, pub)
    assert code2 == 2 and sim2.sent == [] and "신호 출처" in capsys.readouterr().out


def test_run_testnet_default_has_no_signal_feed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _paper_root(tmp_path, signal="", approvals="| A-001 | x |\n")
    code, sim = _run(root, None)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "신호 출처" not in out
    conn = sqlite3.connect(root / "var" / "paper" / "trading.db")
    try:
        assert read_signal_meta(conn) is None                              # testnet 기본은 메타를 쓰지 않음 (V-326)
    finally:
        conn.close()


def test_signal_source_change_tool_restarts_period(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from trading_system.app.m2_baseline import m2_baseline_main
    root = _paper_root(tmp_path, signal="", approvals="| A-901 | 시험용 가상 승인 |\n")
    code, _sim = _run(root, None)                                          # testnet DB 생성
    assert code == 0
    d = root / "config" / "m2" / "app.paper.toml"
    d.write_text(d.read_text(encoding="utf-8") + PUBLIC_ON, encoding="utf-8")
    code, _ = _run(root, PublicSim(lambda: datetime.now(UTC)))
    assert code == 2                                                       # 출처 변경은 기간 재시작 절차로만
    from trading_system.domain.clock import FixedClock
    later = FixedClock(datetime.now(UTC) + timedelta(hours=1))           # 하트비트 신선도 검사(엔진 정지 확인)를 지나게
    assert m2_baseline_main(["signal-source-change", "--mode", "PAPER", "--market", "M2", "--operator", "tester"],
                            root, clock=later) == 0
    assert m2_baseline_main(["signal-source-change", "--mode", "PAPER", "--market", "M2"], root, clock=later) == 2
    repo = Repository(root / "var" / "paper" / "trading.db", Mode.PAPER, order_identity=OrderIdentity.SYMBOL)
    try:
        ev = [json.loads(e["summary"]) for e in repo.audit_events() if e["kind"] == "PAPER_PERIOD_RESTART"]
        assert ev and ev[-1]["signal_source"] == KIND_PUBLIC and ev[-1]["approval"] == "A-901"
    finally:
        repo.close()
    code, _ = _run(root, PublicSim(lambda: datetime.now(UTC)))
    assert code == 0
