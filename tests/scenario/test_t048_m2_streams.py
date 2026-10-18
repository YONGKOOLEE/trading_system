"""CP-036 T-048 — 사용자 스트림 WS(D-205)·시세·규칙·서버 시각 공급(D-206)·스레드·정지 연결(D-215). V-286, V-287.

네트워크 없음: 메모리 가짜 WS API(tests/support/binance_ws_fake)·합성 거래소(binance_exchange_sim), 그리고 127.0.0.1 루프백
가짜 WS 서버(websockets 고정 버전, 외부 연결 없음)로 WS 프레임·ping/pong만 확인한다. 키는 가상 값.
"""

from __future__ import annotations

import json
import shutil
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_t047_m2_adapter import ACCOUNT, KEY, SECRET, SYM, Run, _ms
from tests.scenario.test_tm2_08_market_split import make_m2_root
from tests.support.binance_exchange_sim import ExchangeSim
from tests.support.binance_ws_fake import FakeWsApi, wait_until
from trading_system.app.main import run_main
from trading_system.app.m2_relay import M2Relay
from trading_system.broker.binance.client import TESTNET_REST, BinanceSpotClient
from trading_system.broker.binance.feed import BinanceFeed, FeedPlan
from trading_system.broker.binance.run_guard import RunGuard
from trading_system.broker.binance.run_start import M2RunConnector
from trading_system.broker.binance.signing import Credentials
from trading_system.broker.binance.stream import parse_event
from trading_system.broker.binance.user_stream import StreamState, UserStream
from trading_system.broker.binance.weights import CallPriority, WeightBudget, parse_rate_limits
from trading_system.broker.ws_transport import WsTargetError, WsTransport
from trading_system.domain.clock import FixedClock
from trading_system.domain.enums import Mode, OrderType
from trading_system.domain.models import OrderRequest, client_order_id_of
from trading_system.marketdata.spot_facts import SpotFactsStore
from trading_system.storage.repository import Repository

D = Decimal
REPO = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 10, 0, 30, tzinfo=UTC)


# --- 사용자 스트림 (V-286, 메모리 가짜 WS API) ---------------------------------------------------------------

class StreamRig:
    def __init__(self, **kw: object) -> None:
        from tests.contract.test_binance_adapter import Rig
        self.rig = Rig()
        self.sim = self.rig.sim
        self.api = FakeWsApi(self.sim)
        self.states: list[tuple[str, str]] = []
        self.waits: list[float] = []
        self.stream = UserStream(self.api.connect, Credentials(KEY, SECRET), self.rig.adapter,
                                 now_ms=lambda: self.rig.t, on_state=lambda s, m: self.states.append((s.value, m)),
                                 recv_timeout_s=0.01, reply_timeout_s=0.2,
                                 wait=lambda stop, s: self.waits.append(s), **kw)  # type: ignore[arg-type]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.stream.run, args=(self.stop,), daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        self.thread.join(5)

    def kinds(self) -> list[str]:
        return [s for s, _ in self.states]


def _buy(idem: str = "i-1") -> OrderRequest:
    return OrderRequest("NEW", "BN-PAPER-01", SYM, "BUY", D("0.00100"), OrderType.LIMIT, D("60300.00"),
                        client_order_id=client_order_id_of(idem))


def test_v286_subscribe_signed_request_then_stream_fill_confirms_full_fill() -> None:
    r = StreamRig()
    try:
        r.start()
        assert wait_until(lambda: r.kinds() == ["ONLINE"])
        [sub] = r.api.current.sent
        assert sub["method"] == "userDataStream.subscribe.signature"
        assert set(sub["params"]) == {"apiKey", "timestamp", "signature"} and sub["params"]["apiKey"] == KEY.reveal()
        assert r.rig.adapter.place_order(_buy()).outcome.value == "ACCEPTED"
        events = list(r.rig.adapter.poll_events())
        assert [e.quote_confirmed for e in events if e.kind == "FILL"] == [False]   # FULL 응답(임시 금액)

        def confirmed() -> bool:
            events.extend(r.rig.adapter.poll_events())
            return [e.quote_confirmed for e in events if e.kind == "FILL"] == [False, True]
        assert wait_until(confirmed)                                     # 스트림 체결 = 같은 체결 ID 확정 1회
        fills = [e for e in events if e.kind == "FILL"]
        assert fills[0].fill_id == fills[1].fill_id
    finally:
        r.close()
    assert SECRET.reveal() not in repr(r.stream) and KEY.reveal() not in repr(r.stream)


def test_v286_disconnect_offline_then_reconnected_with_backoff() -> None:
    r = StreamRig()
    try:
        r.start()
        assert wait_until(lambda: r.kinds() == ["ONLINE"])
        r.api.current.drop()
        assert wait_until(lambda: r.kinds()[-1:] == ["RECONNECTED"])
        assert r.kinds() == ["ONLINE", "OFFLINE", "RECONNECTED"] and r.waits[:1] == [1.0]
        assert r.stream.subscriptions == 2
    finally:
        r.close()


def test_v286_server_shutdown_rotates_immediately() -> None:
    r = StreamRig()
    try:
        r.start()
        assert wait_until(lambda: r.kinds() == ["ONLINE"])
        r.api.current.push({"subscriptionId": 0, "event": {"e": "serverShutdown", "E": r.rig.t}})
        assert wait_until(lambda: r.kinds() == ["ONLINE", "RECONNECTED"])
        assert r.waits == [] and len(r.api.conns) == 2 and r.api.conns[0].closed
    finally:
        r.close()


def test_v286_rotates_before_24h() -> None:
    r = StreamRig()
    try:
        r.start()
        assert wait_until(lambda: r.kinds() == ["ONLINE"])
        r.rig.t += 23 * 3_600_000 + 50 * 60_000
        assert wait_until(lambda: r.kinds() == ["ONLINE", "RECONNECTED"])
    finally:
        r.close()


def test_v286_subscription_refused_stops_without_retry() -> None:
    r = StreamRig()
    r.api.refuse = True
    r.start()
    r.thread.join(5)
    assert not r.thread.is_alive() and r.kinds() == ["REFUSED"] and len(r.api.conns) == 1
    assert "-1022" in r.states[0][1] and "Signature" not in r.states[0][1]


def test_v286_connect_failures_back_off_then_online() -> None:
    r = StreamRig()
    r.api.fail_connects = 3
    try:
        r.start()
        assert wait_until(lambda: r.kinds()[-1:] == ["ONLINE"])
        assert r.waits[:3] == [1.0, 2.0, 4.0] and r.kinds().count("OFFLINE") == 3
    finally:
        r.close()


def test_v286_bad_message_warns_and_continues() -> None:
    r = StreamRig()
    try:
        r.start()
        assert wait_until(lambda: r.kinds() == ["ONLINE"])
        r.api.current.push({"subscriptionId": 0, "event": {"e": "executionReport", "s": SYM}})
        r.api.current.push("not json")
        # 재검토 DP-67: 둘 다 해석 불가로 세되, 같은 조건의 반복 경보는 묶어 상태 알림은 1건 (이전 기대값 2건)
        assert wait_until(lambda: r.stream.bad_messages == 2)
        assert r.kinds().count("BAD_MESSAGE") == 1
        assert r.stream.state is StreamState.ONLINE and len(r.api.conns) == 1
    finally:
        r.close()


# --- 루프백 가짜 WS 서버 (websockets 17.1 — 프레임·ping/pong·호스트 검사) -------------------------------------

def test_ws_transport_refuses_hosts_outside_testnet_before_connecting() -> None:
    t = WsTransport(frozenset({"ws-api.testnet.binance.vision"}))
    for url in ("wss://ws-api.binance.com/ws-api/v3", "ws://ws-api.testnet.binance.vision/ws-api/v3",
                "wss://ws-api.testnet.binance.vision:9443/ws-api/v3", "ws://127.0.0.1:1/x"):
        with pytest.raises(WsTargetError):
            t.connect(url)
    assert t.connects == 0


def test_loopback_server_subscribe_event_and_auto_pong() -> None:
    from websockets.sync.server import serve
    got: dict[str, object] = {}
    done = threading.Event()

    def handler(ws) -> None:  # type: ignore[no-untyped-def]
        req = json.loads(ws.recv())
        got["method"] = req["method"]
        ws.send(json.dumps({"id": req["id"], "status": 200, "result": {"subscriptionId": 0}}))
        got["pong"] = ws.ping().wait(2)          # 클라이언트 라이브러리가 자동 pong
        ws.send(json.dumps({"subscriptionId": 0, "event": {"e": "outboundAccountPosition", "E": 1, "u": 1,
                                                            "B": [{"a": "USDT", "f": "1", "l": "0"}]}}))
        done.wait(3)

    with serve(handler, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        th = threading.Thread(target=server.serve_forever, daemon=True)
        th.start()
        from tests.contract.test_binance_adapter import Rig
        rig = Rig()
        states: list[str] = []
        transport = WsTransport(frozenset({"127.0.0.1"}), loopback_for_tests=True)
        stream = UserStream(transport.connect, Credentials(KEY, SECRET), rig.adapter, now_ms=lambda: 0,
                            on_state=lambda s, m: states.append(s.value), url=f"ws://127.0.0.1:{port}",
                            recv_timeout_s=0.05)
        stop = threading.Event()
        t = threading.Thread(target=stream.run, args=(stop,), daemon=True)
        t.start()
        assert wait_until(lambda: "pong" in got, timeout=5)
        stop.set()
        done.set()
        t.join(5)
        server.shutdown()
    assert got["method"] == "userDataStream.subscribe.signature" and got["pong"] is True and states[0] == "ONLINE"


# --- 시세·규칙·서버 시각 공급 (V-287) --------------------------------------------------------------------------

class FeedRig:
    def __init__(self) -> None:
        self.clock = FixedClock(T0)
        self.sim = ExchangeSim(lambda: _ms(self.clock))
        self.alerts: list[tuple[str, str]] = []
        self.halts: list[tuple[str, str]] = []
        self.budget = WeightBudget(parse_rate_limits([{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE",
                                                        "intervalNum": 1, "limit": 200}]), lambda: _ms(self.clock))
        self.guard = RunGuard(self.sim, orders_enabled=False)
        self.client = BinanceSpotClient(TESTNET_REST, Credentials(KEY, SECRET), self.guard, self.budget,
                                        now_ms=lambda: _ms(self.clock), sleep=lambda s: None,
                                        on_alert=lambda k, m: self.alerts.append((k, m)))
        self.facts = SpotFactsStore(now=self.clock.now, universe=(SYM,))
        self.feed = BinanceFeed(self.client, self.facts, [SYM], now=self.clock.now,
                                alert=lambda k, m: self.alerts.append((k, m)), halts=self, account=ACCOUNT)

    def activate(self, halt_type, actor, reason, account=None, **kw):  # type: ignore[no-untyped-def]
        self.halts.append((halt_type.value, reason))
        return ""

    def report_condition(self, *a, **kw) -> None:  # type: ignore[no-untyped-def]
        return None

    def at(self, delta: timedelta) -> None:
        self.clock._now = T0 + delta  # noqa: SLF001
        self.feed.step()

    def paths(self) -> list[str]:
        return [p for _m, p, _ in self.sim.sent]


def test_v287_quotes_at_receive_time_and_only_final_daily_bars() -> None:
    f = FeedRig()
    f.at(timedelta(0))
    q = f.facts.quote(SYM)
    assert q is not None and (q.price, q.at) == (D("60000.00"), T0)
    bars = f.facts.daily_bars(SYM)
    assert bars and bars[-1].at == T0.date() - timedelta(days=1)          # 진행 중(오늘) 봉 제외
    assert "/api/v3/order" not in f.paths() and f.guard.order_sent == 0


def test_v287_61s_gap_leaves_quote_stale_for_gate() -> None:
    f = FeedRig()
    f.at(timedelta(0))
    f.sim.market_down = True
    for s in range(5, 66, 5):
        f.at(timedelta(seconds=s))
    q = f.facts.quote(SYM)
    assert q is not None and q.at == T0 and any(k == "FEED" for k, _ in f.alerts)   # 갱신 없음 → 61초 지연


def test_v287_utc_midnight_reloads_rules_and_klines() -> None:
    f = FeedRig()
    f.at(timedelta(0))
    n_info = f.paths().count("/api/v3/exchangeInfo")
    f.at(timedelta(hours=23, minutes=31))                                  # UTC 00:01 다음 날
    assert f.paths().count("/api/v3/exchangeInfo") == n_info + 1
    assert f.facts.daily_bars(SYM)[-1].at == T0.date()                    # 어제 봉 확정 반영
    assert f.facts.rules(SYM) is not None


def test_v287_filter_reject_triggers_rules_reload_and_failure_clears_rules() -> None:
    f = FeedRig()
    f.feed.request_rules_reload(SYM)                                      # -1013 뒤 (어댑터 on_filter_reject)
    f.at(timedelta(0))
    assert f.facts.rules(SYM) is not None and f.paths().count("/api/v3/exchangeInfo") == 1
    f.sim.exchange_body = {"timezone": "UTC", "symbols": []}             # rateLimits 없음 → 적재 실패
    f.feed.request_rules_reload(SYM)
    f.at(timedelta(seconds=1))
    assert f.facts.rules(SYM) is None and any(k == "RULES" for k, _ in f.alerts)
    assert f.facts.units(SYM) is not None                                 # 위험 축소용 단위는 남음 (D-106)


def test_v287_server_time_offset_warning() -> None:
    f = FeedRig()
    f.sim.server_offset_ms = 600
    f.at(timedelta(0))
    assert any(k == "CLOCK" for k, _ in f.alerts)


def test_v287_budget_shortage_holds_market_calls_first() -> None:
    f = FeedRig()
    assert f.budget.admit(69, CallPriority.RESOLVE).ok                      # 예산 100 중 69 사용 → 시세 상한 70
    f.at(timedelta(0))
    assert "/api/v3/ticker/price" not in f.paths() and any("보류" in m or "예산" in m for _, m in f.alerts)


def test_v287_kline_quality_failure_k10() -> None:
    f = FeedRig()
    f.sim.bad_klines = True
    f.at(timedelta(0))
    assert [h for h, _ in f.halts] == ["K10"] and f.facts.daily_bars(SYM) == ()


# --- 엔진 경로: 스트림 끊김 → K5 → 재연결 → STREAM_RECONNECT 대조 → 해제, 반영은 판정·실행 스레드만 (V-286, D-215) ------

def _started(tmp: Path) -> Run:
    r = Run(tmp)
    r.engine.account_state.set_baseline(ACCOUNT, D("10000"), r.clock.now())  # type: ignore[attr-defined]
    r.engine.start()
    with r.repo.tx():
        r.repo.insert_command("c1", "LOCAL", "tester", "RESUME", {"account": None, "halt_type": "K8"}, r.clock.now())
    r.at(0, 59)
    assert r.repo.active_halts(ACCOUNT) == []
    return r


def test_v286_engine_stream_drop_k5_reconnect_reconcile_release(tmp_path: Path) -> None:
    r = _started(tmp_path)
    relay = M2Relay(ACCOUNT)
    relay.attach(r.engine)
    api = FakeWsApi(r.sim)
    stream = UserStream(api.connect, r.session.credentials, r.session.adapter, now_ms=lambda: _ms(r.clock),
                        on_state=lambda s, m: relay.stream_state(s.value, m), recv_timeout_s=0.01,
                        wait=lambda stop, s: None)
    stop = threading.Event()
    t = threading.Thread(target=stream.run, args=(stop,), daemon=True)
    t.start()
    try:
        assert wait_until(lambda: stream.subscriptions == 1)
        api.current.drop()
        assert wait_until(lambda: stream.subscriptions == 2)
        r.at(0, 59)                                                        # 큐: K5 발동 → 경로 정상 → 대조 요청
        triggers = [x["trigger"] for x in r.repo.select("SELECT trigger FROM reconciliation ORDER BY created_seq")]
        assert "STREAM_RECONNECT" in triggers
        r.at(0, 59)
        assert "K5" not in r.repo.active_halts(ACCOUNT)                    # 연결 복구 + 대조 일치 → 자동 해제
        assert any(e["kind"] == "HALT_ACTIVATED" and '"K5"' in e["summary"] for e in r.repo.audit_events())
    finally:
        stop.set()
        t.join(5)
        r.repo.close()


def test_v286_stream_fill_applied_only_on_engine_tick(tmp_path: Path) -> None:
    r = _started(tmp_path)
    try:
        r.sim.hold_open = True
        r.at(1, 0)                                                         # 매수 IOC — 합성: 열린 채로
        [order] = list(r.sim.orders.values())
        before = len(r.sim.events)
        r.sim.fill_open(order["orderId"])
        for raw in r.sim.events[before:]:
            r.session.adapter.ingest_stream(parse_event(raw))              # 스트림 스레드 몫: 큐에만
        assert r.repo.position(ACCOUNT, SYM).quantity == 0                  # 아직 반영 안 됨
        r.at(1, 1)
        assert r.repo.position(ACCOUNT, SYM).quantity == D("0.000999")      # 판정·실행 스레드 tick에서 반영
    finally:
        r.repo.close()


# --- run --market M2 PAPER (주입 연결, 주문 차단 run) — D-209 시작 순서 ---------------------------------------------

def _paper_root(tmp: Path, paper: str, *, keys: bool = True) -> Path:
    root = make_m2_root(tmp, Mode.PAPER, symbols='quote_asset = "USDT"\nsymbols = ["BTCUSDT"]\n')
    app = root / "config" / "m2" / "app.paper.toml"
    app.write_text(app.read_text(encoding="utf-8") + paper, encoding="utf-8")
    if keys:
        (root / ".env.paper").write_text("TRADE_MODE=PAPER\nBINANCE_TESTNET_API_KEY=synthetic-k\n"
                                         "BINANCE_TESTNET_SECRET_KEY=synthetic-s\n", encoding="utf-8")
    (root / "docs").mkdir()
    shutil.copy(REPO / "docs" / "capabilities.md", root / "docs" / "capabilities.md")
    return root


def _connector(sim: ExchangeSim, api: FakeWsApi) -> M2RunConnector:
    return M2RunConnector(sim.factory(), api.connect, feed_plan=FeedPlan(loop_s=0.05))


def _wall() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def test_run_m2_paper_order_blocked_observation_run_starts_k8_threads_no_orders(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _paper_root(tmp_path, "\n[paper]\nconnection_approved = true\n")
    sim = ExchangeSim(_wall)
    api = FakeWsApi(sim)
    assert run_main(root, {"TRADE_MODE": "PAPER"}, max_seconds=0.5, market="M2",
                    m2_connector=_connector(sim, api)) == 0
    out = capsys.readouterr().out
    assert "주문 차단 run" in out and "시장 M2" in out and "서버 시각 오차" in out
    assert sim.order_posts() == [] and len(api.conns) >= 1
    assert ("GET", "/api/v3/ticker/price") in sim.paths()
    repo = Repository(root / "var" / "paper" / "trading.db", Mode.PAPER)
    try:
        assert "K8" in repo.active_halts("BN-PAPER-01")
        [sw] = [e for e in repo.audit_events() if e["kind"] == "ORDER_SWITCH"]
        assert '"orders_enabled": false' in sw["summary"]
    finally:
        repo.close()


def test_run_m2_paper_without_injected_connector_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _paper_root(tmp_path, "\n[paper]\nconnection_approved = true\n")
    assert run_main(root, {"TRADE_MODE": "PAPER"}, market="M2") == 2
    assert "scripts/run-m2.py" in capsys.readouterr().out and not (root / "var" / "paper" / "trading.db").exists()


def test_v283b_run_m2_paper_start_refused_when_rate_limits_missing(tmp_path: Path,
                                                                   capsys: pytest.CaptureFixture[str]) -> None:
    root = _paper_root(tmp_path, "\n[paper]\nconnection_approved = true\n")
    sim = ExchangeSim(_wall, exchange_body={"timezone": "UTC", "symbols": []})
    api = FakeWsApi(sim)
    assert run_main(root, {"TRADE_MODE": "PAPER"}, market="M2", m2_connector=_connector(sim, api)) == 2
    out = capsys.readouterr().out
    assert "rateLimits" in out and sim.order_posts() == [] and api.conns == []
    assert not (root / "var" / "paper" / "trading.db").exists()


def test_run_m2_paper_default_config_lists_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _paper_root(tmp_path, "", keys=False)
    sim = ExchangeSim(lambda: 0)
    assert run_main(root, {"TRADE_MODE": "PAPER"}, market="M2", m2_connector=_connector(sim, FakeWsApi(sim))) == 2
    out = capsys.readouterr().out
    assert "connection_approved" in out and "BINANCE_TESTNET_API_KEY" in out and sim.sent == []


# --- 검토 지적 (1): 스트림 재연결 대조 요청은 대조가 끝난 뒤에만 지운다 (SpotProfile.reconcile_step) ----------------

def test_reconnect_reconcile_request_kept_when_cycle_tick_raises(tmp_path: Path) -> None:
    r = _started(tmp_path)
    try:
        profile = r.engine.profile
        cycle = profile.cycle
        real = cycle.tick
        calls: list[bool] = []

        def boom(*, stream_reconnected: bool) -> bool:
            calls.append(stream_reconnected)
            raise RuntimeError("합성: 대조 중 오류")
        cycle.tick = boom  # type: ignore[method-assign]
        r.engine.request_reconcile()
        with pytest.raises(RuntimeError):
            profile.reconcile_step(r.clock.now())
        assert r.engine._reconcile_requested.is_set()                     # noqa: SLF001 — 요청이 남아 있다
        cycle.tick = real  # type: ignore[method-assign]
        profile.reconcile_step(r.clock.now())
        assert not r.engine._reconcile_requested.is_set()                 # noqa: SLF001
        triggers = [x["trigger"] for x in r.repo.select("SELECT trigger FROM reconciliation ORDER BY created_seq")]
        assert calls == [True] and triggers[-1] == "STREAM_RECONNECT"
    finally:
        r.repo.close()
