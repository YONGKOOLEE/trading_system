"""바이낸스 소수 문자열 처리 (CP-024 D-118, V-156, BN §6·B-U11).

- 응답의 소수는 문자열로 받아 Decimal로 엄격 해석한다(지수 표기·NaN·float 거부).
- 주문 문자열은 고정소수, stepSize·tickSize 자릿수로 쓴다. 지수 표기를 만들지 않는다.
- 어댑터는 게이트가 승인한 값을 바꾸지 않는다: 자릿수에 맞지 않으면 오류(반올림은 의도 생성 단계의 일).
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal

import pytest

from trading_system.broker.binance import numbers as n


@pytest.mark.parametrize(("raw", "expected"), [
    ("0.00100000", Decimal("0.001")),
    ("6000", Decimal("6000")),
    ("-1.5", Decimal("-1.5")),
    ("0", Decimal("0")),
])
def test_parse_decimal_accepts_plain_strings(raw: str, expected: Decimal) -> None:
    assert n.parse_decimal(raw, "f") == expected


@pytest.mark.parametrize("raw", ["1e-8", "1E3", "NaN", "Infinity", "", " 1", "1.", ".5", "+1", "１", 1.5, None, True])
def test_parse_decimal_rejects_non_plain(raw: object) -> None:
    with pytest.raises(n.BinanceParseError):
        n.parse_decimal(raw, "f")


def test_parse_decimal_error_message_has_field_not_value() -> None:
    with pytest.raises(n.BinanceParseError) as e:
        n.parse_decimal("1e-8", "price")
    assert "price" in str(e.value)
    assert "1e-8" not in str(e.value)


def test_parse_non_negative() -> None:
    assert n.parse_non_negative("0.5", "q") == Decimal("0.5")
    with pytest.raises(n.BinanceParseError):
        n.parse_non_negative("-0.1", "q")


@pytest.mark.parametrize(("step", "places"), [
    ("0.00100000", 3), ("1.00000000", 0), ("0.01", 2), ("10", 0), ("0.00000001", 8),
])
def test_step_places(step: str, places: int) -> None:
    assert n.step_places(Decimal(step)) == places


def test_step_places_rejects_zero_or_negative() -> None:
    with pytest.raises(ValueError):
        n.step_places(Decimal("0"))
    with pytest.raises(ValueError):
        n.step_places(Decimal("-0.1"))


@pytest.mark.parametrize(("value", "step", "expected"), [
    (Decimal("0.123"), Decimal("0.00100000"), "0.123"),
    (Decimal("0.1230000"), Decimal("0.00100000"), "0.123"),
    (Decimal("5"), Decimal("1.00000000"), "5"),
    (Decimal("0.00000001"), Decimal("0.00000001"), "0.00000001"),   # 지수 표기(1E-8) 아님
    (Decimal("1E-8"), Decimal("0.00000001"), "0.00000001"),
    (Decimal("123456789.12"), Decimal("0.01"), "123456789.12"),
    (Decimal("30"), Decimal("10"), "30"),
])
def test_format_exact_plain_fixed_point(value: Decimal, step: Decimal, expected: str) -> None:
    out = n.format_exact(value, step, "quantity")
    assert out == expected
    assert "E" not in out and "e" not in out


@pytest.mark.parametrize(("value", "step"), [
    (Decimal("0.1234"), Decimal("0.001")),
    (Decimal("35"), Decimal("10")),
    (Decimal("-1"), Decimal("1")),
    (Decimal("NaN"), Decimal("1")),
])
def test_format_exact_refuses_to_round(value: Decimal, step: Decimal) -> None:
    with pytest.raises(n.PrecisionError):
        n.format_exact(value, step, "quantity")


def test_to_step_floor_and_ceil() -> None:
    assert n.to_step(Decimal("0.12345"), Decimal("0.001"), ROUND_DOWN) == Decimal("0.123")
    assert n.to_step(Decimal("0.12301"), Decimal("0.001"), ROUND_UP) == Decimal("0.124")
    assert n.to_step(Decimal("0.123"), Decimal("0.001"), ROUND_UP) == Decimal("0.123")
    assert n.to_step(Decimal("37"), Decimal("10"), ROUND_DOWN) == Decimal("30")


def test_ceil_to_places_for_cost_estimate() -> None:
    # 비용 추정은 quoteAssetPrecision 자릿수 올림 (D-118)
    assert n.ceil_to_places(Decimal("1.234561"), 5) == Decimal("1.23457")
    assert n.ceil_to_places(Decimal("1.23456"), 5) == Decimal("1.23456")


def test_plain_never_uses_exponent() -> None:
    assert n.plain(Decimal("1E-10")) == "0.0000000001"
    assert n.plain(Decimal("1E+3")) == "1000"
    with pytest.raises(n.PrecisionError):
        n.plain(Decimal("Infinity"))
