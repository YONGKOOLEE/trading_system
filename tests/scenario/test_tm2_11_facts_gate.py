"""T-M2-11 공급자를 게이트 입력(MarketFacts·SymbolRulesSource)으로 연결한 판정 (CP-024 D-106·D-107·D-116).

V-134(규칙 미적재·적재 실패 → 위험 확대 거부, 위험 축소 허용), V-135(심볼 상태별 허용), V-154(시세 입력 지연 → 위험 확대
거부). 공급자는 합성 exchangeInfo·ticker 응답으로만 채운다(네트워크 없음). 심볼·계좌·값은 가상이다.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.contract.test_binance_rules import INFO, _symbol
from tests.support.gate_fakes import FakeLimits, FakeStrategies, FakeClockHealth
from tests.support.invariants import check_invariants
from tests.support.m2_fakes import ACCOUNT, M2_ORDER_POLICY, M2_POLICY, M2Env, build_m2_env, m2_limits
from trading_system.broker.binance.facts import BinanceMarketFacts
from trading_system.domain.enums import Check, GateResult, Mode, OrderType, RequestSource, RequestType, Side
from trading_system.domain.models import GateRequest
from trading_system.domain.trading_rules import apply_spot_terms
from trading_system.gate.gate import Gate
from trading_system.gate.ports import GateInputs

SYM = "TSTA-USDT"     # 가상 심볼 (fake 브로커 수수료 자산 표와 같은 이름)
D = Decimal


def _info(status: str = "TRADING") -> dict:
    info = copy.deepcopy(INFO)
    info["symbols"] = [_symbol(SYM, status)]
    return info


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[M2Env, BinanceMarketFacts]]:
    e = build_m2_env(tmp_path, ioc=True)
    f = BinanceMarketFacts(now=e.clock.now, universe=(SYM,), account=ACCOUNT)
    assert f.load_exchange_info(_info())
    assert f.update_ticker(SYM, {"symbol": SYM, "price": "10000.00"})
    inputs = GateInputs(FakeLimits({ACCOUNT: m2_limits()}), f, e.account, FakeStrategies(), FakeClockHealth(),
                        spot_rules=f)
    e.gate = Gate(e.repo, e.clock, inputs, M2_POLICY)    # 실행기는 게이트 승인 결과만 받는다 — 바꿀 것 없음
    yield e, f
    violations = check_invariants(e.repo.connection, Mode.BACKTEST.value)
    e.repo.close()
    assert violations == [], violations


def req(f: BinanceMarketFacts, e: M2Env, kind: RequestType, qty: str, key: str) -> GateRequest:
    side = Side.BUY if kind in (RequestType.ENTRY, RequestType.INCREASE) else Side.SELL
    raw = GateRequest(source=RequestSource.SIGNAL, request_type=kind, account=ACCOUNT, instrument=SYM, side=side,
                      quantity=D(qty), order_type=OrderType.MARKET, price=D("1"), idem_key=key, owner="tm211-fixed")
    out = apply_spot_terms(raw, f.quote(SYM), e.clock.now(), f.rules(SYM), M2_ORDER_POLICY)
    assert out is not None
    return out


def run(e: M2Env, r: GateRequest):  # type: ignore[no-untyped-def]
    d = e.gate.decide(r)
    if d.approved is not None:
        e.executor.submit(d.approved)
        e.executor.process_events()
    return d


def test_normal_buy_then_market_sell_with_provider(env: tuple[M2Env, BinanceMarketFacts]) -> None:
    e, f = env
    buy = req(f, e, RequestType.ENTRY, "0.01000", "b1")
    assert (buy.order_type, buy.price) == (OrderType.LIMIT, D("10050.00"))   # 10000 × 1.005, tick 0.01 내림
    assert run(e, buy).result is GateResult.APPROVED
    sell = req(f, e, RequestType.EXIT, "0.00900", "s1")
    assert sell.order_type is OrderType.MARKET
    d = run(e, sell)
    assert d.result is GateResult.APPROVED, d.reason


def test_v134_load_failure_rejects_buy_allows_sell(env: tuple[M2Env, BinanceMarketFacts]) -> None:
    e, f = env
    assert run(e, req(f, e, RequestType.ENTRY, "0.01000", "b1")).result is GateResult.APPROVED
    sell = req(f, e, RequestType.EXIT, "0.00900", "s1")
    inc = req(f, e, RequestType.INCREASE, "0.01000", "i1")
    assert f.load_exchange_info({"rateLimits": "x", "symbols": []}) is False     # 직전 적재 실패
    d = e.gate.decide(inc)
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G10 and "규칙" in (d.reason or "")
    d = run(e, sell)
    assert d.result is GateResult.APPROVED, d.reason


@pytest.mark.parametrize("status", ["CANCEL_ONLY", "HALT", "BREAK", "END_OF_DAY"])
def test_v135_non_trading_status_blocks_buy_and_sell(env: tuple[M2Env, BinanceMarketFacts], status: str) -> None:
    e, f = env
    assert run(e, req(f, e, RequestType.ENTRY, "0.01000", "b1")).result is GateResult.APPROVED
    inc = req(f, e, RequestType.INCREASE, "0.01000", "i1")
    sell = req(f, e, RequestType.EXIT, "0.00900", "s1")
    assert f.load_exchange_info(_info(status))
    for r in (inc, sell):
        d = e.gate.decide(r)
        assert d.result is GateResult.REJECTED and d.failed_check is Check.G13, (r.request_type, d.reason)
    sent = len(e.broker.sent)
    assert run(e, dataclasses.replace(sell, idem_key="s2")).result is GateResult.REJECTED
    assert len(e.broker.sent) == sent


def test_v154_quote_input_age_rejects_buy(env: tuple[M2Env, BinanceMarketFacts]) -> None:
    e, f = env
    buy = req(f, e, RequestType.ENTRY, "0.01000", "b1")
    e.clock.advance(timedelta(seconds=61))
    d = e.gate.decide(buy)
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G11
    assert f.update_ticker(SYM, {"symbol": SYM, "price": "10000.00"})   # 새 수신 → 신선
    assert e.gate.decide(dataclasses.replace(buy, idem_key="b2")).result is GateResult.APPROVED
