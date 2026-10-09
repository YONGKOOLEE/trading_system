"""M2 dust 처리 — 청산 생략·소유권 해제·알림 (CP-024 D-102, T-M2-04, V-131·V-132·V-160). M1은 쓰지 않는다.

판정 규칙은 domain.trading_rules.dust_verdict(거래소 규칙 그대로, 실제로 보낼 매도 경로의 필터). 이 모듈은 기록만 한다.
- 청산(전량 매도) 요청을 게이트에 보내기 전에 `screen_exit`: 확실한 dust면 전송하지 않고 "dust — 전송 안 함" 기록
  (감사 DUST_CONFIRMED) + 알림 + 소유권 해제(사유 "dust 확정"). 원장(포지션)·대조에는 그대로 남긴다.
- 금액 경계는 전송한다. 그 매도가 -1013(필터 거부, 매칭엔진 미도달)으로 명시 거부되면 `after_reject`가 dust로 확정한다.
- 매도 종결 뒤 남은 잔량이 확실한 dust면 `review`로 소유권을 해제한다(청산 신호를 기다리지 않음).
- 확정하지 않는 경우(보수): 규칙 모름, 보유 없음, 같은 종목에 활성 매도 의도·결과 불명 의도가 있음, 수동 보유(manual) 소유권.
[제안] 감액(일부 매도)은 대상이 아니다 — 요청 수량이 청산 가능 수량보다 작으면 판정하지 않고 게이트(G10)에 맡긴다.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal

from trading_system.domain.clock import Clock
from trading_system.domain.enums import RequestType, Side
from trading_system.domain.market import Quote
from trading_system.domain.models import MANUAL_OWNER, GateRequest, SubmitResult
from trading_system.domain.sinks import AlertSink
from trading_system.domain.trading_rules import (
    DustCheck, DustVerdict, SpotOrderPolicy, SymbolRules, dust_verdict, quote_is_fresh, stale_sell_price,
)
from trading_system.storage.repository import DUST_RELEASE_REASON, IntentRow, Repository

FILTER_REJECT_CODE = -1013   # 바이낸스 "didn't reach the Matching Engine" (D-112 명시 거부, BN §5)
DUST_CONFIRMED = "DUST_CONFIRMED"


class DustHandler:
    def __init__(self, repo: Repository, clock: Clock, alerts: AlertSink, *,
                 rules_of: Callable[[str], SymbolRules | None],
                 market_prices_of: Callable[[str], Sequence[Decimal]],
                 quote_of: Callable[[str], Quote | None],
                 policy: SpotOrderPolicy) -> None:
        self._repo = repo
        self._clock = clock
        self._alerts = alerts
        self._rules_of = rules_of
        self._prices_of = market_prices_of
        self._quote_of = quote_of
        self._policy = policy

    # --- 판정 ---------------------------------------------------------------------
    def check(self, account: str, instrument: str) -> DustCheck | None:
        """현재 보유의 dust 판정. 판정할 수 없으면(규칙 모름·보유 없음) None."""
        held = self._repo.position(account, instrument).quantity
        rules = self._rules_of(instrument)
        if held <= 0 or rules is None:
            return None
        quote = self._quote_of(instrument)
        stale = quote is None or not quote_is_fresh(quote, self._clock.now(), self._policy.quote_max_age)
        stale_price = None
        if stale and quote is not None and quote.price > 0:
            stale_price = stale_sell_price(quote.price, rules, self._policy.sell_buffer)
        return dust_verdict(held, rules, stale=stale, market_prices=tuple(self._prices_of(instrument)),
                            stale_price=stale_price)

    def _settled(self, account: str, instrument: str) -> bool:
        """같은 종목에 진행 중인 매도·결과 불명이 없을 때만 확정한다 (불확실하면 확정하지 않음)."""
        return not (self._repo.active_new_intents(account, instrument, Side.SELL.value)
                    or self._repo.unknown_intents(account, instrument))

    # --- 경로 ---------------------------------------------------------------------
    def screen_exit(self, req: GateRequest) -> bool:
        """청산 요청을 게이트에 보내기 전. 확실한 dust면 기록·알림·소유권 해제 후 True(호출자는 보내지 않는다)."""
        if req.side is not Side.SELL or req.request_type not in (RequestType.EXIT, RequestType.REDUCE):
            return False
        c = self.check(req.account, req.instrument)
        if c is None or c.verdict is DustVerdict.SEND:
            return False
        if req.request_type is RequestType.REDUCE and req.quantity < c.quantity:
            return False   # 일부 매도 — 대상 아님
        if not self._settled(req.account, req.instrument):
            return False
        self._confirm(req.account, req.instrument, c.verdict, c.quantity, f"청산 요청 {req.idem_key}")
        return True

    def after_reject(self, intent: IntentRow, result: SubmitResult) -> bool:
        """매도가 -1013으로 명시 거부됐고 그 매도가 금액 경계의 전량 청산이었으면 dust 확정 (D-102 (나), V-160)."""
        if result.code != FILTER_REJECT_CODE or intent.side != Side.SELL.value \
                or intent.request_type not in (RequestType.EXIT.value, RequestType.REDUCE.value):
            return False
        c = self.check(intent.account, intent.instrument)
        if c is None or c.verdict is not DustVerdict.SEND or not c.notional_boundary or intent.quantity < c.quantity:
            return False
        if not self._settled(intent.account, intent.instrument):
            return False
        self._confirm(intent.account, intent.instrument, DustVerdict.DUST_NOTIONAL, c.quantity, "-1013 거부")
        return True

    def review(self, account: str, instrument: str) -> bool:
        """매도 종결 뒤 잔량 점검: 확실한 dust이고 전략 소유권이 남아 있으면 해제한다."""
        own = self._repo.active_ownership(account, instrument)
        if own is None or own.owner == MANUAL_OWNER:
            return False
        c = self.check(account, instrument)
        if c is None or c.verdict is DustVerdict.SEND or not self._settled(account, instrument):
            return False
        self._confirm(account, instrument, c.verdict, c.quantity, "매도 뒤 잔량")
        return True

    # --- 기록 ---------------------------------------------------------------------
    def _confirm(self, account: str, instrument: str, verdict: DustVerdict, quantity: Decimal, cause: str) -> None:
        now = self._clock.now()
        held = self._repo.position(account, instrument).quantity
        with self._repo.tx():
            own = self._repo.active_ownership(account, instrument)
            released = own is not None and own.owner != MANUAL_OWNER
            if released and own is not None:
                self._repo.set_ownership_state(own.ownership_id, "RELEASED", DUST_RELEASE_REASON, now)
            self._repo.audit(now, DUST_CONFIRMED, "dust", account, {
                "instrument": instrument, "verdict": verdict.value, "quantity": str(quantity), "held": str(held),
                "cause": cause, "ownership_released": released})
        self._alerts.alert("WARNING", f"dust — 전송 안 함: {instrument} 잔량 {held} ({verdict.value}, {cause}). "
                                      "포지션 없음 취급, 원장·대조에는 유지", account)
