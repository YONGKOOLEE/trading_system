"""T-039 정책 프로필 (CP-034 D-196, 04 V-270·V-271).

- V-270: 프로필 파일 적재·이름 기록(설정 버전·감사·판정 기록), L7 비율 게이트 판정, 전환 시 엄격화 즉시·완화 다음 기준 시각·
  원격 거부 (01 §2.3.1 기존 규칙 재사용).
- V-271: 백테스트 보고서·요약, 검증 판정표에 프로필 이름과 승인 여부. 프로필 없음 = legacy, 승인 A-ID 없음 = "미승인 한도 가정".
값은 시험용 가상 값이다(paper1·research 프로필 값은 Q-6 미확정).
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, T0, Env
from tests.scenario.test_t009_backtest import config
from tests.scenario.test_t026_validation import W1
from tests.support.gate_fakes import scenario_limits
from tests.support.invariants import check_invariants
from trading_system.app.backtest import render_report, run_backtest
from trading_system.app.validation import ValidationInputs, run_validation
from trading_system.config.versioning import ChangeDirection, ChangeSource, SettingsManager, SettingsOutcome
from trading_system.domain.clock import KST, FixedClock
from trading_system.domain.enums import Check, GateResult, Mode, OrderType, RequestType
from trading_system.domain.limits import UNAPPROVED_LABEL, AccountLimits
from trading_system.domain.models import GateRequest, Signal
from trading_system.storage.repository import Repository


def _req(qty: str, inst: str = "TST-A", ev: str = "e1") -> GateRequest:
    return GateRequest.from_signal(Signal(
        strategy_id="t039", strategy_version=1, evaluation_id=ev, account=ACCOUNT, instrument=inst, rule_id="R1",
        kind=RequestType.ENTRY, quantity=Decimal(qty), order_type=OrderType.MARKET, price=Decimal("10000"),
        as_of=T0 - timedelta(minutes=1), valid_until=T0 + timedelta(hours=8)))


def _ratio(ratio: str, **over: object) -> AccountLimits:
    return dataclasses.replace(scenario_limits(), l7_max_order_amount=None, l7_max_order_ratio=Decimal(ratio), **over)


# --- V-270 게이트: L7 비율 = 기준 금액 × 비율 ------------------------------------------------------------

@pytest.mark.parametrize(("qty", "ok"), [("99", True), ("100", True), ("101", False)])
def test_v270_l7_ratio_cap_is_base_times_ratio(env: Env, qty: str, ok: bool) -> None:
    """기준 금액 10,000,000 × 0.10 = 1,000,000원: 시장가 예약(상한가 10,000) × 100주 = 경계."""
    env.facts.limits.by_account[ACCOUNT] = _ratio("0.10", profile="research")
    d = env.gate.decide(_req(qty))
    if ok:
        assert d.result is GateResult.APPROVED, d.reason
        row = env.repo.decision(d.decision_id)
        snap = json.loads(row["limit_snapshot"])     # type: ignore[index]
        assert snap["l7_limit"] == "1000000.00" and snap["profile"] == "research"
    else:
        assert d.failed_check is Check.G7 and "L7" in (d.reason or "")


def test_v270_l7_ratio_follows_daily_base(env: Env) -> None:
    from trading_system.domain.market import DailyRisk
    env.facts.limits.by_account[ACCOUNT] = _ratio("0.10")
    env.facts.account.daily[ACCOUNT] = DailyRisk(Decimal("5000000"), Decimal("0"), Decimal("0"), Decimal("0"))
    d = env.gate.decide(_req("51"))                 # 510,000 > 5,000,000 × 0.10
    assert d.failed_check is Check.G7 and "L7" in (d.reason or "")


def test_v270_legacy_amount_snapshot_has_no_profile_keys(env: Env) -> None:
    d = env.gate.decide(_req("10"))
    snap = json.loads(env.repo.decision(d.decision_id)["limit_snapshot"])   # type: ignore[index]
    assert "profile" not in snap and "l7_limit" not in snap


# --- V-270 전환 규칙 (01 §2.3.1) -------------------------------------------------------------------

class _Mgr:
    def __init__(self, tmp_path: Path) -> None:
        self.repo = Repository(tmp_path / "t.db", Mode.BACKTEST)
        self.clock = FixedClock(datetime(2026, 10, 1, 10, 0, tzinfo=KST))
        self.mgr = SettingsManager(self.repo, self.clock)

    def close(self) -> None:
        assert check_invariants(self.repo.connection, Mode.BACKTEST.value) == []
        self.repo.close()


PAPER0 = dataclasses.replace(scenario_limits(), profile="paper0", approved_by="A-034")


def test_v270_profile_recorded_in_settings_version_and_audit(tmp_path: Path) -> None:
    m = _Mgr(tmp_path)
    r = m.mgr.submit_account_limits(ACCOUNT, PAPER0, ChangeSource.LOCAL)
    assert r.outcome is SettingsOutcome.APPLIED
    eff = m.mgr.effective_limits(ACCOUNT)
    assert eff is not None and (eff.profile, eff.approved_by) == ("paper0", "A-034")
    row = m.repo.settings_rows("ACCOUNT_LIMITS", ACCOUNT)[-1]
    assert json.loads(row["values_json"])["profile"] == "paper0"
    m.close()


def test_v270_switch_to_stricter_profile_applies_immediately(tmp_path: Path) -> None:
    m = _Mgr(tmp_path)
    m.mgr.submit_account_limits(ACCOUNT, PAPER0, ChangeSource.LOCAL)
    strict = dataclasses.replace(PAPER0, profile="cautious", approved_by=None, l6_max_positions=2)
    r = m.mgr.submit_account_limits(ACCOUNT, strict, ChangeSource.LOCAL)
    assert r.outcome is SettingsOutcome.APPLIED and r.direction is ChangeDirection.TIGHTENED
    assert m.mgr.effective_limits(ACCOUNT).profile == "cautious"   # type: ignore[union-attr]
    event = [e for e in m.repo.audit_events() if e["kind"] == "SETTINGS_CHANGE"][-1]
    assert "profile" in event["summary"] and "cautious" in event["summary"]
    m.close()


def test_v270_switch_to_looser_profile_waits_for_next_reference_time(tmp_path: Path) -> None:
    m = _Mgr(tmp_path)
    m.mgr.submit_account_limits(ACCOUNT, PAPER0, ChangeSource.LOCAL)
    research = dataclasses.replace(PAPER0, profile="research", approved_by=None, l4_daily_loss_ratio=Decimal("0.04"),
                                   l7_max_order_amount=None, l7_max_order_ratio=Decimal("0.10"))
    r = m.mgr.submit_account_limits(ACCOUNT, research, ChangeSource.LOCAL)
    assert r.outcome is SettingsOutcome.SCHEDULED and r.direction is ChangeDirection.LOOSENED
    assert m.mgr.effective_limits(ACCOUNT).profile == "paper0"   # type: ignore[union-attr]
    m.clock.advance(timedelta(hours=21, minutes=59))               # 다음 날 07:59
    assert m.mgr.effective_limits(ACCOUNT).profile == "paper0"   # type: ignore[union-attr]
    m.clock.advance(timedelta(minutes=1))                          # 08:00 일일 기준 시각
    assert m.mgr.effective_limits(ACCOUNT) == research
    m.close()


def test_v270_mixed_profile_switch_is_deferred(tmp_path: Path) -> None:
    m = _Mgr(tmp_path)
    m.mgr.submit_account_limits(ACCOUNT, PAPER0, ChangeSource.LOCAL)
    mixed = dataclasses.replace(PAPER0, profile="paper1", l6_max_positions=9, l5_daily_new_orders=3)
    r = m.mgr.submit_account_limits(ACCOUNT, mixed, ChangeSource.LOCAL)
    assert r.outcome is SettingsOutcome.SCHEDULED and r.direction is ChangeDirection.MIXED
    m.close()


def test_v270_remote_profile_switch_rejected(tmp_path: Path) -> None:
    m = _Mgr(tmp_path)
    m.mgr.submit_account_limits(ACCOUNT, PAPER0, ChangeSource.LOCAL)
    strict = dataclasses.replace(PAPER0, profile="cautious", l6_max_positions=1)
    r = m.mgr.submit_account_limits(ACCOUNT, strict, ChangeSource.REMOTE)
    assert r.outcome is SettingsOutcome.REJECTED and "원격" in (r.reason or "")
    assert m.mgr.effective_limits(ACCOUNT) == PAPER0
    m.close()


# --- V-271 결과·판정표 표시 ------------------------------------------------------------------------------

def _report(cfg, r) -> str:  # type: ignore[no-untyped-def]
    return render_report(cfg, r, settings_hash=cfg.account_limits.version_hash(), reproducible=True, lookahead_bad=[])


def test_v271_backtest_report_legacy_marks_unapproved(tmp_path: Path) -> None:
    cfg = config(tmp_path, "leg")
    r = run_backtest(cfg)
    text = _report(cfg, r)
    assert "한도 프로필: legacy" in text and UNAPPROVED_LABEL in text
    assert "공식 BACKTEST_PASSED 근거로 쓰지 않음" in text
    s = json.loads((r.db_path.parent / "summary.json").read_text(encoding="utf-8"))
    assert "limits_profile" not in s     # 프로필 없는 기존 실행의 요약은 그대로 (V-231 지문 불변)


def test_v271_backtest_report_and_summary_show_approved_profile(tmp_path: Path) -> None:
    base = config(tmp_path, "x").account_limits
    cfg = config(tmp_path, "apr", account_limits=dataclasses.replace(base, profile="paper0", approved_by="A-034"))
    r = run_backtest(cfg)
    text = _report(cfg, r)
    assert "한도 프로필: paper0" in text and "A-034" in text and UNAPPROVED_LABEL not in text
    s = json.loads((r.db_path.parent / "summary.json").read_text(encoding="utf-8"))
    assert (s["limits_profile"], s["limits_profile_approved_by"]) == ("paper0", "A-034")


def test_v271_backtest_report_unapproved_profile(tmp_path: Path) -> None:
    base = config(tmp_path, "x").account_limits
    cfg = config(tmp_path, "una", account_limits=dataclasses.replace(base, profile="research"))
    r = run_backtest(cfg)
    text = _report(cfg, r)
    assert "한도 프로필: research" in text and UNAPPROVED_LABEL in text
    s = json.loads((r.db_path.parent / "summary.json").read_text(encoding="utf-8"))
    assert (s["limits_profile"], s["limits_profile_approved_by"]) == ("research", None)


@pytest.mark.parametrize(("profile", "approved", "needle"), [
    (None, None, UNAPPROVED_LABEL), ("research", None, UNAPPROVED_LABEL), ("paper0", "A-034", "승인 A-034")])
def test_v271_validation_table_shows_profile(tmp_path: Path, profile: str | None, approved: str | None,
                                             needle: str) -> None:
    base = config(tmp_path, "x").account_limits
    tpl = config(tmp_path, "tpl", account_limits=dataclasses.replace(base, profile=profile, approved_by=approved))
    out = run_validation(ValidationInputs(template=tpl, out_dir=tmp_path / "v", report_version=1, windows=(W1,),
                                          random_runs=0, final_from=datetime(2027, 1, 1, tzinfo=UTC).date()))
    c = out["conditions"]
    assert c["limits_profile"] == (profile or "legacy") and c["limits_profile_approved_by"] == approved  # type: ignore[index]
    md = (tmp_path / "v" / "validation-v1.md").read_text(encoding="utf-8")
    assert f"한도 프로필: {profile or 'legacy'}" in md and needle in md
    head = md[:md.index("## 실행 조건")]
    assert "한도 프로필" in head        # 판정표 영역에도 표시
