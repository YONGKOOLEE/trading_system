"""전략 파라미터 값 검증·로드 (R-19, E-9, 04 V-86). 값은 `config/strategies/<전략ID>-v<버전>.toml`의 [params].

- 명세(ParamSpec)에 없는 키, 누락, 형식 오류, 범위 밖이면 거부하고 문제를 한 번에 보고한다.
- 소수는 문자열로 적는다(부동소수 금지). 기본값은 없다.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path

from trading_system.domain.strategy import ParamSpec, StrategySpec

from .errors import ConfigError

PARAM_TYPES = frozenset({"decimal", "int", "str", "bool"})


def _parse_one(p: ParamSpec, raw: object) -> tuple[object | None, str | None]:
    if p.type == "decimal":
        if isinstance(raw, bool) or not isinstance(raw, (str, int, Decimal)):
            return None, f"{p.name}: 소수는 문자열로 적어야 함 (부동소수 금지)"
        try:
            v: object = Decimal(str(raw))
        except InvalidOperation:
            return None, f"{p.name}: 소수 형식이 아님"
        if not v.is_finite():  # type: ignore[attr-defined]
            return None, f"{p.name}: 유한한 값이어야 함"
    elif p.type == "int":
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None, f"{p.name}: 정수여야 함"
        v = raw
    elif p.type == "bool":
        if not isinstance(raw, bool):
            return None, f"{p.name}: true/false여야 함"
        return raw, None
    elif p.type == "str":
        if not isinstance(raw, str) or not raw:
            return None, f"{p.name}: 빈 문자열이 아닌 문자열이어야 함"
        if p.choices and raw not in p.choices:
            return None, f"{p.name}: 허용 값이 아님"
        return raw, None
    else:
        return None, f"{p.name}: 알 수 없는 타입 {p.type}"
    num = Decimal(str(v))
    if p.minimum is not None and num < p.minimum:
        return None, f"{p.name}: 최소값({p.minimum}) 미만"
    if p.maximum is not None and num > p.maximum:
        return None, f"{p.name}: 최대값({p.maximum}) 초과"
    return v, None


def validate_params(spec: StrategySpec, raw: Mapping[str, object]) -> dict[str, object]:
    problems: list[str] = []
    known = {p.name for p in spec.params}
    for key in raw:
        if key not in known:
            problems.append(f"{key}: 명세에 없는 파라미터")
    values: dict[str, object] = {}
    for p in spec.params:
        if p.name not in raw:
            problems.append(f"{p.name}: 필수 값이 없다")
            continue
        v, err = _parse_one(p, raw[p.name])
        if err:
            problems.append(err)
        else:
            values[p.name] = v
    if problems:
        raise ConfigError(problems)
    return values


def params_hash(values: Mapping[str, object]) -> str:
    text = json.dumps({k: str(v) for k, v in sorted(values.items())}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_params_file(spec: StrategySpec, path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except OSError as exc:
        raise ConfigError(f"파라미터 파일을 읽을 수 없음: {path.name}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"파라미터 파일 형식 오류: {path.name}: {exc}") from exc
    problems = [f"{k}: 알 수 없는 최상위 키" for k in raw if k not in ("strategy", "version", "params")]
    if raw.get("strategy") != spec.strategy_id or raw.get("version") != spec.version:
        problems.append("strategy·version이 명세와 다름")
    if problems:
        raise ConfigError(problems)
    params = raw.get("params")
    if not isinstance(params, dict):
        raise ConfigError("[params] 표가 없다")
    return validate_params(spec, params)
