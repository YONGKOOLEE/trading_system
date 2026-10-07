"""한도·시간 설정 검사의 시장별 분리 (CP-024 D-108, T-M2-02, A-064). V-152(M1 검사 불변).

- M1(KRX): 기존 검사 그대로 — 주문 창은 정규장 안, 일일 기준 시각은 장 전, 보고는 장 후.
- M2(UTC 24/7): 일일 기준 시각 = UTC 00:00(A-055), 주문 창은 일 경계를 넘지 않는 한 구간(시작 < 종료),
  시작은 일일 기준 시각 뒤(일 경계 확정 전 주문 방지 [제안]). KRX 정규장 제약은 적용하지 않는다.
아래 M2 시험 값 중 config/m2/limits.paper.toml 외의 값은 가상이다.
"""

from __future__ import annotations

import copy
from datetime import time
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.limits_fixtures import VALID
from trading_system.config.errors import ConfigError
from trading_system.config.limits_file import load_limits_file, parse_account_limits
from trading_system.domain.limits import check_limits

ROOT = Path(__file__).resolve().parents[2]
M2_FILE = ROOT / "config" / "m2" / "limits.paper.toml"

M2_VALID = {
    "account": "BN-BT-01",
    "limits": {
        "l1_max_loss_ratio": "0.005", "l2_max_symbol_ratio": "0.10", "l3_max_total_ratio": "0.50",
        "l4_daily_loss_ratio": "0.01", "l5_daily_new_orders": 5, "l6_max_positions": 3,
        "l7_max_order_amount": "100", "l10_leverage_total_ratio": "none", "warn_usage_ratio": "0.80",
    },
    "time": {
        "order_window_start": "00:10", "order_window_end": "23:50",
        "daily_reference_time": "00:00", "report_time": "00:10",
    },
}


def m2_with(key: str, value: str) -> dict:
    raw = copy.deepcopy(M2_VALID)
    raw["time"][key] = value  # type: ignore[index]
    return raw


def m2_problems(raw: dict) -> str:
    with pytest.raises(ConfigError) as exc:
        parse_account_limits(raw, market="M2")
    return "\n".join(exc.value.problems)


# --- M1: 기존 검사 불변 (V-152) ---------------------------------------------------------
@pytest.mark.parametrize("name", ["limits.paper.toml", "limits.backtest.toml"])
def test_m1_config_files_same_result_with_default_and_explicit_m1(name: str) -> None:
    default = load_limits_file(ROOT / "config" / name)
    explicit = load_limits_file(ROOT / "config" / name, market="M1")
    assert default == explicit
    assert check_limits(default.limits) == check_limits(default.limits, market="M1")


def test_m1_still_rejects_window_outside_krx_session() -> None:
    raw = copy.deepcopy(VALID)
    raw["time"]["order_window_start"] = "00:10"  # type: ignore[index]
    with pytest.raises(ConfigError) as exc:
        parse_account_limits(raw)
    assert "order_window_start: 정규장 시작(09:00) 이후여야 한다" in exc.value.problems


def test_m2_values_rejected_by_m1_rules() -> None:
    """M2 시간 값을 M1 검사로 읽으면 거부된다(시장 혼용 방지). M1 KRX 검사는 M2 값을 통과시키지 않는다."""
    with pytest.raises(ConfigError):
        parse_account_limits(M2_VALID)


def test_unknown_market_rejected() -> None:
    with pytest.raises(ValueError):
        parse_account_limits(M2_VALID, market="M9")


# --- M2: A-064 값 파일 -------------------------------------------------------------------
def test_m2_paper_file_loads_a064_values() -> None:
    parsed = load_limits_file(M2_FILE, market="M2")
    lim = parsed.limits
    assert parsed.account.startswith("BN-")
    assert lim.l1_max_loss_ratio == Decimal("0.005")
    assert lim.l2_max_symbol_ratio == Decimal("0.10")
    assert lim.l3_max_total_ratio == Decimal("0.50")
    assert lim.l4_daily_loss_ratio == Decimal("0.01")
    assert lim.l5_daily_new_orders == 5
    assert lim.l6_max_positions == 3
    assert lim.l7_max_order_amount == Decimal("100")
    assert lim.l10_leverage_total_ratio is None
    assert lim.warn_usage_ratio == Decimal("0.80")
    assert (lim.order_window_start, lim.order_window_end) == (time(0, 10), time(23, 50))
    assert lim.daily_reference_time == time(0, 0)
    assert lim.report_time == time(0, 10)
    assert parsed.warnings == ()


def test_m2_paper_file_rejected_by_m1_loader() -> None:
    with pytest.raises(ConfigError):
        load_limits_file(M2_FILE)


def test_m2_file_has_no_secret_like_values() -> None:
    text = M2_FILE.read_text(encoding="utf-8")
    for word in ("secret", "apikey", "api_key", "token", "SECRET", "TOKEN"):
        assert word not in text


# --- M2: 잘못된 값 거부 ------------------------------------------------------------------
def test_m2_window_crossing_day_boundary_rejected() -> None:
    """자정(UTC)을 넘는 창(시작 > 종료)은 일 경계를 넘으므로 거부 — L4·L5 집계 구간이 갈린다."""
    assert "일 경계" in m2_problems(m2_with("order_window_start", "23:55"))  # 23:55 → 다음 날 23:50
    raw = m2_with("order_window_start", "22:00")
    raw["time"]["order_window_end"] = "02:00"
    assert "일 경계" in m2_problems(raw)


def test_m2_empty_window_rejected() -> None:
    raw = m2_with("order_window_start", "12:00")
    raw["time"]["order_window_end"] = "12:00"
    assert "order_window" in m2_problems(raw)


@pytest.mark.parametrize("ref", ["09:00", "00:01", "23:59"])
def test_m2_daily_reference_must_be_utc_midnight(ref: str) -> None:
    assert "daily_reference_time" in m2_problems(m2_with("daily_reference_time", ref))


def test_m2_window_cannot_start_at_day_boundary() -> None:
    assert "order_window_start" in m2_problems(m2_with("order_window_start", "00:00"))


def test_m2_does_not_apply_krx_session_rules() -> None:
    """KRX 정규장 제약(09:00~15:30, 보고 15:30 후)은 M2에 적용하지 않는다."""
    parsed = parse_account_limits(M2_VALID, market="M2")
    assert parsed.limits.order_window_start == time(0, 10)
    raw = m2_with("report_time", "12:00")
    assert parse_account_limits(raw, market="M2").limits.report_time == time(12, 0)


def test_m2_common_limit_rules_still_apply() -> None:
    raw = copy.deepcopy(M2_VALID)
    raw["limits"]["l1_max_loss_ratio"] = "0.20"  # type: ignore[index]
    assert "L1 ≤ L2" in m2_problems(raw)
