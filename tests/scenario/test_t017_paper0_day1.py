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


# --- 재검토 반영 (T-017 2차) ---------------------------------------------------------------------

def _kt00009(no: str, cntr: str, qty: str, px: str, tm: str) -> dict:
    return {"ord_no": no, "stk_cd": "A005930", "io_tp_nm": "현금매수", "ord_qty": "10", "ord_uv": "0",
            "trde_tp": "시장가", "cntr_no": cntr, "cntr_qty": qty, "cntr_uv": px, "cntr_tm": tm,
            "acpt_tp": "접수", "orig_ord_no": "", "mdfy_cncl_tp": ""}


def test_build_fills_counts_each_row_when_fill_number_is_order_number() -> None:
    from datetime import date
    from trading_system.broker.kiwoom.adapter import build_fills, build_orders
    rows = [_kt00009("0050626", "0050626", "3", "277750", "093000"),
            _kt00009("0050626", "0050626", "3", "277750", "093000"),   # 같은 내용의 두 번째 체결도 센다
            _kt00009("0050626", "0050626", "2", "277800", "093001")]
    fills = build_fills(rows, date(2026, 10, 6))
    assert sum(f.quantity for f in fills) == Decimal("8") and len(fills) == 3
    (o,) = build_orders(rows, [])
    assert o.filled_quantity == Decimal("8")


def test_build_fills_real_fill_numbers_still_deduplicated() -> None:
    from datetime import date
    from trading_system.broker.kiwoom.adapter import build_fills
    rows = [_kt00009("0050626", "0241353", "3", "277750", "093000"),
            _kt00009("0050626", "0241353", "3", "277750", "093000")]   # 같은 체결번호 중복 행 → 1건
    assert len(build_fills(rows, date(2026, 10, 6))) == 1


def _two_step_missing(env: Env):  # type: ignore[no-untyped-def]
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = enter(env, "TST-A", "10")
    order = env.repo.broker_order(env.repo.intent(d.approved.intent_id).broker_order_key)
    query_fill_id_is_order_no(env)
    env.broker.fill(order.order_no, Decimal("3"))
    env.broker.drop_events()
    rec(env).run(ACCOUNT, "PERIODIC")
    env.broker.fill(order.order_no, Decimal("2"))
    env.broker.drop_events()
    rec(env).run(ACCOUNT, "PERIODIC")
    return order


def test_apply_fill_refused_when_several_items_for_same_order(env: Env) -> None:
    from trading_system.ops.reconcile import ResolveError
    _two_step_missing(env)
    items = [i for i in env.repo.open_recon_items(ACCOUNT) if i["kind"] == ItemKind.MISSING_FILL]
    assert len(items) == 2
    with pytest.raises(ResolveError, match="여럿"):
        resolver(env).resolve(items[0]["item_id"], "APPLY_FILL", "u")
    assert env.repo.position(ACCOUNT, "TST-A").quantity == 0


def test_apply_fill_refused_when_internal_changed_since_item(env: Env) -> None:
    from trading_system.domain.models import OrderEvent
    from trading_system.ops.reconcile import ResolveError
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = enter(env, "TST-A", "10")
    order = env.repo.broker_order(env.repo.intent(d.approved.intent_id).broker_order_key)
    query_fill_id_is_order_no(env)
    env.broker.fill(order.order_no, Decimal("3"))
    env.broker.drop_events()
    rec(env).run(ACCOUNT, "PERIODIC")
    it = item(env, ItemKind.MISSING_FILL)
    env.executor.apply_event(OrderEvent(kind="FILL", order_no=order.order_no, fill_id="LATE1", quantity=Decimal("3"),
                                        price=Decimal("10000"), account=ACCOUNT,
                                        trade_date=order.order_key.split("|")[1]))   # 늦은 실시간 통보
    with pytest.raises(ResolveError, match="내부 체결이 바뀜"):
        resolver(env).resolve(it["item_id"], "APPLY_FILL", "u")
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("3")
    rec(env).run(ACCOUNT, "PERIODIC")       # 이제 수량이 같아 자동 종결
    assert ItemKind.MISSING_FILL not in kinds(env)


def test_internal_excess_fill_detected(env: Env) -> None:
    from trading_system.domain.models import OrderEvent
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = enter(env, "TST-A", "10")
    order = env.repo.broker_order(env.repo.intent(d.approved.intent_id).broker_order_key)
    query_fill_id_is_order_no(env)
    env.broker.fill(order.order_no, Decimal("3"))
    env.executor.process_events()
    env.executor.apply_event(OrderEvent(kind="FILL", order_no=order.order_no, fill_id="DUP", quantity=Decimal("2"),
                                        price=Decimal("10000"), account=ACCOUNT,
                                        trade_date=order.order_key.split("|")[1]))   # 이중 반영 가정
    rec(env).run(ACCOUNT, "PERIODIC")
    assert ItemKind.EXCESS_FILL in kinds(env)


def test_legacy_item_on_partial_order_is_not_auto_closed(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = enter(env, "TST-A", "10")
    order = env.repo.broker_order(env.repo.intent(d.approved.intent_id).broker_order_key)
    env.broker.fill(order.order_no, Decimal("3"))
    env.executor.process_events()
    now = env.clock.now()
    with env.repo.tx():   # 이전 형식(broker_total 없음) 항목, 다른 날 주문으로 가정해 이번 조회 값과 무관하게
        rid = env.repo.insert_reconciliation(ACCOUNT, "PERIODIC", "MISMATCH", {}, now)
        env.repo.insert_recon_item(rid, ACCOUNT, ItemKind.MISSING_FILL, "TST-A", order.order_no, None, "3@10000",
                                   json.dumps({"order_key": order.order_key.replace(order.order_key.split("|")[1],
                                                                                    "2000-01-01"),
                                               "fill_id": "X", "quantity": "3", "price": "10000",
                                               "at": now.isoformat()}))
    rec(env).run(ACCOUNT, "PERIODIC")
    assert ItemKind.MISSING_FILL in kinds(env)


def test_restart_reports_open_items_as_emergency_once(env: Env) -> None:
    no = env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("3"))
    env.broker.fill(no)
    env.broker.drop_events()
    rec(env).run(ACCOUNT, "PERIODIC")
    sink = ListSink()
    r2 = rec(env, sink)                      # 재시작된 프로세스
    r2.run(ACCOUNT, "STARTUP")
    env.clock.advance(timedelta(minutes=1))
    r2.run(ACCOUNT, "PERIODIC")
    assert sink.levels("대조 불일치") == ["EMERGENCY"]


def test_average_price_rounded_to_row_precision() -> None:
    from trading_system.reconciliation.reconciler import _avg_price
    t = __import__("datetime").datetime(2026, 10, 6, tzinfo=__import__("datetime").UTC)
    rows = [FillRecord("1", "1", Decimal("1"), Decimal("277750"), t),
            FillRecord("1", "1", Decimal("2"), Decimal("277800"), t)]
    assert _avg_price(rows) == Decimal("277783")


def test_day2_restart_closes_day1_item_and_k4_becomes_resumable(env: Env) -> None:
    """2026-10-07 아침 상황 재현: 1일차 오판 항목(이전 형식)과 K4가 열린 채 재시작 → 시작 대조 MATCH → K4 재개 가능."""
    from trading_system.domain.enums import HaltType
    d = enter(env, "TST-A", "1")
    order = env.repo.broker_order(env.repo.intent(d.approved.intent_id).broker_order_key)
    now = env.clock.now()
    with env.repo.tx():
        rid = env.repo.insert_reconciliation(ACCOUNT, "PERIODIC", "MISMATCH", {}, now)
        env.repo.insert_recon_item(rid, ACCOUNT, ItemKind.MISSING_FILL, "TST-A", order.order_no, None, "1@10000",
                                   json.dumps({"order_key": order.order_key, "fill_id": order.order_no,
                                               "quantity": "1", "price": "10000", "at": now.isoformat()}))
    env.halts.activate(HaltType.K4, "reconciler", "대조 불일치 1건 (PERIODIC)", ACCOUNT)
    env.clock.advance(timedelta(days=1))          # 다음 날: 어제 주문은 오늘 조회에 없다
    env.broker.new_day()
    query_fill_id_is_order_no(env)
    r = rec(env).run(ACCOUNT, "STARTUP")
    assert kinds(env) == [], [(i["kind"], i["detail"]) for i in env.repo.open_recon_items(ACCOUNT)]
    assert r.result == "MATCH"
    assert env.halts.resume(HaltType.K4, ACCOUNT, "LOCAL", "u").ok
