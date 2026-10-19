"""CP-039 T-062 (A-115 Q-3·Q-5, A-127) — G11-S 괴리·신선도, 주문 가격 기준, 가격 기록, 신호 기준 평균 매입가 (D-249~D-252).

V-320 G11-S(θ 경계·요청 유형·참조 시세 지연·테스트넷 지연), V-321 주문 가격·수량 기준(실행 출처), V-323 가격 기록(일부 —
paper-review 지표는 T-063), V-324 신호 기준 평균 매입가. 네트워크 없음 — 가상 시세·fake 브로커·임시 SQLite.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal

import pytest

from tests.scenario.conftest import Env
from tests.scenario.test_t003_gate import hold, op, rejected, sig
from tests.support.m2_engine_world import ACCOUNT, SYM, M2World, fixed_spec
from tests.support.m2_fakes import m2_limits
from trading_system.app.wiring import SignalInputs, StrategyInput
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import Check, GateResult, OrderType, RequestType, Side
from trading_system.domain.market import Quote
from trading_system.domain.models import Signal
from trading_system.domain.signal_source import divergence, signal_basis_avg_price
from trading_system.domain.strategy import EvaluationSnapshot
from trading_system.gate.gate import Gate
from trading_system.gate.ports import SignalCheck
from trading_system.marketdata.signal_feed import SignalFacts
from trading_system.strategy.base import make_signal

D = Decimal
THETA = D("0.01")


class Ref:
    """신호 출처 참조 시세 흉내 (SignalFacts와 같은 계약)."""

    def __init__(self) -> None:
        self.q: dict[str, Quote] = {}

    def reference_quote(self, instrument: str) -> Quote | None:
        return self.q.get(instrument)


def _signal_gate(env: Env, ref: Ref) -> Gate:
    from tests.support.gate_fakes import TEST_POLICY
    return Gate(env.repo, env.clock, replace(env.facts.inputs(), signal=SignalCheck(ref, THETA)), TEST_POLICY)


def _px(env: Env, price: str) -> None:
    env.facts.market.add("TST-A", price, upper=price, lower=str(D(price) * D("0.7")))


# --- 순수 계산 -------------------------------------------------------------------------------------------------

def test_divergence_and_signal_basis_avg() -> None:
    assert divergence(D("10100"), D("10000")) == D("0.01")
    assert divergence(D("9900"), D("10000")) == D("0.01")
    with pytest.raises(ValueError):
        divergence(D("1"), D("0"))
    # V-324: 0.001 @ P_s 100, 0.002 @ P_s 101 → (0.1 + 0.202) ÷ 0.003 (엔진 시험은 최소 주문 금액 5 USDT 때문에 수량 20배)
    avg = signal_basis_avg_price([("BUY", D("0.001"), D("100")), ("BUY", D("0.002"), D("101"))])
    assert avg is not None and avg.quantize(D("0.001")) == D("100.667")
    assert signal_basis_avg_price([("BUY", D("1"), D("100")), ("SELL", D("0.5"), D("120"))]) == D("100")
    assert signal_basis_avg_price([("BUY", D("1"), D("100")), ("SELL", D("1"), D("120")),
                                   ("BUY", D("1"), D("90"))]) == D("90")               # 새 보유는 새로 시작
    assert signal_basis_avg_price([("BUY", D("1"), None), ("BUY", D("1"), D("100"))]) is None   # 모름
    assert signal_basis_avg_price([]) is None


# --- V-320 G11-S 경계·요청 유형 ---------------------------------------------------------------------------------

@pytest.mark.parametrize(("px", "ok"), [("10099", True), ("10100", True), ("10101", False), ("9900", True),
                                        ("9899", False)])
@pytest.mark.parametrize("kind", [RequestType.ENTRY, RequestType.INCREASE])
def test_v320_theta_boundary_for_risk_increase(env: Env, px: str, ok: bool, kind: RequestType) -> None:
    ref = Ref()
    if kind is RequestType.INCREASE:
        hold(env, qty="10")
    _px(env, px)
    ref.q["TST-A"] = Quote("TST-A", D("10000"), env.clock.now())
    d = _signal_gate(env, ref).decide(sig(kind, qty="10", ev=f"x{px}{kind.value}", price=px))
    if ok:
        assert d.result is GateResult.APPROVED, d.reason
    else:
        assert rejected(d, Check.G11, "G11-S") and "괴리" in (d.reason or "")
    row = env.repo.select("SELECT limit_snapshot FROM gate_decision WHERE decision_id=?", (d.decision_id,))[0]
    snap = json.loads(row["limit_snapshot"])
    assert snap["signal_price"] == "10000" and snap["exec_price"] == px
    assert D(snap["divergence"]) == divergence(D(px), D("10000"))


@pytest.mark.parametrize("kind", [RequestType.REDUCE, RequestType.EXIT])
def test_v320_risk_reduction_ignores_divergence_and_reference_age(env: Env, kind: RequestType) -> None:
    hold(env, qty="20")
    ref = Ref()
    _px(env, "12000")                                           # d = 20% — 진입이면 거부
    ref.q["TST-A"] = Quote("TST-A", D("10000"), env.clock.now() - timedelta(hours=1))   # 참조 시세도 늙음
    d = _signal_gate(env, ref).decide(sig(kind, qty="10", ev=f"r{kind.value}", price="12000"))
    assert d.result is GateResult.APPROVED, d.reason
    assert dict(d.checks)[Check.G11] is True


def test_v320_cancel_and_sell_modify_not_checked_buy_modify_is_risk_increase(env: Env) -> None:
    """취소·매도 정정은 G11-S 없음. 매수 정정은 시스템 정의상 위험 확대(GateRequest.is_risk_increasing)라 G11-S 대상
    [임시 판단 — CP-039 V-320은 정정을 따로 적지 않았다. M2는 정정(amend)을 쓰지 않는다(D-104)]."""
    ref = Ref()
    _px(env, "12000")
    g = _signal_gate(env, ref)
    for kind, side in ((RequestType.CANCEL, Side.BUY), (RequestType.CANCEL, Side.SELL), (RequestType.MODIFY, Side.SELL)):
        d = g.decide(op(kind, key=f"k-{kind.value}-{side.value}", side=side, order_type=OrderType.LIMIT, price="12000"))
        assert dict(d.checks)[Check.G11] is True                # 다른 검사(대상 주문 없음)로 거부될 수는 있다
    d = g.decide(op(RequestType.MODIFY, key="k-mod-buy", side=Side.BUY, order_type=OrderType.LIMIT, price="12000"))
    assert dict(d.checks)[Check.G11] is False


@pytest.mark.parametrize(("age", "future", "ok"), [(59, False, True), (60, False, True), (61, False, False),
                                                    (0, True, False)])
def test_v320_reference_freshness_for_entry(env: Env, age: int, future: bool, ok: bool) -> None:
    ref = Ref()
    at = env.clock.now() + timedelta(seconds=1) if future else env.clock.now() - timedelta(seconds=age)
    ref.q["TST-A"] = Quote("TST-A", D("10000"), at)
    d = _signal_gate(env, ref).decide(sig(RequestType.ENTRY, qty="10", ev=f"a{age}{future}"))
    assert (d.result is GateResult.APPROVED) if ok else rejected(d, Check.G11, "참조 시세")


def test_v320_missing_reference_rejects_entry_not_exit(env: Env) -> None:
    hold(env, qty="10")
    g = _signal_gate(env, Ref())
    assert rejected(g.decide(sig(RequestType.ENTRY, "TST-B", qty="10", ev="m1")), Check.G11, "참조 시세")
    assert g.decide(sig(RequestType.EXIT, qty="10", ev="m2")).result is GateResult.APPROVED


def test_v320_without_signal_check_gate_unchanged(env: Env) -> None:
    """V-326: signal 입력이 없으면 G11은 종전 그대로 — 괴리가 커도 진입 승인, 스냅샷에 신호 항목 없음."""
    _px(env, "12000")
    d = env.gate.decide(sig(RequestType.ENTRY, qty="10", ev="plain", price="12000"))
    assert d.result is GateResult.APPROVED
    snap = env.repo.select("SELECT limit_snapshot FROM gate_decision WHERE decision_id=?", (d.decision_id,))[0]
    assert "signal_price" not in (snap["limit_snapshot"] or "")


# --- M2 엔진 경로: 진입 거부·청산 허용·재전송 없음·정지 없음, 주문 가격 = 실행 출처 (V-320·V-321) -------------------------

def _m2(tmp, ref: SignalFacts, *, price: str = "10000.00", qty: str = "0.0100", **kw):  # type: ignore[no-untyped-def]
    w = M2World(tmp, price=price, qty=qty, signal=SignalInputs(ref, THETA), **kw)
    w.set_baseline()
    w.start_and_resume()
    return w


def test_v320_engine_entry_rejected_by_divergence_no_resend_no_halt(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ref = SignalFacts((SYM,))
    w = _m2(tmp_path, ref)
    w.at(1, 0, 1)
    ref.update_reference(SYM, D("9800.00"), w.clock.now())          # d = 2.04%
    w.engine.tick()
    assert w.sent() == []
    dec = w.repo.select("SELECT result, failed_check, reason FROM gate_decision")
    assert [(r["result"], r["failed_check"]) for r in dec] == [("REJECTED", "G11")] and "G11-S" in dec[0]["reason"]
    w.tick_at(1, 5)
    assert w.sent() == [] and len(w.repo.select("SELECT 1 FROM gate_decision")) == 1   # 자동 재전송 없음
    assert set(w.repo.active_halts(ACCOUNT)) == set()                                      # 괴리 거부는 정지가 아니다
    w.close()


def test_v320_engine_exit_allowed_despite_divergence(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ref = SignalFacts((SYM,))
    w = _m2(tmp_path, ref)
    w.at(1, 0, 1)
    ref.update_reference(SYM, D("10000.00"), w.clock.now())
    w.engine.tick()
    assert len(w.sent()) == 1
    w.at(4, 0, 1)
    ref.update_reference(SYM, D("8000.00"), w.clock.now())          # d = 25%
    w.engine.tick()
    assert len(w.sent()) == 2 and w.sent()[-1].side == Side.SELL.value
    w.close()


def test_v321_order_price_from_execution_quote_not_signal(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ref = SignalFacts((SYM,))
    w = _m2(tmp_path, ref, price="100.80", qty="1.0000")
    w.at(1, 0, 1)
    ref.update_reference(SYM, D("100.00"), w.clock.now())           # d = 0.8% ≤ θ
    w.engine.tick()
    sent = w.sent()
    assert len(sent) == 1
    expect = (D("100.80") * D("1.005")).quantize(D("0.01"), rounding=ROUND_DOWN)   # 101.30, tickSize 내림
    assert sent[0].price == expect and sent[0].order_type == OrderType.LIMIT.value
    i = w.repo.select("SELECT price, quantity FROM intent")[0]
    assert D(i["price"]) == expect and D(i["quantity"]) == D("1.0000")
    w.close()


def test_v321_l7_uses_execution_price_gate_does_not_resize(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ref = SignalFacts((SYM,))
    w = _m2(tmp_path, ref, price="100.80", qty="2.0000", limits=m2_limits(l7_max_order_amount=D("200")))
    w.at(1, 0, 1)
    ref.update_reference(SYM, D("100.00"), w.clock.now())           # 2 × 100 = 200 ≤ L7 이지만 2 × 101.30 > L7
    w.engine.tick()
    dec = w.repo.select("SELECT result, failed_check, quantity FROM gate_decision")
    assert [(r["result"], r["failed_check"], D(r["quantity"])) for r in dec] == [("REJECTED", "G7", D("2.0000"))]
    assert w.sent() == []
    w.close()


# --- V-323 체결 시각 P_s 기록 / V-324 신호 기준 평균 매입가 ---------------------------------------------------------

class EnterThenWatch:
    """보유가 없으면 진입 qty, 있으면 판정 입력(보유 평균가)만 기록하고 신호 없음."""

    def __init__(self) -> None:
        self.spec = fixed_spec(strategy_id="t-sigavg")
        self.snaps: list[EvaluationSnapshot] = []

    def evaluate(self, snapshot: EvaluationSnapshot, params) -> list[Signal]:  # type: ignore[no-untyped-def]
        self.snaps.append(snapshot)
        v = snapshot.instruments[0]
        if v.position is None and v.last_price is not None and v.data_ok:
            return [make_signal(self.spec, snapshot, v.instrument, "ENTRY-1", RequestType.ENTRY, D(str(params["qty"])),
                                OrderType.MARKET, v.last_price, timedelta(minutes=30))]
        return []


def _avg_world(tmp, ref: SignalFacts | None):  # type: ignore[no-untyped-def]
    strat = EnterThenWatch()
    w = M2World(tmp, price="101.00", behavior=FakeBehavior.ACCEPT_ONLY,
                strategies=[StrategyInput(strat, {"qty": "0.0600"}, (time(1, 0), time(2, 0)))],
                signal=SignalInputs(ref, THETA) if ref is not None else None)
    w.broker.ioc_limit = False   # 체결을 시험이 나눠 넣는다(부분 체결 2건) — 주문 가격·유형 규칙은 다른 시험(V-321)
    w.set_baseline()
    w.start_and_resume()
    return w, strat


def _two_fills(w: M2World, ref: SignalFacts | None) -> None:
    w.at(1, 0, 1)
    if ref is not None:
        ref.update_reference(SYM, D("100.00"), w.clock.now())
    w.engine.tick()
    order_no = w.repo.select("SELECT order_no FROM broker_order")[0]["order_no"]
    w.broker.fill(order_no, D("0.0200"), D("101.00"))
    w.tick_at(1, 0, 2)
    if ref is not None:
        ref.update_reference(SYM, D("101.00"), w.clock.now())
    w.broker.fill(order_no, D("0.0400"), D("102.00"))
    w.tick_at(1, 0, 3)


def test_v323_v324_fill_time_reference_recorded_and_signal_basis_avg(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ref = SignalFacts((SYM,))
    w, strat = _avg_world(tmp_path, ref)
    _two_fills(w, ref)
    rows = w.repo.fill_signal_prices(ACCOUNT, SYM)
    assert [(r["side"], D(r["quantity"]), D(r["fill_price"]), D(r["ref_price"])) for r in rows] == [
        ("BUY", D("0.0200"), D("101.00"), D("100.00")), ("BUY", D("0.0400"), D("102.00"), D("101.00"))]
    assert all(r["ref_at"] for r in rows)
    w.at(2, 0, 1)
    ref.update_reference(SYM, D("101.00"), w.clock.now())
    w.engine.tick()
    pos = strat.snaps[-1].instruments[0].position
    assert pos is not None and pos.avg_price.quantize(D("0.001")) == D("100.667")      # 신호 기준 (Q-5 (가))
    ledger = w.repo.position(ACCOUNT, SYM).avg_price
    assert ledger > D("101")                                                            # 원장은 테스트넷 체결가
    w.close()


def test_v324_testnet_mode_avg_is_ledger_and_no_signal_rows(tmp_path) -> None:  # type: ignore[no-untyped-def]
    w, strat = _avg_world(tmp_path, None)
    _two_fills(w, None)
    assert w.repo.fill_signal_prices(ACCOUNT, SYM) == []
    w.tick_at(2, 0, 1)
    pos = strat.snaps[-1].instruments[0].position
    assert pos is not None and pos.avg_price == w.repo.position(ACCOUNT, SYM).avg_price
    w.close()


def test_v324_missing_fill_reference_falls_back_to_ledger_with_warning(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ref = SignalFacts((SYM,))
    w, strat = _avg_world(tmp_path, ref)
    w.at(1, 0, 1)
    ref.update_reference(SYM, D("100.00"), w.clock.now())
    w.engine.tick()
    ref._q.clear()  # noqa: SLF001 — 체결 시각에 참조 시세 없음
    w.broker.fill(w.repo.select("SELECT order_no FROM broker_order")[0]["order_no"], D("0.0600"), D("101.00"))
    w.tick_at(1, 0, 2)
    assert [r["ref_price"] for r in w.repo.fill_signal_prices(ACCOUNT, SYM)] == [None]
    w.at(2, 0, 1)
    ref.update_reference(SYM, D("101.00"), w.clock.now())
    w.engine.tick()
    pos = strat.snaps[-1].instruments[0].position
    assert pos is not None and pos.avg_price == w.repo.position(ACCOUNT, SYM).avg_price
    assert any("신호 기준 평균 매입가" in json.loads(a["summary"]).get("message", "") or "신호 기준 평균 매입가" in a["summary"]
               for a in w.repo.audit_events() if a["kind"] == "ALERT")
    w.close()



def test_d250_day_summary_bundles_g11s_rejections(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from trading_system.reporting.m2_day_summary import _signal
    ref = SignalFacts((SYM,))
    w = _m2(tmp_path, ref)
    w.at(1, 0, 1)
    ref.update_reference(SYM, D("9800.00"), w.clock.now())
    w.engine.tick()
    day = w.clock.now().date()
    assert _signal(w.repo, ACCOUNT, day) == ["신호 출처 괴리·지연 진입 거부(G11-S) 1건 — 정지 아님, 청산 영향 없음"]
    assert _signal(w.repo, ACCOUNT, day - timedelta(days=1)) == []      # 없는 날은 줄 없음(기존 요약 문구 불변)
    w.close()
