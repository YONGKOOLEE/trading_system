"""CP-040 T-067 (A-115 Q-2 (나), 착수 A-123 ④) — M2 손절 감시 CAP-HLD-02 (D-262·D-263·D-269).

V-331: 보유 + 손절가, 닫힌 1분 봉 종가 < · = · > 손절가 → 종가 ≤ 손절가에서만 청산 요청 1건 → 게이트 → 시장가 매도,
       진행 중 분 무시, 부분 체결 남은 수량 계속 감시, 비소유 포지션 0, 요청·감사에 원천 = 손절 감시.
V-332: 감지 분 결측·품질 실패 → 청산 0·3회 경고, 직전 손절 요청 결과 불명 중 새 요청 0, K2 → 허용, K1·K8 → 01 §2.6 표대로
       (K8 0, 재시작 직후 0), 3회 연속 EXPIRED → 긴급 알림, M1에서 CAP-HLD-02 등록 거부.
네트워크 없음 — 실제 조립 함수 build_engine(market="M2")·fake 현물 브로커·가짜 시계·가상 1분 봉·임시 SQLite.
능력 표 = 실제 docs/capabilities.md + CAP-HLD-02 개정안 행(M1 미지원·M2 검증 중 — 교체는 승인 뒤, 시험에서만 덧붙임).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.m2_engine_world import ACCOUNT, SYM, M2World, fixed_spec
from tests.support.m2_fakes import m2_limits
from trading_system.app import wiring
from trading_system.app.m2_minutes import M2MinuteInput
from trading_system.app.wiring import StrategyInput
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.config.capabilities import load_capabilities
from trading_system.config.errors import ConfigError
from trading_system.config.strategy_registry import StrategyRegistry
from trading_system.domain.clock import FixedClock
from trading_system.domain.enums import HaltType, Mode, OrderType, RequestType
from trading_system.domain.minute_bar import MinuteBar
from trading_system.domain.minute_series import NO_ADJUSTMENT
from trading_system.domain.models import Signal
from trading_system.domain.stop_watch import STOP_CAPABILITY, STOP_ORIGIN, STOP_RULE_ID, stop_triggered
from trading_system.domain.strategy import EvaluationSnapshot, ParamSpec, RuleSpec
from trading_system.marketdata.spot_minute import SpotMinuteBook
from trading_system.storage.repository import Repository
from trading_system.strategy.base import ParamValues, make_signal

D = Decimal
REPO = Path(__file__).resolve().parents[2]
DAY = date(2026, 10, 10)
BASE_CAPS = ("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-ORD-02", "CAP-INS-05")
STOP = D("9500")


def caps_with_hld02(m1: str = "미지원", m2: str = "검증 중") -> dict:
    caps = load_capabilities(REPO / "docs" / "capabilities.md")
    caps[STOP_CAPABILITY] = {"M1": m1, "M2": m2, "M3": "미지원"}   # 개정안 행 (capabilities.md 교체는 승인 뒤)
    return caps


def utc(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, hh, mm, ss, tzinfo=UTC)


def stop_spec(sid: str = "t-m2stop", *, monitored: bool = True):  # type: ignore[no-untyped-def]
    caps = BASE_CAPS + ((STOP_CAPABILITY,) if monitored else ())
    return fixed_spec(
        strategy_id=sid, capabilities=caps, sizing="S-3",
        holding="손절가 감시(CAP-HLD-02) — 닫힌 1분 봉 종가 ≤ 손절가면 시스템이 청산" if monitored else "판정 1회",
        rules=(RuleSpec("ENTRY-1", "ENTRY", ("상태 입력",), "CAP-TRG-01", "보유 없음", "진입 qty, 손절가 stop",
                        "데이터 품질 실패", 1),),
        params=(ParamSpec("qty", "decimal", "기초 자산", "진입 수량(소수)", D("0.0001"), D("100")),
                ParamSpec("stop", "decimal", "USDT", "손절가", D("1"), D("1000000"))))


class StopEntry:
    """보유가 없을 때만 손절가를 실은 진입 신호를 낸다. 청산 신호는 내지 않는다 — 청산은 시스템 손절 감시만."""

    def __init__(self, sid: str = "t-m2stop", *, monitored: bool = True, with_stop: bool = True) -> None:
        self.spec = stop_spec(sid, monitored=monitored)
        self.with_stop = with_stop

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
        out: list[Signal] = []
        for v in snapshot.instruments:
            if not v.data_ok or v.last_price is None or v.position is not None:
                continue
            out.append(make_signal(self.spec, snapshot, v.instrument, "ENTRY-1", RequestType.ENTRY,
                                   D(str(params["qty"])), OrderType.MARKET, v.last_price, timedelta(minutes=20),
                                   stop_price=D(str(params["stop"])) if self.with_stop else None))
        return out


def make_world(tmp: Path, *, strategies: list[StrategyInput] | None = None, period: int = 1,
               minutes: bool = True, caps: dict | None = None) -> tuple[M2World, SpotMinuteBook]:
    book = SpotMinuteBook((SYM,))
    mi = M2MinuteInput(book, lookback_bars=5, period_minutes=period) if minutes else None
    strategies = strategies or [StrategyInput(StopEntry(), {"qty": "0.0100", "stop": str(STOP)}, (time(0, 40),))]
    w = M2World(tmp, strategies=strategies, caps=caps or caps_with_hld02(), minutes=mi)
    w.set_baseline()
    return w, book


def bar(start: datetime, close: str) -> MinuteBar:
    c = D(close)
    return MinuteBar(SYM, 1, start, c, c + 1, c - 1, c, D("1"), NO_ADJUSTMENT)


def feed(book: SpotMinuteBook, hh: int, mm: int, close: str, *, extra: list[MinuteBar] | None = None) -> None:
    """hh:mm 격자 조회(hh:mm:03 받음)가 직전 닫힌 봉 hh:(mm-1)을 가져온 것처럼."""
    at = utc(hh, mm)
    book.put_minute_bars(SYM, [bar(at - timedelta(minutes=1), close)] + (extra or []), at + timedelta(seconds=3))


def minute(w: M2World, book: SpotMinuteBook, hh: int, mm: int, close: str | None, *, ss: int = 4) -> None:
    """한 분: (close가 있으면) 조회 결과를 넣고 hh:mm:ss tick + 체결 반영 tick."""
    if close is not None:
        feed(book, hh, mm, close)
    w.tick_at(hh, mm, ss)
    w.tick_at(hh, mm, ss + 20)


def enter(w: M2World, book: SpotMinuteBook) -> None:
    """00:30 시작·K8 재개 → 00:40 판정 진입(손절가 9500) → 체결 반영."""
    w.start_and_resume()
    feed(book, 0, 40, "10000")
    w.tick_at(0, 40, 5)
    for s in (10, 20, 30):
        w.tick_at(0, 40, s)
    buys = [r for r in w.sent() if r.side == "BUY"]
    assert len(buys) == 1, w.intents()
    assert w.repo.position(ACCOUNT, SYM).quantity > 0


def sells(w: M2World) -> list:
    return [r for r in w.sent() if r.side == "SELL"]


def stop_decisions(w: M2World) -> list[dict]:
    return w.repo.select("SELECT source, request_type, result, failed_check, idem_key, rule_id, owner, signal_id "
                         "FROM gate_decision WHERE rule_id=? ORDER BY seq", (STOP_RULE_ID,))


def stop_audits(w: M2World) -> list[dict]:
    import json
    return [json.loads(e["summary"]) | {"ref": e["ref"]} for e in w.audits("STOP_MONITOR")]


def alerts(w: M2World, level: str | None = None) -> list[str]:
    import json
    out = []
    for e in w.audits("ALERT"):
        s = json.loads(e["summary"])
        if level is None or s["level"] == level:
            out.append(s["message"])
    return out


# --- 순수 규칙 ----------------------------------------------------------------------------------------------------

def test_stop_triggered_only_at_or_below() -> None:
    assert stop_triggered(D("9499.99"), STOP) and stop_triggered(D("9500"), STOP) and stop_triggered(D("9500.00"), STOP)
    assert not stop_triggered(D("9500.01"), STOP)


# --- V-331 발동 ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("close", "expect"), [("9600", 0), ("9500", 1), ("9400", 1)])
def test_v331_closed_bar_close_vs_stop(tmp_path: Path, close: str, expect: int) -> None:
    w, book = make_world(tmp_path)
    try:
        enter(w, book)
        minute(w, book, 0, 42, close)
        minute(w, book, 0, 43, close)                 # 같은 상황이 계속돼도 한 번 보낸 청산이 체결되면 끝
        assert len(sells(w)) == expect
        if expect:
            [s] = sells(w)
            assert s.order_type is OrderType.MARKET   # 시세 정상 → 시장가 매도 (D-104)
            [d] = stop_decisions(w)
            assert (d["source"], d["request_type"], d["result"]) == ("SIGNAL", "EXIT", "APPROVED")
            assert d["owner"] == "t-m2stop:1" and d["idem_key"].startswith("stop|") and d["signal_id"] == d["idem_key"]
            req = [a for a in stop_audits(w) if a["action"] == "REQUESTED"]
            assert len(req) == 1 and req[0]["origin"] == STOP_ORIGIN and req[0]["close"] == close
            assert req[0]["stop_price"] == str(STOP) and req[0]["bar_start"] == utc(0, 41).isoformat()
            # 전량 청산(청산 가능 수량 전부 체결) → 남은 것은 매도 단위 미만 dust(수수료 차감분) → 감시 대상 아님 (D-102)
            assert w.engine.profile.sellable_position(SYM, "t-m2stop:1") is None
        else:
            assert stop_decisions(w) == []
    finally:
        w.close()


def test_v331_in_progress_minute_ignored(tmp_path: Path) -> None:
    w, book = make_world(tmp_path)
    try:
        enter(w, book)
        # 00:42:03 조회 — 닫힌 00:41 봉은 손절가 위, 진행 중 00:42 봉은 손절가 아래(공급이 걸러냄)
        feed(book, 0, 42, "9600", extra=[bar(utc(0, 42), "9000")])
        w.tick_at(0, 42, 4)
        # 저장소에 진행 중 분 봉이 들어 있어도(다른 경로) 감시는 직전 닫힌 봉만 본다
        with w.repo.tx():
            w.repo.insert_minute_bars([bar(utc(0, 42), "9000")], "binance-testnet", utc(0, 42, 30))
        w.tick_at(0, 42, 40)
        assert sells(w) == [] and stop_decisions(w) == []         # 진행 중 00:42 봉(9000)은 보지 않는다
        minute(w, book, 0, 43, "9700")                            # 00:43 — 00:42 봉이 닫힘: 저장 값(먼저 기록, 추가 전용) 9000
        assert len(sells(w)) == 1
        assert [a["bar_start"] for a in stop_audits(w) if a["action"] == "REQUESTED"] == [utc(0, 42).isoformat()]
    finally:
        w.close()


def test_v331_partial_fill_remaining_still_watched(tmp_path: Path) -> None:
    w, book = make_world(tmp_path)
    try:
        enter(w, book)
        held = w.repo.position(ACCOUNT, SYM).quantity
        # 시세 지연 → IOC 지정가 매도(D-120), 즉시 체결 가능 0.004만 → 나머지 EXPIRED
        w.broker.ioc_liquidity = {SYM: D("0.004")}
        feed(book, 0, 42, "9400")
        w.at(0, 42, 4, quote=False)
        w.quote(age_seconds=120)
        w.engine.tick()
        for s in (10, 20):
            w.at(0, 42, s, quote=False)
            w.engine.tick()
        [first] = sells(w)
        assert first.order_type is OrderType.LIMIT
        left = w.repo.position(ACCOUNT, SYM).quantity
        assert D("0") < left < held
        assert w.repo.active_ownership(ACCOUNT, SYM) is not None            # 남은 수량 → 계속 감시
        w.broker.ioc_liquidity = {}
        minute(w, book, 0, 43, "9450")                                       # 다음 닫힌 봉에서 새 판단
        assert len(sells(w)) == 2 and sells(w)[1].quantity <= left
        assert sells(w)[1].order_type is OrderType.MARKET
        assert len(stop_decisions(w)) == 2
        assert len({d["idem_key"] for d in stop_decisions(w)}) == 2          # 분마다 다른 신호 ID
    finally:
        w.close()


def test_v331_not_owned_or_undeclared_positions_zero(tmp_path: Path) -> None:
    """다른 전략(CAP-HLD-02 미선언, 손절가 있음)이 보유한 종목 — 감시 전략의 유니버스에 있어도 청산 요청 0 (R-28)."""
    other = StrategyInput(StopEntry("t-m2other", monitored=False), {"qty": "0.0100", "stop": str(STOP)},
                          (time(0, 40),))
    mine = StrategyInput(StopEntry(), {"qty": "0.0100", "stop": str(STOP)}, (time(0, 45),))
    w, book = make_world(tmp_path, strategies=[other, mine])
    try:
        enter(w, book)
        own = w.repo.active_ownership(ACCOUNT, SYM)
        assert own is not None and own.owner == "t-m2other:1"
        for mm in range(41, 48):
            minute(w, book, 0, mm, "9000")
        assert sells(w) == [] and stop_decisions(w) == []
        assert w.repo.position(ACCOUNT, SYM).quantity > 0
    finally:
        w.close()


def test_v331_no_stop_price_no_exit_warns_once(tmp_path: Path) -> None:
    s = StrategyInput(StopEntry(with_stop=False), {"qty": "0.0100", "stop": str(STOP)}, (time(0, 40),))
    w, book = make_world(tmp_path, strategies=[s])
    try:
        enter(w, book)
        for mm in (41, 42, 43):
            minute(w, book, 0, mm, "9000")
        assert sells(w) == [] and stop_decisions(w) == []
        assert len([m for m in alerts(w, "WARNING") if "손절가 없음" in m]) == 1
    finally:
        w.close()


# --- V-332 이상 ---------------------------------------------------------------------------------------------------

def test_v332_missing_minute_no_exit_then_warn_after_three(tmp_path: Path) -> None:
    w, book = make_world(tmp_path)
    try:
        enter(w, book)
        book.put_minute_bars(SYM, [], utc(0, 41, 3))         # 조회는 끝났지만 직전 닫힌 봉 없음
        minute(w, book, 0, 41, None)
        assert [m for m in alerts(w, "WARNING") if "손절 감시" in m] == []
        minute(w, book, 0, 42, None, ss=20)                   # 조회 없음 → 기다림 상한(15초) 넘김 = 결측
        book.minute_quality_failed(SYM, "고가·저가 모순", utc(0, 43, 3))   # 품질 실패 = 판단 불가
        minute(w, book, 0, 43, None)
        assert sells(w) == [] and stop_decisions(w) == []
        warns = [m for m in alerts(w, "WARNING") if "손절 감시" in m]
        assert len(warns) == 1 and "3회" in warns[0]
        unjudged = [a for a in stop_audits(w) if a["action"] == "NO_DATA"]
        assert [a["status"] for a in unjudged] == ["LAST_MISSING", "FETCH_TIMEOUT", "QUALITY"]
    finally:
        w.close()


def test_v332_missing_then_recovered_exits(tmp_path: Path) -> None:
    w, book = make_world(tmp_path)
    try:
        enter(w, book)
        for mm in (41, 42, 43):
            minute(w, book, 0, mm, None, ss=20)
        minute(w, book, 0, 44, "9400")
        assert len(sells(w)) == 1
        assert any("손절 감시" in m and "회복" in m for m in alerts(w, "INFO"))
    finally:
        w.close()


def test_v332_unknown_result_no_new_request(tmp_path: Path) -> None:
    w, book = make_world(tmp_path)
    try:
        enter(w, book)
        w.broker.behavior = FakeBehavior.UNKNOWN              # 손절 청산 전송 → 응답 없음(결과 불명)
        minute(w, book, 0, 42, "9400")
        assert len(sells(w)) == 1
        unknown = w.repo.select("SELECT state FROM intent WHERE request_type='EXIT'")
        assert [r["state"] for r in unknown] == ["UNKNOWN"]
        for mm in range(43, 48):
            minute(w, book, 0, mm, "9300")
        assert len(sells(w)) == 1 and len(stop_decisions(w)) == 1   # 해소 전 새 요청 0 (§8-6)
        assert len(w.repo.select("SELECT 1 FROM intent WHERE request_type='EXIT'")) == 1
        assert any(a["action"] == "SKIPPED" and "결과 불명" in a["reason"] for a in stop_audits(w))
    finally:
        w.close()


def test_v332_k2_allows_stop_exit(tmp_path: Path) -> None:
    w, book = make_world(tmp_path)
    try:
        enter(w, book)
        w.engine.halts.activate(HaltType.K2, "test", "일일 손실 한도", ACCOUNT)
        minute(w, book, 0, 42, "9400")
        assert len(sells(w)) == 1
        [d] = stop_decisions(w)
        assert d["result"] == "APPROVED"
    finally:
        w.close()


@pytest.mark.parametrize("halt", [HaltType.K1, HaltType.K8])
def test_v332_k1_k8_no_request_then_resume(tmp_path: Path, halt: HaltType) -> None:
    w, book = make_world(tmp_path)
    try:
        enter(w, book)
        w.engine.halts.activate(halt, "test", "시험 정지", ACCOUNT if halt is HaltType.K1 else None)
        for mm in (42, 43):
            minute(w, book, 0, mm, "9400")
        assert sells(w) == [] and stop_decisions(w) == []     # 01 §2.6: K1 = 운영 요청만, K8 = 허용 없음
        skips = [a for a in stop_audits(w) if a["action"] == "SKIPPED"]
        assert skips and all(f"정지 {halt.value}" in a["reason"] for a in skips)
        assert len([m for m in alerts(w, "WARNING") if "손절 감시" in m and f"정지 {halt.value}" in m]) == 1
        w.command("RESUME", {"account": ACCOUNT if halt is HaltType.K1 else None, "halt_type": halt.value})
        w.tick_at(0, 43, 50)
        assert halt.value not in w.repo.active_halts(ACCOUNT)
        minute(w, book, 0, 44, "9400")                        # 재개 뒤 다음 닫힌 봉에서 새 판단
        assert len(sells(w)) == 1
    finally:
        w.close()


def test_v332_restart_k8_no_request(tmp_path: Path) -> None:
    w, book = make_world(tmp_path)
    try:
        enter(w, book)
        w.restart()
        w.at(0, 41, 50)
        w.engine.start()                                      # 재시작 → K8 (재개 전)
        minute(w, book, 0, 42, "9400")
        minute(w, book, 0, 43, "9400")
        assert sells(w) == [] and stop_decisions(w) == []     # 재시작 직후 주문 없음 (§8-9)
    finally:
        w.close()


def test_v332_three_consecutive_expired_emergency(tmp_path: Path) -> None:
    w, book = make_world(tmp_path)
    try:
        enter(w, book)
        w.broker.behavior = FakeBehavior.ACCEPT_ONLY          # 접수만 → 소멸
        for mm in (42, 43, 44, 45):
            minute(w, book, 0, mm, "9400")
            assert len(sells(w)) == mm - 41                   # 직전 요청이 종결된 뒤 다음 닫힌 봉에서만 새 요청
            w.broker.expire_open()
        w.tick_at(0, 46, 4)
        emergency = [m for m in alerts(w, "EMERGENCY") if "손절 감시" in m]
        assert len(emergency) == 1 and "3회" in emergency[0]
        states = [r["state"] for r in w.repo.select("SELECT state FROM intent WHERE request_type='EXIT' "
                                                     "ORDER BY created_seq")]
        assert states[:3] == ["EXPIRED"] * 3
    finally:
        w.close()


def test_v332_active_exit_blocks_new_stop_request(tmp_path: Path) -> None:
    """미체결(접수만) 청산이 남아 있으면 다음 분에도 새 손절 요청 없음 — 자동 재전송 금지(§8-7)."""
    w, book = make_world(tmp_path)
    try:
        enter(w, book)
        w.broker.behavior = FakeBehavior.ACCEPT_ONLY
        for mm in (42, 43, 44):
            minute(w, book, 0, mm, "9400")
        assert len(sells(w)) == 1 and len(stop_decisions(w)) == 1
    finally:
        w.close()


# --- 등록·조립 (D-269) -------------------------------------------------------------------------------------------

def test_v332_m1_registration_rejected_even_if_table_allows(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "m1.db", Mode.BACKTEST)
    try:
        clock = FixedClock(utc(0, 0))
        spec = replace(stop_spec(), markets=("M1",), capabilities=("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01",
                                                                  "CAP-INS-01", STOP_CAPABILITY), universe=("005930",))
        si = StrategyInput(StopEntry(), {"qty": "1", "stop": "100"}, (time(9, 30),))
        si = replace(si, strategy=type("S", (), {"spec": spec, "evaluate": StopEntry().evaluate})())
        for caps in (caps_with_hld02(m1="검증 중"), caps_with_hld02()):
            registry = StrategyRegistry(repo, clock, None, market="M1")
            with pytest.raises(ConfigError) as e:
                wiring._register_strategies(registry, [si], caps, m2_limits(), Mode.BACKTEST, "KR-BT-01", "M1")  # noqa: SLF001
            assert STOP_CAPABILITY in str(e.value)
    finally:
        repo.close()


@pytest.mark.parametrize(("minutes", "period"), [(False, 1), (True, 5)])
def test_m2_assembly_requires_one_minute_feed(tmp_path: Path, minutes: bool, period: int) -> None:
    with pytest.raises(ConfigError) as e:
        make_world(tmp_path, minutes=minutes, period=period)
    assert STOP_CAPABILITY in str(e.value)


def test_m2_without_hld02_has_no_stop_monitor(tmp_path: Path) -> None:
    s = StrategyInput(StopEntry(monitored=False), {"qty": "0.0100", "stop": str(STOP)}, (time(0, 40),))
    w, book = make_world(tmp_path, strategies=[s], minutes=False)
    try:
        assert w.engine.stop_monitor is None
        enter(w, book)
        for mm in (41, 42):
            minute(w, book, 0, mm, "9000")
        assert sells(w) == []
    finally:
        w.close()
