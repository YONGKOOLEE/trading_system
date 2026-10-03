"""PAPER 실행 기록 집계 (T-017 준비, A-044 2단계): 실제 배선으로 만든 1거래일 기록을 읽기 전용으로 집계한다."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.scenario.test_e2e_paper0_days import DAYS, PaperWorld, run_trading_day, start_and_resume
from trading_system.app.review_tool import review_main
from trading_system.reporting.paper_review import render_markdown, review


@pytest.fixture
def one_day(tmp_path: Path):  # type: ignore[no-untyped-def]
    w = PaperWorld(tmp_path)
    try:
        start_and_resume(w)
        run_trading_day(w, DAYS[0])
        yield w
    finally:
        w.close()


def _db(w: PaperWorld) -> Path:
    return w.root / "var" / "paper" / "trading.db"


def test_review_counts_round_trip_and_costs(one_day: PaperWorld) -> None:
    db = _db(one_day)
    before = db.stat().st_mtime_ns
    r = review(db)
    assert r.trade_dates == [DAYS[0].isoformat()]
    rows = r.days[DAYS[0].isoformat()]
    assert [(x["type"], x["side"], x["state"]) for x in rows] == [("ENTRY", "BUY", "FILLED"), ("EXIT", "SELL", "FILLED")]
    assert r.intent_states["FILLED"] == 2 and r.unknown_attempts == 0 and r.duplicate_signals == []
    assert len(r.fills) == 2 and len(r.order_nos[DAYS[0].isoformat()]) == 2
    assert any(h["type"] == "K8" and h["released_by"] != "-" for h in r.halts)   # 시작 K8 → 사용자 재개
    assert r.daily_bases                                                           # DP-31 기준 기록
    assert db.stat().st_mtime_ns == before                                        # 읽기 전용


def test_review_markdown_has_sections(one_day: PaperWorld) -> None:
    text = render_markdown(review(_db(one_day)))
    for head in ("## 거래 (의도)", "## 게이트 판정", "## 주문번호 (PV-02)", "## 정지 이력", "## 일일 기준"):
        assert head in text


def test_review_cli_writes_file_with_estimated_costs(one_day: PaperWorld, capsys: pytest.CaptureFixture[str]) -> None:
    out = one_day.root / "review.md"
    assert review_main(["--mode", "PAPER", "--root", str(one_day.root), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "추정 비용" in text and "| SELL |" in text
    assert "집계 완료" in capsys.readouterr().out


def test_review_missing_db_refused_without_creating(tmp_path: Path) -> None:
    assert review_main(["--mode", "PAPER", "--root", str(tmp_path)]) == 1
    assert not (tmp_path / "var" / "paper" / "trading.db").exists()


def test_review_live_mode_refused(tmp_path: Path) -> None:
    assert review_main(["--mode", "LIVE", "--root", str(tmp_path)]) == 2
