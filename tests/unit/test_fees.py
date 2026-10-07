"""domain/fees.position_delta (A-062③, CP-024 D-101). 자산 이름·값은 가상이다."""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_system.domain.fees import position_delta

PAIR = ("TSTA", "USDT")
D = Decimal


@pytest.mark.parametrize(("side", "qty", "fee", "asset", "pair", "expected"), [
    ("BUY", D("100"), None, None, None, D("100")),              # M1: 수수료 정보 없음 → 그대로
    ("SELL", D("100"), None, None, None, D("-100")),
    ("BUY", D("0.1000"), D("0.0001"), "TSTA", PAIR, D("0.0999")),  # 기초 자산 수수료 → 순 수령
    ("BUY", D("0.1000"), D("0.01"), "USDT", PAIR, D("0.1000")),    # 호가 자산 수수료 → 포지션 영향 없음
    ("SELL", D("0.0999"), D("0.999"), "USDT", PAIR, D("-0.0999")),
    ("BUY", D("0.1000"), D("0"), "TSTA", PAIR, D("0.1000")),
])
def test_supported(side: str, qty: Decimal, fee: Decimal | None, asset: str | None,
                   pair: tuple[str, str] | None, expected: Decimal) -> None:
    assert position_delta(side, qty, fee, asset, pair) == expected


@pytest.mark.parametrize(("side", "qty", "fee", "asset", "pair"), [
    ("BUY", D("0.1"), D("0.0001"), "TSTFEE", PAIR),   # 기초·호가 외 자산 → K4 대상 (V-130)
    ("SELL", D("0.1"), D("0.0001"), "TSTA", PAIR),    # 매도의 기초 자산 수수료: 설계 정의 없음 → 보수
    ("BUY", D("0.1"), D("0.1"), "TSTA", PAIR),        # 수수료 ≥ 체결 수량
    ("BUY", D("0.1"), D("-0.0001"), "TSTA", PAIR),    # 음수 수수료
    ("BUY", D("0.1"), D("0.0001"), "TSTA", None),     # 자산 쌍 모름
    ("BUY", D("0.1"), D("0.0001"), None, PAIR),       # 한쪽만 있음
    ("BUY", D("0.1"), None, "TSTA", PAIR),
])
def test_unsupported_returns_none(side: str, qty: Decimal, fee: Decimal | None, asset: str | None,
                                  pair: tuple[str, str] | None) -> None:
    assert position_delta(side, qty, fee, asset, pair) is None
