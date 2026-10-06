"""T-017 검증 전략 paper0-verify v2 (A-067 B-5): 매수는 split_time 전 판정에서만, 매도는 split_time 이후 판정에서만.

1일차(2026-10-06)처럼 14:30 매도를 놓치면 v1은 다음 날부터 09:30 매도·14:30 매수로 순서가 뒤집혀 매일 밤 보유한다.
v2는 다음 날 오전엔 아무것도 하지 않고 14:30에 매도 → 그다음 날부터 원래 순서로 돌아온다. 가상 스냅샷만 쓴다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.config.capabilities import load_capabilities
from trading_system.config.limits_file import load_limits_file
from trading_system.config.strategy_params import load_params_file
from trading_system.config.strategy_registry import validate_spec
from trading_system.domain.enums import Mode, RequestType
from trading_system.domain.strategy import Bar, EvaluationSnapshot, InstrumentView, PositionView
from trading_system.strategy.paper0_verify import SPEC_V2, Paper0VerifyV2

ROOT = Path(__file__).resolve().parents[2]
AM = datetime(2026, 10, 7, 0, 30, tzinfo=UTC)    # KST 09:30
PM = datetime(2026, 10, 7, 5, 30, tzinfo=UTC)    # KST 14:30
CODE = "005930"
PARAMS = {"qty": 1, "hold_days": 0, "split_time": "12:00"}


def snap(at: datetime, **kw: object) -> EvaluationSnapshot:
    base: dict = {"instrument": CODE, "daily_bars": (Bar(at.date(), *(Decimal("70000"),) * 4, Decimal("1")),),
                  "last_price": Decimal("70500")}
    base.update(kw)
    return EvaluationSnapshot("ev", "M1", "KR-PAPER-01", at, Decimal("100000000"), (InstrumentView(**base),))


HOLD = PositionView(Decimal("1"), Decimal("70000"), 0, True)


def kinds(at: datetime, **kw: object) -> list[RequestType]:
    return [s.kind for s in Paper0VerifyV2().evaluate(snap(at, **kw), PARAMS)]


def test_morning_flat_enters_afternoon_holding_exits() -> None:
    assert kinds(AM) == [RequestType.ENTRY]
    assert kinds(PM, position=HOLD) == [RequestType.EXIT]


def test_morning_holding_does_not_exit_afternoon_flat_does_not_enter() -> None:
    """1일차 다음 날: 보유 1주로 시작 → 09:30 아무것도 안 함, 14:30 매도. 매도 놓친 날 오후에는 매수 안 함."""
    assert kinds(AM, position=HOLD) == []
    assert kinds(PM) == []


def test_exit_respects_hold_days() -> None:
    held = PositionView(Decimal("1"), Decimal("70000"), 0, True)
    assert Paper0VerifyV2().evaluate(snap(PM, position=held), {**PARAMS, "hold_days": 1}) == []


@pytest.mark.parametrize("bad", ["12", "25:00", "1200", "aa:bb"])
def test_split_time_must_be_hhmm(bad: str) -> None:
    with pytest.raises(ValueError):
        Paper0VerifyV2().evaluate(snap(AM), {**PARAMS, "split_time": bad})


def test_v2_spec_and_params_file_valid() -> None:
    assert SPEC_V2.strategy_id == "paper0-verify" and SPEC_V2.version == 2
    caps = load_capabilities(ROOT / "docs" / "capabilities.md")
    limits = load_limits_file(ROOT / "config" / "limits.paper.toml").limits
    assert validate_spec(SPEC_V2, caps, limits, Mode.PAPER, verification_approval="A-067") == []
    params = load_params_file(SPEC_V2, ROOT / "config" / "strategies" / "paper0-verify-v2.toml")
    assert params["split_time"] == "12:00" and int(str(params["qty"])) == 1


# --- 재검토 반영 (V-1·V-2·V-3) -----------------------------------------------------------------------

from datetime import time  # noqa: E402


@pytest.mark.parametrize("split,times,ok", [
    ("12:00", (time(9, 30), time(14, 30)), True),
    ("14:30", (time(9, 30), time(14, 30)), False),     # 판정 시각과 같음
    ("16:00", (time(9, 30), time(14, 30)), False),     # 둘 다 전 → 청산 불가
    ("09:00", (time(9, 30), time(14, 30)), False),     # 둘 다 후 → 진입 불가
    ("9:30", (time(9, 30), time(14, 30)), False),      # 형식
])
def test_validate_deploy(split: str, times: tuple, ok: bool) -> None:
    problems = Paper0VerifyV2.validate_deploy({**PARAMS, "split_time": split}, times)
    assert (problems == []) is ok, problems


def test_hold_days_above_zero_rejected_at_load(tmp_path: Path) -> None:
    from trading_system.config.errors import ConfigError
    f = tmp_path / "p.toml"
    f.write_text('strategy = "paper0-verify"\nversion = 2\n[params]\nqty = 1\nhold_days = 1\nsplit_time = "12:00"\n',
                 encoding="utf-8")
    with pytest.raises((ConfigError, ValueError)):
        load_params_file(SPEC_V2, f)


def test_boundary_exactly_split_time_is_afternoon() -> None:
    at = datetime(2026, 10, 7, 3, 0, tzinfo=UTC)   # KST 12:00
    assert kinds(at) == [] and kinds(at, position=HOLD) == [RequestType.EXIT]
