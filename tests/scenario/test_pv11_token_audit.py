"""PV-11 토큰 발급 관찰 기록 (pv11-weekend-run.md §1.1·Q-1, A-099).

발급 성공을 사유(INITIAL·REFRESH·REISSUE)·시각·만료 시각과 함께 콜백으로 알리고, run은 감사 기록 TOKEN_ISSUED로
남긴다. 토큰 값은 어디에도 남기지 않는다. 콜백 오류는 토큰 경로에 영향을 주지 않는다.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.support.kiwoom_sim import ACCOUNT_NO, APP_KEY, SECRET, Sim
from trading_system.app.main import token_issue_recorder
from trading_system.broker.kiwoom.adapter import KiwoomAdapter, KiwoomConfig
from trading_system.broker.kiwoom.http import KiwoomHttp
from trading_system.broker.kiwoom.token import TokenIssue
from trading_system.domain.clock import KST, FixedClock
from trading_system.domain.enums import Mode
from trading_system.storage.repository import Repository

ALIAS = "KR-PAPER-01"
T0 = datetime(2026, 10, 16, 10, 0, tzinfo=UTC)  # 금 19:00 KST


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture
def sim(clock: FixedClock) -> Iterator[Sim]:
    s = Sim(now=lambda: clock.now().astimezone(KST).replace(tzinfo=None))
    yield s
    s.close()


def adapter(sim: Sim, clock: FixedClock, on_issued: Callable[[TokenIssue], None]) -> KiwoomAdapter:
    cfg = KiwoomConfig(ALIAS, lambda: ACCOUNT_NO, lambda: APP_KEY, lambda: SECRET, rest_url=sim.url,
                       ws_url=sim.ws_url, per_tr_interval=0.0, order_wait=1.0, query_wait=2.0)
    return KiwoomAdapter(cfg, clock, http=KiwoomHttp(sim.url, 1.0, 0.5), on_token_issued=on_issued)


def test_issue_reasons_initial_refresh_reissue(sim: Sim, clock: FixedClock) -> None:
    sim.expires_in = timedelta(hours=24)
    seen: list[TokenIssue] = []
    a = adapter(sim, clock, seen.append)
    a.query_cash(ALIAS)                                  # 첫 발급
    clock.advance(timedelta(hours=23, minutes=49))       # 만료 11분 전 → 기존 토큰 사용
    a.query_cash(ALIAS)
    assert [e.reason for e in seen] == ["INITIAL"]
    clock.advance(timedelta(minutes=2))                  # 만료 9분 전 → 갱신
    a.query_cash(ALIAS)
    sim.valid_tokens.clear()                             # 인증 오류 → 무효 표시 → 재발급
    a.query_cash(ALIAS)
    assert [e.reason for e in seen] == ["INITIAL", "REFRESH", "REISSUE"]
    first, refresh = seen[0], seen[1]
    assert first.issued_at == T0 and first.previous_expires_at is None and first.count == 1
    assert refresh.previous_expires_at == first.expires_at and refresh.count == 2
    assert refresh.expires_at is not None and refresh.expires_at > first.expires_at
    assert seen[2].previous_expires_at is None and seen[2].count == 3   # 무효화된 토큰은 만료 시각도 지움


def test_issue_event_never_carries_token(sim: Sim, clock: FixedClock) -> None:
    seen: list[TokenIssue] = []
    a = adapter(sim, clock, seen.append)
    a.query_cash(ALIAS)
    tokens = set(sim.valid_tokens)
    assert tokens and not any(t in repr(seen[0]) for t in tokens)
    assert not hasattr(seen[0], "token")


def test_callback_error_does_not_break_token_path(sim: Sim, clock: FixedClock) -> None:
    def boom(_: TokenIssue) -> None:
        raise RuntimeError("관찰 기록 실패")

    a = adapter(sim, clock, boom)
    assert a.query_cash(ALIAS).deposit > 0
    assert a.tokens.issued == 1 and a.tokens.consecutive_failures == 0


def test_failed_issue_does_not_report(sim: Sim, clock: FixedClock) -> None:
    from tests.support.kiwoom_sim import Scripted
    seen: list[TokenIssue] = []
    a = adapter(sim, clock, seen.append)
    sim.script("au10001", *[Scripted("body", body={"return_code": 8005, "return_msg": "x"})] * 3)
    with pytest.raises(Exception):
        a.query_cash(ALIAS)
    assert seen == []


class _Engine:
    """판정·실행 스레드 대신 call_soon을 바로 실행한다."""

    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        self.alerts_seen: list[tuple[str, str]] = []
        self.alerts = self

    def call_soon(self, fn: Callable[[], object]) -> None:
        fn()

    def alert(self, level: str, message: str, ref: str | None) -> None:
        self.alerts_seen.append((level, message))


def test_recorder_writes_audit_and_alerts_only_on_refresh(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "paper.db", Mode.PAPER)
    try:
        eng = _Engine(repo)
        rec = token_issue_recorder(lambda: eng, ALIAS)  # type: ignore[arg-type, return-value]
        exp1 = T0 + timedelta(hours=24)
        rec(TokenIssue("INITIAL", T0, exp1, None, 1))
        rec(TokenIssue("REFRESH", exp1 - timedelta(minutes=9), exp1 + timedelta(hours=24), exp1, 2))
        rows = [e for e in repo.audit_events() if e["kind"] == "TOKEN_ISSUED"]
        assert [r["ref"] for r in rows] == [ALIAS, ALIAS] and {r["actor"] for r in rows} == {"broker"}
        s = [json.loads(r["summary"]) for r in rows]
        assert s[0] == {"reason": "INITIAL", "expires_at": exp1.isoformat(), "previous_expires_at": None, "count": 1}
        assert s[1]["reason"] == "REFRESH" and s[1]["previous_expires_at"] == exp1.isoformat()
        assert [lvl for lvl, _ in eng.alerts_seen] == ["INFO"] and "토큰 갱신" in eng.alerts_seen[0][1]
    finally:
        repo.close()


def test_recorder_before_engine_is_silent() -> None:
    rec = token_issue_recorder(lambda: None, ALIAS)
    rec(TokenIssue("INITIAL", T0, T0 + timedelta(hours=24), None, 1))   # 엔진 전에는 기록할 곳이 없다 — 오류 없음


def test_callback_runs_without_lock_and_may_call_token(sim: Sim, clock: FixedClock) -> None:
    holder: dict[str, KiwoomAdapter] = {}
    seen: list[tuple[bool, str]] = []

    def cb(ev: TokenIssue) -> None:
        tm = holder["a"].tokens
        seen.append((tm._lock.locked(), tm.token()))  # noqa: SLF001 — 잠금 밖 호출 확인, 재진입 교착 없음

    holder["a"] = adapter(sim, clock, cb)
    holder["a"].query_cash(ALIAS)
    assert len(seen) == 1 and seen[0][0] is False


def test_reissue_after_revoke(sim: Sim, clock: FixedClock) -> None:
    seen: list[TokenIssue] = []
    a = adapter(sim, clock, seen.append)
    a.query_cash(ALIAS)
    assert a.tokens.revoke()
    a.query_cash(ALIAS)
    assert [e.reason for e in seen] == ["INITIAL", "REISSUE"] and seen[1].count == 2


def test_recorder_write_failure_is_contained(tmp_path: Path) -> None:
    class _Broken(_Engine):
        log_dir = tmp_path

        def __init__(self) -> None:
            self.alerts_seen = []
            self.alerts = self

            class _Repo:
                def audit(self, *a: object) -> None:
                    raise RuntimeError("저장 실패")
            self.repo = _Repo()  # type: ignore[assignment]

    eng = _Broken()
    rec = token_issue_recorder(lambda: eng, ALIAS)  # type: ignore[arg-type, return-value]
    rec(TokenIssue("REFRESH", T0, T0 + timedelta(hours=24), T0, 2))   # call_soon 안에서 예외가 밖으로 나오지 않는다
    assert eng.alerts_seen == []
