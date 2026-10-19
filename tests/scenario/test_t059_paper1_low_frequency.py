"""CP-038 T-059 (A-121 Q-2 (가), 착수 A-127) + CP-041 T-072 집계 쪽(A-122 Q-4 (가)) — M2 PAPER-1 집계의 전략별 기간 조건.

V-312(저빈도 예외 PAPER-1: 90 UTC일 + 왕복 1건, 상한 180일 → 판단 불가), V-350(D-279 저빈도: 90 UTC일, 왕복 0건이면
판정 보류), 두 규칙의 적용 관계(저빈도 예외 = CP-038 D-238, D-278 저빈도만 = CP-041 D-279), D-240 표시. 판정은 하지 않고 표만.
가상 DB(tmp_path)만 쓴다 — 실제 var/·네트워크 없음. 모든 값은 가상이다.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.domain.clock import to_iso
from trading_system.domain.enums import Mode
from trading_system.reporting.m2_paper1 import (
    LF_LABEL, PeriodClass, build_m2_paper1, period_status, render_m2_paper1,
)
from trading_system.storage.repository import DUST_RELEASE_REASON, OrderIdentity, Repository

D = Decimal
ACCOUNT = "BN-PAPER-01"
START = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
OWNER = "t-lf:1"
EXC = PeriodClass("t-lf", 1, low_frequency=True, exception=True, approved_by="A-900")       # 저빈도 예외(CP-038)
LOW = PeriodClass("t-lf", 1, low_frequency=True, exception=False, approved_by="A-900")      # D-278 저빈도만
GEN = PeriodClass("t-lf", 1, low_frequency=False, exception=False, approved_by="A-900")     # 일반 28일


def _trip(repo: Repository, n: int, entry_at: datetime, exit_at: datetime, *, owner: str = OWNER,
          inst: str = "BTCUSDT", sell: bool = True, reason: str = "전량 청산") -> None:
    """가상 왕복: 진입 체결(BUY) → 전량 청산 체결(SELL) → 소유권 해제. sell=False면 매도 체결 없이 해제(왕복 아님)."""
    for side, at, rt in (("BUY", entry_at, "ENTRY"), ("SELL", exit_at, "EXIT")):
        if side == "SELL" and not sell:
            continue
        did, iid, key = f"d-{n}-{side}", f"i-{n}-{side}", f"k-{n}-{side}"
        repo.insert_decision({"decision_id": did, "created_at": to_iso(at), "source": "STRATEGY", "request_type": rt,
                              "account": ACCOUNT, "instrument": inst, "side": side, "quantity": "0.001",
                              "price": "100", "idem_key": key, "result": "APPROVED", "checks": "[]"})
        repo.insert_intent({"intent_id": iid, "decision_id": did, "idem_key": key, "source": "STRATEGY",
                            "request_type": rt, "account": ACCOUNT, "instrument": inst, "side": side,
                            "quantity": "0.001", "order_type": "MARKET", "price": "100", "owner": owner,
                            "state": "FILLED", "filled_quantity": "0.001", "created_at": to_iso(at)})
        bkey = repo.insert_broker_order(account=ACCOUNT, order_no=f"{n}{side}", at=at, intent_id=iid, instrument=inst,
                                        side=side, quantity=D("0.001"), origin="INTERNAL_NEW")
        repo.insert_fill(bkey, f"f{n}{side}", D("0.001"), D("100"), at)
    repo.insert_ownership(ACCOUNT, inst, owner, f"i-{n}-BUY", entry_at)
    own = repo.select("SELECT ownership_id FROM ownership WHERE intent_id=?", (f"i-{n}-BUY",))[0]["ownership_id"]
    repo.set_ownership_state(own, "CONFIRMED")
    repo.set_ownership_state(own, "RELEASED", reason, exit_at)


def _db(tmp: Path, *, trips: tuple[tuple[datetime, datetime], ...] = (), reset_at: datetime | None = None,
        days: int = 200, extra=None) -> Path:  # type: ignore[no-untyped-def]
    db = tmp / "var" / "paper" / "trading.db"
    repo = Repository(db, Mode.PAPER, order_identity=OrderIdentity.SYMBOL)
    try:
        with repo.tx():
            repo.audit(START, "PAPER_PERIOD_START", "user", ACCOUNT, {"at": "가상"})
            if reset_at is not None:
                repo.audit(reset_at, "PAPER_PERIOD_RESTART", "user", ACCOUNT, {"at": "가상"})
            for k in range(days * 24):                           # 가상 대조 1시간 간격 (가동 추정용 — 이 시험의 대상 아님)
                repo.insert_reconciliation(ACCOUNT, "PERIODIC", "MATCH", {}, START + timedelta(hours=k, minutes=1))
            for n, (a, b) in enumerate(trips, start=1):
                _trip(repo, n, a, b)
            if extra is not None:
                extra(repo)
    finally:
        repo.close()
    return db


def _table(db: Path, as_of: datetime, cls: PeriodClass | None):  # type: ignore[no-untyped-def]
    return build_m2_paper1(db, ACCOUNT, as_of, [], reconcile_gap=timedelta(hours=2),
                           classes=[] if cls is None else [cls])


def _row(db: Path, as_of: datetime, cls: PeriodClass):  # type: ignore[no-untyped-def]
    t = _table(db, as_of, cls)
    assert len(t.strategies) == 1
    return t, t.strategies[0]


# --- V-312 저빈도 예외(CP-038 D-238) -------------------------------------------------------------------------------

def test_v312_90_days_without_round_trip_is_extending(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _, r = _row(db, START + timedelta(days=90), EXC)
    assert (r.rule, r.min_days, r.cap_days, r.utc_days, r.round_trips) == ("CP-038 D-238", 90, 180, 90, 0)
    assert r.status.startswith("연장 중")
    _, early = _row(db, START + timedelta(days=89), EXC)
    assert early.status.startswith("최소 기간 전")


def test_v312_round_trip_on_day_95_meets_period_condition(tmp_path: Path) -> None:
    db = _db(tmp_path, trips=((START + timedelta(days=93), START + timedelta(days=95)),))
    _, before = _row(db, START + timedelta(days=94, hours=23), EXC)         # 청산 체결 전 — 왕복 미완
    assert before.round_trips == 0 and before.status.startswith("연장 중")
    _, after = _row(db, START + timedelta(days=95, hours=1), EXC)
    assert after.round_trips == 1 and after.status.startswith("기간 조건 충족")


def test_v312_180_days_without_round_trip_is_undeterminable(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _, r = _row(db, START + timedelta(days=180), EXC)
    assert r.status.startswith("판단 불가(거래 미관찰)") and "사용자" in r.status
    _, r179 = _row(db, START + timedelta(days=179), EXC)
    assert r179.status.startswith("연장 중")


def test_v312_reset_recounts_days_and_round_trips(tmp_path: Path) -> None:
    reset = START + timedelta(days=10)
    db = _db(tmp_path, trips=((START + timedelta(days=2), START + timedelta(days=4)),), reset_at=reset)
    t, r = _row(db, reset + timedelta(days=90), EXC)
    assert t.period_start == reset and t.resets == 1
    assert r.utc_days == 90 and r.round_trips == 0                           # 리셋 전 왕복은 세지 않는다
    assert r.status.startswith("연장 중")


def test_v312_exception_without_d278_and_d278_without_exception_use_longer_rule(tmp_path: Path) -> None:
    """[v0.2] 저빈도 예외지만 D-278 저빈도가 아님 → CP-038 D-238(90일 + 왕복 1건, 상한 180일 — 둘 중 긴 쪽).
    D-278 저빈도지만 예외 판정 아님 → CP-041 D-279(90일, 왕복 0건이면 판정 보류 — 상한·자동 판단 불가 없음)."""
    db = _db(tmp_path)
    exc_only = PeriodClass("t-lf", 1, low_frequency=False, exception=True, approved_by="A-900")
    _, a = _row(db, START + timedelta(days=180), exc_only)
    assert (a.rule, a.min_days, a.cap_days) == ("CP-038 D-238", 90, 180) and a.status.startswith("판단 불가")
    _, b = _row(db, START + timedelta(days=180), LOW)
    assert (b.rule, b.min_days, b.cap_days) == ("CP-041 D-279", 90, None)
    assert b.status.startswith("판정 보류(왕복 0)")
    unknown_d278 = PeriodClass("t-lf", 1, low_frequency=None, exception=True, approved_by="A-900")
    _, c = _row(db, START + timedelta(days=90), unknown_d278)                # 예외 판정이면 D-238이 이미 더 길다
    assert c.rule == "CP-038 D-238" and c.min_days == 90


def test_v312_d240_label_only_for_exception_and_no_verdict_words(tmp_path: Path) -> None:
    db = _db(tmp_path)
    md = render_m2_paper1(_table(db, START + timedelta(days=90), EXC))
    assert LF_LABEL == "저빈도 예외(CP-038)" and LF_LABEL in md
    assert "CP-038 D-238" in md and "왕복" in md and "180" in md
    assert "합격" not in md.replace("합격 여부는", "").replace("합격 불가", "") and "불합격" not in md
    md_low = render_m2_paper1(_table(db, START + timedelta(days=90), LOW))
    assert LF_LABEL not in md_low and "CP-041 D-279" in md_low


def test_v312_dust_release_with_sell_counts_release_without_sell_does_not(tmp_path: Path) -> None:
    def extra(repo: Repository) -> None:
        _trip(repo, 10, START + timedelta(days=5), START + timedelta(days=6), reason=DUST_RELEASE_REASON)
        _trip(repo, 11, START + timedelta(days=7), START + timedelta(days=8), sell=False, reason=DUST_RELEASE_REASON)
        _trip(repo, 12, START + timedelta(days=9), START + timedelta(days=10), reason="외부 거래로 보유 0")
        _trip(repo, 13, START + timedelta(days=11), START + timedelta(days=12), owner="other:1")
    db = _db(tmp_path, extra=extra)
    _, r = _row(db, START + timedelta(days=20), EXC)
    assert r.round_trips == 1


# --- V-350 D-279 저빈도·28일 전략 ----------------------------------------------------------------------------------

def test_v350_low_frequency_90_days_with_reset_and_zero_round_trips_on_hold(tmp_path: Path) -> None:
    reset = START + timedelta(days=3)
    db = _db(tmp_path, trips=((START + timedelta(days=1), START + timedelta(days=2)),), reset_at=reset)
    _, r = _row(db, reset + timedelta(days=89), LOW)
    assert r.utc_days == 89 and r.status.startswith("최소 기간 전")
    _, r = _row(db, reset + timedelta(days=90), LOW)
    assert (r.utc_days, r.round_trips, r.min_days) == (90, 0, 90)
    assert r.status.startswith("판정 보류(왕복 0)") and "D-279" in r.status


def test_v350_28_day_strategy_keeps_v303_values(tmp_path: Path) -> None:
    """28일 전략(D-232): 전략별 칸을 붙여도 V-303 표 값(기간·리셋·UTC일·실행률·정지)은 그대로."""
    from tests.scenario.test_t055_m2_schedule_review import ACCOUNT as A303, GAP, START as S303, _db as db303
    from trading_system.reporting.m2_paper1 import EvalSchedule
    db = db303(tmp_path)
    as_of = S303 + timedelta(days=28, hours=1)
    sched = [EvalSchedule(times_utc=(time(1, 0),))]
    base = build_m2_paper1(db, A303, as_of, sched, reconcile_gap=GAP)
    withc = build_m2_paper1(db, A303, as_of, sched, reconcile_gap=GAP, classes=[GEN])
    for f in ("period_start", "resets", "utc_days", "planned", "planned_in_stops", "executed", "rate", "uptime",
              "down_planned", "down_unplanned", "gaps", "restarts", "k8_gave_up", "k8_rejected"):
        assert getattr(base, f) == getattr(withc, f), f
    assert base.strategies == [] and render_m2_paper1(base).count("PAPER-1 기간 조건") == 0
    r = withc.strategies[0]
    assert (r.rule, r.min_days, r.cap_days) == ("CP-037 D-232", 28, None) and r.status.startswith("기간 조건 충족")
    md = render_m2_paper1(withc)
    assert md.replace(_section(md), "") == render_m2_paper1(base)          # 종전 표 글자 그대로 + 전략별 칸만 추가


def _section(md: str) -> str:
    i = md.index("\n\n### PAPER-1 기간 조건")
    j = md.find("\n\n가동 시간은", i)
    return md[i:j]


def test_unclassified_strategy_shows_unclassified(tmp_path: Path) -> None:
    db = _db(tmp_path)
    none = PeriodClass("t-lf", 1, low_frequency=None, exception=False, approved_by=None, note="분류 파일 없음")
    t, r = _row(db, START + timedelta(days=120), none)
    assert r.rule == "미분류" and r.min_days is None and r.status.startswith("미분류")
    assert "분류 파일 없음" in render_m2_paper1(t)


@pytest.mark.parametrize("cls,days,rt,head", [
    (GEN, 27, 0, "최소 기간 전"), (GEN, 28, 0, "기간 조건 충족"),
    (LOW, 90, 1, "기간 조건 충족"), (LOW, 200, 0, "판정 보류(왕복 0)"),
    (EXC, 90, 1, "기간 조건 충족"), (EXC, 60, 1, "최소 기간 전"), (EXC, 200, 1, "기간 조건 충족"),
])
def test_period_status_table(cls: PeriodClass, days: int, rt: int, head: str) -> None:
    assert period_status(cls, days, rt).status.startswith(head)
