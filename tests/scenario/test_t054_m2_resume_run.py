"""CP-037 T-054 — run 쪽 M2 K8 자동 재개 추가 조건 (D-226, V-297 (c)(d)) — 조립 지점(app/main._run_m2) 연결 확인.

네트워크 없음: 합성 거래소(binance_exchange_sim)·메모리 가짜 WS API. 키는 가상 값. run_main(market="M2", PAPER)을 짧게 두 번
돌린다 — 첫 번째로 M2 DB를 만들고, 그 DB에 AUTO 채널 RESUME K8을 넣은 뒤 서버 시각이 2초 어긋난 합성 거래소로 다시 시작한다.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tests.scenario.test_t048_m2_streams import _connector, _paper_root, _wall
from tests.support.binance_exchange_sim import ExchangeSim
from tests.support.binance_ws_fake import FakeWsApi
from trading_system.app.m2_relay import M2Relay
from trading_system.app.m2_resume_conditions import M2ResumeConditions
from trading_system.app.main import run_main
from trading_system.domain.enums import Mode
from trading_system.domain.market import Quote
from trading_system.marketdata.spot_facts import SpotFactsStore
from trading_system.storage.repository import OrderIdentity, Repository

from tests.support.m2_fakes import symbol_rules

T0 = datetime(2026, 10, 10, 1, 0, tzinfo=UTC)
SYM = "TSTA-USDT"


def _conditions(**over: object) -> tuple[M2ResumeConditions, SpotFactsStore]:
    facts = SpotFactsStore(now=lambda: T0, universe=(SYM,))
    facts.load_rules({SYM: symbol_rules(SYM)})
    facts.update_quote(Quote(SYM, Decimal("100"), T0 - timedelta(seconds=10)))
    kw: dict = dict(server_offset_ms=lambda: 20, warn_ms=500, facts=facts, symbols=(SYM,), stream_online=lambda: True,
                    now=lambda: T0, max_quote_age=timedelta(seconds=60))
    kw.update(over)
    return M2ResumeConditions(**kw), facts


def test_conditions_ok_returns_none() -> None:
    c, _ = _conditions()
    assert c.problems() is None


def test_conditions_each_failure_listed() -> None:
    def boom() -> int:
        raise TimeoutError("가상")
    c, facts = _conditions(server_offset_ms=boom, stream_online=lambda: False)
    facts.update_quote(Quote(SYM, Decimal("100"), T0 - timedelta(seconds=61)))
    why = c.problems()
    assert why is not None
    assert "서버 시각 조회 실패(TimeoutError)" in why and "사용자 스트림 미연결" in why and "61초" in why
    c2, facts2 = _conditions(server_offset_ms=lambda: 500)
    facts2.clear_rules()
    why2 = c2.problems()
    assert why2 is not None and "500ms" in why2 and "exchangeInfo 규칙" in why2


def test_relay_stream_online_tracks_states_immediately() -> None:
    relay = M2Relay("BN-PAPER-01")
    assert relay.stream_online is False                       # 연결 확인 전에는 미연결
    relay.stream_state("ONLINE", "가상")
    assert relay.stream_online is True
    relay.stream_state("OFFLINE", "가상")
    assert relay.stream_online is False
    relay.stream_state("RECONNECTED", "가상")
    assert relay.stream_online is True
    relay.stream_state("REFUSED", "가상")
    assert relay.stream_online is False


def test_run_m2_paper_auto_resume_rejected_when_server_time_off(tmp_path: Path) -> None:
    root = _paper_root(tmp_path, "\n[paper]\nconnection_approved = true\n")
    sim = ExchangeSim(_wall)
    assert run_main(root, {"TRADE_MODE": "PAPER"}, max_seconds=0.3, market="M2",
                    m2_connector=_connector(sim, FakeWsApi(sim))) == 0
    db = root / "var" / "paper" / "trading.db"
    repo = Repository(db, Mode.PAPER, order_identity=OrderIdentity.SYMBOL)
    try:
        now = datetime.now(UTC)
        with repo.tx():
            repo.insert_command("auto-test-1", "AUTO", "m2-supervisor", "RESUME",
                                {"account": "BN-PAPER-01", "halt_type": "K8",
                                 "not_after": (now + timedelta(minutes=10)).isoformat()}, now)
    finally:
        repo.close()
    skewed = ExchangeSim(lambda: _wall() + 2000)             # 바이낸스 서버 시각이 2초 앞섬
    assert run_main(root, {"TRADE_MODE": "PAPER"}, max_seconds=0.5, market="M2",
                    m2_connector=_connector(skewed, FakeWsApi(skewed))) == 0
    repo = Repository(db, Mode.PAPER, order_identity=OrderIdentity.SYMBOL)
    try:
        cmd = repo.command("auto-test-1")
        assert cmd is not None and cmd["status"] == "REJECTED", cmd
        assert "M2 자동 재개 조건 미충족" in cmd["result"] and "서버 시각 오차" in cmd["result"]
        assert repo.select("SELECT 1 FROM reconciliation WHERE trigger='AUTO_RESUME'") == []
        assert "K8" in repo.active_halts("BN-PAPER-01")
        assert skewed.order_posts() == []
        [audit] = [e for e in repo.audit_events() if e["kind"] == "OPS_COMMAND" and e["ref"] == "auto-test-1"]
        assert json.loads(audit["summary"])["status"] == "REJECTED"
    finally:
        repo.close()
