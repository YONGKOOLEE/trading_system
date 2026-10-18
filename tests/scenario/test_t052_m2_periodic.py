"""CP-037 T-052 (A-113·A-114) — M2 주기 판정 CAP-TRG-02 (D-219), 분봉 입력 대기·무효(D-220), 위험 축소 신호 보존(DP-78).

V-291: 판정은 UTC [00:10, 23:50) 안의 k×N분(+여유)에만, 판정 ID 시장 M2·UTC 시각, 유예 초과는 건너뜀 기록, 재시작 뒤 지난
주기 판정 0, 신호 기한 ≤ 다음 판정, M1 등록 거부. 네트워크 없음 — 실제 조립 함수 build_engine(market="M2")·fake 현물 브로커·
가짜 시계·임시 SQLite. 능력 표는 실제 docs/capabilities.md(BACKTEST 대상 — M2 열 "검증 중").
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_t049_m2_ops import M1_BEFORE
from tests.support.m2_engine_world import ACCOUNT, SYM, M2World, fixed_spec
from tests.support.m2_fakes import m2_limits
from trading_system.app.m2_minutes import MINUTE_CAPABILITY, MINUTE_INPUT_AUDIT, M2MinuteInput
from trading_system.app.wiring import StrategyInput, load_strategy, period_grid_of
from trading_system.config.app_file import load_app_settings, parse_app_settings
from trading_system.config.capabilities import load_capabilities
from trading_system.config.errors import ConfigError
from trading_system.config.limits_file import load_limits_file
from trading_system.config.strategy_registry import validate_spec
from trading_system.domain.enums import Mode, OrderType, RequestType
from trading_system.domain.minute_bar import MinuteBar
from trading_system.domain.minute_series import NO_ADJUSTMENT, PeriodGrid
from trading_system.domain.models import Signal
from trading_system.domain.strategy import (
    EvaluationSnapshot, InstrumentView, ParamSpec, RuleSpec, StrategyLimits, evaluation_id_of,
)
from trading_system.evaluation.runner import run_evaluation
from trading_system.marketdata.spot_minute import SpotMinuteBook
from trading_system.strategy.base import ParamValues, make_signal
from trading_system.strategy.m2_verify import M2Verify

D = Decimal
REPO = Path(__file__).resolve().parents[2]
CAPS = load_capabilities(REPO / "docs" / "capabilities.md")
DAY = date(2026, 10, 10)
LOTS = {SYM: D("0.01")}
PER_CAPS = ("CAP-TRG-02", "CAP-DAT-01", "CAP-ORD-01", "CAP-ORD-02", "CAP-INS-05")


def utc(hh: int, mm: int, ss: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=UTC)


def periodic_spec(*, minute: bool = False, sid: str = "t-m2per"):  # type: ignore[no-untyped-def]
    caps = PER_CAPS + ((MINUTE_CAPABILITY,) if minute else ())
    inputs = ("상태 입력",) + ((MINUTE_CAPABILITY,) if minute else ())
    return replace(
        fixed_spec(strategy_id=sid), capabilities=caps,
        trigger="CAP-TRG-02 period_minutes N분 격자 (UTC 00:00 + k×N분 + 확정 여유, 주문 창 안)",
        rules=(RuleSpec("EXIT-1", "EXIT", inputs, "CAP-TRG-02", "내 소유 보유", "청산 전량", "데이터 품질 실패", 1),
               RuleSpec("ENTRY-1", "ENTRY", inputs, "CAP-TRG-02", "보유 없음", "진입 qty", "데이터 품질 실패", 1)),
        params=(ParamSpec("qty", "decimal", "기초 자산", "진입 수량(소수)", D("0.0001"), D("100")),
                ParamSpec("period_minutes", "int", "분", "주기 판정 격자 N분 (1440의 약수)", D("1"), D("720"))),
        validity_rule="다음 판정 시각 전", validity_max_minutes=720)


class PeriodicProbe:
    """판정 입력을 기록한다. 내 소유 보유면 청산, 무보유면 진입 — 분봉 입력이 무효여도 진입 신호를 내는 '부주의한' 전략
    (엔진이 위험 확대를 거부하는지 보기 위함, DP-78)."""

    def __init__(self, *, minute: bool = False) -> None:
        self.spec = periodic_spec(minute=minute)
        self.snaps: list[EvaluationSnapshot] = []

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
        self.snaps.append(snapshot)
        assert snapshot.next_evaluation_at is not None
        valid = (snapshot.next_evaluation_at - snapshot.as_of) / 2
        out: list[Signal] = []
        for v in snapshot.instruments:
            if not v.data_ok or v.last_price is None:
                continue
            pos = v.position
            if pos is not None and pos.owned_by_me and pos.quantity > 0:
                out.append(make_signal(self.spec, snapshot, v.instrument, "EXIT-1", RequestType.EXIT, pos.quantity,
                                       OrderType.MARKET, v.last_price, valid))
            elif pos is None:
                out.append(make_signal(self.spec, snapshot, v.instrument, "ENTRY-1", RequestType.ENTRY,
                                       D(str(params["qty"])), OrderType.MARKET, v.last_price, valid))
        return out


def world(tmp: Path, n: int, *, minute: bool = False, minutes: M2MinuteInput | None = None,
          probe: PeriodicProbe | None = None) -> tuple[M2World, PeriodicProbe]:
    p = probe or PeriodicProbe(minute=minute)
    w = M2World(tmp, strategies=[StrategyInput(p, {"qty": "0.0100", "period_minutes": n}, ())], caps=CAPS,
                minutes=minutes)
    w.set_baseline()
    return w, p


def evaluations(w: M2World) -> list[dict]:
    return w.audits("EVALUATION")


def skipped(w: M2World) -> list[dict]:
    return w.audits("EVALUATION_SKIPPED")


# --- 격자 (domain) ------------------------------------------------------------------------------------------------

def test_grid_next_after_count_and_day_times() -> None:
    g = PeriodGrid(5)
    assert g.next_after(utc(0, 30, 2)) == utc(0, 35, 2)
    assert g.next_after(utc(23, 45, 2)) == utc(0, 10, 2, day=DAY + timedelta(days=1))   # 창 끝(23:50) 제외 — 다음 날 첫 격자 (A-124 ①)
    assert g.next_after(utc(23, 50, 2)) == utc(0, 10, 2, day=DAY + timedelta(days=1))   # 창 밖 건너 다음 날 첫 격자
    assert g.next_after(utc(0, 0, 0)) == utc(0, 10, 2)
    assert g.count_between(utc(0, 30, 2), utc(0, 50, 2)) == 3
    times = g.day_times(DAY)
    assert times[0] == utc(0, 10, 2) and times[-1] == utc(23, 45, 2) and len(times) == (1430 - 10) // 5   # [00:10, 23:50)
    assert PeriodGrid(1).fetch_wait == timedelta(seconds=15) and PeriodGrid(15).fetch_wait == timedelta(seconds=30)
    assert PeriodGrid(15).grace == timedelta(seconds=60)


# --- V-291 일정 ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("n", [1, 5, 15])
def test_v291_decisions_only_on_grid_inside_order_window(tmp_path: Path, n: int) -> None:
    w, p = world(tmp_path, n)
    try:
        w.at(23, 44, day=DAY)
        w.start_and_resume()                                      # 시작 전 예정 시각은 판정·건너뜀 기록 없음
        t = utc(23, 44, 5)
        end = utc(0, 20, 30, day=DAY + timedelta(days=1))
        while t <= end:                                          # 1분마다 :05초 tick
            w.tick_at(t.hour, t.minute, t.second, day=t.date())
            t += timedelta(minutes=1)
        grid = PeriodGrid(n)
        expected = [x for x in grid.day_times(DAY) if x >= utc(23, 44, 5) - timedelta(seconds=3)] + \
            [x for x in grid.day_times(DAY + timedelta(days=1)) if x <= end]
        got = [s.as_of - timedelta(seconds=3) for s in p.snaps]
        assert got == expected and got                            # tick 3초 뒤 판정, 창 밖(23:51~00:09) 판정 없음
        for at, ev in zip(expected, evaluations(w), strict=True):
            assert ev["ref"] == evaluation_id_of("t-m2per", 1, "M2", at.isoformat())   # 판정 ID = 시장 M2·UTC 예정 시각
        for s in p.snaps:                                         # 신호 기한 상한 = 다음 예정 판정
            assert s.next_evaluation_at == grid.next_after(s.as_of - timedelta(seconds=3))
        assert skipped(w) == []
    finally:
        w.close()


def test_v291_late_tick_skipped_and_within_grace_evaluated(tmp_path: Path) -> None:
    w, p = world(tmp_path, 15)
    try:
        w.start_and_resume()                                      # 00:30:00 시작
        w.tick_at(0, 31, 3)                                       # 00:30:02 + 61초 > 유예 60초 → 늦은 판정 금지
        [s] = skipped(w)
        assert "유예" in s["summary"] and p.snaps == []
        w.tick_at(0, 45, 50)                                      # 00:45:02 + 48초 → 유예 안
        assert len(p.snaps) == 1 and len(evaluations(w)) == 1
    finally:
        w.close()


def test_v291_restart_does_not_evaluate_past_period(tmp_path: Path) -> None:
    w, p = world(tmp_path, 15)
    try:
        w.at(0, 29, 50)
        w.start_and_resume()
        w.restart()                                               # 00:30:02 판정 전에 프로세스 종료
        w.at(0, 30, 30)
        w.start_and_resume()                                      # 00:30:30 재시작 (유예 안이지만 시작 전 예정 시각)
        w.tick_at(0, 30, 40)
        w.tick_at(0, 31, 0)
        assert p.snaps == [] and evaluations(w) == []             # 지난 주기 판정 0
        rs = [x for x in skipped(w) if "재시작" in x["summary"]]     # A-124 ③: 건너뜀 기록 1건(집계에서 구분)
        assert len(rs) == 1 and rs[0]["ref"].endswith("|" + utc(0, 30, 2).isoformat())
        w.tick_at(0, 45, 5)
        assert len(p.snaps) == 1 and p.snaps[0].as_of == utc(0, 45, 5)
    finally:
        w.close()


def test_v291_restart_after_evaluation_records_no_restart_skip(tmp_path: Path) -> None:
    """이전 프로세스가 이미 판정한 예정 시각이면 재시작 건너뜀 기록을 남기지 않는다(판정 여부는 이전 기록이 말함)."""
    w, p = world(tmp_path, 15)
    try:
        w.at(0, 29, 50)
        w.start_and_resume()
        w.tick_at(0, 30, 5)                                       # 00:30:02 판정
        assert len(evaluations(w)) == 1
        w.restart()
        w.at(0, 30, 30)
        w.start_and_resume()
        w.tick_at(0, 30, 40)
        assert len(evaluations(w)) == 1 and not [x for x in skipped(w) if "재시작" in x["summary"]]
    finally:
        w.close()


def test_v291_loop_stall_gap_audited(tmp_path: Path) -> None:
    w, _ = world(tmp_path, 5)
    try:
        w.start_and_resume()
        w.tick_at(0, 30, 5)
        w.tick_at(0, 50, 5)                                       # 00:35·00:40·00:45 판정 없이 지나감
        gaps = [s for s in skipped(w) if "건너뜀" in s["summary"]]
        assert len(gaps) == 1 and '"count": 3' in gaps[0]["summary"]
        assert len(evaluations(w)) == 2
    finally:
        w.close()


def test_v291_global_halt_skips_with_audit(tmp_path: Path) -> None:
    w, p = world(tmp_path, 5)
    try:
        w.start_and_resume()
        w.command("KILL", {"account": ACCOUNT, "reason": "시험"})
        w.tick_at(0, 30, 1)
        w.tick_at(0, 30, 5)
        assert p.snaps == [] and any("전역 정지" in s["summary"] for s in skipped(w))
    finally:
        w.close()


# --- 분봉 조회 대기·시한 (D-220, DP-79) ------------------------------------------------------------------------------

def _bars(first: datetime, n: int) -> list[MinuteBar]:
    return [MinuteBar(SYM, 1, first + timedelta(minutes=i), D(100), D(101), D(99), D(100), D(1), NO_ADJUSTMENT)
            for i in range(n)]


def minute_world(tmp: Path) -> tuple[M2World, PeriodicProbe, SpotMinuteBook]:
    book = SpotMinuteBook((SYM,))
    w, p = world(tmp, 5, minute=True, minutes=M2MinuteInput(book, lookback_bars=10, period_minutes=5))
    return w, p, book


def test_wait_for_fetch_of_decision_time_then_evaluate_ok(tmp_path: Path) -> None:
    w, p, book = minute_world(tmp_path)
    try:
        w.start_and_resume()
        book.put_minute_bars(SYM, _bars(utc(0, 15), 11), utc(0, 25, 3))   # 이전 격자 조회(00:25:02)
        w.tick_at(0, 30, 2)
        w.tick_at(0, 30, 3)                                       # 00:30:02 조회 아직 — 기다림(판정 없음)
        assert p.snaps == [] and evaluations(w) == [] and skipped(w) == []
        book.put_minute_bars(SYM, _bars(utc(0, 20), 11), utc(0, 30, 4))   # 00:20~00:30 (00:30 진행 중)
        w.tick_at(0, 30, 5)
        [s] = p.snaps
        v = s.view(SYM)
        assert v is not None and v.minute_ok and v.data_ok
        assert [b.at for b in v.minute_bars] == [utc(0, m) for m in range(20, 30)]
        assert len(w.sent()) == 1                                 # 분봉 정상 → 진입 허용
    finally:
        w.close()


def test_fetch_timeout_marks_minute_invalid_blocks_entry_records(tmp_path: Path) -> None:
    w, p, book = minute_world(tmp_path)
    try:
        w.start_and_resume()
        book.put_minute_bars(SYM, _bars(utc(0, 15), 11), utc(0, 25, 3))
        w.tick_at(0, 30, 20)                                      # 기다림 상한 30초 안
        assert p.snaps == []
        w.tick_at(0, 30, 33)                                      # 30초 넘음 → 그 판정 분봉 입력 무효로 판정
        [s] = p.snaps
        v = s.view(SYM)
        assert v is not None and not v.minute_ok and v.minute_bars == () and v.data_ok
        [mi] = w.audits(MINUTE_INPUT_AUDIT)
        assert "FETCH_TIMEOUT" in mi["summary"]
        [ev] = evaluations(w)
        assert '"dropped": 1' in ev["summary"] and SYM in ev["summary"]          # 부주의한 진입 신호는 엔진이 버림
        assert w.sent() == []
    finally:
        w.close()


def test_dp78_exit_still_sent_when_minute_input_invalid(tmp_path: Path) -> None:
    w, p, book = minute_world(tmp_path)
    try:
        w.start_and_resume()
        book.put_minute_bars(SYM, _bars(utc(0, 20), 11), utc(0, 30, 3))
        w.tick_at(0, 30, 4)                                       # 정상 → 진입
        for s in (10, 20, 30):
            w.tick_at(0, 31, s)                                   # 체결 반영
        assert len(w.sent()) == 1
        w.tick_at(0, 35, 40)                                      # 00:35:02 조회 없음 → 시한 넘음 → 분봉 무효 판정
        v = p.snaps[-1].view(SYM)
        assert v is not None and not v.minute_ok and v.position is not None and v.position.owned_by_me
        sells = [r for r in w.sent() if r.side == "SELL"]
        assert len(sells) == 1                                    # 위험 축소(청산)는 분봉 무효여도 보낸다 (§8-3)
        assert all(r.side != "BUY" for r in w.sent()[1:])
    finally:
        w.close()


def test_dp78_runner_blocks_only_risk_increasing_when_minute_invalid() -> None:
    spec = periodic_spec(minute=True)
    at = utc(1, 0, 2)
    snap_args = dict(evaluation_id="ev", market="M2", account=ACCOUNT, as_of=at, base_amount=D("1"),
                     next_evaluation_at=at + timedelta(minutes=5))

    class Two:
        def __init__(self) -> None:
            self.spec = spec

        def evaluate(self, snap, params):  # type: ignore[no-untyped-def]
            kind = RequestType.EXIT if snap.instruments[0].position else RequestType.ENTRY
            return [make_signal(spec, snap, SYM, f"{kind.value}-1", kind, D("0.01"), OrderType.MARKET, D(100),
                                timedelta(minutes=1))]

    from trading_system.domain.strategy import PositionView
    flat = EvaluationSnapshot(**snap_args, instruments=(InstrumentView(SYM, last_price=D(100), minute_ok=False),))
    r = run_evaluation(Two(), flat, {}, LOTS)
    assert r.signals == [] and "분봉" in r.dropped[0].reason
    held = EvaluationSnapshot(**snap_args, instruments=(InstrumentView(
        SYM, last_price=D(100), minute_ok=False, position=PositionView(D("0.01"), D(100), 0, True)),))
    r = run_evaluation(Two(), held, {}, LOTS)
    assert [s.kind for s in r.signals] == [RequestType.EXIT] and r.dropped == []
    ok = EvaluationSnapshot(**snap_args, instruments=(InstrumentView(SYM, last_price=D(100)),))
    assert [s.kind for s in run_evaluation(Two(), ok, {}, LOTS).signals] == [RequestType.ENTRY]   # 기본 minute_ok True


# --- 등록·배포 검사 -------------------------------------------------------------------------------------------------

def test_capabilities_m2_trg02_dat02_under_verification_m1_unchanged() -> None:
    assert CAPS["CAP-TRG-02"]["M2"] == "검증 중" and CAPS["CAP-DAT-02"]["M2"] == "검증 중"
    m1 = {cap: row["M1"] for cap, row in CAPS.items() if cap != "CAP-INS-05"}
    assert m1 == M1_BEFORE                                        # M1 열 무변경 (V-152)
    for cap in ("CAP-TRG-03", "CAP-TRG-04", "CAP-DAT-03", "CAP-DAT-04"):
        assert CAPS[cap]["M2"] == "미지원"


def test_deployed_m1_paper_config_and_m2_strategies_still_validate() -> None:
    app = load_app_settings(REPO / "config" / "app.paper.toml", Mode.PAPER, REPO)
    limits = load_limits_file(REPO / "config" / "limits.paper.toml").limits
    assert app.strategies
    for dep in app.strategies:
        strat, _ = load_strategy(dep)
        assert validate_spec(strat.spec, CAPS, limits, Mode.PAPER, dep.verification_approval) == []
    assert validate_spec(M2Verify().spec, CAPS, m2_limits(), Mode.PAPER, "A-112") == []   # M2 PAPER-0 예외 그대로
    # btc-trend v1 (feat/btc-trend-v1-review 803c8c5 strategy/btc_trend.py SPEC의 능력·시장·크기 — 이 브랜치에 없는 파일)
    btc = replace(fixed_spec(strategy_id="btc-trend", universe=("BTCUSDT",), sizing="S-3",
                             capabilities=("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-INS-05"),
                             rules=(RuleSpec("EXIT-1", "EXIT", ("상태 입력",), "CAP-TRG-01", "c", "a", "i", 1),
                                    RuleSpec("ENTRY-1", "ENTRY", ("CAP-DAT-01",), "CAP-TRG-01", "c", "a", "i", 1))))
    assert validate_spec(btc, CAPS, m2_limits(), Mode.BACKTEST) == []


def test_periodic_minute_strategy_registers_for_m2_backtest_only() -> None:
    spec = periodic_spec(minute=True)
    assert validate_spec(spec, CAPS, m2_limits(), Mode.BACKTEST) == []
    assert any("CAP-TRG-02" in x for x in validate_spec(spec, CAPS, m2_limits(), Mode.PAPER))   # "지원" 전 PAPER 불가
    assert any("CAP-TRG-02" in x for x in validate_spec(spec, CAPS, m2_limits(), Mode.PAPER, "A-112"))  # 예외 범위 밖
    m1 = replace(spec, markets=("M1",), capabilities=tuple(c for c in spec.capabilities if c != "CAP-INS-05")
                 + ("CAP-INS-01",))
    problems = validate_spec(m1, CAPS, None, Mode.BACKTEST)
    assert any(p.startswith("CAP-TRG-02: M1에서 미지원") for p in problems)   # M1 등록 거부 유지


def _si(spec, params: dict, times: tuple = ()) -> StrategyInput:  # type: ignore[no-untyped-def]
    class S:
        def __init__(self) -> None:
            self.spec = spec

        def evaluate(self, snap, params):  # type: ignore[no-untyped-def]
            return []
    return StrategyInput(S(), params, times)


def test_period_grid_of_rules() -> None:
    spec = periodic_spec(minute=True)
    g = period_grid_of(_si(spec, {"qty": D("0.01"), "period_minutes": 15}), "M2", minute_period=5,
                       margin=timedelta(seconds=2))
    assert g == PeriodGrid(15, timedelta(seconds=2))
    assert period_grid_of(_si(fixed_spec(), {"qty": D("0.01")}, (time(1, 0),)), "M2") is None   # CAP-TRG-01 그대로
    bad = [
        (_si(spec, {"qty": D("0.01"), "period_minutes": 15}), "M1", None, "M1"),                      # M1 미개방
        (_si(spec, {"qty": D("0.01"), "period_minutes": 15}, (time(1, 0),)), "M2", None, "eval_times"),
        (_si(spec, {"qty": D("0.01"), "period_minutes": 7}), "M2", None, "1440"),
        (_si(spec, {"qty": D("0.01"), "period_minutes": 1440}), "M2", None, "주문 창"),
        (_si(spec, {"qty": D("0.01"), "period_minutes": 10}), "M2", 15, "배수"),                       # 조회 격자와 어긋남
        (_si(spec, {"qty": D("0.01")}), "M2", None, "period_minutes"),
        (_si(fixed_spec(), {"qty": D("0.01")}, ()), "M2", None, "eval_times"),                        # 시각 판정인데 시각 없음
    ]
    for si, market, mp, needle in bad:
        with pytest.raises(ConfigError) as exc:
            period_grid_of(si, market, minute_period=mp)
        assert any(needle in x for x in exc.value.problems), (needle, exc.value.problems)


def test_engine_build_refuses_periodic_with_eval_times(tmp_path: Path) -> None:
    p = PeriodicProbe()
    with pytest.raises(ConfigError):
        M2World(tmp_path, strategies=[StrategyInput(p, {"qty": "0.0100", "period_minutes": 5}, (time(1, 0),))],
                caps=CAPS)


def test_app_settings_m2_allows_empty_eval_times_m1_does_not(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    params = tmp_path / "config" / "p.toml"
    params.write_text("", encoding="utf-8")
    base = {"app": {"account": "BN-PAPER-01", "loop_seconds": "0.5", "reconcile_minutes": 1,
                    "sntp_servers": ["a:123"], "market": "M2", "broker": "fake"},
            "strategies": [{"module": "x.y:Z", "params": "config/p.toml", "eval_times": []}]}
    assert parse_app_settings(base, Mode.BACKTEST, tmp_path).strategies[0].eval_times == ()
    m1 = {**base, "app": {k: v for k, v in base["app"].items() if k not in ("market", "broker")}}
    with pytest.raises(ConfigError):
        parse_app_settings(m1, Mode.BACKTEST, tmp_path)
