"""바이낸스 소수 문자열 ↔ Decimal (CP-024 D-118, BN §6·B-U11).

- 응답의 가격·수량·금액은 문자열 고정소수로 받는다. 지수 표기·NaN·무한대·float·부호 '+'·공백은 해석 불가로 본다.
- 주문 문자열은 고정소수, stepSize·tickSize의 유효 자릿수로 쓴다(지수 표기 없음, B-U11 미확인 → 가장 보수적인 표기).
- `format_exact`는 값을 바꾸지 않는다: 자릿수(배수)에 맞지 않으면 PrecisionError. 반올림은 의도를 만드는 쪽의 일이다
  (매수 가격 내림 D-104, 시세 지연 매도 가격 올림 D-120, 수량 내림 D-118) — `to_step`을 쓴다.
- 오류 메시지에는 필드 이름만 넣고 값은 넣지 않는다(응답 원문을 로그·메시지에 남기지 않기 위함).
"""

from __future__ import annotations

import re
from decimal import ROUND_CEILING, Decimal, InvalidOperation

_PLAIN = re.compile(r"-?\d+(\.\d+)?", re.ASCII)


class BinanceParseError(ValueError):
    """응답 값을 해석할 수 없음. 호출자는 '응답 해석 불가'로 처리한다(주문이면 결과 불명, 조회면 조회 실패)."""


class PrecisionError(ValueError):
    """주문 값이 자릿수 규칙에 맞지 않음 → 전송 전 실패."""


def parse_decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, str) or not _PLAIN.fullmatch(value):
        raise BinanceParseError(f"{field}: 고정소수 문자열이 아님")
    try:
        return Decimal(value)
    except InvalidOperation:  # pragma: no cover - 정규식이 먼저 막음
        raise BinanceParseError(f"{field}: 숫자 변환 실패") from None


def parse_non_negative(value: object, field: str) -> Decimal:
    d = parse_decimal(value, field)
    if d < 0:
        raise BinanceParseError(f"{field}: 음수")
    return d


def parse_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BinanceParseError(f"{field}: 정수가 아님")
    return value


def step_places(step: Decimal) -> int:
    """stepSize·tickSize의 유효 소수 자릿수. "0.00100000" → 3, "1.00000000" → 0, "10" → 0."""
    if not step.is_finite() or step <= 0:
        raise ValueError("step은 0보다 큰 유한값이어야 한다")
    exp = step.normalize().as_tuple().exponent
    assert isinstance(exp, int)
    return max(0, -exp)


def plain(value: Decimal) -> str:
    """지수 표기 없는 고정소수 문자열. 후행 0은 정규화로 없앤다."""
    if not value.is_finite():
        raise PrecisionError("유한값이 아님")
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


def format_exact(value: Decimal, step: Decimal, field: str) -> str:
    """value가 step의 양의 배수일 때만 step 자릿수의 고정소수 문자열로 쓴다. 반올림하지 않는다."""
    if not value.is_finite() or value <= 0:
        raise PrecisionError(f"{field}: 0보다 큰 유한값이어야 한다")
    places = step_places(step)
    if value % step != 0:
        raise PrecisionError(f"{field}: 단위({plain(step)})의 배수가 아님")
    return format(value.quantize(Decimal(1).scaleb(-places)), "f")


def to_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    """value를 step 배수로 맞춘다(ROUND_DOWN = 내림, ROUND_UP = 올림 — 양수 기준)."""
    step_places(step)
    units = (value / step).to_integral_value(rounding=rounding)
    return (units * step).normalize()


def ceil_to_places(value: Decimal, places: int) -> Decimal:
    """비용 추정용 올림(quoteAssetPrecision 자릿수, D-118 보수)."""
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_CEILING)
