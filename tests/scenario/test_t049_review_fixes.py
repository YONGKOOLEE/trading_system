"""CP-036 (A-106) T-047~T-049 독립 재검토 반영 시험 — 알림 폭주 억제(DP-67)·RunGuard 메모리·WS 메서드 허용 목록(DP-69)·
주문 전송 승인 A-ID 기록 확인(DP-66)·서버 종료 통보 연속 백오프(DP-68)·reconcile-resolve M2 지연 import·기준선 도구 잠금·
운영 요청 사전 거부 긴급 알림·누락 시험(죽은 공급·스트림 스레드 → K5, /reduce M2, run-m2 호스트 상수).

네트워크 없음: 합성 거래소·메모리 가짜 WS API·가짜 WS 연결 객체만. 키·계좌는 가상 값.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_t047_m2_adapter import SYM, _app
from tests.scenario.test_t048_m2_streams import FeedRig, StreamRig, _connector, _paper_root, _wall
from tests.scenario.test_t049_m2_ops import _baseline, _hold, _m2_root_with_strategy
from tests.support.binance_exchange_sim import ExchangeSim
from tests.support.binance_ws_fake import FakeWsApi, wait_until
from tests.support.m2_engine_world import ACCOUNT as ACCOUNT_M2
from tests.support.m2_engine_world import SYM as M2_SYM
from tests.support.m2_engine_world import M2World
from trading_system.app.main import run_main
from trading_system.app.startup import check_order_switch
from trading_system.broker.base import BrokerQueryError
from trading_system.broker.binance.client import TESTNET_REST
from trading_system.broker.binance.feed import FeedPlan
from trading_system.broker.binance.preflight import TESTNET_HOST
from trading_system.broker.binance.run_guard import RunGuard
from trading_system.broker.binance.run_start import M2RunConnector
from trading_system.broker.binance.user_stream import SUBSCRIBE, WS_API_HOST
from trading_system.broker.ws_transport import WsConnection
from trading_system.config.errors import ConfigError
from trading_system.domain.enums import Mode, OrderType
from trading_system.storage.repository import OrderIdentity, Repository

D = Decimal
REPO = Path(__file__).resolve().parents[2]


# --- 1. 알림 폭주 억제 (DP-67) ----------------------------------------------------------------------------------

def test_one_hour_market_outage_alerts_bounded_with_summary_and_recovery() -> None:
    f = FeedRig()
    f.at(timedelta(0))
    f.sim.market_down = True                                               # 시세·평균가 조회 5XX 1시간
    for s in range(1, 3601):
        f.at(timedelta(seconds=s))
    f.sim.market_down = False
    for s in range(3601, 3601 + 400):                                     # 회복 뒤 안정 확인(5분)
        f.at(timedelta(seconds=s))
    feed = [m for k, m in f.alerts if k == "FEED"]
    ticker = [m for m in feed if m.startswith("ticker ")]
    avg = [m for m in feed if m.startswith("avgPrice ")]
    # 이전: 실패·보류마다 1건(1시간에 ticker 720 + avgPrice 60). 이제 키마다 시작 1 + 30분 요약 + 회복 1
    assert len(feed) <= 8, feed
    assert 2 <= len(ticker) <= 4 and 2 <= len(avg) <= 4
    assert "억제" in ticker[1] and "회복" in ticker[-1] and "억제" in ticker[-1]
    assert "회복" in avg[-1]
    q = f.facts.quote(SYM)
    assert q is not None and q.at == f.clock.now()                       # 회복 뒤 시세 갱신


def test_feed_repeated_failure_after_recovery_alerts_again() -> None:
    f = FeedRig()
    f.at(timedelta(0))
    f.sim.market_down = True
    f.at(timedelta(seconds=5))
    f.sim.market_down = False
    for s in range(6, 320):
        f.at(timedelta(seconds=s))
    f.sim.market_down = True
    f.at(timedelta(seconds=325))
    ticker = [m for k, m in f.alerts if k == "FEED" and m.startswith("ticker ")]
    assert len(ticker) == 3 and "회복" in ticker[1] and "실패" in ticker[2]   # 회복 뒤 새 실패는 바로 알린다


def test_user_stream_bad_messages_alert_once_then_summary() -> None:
    r = StreamRig()
    try:
        r.start()
        assert wait_until(lambda: r.kinds() == ["ONLINE"])
        for _ in range(200):
            r.api.current.push("not json")
        assert wait_until(lambda: r.stream.bad_messages == 200)
        assert r.kinds().count("BAD_MESSAGE") == 1                         # 이전: 200건
        r.rig.t += 31 * 60_000
        r.api.current.push("not json")
        assert wait_until(lambda: r.kinds().count("BAD_MESSAGE") == 2)
        summary = [m for s, m in r.states if s == "BAD_MESSAGE"][1]
        assert "199건" in summary
    finally:
        r.close()


# --- 2. RunGuard 메모리 ------------------------------------------------------------------------------------------

def test_run_guard_keeps_counters_and_bounded_recent_calls() -> None:
    sim = ExchangeSim(lambda: 0)
    guard = RunGuard(sim, orders_enabled=False)
    for _ in range(5000):
        guard.send("GET", f"{TESTNET_REST}/api/v3/ticker/price?symbol=BTCUSDT", {})
    guard.send("GET", f"{TESTNET_REST}/api/v3/time", {})
    assert guard.calls[("GET", "/api/v3/ticker/price")] == 5000 and guard.calls[("GET", "/api/v3/time")] == 1
    assert guard.sent == 5001
    assert len(guard.recent) <= guard.recent.maxlen and guard.recent.maxlen <= 1000   # type: ignore[operator]
    assert guard.recent[-1].path == "/api/v3/time" and guard.recent[-1].status == 200
    assert not hasattr(guard, "records")


# --- 3. WS API 메서드 허용 목록 (DP-69) ---------------------------------------------------------------------------

class _FakeClientConn:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


@pytest.mark.parametrize("payload", [
    {"id": "x", "method": "order.place", "params": {"symbol": "BTCUSDT"}},
    {"id": "x", "method": "order.cancel", "params": {}},
    {"id": "x", "method": "userDataStream.start", "params": {}},
    {"id": "x", "params": {}},
    "not json",
    [1, 2],
])
def test_ws_transport_refuses_methods_outside_user_stream_before_send(payload: object) -> None:
    from trading_system.broker.ws_transport import WsMethodRefused
    raw = _FakeClientConn()
    conn = WsConnection(raw)  # type: ignore[arg-type]
    with pytest.raises(WsMethodRefused):
        conn.send(payload if isinstance(payload, str) else json.dumps(payload))
    assert raw.sent == []


def test_ws_transport_allows_user_stream_subscribe_unsubscribe_ping() -> None:
    from trading_system.broker.ws_transport import WS_API_METHODS
    raw = _FakeClientConn()
    conn = WsConnection(raw)  # type: ignore[arg-type]
    for m in (SUBSCRIBE, "userDataStream.unsubscribe", "ping"):
        conn.send(json.dumps({"id": "1", "method": m, "params": {}}))
    assert len(raw.sent) == 3 and SUBSCRIBE in WS_API_METHODS
    assert not any(m.startswith("order") for m in WS_API_METHODS)


# --- 4. 주문 전송 승인 A-ID = docs/approvals.md 표의 행 (DP-66, D-204 보수 해석) -----------------------------------

_SWITCH_ON = '[paper]\nconnection_approved = true\norders_approved = true\norders_approval = "{a}"\n'
_TABLE = ("# 승인 기록\n\n| A-ID | 일자 | 유형 |\n|---|---|---|\n| A-001 | 2026-09-30 | 산출물 |\n"
          "| A-777 | 2026-10-16 | 실전 주문 코드 |\n")


def test_recorded_approval_ids_reads_only_table_a_id_column(tmp_path: Path) -> None:
    from trading_system.app.startup import recorded_approval_ids
    p = tmp_path / "approvals.md"
    p.write_text(_TABLE + "본문 언급 A-888은 행이 아님. | 비고 | A-999 |\n", encoding="utf-8")
    assert recorded_approval_ids(p) == frozenset({"A-001", "A-777"})
    assert recorded_approval_ids(tmp_path / "missing.md") == frozenset()


def test_order_switch_refuses_a_id_not_recorded(tmp_path: Path) -> None:
    p = tmp_path / "approvals.md"
    p.write_text(_TABLE, encoding="utf-8")
    with pytest.raises(ConfigError, match="A-123"):
        check_order_switch(_app(_SWITCH_ON.format(a="A-123")), deployed_strategies=1, approvals_file=p)
    with pytest.raises(ConfigError, match="승인 기록 표"):                 # 표 파일 없음 = 확인된 A-ID 없음
        check_order_switch(_app(_SWITCH_ON.format(a="A-777")), deployed_strategies=1,
                           approvals_file=tmp_path / "none.md")
    sw = check_order_switch(_app(_SWITCH_ON.format(a="A-777")), deployed_strategies=1, approvals_file=p)
    assert (sw.enabled, sw.approval) == (True, "A-777")


def test_run_m2_refuses_unrecorded_a_id_before_connecting(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _paper_root(tmp_path, "\n" + _SWITCH_ON.format(a="A-778"))
    (root / "docs" / "approvals.md").write_text(_TABLE, encoding="utf-8")
    sim = ExchangeSim(_wall)
    api = FakeWsApi(sim)
    assert run_main(root, {"TRADE_MODE": "PAPER"}, max_seconds=0.5, market="M2",
                    m2_connector=_connector(sim, api)) == 2
    out = capsys.readouterr().out
    assert "A-778" in out and sim.sent == [] and api.conns == []
    assert not (root / "var" / "paper" / "trading.db").exists()


def test_run_m2_starts_with_recorded_a_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _paper_root(tmp_path, "\n" + _SWITCH_ON.format(a="A-777"))
    (root / "docs" / "approvals.md").write_text(_TABLE, encoding="utf-8")
    sim = ExchangeSim(_wall)
    api = FakeWsApi(sim)
    assert run_main(root, {"TRADE_MODE": "PAPER"}, max_seconds=0.3, market="M2",
                    m2_connector=_connector(sim, api)) == 0
    assert "A-777" in capsys.readouterr().out
    repo = Repository(root / "var" / "paper" / "trading.db", Mode.PAPER)
    try:
        [sw] = [e for e in repo.audit_events() if e["kind"] == "ORDER_SWITCH"]
        assert '"orders_enabled": true' in sw["summary"] and "A-777" in sw["summary"]
    finally:
        repo.close()


# --- 5. serverShutdown·eventStreamTerminated 연속 → 백오프 (DP-68) ------------------------------------------------

def test_consecutive_server_terminations_back_off_no_hot_loop() -> None:
    r = StreamRig()
    r.api.terminate_on_subscribe = True                                   # 구독 직후마다 서버 종료 통보
    try:
        r.start()
        assert wait_until(lambda: len(r.api.conns) >= 8)
        r.close()
        # 첫 종료는 바로 교체(기존 V-286), 그 뒤 연속 종료는 1·2·4·8·16·30초 대기
        assert r.waits[:6] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
        assert len(r.api.conns) - 1 <= len(r.waits) + 1                   # 대기 없이 다시 연결한 것은 첫 1번뿐
    finally:
        r.close()


def test_termination_after_stable_session_rotates_immediately_again() -> None:
    r = StreamRig()
    try:
        r.start()
        assert wait_until(lambda: r.kinds() == ["ONLINE"])
        for n in range(2, 5):                                              # 매번 안정 구간(> 60초) 뒤 종료 통보
            r.rig.t += 120_000
            r.api.current.push({"subscriptionId": 0, "event": {"e": "eventStreamTerminated", "E": r.rig.t}})
            assert wait_until(lambda n=n: r.stream.subscriptions == n)
        assert r.waits == []
    finally:
        r.close()


# --- 6. reconcile-resolve: M2 모듈 import 실패가 M1 도구를 막지 않는다 ---------------------------------------------

def _load_script(name: str, file: str):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_reconcile_resolve_m1_dispatch_works_when_m2_module_import_fails(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setitem(sys.modules, "trading_system.app.m2_baseline", None)   # import 하면 ImportError
    mod = _load_script("rr_script_m2_broken", "reconcile-resolve.py")
    assert mod.main(["--mode", "LIVE", "--root", str(tmp_path), "item-1", "EXTERNAL"]) == 2   # M1 본체까지 감
    assert "LIVE" in capsys.readouterr().out
    with pytest.raises(ImportError):
        mod.main(["initial-baseline", "--mode", "BACKTEST", "--market", "M2"])


def test_reconcile_resolve_m2_subcommand_names_match_module() -> None:
    from trading_system.app.m2_baseline import SUBCOMMANDS
    mod = _load_script("rr_script", "reconcile-resolve.py")
    assert tuple(mod.M2_SUBCOMMANDS) == tuple(SUBCOMMANDS)
    assert mod.REST_HOSTS == frozenset({TESTNET_HOST})


# --- 7. 기준선 도구: 두 번째 잔고 조회 실패 → 거부, 실행 잠금 보유 ----------------------------------------------------

def test_reset_confirm_second_balance_query_failure_refused_nothing_written(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import trading_system.app.m2_baseline as mb
    root = _m2_root_with_strategy(tmp_path)
    assert _baseline(root, "initial-baseline", "--quote-amount", "10000") == 0

    class Flaky(mb._Balances):  # noqa: SLF001
        calls = 0

        def query_balances(self, account: str):  # type: ignore[no-untyped-def]
            Flaky.calls += 1
            if Flaky.calls >= 2:
                raise BrokerQueryError("합성: 조회 실패")
            return super().query_balances(account)
    monkeypatch.setattr(mb, "_Balances", Flaky)
    capsys.readouterr()
    assert _baseline(root, "reset-confirm", "--quote-amount", "9000") == 2
    out = capsys.readouterr().out
    assert "거부" in out and "잔고 조회 실패" in out and Flaky.calls == 2
    repo = Repository(root / "var" / "backtest" / "trading.db", Mode.BACKTEST, order_identity=OrderIdentity.SYMBOL)
    try:
        kinds = [e["kind"] for e in repo.audit_events()]
        assert "TESTNET_RESET_CONFIRMED" not in kinds and "PAPER_PERIOD_RESTART" not in kinds
    finally:
        repo.close()


def test_baseline_tool_holds_run_lock_while_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import trading_system.app.m2_baseline as mb
    from trading_system.app.instance_lock import InstanceLocked, acquire_instance_lock
    root = _m2_root_with_strategy(tmp_path)
    base = root / "var" / "backtest"
    seen: list[str] = []
    real = mb.initial_baseline

    def probe(*a, **kw):  # type: ignore[no-untyped-def]
        try:
            acquire_instance_lock(base).release()
            seen.append("엔진 시작 가능")
        except InstanceLocked:
            seen.append("잠김")
        return real(*a, **kw)
    monkeypatch.setattr(mb, "initial_baseline", probe)
    assert _baseline(root, "initial-baseline", "--quote-amount", "10000") == 0
    assert seen == ["잠김"]
    acquire_instance_lock(base).release()                                 # 도구가 끝나면 풀림


# --- 8·9. 운영 요청: 사전 거부도 긴급 알림, /reduce M2 경로 --------------------------------------------------------

def _emergencies(w: M2World) -> list[str]:
    return [json.loads(e["summary"])["message"] for e in w.repo.audit_events()
            if e["kind"] == "ALERT" and json.loads(e["summary"])["level"] == "EMERGENCY"]


@pytest.fixture
def w(tmp_path: Path):  # type: ignore[no-untyped-def]
    world = M2World(tmp_path, strategies=[])
    world.set_baseline()
    _hold(world)
    world.start_and_resume()
    yield world
    world.repo.close()


@pytest.mark.parametrize("cmd", ["EXIT", "REDUCE"])
def test_ops_prepare_refusal_raises_emergency_like_gate_refusal(w: M2World, cmd: str) -> None:
    w.command(cmd, {"account": ACCOUNT_M2, "instrument": M2_SYM, "quantity": "0.00995"})
    w.tick_at(1, 0)
    assert w.sent() == []
    [row] = w.repo.select(f"SELECT status FROM ops_command WHERE command='{cmd}'")
    assert row["status"] == "REJECTED"
    assert any(f"운영 요청 거부 ({cmd})" in m and "LOT" in m for m in _emergencies(w))


def test_reduce_m2_market_sell_partial_quantity(w: M2World) -> None:
    w.command("REDUCE", {"account": ACCOUNT_M2, "instrument": M2_SYM, "quantity": "0.0050"})
    w.tick_at(1, 0)
    [sell] = w.sent()
    assert (sell.side, sell.order_type, sell.quantity) == ("SELL", OrderType.MARKET, D("0.0050"))
    [row] = w.repo.select("SELECT status FROM ops_command WHERE command='REDUCE'")
    assert row["status"] == "DONE"


# --- 9. 죽은 바이낸스 공급·스트림 스레드 → K5 + 긴급 알림, run-m2 호스트 상수 ------------------------------------------

class _DyingConnector(M2RunConnector):
    def __call__(self, **kw):  # type: ignore[no-untyped-def,override]
        run = super().__call__(**kw)

        def die(stop: threading.Event) -> None:
            raise RuntimeError("합성: 스레드 종료")
        if not isinstance(run, str):
            run.feed.run = die  # type: ignore[method-assign]
            run.stream.run = die  # type: ignore[method-assign]
        return run


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_dead_binance_feed_and_user_stream_threads_raise_k5(tmp_path: Path) -> None:
    root = _paper_root(tmp_path, "\n[paper]\nconnection_approved = true\n")
    sim = ExchangeSim(_wall)
    api = FakeWsApi(sim)
    connector = _DyingConnector(sim.factory(), api.connect, feed_plan=FeedPlan(loop_s=0.05))
    assert run_main(root, {"TRADE_MODE": "PAPER"}, max_seconds=2.5, market="M2", m2_connector=connector) == 0
    repo = Repository(root / "var" / "paper" / "trading.db", Mode.PAPER)
    try:
        assert "K5" in repo.active_halts("BN-PAPER-01")
        msgs = [json.loads(e["summary"])["message"] for e in repo.audit_events() if e["kind"] == "ALERT"
                and json.loads(e["summary"])["level"] == "EMERGENCY"]
        assert any("바이낸스 시세 공급 스레드 종료" in m for m in msgs)
        assert any("바이낸스 사용자 스트림 스레드 종료" in m for m in msgs)
    finally:
        repo.close()
    assert sim.order_posts() == []


def test_run_m2_script_hosts_equal_package_constants() -> None:
    mod = _load_script("run_m2_script", "run-m2.py")
    assert mod.REST_HOSTS == frozenset({TESTNET_HOST}) and mod.WS_HOSTS == frozenset({WS_API_HOST})

