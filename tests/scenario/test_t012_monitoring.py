"""T-012 감시·보고 (05-plan T-012 + CP-018 추가, 04 V-61·V-72·V-110·V-111·V-114·V-116·V-117, 03 §8.4).

SNTP는 로컬 루프백의 가짜 서버로만 시험한다 (외부 연결 없음). 모든 값은 가상이다.
"""

from __future__ import annotations

import json
import socket
import struct
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, PRICES, T0, Env, make_signal
from tests.support.gate_fakes import scenario_limits
from trading_system.config.errors import ConfigError
from trading_system.config.market_file import parse_alert_settings
from trading_system.domain.enums import Check, GateResult, HaltType, OrderType, RequestType
from trading_system.domain.market import CostRates
from trading_system.domain.models import GateRequest, Signal
from trading_system.monitoring.liveness import LoopMonitor, watchdog_check, write_heartbeat
from trading_system.monitoring.sntp import (
    ClockMonitor, SntpError, _to_ntp, build_request, parse_response, query_offset,
)
from trading_system.monitoring.watchers import AlertThresholds, AlertWatcher, DailyRiskTracker
from trading_system.reporting.daily import DailyReporter
from trading_system.storage.alerts import AuditAlertSink

ROOT = Path(__file__).resolve().parents[2]


def alerts(env: Env, level: str | None = None) -> list[str]:
    out = []
    for e in env.repo.audit_events():
        if e["kind"] == "ALERT":
            s = json.loads(e["summary"])
            if level is None or s["level"] == level:
                out.append(s["message"])
    return out


# --- SNTP (D-053, V-72) ----------------------------------------------------------------------

def _response(request: bytes, server_time: datetime, stratum: int = 2) -> bytes:
    resp = bytearray(48)
    resp[0] = (0 << 6) | (4 << 3) | 4
    resp[1] = stratum
    resp[24:32] = request[40:48]  # originate = 클라이언트 송신 시각
    struct.pack_into("!II", resp, 32, *_to_ntp(server_time))
    struct.pack_into("!II", resp, 40, *_to_ntp(server_time))
    return bytes(resp)


def test_sntp_offset_parse() -> None:
    t1 = datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC)
    req = build_request(t1)
    resp = _response(req, t1 + timedelta(seconds=1, milliseconds=600))
    off = parse_response(resp, t1, t1 + timedelta(milliseconds=200))
    assert off == Decimal("1.500000")  # ((1.6) + (1.6 − 0.2)) / 2
    with pytest.raises(SntpError):
        parse_response(_response(build_request(t1 + timedelta(seconds=9)), t1), t1, t1)  # 다른 요청의 응답
    with pytest.raises(SntpError):
        parse_response(_response(req, t1, stratum=0), t1, t1)


def test_sntp_against_local_fake_server() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]

    def serve() -> None:
        data, addr = srv.recvfrom(512)
        srv.sendto(_response(data, datetime.now(UTC) + timedelta(seconds=3)), addr)

    t = threading.Thread(target=serve)
    t.start()
    off = query_offset("127.0.0.1", port, timeout=2)
    t.join()
    srv.close()
    assert Decimal("2.5") < off < Decimal("3.5")
    with pytest.raises(SntpError):
        query_offset("127.0.0.1", port, timeout=0.2)  # 서버 없음


@pytest.mark.parametrize(("offsets", "expected_k6"), [
    ([Decimal("0.2")], False), ([Decimal("1.5")], True), ([Decimal("-1.01")], True), ([None, None], True),
    ([None, Decimal("0.1")], False),  # 첫 서버 실패, 둘째 정상
])
def test_v72_clock_monitor(env: Env, offsets: list, expected_k6: bool) -> None:
    it = iter(offsets)

    def query(host: str, port: int) -> Decimal:
        v = next(it)
        if v is None:
            raise SntpError("가짜 실패")
        return v

    m = ClockMonitor(env.repo, env.clock, [("a", 1), ("b", 2)], env.halts, None, query)
    m.check()
    assert ("K6" in env.repo.active_halts(ACCOUNT)) is expected_k6
    assert m.clock_ok() is (False if expected_k6 and offsets[-1] is not None else (None if expected_k6 else True))
    rows = env.repo.select("SELECT result FROM clock_check")
    assert len(rows) == 1


def test_v72_k6_released_when_clock_recovers_and_stale_check_is_unknown(env: Env) -> None:
    vals = iter([Decimal("2"), Decimal("0.1")])
    m = ClockMonitor(env.repo, env.clock, [("a", 1)], env.halts, None, lambda h, p: next(vals))
    m.check()
    assert "K6" in env.repo.active_halts(ACCOUNT)
    m.check()
    assert "K6" not in env.repo.active_halts(ACCOUNT)
    env.clock.advance(timedelta(minutes=21))
    assert m.clock_ok() is None  # 점검이 오래되면 모름 → 게이트 G14 거부


# --- V-61 루프 지연·시계 점프, V-111 watchdog --------------------------------------------------

@pytest.mark.parametrize(("d_mono", "d_wall", "fires"), [
    (1.0, 1.0, False), (6.0, 6.0, True), (1.0, 120.0, True), (1.0, 3.5, True), (1.0, 2.5, False),
])
def test_v61_loop_delay_and_clock_jump_trigger_k8(env: Env, d_mono: float, d_wall: float, fires: bool) -> None:
    m = LoopMonitor(env.halts, AuditAlertSink(env.repo, env.clock))
    m.tick(100.0, T0)
    reason = m.tick(100.0 + d_mono, T0 + timedelta(seconds=d_wall))
    assert (reason is not None) is fires
    assert ("K8" in env.repo.active_halts(ACCOUNT)) is fires
    if fires:
        d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY)))
        assert d.failed_check is Check.G2


def test_v111_watchdog(tmp_path: Path) -> None:
    hb = tmp_path / "var" / "backtest" / "heartbeat"
    assert not watchdog_check(hb, T0).alive
    write_heartbeat(hb, T0)
    assert watchdog_check(hb, T0 + timedelta(seconds=20)).alive
    st = watchdog_check(hb, T0 + timedelta(seconds=31))
    assert not st.alive and "멈춤" in st.detail
    assert not watchdog_check(hb, T0 - timedelta(minutes=5)).alive
    write_heartbeat(hb, datetime.now(UTC))
    ok = subprocess.run([sys.executable, str(ROOT / "scripts" / "watchdog.py"), "--mode", "BACKTEST", "--once",
                         "--root", str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    write_heartbeat(hb, datetime.now(UTC) - timedelta(minutes=5))
    bad = subprocess.run([sys.executable, str(ROOT / "scripts" / "watchdog.py"), "--mode", "BACKTEST", "--once",
                          "--root", str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert bad.returncode == 1 and "이상" in bad.stdout


# --- V-116·V-117 경고 ----------------------------------------------------------------------------

def _watcher(env: Env, count: int = 3, minutes: int = 10) -> AlertWatcher:
    return AlertWatcher(env.repo, AuditAlertSink(env.repo, env.clock),
                        AlertThresholds(Decimal("0.80"), count, minutes), time(8, 0))


def _enter(env: Env, inst: str, qty: str, ev: str):  # type: ignore[no-untyped-def]
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, instrument=inst, quantity=qty,
                                                            evaluation=ev)))
    if d.approved:
        env.executor.submit(d.approved)
        env.executor.process_events()
    return d


def test_v116_limit_usage_warning_once_per_limit_per_day(env: Env) -> None:
    w = _watcher(env)
    _enter(env, "TST-A", "100", "a")       # L2 50%
    assert w.scan(ACCOUNT, env.clock.now()) == []
    _enter(env, "TST-A", "70", "b")        # 증액 아님(진입 중복) → 거부되므로 다른 종목으로
    _enter(env, "TST-B", "170", "c")       # L2 85% ≥ 80%
    fired = w.scan(ACCOUNT, env.clock.now())
    assert len(fired) == 1 and "L2" in fired[0]
    _enter(env, "TST-C", "180", "d")       # L2 90%: 같은 날 같은 한도 → 반복 없음
    assert all("L2" not in f for f in w.scan(ACCOUNT, env.clock.now()))
    assert any("L2" in m for m in alerts(env, "WARNING"))


def test_v117_reject_surge_warning(env: Env) -> None:
    w = _watcher(env, count=3, minutes=10)
    for i in range(2):
        _enter(env, "TST-A", "999", f"x{i}")  # 한도 초과 거부
    assert not any("거부 급증" in f for f in w.scan(ACCOUNT, env.clock.now()))
    _enter(env, "TST-A", "999", "x3")
    fired = w.scan(ACCOUNT, env.clock.now())
    assert any("거부 급증" in f and "G7" in f for f in fired)
    assert not any("거부 급증" in f for f in w.scan(ACCOUNT, env.clock.now()))  # 같은 창 반복 억제


def test_alert_settings_parse() -> None:
    assert parse_alert_settings({"alerts": {"reject_surge_count": 5, "reject_surge_minutes": 10}}) == (5, 10)
    for bad in ({}, {"alerts": {"reject_surge_count": 0, "reject_surge_minutes": 10}},
                {"alerts": {"reject_surge_count": 5}}, {"alerts": {"reject_surge_count": 5, "reject_surge_minutes": 1,
                                                                    "x": 1}}):
        with pytest.raises(ConfigError):
            parse_alert_settings(bad)


# --- 일일 손익 추적 (01 §2.4, L4 → K2) -----------------------------------------------------------

def test_daily_risk_tracker_feeds_gate_and_k2(env: Env) -> None:
    prices = dict(PRICES)
    tracker = DailyRiskTracker(env.repo, prices.get)
    _enter(env, "TST-A", "100", "a")
    tracker.start_day(ACCOUNT, Decimal("9000000"))       # 예수금 9,000,000 + 보유 1,000,000
    assert tracker.daily_risk(ACCOUNT).base_amount == Decimal("10000000")
    prices["TST-A"] = Decimal("7000")                     # 보유 평가 −300,000 (3%)
    risk = tracker.daily_risk(ACCOUNT)
    assert risk.loss_ratio == Decimal("0.03")
    assert env.halts.check_daily_loss(ACCOUNT, risk, Decimal("0.02")) is not None
    assert "K2" in env.repo.active_halts(ACCOUNT)
    del prices["TST-A"]
    assert tracker.daily_risk(ACCOUNT).evaluable is False


# --- V-110·V-114 일일 보고 -------------------------------------------------------------------------

def _sig(strategy: str, kind: RequestType, inst: str, qty: str, ev: str, price: str = "10000") -> GateRequest:
    return GateRequest.from_signal(Signal(strategy, 1, ev, ACCOUNT, inst, "R", kind, Decimal(qty), OrderType.MARKET,
                                          Decimal(price), T0 - timedelta(minutes=1), T0 + timedelta(minutes=30)))


def test_v114_v110_daily_report_by_strategy(env: Env) -> None:
    for req in (_sig("s-a", RequestType.ENTRY, "TST-A", "10", "1"), _sig("s-b", RequestType.ENTRY, "TST-B", "20", "2")):
        d = env.gate.decide(req)
        env.executor.submit(d.approved)
        env.executor.process_events()
    env.broker.behavior = env.broker.behavior
    d = env.gate.decide(_sig("s-a", RequestType.EXIT, "TST-A", "10", "3", price="11000"))
    env.executor.submit(d.approved)
    env.executor.process_events()
    _enter(env, "TST-C", "999", "rej")  # 거부 1건
    rep = DailyReporter(env.repo, AuditAlertSink(env.repo, env.clock),
                        CostRates(Decimal("0.001"), Decimal("0")), PRICES.get)
    content = rep.generate(ACCOUNT, env.clock.now() + timedelta(days=1), time(8, 0), "hash-x")
    assert content is not None
    by = content["by_strategy"]
    assert set(by) == {"s-a:1", "s-b:1"}
    # s-a: 매수 10×10,000 비용 100, 매도 10×11,000 비용 110 → 실현 = 10,000 − 110 − 100(매수 비용)
    assert Decimal(by["s-a:1"]["realized"]) == Decimal("9790")
    assert Decimal(by["s-b:1"]["realized"]) == Decimal("-200")
    assert content["rejects_by_check"] == {"G7": 1} and content["orders_approved"] == 3
    assert env.repo.daily_reports(ACCOUNT)[0]["status"] == "OK"
    assert content["settings_hash"] == "hash-x"


def test_v110_report_failure_is_recorded_and_alerted(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    import trading_system.reporting.daily as daily
    monkeypatch.setattr(daily, "build_report", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    rep = DailyReporter(env.repo, AuditAlertSink(env.repo, env.clock), CostRates(Decimal("0"), Decimal("0")),
                        PRICES.get)
    assert rep.generate(ACCOUNT, env.clock.now(), time(8, 0), None) is None
    assert env.repo.daily_reports(ACCOUNT)[0]["status"] == "FAILED"
    assert any("일일 보고 생성 실패" in m for m in alerts(env, "EMERGENCY"))
    assert replace and scenario_limits and GateResult and HaltType  # 표시용


def test_day3_watchdog_retries_transient_read_failure(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """3일차(2026-10-08 15:09) 허위 경보: run이 하트비트 파일을 교체하는 순간 읽으면 Windows가 PermissionError →
    한 번의 읽기 실패로 "읽을 수 없음" 긴급 알림. 쓰는 쪽(RV-06)처럼 읽는 쪽도 짧게 재시도한다."""
    hb = tmp_path / "heartbeat"
    write_heartbeat(hb, T0)
    real = Path.read_text
    fails = {"n": 2}

    def flaky(self: Path, *a, **k):  # type: ignore[no-untyped-def]
        if self == hb and fails["n"] > 0:
            fails["n"] -= 1
            raise PermissionError("교체 중")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky)
    assert watchdog_check(hb, T0 + timedelta(seconds=5), sleep=lambda s: None).alive
    fails["n"] = 99   # 계속 실패하면 여전히 이상으로 본다
    st = watchdog_check(hb, T0 + timedelta(seconds=5), sleep=lambda s: None)
    assert not st.alive and "읽을 수 없음" in st.detail
