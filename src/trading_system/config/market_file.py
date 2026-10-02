"""시장·게이트 동작 설정 로더 (03 D-095·D-065·§6.2, 01 G11). 파일: `config/market.<mode>.toml`.

[gate] quote_max_age_seconds(정수 초, 승인값 60 A-028), price_limit_ratio(문자열 비율, KRX ±30% 확인)
[costs] commission_rate, sell_tax_rate (문자열 비율, 모의·실전 실제 요율은 사용자 확인 K-U9)
기본값은 없다. 누락·형식 오류·알 수 없는 키면 시작을 거부한다.
"""

from __future__ import annotations

import tomllib
from decimal import Decimal, InvalidOperation
from pathlib import Path

from trading_system.domain.market import CostRates, GateSettings

from .errors import ConfigError


def _ratio(raw: object, name: str, problems: list[str], allow_zero: bool) -> Decimal | None:
    if isinstance(raw, bool) or not isinstance(raw, str):
        problems.append(f"{name}: 문자열 비율이어야 함 (부동소수 금지)")
        return None
    try:
        v = Decimal(raw)
    except InvalidOperation:
        problems.append(f"{name}: 소수 형식이 아님")
        return None
    if not v.is_finite() or v < 0 or (v == 0 and not allow_zero) or v >= 1:
        problems.append(f"{name}: 범위 밖 ({'0 이상' if allow_zero else '0 초과'} 1 미만)")
        return None
    return v


def parse_market_settings(raw: dict) -> GateSettings:
    problems: list[str] = []
    for k in raw:
        if k not in ("gate", "costs", "alerts"):
            problems.append(f"{k}: 알 수 없는 키")
    gate, costs = raw.get("gate"), raw.get("costs")
    if not isinstance(gate, dict):
        problems.append("[gate] 표가 없다")
        gate = {}
    if not isinstance(costs, dict):
        problems.append("[costs] 표가 없다")
        costs = {}
    for k in gate:
        if k not in ("quote_max_age_seconds", "price_limit_ratio"):
            problems.append(f"gate.{k}: 알 수 없는 키")
    for k in costs:
        if k not in ("commission_rate", "sell_tax_rate"):
            problems.append(f"costs.{k}: 알 수 없는 키")
    age = gate.get("quote_max_age_seconds")
    if isinstance(age, bool) or not isinstance(age, int) or age <= 0:
        problems.append("gate.quote_max_age_seconds: 0보다 큰 정수(초)")
    plr = _ratio(gate.get("price_limit_ratio"), "gate.price_limit_ratio", problems, allow_zero=False)
    com = _ratio(costs.get("commission_rate"), "costs.commission_rate", problems, allow_zero=True)
    tax = _ratio(costs.get("sell_tax_rate"), "costs.sell_tax_rate", problems, allow_zero=True)
    if problems:
        raise ConfigError(problems)
    return GateSettings(age, plr, CostRates(com, tax))  # type: ignore[arg-type]


def parse_alert_settings(raw: dict) -> tuple[int, int]:
    """[alerts] reject_surge_count(건), reject_surge_minutes(분) — 거부 급증 경고 기준 (01 §2.10, V-117). 기본값 없음."""
    a = raw.get("alerts")
    problems: list[str] = []
    if not isinstance(a, dict):
        raise ConfigError("[alerts] 표가 없다")
    for k in a:
        if k not in ("reject_surge_count", "reject_surge_minutes"):
            problems.append(f"alerts.{k}: 알 수 없는 키")
    values = []
    for k in ("reject_surge_count", "reject_surge_minutes"):
        v = a.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            problems.append(f"alerts.{k}: 1 이상 정수")
        values.append(v)
    if problems:
        raise ConfigError(problems)
    return values[0], values[1]  # type: ignore[return-value]


def load_market_settings(path: Path) -> GateSettings:
    try:
        with path.open("rb") as f:
            return parse_market_settings(tomllib.load(f))
    except OSError as exc:
        raise ConfigError(f"시장 설정 파일을 읽을 수 없음: {path.name}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"시장 설정 파일 형식 오류: {path.name}: {exc}") from exc
