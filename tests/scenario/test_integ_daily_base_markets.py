"""통합 시험(백테스트·M2 병합) 적응: 일일 기준(DAILY_BASE) 감사 기록의 시장별 분리.

M1(monitoring.watchers·reporting, Repository.daily_base, 형식 domain.daily_base.DailyBase — market 필드 없음)과
M2(monitoring.spot_cash, 요약에 market="M2", 거래일 = UTC 날짜)가 같은 감사 종류 DAILY_BASE를 쓴다. 두 쪽은
ref(계좌 별칭)로 나뉘지만, 같은 별칭·같은 날짜 문자열이 겹쳐도 서로의 기준을 읽지 않아야 한다(V-152 M1 동작 불변).
모든 값은 가상이다.
"""

from __future__ import annotations

import json
from datetime import time, timedelta
from decimal import Decimal

from tests.scenario.conftest import ACCOUNT, Env
from trading_system.domain.clock import to_iso
from trading_system.domain.daily_base import DailyBase
from trading_system.domain.market import CostRates
from trading_system.domain.market_time import M2_TIME
from trading_system.monitoring.spot_cash import QuoteBalance, SpotAccountFacts
from trading_system.monitoring.watchers import DAILY_BASE, DailyRiskTracker

RATES = CostRates(Decimal("0.00015"), Decimal("0.002"))
M2_REF = time(0, 0)   # M2 일 경계 UTC 00:00 (A-055)


def _m2_row(env: Env, trade_date: str, cash: str, value: str = "0") -> None:
    now = env.clock.now()
    with env.repo.tx():
        env.repo.audit(now, DAILY_BASE, "monitor", ACCOUNT, {
            "market": "M2", "trade_date": trade_date, "cash": cash, "value": value, "at": now.isoformat()})


def test_m1_daily_base_ignores_m2_rows_same_account_same_date(env: Env) -> None:
    today = env.repo.trade_date_of(env.clock.now())
    _m2_row(env, today, "777")
    assert env.repo.daily_base(ACCOUNT, today) is None                 # M2 행은 M1 기준이 아님 (기록 없음)
    # 같은 날 M2 행이 형식 오류여도 M1 쪽 손상(DailyBaseCorrupt)으로 보지 않는다
    with env.repo.tx():
        env.repo.connection.execute(
            "INSERT INTO audit_event(mode, at, kind, actor, ref, summary) VALUES(?,?,?,?,?,?)",
            (env.repo.mode.value, to_iso(env.clock.now()), DAILY_BASE, "monitor", ACCOUNT,
             json.dumps({"market": "M2", "trade_date": today, "cash": "abc"})))
    assert env.repo.daily_base(ACCOUNT, today) is None
    tracker = DailyRiskTracker(env.repo, lambda i: Decimal("10000"), clock=env.clock, cost_rates=RATES)
    assert tracker.start_day(ACCOUNT, Decimal("10000000")) == Decimal("10000000")   # M1은 자기 기준을 새로 고정
    m1 = env.repo.daily_base(ACCOUNT, today)
    assert m1 is not None and m1.total == Decimal("10000000")
    _m2_row(env, today, "555")                                          # 뒤에 쓴 M2 행도 M1 기준을 가리지 않음
    assert env.repo.daily_base(ACCOUNT, today) == m1


def test_m1_daily_base_legacy_and_explicit_m1_rows_still_read(env: Env) -> None:
    now = env.clock.now()
    today = env.repo.trade_date_of(now)
    legacy = DailyBase(today, Decimal("1000"), Decimal("500"), now)
    with env.repo.tx():
        env.repo.audit(now, DAILY_BASE, "monitor", ACCOUNT, legacy.to_summary())
    assert env.repo.daily_base(ACCOUNT, today) == legacy                # market 없는 기존 형식 그대로
    with env.repo.tx():
        env.repo.audit(now, DAILY_BASE, "monitor", ACCOUNT, {**legacy.to_summary(), "market": "M1", "cash": "2000"})
    got = env.repo.daily_base(ACCOUNT, today)
    assert got is not None and got.cash == Decimal("2000")              # market="M1"은 M1 행


def test_m2_spot_facts_ignores_m1_rows_same_account(env: Env) -> None:
    now = env.clock.now()
    m2_today = M2_TIME.trade_date(now, M2_REF).isoformat()
    with env.repo.tx():   # M1 형식(market 없음) 행이 M2 거래일과 같은 날짜 문자열로 있어도
        env.repo.audit(now, DAILY_BASE, "monitor", ACCOUNT,
                       DailyBase(m2_today, Decimal("1"), Decimal("1"), now).to_summary())
    facts = SpotAccountFacts(env.repo, env.clock, quote_asset="USDT", asset_pair=lambda s: None,
                             price_of=lambda s: None, cost_rates=RATES, reference_time=M2_REF,
                             max_age=timedelta(minutes=3))
    facts.update_balance(ACCOUNT, QuoteBalance(Decimal("9000"), Decimal("0"), now))
    assert facts.start_day(ACCOUNT) == Decimal("9000")                  # M1 행을 M2 기준으로 쓰지 않음
    m2_rows = [json.loads(r["summary"]) for r in env.repo.select(
        "SELECT summary FROM audit_event WHERE kind=? AND ref=?", (DAILY_BASE, ACCOUNT))]
    assert [r.get("market") for r in m2_rows] == [None, "M2"]

