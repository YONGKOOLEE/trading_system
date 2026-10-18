"""CP-037 (A-113) T-051 독립 재검토 반영 시험 — 분봉 품질 실패가 비분봉 전략을 막지 않음(K10 대신 분봉 입력 무효, DP-72),
분봉 저장 실패 격리(계좌 전역 K8 아님, DP-76), 주기 검사(격자 시각이 주문 창 밖인 period_minutes 거부, BACKTEST 조회 없음
안내, DP-77), klines weight 상수 단일화(client.WEIGHT).

네트워크 없음: 합성 거래소·가짜 시계·임시 SQLite·실제 조립 함수 build_engine(market="M2")과 실제 HaltManager.
"""

from __future__ import annotations

import inspect
from datetime import time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_t045_m2_run import _m2_backtest_root
from tests.scenario.test_t051_m2_minute import (
    CAPS_MIN, GOOD, MINUTE_ON, SYM, WSYM, MinuteRig, Recorder, _market, _rows, utc,
)
from tests.support.m2_engine_world import ACCOUNT as W_ACCOUNT
from tests.support.m2_engine_world import FixedM2Strategy, M2World, fixed_spec
from trading_system.app import main as app_main
from trading_system.app.m2_minutes import (
    MINUTE_CAPABILITY, MINUTE_INPUT_AUDIT, MINUTE_QUALITY_AUDIT, M2MinuteInput,
)
from trading_system.app.main import run_main
from trading_system.app.wiring import StrategyInput
from trading_system.broker.binance import client as binance_client
from trading_system.broker.binance.feed import MinutePlan, minute_budget_problem
from trading_system.broker.binance.weights import parse_rate_limits
from trading_system.config.errors import ConfigError
from trading_system.config.m2_files import parse_m2_market
from trading_system.domain.minute_bar import MinuteBar
from trading_system.domain.minute_series import NO_ADJUSTMENT, PeriodGrid, boundaries_in_window
from trading_system.marketdata.spot_minute import SpotMinuteBook
from trading_system.storage.repository import StorageFailure

D = Decimal


@pytest.fixture
def rig(tmp_path: Path):  # type: ignore[no-untyped-def]
    r = MinuteRig(tmp_path)
    yield r
    r.close()


# --- 1. 분봉 품질 실패 → K10 없음, CAP-DAT-02 전략만 분봉 입력 무효 (DP-72) -----------------------------------------

def test_minute_book_has_no_halt_sink_and_main_does_not_wire_one() -> None:
    book = SpotMinuteBook(("BTCUSDT",))
    assert not hasattr(book, "halts") and "halts" not in inspect.signature(SpotMinuteBook).parameters
    assert "minute_book.halts" not in inspect.getsource(app_main)


def test_quality_failure_audited_and_alert_throttled_then_recovery(rig: MinuteRig) -> None:
    rig.at(utc(0, 25, 2))
    rig.sim.minute_body = _rows((utc(0, 29), "100", "99", "98", "100", "1"))      # 고저 모순
    for t in (utc(0, 30, 2), utc(0, 35, 2), utc(0, 40, 2)):                       # 30분 안 반복 → 경고 1건
        rig.at(t)
        res = rig.decide(t, f"q{t.minute}")[SYM]
        assert not res.ok and res.record["status"] == "QUALITY"
    warns = [(lv, m) for lv, m, _ in rig.alerts.items if "분봉 품질" in m]
    assert len(warns) == 1 and warns[0][0] == "WARNING"
    assert len(rig.audits(MINUTE_QUALITY_AUDIT)) == 3                              # 감사는 실패마다
    rig.sim.minute_body = None
    rig.at(utc(0, 45, 2))
    assert rig.decide(utc(0, 45, 2), "q-ok")[SYM].ok
    assert any(lv == "INFO" and "분봉 품질 회복" in m for lv, m, _ in rig.alerts.items)


def test_quality_failure_summary_after_30_minutes(rig: MinuteRig) -> None:
    rig.sim.minute_body = _rows((utc(0, 29), *GOOD), (utc(0, 29), *GOOD))         # 같은 시각 중복
    t = utc(1, 0, 2)
    for i in range(13):                                                            # 5분마다 1시간
        rig.at(t + timedelta(minutes=5 * i))
        rig.input.flush(rig.repo, rig.alerts, t + timedelta(minutes=5 * i))
    warns = [m for _, m, _ in rig.alerts.items if "분봉 품질" in m]
    assert 2 <= len(warns) <= 3 and any("계속" in m for m in warns[1:])


def _bars_0040_0100() -> list[MinuteBar]:
    return [MinuteBar(WSYM, 1, utc(0, 40) + timedelta(minutes=i), D(100), D(101), D(99), D(100), D(1), NO_ADJUSTMENT)
            for i in range(21)]                                   # 00:40~01:00 (01:00 진행 중)


def _two_strategy_world(tmp: Path, *, preload: bool = True) -> tuple[M2World, Recorder, SpotMinuteBook]:
    """실제 조립(build_engine M2)·실제 HaltManager: 분봉 미선언 고정 전략(m2-verify처럼 진입) + CAP-DAT-02 선언 기록 전략."""
    caps = ("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-ORD-02", "CAP-INS-X1", MINUTE_CAPABILITY)
    rec = Recorder(fixed_spec(strategy_id="t-m2min", capabilities=caps))
    book = SpotMinuteBook((WSYM,))
    w = M2World(tmp, strategies=[StrategyInput(FixedM2Strategy(), {"qty": "0.0100"}, (time(1, 0, 2),)),
                                 StrategyInput(rec, {"qty": "0.0100"}, (time(1, 0, 2),))],
                caps=CAPS_MIN, minutes=M2MinuteInput(book, lookback_bars=10))
    if preload:
        book.put_minute_bars(WSYM, _bars_0040_0100(), utc(1, 0, 2))
    w.set_baseline()
    return w, rec, book


def test_real_halt_manager_quality_failure_blocks_only_minute_strategy(tmp_path: Path) -> None:
    w, rec, book = _two_strategy_world(tmp_path)
    try:
        w.start_and_resume()
        bad = [MinuteBar(WSYM, 1, utc(0, 59), D(100), D(99), D(98), D(100), D(1), NO_ADJUSTMENT)]   # 고저 모순
        book.put_minute_bars(WSYM, bad, utc(1, 0, 2))
        w.tick_at(1, 0, 3)
        assert [r for r in w.repo.halt_rows(W_ACCOUNT) if r["halt_type"] == "K10"] == []     # 종목 정지 없음
        assert w.repo.active_halts(W_ACCOUNT) == []
        assert len(w.sent()) == 1                                    # 비분봉 전략(고정 진입)은 그대로 주문
        v = rec.snaps[0].view(WSYM)
        assert v is not None and not v.data_ok and v.minute_bars == ()                     # 분봉 전략만 무효
        [a] = w.audits(MINUTE_INPUT_AUDIT)
        assert "QUALITY" in a["summary"]
        assert w.audits(MINUTE_QUALITY_AUDIT)
        alerts = [e["summary"] for e in w.audits("ALERT")]
        assert any("분봉 품질" in s for s in alerts)
        assert not any("엔진 단계 오류" in s for s in alerts)
    finally:
        w.close()


# --- 2. 분봉 저장 실패 격리 — 계좌 전역 K8 아님, 대기 봉 재시도, CAP-DAT-02 입력 무효 (DP-76) --------------------------

def test_flush_storage_failure_keeps_bars_for_retry_and_alerts_throttled(rig: MinuteRig) -> None:
    rig.at(utc(0, 30, 2))
    rig.repo.fail_next_transaction = True
    assert rig.input.flush(rig.repo, rig.alerts, utc(0, 30, 2)) == (0, 0, 0)       # 예외를 엔진 단계로 올리지 않음
    assert rig.repo.minute_bars(SYM, 1, NO_ADJUSTMENT) == []
    rig.repo.fail_next_transaction = True
    assert rig.input.flush(rig.repo, rig.alerts, utc(0, 30, 3)) == (0, 0, 0)
    warns = [(lv, m) for lv, m, _ in rig.alerts.items if "분봉 저장 실패" in m]
    assert len(warns) == 1 and warns[0][0] == "WARNING"                             # 반복은 묶음
    rig.repo.fail_next_transaction = True                                           # 판정 직전 flush도 실패
    res = rig.decide(utc(0, 30, 4), "s1")[SYM]
    assert not res.ok and res.record["status"] == "STORE_FAILED"
    ok = rig.decide(utc(0, 30, 5), "s2")[SYM]                               # 재시도 성공 → 남겨 둔 봉 저장
    assert ok.ok and [b.at for b in ok.bars] == [utc(0, m) for m in range(20, 30)]
    assert any(lv == "INFO" and "분봉 저장 회복" in m for lv, m, _ in rig.alerts.items)


def test_book_requeue_keeps_order_and_cap() -> None:
    book = SpotMinuteBook((WSYM,), max_pending=5)
    bars = [MinuteBar(WSYM, 1, utc(0, m), D(1), D(1), D(1), D(1), D(1), NO_ADJUSTMENT) for m in range(6)]
    book.put_minute_bars(WSYM, bars[3:5], utc(1, 0))
    book.requeue(bars[:3])
    assert [b.start for b in book.drain()] == [utc(0, m) for m in range(5)]
    book.put_minute_bars(WSYM, bars[4:6], utc(1, 0))
    book.requeue(bars[:4])                                       # 6개 > 상한 5 → 가장 오래된 것부터 버림
    assert [b.start for b in book.drain()] == [utc(0, m) for m in (1, 2, 3, 4, 5)] and book.dropped == 1


def test_engine_minute_storage_failure_no_k8_and_other_strategy_trades(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    w, rec, book = _two_strategy_world(tmp_path, preload=False)
    try:
        w.start_and_resume()
        book.put_minute_bars(WSYM, _bars_0040_0100(), utc(1, 0, 2))   # 시작 뒤 받은 봉 — 저장 전

        def boom(*a, **kw):  # type: ignore[no-untyped-def]
            raise StorageFailure("주입된 분봉 저장 실패")
        monkeypatch.setattr(w.repo, "insert_minute_bars", boom)
        w.tick_at(1, 0, 3)
        assert "K8" not in w.repo.active_halts(W_ACCOUNT) and w.engine.errors == []
        assert len(w.sent()) == 1                                    # 비분봉 전략 주문은 그대로
        v = rec.snaps[0].view(WSYM)
        assert v is not None and not v.data_ok
        [a] = w.audits(MINUTE_INPUT_AUDIT)
        assert "STORE_FAILED" in a["summary"]
        alerts = [e["summary"] for e in w.audits("ALERT")]
        assert any("분봉 저장 실패" in s for s in alerts) and not any("엔진 단계 오류" in s for s in alerts)
        monkeypatch.undo()
        w.tick_at(1, 0, 10)
        assert w.repo.minute_bars(WSYM, 1, NO_ADJUSTMENT)          # 남겨 둔 봉이 다음 tick에 저장
    finally:
        w.close()


# --- 3. 주기 검사 — 격자 시각이 주문 창 밖이면 거부, BACKTEST는 조회 없음 안내 (DP-77) ------------------------------------

def test_period_with_no_boundary_inside_order_window_refused() -> None:
    assert boundaries_in_window(1440) == 0 and boundaries_in_window(720) == 1 and boundaries_in_window(5) > 0
    with pytest.raises(ValueError, match="주문 창"):
        PeriodGrid(1440)
    with pytest.raises(ValueError):
        MinutePlan(period_minutes=1440, lookback_bars=10)
    assert PeriodGrid(1440, window_start=time(0, 0)).latest_at_or_before(utc(0, 0, 3)) == utc(0, 0, 2)
    with pytest.raises(ConfigError) as exc:
        parse_m2_market(_market('[minute]\nenabled = true\nperiod_minutes = 1440\nlookback_bars = 60\n'
                                'approval = "A-999"\n'))
    assert any("주문 창" in p for p in exc.value.problems)
    assert parse_m2_market(_market('[minute]\nenabled = true\nperiod_minutes = 720\nlookback_bars = 60\n'
                                   'approval = "A-999"\n')).minute.period_minutes == 720


def test_backtest_minute_enabled_prints_no_fetch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _m2_backtest_root(tmp_path)
    p = root / "config" / "m2" / "market.backtest.toml"
    p.write_text(p.read_text(encoding="utf-8") + MINUTE_ON, encoding="utf-8")
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.3, market="M2") == 0
    out = capsys.readouterr().out
    assert "분봉 조회 켜짐" not in out and "BACKTEST" in out and "분봉 조회 없음" in out


# --- 4. klines weight 상수 단일화 -------------------------------------------------------------------------------------

def test_minute_budget_uses_client_klines_weight(monkeypatch: pytest.MonkeyPatch) -> None:
    limits = parse_rate_limits([{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1,
                                 "limit": 200}])                  # 내부 예산 100, 분봉 몫 10
    assert minute_budget_problem(limits, 5, MinutePlan(1, 60)) is None
    monkeypatch.setitem(binance_client.WEIGHT, "klines", 4)
    problem = minute_budget_problem(limits, 5, MinutePlan(1, 60))
    assert problem is not None and "weight 4" in problem
