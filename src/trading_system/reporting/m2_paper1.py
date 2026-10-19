"""M2 PAPER-1 집계 표 (CP-037 D-232, T-055, V-303) — 읽기 전용. **판정은 하지 않고 표만** 만든다(합격 판정은 사용자, D-232).

표 항목과 출처(모두 M2 DB):
- PAPER 기간 시작: 감사 PAPER_PERIOD_START·PAPER_PERIOD_RESTART(리셋 확인 — A-062①, CP-036 D-210) 중 마지막. 없으면 첫 대조
  기록 시각(표에 "시작 기록 없음"으로 표시). 리셋 횟수 = PAPER_PERIOD_RESTART 수.
- UTC일 수: 기간 시작 뒤 지난 UTC 일 경계(00:00) 수(D-232 "UTC 일 경계 28회").
- 가동·정지 시간: run은 대조를 주기적으로(설정 reconcile_minutes, M2 기본 60초) 기록한다 → 대조 기록 사이 간격이 기준(기본 3분)
  이하인 구간 = 가동, 넘으면 정지 구간 [DP]. 정지 구간에 감독 감사 PLANNED_STOP·SUPERVISOR_STOP(구간 시작 5분 전부터)이 있으면
  계획 정지, 아니면 비계획 정지.
- 판정 실행률: 분모 = 배포 일정(정해진 UTC 시각 또는 주기 격자)의 예정 판정 중 계획 정지 구간 밖인 것, 분자 = 기간 안 감사
  EVALUATION 수(늦은 판정은 금지라 기록 시각 ≈ 예정 시각). 일정을 모르면 "일정 미확인".
- 자동 재시작: 감독 감사 RESTART(시각·사유). K8 자동 재개 실패: K8_AUTO_RESUME_GAVE_UP 수, AUTO 채널 거부 명령 수.
- 신호 일치율(D-220 저장 봉 기준 재계산): **미계산** — 저장 봉으로 전략을 다시 돌리는 재계산 도구가 아직 없다(DP, T-M2-17 전 필요).
- [CP-041 T-072·CP-038 T-059] 전략별 PAPER-1 기간 조건(classes를 줄 때만 — 안 주면 표는 종전 그대로, V-303):
  * 규칙: 저빈도 예외(CP-038 판정표 "저빈도 예외(CP-038)") → **CP-038 D-238**(최소 90 UTC일 + PAPER 안 완결 왕복 1건, 없으면
    왕복 완료까지 연장, 상한 180 UTC일 → "판단 불가(거래 미관찰)"·사용자 결정). D-278 저빈도이지만 예외 판정이 아님 → **CP-041
    D-279**(최소 90 UTC일, 90일 뒤 왕복 0건이면 "판정 보류" → 사용자 결정, 자동 연장·상한 없음). 일반(D-278 저빈도 아님) →
    **CP-037 D-232**(최소 28 UTC일). 분류 없음 → "미분류". 두 규칙이 겹치면(예외이면서 D-278 저빈도) 더 구체적이고 긴 D-238을
    적용한다(CP-041 D-279 "둘 중 긴 기간", CP-038 D-238 v0.2).
  * UTC일 수·왕복은 PAPER 기간 시작(리셋 확인이면 그 시점)부터 다시 센다(D-232 리셋 재계산).
  * 완결 왕복(04 D-078): 그 전략(owner = "전략:버전") 소유권이 기간 안에 생겨 "전량 청산" 또는 "dust 확정"으로 해제됐고, 그 사이에
    같은 owner·심볼의 매수 체결과 매도 체결이 모두 있는 것. 리셋·외부 거래·체결 0주 해제는 왕복이 아니다.
  * 판정 실행률·안전성 등 나머지 D-232 기준은 모든 규칙에 그대로 적용된다(이 칸은 기간 조건만 표시 — 판정은 사용자).
민감값: 계좌 식별·금액 없음.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from trading_system.domain.clock import from_iso, to_iso
from trading_system.domain.enums import Mode
from trading_system.domain.minute_series import PeriodGrid
from trading_system.storage.repository import OrderIdentity, Repository

SUPERVISOR_AUDIT = "M2_SUPERVISOR"            # app/m2_supervisor.AUDIT_KIND와 같은 값 (reporting은 app을 import하지 않는다)
PLANNED_EVENTS = frozenset({"PLANNED_STOP", "SUPERVISOR_STOP"})
PLANNED_LEAD = timedelta(minutes=5)
DEFAULT_GAP = timedelta(minutes=3)
LF_LABEL = "저빈도 예외(CP-038)"          # app/low_frequency.LABEL과 같은 값 (D-240 표시 — reporting은 app을 import하지 않는다)
FULL_EXIT_REASONS = ("전량 청산", "dust 확정")   # execution/executor 해제 사유·storage.repository.DUST_RELEASE_REASON
SIGNAL_MATCH_NOTE = "미계산 — 저장 봉(D-220) 재계산 도구 없음(T-M2-17 전 필요, DP)"


@dataclass(frozen=True)
class EvalSchedule:
    """배포 하나의 판정 일정: 정해진 UTC 시각(매일) 또는 주기 격자(CAP-TRG-02)."""
    times_utc: tuple[time, ...] = ()
    period: PeriodGrid | None = None

    def planned(self, start: datetime, end: datetime) -> list[datetime]:
        out: list[datetime] = []
        d = start.astimezone(UTC).date()
        while d <= end.astimezone(UTC).date():
            if self.period is not None:
                day = list(self.period.day_times(d))
            else:
                day = [datetime.combine(d, t, tzinfo=UTC) for t in self.times_utc]
            out += [x for x in day if start <= x <= end]
            d += timedelta(days=1)
        return out


@dataclass(frozen=True)
class PeriodClass:
    """배포 전략 하나의 PAPER 기간 분류(PAPER 전환 승인 때 함께 승인 — CP-041 D-278 (4)). low_frequency None = D-278 미분류."""
    strategy_id: str
    version: int
    low_frequency: bool | None
    exception: bool = False            # CP-038 판정표 "통과(저빈도 예외, CP-038…)"
    approved_by: str | None = None
    note: str = ""                     # 분류 파일 없음·오류 등

    @property
    def owner(self) -> str:
        return f"{self.strategy_id}:{self.version}"


@dataclass(frozen=True)
class StrategyPeriod:
    owner: str
    rule: str                  # "CP-038 D-238" | "CP-041 D-279" | "CP-037 D-232" | "미분류"
    min_days: int | None
    cap_days: int | None
    utc_days: int
    round_trips: int
    status: str
    label: str                 # 저빈도 예외면 LF_LABEL (D-240), 아니면 ""
    cls: PeriodClass


def period_status(cls: PeriodClass, utc_days: int, round_trips: int) -> StrategyPeriod:
    """기간 조건 상태 문구(판정 아님). 규칙 선택은 모듈 머리말."""
    d, rt = utc_days, round_trips
    rule: str
    lo: int | None
    cap: int | None
    if cls.exception:
        rule, lo, cap = "CP-038 D-238", 90, 180
        if d < lo:
            st = f"최소 기간 전 ({d}/{lo} UTC일, 완결 왕복 {rt}건)"
        elif rt >= 1:
            st = f"기간 조건 충족 ({lo} UTC일 + 완결 왕복 {rt}건)"
        elif d < cap:
            st = f"연장 중 — 완결 왕복 1건 완료까지 (상한 {cap} UTC일, 현재 {d}일)"
        else:
            st = (f"판단 불가(거래 미관찰) — 상한 {cap} UTC일 도달, 완결 왕복 0건. 연장·중단은 사용자 결정"
                  "(합격 불가, CP-038 D-238)")
    elif cls.low_frequency is True:
        rule, lo, cap = "CP-041 D-279", 90, None
        if d < lo:
            st = f"최소 기간 전 ({d}/{lo} UTC일, 완결 왕복 {rt}건)"
        elif rt >= 1:
            st = f"기간 조건 충족 ({lo} UTC일, 완결 왕복 {rt}건)"
        else:
            st = (f"판정 보류(왕복 0) — {d} UTC일 경과, 완결 왕복 0건. 30일 단위 연장 또는 중단은 사용자 결정"
                  "(자동 연장 없음, D-279)")
    elif cls.low_frequency is False:
        rule, lo, cap = "CP-037 D-232", 28, None
        st = f"최소 기간 전 ({d}/{lo} UTC일)" if d < lo else f"기간 조건 충족 ({lo} UTC일)"
    else:
        rule, lo, cap = "미분류", None, None
        st = "미분류 — 최소 기간 미확정 (CP-041 D-278 분류는 PAPER 전환 승인 때 함께 승인)"
    return StrategyPeriod(cls.owner, rule, lo, cap, d, rt, st, LF_LABEL if cls.exception else "", cls)


@dataclass
class Paper1Table:
    period_start: datetime
    period_basis: str
    as_of: datetime
    resets: int
    utc_days: int
    uptime: timedelta
    down_planned: timedelta
    down_unplanned: timedelta
    gaps: list[tuple[datetime, datetime, bool]]
    planned: int
    planned_in_stops: int
    executed: int
    skipped: int
    rate: float | None
    schedule_known: bool
    restarts: list[tuple[datetime, str]] = field(default_factory=list)
    k8_gave_up: int = 0
    k8_rejected: int = 0
    signal_match: str = SIGNAL_MATCH_NOTE
    strategies: list[StrategyPeriod] = field(default_factory=list)


def build_m2_paper1(db: Path, account: str, as_of: datetime, schedules: Sequence[EvalSchedule], *,
                    reconcile_gap: timedelta = DEFAULT_GAP, classes: Sequence[PeriodClass] = ()) -> Paper1Table:
    if not db.is_file():
        raise FileNotFoundError(db)
    repo = Repository(db, Mode.PAPER, order_identity=OrderIdentity.SYMBOL)
    try:
        t = _build(repo, account, as_of, schedules, reconcile_gap)
        t.strategies = [period_status(c, t.utc_days, _round_trips(repo, account, c.owner, t.period_start, as_of))
                        for c in classes]
        return t
    finally:
        repo.close()


def _round_trips(repo: Repository, account: str, owner: str, start: datetime, as_of: datetime) -> int:
    """기간 안 완결 왕복 수(04 D-078) — 모듈 머리말 정의."""
    marks = ",".join("?" for _ in FULL_EXIT_REASONS)
    fill_exists = ("EXISTS (SELECT 1 FROM fill f JOIN broker_order b ON f.order_key=b.order_key "
                   "JOIN intent i ON i.intent_id=b.intent_id WHERE i.owner=o.owner AND i.account=o.account "
                   "AND i.instrument=o.instrument AND i.side=? AND f.at>=o.created_at AND f.at<=o.released_at)")
    row = repo.select(
        f"SELECT COUNT(*) AS n FROM ownership o WHERE o.account=? AND o.owner=? AND o.state='RELEASED' "
        f"AND o.released_reason IN ({marks}) AND o.created_at>=? AND o.released_at<=? AND {fill_exists} AND {fill_exists}",
        (account, owner, *FULL_EXIT_REASONS, to_iso(start.astimezone(UTC)), to_iso(as_of.astimezone(UTC)), "BUY", "SELL"))
    return int(row[0]["n"])


def _audits(repo: Repository, kinds: Sequence[str], until: datetime) -> list[dict]:
    marks = ",".join("?" for _ in kinds)
    return repo.select(f"SELECT at, kind, summary FROM audit_event WHERE kind IN ({marks}) AND at<=? ORDER BY seq",
                       (*kinds, to_iso(until.astimezone(UTC))))


def _build(repo: Repository, account: str, as_of: datetime, schedules: Sequence[EvalSchedule],
           gap_limit: timedelta) -> Paper1Table:
    periods = _audits(repo, ("PAPER_PERIOD_START", "PAPER_PERIOD_RESTART"), as_of)
    resets = sum(1 for p in periods if p["kind"] == "PAPER_PERIOD_RESTART")
    recon = [from_iso(r["created_at"]) for r in repo.select(
        "SELECT created_at FROM reconciliation WHERE account=? AND created_at<=? ORDER BY created_at",
        (account, to_iso(as_of.astimezone(UTC))))]
    if periods:
        start, basis = from_iso(periods[-1]["at"]), f"{periods[-1]['kind']} 기록"
    elif recon:
        start, basis = recon[0], "PAPER 기간 시작 기록 없음 — 첫 대조 기록부터"
    else:
        start, basis = as_of, "PAPER 기간 시작 기록·대조 기록 없음"
    utc_days = max(0, (as_of.astimezone(UTC).date() - start.astimezone(UTC).date()).days)
    sup = [(from_iso(r["at"]), json.loads(r["summary"])) for r in _audits(repo, (SUPERVISOR_AUDIT,), as_of)
           if from_iso(r["at"]) >= start]
    planned_marks = [at for at, s in sup if s.get("event") in PLANNED_EVENTS]
    points = [t for t in recon if t >= start]
    gaps: list[tuple[datetime, datetime, bool]] = []
    edges = [start, *points, as_of]
    uptime = timedelta(0)
    for a, b in zip(edges, edges[1:]):
        if b - a > gap_limit:
            gaps.append((a, b, any(a - PLANNED_LEAD <= m <= b for m in planned_marks)))
        else:
            uptime += b - a
    down_planned = sum((b - a for a, b, p in gaps if p), timedelta(0))
    down_unplanned = sum((b - a for a, b, p in gaps if not p), timedelta(0))
    planned_times = [t for s in schedules for t in s.planned(start, as_of)]
    in_stops = sum(1 for t in planned_times if any(a <= t <= b for a, b, p in gaps if p))
    evals = _audits(repo, ("EVALUATION", "EVALUATION_SKIPPED"), as_of)
    executed = sum(1 for e in evals if e["kind"] == "EVALUATION" and from_iso(e["at"]) >= start)
    skipped = sum(1 for e in evals if e["kind"] == "EVALUATION_SKIPPED" and from_iso(e["at"]) >= start)
    denominator = len(planned_times) - in_stops
    rate = executed / denominator if schedules and denominator > 0 else None
    restarts = [(at, str(s.get("reason", ""))) for at, s in sup if s.get("event") == "RESTART"]
    gave_up = sum(1 for _, s in sup if s.get("event") == "K8_AUTO_RESUME_GAVE_UP")
    rejected = repo.select("SELECT COUNT(*) AS n FROM ops_command WHERE channel='AUTO' AND status='REJECTED' "
                           "AND created_at>=? AND created_at<=?",
                           (to_iso(start.astimezone(UTC)), to_iso(as_of.astimezone(UTC))))[0]["n"]
    return Paper1Table(start, basis, as_of, resets, utc_days, uptime, down_planned, down_unplanned, gaps,
                       len(planned_times), in_stops, executed, skipped, rate, bool(schedules), restarts, gave_up,
                       int(rejected))


def _hours(d: timedelta) -> str:
    return f"{d.total_seconds() / 3600:.1f}시간"


def _utc(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")


def _strategy_lines(rows: Sequence[StrategyPeriod]) -> list[str]:
    out = ["", "### PAPER-1 기간 조건 (전략별 — CP-041 D-278·D-279, CP-038 D-238·D-240. 표시만, 판정은 사용자)", "",
           "| 전략 | 표시 | 분류(D-278) | 적용 규칙 | 최소 기간 | 상한 | UTC일 | 완결 왕복 | 상태 | 분류 승인 |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        c = r.cls
        d278 = {True: "저빈도", False: "일반", None: "미분류"}[c.low_frequency]
        lo = "—" if r.min_days is None else f"{r.min_days} UTC일"
        cap = "—" if r.cap_days is None else f"{r.cap_days} UTC일"
        note = f" — {c.note}" if c.note else ""
        out.append(f"| {r.owner} | {r.label or '—'} | {d278} | {r.rule} | {lo} | {cap} | {r.utc_days} | "
                   f"{r.round_trips} | {r.status} | {c.approved_by or '—'}{note} |")
    out += ["", "적용 규칙: 저빈도 예외 = CP-038 D-238(90일 + 왕복 1건, 상한 180일), D-278 저빈도만 = CP-041 D-279(90일, "
            "왕복 0건이면 판정 보류), 일반 = CP-037 D-232(28일). 예외이면서 D-278 저빈도면 더 긴 D-238. UTC일·왕복은 리셋 확인 "
            "뒤부터 다시 센다."]
    return out


def render_m2_paper1(t: Paper1Table) -> str:
    """마크다운 표. 판정(합격·불합격)은 쓰지 않는다 — D-232 기준과의 비교는 사용자가 한다."""
    if t.schedule_known and t.rate is not None:
        rate = (f"{t.rate * 100:.1f}% (실행 {t.executed} / 분모 {t.planned - t.planned_in_stops} = 예정 {t.planned} − "
                f"계획 정지 중 예정 {t.planned_in_stops})")
    elif t.schedule_known:
        rate = f"분모 0 — 예정 {t.planned}, 계획 정지 중 {t.planned_in_stops}"
    else:
        rate = f"일정 미확인 — 배포 일정을 읽지 못함(실행 {t.executed}건)"
    rows = [
        ("PAPER 기간 시작", f"{_utc(t.period_start)} UTC ({t.period_basis})"),
        ("리셋(기간 다시 계산)", f"{t.resets}회"),
        ("UTC일 수(일 경계 통과)", f"{t.utc_days}"),
        ("판정 실행률", rate),
        ("판정 건너뜀 기록", f"{t.skipped}건"),
        ("가동 시간", _hours(t.uptime)),
        ("계획 정지", f"{_hours(t.down_planned)} ({sum(1 for g in t.gaps if g[2])}구간 — 판정 실행률 분모에서 뺌)"),
        ("비계획 정지", f"{_hours(t.down_unplanned)} ({sum(1 for g in t.gaps if not g[2])}구간)"),
        ("자동 재시작", f"{len(t.restarts)}회" + ("" if not t.restarts else
                                              " — " + ", ".join(f"{_utc(a)} {r}" for a, r in t.restarts))),
        ("K8 자동 재개 실패", f"12회 실패 중단 {t.k8_gave_up}회, 거부된 자동 재개 명령 {t.k8_rejected}건"),
        ("신호 일치율(저장 봉 기준)", t.signal_match),
    ]
    lines = [f"## M2 PAPER-1 집계 ({_utc(t.as_of)} UTC 기준, CP-037 D-232)", "",
             "판정은 하지 않는다 — 합격 여부는 D-232 기준(최소 28 UTC일·판정 실행률 90% 이상·공통 안전성 0건 등)과 "
             "비교해 사용자가 정한다.", "", "| 항목 | 값 |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    if t.gaps:
        lines += ["", "| 정지 구간 시작(UTC) | 끝(UTC) | 길이 | 구분 |", "|---|---|---|---|"]
        lines += [f"| {_utc(a)} | {_utc(b)} | {_hours(b - a)} | {'계획' if p else '비계획'} |" for a, b, p in t.gaps]
    if t.strategies:
        lines += _strategy_lines(t.strategies)
    lines += ["", "가동 시간은 대조 기록 간격으로 추정한다(간격이 기준 이하인 구간 = 가동). 감독 밖에서 손으로 돌린 run도 대조를 "
              "남기므로 포함된다."]
    return "\n".join(lines) + "\n"


__all__ = ["EvalSchedule", "LF_LABEL", "Paper1Table", "PeriodClass", "StrategyPeriod", "build_m2_paper1",
           "period_status", "render_m2_paper1", "SIGNAL_MATCH_NOTE"]
