"""T-M2-02 시장별 시간 — 게이트 L8 주문 창(UTC)·L5 일 경계(UTC 00:00)·K2 재개·설정 완화 적용 시각 (CP-024 D-108).

V-141: 주문 창(UTC 00:10~23:50) 밖 신규·증액 거부, 감액·청산 허용.
V-140(게이트·정지·설정 부분): UTC 00:00 경계로 L5 집계·K2 재개·완화 적용이 나뉘고 KST 자정과 무관.
M1 게이트·정지·설정의 KST 동작은 기존 시험(test_t003_gate·test_t005_halts·test_config_versioning)이 그대로 검증한다(V-152).
모든 값은 가상이다(tests/support/m2_fakes.py). 네트워크 없음.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.invariants import check_invariants
from tests.support.m2_fakes import ACCOUNT, M2Env, build_m2_env, m2_limits
from trading_system.config.versioning import ChangeSource, SettingsManager, SettingsOutcome
from trading_system.domain.enums import Check, GateResult, HaltType, Mode, OrderType, RequestSource, RequestType, Side
from trading_system.domain.market_time import M2_TIME
from trading_system.domain.models import GateRequest
from trading_system.halt.manager import Channel

STRATEGY = "tm202-fixed"


@pytest.fixture
def env(tmp_path: Path) -> Iterator[M2Env]:
    e = build_m2_env(tmp_path)
    yield e
    violations = check_invariants(e.repo.connection, Mode.BACKTEST.value)
    e.repo.close()
    assert violations == [], violations


def at(env: M2Env, hh: int, mm: int, ss: int = 0, day: int = 7) -> None:
    env.clock._now = datetime(2026, 10, day, hh, mm, ss, tzinfo=UTC)  # noqa: SLF001


def entry(env: M2Env, key: str, instrument: str = "TSTA-USDT", qty: str = "0.0100") -> GateRequest:
    price = env.market.prices[instrument] * Decimal("1.005")
    return GateRequest(source=RequestSource.SIGNAL, request_type=RequestType.ENTRY, account=ACCOUNT,
                       instrument=instrument, side=Side.BUY, quantity=Decimal(qty), order_type=OrderType.LIMIT,
                       price=price.quantize(Decimal("0.01")), idem_key=key, owner=STRATEGY,
                       valid_until=env.clock.now() + timedelta(minutes=5))


def exit_(env: M2Env, key: str, qty: str = "0.0100") -> GateRequest:
    return GateRequest(source=RequestSource.OPERATOR, request_type=RequestType.EXIT, account=ACCOUNT,
                       instrument="TSTA-USDT", side=Side.SELL, quantity=Decimal(qty), order_type=OrderType.MARKET,
                       price=env.market.prices["TSTA-USDT"], idem_key=key)


def run(env: M2Env, req: GateRequest):  # type: ignore[no-untyped-def]
    d = env.gate.decide(req)
    if d.approved is not None:
        env.executor.submit(d.approved)
        env.executor.process_events()
    return d


# --- V-141 주문 창 (UTC) ------------------------------------------------------------------------
@pytest.mark.parametrize(("hh", "mm", "ok"), [
    (0, 9, False), (0, 10, True), (12, 0, True), (15, 0, True),   # 15:00 UTC = KST 자정 — M2에는 경계 아님
    (23, 49, True), (23, 50, False), (23, 59, False),
])
def test_v141_order_window_is_utc(env: M2Env, hh: int, mm: int, ok: bool) -> None:
    at(env, hh, mm)
    d = env.gate.decide(entry(env, f"w-{hh}-{mm}"))
    assert (d.result is GateResult.APPROVED) is ok, d.reason
    if not ok:
        assert d.failed_check is Check.G12 and "L8" in (d.reason or "")


def test_v141_reduce_allowed_outside_window(env: M2Env) -> None:
    at(env, 1, 0)
    assert run(env, entry(env, "buy")).result is GateResult.APPROVED
    at(env, 23, 55)
    d = run(env, exit_(env, "sell", "0.0099"))  # 순 수령 0.00999 중 stepSize 0.0001 단위로 팔 수 있는 수량
    assert d.result is GateResult.APPROVED, d.reason
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == Decimal("0.00009")


def test_weekend_is_open_247(env: M2Env) -> None:
    at(env, 12, 0, day=10)  # 2026-10-10 토요일
    assert env.gate.decide(entry(env, "sat")).result is GateResult.APPROVED


# --- V-140 L5 일일 신규 주문 수 = UTC 날짜 기준 ---------------------------------------------------------
def test_v140_l5_counts_reset_at_utc_midnight_not_kst(tmp_path: Path) -> None:
    e = build_m2_env(tmp_path, limits=m2_limits(l5_daily_new_orders=2))
    try:
        at(e, 14, 0)
        assert run(e, entry(e, "a1")).result is GateResult.APPROVED
        at(e, 16, 0)   # KST 자정(15:00 UTC)을 넘었지만 같은 UTC 거래일
        assert run(e, entry(e, "a2", "TSTB-USDT", "10.0")).result is GateResult.APPROVED
        at(e, 23, 0)
        third = e.gate.decide(entry(e, "a3", "TSTC-USDT", "10.0"))
        assert third.result is GateResult.REJECTED and "L5" in (third.reason or "")
        at(e, 0, 10, day=8)  # UTC 00:00 경계 뒤 → 새 거래일
        assert e.gate.decide(entry(e, "a4", "TSTC-USDT", "10.0")).result is GateResult.APPROVED
    finally:
        e.repo.close()


# --- V-140 K2(일일 손실) 재개 = 다음 UTC 00:00 이후 ------------------------------------------------------
def test_v140_k2_resume_waits_for_next_utc_midnight(env: M2Env) -> None:
    at(env, 10, 0)
    env.halts.activate(HaltType.K2, "risk", "L4 도달 (가상)", ACCOUNT)
    at(env, 15, 30)   # KST 자정 다음 — M1이라면 "다음 일일 기준 시각"을 지난 것으로 볼 수 있는 시각
    r = env.halts.resume(HaltType.K2, ACCOUNT, Channel.LOCAL, "사람")
    assert not r.ok and "다음 일일 기준 시각 전" in r.reason
    at(env, 0, 1, day=8)
    r = env.halts.resume(HaltType.K2, ACCOUNT, Channel.LOCAL, "사람")
    assert "다음 일일 기준 시각 전" not in r.reason


# --- 설정 완화는 다음 UTC 00:00부터 (01 §2.3.1, A-064) ----------------------------------------------------
def test_settings_loosening_effective_next_utc_midnight(env: M2Env) -> None:
    mgr = SettingsManager(env.repo, env.clock, market_time=M2_TIME)
    at(env, 10, 0)
    assert mgr.submit_account_limits(ACCOUNT, m2_limits(), ChangeSource.LOCAL).outcome is SettingsOutcome.APPLIED
    looser = m2_limits(l5_daily_new_orders=20)
    res = mgr.submit_account_limits(ACCOUNT, looser, ChangeSource.LOCAL)
    assert res.outcome is SettingsOutcome.SCHEDULED
    assert res.effective_at == datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    tighter = m2_limits(l5_daily_new_orders=1, order_window_end=time(20, 0))
    assert mgr.submit_account_limits(ACCOUNT, tighter, ChangeSource.LOCAL).outcome is SettingsOutcome.APPLIED
