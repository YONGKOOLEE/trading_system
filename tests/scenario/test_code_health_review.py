"""코드 정리(A-084) 재검토 M-1: 손상된 일일 기준(DAILY_BASE) 기록은 "기록 없음"과 구분해 모름으로 다룬다.

이전에는 손상 행을 "기록 없음"으로 보고 지금 현금 + 평가액으로 기준을 새로 고정·기록했다 — 장중 재시작이면 그날 앞선
손실이 L4 일일 손실 계산에서 사라지고, 새 행이 그대로 남는다. 손상이면: 새로 계산·기록하지 않고 None(모름 → 게이트가
위험 확대 거부), EMERGENCY 알림 1회(같은 날·같은 프로세스), 일일 요약은 비율 대신 "기준 기록 손상".
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, Env
from tests.scenario.test_t020_app import ACCOUNT as APP_ACCOUNT, DAY, World
from trading_system.domain.clock import KST, to_iso
from trading_system.domain.daily_base import DailyBase, DailyBaseCorrupt
from trading_system.domain.enums import Mode
from trading_system.domain.market import CostRates
from trading_system.monitoring.watchers import DAILY_BASE, DailyRiskTracker
from trading_system.storage.repository import Repository

CORRUPT_MSG = "일일 기준 기록 손상 — 사람 확인 필요"
RATES = CostRates(Decimal("0.00015"), Decimal("0.002"))


class _Sink:
    def __init__(self) -> None:
        self.got: list[tuple[str, str, str | None]] = []

    def alert(self, level: str, message: str, ref: str | None) -> None:
        self.got.append((level, message, ref))


def _raw_base(repo: Repository, account: str, at: datetime, summary_text: str) -> None:
    with repo.tx():
        repo.connection.execute(
            "INSERT INTO audit_event(mode, at, kind, actor, ref, summary) VALUES(?,?,?,?,?,?)",
            (repo.mode.value, to_iso(at), DAILY_BASE, "monitor", account, summary_text))


def _bad_fields(day: str, at: datetime) -> str:
    return json.dumps({"trade_date": day, "cash": "abc", "value": "0", "at": at.isoformat()})


def _base_rows(repo: Repository, account: str) -> int:
    return len(repo.select("SELECT seq FROM audit_event WHERE kind=? AND ref=?", (DAILY_BASE, account)))


def _today(env: Env) -> str:
    return env.repo.trade_date_of(env.clock.now())


# --- 저장소: 기록 없음(None) / 손상(DailyBaseCorrupt) / 정상 구분 ---------------------------------------------

@pytest.mark.parametrize("text", ["{not json", "[]", '{"cash": "1"}', None])
def test_m1_repository_distinguishes_missing_from_corrupt(env: Env, text: str | None) -> None:
    now = env.clock.now()
    today = _today(env)
    assert env.repo.daily_base(ACCOUNT, today) is None                          # 기록 없음
    _raw_base(env.repo, ACCOUNT, now, text if text is not None else _bad_fields(today, now))
    with pytest.raises(DailyBaseCorrupt):                                       # 오늘 손상 행 → 손상 (None 아님)
        env.repo.daily_base(ACCOUNT, today)
    tomorrow = (now + timedelta(days=1)).astimezone(KST).date().isoformat()
    assert env.repo.daily_base(ACCOUNT, tomorrow) is None                       # 다른 날의 손상 행은 오늘과 무관
    good = DailyBase(today, Decimal("1000"), Decimal("500"), now)
    with env.repo.tx():
        env.repo.audit(now, DAILY_BASE, "monitor", ACCOUNT, good.to_summary())
    assert env.repo.daily_base(ACCOUNT, today) == good                          # 손상 뒤 새 정상 행(사람 복구)은 읽는다


# --- 추적기: 손상이면 새로 계산·기록하지 않고 None, 알림 1회 -----------------------------------------------------

def test_m1_tracker_corrupt_today_row_is_unknown_no_new_row_alert_once(env: Env) -> None:
    now = env.clock.now()
    _raw_base(env.repo, ACCOUNT, now, _bad_fields(_today(env), now))
    sink = _Sink()
    tracker = DailyRiskTracker(env.repo, lambda i: Decimal("10000"), clock=env.clock, cost_rates=RATES, alerts=sink)
    rows = _base_rows(env.repo, ACCOUNT)
    assert tracker.start_day(ACCOUNT, Decimal("10000000")) is None
    env.clock.advance(timedelta(minutes=1))
    assert tracker.start_day(ACCOUNT, Decimal("10000000")) is None              # 재시도도 같은 판단
    assert _base_rows(env.repo, ACCOUNT) == rows                                # 새 DAILY_BASE 행 없음
    assert tracker.daily_risk(ACCOUNT) is None                                  # 모름 → 게이트 G7 위험 확대 거부
    assert [g for g in sink.got if g[1] == CORRUPT_MSG] == [("EMERGENCY", CORRUPT_MSG, ACCOUNT)]   # 정확히 1회
    assert len(sink.got) == 1


def test_m1_tracker_valid_row_still_reused_and_missing_row_still_created(env: Env) -> None:
    sink = _Sink()
    tracker = DailyRiskTracker(env.repo, lambda i: Decimal("10000"), clock=env.clock, cost_rates=RATES, alerts=sink)
    assert tracker.start_day(ACCOUNT, Decimal("10000000")) == Decimal("10000000")   # 기록 없음 → 새로 고정·기록
    assert _base_rows(env.repo, ACCOUNT) == 1
    restarted = DailyRiskTracker(env.repo, lambda i: Decimal("10000"), clock=env.clock, cost_rates=RATES, alerts=sink)
    assert restarted.start_day(ACCOUNT, Decimal("9000000")) == Decimal("10000000")  # 정상 기록 → 재사용
    assert _base_rows(env.repo, ACCOUNT) == 1
    assert sink.got == []


# --- 조립된 엔진: 손상 기록이면 위험 확대 주문이 게이트에서 거부되고 알림은 1회 ------------------------------------

def test_m1_wired_engine_corrupt_base_refuses_entry_and_alerts_once(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        now = w.clock.now()
        _raw_base(w.repo, APP_ACCOUNT, now, _bad_fields(DAY.isoformat(), now))
        rows = _base_rows(w.repo, APP_ACCOUNT)
        w.engine.start()
        w.resume_k8()
        w.at(9, 5)
        w.engine.tick()
        w.engine.tick()
        w.at(9, 7)
        w.engine.tick()
        assert w.intents() == []                                                # 진입 주문 없음
        decided = w.repo.select("SELECT request_type, result, failed_check, reason FROM gate_decision")
        assert decided, "전략 진입 신호가 게이트에 와야 한다"
        assert all(d["request_type"] == "ENTRY" and d["result"] == "REJECTED" and d["failed_check"] == "G7"
                   and "기준 금액 모름" in d["reason"] for d in decided), decided
        assert _base_rows(w.repo, APP_ACCOUNT) == rows                          # 새 기준 행 없음
        emergencies = [json.loads(e["summary"])["message"] for e in w.repo.audit_events() if e["kind"] == "ALERT"
                       and json.loads(e["summary"])["level"] == "EMERGENCY"]
        assert emergencies.count(CORRUPT_MSG) == 1, emergencies
    finally:
        w.close()


# --- 일일 요약·보고: 비율 대신 "기준 기록 손상" ------------------------------------------------------------------

def test_m1_day_summary_and_daily_report_show_corrupt_instead_of_ratio(env: Env, tmp_path: Path) -> None:
    from tests.scenario.test_rv_halts import _buy_fill
    from trading_system.reporting.daily import DailyReporter, realized_ratio_text
    from trading_system.reporting.day_summary import build_day_summary

    _buy_fill(env)
    now = env.clock.now()
    day = now.astimezone(KST).date()
    _raw_base(env.repo, ACCOUNT, now, _bad_fields(day.isoformat(), now))
    text = build_day_summary(tmp_path / "trading.db", Mode.BACKTEST, ACCOUNT, day, time(8, 0), RATES, [])
    pnl = [ln for ln in text.splitlines() if ln.startswith("실현 손익")]
    assert pnl and "기준 기록 손상" in pnl[0] and "%" not in pnl[0], text
    assert "집계 실패" not in text
    assert "기준 기록 손상" in realized_ratio_text(Decimal("-10"), None, corrupt=True)
    sink = _Sink()                                       # 일일 보고(엔진 안)도 예외 없이 손상 표시
    content = DailyReporter(env.repo, sink, RATES, lambda i: Decimal("10000")).generate(
        ACCOUNT, now + timedelta(days=1), time(8, 0), None)
    assert content is not None
    [(level, msg, _)] = [g for g in sink.got if g[1].startswith("일일 보고")]
    assert level == "INFO" and "기준 기록 손상" in msg and "%" not in msg
