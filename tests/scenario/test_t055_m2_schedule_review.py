"""CP-037 T-055 — 예약 작업 스크립트 M2판(V-302, 정적 검사만 — 실행·등록하지 않음)·M2 PAPER-1 집계(V-303, 가상 기록).

예약 작업 스크립트는 글자로만 검사한다(작업 스케줄러를 부르지 않는다 — 등록은 사용자, D-231). 집계는 가상 DB 기록으로 표만
확인한다(판정은 하지 않음). 모든 값은 가상이다.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

from trading_system.app.m2_supervisor import AUDIT_KIND, SUPERVISOR_ACTOR
from trading_system.domain.enums import Mode
from trading_system.domain.minute_series import PeriodGrid
from trading_system.reporting.m2_paper1 import EvalSchedule, build_m2_paper1, render_m2_paper1
from trading_system.storage.repository import OrderIdentity, Repository

REPO = Path(__file__).resolve().parents[2]
REG = REPO / "scripts" / "schedule-register-m2.ps1"
UNREG = REPO / "scripts" / "schedule-unregister-m2.ps1"
M1_REG = REPO / "scripts" / "schedule-register.ps1"
M1_UNREG = REPO / "scripts" / "schedule-unregister.ps1"


def _code(path: Path) -> str:
    """주석(<# … #>, # …)을 뺀 실행 줄만 — 설명 문구가 아니라 실제 정의를 검사한다."""
    text = path.read_text(encoding="utf-8-sig")
    text = re.sub(r"<#.*?#>", "", text, flags=re.S)
    return "\n".join(ln.split(" #")[0] for ln in text.splitlines() if not ln.lstrip().startswith("#"))


# --- V-302 예약 작업 스크립트 (정적 검사) ---------------------------------------------------------------

def test_v302_task_names_have_m2_prefix_and_differ_from_m1() -> None:
    code = _code(REG)
    assert "$Prefix = 'TradingSystem-PAPER-M2-'" in code
    assert "$SupTask = $Prefix + 'Supervisor'" in code and "$ClockTask = $Prefix + 'ClockSync'" in code
    for m1 in ("TradingSystem-PAPER-Autopilot", "'Autopilot'"):
        assert m1 not in code
    m1 = _code(M1_REG)                                  # M1 작업 2개 정의 그대로 (이 작업에서 바꾸지 않음)
    assert "$Prefix = 'TradingSystem-PAPER-'" in m1
    assert "$ClockTask = $Prefix + 'ClockSync'" in m1 and "$AutoTask = $Prefix + 'Autopilot'" in m1
    assert "M2" not in m1 and "M2" not in _code(M1_UNREG)


def test_v302_path_is_m2_checkout_and_mode_line_checked() -> None:
    code = _code(REG)
    assert "(Split-Path -Leaf $Root) -ne 'm2-binance'" in code
    assert "config\\m2\\app.paper.toml" in code
    assert "scripts\\m2-supervisor.py" in code
    assert "'^\\s*TRADE_MODE\\s*='" in code and "'TRADE_MODE=PAPER'" in code
    assert "--mode PAPER --market M2" in code
    assert "BaseUtcOffset" in code                      # 시계 동기화 시각(08:52)은 KST 전제 — UTC+9 아니면 멈춤


def test_v302_interactive_no_password_no_secrets_on_command_line() -> None:
    code = _code(REG)
    assert code.count("-LogonType Interactive") == 2
    for bad in ("-Password", "S4U", "ServiceAccount", "Password", "-RunLevel Highest -LogonType Password"):
        assert bad not in code
    for secret in ("BINANCE_", "TOKEN", "SECRET", "CHAT_ID", ".env.paper"):
        assert secret not in code
    sup = code[code.index("$supArgs"):code.index("Register-ScheduledTask -TaskName $SupTask")]
    assert "-RunLevel Limited" in sup


def test_v302_supervisor_no_time_limit_logon_and_daily_triggers() -> None:
    code = _code(REG)
    sup = code[code.index("$supArgs"):code.index("Register-ScheduledTask -TaskName $SupTask")]
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in sup
    assert "-AtLogOn -User $User" in sup and "-Daily -At $DailyAt" in sup
    assert "-MultipleInstances IgnoreNew" in sup
    clock = code[code.index("$clockAction"):code.index("Register-ScheduledTask -TaskName $ClockTask")]
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes 5)" in clock and "'/resync'" in clock and "'08:52'" in clock


def test_v302_existing_name_stops_before_any_registration() -> None:
    code = _code(REG)
    check = code.index("Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue")
    first_register = code.index("Register-ScheduledTask")
    assert check < first_register
    block = code[check:code.index("\n}", check)]
    assert "throw" in block
    assert "Unregister-ScheduledTask" not in code and "Set-ScheduledTask" not in code


def test_v302_unregister_only_m2_names() -> None:
    code = _code(UNREG)
    assert "$Prefix = 'TradingSystem-PAPER-M2-'" in code
    assert "@(($Prefix + 'Supervisor'), ($Prefix + 'ClockSync'))" in code
    assert "Unregister-ScheduledTask -TaskName $name -Confirm:$false" in code
    assert "Autopilot" not in code
    assert "-TaskName ($Prefix + '*')" not in code.split("Unregister-ScheduledTask")[1].split("\n")[0]


def test_v302_scripts_require_admin_and_no_agent_execution_note() -> None:
    for path in (REG, UNREG):
        text = path.read_text(encoding="utf-8-sig")
        assert "#Requires -RunAsAdministrator" in text
        assert "에이전트는 이 스크립트를 실행하지 않는다" in text


# --- V-303 M2 PAPER-1 집계 (가상 기록) ------------------------------------------------------------------

ACCOUNT = "BN-PAPER-01"
START = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
STEP = timedelta(minutes=10)           # 가상 대조 간격(실운영 60초 — 시험은 기록 수를 줄이려고 10분, 간격 기준도 맞춤)
GAP = timedelta(minutes=25)


def _db(tmp: Path) -> Path:
    db = tmp / "var" / "paper" / "trading.db"
    repo = Repository(db, Mode.PAPER, order_identity=OrderIdentity.SYMBOL)
    try:
        with repo.tx():
            # 처음 기간 시작 → 리셋 1회(기간 다시 계산)
            repo.audit(START - timedelta(days=5), "PAPER_PERIOD_START", "user", ACCOUNT, {"at": "가상"})
            repo.audit(START, "PAPER_PERIOD_RESTART", "user", ACCOUNT, {"at": "가상"})
            end = START + timedelta(days=28, hours=1)
            planned = (START + timedelta(days=3, hours=2), START + timedelta(days=3, hours=4))     # 계획 정지 2시간
            unplanned = (START + timedelta(days=10), START + timedelta(days=10, hours=1))          # 비계획 1시간
            t = START + timedelta(minutes=1)
            while t < end:
                if not (planned[0] < t < planned[1] or unplanned[0] < t < unplanned[1]):
                    repo.insert_reconciliation(ACCOUNT, "PERIODIC", "MATCH", {}, t)
                t += STEP
            repo.audit(planned[0] + timedelta(minutes=1), AUDIT_KIND, SUPERVISOR_ACTOR, None,
                       {"event": "PLANNED_STOP", "code": 0})
            repo.audit(unplanned[0] + timedelta(minutes=1), AUDIT_KIND, SUPERVISOR_ACTOR, None,
                       {"event": "RUN_EXIT", "code": 1})
            for k, at in enumerate((START + timedelta(days=10, minutes=5), START + timedelta(days=12)), start=1):
                repo.audit(at, AUDIT_KIND, SUPERVISOR_ACTOR, None, {"event": "RESTART", "n": k, "reason": "코드 1"})
            repo.audit(START + timedelta(days=12, hours=1), AUDIT_KIND, SUPERVISOR_ACTOR, None,
                       {"event": "K8_AUTO_RESUME_GAVE_UP", "attempts": 12})
            repo.insert_command("auto-x", "AUTO", SUPERVISOR_ACTOR, "RESUME", {"halt_type": "K8"}, START + timedelta(days=12))
            repo.finish_command("auto-x", "REJECTED", "가상 거부", START + timedelta(days=12))
            # 판정: 매일 01:00 UTC 1회 예정. 계획 정지 중 예정 없음, 비계획 정지(10일차 00:00~01:00)에 01:00 예정이 겹치지
            # 않게 — 별도로 20일차 판정 1건 누락
            d = START.date() + timedelta(days=1)
            while datetime.combine(d, time(1, 0), tzinfo=UTC) < end:
                at = datetime.combine(d, time(1, 0), tzinfo=UTC)
                if d != (START + timedelta(days=20)).date():
                    repo.audit(at + timedelta(seconds=2), "EVALUATION", "engine", f"ev-{d}", {"strategy": "s@1"})
                d += timedelta(days=1)
            # 기간 시작 전 기록은 세지 않는다
            repo.audit(START - timedelta(days=2), "EVALUATION", "engine", "ev-old", {"strategy": "s@1"})
    finally:
        repo.close()
    return db


def test_v303_table_counts_without_verdict(tmp_path: Path) -> None:
    db = _db(tmp_path)
    as_of = START + timedelta(days=28, hours=1)
    t = build_m2_paper1(db, ACCOUNT, as_of, [EvalSchedule(times_utc=(time(1, 0),))], reconcile_gap=GAP)
    assert t.period_start == START and t.resets == 1
    assert t.utc_days == 28                                     # 리셋 확인 시점부터 다시 계산
    assert t.planned == 28 and t.planned_in_stops == 0 and t.executed == 27
    assert t.rate is not None and abs(t.rate - 27 / 28) < 1e-9
    assert len(t.restarts) == 2 and t.k8_gave_up == 1 and t.k8_rejected == 1
    assert timedelta(hours=1, minutes=50) <= t.down_planned <= timedelta(hours=2, minutes=20)
    assert timedelta(minutes=50) <= t.down_unplanned <= timedelta(hours=1, minutes=20)
    assert abs((t.uptime + t.down_planned + t.down_unplanned) - (as_of - START)) <= STEP
    md = render_m2_paper1(t)
    for part in ("UTC일 수", "판정 실행률", "계획 정지", "비계획 정지", "자동 재시작", "K8 자동 재개", "신호 일치율",
                 "판정은 하지 않는다"):
        assert part in md, part
    assert "합격" not in md.replace("합격 여부는", "") and "불합격" not in md


def test_v303_planned_stop_excluded_from_denominator(tmp_path: Path) -> None:
    db = _db(tmp_path)
    as_of = START + timedelta(days=28, hours=1)
    # 판정 격자 N=60분 — 계획 정지(2시간) 안 예정은 분모에서 뺀다
    grid = PeriodGrid(60, timedelta(seconds=2))
    t = build_m2_paper1(db, ACCOUNT, as_of, [EvalSchedule(period=grid)], reconcile_gap=GAP)
    assert t.planned_in_stops >= 1
    assert t.rate is not None and t.planned > t.planned - t.planned_in_stops
    md = render_m2_paper1(t)
    assert f"{t.planned - t.planned_in_stops}" in md


def test_v303_no_period_record_and_unknown_schedule(tmp_path: Path) -> None:
    db = tmp_path / "var" / "paper" / "trading.db"
    repo = Repository(db, Mode.PAPER, order_identity=OrderIdentity.SYMBOL)
    try:
        with repo.tx():
            repo.insert_reconciliation(ACCOUNT, "STARTUP", "MATCH", {}, START)
    finally:
        repo.close()
    t = build_m2_paper1(db, ACCOUNT, START + timedelta(days=2), [], reconcile_gap=GAP)
    assert t.period_start == START and t.period_basis.startswith("PAPER 기간 시작 기록 없음")
    assert t.rate is None
    assert "일정 미확인" in render_m2_paper1(t)


def test_v303_review_cli_market_m2_appends_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tests.scenario.test_t054_m2_supervisor import _root
    from trading_system.app.review_tool import review_main
    root = _root(tmp_path)
    db = root / "var" / "paper" / "trading.db"
    repo = Repository(db, Mode.PAPER, order_identity=OrderIdentity.SYMBOL)
    try:
        with repo.tx():
            repo.audit(START, "PAPER_PERIOD_START", "user", ACCOUNT, {"at": "가상"})
            repo.insert_reconciliation(ACCOUNT, "STARTUP", "MATCH", {}, START)
    finally:
        repo.close()
    out_file = tmp_path / "review.md"
    assert review_main(["--mode", "PAPER", "--market", "M2", "--root", str(root), "--out", str(out_file)]) == 0
    text = out_file.read_text(encoding="utf-8")
    assert "M2 PAPER-1 집계" in text and "판정 실행률" in text
    assert "집계 완료" in capsys.readouterr().out
    # M1 체크아웃에서 --market M2는 거부(설정 없음)
    from tests.scenario.test_t020_main import make_root
    m1 = make_root(tmp_path / "m1", Mode.PAPER)
    assert review_main(["--mode", "PAPER", "--market", "M2", "--root", str(m1)]) == 2


def test_v303_review_cli_m1_default_unchanged(tmp_path: Path) -> None:
    from tests.scenario.test_t020_main import make_root
    from trading_system.app.review_tool import review_main
    root = make_root(tmp_path, Mode.PAPER)
    db = root / "var" / "paper" / "trading.db"
    Repository(db, Mode.PAPER).close()
    out_file = tmp_path / "m1.md"
    assert review_main(["--mode", "PAPER", "--root", str(root), "--out", str(out_file)]) == 0
    assert "M2 PAPER-1" not in out_file.read_text(encoding="utf-8")
