"""한도·시간 설정 파일 로더 (01 D-026, §2.3.1).

파일 형식(TOML): 최상위 `account`, `[limits]`, `[time]`. 모든 값은 필수이며 기본값이 없다.
값(리스크 수치)은 사용자가 정해 `config/limits.<mode>.toml`에 둔다. 저장소에는 형식 예제만 둔다.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from trading_system.domain.limits import AccountLimits, check_limits

from .errors import ConfigError
from .schema import Field, alias_problem, parse_fields

_POSITIVE = Decimal("0")
LIMIT_SPEC = (
    Field("l1_max_loss_ratio", "decimal", min=_POSITIVE, min_exclusive=True),
    Field("l2_max_symbol_ratio", "decimal", min=_POSITIVE, min_exclusive=True),
    Field("l3_max_total_ratio", "decimal", min=_POSITIVE, min_exclusive=True),
    Field("l4_daily_loss_ratio", "decimal", min=_POSITIVE, min_exclusive=True),
    Field("l5_daily_new_orders", "int", min=1),
    Field("l6_max_positions", "int", min=1),
    Field("l7_max_order_amount", "decimal", min=_POSITIVE, min_exclusive=True),
    Field("l10_leverage_total_ratio", "decimal_or_none", min=_POSITIVE, min_exclusive=True),
    Field("warn_usage_ratio", "decimal", min=_POSITIVE, min_exclusive=True, max=Decimal("1")),
)
TIME_SPEC = (
    Field("order_window_start", "time"),
    Field("order_window_end", "time"),
    Field("daily_reference_time", "time"),
    Field("report_time", "time"),
)
TOP_KEYS = {"account", "limits", "time"}


@dataclass(frozen=True)
class ParsedLimits:
    account: str
    limits: AccountLimits
    warnings: tuple[str, ...]


def parse_account_limits(raw: Mapping[str, object]) -> ParsedLimits:
    problems: list[str] = []
    for key in raw:
        if key not in TOP_KEYS:
            problems.append(f"{key}: 알 수 없는 키")

    account = raw.get("account")
    if "account" not in raw:
        problems.append("account: 필수 값이 없다")
    elif (msg := alias_problem(account)) is not None:
        problems.append(msg)

    values: dict[str, object] = {}
    for section, spec in (("limits", LIMIT_SPEC), ("time", TIME_SPEC)):
        body = raw.get(section)
        if body is None:
            problems.append(f"{section}: 섹션이 없다")
        elif not isinstance(body, Mapping):
            problems.append(f"{section}: 표(table)여야 한다")
        else:
            section_values, section_problems = parse_fields(spec, body, section)
            values.update(section_values)
            problems.extend(section_problems)

    if problems:
        raise ConfigError(problems)

    limits = AccountLimits(**values)  # type: ignore[arg-type]
    logic_problems, warnings = check_limits(limits)
    if logic_problems:
        raise ConfigError(logic_problems)
    return ParsedLimits(account=str(account), limits=limits, warnings=tuple(warnings))


def load_limits_file(path: Path) -> ParsedLimits:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"한도 설정 파일을 읽을 수 없다: {path.name} ({type(exc).__name__})") from exc
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: TOML 문법 오류 ({exc})") from exc
    try:
        return parse_account_limits(raw)
    except ConfigError as exc:
        raise ConfigError([f"{path.name}: {p}" for p in exc.problems]) from exc

