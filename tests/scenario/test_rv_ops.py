"""RV-16·RV-17 운영 가시성·시작 점검 보완 (검토 기록 docs/changes/review-2026-10-03-pre-paper0.md).

- 알림: WARNING 이상은 콘솔(stderr)에도 내보내고, 모든 알림을 운영 로그(var/<mode>/logs/ops-YYYYMMDD.jsonl)에 남긴다.
- 같은 (등급·문구·참조) 알림은 억제 창(60초) 안에서 한 번만 기록하고, 창이 닫히면 억제 건수를 요약한다.
- 예외 traceback은 가려서 운영 로그에 남긴다.
- status: 포지션·미체결·당일 의도/체결 수·최근 운영 명령·최근 ALERT·하트비트·시계 점검·전략 상태.
- preflight: 본 프로세스 하트비트가 살아 있으면 거부. SNTP 1회 점검(오차 1초 초과는 실패).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_t015_preflight import (  # noqa: F401 — 픽스처 재사용
    CODE, ENV_OK, clock, make_root, sim)
from tests.support.kiwoom_sim import Sim
from trading_system.app.preflight import preflight_main
from trading_system.domain.clock import FixedClock
from trading_system.domain.enums import Mode
from trading_system.monitoring.sntp import SntpError
from trading_system.ops.cli import status_main
from trading_system.storage.alerts import AuditAlertSink
from trading_system.storage.oplog import log_exception
from trading_system.storage.repository import Repository

T0 = datetime(2026, 10, 5, 0, 31, tzinfo=UTC)  # 09:31 KST
ACCT_NO = "1234567890"


def alerts_of(repo: Repository) -> list[dict]:
    return [json.loads(e["summary"]) | {"ref": e["ref"]} for e in repo.audit_events() if e["kind"] == "ALERT"]


def ops_lines(log_dir: Path) -> list[dict]:
    return [json.loads(x) for p in sorted(log_dir.glob("ops-*.jsonl"))
            for x in p.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def env(tmp_path: Path):  # type: ignore[no-untyped-def]
    repo = Repository(tmp_path / "var" / "paper" / "trading.db", Mode.PAPER)
    yield repo, FixedClock(T0), tmp_path / "var" / "paper" / "logs"
    repo.close()


# --- (a) 콘솔·운영 로그 -------------------------------------------------------------------------

def test_warning_alert_goes_to_console_and_ops_log_masked(env, capsys: pytest.CaptureFixture[str]) -> None:
    repo, clk, logs = env
    sink = AuditAlertSink(repo, clk, fallback=logs / "alerts-fallback.log")
    sink.alert("EMERGENCY", f"정지 K2: 계좌 {ACCT_NO} 손실 한도", "halt-1")
    err = capsys.readouterr().err
    assert "2026-10-05 09:31:00 KST" in err and "EMERGENCY" in err and "K2" in err and "손실 한도" in err
    assert ACCT_NO not in err
    lines = ops_lines(logs)
    assert (logs / "ops-20261005.jsonl").exists()
    assert lines[-1]["kind"] == "ALERT" and lines[-1]["level"] == "EMERGENCY" and lines[-1]["halt_type"] == "K2"
    assert ACCT_NO not in json.dumps(lines, ensure_ascii=False)
    assert len(alerts_of(repo)) == 1  # DB 감사 기록은 그대로


def test_info_alert_not_on_console_but_in_ops_log(env, capsys: pytest.CaptureFixture[str]) -> None:
    repo, clk, logs = env
    AuditAlertSink(repo, clk, fallback=logs / "alerts-fallback.log").alert("INFO", "일일 보고", None)
    assert capsys.readouterr().err == ""
    assert ops_lines(logs)[-1]["level"] == "INFO"


def test_explicit_log_dir_without_fallback(env, tmp_path: Path) -> None:
    repo, clk, _ = env
    d = tmp_path / "x" / "logs"
    AuditAlertSink(repo, clk, log_dir=d).alert("WARNING", "대조 조회 실패", None)
    assert ops_lines(d)[-1]["message"] == "대조 조회 실패"


# --- (b) 중복 억제 ----------------------------------------------------------------------------

def test_identical_alerts_suppressed_within_window_then_summarized(env, capsys: pytest.CaptureFixture[str]) -> None:
    repo, clk, logs = env
    sink = AuditAlertSink(repo, clk, fallback=logs / "alerts-fallback.log")
    for _ in range(10):  # 0.5초마다 같은 알림
        sink.alert("WARNING", "예수금 조회 실패: 타임아웃", None)
        clk.advance(timedelta(seconds=0.5))
    assert len(alerts_of(repo)) == 1
    assert capsys.readouterr().err.count("예수금 조회 실패") == 1
    clk.advance(timedelta(seconds=60))
    sink.flush()
    rows = alerts_of(repo)
    assert len(rows) == 2
    assert rows[1]["level"] == "WARNING" and rows[1]["suppressed"] == 9 and "9건" in rows[1]["message"]
    sink.flush()  # 두 번 요약하지 않는다
    assert len(alerts_of(repo)) == 2


def test_summary_emitted_lazily_on_next_alert_after_window(env) -> None:
    repo, clk, logs = env
    sink = AuditAlertSink(repo, clk, fallback=logs / "alerts-fallback.log")
    sink.alert("WARNING", "같은 알림", None)
    sink.alert("WARNING", "같은 알림", None)
    clk.advance(timedelta(seconds=61))
    sink.alert("WARNING", "같은 알림", None)
    msgs = [r["message"] for r in alerts_of(repo)]
    assert len(msgs) == 3 and "1건" in msgs[1] and msgs[2] == "같은 알림"


def test_no_summary_when_nothing_suppressed(env) -> None:
    repo, clk, logs = env
    sink = AuditAlertSink(repo, clk, fallback=logs / "alerts-fallback.log")
    sink.alert("WARNING", "한 번", None)
    clk.advance(timedelta(seconds=61))
    sink.flush()
    assert len(alerts_of(repo)) == 1


def test_different_ref_or_level_not_suppressed(env) -> None:
    repo, clk, logs = env
    sink = AuditAlertSink(repo, clk, fallback=logs / "alerts-fallback.log")
    sink.alert("EMERGENCY", "결과 불명", "intent-1")
    sink.alert("EMERGENCY", "결과 불명", "intent-2")
    sink.alert("WARNING", "결과 불명", "intent-1")
    assert len(alerts_of(repo)) == 3


def test_storage_failure_still_falls_back_and_prints(env, capsys: pytest.CaptureFixture[str]) -> None:
    repo, clk, logs = env
    sink = AuditAlertSink(repo, clk, fallback=logs / "alerts-fallback.log")
    repo.fail_next_transaction = True
    sink.alert("EMERGENCY", "정지 K7: 저장 실패", None)
    assert "정지 K7" in (logs / "alerts-fallback.log").read_text(encoding="utf-8")
    assert "K7" in capsys.readouterr().err


# --- (c) 예외 traceback ------------------------------------------------------------------------

def test_log_exception_writes_masked_traceback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def boom() -> None:
        raise RuntimeError(f"계좌 {ACCT_NO} 처리 실패 Bearer abc.def")
    try:
        boom()
    except RuntimeError as exc:
        log_exception(tmp_path, "engine.step:reconcile", exc, at=T0)
    rec = ops_lines(tmp_path)[-1]
    assert rec["kind"] == "EXCEPTION" and rec["where"] == "engine.step:reconcile"
    assert rec["error"] == "RuntimeError" and "boom" in rec["traceback"]
    text = (tmp_path / "ops-20261005.jsonl").read_text(encoding="utf-8")
    assert ACCT_NO not in text and "abc.def" not in text
    err = capsys.readouterr().err
    assert "RuntimeError" in err and ACCT_NO not in err


def test_log_exception_never_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    log_exception(blocker / "logs", "w", ValueError("x"), at=T0)  # 디렉터리를 만들 수 없어도 예외 없음


# --- (d) status --------------------------------------------------------------------------------

def seed_status(root: Path, now: datetime) -> None:
    repo = Repository(root / "var" / "paper" / "trading.db", Mode.PAPER)
    try:
        with repo.tx():
            repo.set_position("KR-PAPER-01", "005930", Decimal("3"), Decimal("70100"))
            key = repo.insert_broker_order(account="KR-PAPER-01", order_no="0000123", at=now, intent_id=None,
                                           instrument="005930", side="SELL", quantity=Decimal("3"),
                                           origin="INTERNAL_NEW")
            repo.insert_fill(key, "F1", Decimal("1"), Decimal("70200"), now)
            for i in range(12):
                repo.insert_command(f"c{i}", "LOCAL", "me", "RESUME", {"halt_type": "K8"}, now)
                repo.finish_command(f"c{i}", "DONE" if i % 2 else "REJECTED", f"결과{i}", now)
            for i in range(12):
                repo.audit(now, "ALERT", "system", None, {"level": "WARNING", "message": f"알림{i}"})
            repo.insert_clock_check(now, "time.bora.net:123", Decimal("-0.076"), "OK")
            repo.upsert_strategy_state("paper0-verify", 1, "KRX", "PAPER", None, "BACKTEST", "h", now)
    finally:
        repo.close()
    hb = root / "var" / "paper" / "heartbeat"
    hb.write_text((now - timedelta(seconds=4)).isoformat(), encoding="utf-8")


def test_status_shows_operational_picture(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_status(tmp_path, T0)
    assert status_main(["--mode", "PAPER", "--root", str(tmp_path)], clock=FixedClock(T0)) == 0
    out = capsys.readouterr().out
    assert "005930 수량 3 평균가 70100" in out
    assert "미체결 주문: 1건" in out and "0000123" in out
    assert "당일 의도 0건" in out and "체결 1건" in out
    assert "결과11" in out and "결과2" in out and "결과0" not in out  # 최근 10건만
    assert "알림11" in out and "알림2" in out and "알림0" not in out
    assert "하트비트: 4초 전" in out and "살아 있음" in out
    assert "time.bora.net:123" in out and "-0.076" in out and "OK" in out
    assert "paper0-verify v1 KRX PAPER" in out


def test_status_heartbeat_stale_or_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_status(tmp_path, T0)
    status_main(["--mode", "PAPER", "--root", str(tmp_path)], clock=FixedClock(T0 + timedelta(minutes=5)))
    assert "멈춤" in capsys.readouterr().out
    (tmp_path / "var" / "paper" / "heartbeat").unlink()
    status_main(["--mode", "PAPER", "--root", str(tmp_path)], clock=FixedClock(T0))
    assert "하트비트: 없음" in capsys.readouterr().out


# --- RV-17 preflight ----------------------------------------------------------------------------

@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    made: list[str] = []
    monkeypatch.setattr("trading_system.app.preflight.KiwoomHttp", lambda *a, **k: made.append("http"))
    return made


def test_preflight_refuses_while_engine_heartbeat_fresh(tmp_path: Path, no_network: list[str],
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path, env_lines=ENV_OK, approved=True)
    hb = root / "var" / "paper" / "heartbeat"
    hb.parent.mkdir(parents=True)
    hb.write_text((T0 - timedelta(seconds=5)).isoformat(), encoding="utf-8")
    assert preflight_main(root, {"TRADE_MODE": "PAPER"}, clock=FixedClock(T0)) == 2
    out = capsys.readouterr().out
    assert "연결 없음" in out and "하트비트" in out
    assert no_network == []


def test_preflight_refuses_while_instance_lock_held(tmp_path: Path, no_network: list[str],
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    """하트비트가 아직 없어도(시작 직후) 실행 잠금이 잡혀 있으면 거부 (RV-15)."""
    from trading_system.app.instance_lock import acquire_instance_lock
    root = make_root(tmp_path, env_lines=ENV_OK, approved=True)
    lock = acquire_instance_lock(root / "var" / "paper")
    try:
        assert preflight_main(root, {"TRADE_MODE": "PAPER"}, clock=FixedClock(T0)) == 2
    finally:
        lock.release()
    assert "실행 중" in capsys.readouterr().out
    assert no_network == []


def _run(root: Path, sim: Sim, clock: FixedClock, query) -> int:  # type: ignore[no-untyped-def]
    return preflight_main(root, {"TRADE_MODE": "PAPER"}, rest_url=sim.url, ws_url=sim.ws_url, clock=clock,
                          probe_code=CODE, per_tr_interval=0.0, sntp_query=query)


def _report(root: Path) -> dict:
    return json.loads(next((root / "var" / "paper" / "preflight").glob("*.json")).read_text(encoding="utf-8"))


def test_preflight_old_heartbeat_does_not_block_and_sntp_ok(tmp_path: Path, sim: Sim, clock: FixedClock,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path, env_lines=ENV_OK, approved=True)
    hb = root / "var" / "paper" / "heartbeat"
    hb.parent.mkdir(parents=True)
    hb.write_text((clock.now() - timedelta(minutes=10)).isoformat(), encoding="utf-8")
    asked: list[str] = []

    def q(host: str, port: int) -> Decimal:
        asked.append(host)
        return Decimal("-0.076")
    assert _run(root, sim, clock, q) == 0
    out = capsys.readouterr().out
    assert asked == ["sntp.invalid"] and "-0.076" in out and "승인 전" not in out
    rep = _report(root)
    assert rep["clock_offset"] == "-0.076" and rep["ok"] is True


def test_preflight_sntp_offset_over_1s_fails(tmp_path: Path, sim: Sim, clock: FixedClock,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path, env_lines=ENV_OK, approved=True)
    assert _run(root, sim, clock, lambda h, p: Decimal("1.5")) == 1
    assert any("시계" in f for f in _report(root)["failures"])


def test_preflight_sntp_unreachable_is_warning(tmp_path: Path, sim: Sim, clock: FixedClock,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path, env_lines=ENV_OK, approved=True)

    def q(host: str, port: int) -> Decimal:
        raise SntpError(f"{host}:{port} 응답 없음: TimeoutError")
    assert _run(root, sim, clock, q) == 0
    rep = _report(root)
    assert any("시계" in w and "판정 불가" in w for w in rep["warnings"]) and rep["clock_offset"] is None


def test_preflight_does_not_resolve_reserved_invalid_host(tmp_path: Path, sim: Sim, clock: FixedClock,
                                                          monkeypatch: pytest.MonkeyPatch,
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    # 기본 질의(실제 소켓)에서도 .invalid(RFC 6761)는 조회하지 않는다 — 시험이 네트워크를 쓰지 않게
    called: list[str] = []
    monkeypatch.setattr("trading_system.app.preflight.query_offset", lambda *a, **k: called.append("x"))
    root = make_root(tmp_path, env_lines=ENV_OK, approved=True)
    assert preflight_main(root, {"TRADE_MODE": "PAPER"}, rest_url=sim.url, ws_url=sim.ws_url, clock=clock,
                          probe_code=CODE, per_tr_interval=0.0) == 0
    assert called == []



# --- RV-21 지원: report_time(16:00)에 "오늘(일일 기준 시각부터 지금까지)" 보고 -----------------------------

def test_daily_report_for_current_trade_day_at_report_time(env) -> None:
    from datetime import time

    from trading_system.domain.market import CostRates
    from trading_system.reporting.daily import DailyReporter, current_trade_day
    repo, clk, logs = env
    at_1600 = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)  # 16:00 KST
    assert str(current_trade_day(at_1600, time(8, 0))) == "2026-10-05"
    assert str(current_trade_day(datetime(2026, 10, 4, 22, 0, tzinfo=UTC), time(8, 0))) == "2026-10-04"  # 07:00 KST
    with repo.tx():
        key = repo.insert_broker_order(account="KR-PAPER-01", order_no="1", at=T0, intent_id=None,
                                       instrument="005930", side="BUY", quantity=Decimal("1"), origin="INTERNAL_NEW")
        repo.insert_fill(key, "F1", Decimal("1"), Decimal("70000"), T0)
    rep = DailyReporter(repo, AuditAlertSink(repo, clk), CostRates(Decimal("0"), Decimal("0")), lambda c: None)
    content = rep.generate(ACCOUNT_ALIAS, at_1600, time(8, 0), None, current_day=True)
    assert content is not None and content["trade_day"] == "2026-10-05" and content["fills_today"] == 1
    # 기본 동작(방금 끝난 거래일)은 그대로
    prev = rep.generate(ACCOUNT_ALIAS, at_1600, time(8, 0), None)
    assert prev is not None and prev["trade_day"] == "2026-10-04"


ACCOUNT_ALIAS = "KR-PAPER-01"
