"""[CP-042 D-282, A-128 ②] 공식 판정 전용 한도 파일 config/m2/limits.verdict.toml은 운영 PAPER 한도(A-064) 값과 같고
프로필 표시(profile·approved_by)만 다르다. 운영 PAPER 파일은 바꾸지 않는다(설정 해시 불변 — test_cp034_paper_invariance)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from trading_system.config.limits_file import load_limits_file

ROOT = Path(__file__).resolve().parents[2]


def test_verdict_profile_matches_paper_limits() -> None:
    paper = load_limits_file(ROOT / "config" / "m2" / "limits.paper.toml", "M2")
    verdict = load_limits_file(ROOT / "config" / "m2" / "limits.verdict.toml", "M2")
    assert verdict.account == paper.account
    assert (verdict.limits.profile, verdict.limits.approved_by) == ("m2-paper", "A-064")
    assert (paper.limits.profile, paper.limits.approved_by) == (None, None)
    same = replace(verdict.limits, profile=None, approved_by=None)
    assert same.version_hash() == paper.limits.version_hash()       # 값 동일 — 프로필 표시만 다름
