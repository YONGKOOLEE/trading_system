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


@pytest.mark.parametrize("kind", ["limits", "market"])
def test_backtest_values_equal_paper_except_account(kind: str) -> None:
    bt, paper = _load(f"{kind}.backtest.toml"), _load(f"{kind}.paper.toml")
    assert _without_account(bt) == _without_account(paper)


def test_account_alias_differs_per_mode() -> None:
    bt, paper = _load("limits.backtest.toml"), _load("limits.paper.toml")
    assert bt.get("account") != paper.get("account")
