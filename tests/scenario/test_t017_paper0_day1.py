"""T-017 PAPER-0 1일차(2026-10-06) 결함 수정.

1. K-U14 실측: 키움 모의 kt00009 cntr_no는 실시간 909(체결번호)와 다른 값(주문번호와 같음)이었다 →
   체결번호 비교로는 같은 체결을 누락 체결로 잘못 잡아 K4. 주문별 체결 수량 합으로 대조한다.
2. 열린 불일치 항목이 그대로인데 대조마다(1분) EMERGENCY 재발송 → 하루 361건, 텔레그램 K11 유발.
   새 항목이 생길 때만 EMERGENCY, 미해소 상기는 60분마다 WARNING 1건.
3. 실시간 1h(VI)가 종목코드에 거래소 접미사(_AL·_NX)를 붙여 옴 → 해석 실패로 버림(우리 종목 VI도 놓침).
모든 값은 가상이다.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest

from tests.scenario.conftest import ACCOUNT, Env
from tests.scenario.test_t008_reconcile import enter, item, kinds, resolver
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.broker.kiwoom import parse
from trading_system.domain.models import FillRecord
from trading_system.reconciliation.reconciler import ItemKind, Reconciler


class ListSink:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []

    def alert(self, level: str, message: str, ref: str | None) -> None:
        self.items.append((level, message))

    def levels(self, prefix: str) -> list[str]:
        return [lv for lv, m in self.items if m.startswith(prefix)]


def rec(env: Env, sink: ListSink | None = None) -> Reconciler:
    return Reconciler(env.repo, env.broker, env.clock, env.halts, sink or ListSink())


def query_fill_id_is_order_no(env: Env) -> None:
    """키움 모의 실측과 같게: 조회 체결의 체결번호 자리에 주문번호가 온다 (실시간 체결번호와 다름)."""
    original = env.broker.query_fills

    def q(account: str) -> list[FillRecord]:
        return [FillRecord(f.order_no, f.order_no, f.quantity, f.price, f.at) for f in original(account)]
    env.broker.query_fills = q  # type: ignore[method-assign]


# --- 1. K-U14: 체결번호가 경로마다 달라도 같은 체결은 일치 ------------------------------------------

def test_same_fill_with_different_fill_numbers_matches(env: Env) -> None:
    enter(env, "TST-A", "10")
    query_fill_id_is_order_no(env)
    r = rec(env).run(ACCOUNT, "PERIODIC")
    assert r.result == "MATCH", kinds(env)
    assert kinds(env) == []


def test_missing_fill_still_detected_by_quantity(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = enter(env, "TST-A", "10")
    order = env.repo.broker_order(env.repo.intent(d.approved.intent_id).broker_order_key)
    env.broker.fill(order.order_no, Decimal("4"))
    env.broker.drop_events()          # 실시간 체결 통보 유실
    query_fill_id_is_order_no(env)
    rec(env).run(ACCOUNT, "PERIODIC")
    it = item(env, ItemKind.MISSING_FILL)
    detail = json.loads(it["detail"])
    assert detail["quantity"] == "4" and detail["broker_total"] == "4" and detail["internal_total"] == "0"
    resolver(env).resolve(it["item_id"], "APPLY_FILL", "u")
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("4")


def test_partial_missing_fill_reports_only_the_difference(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = enter(env, "TST-A", "10")
    order = env.repo.broker_order(env.repo.intent(d.approved.intent_id).broker_order_key)
    env.broker.fill(order.order_no, Decimal("3"))
    env.executor.process_events()     # 첫 3주는 통보로 반영
    env.broker.fill(order.order_no, Decimal("2"))
    env.broker.drop_events()          # 다음 2주 통보 유실
    query_fill_id_is_order_no(env)
    rec(env).run(ACCOUNT, "PERIODIC")
    detail = json.loads(item(env, ItemKind.MISSING_FILL)["detail"])
    assert (detail["quantity"], detail["broker_total"], detail["internal_total"]) == ("2", "5", "3")


def test_open_missing_fill_item_auto_closes_when_quantities_match(env: Env) -> None:
    """1일차에 열린 항목(체결번호 차이로 생긴 오판)은 수량이 맞으면 자동 종결. 원장은 바꾸지 않는다."""
    d = enter(env, "TST-A", "10")
    order = env.repo.broker_order(env.repo.intent(d.approved.intent_id).broker_order_key)
    now = env.clock.now()
    with env.repo.tx():
        rid = env.repo.insert_reconciliation(ACCOUNT, "PERIODIC", "MISMATCH", {}, now)
        env.repo.insert_recon_item(rid, ACCOUNT, ItemKind.MISSING_FILL, "TST-A", order.order_no, None, "10@10000",
                                   json.dumps({"order_key": order.order_key, "fill_id": order.order_no,
                                               "quantity": "10", "price": "10000", "at": now.isoformat()}))
    query_fill_id_is_order_no(env)
    before = env.repo.position(ACCOUNT, "TST-A").quantity
    r = rec(env).run(ACCOUNT, "PERIODIC")
    assert r.result == "MATCH" and kinds(env) == []
    assert env.repo.position(ACCOUNT, "TST-A").quantity == before
    assert any(e["kind"] == "RECON_ITEM_AUTO_RESOLVED" for e in env.repo.audit_events())


def test_open_missing_fill_item_stays_when_internal_is_still_short(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = enter(env, "TST-A", "10")
    order = env.repo.broker_order(env.repo.intent(d.approved.intent_id).broker_order_key)
    env.broker.fill(order.order_no)
    env.broker.drop_events()
    rec(env).run(ACCOUNT, "PERIODIC")
    rec(env).run(ACCOUNT, "PERIODIC")
    assert ItemKind.MISSING_FILL in kinds(env)


# --- 2. 미해소 불일치 알림 반복 억제 ------------------------------------------------------------

def test_emergency_only_for_new_items_then_hourly_reminder(env: Env) -> None:
    sink = ListSink()
    r = rec(env, sink)
    no = env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("3"))
    env.broker.fill(no)
    env.broker.drop_events()
    r.run(ACCOUNT, "PERIODIC")
    assert sink.levels("대조 불일치") == ["EMERGENCY"]
    for _ in range(10):               # 같은 항목이 열린 채 1분마다 대조
        env.clock.advance(timedelta(minutes=1))
        r.run(ACCOUNT, "PERIODIC")
    assert sink.levels("대조 불일치") == ["EMERGENCY"]
    env.clock.advance(timedelta(minutes=55))
    r.run(ACCOUNT, "PERIODIC")
    assert sink.levels("대조 불일치") == ["EMERGENCY", "WARNING"]
    env.clock.advance(timedelta(minutes=1))
    r.run(ACCOUNT, "PERIODIC")
    assert sink.levels("대조 불일치") == ["EMERGENCY", "WARNING"]


def test_new_item_while_others_open_alerts_again(env: Env) -> None:
    sink = ListSink()
    r = rec(env, sink)
    a = env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("3"))
    env.broker.fill(a)
    env.broker.drop_events()
    r.run(ACCOUNT, "PERIODIC")
    env.clock.advance(timedelta(minutes=1))
    b = env.broker.add_external_order(ACCOUNT, "TST-B", "BUY", Decimal("2"))
    env.broker.fill(b)
    env.broker.drop_events()
    r.run(ACCOUNT, "PERIODIC")
    assert sink.levels("대조 불일치") == ["EMERGENCY", "EMERGENCY"]


def test_k4_still_held_while_items_open_without_repeated_alert(env: Env) -> None:
    from trading_system.domain.enums import HaltType
    r = rec(env)
    no = env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("3"))
    env.broker.fill(no)
    env.broker.drop_events()
    r.run(ACCOUNT, "PERIODIC")
    env.clock.advance(timedelta(minutes=1))
    assert r.run(ACCOUNT, "PERIODIC").result == "MISMATCH"
    assert HaltType.K4 in env.repo.active_halts(ACCOUNT)


# --- 3. 실시간 1h 종목코드의 거래소 접미사 ------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [("005930", ("005930", None)), ("005930_AL", ("005930", "AL")),
                                          ("005930_NX", ("005930", "NX")), ("A005930", ("005930", None))])
def test_stock_code_with_venue(raw: str, expected: tuple[str, str | None]) -> None:
    assert parse.stock_code_with_venue(raw, "9001") == expected


@pytest.mark.parametrize("raw", ["005930_XX", "0059301", "005930_", "", "_AL"])
def test_stock_code_with_venue_rejects_unknown(raw: str) -> None:
    with pytest.raises(parse.KiwoomParseError):
        parse.stock_code_with_venue(raw, "9001")


def test_plain_stock_code_still_rejects_venue_suffix() -> None:
    with pytest.raises(parse.KiwoomParseError):
        parse.stock_code("005930_AL", "stk_cd")
