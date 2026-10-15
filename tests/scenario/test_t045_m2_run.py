"""CP-036 T-045 — M2 최소 종단 간 run (fake). D-201(엔진 하나 + 시장 프로파일)·D-202(build_engine 하나)·D-207(최소)·
D-209(시작 = K8)·D-215(주문은 판정·실행 스레드만). 검증 V-280·V-281(신호 경로)·V-283(e)(f). 네트워크 없음.

가상 심볼 TSTA-USDT·가상 계좌 BN-BT-01·가상 규칙·가상 1d 봉. 시각은 모두 UTC (판정 01:00·04:00 UTC, 일 경계 00:00 UTC).
M1 동작 불변(V-152)은 기존 M1 시험 전체 회귀로 확인한다(이 파일은 M2만).
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_tm2_08_market_split import make_m2_root
from tests.support.m2_engine_world import (
    ACCOUNT, CAPS_M2, DAY, SETTINGS, SYM, FixedM2Strategy, M2World, fixed_spec, utc,
)
from tests.support.m2_fakes import M2_ORDER_POLICY, QUOTE_ASSET, m2_limits, symbol_rules
from trading_system.app.main import run_main
from trading_system.app.wiring import SpotInputs, StrategyInput, build_engine
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.config.errors import ConfigError
from trading_system.domain.enums import (
    GateResult, HaltType, IntentState, Mode, OrderType, RequestSource, RequestType, Side,
)
from trading_system.domain.market_time import Market
from trading_system.domain.models import GateRequest
from trading_system.domain.strategy import evaluation_id_of
from trading_system.storage.repository import Repository

REPO = Path(__file__).resolve().parents[2]
D = Decimal
NEXT = DAY + timedelta(days=1)


@pytest.fixture
def w(tmp_path: Path):  # type: ignore[no-untyped-def]
    world = M2World(tmp_path)
    world.set_baseline()
    yield world
    world.close()


def _evaluations(w: M2World) -> list[str]:
    return [e["ref"] for e in w.audits("EVALUATION")]


def _ev_id(at: datetime) -> str:
    spec = fixed_spec()
    return evaluation_id_of(spec.strategy_id, spec.version, "M2", at.isoformat())


# --- V-280 판정 시각·일 경계 (UTC) ---------------------------------------------------------------

def test_v280_m2_evaluates_only_at_utc_times_with_m2_evaluation_id(w: M2World) -> None:
    """판정 시각 01:00·04:00은 UTC(KST 해석 아님 — KST 01:00 = UTC 16:00 전날), 판정 ID 시장 M2, 토요일에도 판정."""
    w.start_and_resume()
    w.tick_at(0, 59)
    assert _evaluations(w) == []
    w.tick_at(1, 0)
    assert _evaluations(w) == [_ev_id(utc(DAY, 1, 0))]
    w.tick_at(1, 2)
    w.tick_at(16, 0)                       # KST 01:00 (다음 날) — 판정 시각 아님
    w.tick_at(19, 0)                       # KST 04:00
    assert _evaluations(w) == [_ev_id(utc(DAY, 1, 0))]


def test_v280_late_tick_beyond_grace_skips_and_restart_does_not_reevaluate(w: M2World) -> None:
    w.start_and_resume()
    w.tick_at(1, 0)
    assert len(_evaluations(w)) == 1 and len(w.sent()) == 1
    w.at(1, 3)
    w.restart()                             # 01:03 재시작 → K8 → 01:06 재개 (같은 판정 시각 + 유예 안)
    w.start_and_resume()
    w.tick_at(1, 4)
    assert len(_evaluations(w)) == 1 and len(w.sent()) == 1    # T-044 M2판: 재판정 없음
    w.tick_at(4, 6)                         # 04:00 + 6분 > 유예 5분 → 건너뜀 기록
    skipped = [e for e in w.audits("EVALUATION_SKIPPED") if "유예" in e["summary"]]
    assert len(skipped) == 1 and len(_evaluations(w)) == 1


def test_v280_daily_base_and_report_at_utc_midnight_not_kst(w: M2World) -> None:
    w.start_and_resume()
    w.tick_at(1, 0)
    w.tick_at(14, 59)
    w.tick_at(15, 0)                        # KST 00:00 — 일 경계 아님
    w.tick_at(15, 1)
    days = [e["summary"] for e in w.audits("DAILY_BASE")]
    assert len(days) == 1 and f'"trade_date": "{DAY.isoformat()}"' in days[0] and '"market": "M2"' in days[0]
    w.tick_at(0, 0, day=NEXT)               # UTC 00:00 — 일 경계: 일 경계 대조 + 마감 거래일 보고 + 새 기준 금액
    w.tick_at(0, 1, day=NEXT)
    bases = [e["summary"] for e in w.audits("DAILY_BASE")]
    assert len(bases) == 2 and f'"trade_date": "{NEXT.isoformat()}"' in bases[1]
    triggers = [r["trigger"] for r in w.repo.select("SELECT trigger FROM reconciliation ORDER BY created_seq")]
    assert triggers[0] == "STARTUP" and triggers.count("DAY_BOUNDARY") == 1
    reports = w.repo.daily_reports(ACCOUNT)
    assert any(r["trade_day"] == DAY.isoformat() for r in reports)


# --- V-283(f) 정상 종단 간 + V-281 신호 경로 ---------------------------------------------------------

def test_v283f_round_trip_ioc_buy_market_sell_dust_release_and_reentry(tmp_path: Path) -> None:
    w = M2World(tmp_path, eval_times=(time(1, 0), time(2, 0), time(4, 0)))
    w.set_baseline()
    try:
        _round_trip(w)
    finally:
        w.close()


def _round_trip(w: M2World) -> None:
    w.engine.start()
    assert "K8" in w.repo.active_halts(ACCOUNT)
    w.tick_at(1, 0)                                              # K8 중 판정 시각 → 건너뜀, 주문 0
    assert w.sent() == [] and any("전역 정지" in e["summary"] for e in w.audits("EVALUATION_SKIPPED"))
    w.command("RESUME", {"account": None, "halt_type": "K8"})
    w.tick_at(1, 1)
    assert "K8" not in w.repo.active_halts(ACCOUNT) and w.sent() == []   # 지난 판정 시각은 다시 하지 않음
    w.tick_at(2, 0)
    [buy] = w.sent()
    # V-281 (a): 매수 = IOC 지정가, 가격 = 최근가 × 1.005 tick 내림, 고객 주문 ID 있음
    assert (buy.side, buy.order_type, buy.price, buy.quantity) == ("BUY", OrderType.LIMIT, D("10050.00"), D("0.0100"))
    assert buy.client_order_id
    w.tick_at(2, 2)                                              # 체결 반영 + 주기 대조
    pos = w.repo.position(ACCOUNT, SYM)
    assert pos.quantity == D("0.00999")                          # 순 수령 (수수료 = 기초 자산, D-101)
    assert w.repo.active_ownership(ACCOUNT, SYM).owner == "t-m2fixed:1"
    last = w.repo.select("SELECT result FROM reconciliation ORDER BY created_seq DESC LIMIT 1")[0]["result"]
    assert last == "MATCH"
    w.tick_at(4, 0)
    sell = w.sent()[-1]
    # V-281 (b): 매도 = 수량 지정 시장가, 수량 = 청산 가능 수량(step 0.0001 내림)
    assert (sell.side, sell.order_type, sell.quantity) == ("SELL", OrderType.MARKET, D("0.0099"))
    w.tick_at(4, 2)
    assert w.repo.position(ACCOUNT, SYM).quantity == D("0.00009")   # 잔량 = dust
    assert w.repo.active_ownership(ACCOUNT, SYM) is None            # dust 확정 → 소유권 해제
    assert len(w.audits("DUST_CONFIRMED")) == 1
    w.tick_at(0, 0, day=NEXT)
    w.tick_at(1, 0, day=NEXT)                                    # 다음 날 재진입 허용
    assert [r.side for r in w.sent()] == ["BUY", "SELL", "BUY"]
    assert w.repo.active_halts(ACCOUNT) == []


def test_v281c_stale_quote_exit_is_ioc_limit_sell_below_last_good(w: M2World) -> None:
    w.start_and_resume()
    w.tick_at(1, 0)
    w.tick_at(1, 2)
    w.at(4, 0, quote=False)
    w.quote(age_seconds=61)                                      # 마지막 정상 시세가 61초 전
    w.engine.tick()
    sell = w.sent()[-1]
    assert (sell.side, sell.order_type, sell.price, sell.quantity) == ("SELL", OrderType.LIMIT, D("9950.00"),
                                                                       D("0.0099"))


def test_v281d_stale_quote_entry_rejected_by_gate_g11(w: M2World) -> None:
    w.start_and_resume()
    w.at(1, 0, quote=False)
    w.quote(age_seconds=61)
    w.engine.tick()
    assert w.sent() == []
    rows = w.repo.select("SELECT result, failed_check FROM gate_decision")
    assert rows and rows[-1]["result"] == GateResult.REJECTED.value and rows[-1]["failed_check"] == "G11"


def test_v281e_rules_not_loaded_entry_makes_no_intent_and_alerts(w: M2World) -> None:
    """규칙 미적재(직전 적재 실패, D-106): 판정 입력 data_ok 거짓 → 신호 없음. 의도 생성 시점에 규칙이 없으면(공급 스레드가
    그사이 규칙을 버린 경우) 의도를 만들지 않고 감사·알림."""
    w.start_and_resume()
    w.facts.clear_rules()
    w.tick_at(1, 0)
    assert w.sent() == [] and w.intents() == []
    req = GateRequest(source=RequestSource.SIGNAL, request_type=RequestType.ENTRY, account=ACCOUNT, instrument=SYM,
                      side=Side.BUY, quantity=D("0.0100"), order_type=OrderType.MARKET, price=D("10000.00"),
                      idem_key="t-terms-1", owner="t-m2fixed:1")
    assert w.engine.profile.prepare(req) is None
    assert w.sent() == [] and w.intents() == []
    [rec] = w.audits("ORDER_TERMS_UNAVAILABLE")
    assert SYM in rec["summary"] and "ENTRY" in rec["summary"]
    assert any("주문 유형·가격" in a["summary"] for a in w.audits("ALERT"))


def test_v281f_certain_dust_exit_request_not_sent_and_ownership_released(w: M2World) -> None:
    w.start_and_resume()
    w.tick_at(1, 0)
    w.tick_at(1, 2)
    big = replace(symbol_rules(SYM), market_min_qty=D("0.01"), lot_min_qty=D("0.01"))
    w.facts.load_rules({SYM: big})                               # 보유 0.00999 < minQty 0.01 → 확실한 dust
    req = GateRequest(source=RequestSource.SIGNAL,
        request_type=RequestType.EXIT, account=ACCOUNT, instrument=SYM, side=Side.SELL, quantity=D("0.0099"),
        order_type=OrderType.MARKET, price=D("10000.00"), idem_key="t-dust-1", owner="t-m2fixed:1")
    before = len(w.broker.sent)
    assert w.engine.profile.prepare(req) is None
    assert len(w.broker.sent) == before
    assert len(w.audits("DUST_CONFIRMED")) == 1 and w.repo.active_ownership(ACCOUNT, SYM) is None


# --- 주문 차단 5종 (각각 브로커 전송 0) -----------------------------------------------------------------

def test_block_kill_file(w: M2World) -> None:
    w.start_and_resume()
    (w.tmp / "KILL").write_text("KILL", encoding="utf-8")
    w.tick_at(1, 0)
    assert w.sent() == []


def test_block_k1(w: M2World) -> None:
    w.start_and_resume()
    w.engine.halts.activate(HaltType.K1, "tester", "시험 K1", ACCOUNT)
    w.tick_at(1, 0)
    assert w.sent() == []


def test_block_limit_exceeded(tmp_path: Path) -> None:
    w = M2World(tmp_path, qty="0.6000")                         # 0.6 × 10050 = 6030 USDT > L1 2000·L7 5000
    w.set_baseline()
    try:
        w.start_and_resume()
        w.tick_at(1, 0)
        assert w.sent() == []
        rows = w.repo.select("SELECT failed_check, reason FROM gate_decision")
        assert rows[-1]["failed_check"] == "G7" and "초과" in rows[-1]["reason"]
    finally:
        w.close()


def test_block_duplicate_signal(w: M2World) -> None:
    w.start_and_resume()
    w.broker.behavior = FakeBehavior.ACCEPT_ONLY
    w.broker.ioc_limit = False                                  # 접수만 — 같은 판정의 같은 신호가 다시 와도
    w.tick_at(1, 0)
    assert len(w.sent()) == 1
    w.engine.evaluated.clear()
    w.tick_at(1, 1)                                             # 같은 판정 ID·같은 신호 → 중복 의도 거부
    assert len(w.sent()) == 1


def test_block_storage_failure(w: M2World) -> None:
    w.start_and_resume()
    decide = w.engine.gate.decide

    def failing(req):  # type: ignore[no-untyped-def]
        w.repo.fail_next_transaction = True
        return decide(req)
    w.engine.gate.decide = failing  # type: ignore[method-assign]
    w.tick_at(1, 0)
    assert w.sent() == []


# --- V-283(e) 결과 불명이 남은 채 재시작 ------------------------------------------------------------

def test_v283e_restart_with_unknown_intent_k8_query_only_no_resend(tmp_path: Path) -> None:
    w = M2World(tmp_path, behavior=FakeBehavior.UNKNOWN)
    w.set_baseline()
    try:
        w.start_and_resume()
        w.tick_at(1, 0)
        assert len(w.sent()) == 1
        assert [i["state"] for i in w.intents()] == [IntentState.UNKNOWN.value]
        w.at(1, 3)
        w.restart()
        w.broker.behavior = FakeBehavior.FILL
        w.engine.start()
        assert "K8" in w.repo.active_halts(ACCOUNT)
        for hh, mm, ss in ((1, 3, 5), (1, 3, 20), (1, 3, 40), (1, 4, 20), (1, 5, 0), (1, 6, 0)):
            w.tick_at(hh, mm, ss)
        assert len(w.sent()) == 1                               # 해소는 조회만 — 같은 의도 재전송 0
        assert w.broker.lookups                                 # 고객 주문 ID 조회 (D-111)
        w.tick_at(4, 0)                                         # K8 재개 전: 판정 없음, 주문 0
        assert len(w.sent()) == 1
    finally:
        w.close()


# --- 조립 거부 (D-202) -------------------------------------------------------------------------------

def _build(tmp_path: Path, repo: Repository, strategies: list[StrategyInput]):  # type: ignore[no-untyped-def]
    from tests.support.m2_engine_world import PAIRS, ClockOk
    from trading_system.broker.fake.spot import FakeSpotBroker
    from trading_system.domain.clock import FixedClock
    from trading_system.marketdata.spot_facts import SpotFactsStore
    clock = FixedClock(utc(DAY, 0, 30))
    return build_engine(mode=Mode.BACKTEST, repo=repo, clock=clock, broker=FakeSpotBroker(now=clock.now), store=None,
                        account=ACCOUNT, account_limits=m2_limits(), gate_settings=SETTINGS, caps=CAPS_M2,
                        strategies=strategies, kill_file=tmp_path / "KILL", clock_health=ClockOk(), market="M2",
                        spot=SpotInputs(SpotFactsStore(now=clock.now, universe=(SYM,)), PAIRS, QUOTE_ASSET,
                                        M2_ORDER_POLICY))


def test_m2_build_refuses_trade_date_identity_repo(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "t.db", Mode.BACKTEST)          # M1 식별(거래일) 저장소
    try:
        with pytest.raises(ConfigError, match="SYMBOL"):
            _build(tmp_path, repo, [])
    finally:
        repo.close()


@pytest.mark.parametrize("over,needle", [
    ({"markets": ("M1",)}, "markets"),
    ({"universe_method": "U-B", "universe": (), "universe_list": "m2-list"}, "U-A"),
])
def test_m2_build_refuses_strategy_not_for_m2(tmp_path: Path, over: dict, needle: str) -> None:
    from trading_system.storage.repository import OrderIdentity
    repo = Repository(tmp_path / "t.db", Mode.BACKTEST, order_identity=OrderIdentity.SYMBOL)
    try:
        with pytest.raises(ConfigError, match=needle):
            _build(tmp_path, repo, [StrategyInput(FixedM2Strategy(fixed_spec(**over)), {"qty": "0.0100"},
                                                  (time(1, 0),))])
    finally:
        repo.close()


def test_m2_build_requires_spot_inputs(tmp_path: Path) -> None:
    from trading_system.storage.repository import OrderIdentity
    repo = Repository(tmp_path / "t.db", Mode.BACKTEST, order_identity=OrderIdentity.SYMBOL)
    try:
        with pytest.raises(ConfigError, match="M2"):
            build_engine(mode=Mode.BACKTEST, repo=repo, clock=None, broker=None, store=None,  # type: ignore[arg-type]
                         account=ACCOUNT, account_limits=m2_limits(), gate_settings=SETTINGS, caps=CAPS_M2,
                         strategies=[], kill_file=tmp_path / "KILL", clock_health=None,  # type: ignore[arg-type]
                         market="M2")
    finally:
        repo.close()


# --- run --market M2 (BACKTEST·fake, 오프라인) ---------------------------------------------------------

def _meta(db: Path) -> dict[str, str]:
    conn = sqlite3.connect(db)
    try:
        return dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()


def _m2_backtest_root(tmp_path: Path) -> Path:
    root = make_m2_root(tmp_path, Mode.BACKTEST)
    (root / "docs").mkdir()
    shutil.copy(REPO / "docs" / "capabilities.md", root / "docs" / "capabilities.md")
    return root


def test_run_m2_backtest_end_to_end_offline_starts_in_k8(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _m2_backtest_root(tmp_path)
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.3, market="M2") == 0
    out = capsys.readouterr().out
    assert "시장 M2" in out and "--market M2 K8" in out
    base = root / "var" / "backtest"
    assert (base / "heartbeat").exists()
    db = base / "trading.db"
    assert (_meta(db)["market"], _meta(db)["broker"]) == ("M2", "fake")
    repo = Repository(db, Mode.BACKTEST)
    try:
        assert "K8" in set(repo.active_halts("BN-BT-01"))            # 시작 = K8, 재개는 사용자만 (D-209)
        assert repo.select("SELECT trigger FROM reconciliation ORDER BY created_seq LIMIT 1")[0]["trigger"] == "STARTUP"
        assert repo.select("SELECT COUNT(*) AS n FROM intent")[0]["n"] == 0
    finally:
        repo.close()
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.1, market="M2") == 0   # 같은 DB로 다시 시작


def test_run_m2_on_m1_db_refused_before_engine(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from trading_system.storage.market_meta import bind_market
    root = _m2_backtest_root(tmp_path)
    db = root / "var" / "backtest" / "trading.db"
    db.parent.mkdir(parents=True)
    repo = Repository(db, Mode.BACKTEST)
    bind_market(repo.connection, Market.M1, "fake", created=True)
    repo.close()
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.1, market="M2") == 2
    assert "시작 거부" in capsys.readouterr().out
    assert not (root / "var" / "backtest" / "heartbeat").exists()


def test_run_m2_paper_still_refused_until_adapter(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """PAPER(바이낸스 테스트넷)는 어댑터(T-047)·연결 승인(T-050) 전 — 검사 뒤 거부, DB·잠금 없음."""
    root = make_m2_root(tmp_path, Mode.PAPER)
    assert run_main(root, {"TRADE_MODE": "PAPER"}, market="M2") == 2
    out = capsys.readouterr().out
    assert "시작 거부(브로커 연결 없음)" in out and "T-047" in out and "placeholder" not in out
    assert not (root / "var").exists()



