"""T-M2-09 공통 V의 M2 재실행 — 남은 항목 (04 2-M2.4 V-157, CP-024 §4, 05 T-M2-09).

test_tm2_09_v157.py의 대응표(NOT_RUN)에 남았던 V 중 네트워크·테스트넷·M2 엔진 조립(T-M2-14) 없이 fake·합성 응답으로
검증할 수 있는 것을 M2 설정(tests/support/m2_fakes: 가상 심볼·소수 수량·UTC 일 경계·USDT)으로 다시 실행한다.
모든 값은 가상이다. 네트워크 없음(텔레그램은 로컬 가짜 서버). 매 시험 끝에 불변 조건을 검사한다 (04 D-069).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import dataclasses
import json
import threading
import time as _time

from tests.contract.test_binance_market import DAY, _k
from tests.scenario.test_t013_telegram import CHAT, TOKEN, FakeTelegram, code_from
from tests.scenario.test_tm2_08_market_split import make_m2_root
from tests.scenario.test_tm2_09_v157 import alerts, halts, run, sig, spot_reconciler
from tests.scenario.test_tm2_11_facts_gate import SYM, _info, req
from tests.scenario.test_tm2_11_facts_gate import run as run_req
from tests.support.gate_fakes import FakeClockHealth, FakeLimits, FakeStrategies
from tests.support.invariants import check_invariants
from tests.support.m2_fakes import ACCOUNT, M2_POLICY, RULES, T0, M2Env, build_m2_env, m2_limits
from tests.support.sample_strategies import PARAMS, cross_spec
from trading_system.app.main import run_main
from trading_system.app.startup import LiveNotAvailable, ModeConsistencyError, check_m2_startup
from trading_system.broker.binance.facts import BinanceMarketFacts
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.config.capabilities import load_capabilities
from trading_system.config.errors import ConfigError
from trading_system.config.strategy_params import load_params_file, validate_params
from trading_system.config.strategy_registry import StrategyRegistry, validate_spec
from trading_system.config.versioning import ChangeSource, SettingsManager, SettingsOutcome
from trading_system.domain.enums import (
    Check, GateResult, HaltType, Mode, OrderType, RequestSource, RequestType, Side,
)
from trading_system.domain.market import estimate_cost
from trading_system.domain.market_time import M2_TIME
from trading_system.domain.models import GateRequest, OrderEvent
from trading_system.domain.strategy import ParamSpec, RuleSpec, StrategyLimits, StrategyState
from trading_system.execution.executor import ExecutionBlocked
from trading_system.gate.gate import Gate
from trading_system.gate.ports import GateInputs
from trading_system.halt.manager import Channel
from trading_system.monitoring.liveness import LoopMonitor, watchdog_check, write_heartbeat
from trading_system.monitoring.sntp import ClockMonitor, SntpError
from trading_system.monitoring.watchers import AlertThresholds, AlertWatcher
from trading_system.ops.commands import OpsProcessor
from trading_system.ops.telegram import CommandBot, Notifier, TelegramApi
from trading_system.reconciliation.reconciler import RecoveryChecker
from trading_system.reporting.daily import DailyReporter
from trading_system.storage.alerts import AuditAlertSink
from trading_system.storage.jsonlog import JsonLogger, Masker
from trading_system.storage.repository import ModeMismatch, Repository, StorageFailure

D = Decimal
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[M2Env]:
    e = build_m2_env(tmp_path)
    yield e
    violations = check_invariants(e.repo.connection, Mode.BACKTEST.value)
    e.repo.close()
    assert violations == [], violations


def at(env: M2Env, when: datetime) -> None:
    """가상 시계를 앞으로만 옮긴다 (UTC 시각)."""
    assert when >= env.clock.now()
    env.clock.advance(when - env.clock.now())


# --- V-116·V-117 경고 — M2 일 경계 = UTC 00:00 (D-108) -------------------------------------------------------

def _m2_watcher(env: M2Env, count: int = 3, minutes: int = 10) -> AlertWatcher:
    return AlertWatcher(env.repo, AuditAlertSink(env.repo, env.clock), AlertThresholds(D("0.80"), count, minutes),
                        time(0, 0), market_time=M2_TIME)


def test_v116_m2_limit_usage_warning_once_per_utc_day(env: M2Env) -> None:
    w = _m2_watcher(env)
    at(env, datetime(2026, 10, 7, 23, 30, tzinfo=UTC))                      # KST 10-08 08:30
    assert run(env, sig(RequestType.ENTRY, qty="0.1990", env=env)).result is GateResult.APPROVED   # L2 99.99%
    fired = w.scan(ACCOUNT, env.clock.now())
    assert len(fired) == 1 and "L2" in fired[0]
    assert w.scan(ACCOUNT, env.clock.now()) == []                           # 같은 날 같은 한도: 반복 없음
    at(env, datetime(2026, 10, 8, 0, 30, tzinfo=UTC))                       # KST로는 같은 날(09:30), UTC로는 다음 날
    assert run(env, sig(RequestType.ENTRY, "TSTB-USDT", "700.0", "e2", env=env)).result is GateResult.APPROVED  # 87.9%
    fired2 = w.scan(ACCOUNT, env.clock.now())
    assert len(fired2) == 1 and "L2" in fired2[0]                           # UTC 새 날 → 다시 1회
    assert sum("L2" in m for m in alerts(env)) == 2


def test_v116_m1_default_still_uses_kst_day(env: M2Env) -> None:
    """기본값(M1, KST) 동작 불변: 같은 승인 열을 KST 기준으로 보면 두 번째 경고는 같은 KST 날이라 반복 억제된다."""
    w = AlertWatcher(env.repo, AuditAlertSink(env.repo, env.clock), AlertThresholds(D("0.80"), 3, 10), time(0, 0))
    at(env, datetime(2026, 10, 7, 23, 30, tzinfo=UTC))
    run(env, sig(RequestType.ENTRY, qty="0.1990", env=env))
    assert len(w.scan(ACCOUNT, env.clock.now())) == 1
    at(env, datetime(2026, 10, 8, 0, 30, tzinfo=UTC))
    run(env, sig(RequestType.ENTRY, "TSTB-USDT", "700.0", "e2", env=env))
    assert w.scan(ACCOUNT, env.clock.now()) == []


def test_v117_m2_reject_surge_warning(env: M2Env) -> None:
    w = _m2_watcher(env, count=3, minutes=10)
    for i in range(2):
        run(env, sig(RequestType.ENTRY, "TSTB-USDT", "3000.0", f"x{i}", env=env))   # L7 초과 거부
    assert not any("거부 급증" in f for f in w.scan(ACCOUNT, env.clock.now()))
    run(env, sig(RequestType.ENTRY, "TSTB-USDT", "3000.0", "x3", env=env))
    fired = w.scan(ACCOUNT, env.clock.now())
    assert any("거부 급증" in f and "G7" in f for f in fired)
    assert not any("거부 급증" in f for f in w.scan(ACCOUNT, env.clock.now()))   # 같은 창 반복 억제


def test_v117_m2_surge_window_counts_within_utc_day(env: M2Env) -> None:
    """거부 집계 범위는 그 시장의 거래일 안이다 — M2는 UTC 00:00에서 나뉜다(KST 자정·M1 기준 시각과 무관)."""
    w = _m2_watcher(env, count=3, minutes=20)                               # 창 20분 = 3건 모두 창 안
    at(env, datetime(2026, 10, 7, 23, 55, tzinfo=UTC))
    for i in range(2):
        run(env, sig(RequestType.ENTRY, "TSTB-USDT", "3000.0", f"y{i}", env=env))
    at(env, datetime(2026, 10, 8, 0, 11, tzinfo=UTC))
    run(env, sig(RequestType.ENTRY, "TSTB-USDT", "3000.0", "y3", env=env))
    assert not any("거부 급증" in f for f in w.scan(ACCOUNT, env.clock.now()))
    m1 = AlertWatcher(env.repo, AuditAlertSink(env.repo, env.clock), AlertThresholds(D("0.80"), 3, 20), time(0, 0))
    assert any("거부 급증" in f for f in m1.scan(ACCOUNT, env.clock.now()))   # KST 날로는 같은 날 3건


# --- V-02 레버리지 배수 노출 — M2 현물은 배수 1 (해당 없음 판정은 사용자 확인, NOT_RUN 유지) -------------------------

def test_v02_m2_spot_exposure_multiplier_is_one(env: M2Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    s = sig(RequestType.ENTRY, qty="0.1000", env=env)
    assert run(env, s).result is GateResult.APPROVED
    [rsv] = env.repo.active_reservations(ACCOUNT)
    assert rsv.amount == D("0.1000") * s.price                          # 노출 = 주문 금액 × 1 (소수 수량, Decimal)
    assert rsv.cash_amount > rsv.amount                                   # 현금 예약 = 주문 금액 + 추정 수수료(배수와 무관)
    f = BinanceMarketFacts(now=env.clock.now, universe=(SYM,), account=ACCOUNT)
    assert f.load_exchange_info(_info())
    assert f.instrument(SYM).leverage == D("1")                          # 실제 공급자도 배수 1만 준다
    assert m2_limits().l10_leverage_total_ratio is None                 # M2 L10 없음 (CP-024 §2.4)


# --- V-76 데이터 품질 실패 — M2 klines (부분: 결측·이상치 기준 미정, NOT_RUN 유지) -----------------------------------

def test_v76_m2_kline_quality_failure_invalidates_signal_via_k10(tmp_path: Path) -> None:
    e = build_m2_env(tmp_path, ioc=True)
    try:
        f = BinanceMarketFacts(now=e.clock.now, universe=(SYM,), halts=e.halts, account=ACCOUNT)
        assert f.load_exchange_info(_info()) and f.update_ticker(SYM, {"symbol": SYM, "price": "10000.00"})
        e.gate = Gate(e.repo, e.clock, GateInputs(FakeLimits({ACCOUNT: m2_limits()}), f, e.account, FakeStrategies(),
                                                  FakeClockHealth(), spot_rules=f), M2_POLICY)
        d0 = int(datetime(2026, 10, 5, tzinfo=UTC).timestamp() * 1000)
        for bad in ([_k(d0), _k(d0)], [_k(d0, h="97.0")], [_k(d0, close="1e2")]):   # 중복·OHLC 모순·형식
            assert f.update_klines(SYM, bad) is False
            assert f.daily_bars(SYM) == []                                   # 전략 입력 없음 = 신호 없음
            assert "K10" in halts(e)
            assert run_req(e, req(f, e, RequestType.ENTRY, "0.0010", f"k{len(bad)}")).failed_check is Check.G2
        assert f.update_klines(SYM, [_k(d0), _k(d0 + DAY)]) is True
        assert "K10" not in halts(e)
        assert run_req(e, req(f, e, RequestType.ENTRY, "0.0010", "ok")).result is GateResult.APPROVED
        assert check_invariants(e.repo.connection, Mode.BACKTEST.value) == []
    finally:
        e.repo.close()


# --- V-61 루프 지연·시계 점프 → K8 → M2 복구 조건 ----------------------------------------------------------------

@pytest.mark.parametrize(("d_mono", "d_wall", "fires"), [
    (1.0, 1.0, False), (6.0, 6.0, True), (1.0, 120.0, True), (1.0, 3.5, True), (1.0, 2.5, False),
])
def test_v61_m2_loop_delay_and_clock_jump_trigger_k8_then_spot_recovery(env: M2Env, d_mono: float, d_wall: float,
                                                                       fires: bool) -> None:
    m = LoopMonitor(env.halts, AuditAlertSink(env.repo, env.clock))
    m.tick(100.0, T0)
    reason = m.tick(100.0 + d_mono, T0 + timedelta(seconds=d_wall))
    assert (reason is not None) is fires
    assert ("K8" in halts(env)) is fires
    if fires:
        assert run(env, sig(RequestType.ENTRY, env=env)).failed_check is Check.G2
        rec = RecoveryChecker(env.repo, spot_reconciler(env), env.halts, clock_ok=lambda: True).check(ACCOUNT)
        assert rec.ok                                                        # 재시작과 같은 대조(M2 자산 대조)
        assert env.halts.resume(HaltType.K8, None, Channel.LOCAL, "user").ok
        assert run(env, sig(RequestType.ENTRY, ev="e2", env=env)).result is GateResult.APPROVED


# --- V-72 시계 오차 → K6 (M2 게이트) -----------------------------------------------------------------------------

def test_v72_m2_clock_offset_raises_k6_and_recovers(env: M2Env) -> None:
    vals = iter([D("1.5"), D("0.1")])
    m = ClockMonitor(env.repo, env.clock, [("a", 1)], env.halts, None, lambda h, p: next(vals), samples=1)
    m.check()
    assert "K6" in halts(env)
    assert run(env, sig(RequestType.ENTRY, env=env)).failed_check is Check.G2
    m.check()
    assert "K6" not in halts(env)
    assert run(env, sig(RequestType.ENTRY, ev="e2", env=env)).result is GateResult.APPROVED


def test_v72_m2_all_clock_servers_fail_is_k6(env: M2Env) -> None:
    def fail(h: str, p: int) -> Decimal:
        raise SntpError("가짜 실패")
    ClockMonitor(env.repo, env.clock, [("a", 1), ("b", 2)], env.halts, None, fail, samples=1).check()
    assert "K6" in halts(env)


# --- V-85·V-86·V-113 전략 등록·파라미터·상태 (M2 시장 열) -----------------------------------------------------------

# 가상 능력 표: M2 열이 "지원"인 경우를 흉내(실제 docs/capabilities.md의 M2 열은 전부 미지원·해당 없음 — CP 전).
# CAP-INS-X1은 가상 상품 능력 ID(M2 상품 CAP은 해당 차수 CP로 추가 예정)
CAPS_M2 = {cap: {"M1": "미지원", "M2": "지원", "M3": "미지원"}
           for cap in ("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-ORD-02", "CAP-INS-X1")}
PARAMS_M2 = {"entry_level": "9000", "exit_level": "8000", "qty": "0.1000"}


def m2_spec(**over: object):  # type: ignore[no-untyped-def]
    base = cross_spec(
        strategy_id="t-m2cross", markets=("M2",), universe=("TSTA-USDT", "TSTB-USDT", "TSTC-USDT"),
        capabilities=("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-ORD-02", "CAP-INS-X1"),
        data_requirements="1d 봉 종가 2개 (UTC)", trigger="CAP-TRG-01 매일 00:15 UTC",
        params=(ParamSpec("entry_level", "decimal", "USDT", "진입 기준선", D("0.0001"), D("1000000")),
                ParamSpec("exit_level", "decimal", "USDT", "청산 기준선", D("0.0001"), D("1000000")),
                ParamSpec("qty", "decimal", "기초 자산", "진입 수량(소수)", D("0.0001"), D("100"))))
    return dataclasses.replace(base, **over)


def test_v85_m2_current_capability_table_refuses_m2_registration() -> None:
    """[T-049 기대값 변경] CP-036 D-212 (3)(A-106)로 capabilities.md M2 열에 TRG-01·DAT-01·ORD-01·ORD-02 "검증 중"과
    CAP-INS-05 행이 들어갔다 — 이제 M2 열은 "미지원"이 아니라 "검증 중"이므로 PAPER 등록은 그 사유로 거부되고(PAPER-0
    예외 없이), BACKTEST는 능력 상태로는 거부되지 않는다. 가상 상품 능력(CAP-INS-X1)은 여전히 정의되지 않은 능력으로 거부.
    영향 V-85·V-289."""
    caps = load_capabilities(ROOT / "docs" / "capabilities.md")
    paper = validate_spec(m2_spec(), caps, m2_limits(), Mode.PAPER)
    assert any("M2에서 검증 중" in p for p in paper), paper
    backtest = validate_spec(m2_spec(), caps, m2_limits(), Mode.BACKTEST)
    assert not any("M2에서" in p for p in backtest), backtest
    for problems in (paper, backtest):
        assert any("CAP-INS-X1: 정의되지 않은 능력" in p for p in problems), problems


@pytest.mark.parametrize(("over", "needle"), [
    ({"name": ""}, "name"),
    ({"markets": ("M2", "M4")}, "markets"),
    ({"rules": (RuleSpec("E1", "ENTRY", (), "CAP-TRG-01", "c", "a", "i", 1),
                RuleSpec("E2", "ENTRY", (), "CAP-TRG-01", "c", "a", "i", 1))}, "우선순위 중복"),
    ({"limits": StrategyLimits(l2_max_symbol_ratio=D("0.5"))}, "계좌 한도보다 큼"),
    ({"validity_max_minutes": 0}, "절대 상한"),
    ({"capabilities": ("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-INS-X1", "CAP-DAT-05")}, "CAP-DAT-05"),
])
def test_v85_m2_registration_rejects(over: dict, needle: str) -> None:
    assert validate_spec(m2_spec(), CAPS_M2, m2_limits(), Mode.PAPER) == []
    problems = validate_spec(m2_spec(**over), CAPS_M2, m2_limits(), Mode.PAPER)
    assert any(needle in p for p in problems), problems


def test_v85_m2_rejected_registration_recorded_no_state_and_markets_separate(env: M2Env) -> None:
    reg = StrategyRegistry(env.repo, env.clock, env.halts, market="M2")
    bad = reg.register(m2_spec(name=""), dict(PARAMS_M2), CAPS_M2, m2_limits())
    assert not bad.ok and env.repo.strategy_state("t-m2cross", 1, "M2") is None
    assert reg.signal_rejection("t-m2cross:1", RequestType.ENTRY) == "등록되지 않은 전략 (R-17)"
    assert reg.register(m2_spec(), dict(PARAMS_M2), CAPS_M2, m2_limits(), Mode.BACKTEST).ok
    assert env.repo.strategy_state("t-m2cross", 1, "M2") is not None
    assert env.repo.strategy_state("t-m2cross", 1, "M1") is None             # 상태는 시장별


@pytest.mark.parametrize(("params", "needle"), [
    ({"entry_level": "9000", "exit_level": "8000"}, "qty: 필수"),
    ({**PARAMS_M2, "qty": "0"}, "최소값"),
    ({**PARAMS_M2, "qty": "100.5"}, "최대값"),
    ({**PARAMS_M2, "qty": 0.1}, "부동소수"),
    ({**PARAMS_M2, "extra": 1}, "명세에 없는"),
    ({**PARAMS_M2, "qty": "abc"}, "소수 형식"),
    ({**PARAMS_M2, "qty": "NaN"}, "유한한 값"),
])
def test_v86_m2_parameter_validation(params: dict, needle: str) -> None:
    with pytest.raises(ConfigError) as e:
        validate_params(m2_spec(), params)
    assert any(needle in p for p in e.value.problems), e.value.problems


def test_v86_m2_params_file_decimal_quantity_and_float_refused(tmp_path: Path) -> None:
    p = tmp_path / "t-m2cross-v1.toml"
    p.write_text('strategy = "t-m2cross"\nversion = 1\n[params]\nentry_level = "9000"\nexit_level = "8000"\n'
                 'qty = "0.1000"\n', encoding="utf-8")
    assert load_params_file(m2_spec(), p) == {"entry_level": D("9000"), "exit_level": D("8000"), "qty": D("0.1000")}
    p.write_text(p.read_text(encoding="utf-8").replace('qty = "0.1000"', "qty = 0.1"), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_params_file(m2_spec(), p)                                     # 부동소수 → 시작 거부


def test_v113_m2_paper_mode_rejects_draft_and_backtest_passed(tmp_path: Path) -> None:
    e = build_m2_env(tmp_path, "mock", mode=Mode.PAPER)       # 게이트 G1: PAPER 계좌 = 모의 환경
    try:
        reg = StrategyRegistry(e.repo, e.clock, e.halts, market="M2")
        e.gate = Gate(e.repo, e.clock, dataclasses.replace(e.gate._in, strategies=reg), M2_POLICY)  # noqa: SLF001
        e.halts._gate = e.gate  # noqa: SLF001 — 정지 정책 취소도 같은 게이트로
        assert reg.register(m2_spec(), dict(PARAMS_M2), CAPS_M2, m2_limits()).ok
        d1 = e.gate.decide(GateRequest.from_signal(sig(RequestType.ENTRY, ev="a", strategy="t-m2cross", env=e)))
        assert d1.failed_check is Check.G6 and "V-113" in (d1.reason or "")
        reg.transition("t-m2cross", 1, StrategyState.BACKTEST_PASSED, "A-901", evidence="bt.md")
        d2 = e.gate.decide(GateRequest.from_signal(sig(RequestType.ENTRY, ev="b", strategy="t-m2cross", env=e)))
        assert d2.failed_check is Check.G6
        reg.transition("t-m2cross", 1, StrategyState.PAPER, "A-902")
        d3 = e.gate.decide(GateRequest.from_signal(sig(RequestType.ENTRY, ev="c", strategy="t-m2cross", env=e)))
        assert d3.result is GateResult.APPROVED
        assert check_invariants(e.repo.connection, Mode.PAPER.value) == []
    finally:
        e.repo.close()


# --- V-87 설정 변경 — M2 (강화 즉시·완화는 다음 UTC 00:00은 test_tm2_02_time, 여기서는 원격 거부·혼합) ----------------

def test_v87_m2_remote_change_rejected_and_mixed_waits_for_utc_midnight(env: M2Env) -> None:
    mgr = SettingsManager(env.repo, env.clock, market_time=M2_TIME)
    base = m2_limits()
    assert mgr.submit_account_limits(ACCOUNT, base, ChangeSource.LOCAL).outcome is SettingsOutcome.APPLIED
    r = mgr.submit_account_limits(ACCOUNT, m2_limits(l2_max_symbol_ratio=D("0.05")), ChangeSource.REMOTE)
    assert r.outcome is SettingsOutcome.REJECTED and mgr.effective_limits(ACCOUNT) == base
    rows = env.repo.settings_rows("ACCOUNT_LIMITS", ACCOUNT)
    assert rows[-1]["result"] == "REJECTED" and rows[-1]["source"] == "REMOTE" and "원격" in rows[-1]["reason"]
    mixed = m2_limits(l2_max_symbol_ratio=D("0.10"), l6_max_positions=8)            # 강화 + 완화 = 혼합
    res = mgr.submit_account_limits(ACCOUNT, mixed, ChangeSource.LOCAL)
    assert res.outcome is SettingsOutcome.SCHEDULED
    assert res.effective_at == datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    assert mgr.effective_limits(ACCOUNT) == base
    at(env, datetime(2026, 10, 8, 0, 0, tzinfo=UTC))
    assert mgr.effective_limits(ACCOUNT) == mixed


# --- V-100·V-101·V-102 모드 검사 — M2 시작 경로 ----------------------------------------------------------------

@pytest.mark.parametrize("environ", [{}, {"TRADE_MODE": "live"}, {"TRADE_MODE": "TEST"}, {"TRADE_MODE": ""}])
def test_v100_m2_trade_mode_missing_or_invalid_exits_without_connection(tmp_path: Path, environ: dict,
                                                                         capsys: pytest.CaptureFixture[str]) -> None:
    root = make_m2_root(tmp_path, Mode.PAPER)
    assert run_main(root, environ, market="M2") == 2
    out = capsys.readouterr().out
    assert "시작 거부(브로커 연결 없음)" in out and "TRADE_MODE" in out and "placeholder" not in out
    assert not (root / "var").exists()


def test_v101_m2_mode_storage_credentials_mismatch_refused(tmp_path: Path) -> None:
    root = make_m2_root(tmp_path, Mode.PAPER)
    paper_db, paper_env = root / "var" / "paper" / "trading.db", root / ".env.paper"
    (root / "var" / "live").mkdir(parents=True)
    with pytest.raises(ModeConsistencyError):
        check_m2_startup(Mode.PAPER, root, root / "var" / "live" / "trading.db", paper_env)
    live_env = root / ".env.live"
    live_env.write_text("TRADE_MODE=LIVE\nBINANCE_LIVE_API_KEY=placeholder\n", encoding="utf-8")
    with pytest.raises(ModeConsistencyError) as exc:
        check_m2_startup(Mode.PAPER, root, paper_db, live_env)
    assert "placeholder" not in str(exc.value)
    paper_env.write_text("TRADE_MODE=LIVE\nBINANCE_PAPER_API_KEY=placeholder\n", encoding="utf-8")
    with pytest.raises(ModeConsistencyError):
        check_m2_startup(Mode.PAPER, root, paper_db, paper_env)                 # 파일 안 모드 불일치
    with pytest.raises(ModeConsistencyError):
        check_m2_startup(Mode.BACKTEST, root, root / "var" / "backtest" / "trading.db", paper_env)   # BACKTEST + 자격정보
    assert not (root / "var" / "paper").exists()


def test_v102_m2_live_by_mode_value_only_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_m2_root(tmp_path, Mode.PAPER)
    (root / ".env.live").write_text("TRADE_MODE=LIVE\nBINANCE_LIVE_API_KEY=placeholder\n", encoding="utf-8")
    assert run_main(root, {"TRADE_MODE": "LIVE"}, market="M2") == 2
    out = capsys.readouterr().out
    assert "시작 거부(브로커 연결 없음)" in out and "LIVE" in out and "placeholder" not in out
    with pytest.raises(LiveNotAvailable):
        check_m2_startup(Mode.LIVE, root, root / "var" / "live" / "trading.db", root / ".env.live")
    assert not (root / "var").exists()


# --- V-104 마스킹 — M2 로그(API 키·HMAC 비밀·서명·listenKey) ----------------------------------------------------------

def test_v104_m2_masking(tmp_path: Path) -> None:
    key, secret = "synthetic-api-key-0123456789abcdef", "synthetic-secret-fedcba9876543210"
    log = JsonLogger(tmp_path / "logs", "m2", Masker([key, secret]))
    log.write({"msg": f"서명 실패 secret={secret} 키 {key}", "X-MBX-APIKEY": key, "signature": "a1b2c3d4e5f6",
               "path": "/api/v3/order?symbol=TSTAUSDT&timestamp=1760000040000&signature=deadbeefcafe",
               "listenKey": "pqia91ma19a5s61cv6a8", "nested": {"api_key": "x", "note": "acct 9876543210"},
               "symbol": "TSTA-USDT", "orderId": 28, "clientOrderId": "tm209x0001", "qty": "0.1000",
               "price": "10050.00", "account": "BN-BT-01"},
              at=datetime(2026, 10, 7, 23, 30, tzinfo=UTC))
    [f] = list((tmp_path / "logs").glob("m2-*.jsonl"))
    text = f.read_text(encoding="utf-8")
    for leaked in (key, secret, "a1b2c3d4e5f6", "deadbeefcafe", "pqia91ma19a5s61cv6a8", "9876543210"):
        assert leaked not in text, leaked
    rec = json.loads(text)
    assert rec["symbol"] == "TSTA-USDT" and rec["qty"] == "0.1000" and rec["price"] == "10050.00"
    assert rec["orderId"] == 28 and rec["clientOrderId"] == "tm209x0001"
    assert rec["X-MBX-APIKEY"] == "***" and rec["signature"] == "***" and rec["nested"]["api_key"] == "***"


# --- V-111 프로세스 감시 — M2 체크아웃 하트비트 (UTC 자정 걸침) ----------------------------------------------------

def test_v111_m2_watchdog_heartbeat(tmp_path: Path) -> None:
    hb = tmp_path / "m2root" / "var" / "paper" / "heartbeat"
    t = datetime(2026, 10, 7, 23, 59, 50, tzinfo=UTC)
    assert not watchdog_check(hb, t).alive
    write_heartbeat(hb, t)
    assert watchdog_check(hb, t + timedelta(seconds=20)).alive              # UTC 자정을 넘어도 경과 시간만 본다
    st = watchdog_check(hb, t + timedelta(seconds=31))
    assert not st.alive and "멈춤" in st.detail


# --- V-57·V-107·V-118·V-128 텔레그램 (M2 봇, 로컬 가짜 서버) ------------------------------------------------------

@pytest.fixture
def tg() -> Iterator[FakeTelegram]:
    f = FakeTelegram()
    yield f
    f.close()


def m2_api(tg: FakeTelegram) -> TelegramApi:
    return TelegramApi(lambda: TOKEN, tg.url, market="M2")


def m2_bot(e: M2Env, tg: FakeTelegram) -> CommandBot:
    sink = AuditAlertSink(e.repo, e.clock)
    return CommandBot(e.repo, m2_api(tg), CHAT, ACCOUNT, e.clock, e.kill_file, lambda lvl, m: sink.alert(lvl, m, None),
                      market="M2")


def m2_ops(e: M2Env) -> OpsProcessor:
    return OpsProcessor(e.repo, e.gate, e.executor, e.halts, e.clock, kill_file=e.kill_file)


def test_v57_m2_duplicate_update_processed_once(env: M2Env, tg: FakeTelegram) -> None:
    m2_bot(env, tg).poll_once(timeout=0)
    tg.push(5, "/kill")
    m2_bot(env, tg).poll_once(timeout=0)
    tg.updates.clear()
    tg.push(5, "/kill")                      # 같은 update_id 재전달
    m2_bot(env, tg).poll_once(timeout=0)     # 재시작해 offset을 잃어도
    assert len(m2_ops(env).process_pending()) == 1
    assert tg.sent[-1]["text"].startswith("[M2]") and "이미 받은 명령" in tg.sent[-1]["text"]
    assert "K1" in halts(env)


def test_v107_m2_unknown_chat_and_confirmation_code(env: M2Env, tg: FakeTelegram) -> None:
    b = m2_bot(env, tg)
    tg.push(1, "/kill", chat="999")
    b.poll_once(timeout=0)
    assert env.repo.pending_commands() == [] and tg.sent == []
    assert "REMOTE_REJECTED" in [e["kind"] for e in env.repo.audit_events()]
    assert any("허용되지 않은 채팅" in m for m in alerts(env))
    tg.push(2, "/exit TSTA-USDT 0.0500")
    b.poll_once(timeout=0)
    good = code_from(tg)
    tg.push(3, "/confirm deadbe")
    b.poll_once(timeout=0)
    assert env.repo.pending_commands() == [] and "틀렸거나 만료" in tg.sent[-1]["text"]
    env.clock.advance(timedelta(seconds=61))
    tg.push(4, f"/confirm {good}")
    b.poll_once(timeout=0)
    assert env.repo.pending_commands() == []                                # 만료
    tg.push(5, "/exit TSTA-USDT 0.0500")
    b.poll_once(timeout=0)
    tg.push(6, f"/confirm {code_from(tg)}")
    b.poll_once(timeout=0)
    assert [c["command"] for c in env.repo.pending_commands()] == ["EXIT"]


def test_v118_m2_rejected_risk_reduction_alerts_over_telegram(env: M2Env, tg: FakeTelegram) -> None:
    b = m2_bot(env, tg)
    tg.push(1, "/exit TSTA-USDT 0.0500")    # 보유 없음 → 게이트 거부
    b.poll_once(timeout=0)
    tg.push(2, f"/confirm {code_from(tg)}")
    b.poll_once(timeout=0)
    [o] = m2_ops(env).process_pending()
    assert o.status == "REJECTED" and env.broker.sent == []
    Notifier(env.repo, m2_api(tg), CHAT, env.clock, env.halts).pump()
    assert any(m["text"].startswith("[M2] [긴급]") and "운영 요청 거부" in m["text"] for m in tg.sent)


def test_v128_m2_stale_remote_commands_not_executed(env: M2Env, tg: FakeTelegram) -> None:
    now = int(env.clock.now().timestamp())
    b = m2_bot(env, tg)
    tg.push(1, "/kill 오래전", date=now - 3600)
    tg.push(2, "/status", date=now - 300)
    tg.push(3, "hi", date=now - 7200)
    assert b.poll_once(timeout=0) == 3
    assert m2_ops(env).process_pending() == [] and halts(env) == []
    texts = [m["text"] for m in tg.sent]
    assert len(texts) == 2 and all(t.startswith("[M2]") and "꺼져 있던 동안" in t for t in texts)
    assert sum(1 for e in env.repo.audit_events() if e["kind"] == "REMOTE_STALE") == 3
    tg.push(4, "/kill 방금", date=now - 60)
    b.poll_once(timeout=0)
    assert [o.status for o in m2_ops(env).process_pending()] == ["DONE"]


# --- V-115 견고성 묶음 B-1~B-8 (CP-016 표 B) — M2 설정 ------------------------------------------------------------

def _req(**kw: object) -> GateRequest:
    r = RULES["TSTA-USDT"]
    base = dict(source=RequestSource.SIGNAL, request_type=RequestType.ENTRY, account=ACCOUNT, instrument="TSTA-USDT",
                side=Side.BUY, quantity=D("0.1000"), order_type=OrderType.LIMIT, price=D("10050.00"), idem_key="k-1",
                owner="tm209-a:1")
    base.update(kw)
    assert r.tick_size == D("0.01")
    return GateRequest(**base)  # type: ignore[arg-type]


def _hold(env: M2Env, inst: str = "TSTA-USDT", qty: str = "0.1000", ev: str = "h") -> None:
    assert run(env, sig(RequestType.ENTRY, inst, qty, ev, env=env)).result is GateResult.APPROVED


def test_v115_b1_m2_reopen_db_with_other_mode_refused(tmp_path: Path) -> None:
    p = tmp_path / "t.db"
    from trading_system.storage.repository import OrderIdentity
    Repository(p, Mode.BACKTEST, order_identity=OrderIdentity.SYMBOL).close()
    with pytest.raises(ModeMismatch):
        Repository(p, Mode.PAPER, order_identity=OrderIdentity.SYMBOL)
    Repository(p, Mode.BACKTEST, order_identity=OrderIdentity.SYMBOL).close()


@pytest.mark.parametrize(("kw", "check"), [
    ({"quantity": D("0")}, Check.G10),
    ({"quantity": D("-0.1")}, Check.G10),
    ({"price": D("0")}, Check.G10),
    ({"side": Side.SELL}, Check.G3),
    ({"source": RequestSource.OPERATOR, "owner": None}, Check.G3),
    ({"owner": None}, Check.G3),
])
def test_v115_b2_m2_malformed_or_unauthorized_requests_rejected(env: M2Env, kw: dict, check: Check) -> None:
    d = env.gate.decide(_req(**kw))
    assert d.result is GateResult.REJECTED and d.failed_check is check, d.reason
    assert env.repo.intents() == [] and env.broker.sent == []


def test_v115_b2_m2_negative_quantity_sell_rejected(env: M2Env) -> None:
    _hold(env)
    d = env.gate.decide(_req(request_type=RequestType.REDUCE, side=Side.SELL, quantity=D("-0.05"),
                             order_type=OrderType.MARKET, idem_key="k-2"))
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G10


def test_v115_b3_m2_missing_price_rejects_instead_of_crashing(env: M2Env) -> None:
    env.market.prices.pop("TSTB-USDT")
    d = env.gate.decide(GateRequest.from_signal(sig(RequestType.ENTRY, "TSTB-USDT", "10.0", "z", env=env)))
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G11, d.reason
    assert env.repo.decision(d.decision_id) is not None


def test_v115_b4_m2_storage_failure_after_send_blocks_further_sends(env: M2Env) -> None:
    d1 = env.gate.decide(GateRequest.from_signal(sig(RequestType.ENTRY, env=env)))
    d2 = env.gate.decide(GateRequest.from_signal(sig(RequestType.ENTRY, "TSTB-USDT", "10.0", "e2", env=env)))
    original = env.broker.place_order

    def place_then_break_storage(r):  # type: ignore[no-untyped-def]
        result = original(r)
        env.repo.fail_next_transaction = True
        return result

    env.broker.place_order = place_then_break_storage  # type: ignore[method-assign]
    with pytest.raises(StorageFailure):
        env.executor.submit(d1.approved)
    env.broker.place_order = original  # type: ignore[method-assign]
    sent = len(env.broker.sent)
    with pytest.raises(ExecutionBlocked):
        env.executor.submit(d2.approved)
    assert len(env.broker.sent) == sent


@pytest.mark.parametrize("bad", ["missing_id", "zero_qty", "over_fill", "bad_price"])
def test_v115_b5_m2_anomalous_fill_not_applied(env: M2Env, bad: str) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = run(env, sig(RequestType.ENTRY, env=env))
    order_no = env.repo.intent(d.approved.intent_id).broker_order_key.split("|")[-1]
    ev = {
        "missing_id": OrderEvent("FILL", order_no, None, D("0.1000"), D("10050.00")),
        "zero_qty": OrderEvent("FILL", order_no, "F1", D("0"), D("10050.00")),
        "over_fill": OrderEvent("FILL", order_no, "F1", D("0.1001"), D("10050.00")),
        "bad_price": OrderEvent("FILL", order_no, "F1", D("0.1000"), D("0")),
    }[bad]
    env.broker.inject(ev)
    env.executor.process_events()
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == D("0")
    assert any(e["kind"] == "EVENT_ANOMALY" for e in env.repo.audit_events())


def test_v115_b6_m2_events_kept_when_storage_fails_midway(env: M2Env) -> None:
    d1 = env.gate.decide(GateRequest.from_signal(sig(RequestType.ENTRY, env=env)))
    d2 = env.gate.decide(GateRequest.from_signal(sig(RequestType.ENTRY, "TSTB-USDT", "10.0", "e2", env=env)))
    env.executor.submit(d1.approved)
    env.executor.submit(d2.approved)
    env.repo.fail_next_transaction = True
    with pytest.raises(StorageFailure):
        env.executor.process_events()
    env.executor.process_events()
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == D("0.0999")   # 순 수령(수수료 = 기초 자산 0.1%)
    assert env.repo.position(ACCOUNT, "TSTB-USDT").quantity == D("9.990")


def test_v115_b7_m2_concurrent_double_submit_sends_once(env: M2Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = env.gate.decide(GateRequest.from_signal(sig(RequestType.ENTRY, env=env)))
    errors: list[BaseException] = []

    def go() -> None:
        try:
            env.executor.submit(d.approved)
        except (ValueError, StorageFailure) as exc:
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(env.broker.sent) == 1 and len(errors) == 3


def test_v115_b8_m2_reader_never_sees_uncommitted_rows(env: M2Env) -> None:
    started, seen = threading.Event(), []
    original = env.repo.insert_intent

    def insert_then_wait_and_fail(row):  # type: ignore[no-untyped-def]
        original(row)
        started.set()
        _time.sleep(0.3)
        raise RuntimeError("판정 도중 실패 → 롤백")

    env.repo.insert_intent = insert_then_wait_and_fail  # type: ignore[method-assign]

    def decide() -> None:
        with pytest.raises(RuntimeError):
            env.gate.decide(GateRequest.from_signal(sig(RequestType.ENTRY, env=env)))

    t = threading.Thread(target=decide)
    t.start()
    assert started.wait(5)
    seen.extend(env.repo.intents())
    t.join()
    assert seen == []


# --- V-110·V-114 일일 보고 — M2 거래일 = UTC 날짜 (D-108) -----------------------------------------------------------

def _m2_reporter(env: M2Env) -> DailyReporter:
    def price_of(inst: str) -> Decimal | None:
        q = env.market.quote(inst)
        return None if q is None else q.price
    return DailyReporter(env.repo, AuditAlertSink(env.repo, env.clock), M2_POLICY.cost_rates, price_of,
                         market_time=M2_TIME)


def test_v114_v110_m2_daily_report_by_strategy_on_utc_day(env: M2Env) -> None:
    at(env, datetime(2026, 10, 7, 23, 0, tzinfo=UTC))                      # KST 10-08 08:00, UTC 10-07
    assert run(env, sig(RequestType.ENTRY, qty="0.1000", ev="a1", env=env)).result is GateResult.APPROVED
    assert run(env, sig(RequestType.ENTRY, "TSTB-USDT", "10.0", "b1", strategy="tm209-b", env=env)).approved
    assert run(env, sig(RequestType.EXIT, qty="0.0999", ev="a2", env=env)).result is GateResult.APPROVED
    run(env, sig(RequestType.ENTRY, "TSTC-USDT", "3000.0", "rej", env=env))   # L7 초과 거부 1건
    at(env, datetime(2026, 10, 8, 0, 10, tzinfo=UTC))                       # 보고 시각 = UTC 00:10
    content = _m2_reporter(env).generate(ACCOUNT, env.clock.now(), time(0, 0), "hash-m2")
    assert content is not None and content["trade_day"] == "2026-10-07"
    by = content["by_strategy"]
    assert set(by) == {"tm209-a:1", "tm209-b:1"}
    assert by["tm209-a:1"]["fills"] == "2" and by["tm209-b:1"]["fills"] == "1"
    fills = env.repo.select("SELECT f.quantity, f.price, o.side, o.instrument FROM fill f JOIN broker_order o "
                            "ON o.order_key=f.order_key ORDER BY f.rowid")
    rates = M2_POLICY.cost_rates
    (bq, bp), (sq, sp) = [(D(f["quantity"]), D(f["price"])) for f in fills if f["instrument"] == "TSTA-USDT"]
    a_realized = (sp - bp) * sq - estimate_cost(sq * sp, True, rates) - estimate_cost(bq * bp, False, rates)
    assert D(by["tm209-a:1"]["realized"]) == a_realized
    [(cq, cp)] = [(D(f["quantity"]), D(f["price"])) for f in fills if f["instrument"] == "TSTB-USDT"]
    assert D(by["tm209-b:1"]["realized"]) == -estimate_cost(cq * cp, False, rates)
    assert content["rejects_by_check"] == {"G7": 1} and content["orders_approved"] == 3
    assert env.repo.daily_reports(ACCOUNT)[0]["status"] == "OK"
    m1_style = DailyReporter(env.repo, AuditAlertSink(env.repo, env.clock), M2_POLICY.cost_rates, lambda i: None)
    assert m1_style.generate(ACCOUNT, env.clock.now(), time(0, 0), None)["by_strategy"] == {}   # KST 날로는 다른 날


def test_v110_m2_report_failure_is_recorded_and_alerted(env: M2Env, monkeypatch: pytest.MonkeyPatch) -> None:
    import trading_system.reporting.daily as daily
    monkeypatch.setattr(daily, "build_report", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    at(env, datetime(2026, 10, 8, 0, 10, tzinfo=UTC))
    assert _m2_reporter(env).generate(ACCOUNT, env.clock.now(), time(0, 0), None) is None
    [row] = env.repo.daily_reports(ACCOUNT)
    assert row["status"] == "FAILED" and row["trade_day"] == "2026-10-07"
    assert any("일일 보고 생성 실패 (2026-10-07)" in m for m in alerts(env))
