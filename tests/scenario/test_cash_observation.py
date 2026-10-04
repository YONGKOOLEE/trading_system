"""예수금 관찰 기록 (DP-31 재결정 입력, 2026-10-04 사용자 승인): kt00001 조회 값(entr·d1_entra·d2_entra·
ord_alow_amt·pymn_alow_amt)을 값이 바뀔 때마다 감사 기록 CASH_OBSERVED로 남기고 PAPER 집계에 보인다.

관찰 전용 — 게이트·현금 계산(CashView.deposit·broker_orderable)에는 쓰지 않는다. 선택 필드가 없거나 해석할 수
없어도 조회는 실패하지 않는다(필수 필드 entr·ord_alow_amt는 종전대로 실패).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_e2e_paper0_days import DAYS, PaperWorld, run_trading_day, start_and_resume
from tests.scenario.test_t014_kiwoom import ALIAS, adapter, clock, sim  # noqa: F401 (fixture)
from tests.support.kiwoom_sim import Scripted, Sim
from trading_system.broker.base import BrokerQueryError
from trading_system.domain.clock import FixedClock
from trading_system.reporting.paper_review import render_markdown, review
from trading_system.storage.repository import StorageFailure


def _ok(**fields: str) -> Scripted:
    return Scripted("body", body={"return_code": 0, "return_msg": "조회 완료", **fields})


def test_kiwoom_cash_carries_observed_fields(sim: Sim, clock: FixedClock) -> None:  # noqa: F811
    sim.script("kt00001", _ok(entr="000000100000000", d1_entra="000000099000000", d2_entra="000000098500000",
                              ord_alow_amt="000000098000000", pymn_alow_amt="000000097000000"))
    cash = adapter(sim, clock).query_cash(ALIAS)
    assert cash.deposit == Decimal("100000000") and cash.broker_orderable == Decimal("98000000")
    assert dict(cash.observed) == {"entr": "100000000", "d1_entra": "99000000", "d2_entra": "98500000",
                                   "ord_alow_amt": "98000000", "pymn_alow_amt": "97000000"}


def test_kiwoom_cash_optional_field_missing_or_bad_does_not_fail(sim: Sim, clock: FixedClock) -> None:  # noqa: F811
    sim.script("kt00001", _ok(entr="000000100000000", ord_alow_amt="000000100000000", d2_entra="abc"))
    cash = adapter(sim, clock).query_cash(ALIAS)
    obs = dict(cash.observed)
    assert obs["entr"] == "100000000" and obs["d2_entra"] == "해석 불가" and "d1_entra" not in obs


def test_kiwoom_cash_required_field_bad_still_fails(sim: Sim, clock: FixedClock) -> None:  # noqa: F811
    sim.script("kt00001", _ok(entr="abc", ord_alow_amt="000000100000000"))
    with pytest.raises(BrokerQueryError):
        adapter(sim, clock).query_cash(ALIAS)


@pytest.fixture
def one_day(tmp_path: Path) -> Iterator[PaperWorld]:
    w = PaperWorld(tmp_path)
    try:
        start_and_resume(w)
        run_trading_day(w, DAYS[0])
        yield w
    finally:
        w.close()


def _cash_events(w: PaperWorld) -> list[dict]:
    return [json.loads(e["summary"]) for e in w.repo.audit_events() if e["kind"] == "CASH_OBSERVED"]


def test_engine_records_cash_only_when_changed(one_day: PaperWorld) -> None:
    events = _cash_events(one_day)
    assert events, "예수금 조회가 있었는데 관찰 기록이 없음"
    keys = [(e["deposit"], e["orderable"], json.dumps(e["observed"], sort_keys=True)) for e in events]
    assert all(a != b for a, b in zip(keys, keys[1:])), "같은 값을 연속으로 기록함"
    assert all(e["account"] == "KR-PAPER-01" for e in events)


def test_review_shows_cash_observations(one_day: PaperWorld) -> None:
    r = review(one_day.root / "var" / "paper" / "trading.db")
    assert r.cash_observations
    assert "## 예수금 관찰 (CASH_OBSERVED, DP-31)" in render_markdown(r)


# --- 독립 검토 #1 (2026-10-04): 관찰 기록 실패가 대조·시작을 막지 않는다 (관찰 전용) -----------------------

def _reconcile_count(w: PaperWorld) -> int:
    return w.repo.connection.execute("SELECT COUNT(*) FROM reconciliation").fetchone()[0]


def test_cash_observation_failure_does_not_block_reconcile_or_raise_k8(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = PaperWorld(tmp_path)
    try:
        start_and_resume(w)
        assert "K8" not in w.repo.active_halts("KR-PAPER-01")

        def boom(cash: object) -> None:
            raise StorageFailure("주입된 저장 실패 (관찰 기록)")

        monkeypatch.setattr(w.engine, "_observe_cash", boom)
        before = _reconcile_count(w)
        w.engine.request_reconcile()
        w.engine._reconcile_if_due(w.clock.now())          # 예외가 밖으로 나오면 tick이 K8을 건다
        assert _reconcile_count(w) == before + 1           # 대조는 그대로 실행
        assert "K8" not in w.repo.active_halts("KR-PAPER-01")
    finally:
        w.close()


def test_cash_observation_retried_after_failure(tmp_path: Path) -> None:
    w = PaperWorld(tmp_path)
    try:
        start_and_resume(w)
        n = len(_cash_events(w))
        w.engine._last_cash_observed = None                # 다음 조회가 기록 대상이 되게
        w.repo.fail_next_transaction = True                # 관찰 기록의 tx가 실패
        w.engine._refresh_cash()                           # 예외 없이 넘어가야 함
        assert len(_cash_events(w)) == n
        w.engine._refresh_cash()                           # 다음 조회에서 다시 기록
        assert len(_cash_events(w)) == n + 1
    finally:
        w.close()
