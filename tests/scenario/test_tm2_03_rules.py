"""T-M2-03 거래 규칙 사전 검사·IOC 매수 미체결분·시세 지연 중 IOC 지정가 매도 (CP-024 D-104·D-106·D-120).

V-133(일부: PRICE_FILTER·LOT_SIZE·MARKET_LOT_SIZE·NOTIONAL 위반 → 게이트 거부, 전송 0),
V-134(규칙 미적재 → 위험 확대 거부, 축소 허용), V-136(IOC 매수 부분 체결 + EXPIRED → 체결분만 포지션, 남은 예약 해제,
의도 종결, 재시도 없음), V-154(시세 지연 → 위험 확대 거부, 감액·청산은 IOC 지정가 매도(× 0.995, tick 올림), 시장가 매도 0,
미체결 시 재시도 없음), V-160(시세 지연 중 LOT_SIZE·지정가 금액 기준 부분).
모든 규칙·시세 값은 가상이다(tests/support/m2_fakes.py). 네트워크 없음.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.invariants import check_invariants
from tests.support.m2_fakes import ACCOUNT, M2_ORDER_POLICY, RULES, M2Env, build_m2_env
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import (
    Check, GateResult, IntentState, Mode, OrderType, RequestSource, RequestType, Side,
)
from trading_system.domain.market import Quote
from trading_system.domain.models import GateRequest
from trading_system.domain.trading_rules import apply_spot_terms

STRATEGY = "tm203-fixed"
D = Decimal


@pytest.fixture
def env(tmp_path: Path) -> Iterator[M2Env]:
    e = build_m2_env(tmp_path, ioc=True)
    yield e
    violations = check_invariants(e.repo.connection, Mode.BACKTEST.value)
    e.repo.close()
    assert violations == [], violations


def raw(kind: RequestType, qty: str, key: str, instrument: str = "TSTA-USDT",
        source: RequestSource = RequestSource.SIGNAL) -> GateRequest:
    """전략·운영 요청 원형 — 주문 유형·가격은 아직 정하지 않음(자리 값). apply_spot_terms가 정한다(D-104·D-120)."""
    side = Side.BUY if kind in (RequestType.ENTRY, RequestType.INCREASE) else Side.SELL
    return GateRequest(source=source, request_type=kind, account=ACCOUNT, instrument=instrument, side=side,
                       quantity=D(qty), order_type=OrderType.MARKET, price=D("1"), idem_key=key,
                       owner=STRATEGY if source is RequestSource.SIGNAL else None)


def terms(env: M2Env, req: GateRequest) -> GateRequest:
    out = apply_spot_terms(req, env.market.quote(req.instrument), env.clock.now(),
                           env.rules.rules(req.instrument), M2_ORDER_POLICY)
    assert out is not None
    return out


def run(env: M2Env, req: GateRequest):  # type: ignore[no-untyped-def]
    d = env.gate.decide(req)
    if d.approved is not None:
        env.executor.submit(d.approved)
        env.executor.process_events()
    return d


def buy_position(env: M2Env, qty: str = "0.0100") -> None:
    d = run(env, terms(env, raw(RequestType.ENTRY, qty, f"buy-{qty}")))
    assert d.result is GateResult.APPROVED, d.reason


# --- 매수 가격 규칙 (D-104) ------------------------------------------------------------------------
def test_buy_terms_ioc_limit_price_floor_and_reserve_at_order_price(env: M2Env) -> None:
    env.market.prices["TSTA-USDT"] = D("1234.57")
    req = terms(env, raw(RequestType.ENTRY, "0.0100", "b1"))
    assert (req.order_type, req.price) == (OrderType.LIMIT, D("1240.74"))   # 1240.74285 → 내림
    d = env.gate.decide(req)  # 판정까지만 — 예약 확인
    assert d.result is GateResult.APPROVED
    rsv = env.repo.reservation_for(d.approved.intent_id)
    assert rsv is not None and rsv.amount == D("0.0100") * D("1240.74")  # 예약 = 주문가 기준


# --- V-136 IOC 매수 부분 체결 + EXPIRED -------------------------------------------------------------
def test_v136_ioc_partial_fill_then_expired(env: M2Env) -> None:
    env.broker.ioc_liquidity["TSTA-USDT"] = D("0.0040")
    d = run(env, terms(env, raw(RequestType.ENTRY, "0.0100", "p1")))
    assert d.result is GateResult.APPROVED
    intent = env.repo.intent(d.approved.intent_id)
    assert intent.state is IntentState.EXPIRED and intent.filled_quantity == D("0.0040")
    # 체결분만 포지션 (기초 자산 수수료 0.1% 차감 = 순 수령, D-101)
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == D("0.0040") - D("0.0040") * D("0.001")
    assert env.repo.active_reservations(ACCOUNT) == []          # 남은 예약 해제
    assert len(env.broker.sent) == 1                            # 자동 재시도 없음
    own = env.repo.active_ownership(ACCOUNT, "TSTA-USDT")
    assert own is not None and own.state == "CONFIRMED"
    reasons = [e for e in env.repo.audit_events() if e["kind"] == "INTENT_STATE" and "IOC" in (e["summary"] or "")]
    assert reasons, "만료 사유가 IOC 미체결분 소멸로 기록되어야 한다"


def test_v136_ioc_zero_fill_expired_releases_reservation_and_ownership(env: M2Env) -> None:
    env.broker.ioc_liquidity["TSTA-USDT"] = D("0")
    d = run(env, terms(env, raw(RequestType.ENTRY, "0.0100", "z1")))
    intent = env.repo.intent(d.approved.intent_id)
    assert intent.state is IntentState.EXPIRED and intent.filled_quantity == 0
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == 0
    assert env.repo.active_reservations(ACCOUNT) == []
    assert env.repo.active_ownership(ACCOUNT, "TSTA-USDT") is None
    assert len(env.broker.sent) == 1


def test_ioc_full_liquidity_fills_all(env: M2Env) -> None:
    d = run(env, terms(env, raw(RequestType.ENTRY, "0.0100", "f1")))
    assert env.repo.intent(d.approved.intent_id).state is IntentState.FILLED


# --- V-133 필터 사전 검사 (가상 규칙값) -----------------------------------------------------------------
@pytest.mark.parametrize(("qty", "price", "key"), [
    ("0.00041", "10050.00", "LOT_SIZE"),
    ("0.0004", "10050.005", "PRICE_FILTER"),
    ("0.0004", "10050.00", "NOTIONAL"),   # 4.02 < 5
])
def test_v133_buy_filter_violation_rejected_nothing_sent(env: M2Env, qty: str, price: str, key: str) -> None:
    req = dataclasses.replace(raw(RequestType.ENTRY, qty, f"v-{qty}-{price}"), order_type=OrderType.LIMIT,
                              price=D(price))
    d = run(env, req)
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G10 and key in (d.reason or "")
    assert env.broker.sent == []


def test_v133_market_sell_market_lot_size_violation(env: M2Env) -> None:
    env.rules.table["TSTA-USDT"] = dataclasses.replace(env.rules.table["TSTA-USDT"], market_step=D("0.001"))
    buy_position(env, "0.0100")
    held = env.repo.position(ACCOUNT, "TSTA-USDT").quantity     # 0.009990 (순 수령)
    d = run(env, terms(env, raw(RequestType.EXIT, str(held), "x1")))  # MARKET_LOT_SIZE step 0.001에 안 맞음
    assert d.result is GateResult.REJECTED and "MARKET_LOT_SIZE" in (d.reason or "")
    assert len(env.broker.sent) == 1
    ok = run(env, terms(env, raw(RequestType.EXIT, "0.009", "x2")))
    assert ok.result is GateResult.APPROVED, ok.reason


def test_m2_modify_not_supported(env: M2Env) -> None:
    req = dataclasses.replace(raw(RequestType.MODIFY, "0.0100", "m1", source=RequestSource.EXEC_POLICY),
                              side=Side.BUY, order_type=OrderType.LIMIT, price=D("10050.00"),
                              target_order_key="none")
    d = env.gate.decide(req)
    assert d.result is GateResult.REJECTED
    assert (Check.G10, False) in d.checks   # 대상 주문과 무관하게 M2는 정정 미지원 (D-104)


# --- V-134 규칙 미적재 → 위험 확대 거부, 축소 허용 ----------------------------------------------------------
def test_v134_rules_missing_rejects_buy_allows_sell(env: M2Env) -> None:
    buy_position(env, "0.0100")
    env.rules.table.pop("TSTA-USDT")  # 규칙 미적재(적재 실패) 모사
    d = env.gate.decide(dataclasses.replace(raw(RequestType.INCREASE, "0.0100", "i1"),
                                            order_type=OrderType.LIMIT, price=D("10050.00")))
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G10 and "규칙" in (d.reason or "")
    sell = dataclasses.replace(raw(RequestType.EXIT, "0.0090", "s1"), price=D("10000.00"))
    assert run(env, sell).result is GateResult.APPROVED


def test_v134_rules_missing_allows_limit_sell_despite_m1_price_band(env: M2Env) -> None:
    """[M2 재통합 2026-10-09] 규칙 미적재 위험 축소는 M1 G10 경로로 넘어간다. T-027(CP-032 D-174) 상·하한가 검사가
    생겨도 M2 시세에는 상·하한가·기준가가 없으므로 IOC 지정가 매도는 그대로 허용된다 (V-134, M2 동작 불변)."""
    buy_position(env, "0.0100")
    env.rules.table.pop("TSTA-USDT")
    sell = dataclasses.replace(raw(RequestType.EXIT, "0.0090", "s-lim"), order_type=OrderType.LIMIT,
                               price=D("9950.00"))
    d = run(env, sell)
    assert d.result is GateResult.APPROVED, d.reason
    assert env.broker.sent[-1].order_type is OrderType.LIMIT


# --- V-154 시세 지연 중 감액·청산 = IOC 지정가 매도 ------------------------------------------------------------
def test_v154_stale_quote_market_sell_rejected_then_limit_ioc_next_request(env: M2Env) -> None:
    buy_position(env, "0.0100")
    sent_before = len(env.broker.sent)
    fresh_terms = terms(env, raw(RequestType.EXIT, "0.009", "e1"))
    assert fresh_terms.order_type is OrderType.MARKET
    env.market.quote_age["TSTA-USDT"] = timedelta(seconds=61)     # 의도 생성 뒤 시세 지연
    d = run(env, fresh_terms)
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G11   # 게이트는 의도를 바꾸지 않는다
    # 다음 판정에서 지정가 의도로 다시 만든다 (자동 재전송 아님 — 새 요청·새 멱등키)
    req = terms(env, raw(RequestType.EXIT, "0.009", "e2"))
    assert (req.order_type, req.price) == (OrderType.LIMIT, D("9950.00"))   # 10000.00 × 0.995, tick 올림
    env.broker.ioc_liquidity["TSTA-USDT"] = D("0")                  # 호가 없음 → 미체결 EXPIRED
    d2 = run(env, req)
    assert d2.result is GateResult.APPROVED, d2.reason
    assert env.repo.intent(d2.approved.intent_id).state is IntentState.EXPIRED
    new_sent = env.broker.sent[sent_before:]
    assert len(new_sent) == 1 and new_sent[0].order_type is OrderType.LIMIT   # 시장가 매도 전송 0, 재시도 없음
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == D("0.009990")


def test_v154_stale_quote_rejects_buy(env: M2Env) -> None:
    env.market.quote_age["TSTA-USDT"] = timedelta(seconds=61)
    d = run(env, terms(env, raw(RequestType.ENTRY, "0.0100", "sb")))
    assert d.result is GateResult.REJECTED and env.broker.sent == []


def test_v154_operator_reduce_stale_fills_partially(env: M2Env) -> None:
    buy_position(env, "0.0100")
    env.market.quote_age["TSTA-USDT"] = timedelta(seconds=120)
    env.broker.ioc_liquidity["TSTA-USDT"] = D("0.004")
    req = terms(env, raw(RequestType.REDUCE, "0.009", "op1", source=RequestSource.OPERATOR))
    d = run(env, req)
    assert d.result is GateResult.APPROVED
    assert env.repo.intent(d.approved.intent_id).state is IntentState.EXPIRED
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == D("0.009990") - D("0.004")


def test_v160_stale_limit_sell_uses_limit_notional(env: M2Env) -> None:
    """시세 지연 중에는 LOT_SIZE·주문가 기준 NOTIONAL로 검사 (확실한 위반만 거부)."""
    buy_position(env, "0.0100")
    env.market.quote_age["TSTA-USDT"] = timedelta(seconds=61)
    tiny = terms(env, raw(RequestType.EXIT, "0.0005", "t1"))       # 0.0005 × 9950 = 4.975 < 5
    d = run(env, tiny)
    assert d.result is GateResult.REJECTED and "NOTIONAL" in (d.reason or "")


def test_m1_rules_unchanged_when_no_spot_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """spot 규칙 공급자가 없으면(M1) G10은 M1 검사(수량 단위·호가 단위·상·하한가)만 하고 M2 거래 규칙은 보지 않는다 (V-152).

    [M2 재통합 2026-10-09, CP-032 D-174·A-085 Q-1] T-027 뒤 M1 경로의 위험 확대 지정가는 상·하한가를 알아야 승인된다.
    그래서 M1 시세처럼 상·하한가를 넣어 같은 의도(minNotional 미적용)를 확인하고, 상·하한가 모름 거부도 함께 고정한다.
    M2 실경로(spot 규칙 공급자 있음)는 이 검사 전에 판정을 끝낸다 — 동작 불변.
    """
    e = build_m2_env(tmp_path, spot_rules=False)
    try:
        req = dataclasses.replace(raw(RequestType.ENTRY, "0.0004", "m1"), order_type=OrderType.LIMIT,
                                  price=D("10050.00"))
        no_band = e.gate.decide(dataclasses.replace(req, idem_key="m1-no-band"))
        assert no_band.result is GateResult.REJECTED and no_band.failed_check is Check.G10
        assert "D-174" in (no_band.reason or "")      # 상·하한가 모름 → 위험 확대 지정가 거부 (T-027)
        quote = e.market.quote

        def banded(instrument: str) -> Quote | None:   # M1 시세처럼 상·하한가를 아는 경우 (가상 값)
            q = quote(instrument)
            return None if q is None else dataclasses.replace(q, upper_limit=D("13000.00"), lower_limit=D("7000.00"))

        monkeypatch.setattr(e.market, "quote", banded)
        assert e.gate.decide(req).result is GateResult.APPROVED   # minNotional은 보지 않는다
    finally:
        e.repo.close()


def test_rules_fixture_is_virtual() -> None:
    assert set(RULES) == {"TSTA-USDT", "TSTB-USDT", "TSTC-USDT"}
    assert FakeBehavior.FILL.value == "FILL"
