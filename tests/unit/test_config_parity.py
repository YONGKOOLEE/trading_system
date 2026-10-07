"""BACKTEST 설정 = PAPER 설정 (DP-30, review-2026-10-03 C절).

검증 전략 백테스트 기록(paper0-verify backtest-v1)은 두 설정이 같다는 전제로 작성됐다.
계좌 별칭만 모드별로 달라야 하고(CLAUDE.md §7 모드 분리), 나머지 값은 같아야 한다.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

CONFIG = Path(__file__).resolve().parents[2] / "config"


def _load(name: str) -> dict:
    with (CONFIG / name).open("rb") as f:
        return tomllib.load(f)


def _without_account(d: object) -> object:
    if isinstance(d, dict):
        return {k: _without_account(v) for k, v in d.items() if k != "account"}
    return d


def _without_paper_commission(d: dict) -> dict:
    """A-075 (2026-10-07, 요구 변경): PAPER 수수료율만 키움 모의 실측(0.35%)으로 다르다. 백테스트는 실매매 요율
    가정을 유지한다. 매도 세금 등 나머지 값은 같아야 한다(DP-30). PAPER·백테스트 비교 때는 수수료 차이를 따로 본다."""
    out = {k: v for k, v in d.items()}
    out["costs"] = {k: v for k, v in d.get("costs", {}).items() if k != "commission_rate"}
    return out


@pytest.mark.parametrize("kind", ["limits", "market"])
def test_backtest_values_equal_paper_except_account(kind: str) -> None:
    bt, paper = _load(f"{kind}.backtest.toml"), _load(f"{kind}.paper.toml")
    if kind == "market":
        bt, paper = _without_paper_commission(bt), _without_paper_commission(paper)
    assert _without_account(bt) == _without_account(paper)


def test_paper_commission_is_mock_measured_and_backtest_keeps_live_rate() -> None:
    """A-075: PAPER 0.35%(모의 실측), 백테스트 0.015%(실매매 요율 가정) — 서로 바뀌면 실패."""
    assert _load("market.paper.toml")["costs"]["commission_rate"] == "0.0035"
    assert _load("market.backtest.toml")["costs"]["commission_rate"] == "0.00015"


def test_account_alias_differs_per_mode() -> None:
    bt, paper = _load("limits.backtest.toml"), _load("limits.paper.toml")
    assert bt.get("account") != paper.get("account")
