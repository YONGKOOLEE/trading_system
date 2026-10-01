"""설정 값 스키마 검증 (R-19, 01 §2.3.1-2·3).

- 누락·형식 오류·범위 밖·알 수 없는 키는 모두 오류다. 기본값은 없다.
- 비율·금액은 문자열 또는 정수로 받아 Decimal로 만든다. TOML float는 거부한다 (이진 부동소수 금지).
- 문제는 하나씩 던지지 않고 모아서 한 번에 보고한다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from typing import Literal

from .errors import ConfigError

Kind = Literal["decimal", "int", "time", "decimal_or_none", "str"]

_DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_ALIAS = re.compile(r"^[A-Z]{2}-[A-Z0-9]{2,12}-\d{2}$")
_LONG_DIGITS = re.compile(r"\d{8,}")


@dataclass(frozen=True)
class Field:
    name: str
    kind: Kind
    min: Decimal | int | None = None
    max: Decimal | int | None = None
    min_exclusive: bool = False


def _parse_decimal(name: str, raw: object) -> tuple[Decimal | None, str | None]:
    if isinstance(raw, bool) or isinstance(raw, float):
        return None, f"{name}: float·bool은 허용하지 않는다 (문자열 또는 정수로 쓴다)"
    if isinstance(raw, int):
        return Decimal(raw), None
    if isinstance(raw, str) and _DECIMAL.match(raw):
        return Decimal(raw), None
    return None, f"{name}: 십진수 형식이 아니다 (예: \"0.20\")"


def _check_range(f: Field, value: Decimal | int) -> str | None:
    if f.min is not None:
        if f.min_exclusive and value <= f.min:
            return f"{f.name}: {f.min}보다 커야 한다"
        if not f.min_exclusive and value < f.min:
            return f"{f.name}: {f.min} 이상이어야 한다"
    if f.max is not None and value > f.max:
        return f"{f.name}: {f.max} 이하여야 한다"
    return None


def parse_field(f: Field, raw: object) -> tuple[object, str | None]:
    if f.kind == "int":
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None, f"{f.name}: 정수여야 한다"
        err = _check_range(f, raw)
        return (None, err) if err else (raw, None)
    if f.kind == "decimal":
        value, err = _parse_decimal(f.name, raw)
        if err or value is None:
            return None, err
        err = _check_range(f, value)
        return (None, err) if err else (value, None)
    if f.kind == "decimal_or_none":
        if raw == "none":
            return None, None
        value, err = _parse_decimal(f.name, raw)
        if err or value is None:
            return None, f"{f.name}: 십진수 또는 \"none\"이어야 한다" if not isinstance(raw, (float, bool)) else err
        err = _check_range(f, value)
        return (None, err) if err else (value, None)
    if f.kind == "time":
        if not isinstance(raw, str) or not (m := _TIME.match(raw)):
            return None, f"{f.name}: \"HH:MM\"(24시간) 형식이어야 한다"
        return time(int(m.group(1)), int(m.group(2))), None
    if f.kind == "str":
        if not isinstance(raw, str) or not raw.strip():
            return None, f"{f.name}: 비어 있지 않은 문자열이어야 한다"
        return raw, None
    raise AssertionError(f"알 수 없는 kind {f.kind}")


def parse_fields(fields: Sequence[Field], raw: Mapping[str, object], where: str) -> tuple[dict[str, object], list[str]]:
    """섹션 하나를 검증한다. (값, 문제 목록)을 돌려준다."""
    values: dict[str, object] = {}
    problems: list[str] = []
    known = {f.name for f in fields}
    for key in raw:
        if key not in known:
            problems.append(f"{where}.{key}: 알 수 없는 키")
    for f in fields:
        if f.name not in raw:
            problems.append(f"{where}.{f.name}: 필수 값이 없다")
            continue
        value, err = parse_field(f, raw[f.name])
        if err:
            problems.append(f"{where}.{err}")
        else:
            values[f.name] = value
    return values, problems


def validate_params(fields: Sequence[Field], raw: Mapping[str, object], where: str = "params") -> dict[str, object]:
    """전략 파라미터 검증 (R-19, E-9). 문제가 있으면 ConfigError."""
    values, problems = parse_fields(fields, raw, where)
    if problems:
        raise ConfigError(problems)
    return values


def alias_problem(alias: object) -> str | None:
    """계좌 별칭 형식 검사. 실제 계좌번호를 별칭으로 쓰는 실수를 막는다 (CLAUDE.md §6)."""
    if not isinstance(alias, str) or not _ALIAS.match(alias):
        return "account: 별칭 형식이 아니다 (예: KR-PAPER-01, 실제 계좌번호 금지)"
    if _LONG_DIGITS.search(alias):
        return "account: 숫자가 길게 이어진 값은 별칭으로 쓸 수 없다 (계좌번호로 의심)"
    return None
