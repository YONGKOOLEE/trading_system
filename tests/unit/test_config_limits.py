"""한도·시간 설정 스키마와 논리 제약 (04 V-86, 01 §2.3.1-2·3, D-026).

아래 값은 테스트용 가상 값이다. 승인된 리스크 수치가 아니며 저장소에 기본값으로 두지 않는다.
"""

from __future__ import annotations

import copy
from datetime import time
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.limits_fixtures import TOML, VALID
from trading_system.config.errors import ConfigError
from trading_system.config.limits_file import load_limits_file, parse_account_limits
from trading_system.config.schema import Field, validate_params

LIMIT_KEYS = list(VALID["limits"])  # type: ignore[arg-type]
TIME_KEYS = list(VALID["time"])  # type: ignore[arg-type]


def mutated(section: str, key: str, value: object) -> dict:
    raw = copy.deepcopy(VALID)
    raw[section][key] = value  # type: ignore[index]
    return raw


def problems_of(raw: dict) -> str:
    with pytest.raises(ConfigError) as exc:
        parse_account_limits(raw)
    return "\n".join(exc.value.problems)


def test_valid_config_parses_to_decimals_and_times() -> None:
    parsed = parse_account_limits(VALID)
    lim = parsed.limits
    assert parsed.account == "KR-BT-01"
    assert lim.l2_max_symbol_ratio == Decimal("0.20")
    assert isinstance(lim.l2_max_symbol_ratio, Decimal)
    assert lim.l5_daily_new_orders == 10
    assert lim.l10_leverage_total_ratio is None
    assert lim.order_window_start == time(9, 5)
    assert parsed.warnings == ()


@pytest.mark.parametrize("key", LIMIT_KEYS)
def test_missing_limit_key_rejected(key: str) -> None:
    raw = copy.deepcopy(VALID)
    del raw["limits"][key]  # type: ignore[attr-defined]
    assert key in problems_of(raw)


@pytest.mark.parametrize("key", TIME_KEYS)
def test_missing_time_key_rejected(key: str) -> None:
    raw = copy.deepcopy(VALID)
    del raw["time"][key]  # type: ignore[attr-defined]
    assert key in problems_of(raw)


@pytest.mark.parametrize("section", ["limits", "time"])
def test_missing_section_and_account_rejected(section: str) -> None:
    raw = copy.deepcopy(VALID)
    del raw[section]
    assert section in problems_of(raw)
    raw = copy.deepcopy(VALID)
    del raw["account"]
    assert "account" in problems_of(raw)


def test_unknown_keys_rejected() -> None:
    assert "typo_key" in problems_of(mutated("limits", "typo_key", "1"))
    raw = copy.deepcopy(VALID)
    raw["extra"] = 1
    assert "extra" in problems_of(raw)


@pytest.mark.parametrize("value", [0.2, True, ["0.2"], None, "abc", "1e-1", "0,2", "", "NaN", "Infinity"])
def test_ratio_type_and_format_errors(value: object) -> None:
    assert "l2_max_symbol_ratio" in problems_of(mutated("limits", "l2_max_symbol_ratio", value))


def test_float_literal_message_mentions_float() -> None:
    assert "float" in problems_of(mutated("limits", "l2_max_symbol_ratio", 0.2))


def test_integer_ratio_accepted_as_decimal() -> None:
    parsed = parse_account_limits(mutated("limits", "l3_max_total_ratio", 1))
    assert parsed.limits.l3_max_total_ratio == Decimal("1")


@pytest.mark.parametrize("key", ["l1_max_loss_ratio", "l2_max_symbol_ratio", "l3_max_total_ratio", "l4_daily_loss_ratio"])
@pytest.mark.parametrize("value", ["0", "-0.1"])
def test_ratios_must_be_positive(key: str, value: str) -> None:
    assert key in problems_of(mutated("limits", key, value))


@pytest.mark.parametrize(("key", "value"), [
    ("l1_max_loss_ratio", "0.30"),   # L1 > L2
    ("l2_max_symbol_ratio", "0.60"),  # L2 > L3
    ("l10_leverage_total_ratio", "0.60"),  # L10 > L3
])
def test_ordering_constraints(key: str, value: str) -> None:
    assert key in problems_of(mutated("limits", key, value))


def test_l1_equal_l2_equal_l3_allowed() -> None:
    raw = copy.deepcopy(VALID)
    for k in ("l1_max_loss_ratio", "l2_max_symbol_ratio", "l3_max_total_ratio"):
        raw["limits"][k] = "0.30"  # type: ignore[index]
    parse_account_limits(raw)


@pytest.mark.parametrize("key", ["l5_daily_new_orders", "l6_max_positions"])
@pytest.mark.parametrize("value", [0, -1, "5", 1.5, True, None])
def test_counts_must_be_positive_integers(key: str, value: object) -> None:
    assert key in problems_of(mutated("limits", key, value))


@pytest.mark.parametrize("value", ["0", "-1", 0, "abc"])
def test_max_order_amount_must_be_positive(value: object) -> None:
    assert "l7_max_order_amount" in problems_of(mutated("limits", "l7_max_order_amount", value))


def test_l10_none_or_ratio() -> None:
    assert parse_account_limits(mutated("limits", "l10_leverage_total_ratio", "0.30")).limits.l10_leverage_total_ratio == Decimal("0.30")
    assert "l10_leverage_total_ratio" in problems_of(mutated("limits", "l10_leverage_total_ratio", "None"))
    assert "l10_leverage_total_ratio" in problems_of(mutated("limits", "l10_leverage_total_ratio", "0"))


@pytest.mark.parametrize("value", ["0", "1.01", "-0.5"])
def test_warn_usage_ratio_range(value: str) -> None:
    assert "warn_usage_ratio" in problems_of(mutated("limits", "warn_usage_ratio", value))


def test_warn_usage_ratio_one_allowed() -> None:
    parse_account_limits(mutated("limits", "warn_usage_ratio", "1"))


@pytest.mark.parametrize("value", ["9:00", "25:00", "09:60", "09:00:00", "0900", "", 900, None, "09:5"])
def test_time_format_errors(value: object) -> None:
    assert "order_window_start" in problems_of(mutated("time", "order_window_start", value))


def test_window_start_must_precede_end() -> None:
    raw = mutated("time", "order_window_start", "15:15")
    assert "order_window" in problems_of(raw)
    raw = mutated("time", "order_window_end", "09:05")
    assert "order_window" in problems_of(raw)


@pytest.mark.parametrize(("key", "value"), [
    ("order_window_start", "08:59"),  # 정규장 시작 전
    ("order_window_end", "15:31"),    # 정규장 종료 후
])
def test_window_must_be_inside_regular_session(key: str, value: str) -> None:
    assert key in problems_of(mutated("time", key, value))


def test_window_may_cover_full_regular_session() -> None:
    raw = mutated("time", "order_window_start", "09:00")
    raw["time"]["order_window_end"] = "15:30"
    parse_account_limits(raw)


def test_daily_reference_must_be_before_open_and_report_after_close() -> None:
    assert "daily_reference_time" in problems_of(mutated("time", "daily_reference_time", "09:00"))
    assert "daily_reference_time" in problems_of(mutated("time", "daily_reference_time", "10:00"))
    assert "report_time" in problems_of(mutated("time", "report_time", "15:30"))
    assert "report_time" in problems_of(mutated("time", "report_time", "09:10"))


def test_all_problems_reported_at_once() -> None:
    raw = copy.deepcopy(VALID)
    raw["limits"]["l2_max_symbol_ratio"] = "0"  # type: ignore[index]
    raw["limits"]["l5_daily_new_orders"] = 0  # type: ignore[index]
    del raw["time"]["report_time"]  # type: ignore[attr-defined]
    with pytest.raises(ConfigError) as exc:
        parse_account_limits(raw)
    assert len(exc.value.problems) >= 3


def test_l3_above_100_percent_is_warning_not_error() -> None:
    raw = mutated("limits", "l3_max_total_ratio", "1.50")
    parsed = parse_account_limits(raw)
    assert any("l3_max_total_ratio" in w for w in parsed.warnings)


@pytest.mark.parametrize("alias", ["", "1234567890", "KR-12345678-01", "kr-paper-01", 5, None, "KR PAPER 01"])
def test_account_alias_rules(alias: object) -> None:
    raw = copy.deepcopy(VALID)
    raw["account"] = alias
    assert "account" in problems_of(raw)


# --- 파일 로딩 -------------------------------------------------------------



def test_load_file(tmp_path: Path) -> None:
    p = tmp_path / "limits.backtest.toml"
    p.write_text(TOML, encoding="utf-8")
    assert load_limits_file(p).limits.l6_max_positions == 5


def test_toml_float_literal_rejected(tmp_path: Path) -> None:
    p = tmp_path / "limits.toml"
    p.write_text(TOML.replace('"0.20"', "0.20"), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_limits_file(p)
    assert "float" in str(exc.value)


def test_toml_syntax_error_and_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "bad.toml"
    p.write_text("[limits\nx = ", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_limits_file(p)
    with pytest.raises(ConfigError):
        load_limits_file(tmp_path / "missing.toml")


def test_example_file_is_placeholder_only_and_fails_validation() -> None:
    """예제는 형식만 보여 주는 자리표시자다. 그대로는 시작할 수 없어야 한다 (기본값 없음, 01 §2.3.1-2)."""
    example = Path(__file__).resolve().parents[2] / "config" / "limits.example.toml"
    assert example.is_file()
    with pytest.raises(ConfigError):
        load_limits_file(example)


# --- 전략 파라미터 일반 스키마 (V-86, R-19) ---------------------------------

PARAMS = [
    Field("fast_window", "int", min=2, max=200),
    Field("threshold", "decimal", min=Decimal("0"), max=Decimal("1")),
    Field("name", "str"),
]


def test_param_validation_ok() -> None:
    values = validate_params(PARAMS, {"fast_window": 5, "threshold": "0.5", "name": "x"})
    assert values == {"fast_window": 5, "threshold": Decimal("0.5"), "name": "x"}


@pytest.mark.parametrize(("raw", "needle"), [
    ({"threshold": "0.5", "name": "x"}, "fast_window"),                       # 누락
    ({"fast_window": 1, "threshold": "0.5", "name": "x"}, "fast_window"),     # 범위 밖
    ({"fast_window": 201, "threshold": "0.5", "name": "x"}, "fast_window"),
    ({"fast_window": 5, "threshold": "1.5", "name": "x"}, "threshold"),
    ({"fast_window": 5, "threshold": 0.5, "name": "x"}, "threshold"),         # float
    ({"fast_window": "5", "threshold": "0.5", "name": "x"}, "fast_window"),   # 형식
    ({"fast_window": 5, "threshold": "0.5", "name": "x", "typo": 1}, "typo"),  # 알 수 없는 키
])
def test_param_validation_errors(raw: dict, needle: str) -> None:
    with pytest.raises(ConfigError) as exc:
        validate_params(PARAMS, raw)
    assert needle in str(exc.value)
