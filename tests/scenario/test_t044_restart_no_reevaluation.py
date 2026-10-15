"""T-044 재시작 뒤 같은 판정 시각 재판정 방지 (CP-035 §3 관찰 → Q-3, A-105 "결함으로 고친다", DP-21 유예 5분).

판정 완료 표시(Engine.evaluated)가 메모리뿐이라, 판정 시각 + 유예(EVAL_GRACE 5분) 안에 재시작하고 K8을 재개하면 이미
판정했거나 건너뛴 판정 시각을 새 프로세스가 다시 판정해 두 번째 신호를 낼 수 있었다(재현: 09:30 매수 → 09:31 재시작·재개
→ 같은 09:30 판정에서 청산 신호). 고친 동작:
- 시작(start) 때 오늘 판정 시각마다 저장된 감사 기록(EVALUATION — 판정 ID, EVALUATION_SKIPPED — 판정 키)을 읽어 이미
  처리한 판정 시각을 판정 완료로 표시한다. 한 번 실행 안의 동작(판정·건너뜀 뒤 다시 판정하지 않음)과 같다.
- 기록을 읽지 못하면 오늘 이미 지난 판정 시각을 모두 판정 완료로 보고(판정하지 않음) 알린다. 앞으로 올 판정 시각은 그대로.

fake 브로커·가짜 시계·가상 승인(A-9xx)만 쓴다. 네트워크 없음.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.scenario.test_e2e_paper0_days import DAYS, PaperWorld, buy_sell_of_day, kst, start_and_resume
from trading_system.storage.repository import StorageFailure

V1 = "paper0-verify:1"


def _evals_at(w: PaperWorld, hh: int, mm: int) -> list[tuple[str, dict]]:
    """그 판정 시각(오늘 DAYS[0])의 판정·건너뜀 감사. EVALUATION은 요약의 strategy, 건너뜀은 참조(판정 키)로 가린다."""
    at = kst(DAYS[0], hh, mm).isoformat()
    from trading_system.domain.strategy import evaluation_id_of
    ev_id = evaluation_id_of("paper0-verify", 1, "M1", at)
    return [(e["kind"], e["s"]) for e in w.evaluations() if e["ref"] in (ev_id, f"{V1}|{at}")]


def _restart_resume_same_tick(w: PaperWorld) -> None:
    """재시작 → 시작 → K8 재개 명령을 넣고 한 주기: 운영 명령(재개)이 판정 단계보다 먼저 처리되는 tick."""
    w.restart()
    assert w.engine.start().ok
    w.command("RESUME", {"account": None, "halt_type": "K8"})
    w.step()
    assert "K8" not in w.active(), w.active()


def test_t044_restart_within_grace_does_not_reevaluate_evaluated_slot(tmp_path: Path) -> None:
    w = PaperWorld(tmp_path)
    try:
        start_and_resume(w)
        w.run_until(kst(DAYS[0], 9, 31))
        assert buy_sell_of_day(w, DAYS[0]) == [("ENTRY", "FILLED")] and w.position() == 1
        assert [k for k, _ in _evals_at(w, 9, 30)] == ["EVALUATION"]
        _restart_resume_same_tick(w)                       # 09:31 — 09:30 판정 유예(5분) 안
        w.run_until(kst(DAYS[0], 9, 40))
        assert [k for k, _ in _evals_at(w, 9, 30)] == ["EVALUATION"]       # 다시 판정하지 않음
        assert buy_sell_of_day(w, DAYS[0]) == [("ENTRY", "FILLED")]       # 두 번째 신호(청산) 없음
        assert w.position() == 1
        w.run_until(kst(DAYS[0], 14, 31))                  # 다음 판정 시각은 그대로
        assert buy_sell_of_day(w, DAYS[0]) == [("ENTRY", "FILLED"), ("EXIT", "FILLED")]
    finally:
        w.close()


def test_t044_restart_within_grace_does_not_evaluate_skipped_slot(tmp_path: Path) -> None:
    """앞 프로세스가 K8 중이라 09:30을 건너뜀(EVALUATION_SKIPPED) → 09:31 재시작·재개해도 09:30을 판정하지 않는다
    (한 번 실행 안에서 건너뛴 판정 시각을 다시 판정하지 않는 것과 같다)."""
    w = PaperWorld(tmp_path)
    try:
        assert w.engine.start().ok
        w.run_until(kst(DAYS[0], 9, 31))                   # K8 재개 없음
        assert [(k, s.get("reason", "")[:5]) for k, s in _evals_at(w, 9, 30)] == [("EVALUATION_SKIPPED", "전역 정지")]
        _restart_resume_same_tick(w)
        w.run_until(kst(DAYS[0], 9, 40))
        assert [k for k, _ in _evals_at(w, 9, 30)] == ["EVALUATION_SKIPPED"]
        assert buy_sell_of_day(w, DAYS[0]) == [] and w.sent_count() == 0  # 늦은 진입 없음
    finally:
        w.close()


def test_t044_audit_read_failure_treats_past_slots_as_evaluated_and_alerts(tmp_path: Path,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """판정 기록을 읽지 못하면 보수적으로: 오늘 지난 판정 시각은 판정하지 않고 알린다. 앞으로 올 판정 시각(14:30)은 정상."""
    w = PaperWorld(tmp_path)
    try:
        start_and_resume(w)
        w.run_until(kst(DAYS[0], 9, 31))
        assert w.position() == 1
        w.restart()

        def broken(*a: object, **k: object) -> set[str]:
            raise StorageFailure("가상 읽기 실패")
        monkeypatch.setattr(w.repo, "audited_refs", broken)
        assert w.engine.start().ok
        monkeypatch.undo()
        w.command("RESUME", {"account": None, "halt_type": "K8"})
        w.step()
        w.run_until(kst(DAYS[0], 9, 40))
        assert [k for k, _ in _evals_at(w, 9, 30)] == ["EVALUATION"]       # 다시 판정하지 않음
        assert buy_sell_of_day(w, DAYS[0]) == [("ENTRY", "FILLED")]
        alerts = [e for e in w.repo.audit_events() if e["kind"] == "ALERT" and "T-044" in e["summary"]]
        assert len(alerts) == 1 and "판정하지 않음" in alerts[0]["summary"], alerts
        assert "09:30" in alerts[0]["summary"]
        w.run_until(kst(DAYS[0], 14, 31))
        assert buy_sell_of_day(w, DAYS[0]) == [("ENTRY", "FILLED"), ("EXIT", "FILLED")]
    finally:
        w.close()
