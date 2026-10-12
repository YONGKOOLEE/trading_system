"""한도·시간 설정 파일 로더 (01 D-026, §2.3.1).

파일 형식(TOML): 최상위 `account`, `[limits]`, `[time]`. 모든 값은 필수이며 기본값이 없다.
[CP-034] 선택 키: `[limits] l1_entry_buffer_ratio`(D-195, 문자열 십진수, 0 이상 0.2 미만). 없으면 기존 동작이다.
[CP-034 D-196] 최상위 `profile`(정책 프로필 이름, 없으면 legacy)·`approved_by`(그 프로필 값의 승인 A-ID, profile 필요).
L7은 `l7_max_order_amount`(금액) 또는 `l7_max_order_ratio`(기준 금액 대비 비율, 문자열) 중 하나만 — 둘 다·둘 다 없음은 거부.
값(리스크 수치)은 사용자가 정해 `config/limits.<mode>.toml`에 둔다. 저장소에는 형식 예제만 둔다.
시간 제약은 시장별로 검사한다(CP-024 D-108): 기본 M1(KRX), M2 설정(`config/m2/limits.<mode>.toml`)은 market="M2".
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from trading_system.domain.limits import AccountLimits, check_limits
from trading_system.domain.market_time import Market

from .errors import ConfigError
from .schema import Field, alias_problem, parse_field, parse_fields

_POSITIVE = Decimal("0")
LIMIT_SPEC = (
    Field("l1_max_loss_ratio", "decimal", min=_POSITIVE, min_exclusive=True),
    Field("l2_max_symbol_ratio", "decimal", min=_POSITIVE, min_exclusive=True),
    Field("l3_max_total_ratio", "decimal", min=_POSITIVE, min_exclusive=True),
    Field("l4_daily_loss_ratio", "decimal", min=_POSITIVE, min_exclusive=True),
    Field("l5_daily_new_orders", "int", min=1),
    Field("l6_max_positions", "int", min=1),
    Field("l10_leverage_total_ratio", "decimal_or_none", min=_POSITIVE, min_exclusive=True),
    Field("warn_usage_ratio", "decimal", min=_POSITIVE, min_exclusive=True, max=Decimal("1")),
)
# [CP-034] 선택 키 — 없으면 기존 동작(값 None). 문자열 십진수만(정수·float 불가), 범위는 check_limits
OPTIONAL_LIMIT_SPEC = (
    Field("l1_entry_buffer_ratio", "decimal"),
    Field("l7_max_order_amount", "decimal", min=_POSITIVE, min_exclusive=True),   # 기존 형식(정수·문자열 허용)
    Field("l7_max_order_ratio", "decimal", min=_POSITIVE, min_exclusive=True),
)
STRING_ONLY = {"l1_entry_buffer_ratio", "l7_max_order_ratio"}   # 새 키는 문자열 십진수만
L7_KEYS = ("l7_max_order_amount", "l7_max_order_ratio")
_PROFILE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_APPROVAL = re.compile(r"A-\d{3,}")
TIME_SPEC = (
    Field("order_window_start", "time"),
    Field("order_window_end", "time"),
    Field("daily_reference_time", "time"),
    Field("report_time", "time"),
)
TOP_KEYS = {"account", "limits", "time", "profile", "approved_by"}


@dataclass(frozen=True)
class ParsedLimits:
    account: str
    limits: AccountLimits
    warnings: tuple[str, ...]


def parse_account_limits(raw: Mapping[str, object], market: str | Market = Market.M1) -> ParsedLimits:
    market = Market(market)  # 알 수 없는 시장이면 ValueError (설정 검사 전에 호출 오류로 드러냄)
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
            optional = OPTIONAL_LIMIT_SPEC if section == "limits" else ()
            names = {f.name for f in optional}
            section_values, section_problems = parse_fields(
                spec, {k: v for k, v in body.items() if k not in names}, section)
            values.update(section_values)
            problems.extend(section_problems)
            for f in optional:
                if f.name not in body:
                    continue
                raw_value = body[f.name]
                if f.name in STRING_ONLY and not isinstance(raw_value, str):
                    problems.append(f"{section}.{f.name}: 문자열 십진수여야 한다 (예: \"0.03\")")
                    continue
                value, err = parse_field(f, raw_value)
                if err:
                    problems.append(f"{section}.{err}")
                else:
                    values[f.name] = value
            if section == "limits":
                present = [k for k in L7_KEYS if k in body]
                if len(present) == 2:
                    problems.append("limits.l7_max_order_amount·l7_max_order_ratio: 둘 중 하나만 둔다 (CP-034 D-196)")
                elif not present:
                    problems.append("limits.l7_max_order_amount: 필수 값이 없다 (또는 l7_max_order_ratio, 하나만)")

    profile, approved_by = _profile_fields(raw, problems)
    if problems:
        raise ConfigError(problems)

    values.setdefault("l7_max_order_amount", None)
    limits = AccountLimits(**values, profile=profile, approved_by=approved_by)  # type: ignore[arg-type]
    logic_problems, warnings = check_limits(limits, market)
    if logic_problems:
        raise ConfigError(logic_problems)
    return ParsedLimits(account=str(account), limits=limits, warnings=tuple(warnings))


def _profile_fields(raw: Mapping[str, object], problems: list[str]) -> tuple[str | None, str | None]:
    """정책 프로필 이름·승인 A-ID (CP-034 D-196). 둘 다 선택. 승인 A-ID는 프로필 이름이 있을 때만."""
    profile = raw.get("profile")
    approved_by = raw.get("approved_by")
    if profile is not None and (not isinstance(profile, str) or not _PROFILE.fullmatch(profile)):
        problems.append("profile: 영문 소문자·숫자·_·- 1~32자 (예: paper0)")
        profile = None
    if approved_by is not None:
        if not isinstance(approved_by, str) or not _APPROVAL.fullmatch(approved_by):
            problems.append("approved_by: 승인 A-ID 형식(A-숫자)")
            approved_by = None
        elif "profile" not in raw:
            problems.append("approved_by: profile 없이 쓸 수 없다 (승인은 프로필 단위)")
            approved_by = None
    return profile, approved_by  # type: ignore[return-value]


def load_limits_file(path: Path, market: str | Market = Market.M1) -> ParsedLimits:
    market = Market(market)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"한도 설정 파일을 읽을 수 없다: {path.name} ({type(exc).__name__})") from exc
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: TOML 문법 오류 ({exc})") from exc
    try:
        return parse_account_limits(raw, market)
    except ConfigError as exc:
        raise ConfigError([f"{path.name}: {p}" for p in exc.problems]) from exc

