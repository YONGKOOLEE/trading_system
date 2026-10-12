"""CP-034(T-038~T-040) M1 PAPER 동작 불변 고정 (A-092: paper0 프로필 여유 추가·한도 값 변경은 사용자 지시 시).

실제 `config/limits.paper.toml`·`config/app.paper.toml`을 그대로 읽어 게이트 판정이 CP-034 구현 전과 같은지 본다.
- 한도 설정 해시(판정 기록 settings_hash, 시작 때 설정 버전 제출)가 구현 전 값과 같다 → 재시작해도 새 설정 버전이 생기지 않는다.
- paper0-verify v2 시나리오(1주 시장가 진입·청산, 손절가 없음 = D-020 경로)의 판정·예약·판정 기록 스냅샷 키가 같다.
- 손절가가 있는 신호도 이 파일(여유 키 없음)에서는 상한가 기준 L1 그대로다(D-195는 여유 키가 있을 때만).
기대값은 구현 전 코드(main 783ca75)에서 이 시험으로 확인해 고정했다. 시장 값(가격·기준 금액)은 가상이다.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, Env, build_env
from tests.support.invariants import check_invariants
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.config.limits_file import load_limits_file
from trading_system.domain.enums import Check, GateResult, Mode, OrderType, RequestType
from trading_system.domain.market import CashView, DailyRisk
from trading_system.domain.models import GateDecision, GateRequest, Signal

ROOT = Path(__file__).resolve().parents[2]
PAPER_LIMITS = ROOT / "config" / "limits.paper.toml"
PAPER_APP = ROOT / "config" / "app.paper.toml"
# 구현 전(main 783ca75) load_limits_file(config/limits.paper.toml).limits.version_hash()
PAPER_SETTINGS_HASH = "15f250562940edf6e42049c6a9323df2e4a390f32897d5346a0d231026d723e7"
M2_PAPER_SETTINGS_HASH = "92245d466ac51a8fac70ab326b33e4a546edb443698c39e47977e6a4511574e2"
# 구현 전 판정 기록(limit_snapshot)의 키 — 새 키가 PAPER 판정 기록에 끼어들지 않는다
SNAPSHOT_KEYS_ENTRY = {"base_amount", "cash_available", "cash_need", "exposure_add", "loss_ratio", "notional",
                       "order_risk", "orders_today", "symbol_after", "symbol_limit", "total_after", "total_limit"}
CODE = "005930"
AT = datetime(2026, 10, 1, 0, 30, tzinfo=UTC)   # KST 09:30 (paper0-verify v2 진입 판정 시각)
BASE = "100000000"                              # 가상 기준 금액 1억 원 (L1 0.5% = 500,000원)


@pytest.fixture
def penv(tmp_path: Path):  # type: ignore[no-untyped-def]
    e = build_env(tmp_path)
    e.clock._now = AT   # noqa: SLF001 — 시험 시계
    e.facts.limits.by_account[ACCOUNT] = load_limits_file(PAPER_LIMITS).limits
    e.facts.account.daily[ACCOUNT] = DailyRisk(Decimal(BASE), Decimal("0"), Decimal("0"), Decimal("0"))
    e.facts.account.cashes[ACCOUNT] = CashView(Decimal(BASE), Decimal(BASE))
    e.facts.market.add(CODE, "70500", upper="91600", lower="49400")   # 가상 시세 (상한가 = 기준가 × 1.3 근처)
    for c in ("000660", "035420", "051910"):
        e.facts.market.add(c, "70500", upper="91600", lower="49400")
    yield e
    violations = check_invariants(e.repo.connection, Mode.BACKTEST.value)
    e.repo.close()
    assert violations == [], violations


def _sig(kind: RequestType, qty: str, inst: str = CODE, ev: str = "ev", stop: str | None = None,
         strategy: str = "paper0-verify", version: int = 2) -> GateRequest:
    return GateRequest.from_signal(Signal(
        strategy_id=strategy, strategy_version=version, evaluation_id=ev, account=ACCOUNT, instrument=inst,
        rule_id="ENTRY-1" if kind is RequestType.ENTRY else "EXIT-1", kind=kind, quantity=Decimal(qty),
        order_type=OrderType.MARKET, price=Decimal("70500"), as_of=AT, valid_until=AT.replace(hour=6),
        stop_price=None if stop is None else Decimal(stop)))


def _snapshot(env: Env, d: GateDecision) -> dict[str, str]:
    row = env.repo.decision(d.decision_id)
    assert row is not None and row["settings_hash"] == PAPER_SETTINGS_HASH
    return json.loads(row["limit_snapshot"])


def test_real_paper_limits_hash_unchanged() -> None:
    assert load_limits_file(PAPER_LIMITS).limits.version_hash() == PAPER_SETTINGS_HASH
    assert load_limits_file(ROOT / "config" / "m2" / "limits.paper.toml", "M2").limits.version_hash() \
        == M2_PAPER_SETTINGS_HASH


def test_paper0_v2_entry_exit_decisions_unchanged(penv: Env) -> None:
    """09:30 1주 시장가 진입(손절가 없음 → L1 = 상한가 × 수량) 승인 → 체결 → 14:30 청산 승인."""
    d = penv.gate.decide(_sig(RequestType.ENTRY, "1"))
    assert d.result is GateResult.APPROVED, d.reason
    snap = _snapshot(penv, d)
    assert set(snap) == SNAPSHOT_KEYS_ENTRY
    assert (snap["notional"], snap["order_risk"], snap["exposure_add"], snap["cash_need"]) == \
        ("91600", "91600", "91600", "91600")
    rsv = penv.repo.active_reservations(ACCOUNT)
    assert [(r.instrument, r.amount, r.cash_remaining) for r in rsv] == [(CODE, Decimal("91600"), Decimal("91600"))]
    penv.executor.submit(d.approved)   # type: ignore[arg-type]
    penv.executor.process_events()
    assert penv.repo.position(ACCOUNT, CODE).quantity == Decimal("1")
    penv.clock._now = AT.replace(hour=5)   # noqa: SLF001 — KST 14:30
    x = penv.gate.decide(_sig(RequestType.EXIT, "1", ev="pm"))
    assert x.result is GateResult.APPROVED, x.reason


@pytest.mark.parametrize(("qty", "stop", "ok", "text"), [
    ("5", None, True, None),            # 458,000 ≤ 500,000 (주문 금액 전체, D-020)
    ("6", None, False, "L1"),           # 549,600 > 500,000
    ("100", "69000", False, "L1"),      # 손절가 있어도 여유 키 없음 → 상한가 기준 (91,600 − 69,000) × 100
    ("10", "70000", True, None),        # (91,600 − 70,000) × 10 = 216,000, 주문 금액 916,000 ≤ L7
    ("11", "70000", False, "L7"),       # L1 237,600 통과, 주문 금액 1,007,600 > L7 1,000,000 (상한가 기준)
    ("1", "70500", False, "손절가"),     # 손절가 ≥ 신호 가격 → G3 (여유 키 없음 → 기존 경계)
])
def test_real_paper_limits_l1_l7_paths_unchanged(penv: Env, qty: str, stop: str | None, ok: bool,
                                                 text: str | None) -> None:
    d = penv.gate.decide(_sig(RequestType.ENTRY, qty, stop=stop))
    if ok:
        assert d.result is GateResult.APPROVED, d.reason
    else:
        assert d.result is GateResult.REJECTED and text in (d.reason or ""), d.reason
        assert d.failed_check is (Check.G3 if text == "손절가" else Check.G7)


def test_real_paper_limits_l6_account_slots_unchanged(penv: Env) -> None:
    """계좌 L6 = 3 (A-034). 전략 슬롯 설정이 없으므로 같은 전략이 3종목까지, 4번째는 L6 거부."""
    penv.broker.behavior = FakeBehavior.ACCEPT_ONLY
    for i, c in enumerate((CODE, "000660", "035420")):
        d = penv.gate.decide(_sig(RequestType.ENTRY, "1", inst=c, ev=f"e{i}"))
        assert d.result is GateResult.APPROVED, d.reason
    d = penv.gate.decide(_sig(RequestType.ENTRY, "1", inst="051910", ev="e4"))
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G7 and "L6" in (d.reason or "")
