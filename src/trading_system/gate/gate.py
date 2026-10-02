"""중앙 주문 통제·리스크 게이트 (R-03, R-04, R-05, R-28, 01 §2.2, 03 §6.1).

- 모든 요청을 하나씩 판정한다 (저장소 트랜잭션 + 잠금으로 직렬화, 01 D-022).
- 검사·판정·의도·한도 예약·소유권 예약·멱등키를 한 저장 단위로 기록한다 (02 D-030, INV-03).
- 요청을 바꾸지 않는다. 승인 또는 거부만 한다 (00 D-009, 01 D-016).

T-001 범위 검사: G1·G2·G3·G4·G6·G7(L2·L3)·G9·G15. 나머지 G와 요청 유형별 세부 규칙은 T-003.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_system.domain.clock import Clock, to_iso
from trading_system.domain.enums import Check, GateResult, IntentState, Mode, RequestSource, RequestType, Side
from trading_system.domain.models import ApprovedIntent, GateDecision, GateRequest, Limits
from trading_system.storage.repository import Repository, new_id

# 모드별 허용 브로커 환경 (R-01)
_ALLOWED_ENV = {Mode.BACKTEST: {"fake"}, Mode.PAPER: {"mock"}, Mode.LIVE: {"real"}}
_OPEN_ORDER_STATES = {"ACCEPTED", "PARTIAL"}


@dataclass
class _Snapshot:
    symbol_after: Decimal | None = None
    symbol_limit: Decimal | None = None
    total_after: Decimal | None = None
    total_limit: Decimal | None = None


class _Reject(Exception):
    def __init__(self, check: Check, reason: str) -> None:
        super().__init__(reason)
        self.check = check
        self.reason = reason


class Gate:
    def __init__(self, repo: Repository, clock: Clock, limits: Mapping[str, Limits],
                 price_of: Callable[[str], Decimal]) -> None:
        self._repo = repo
        self._clock = clock
        self._limits = dict(limits)
        self._price_of = price_of

    # --- 공개 API ---------------------------------------------------------------
    def decide(self, req: GateRequest) -> GateDecision:
        """요청을 판정하고 기록한다. 저장 실패 시 StorageFailure를 그대로 올린다 (G15, 전송 없음)."""
        with self._repo.tx():
            now = self._clock.now()
            decision_id = new_id("gd")
            passed: list[tuple[Check, bool]] = []
            snap = _Snapshot()
            try:
                self._run_checks(req, now, passed, snap)
            except _Reject as rej:
                passed.append((rej.check, False))
                self._record_decision(decision_id, req, now, GateResult.REJECTED, rej.check, rej.reason, passed, snap)
                return GateDecision(decision_id, GateResult.REJECTED, rej.check, rej.reason, None, tuple(passed))
            self._record_decision(decision_id, req, now, GateResult.APPROVED, None, None, passed, snap)
            approved = self._create_intent(decision_id, req, now, snap)
            return GateDecision(decision_id, GateResult.APPROVED, None, None, approved, tuple(passed))

    # --- 검사 ---------------------------------------------------------------
    def _run_checks(self, req: GateRequest, now: datetime, passed: list, snap: _Snapshot) -> None:
        for check, fn in (
            (Check.G1, self._g1_mode),
            (Check.G2, self._g2_halt),
            (Check.G3, self._g3_format_and_expiry),
            (Check.G4, self._g4_duplicate),
            (Check.G6, self._g6_ownership),
            (Check.G7, self._g7_limits),
            (Check.G9, self._g9_sell_quantity),
            (Check.G10, self._g10_quantity_price),
        ):
            fn(req, now, snap)
            passed.append((check, True))

    def _g1_mode(self, req: GateRequest, now: datetime, snap: _Snapshot) -> None:
        acct = self._repo.account(req.account)
        if acct is None:
            raise _Reject(Check.G1, "등록되지 않은 계좌")
        if acct.mode is not self._repo.mode or acct.broker_env not in _ALLOWED_ENV[self._repo.mode]:
            raise _Reject(Check.G1, "모드·브로커 환경 불일치")

    def _g2_halt(self, req: GateRequest, now: datetime, snap: _Snapshot) -> None:
        halts = self._repo.active_halts(req.account)
        # T-001: 활성 정지가 하나라도 있으면 위험 확대 차단. 유형별 세부 동작(01 §2.6)은 T-005.
        if halts and req.is_risk_increasing:
            raise _Reject(Check.G2, f"정지 중 위험 확대 차단: {','.join(sorted(halts))}")

    def _g3_format_and_expiry(self, req: GateRequest, now: datetime, snap: _Snapshot) -> None:
        """요청 형식·출처 권한·기한 (01 §2.1·§2.2 G3, R-21)."""
        rt = req.request_type
        if rt.is_new_order and req.side is not rt.default_side:
            raise _Reject(Check.G3, f"요청 유형 {rt.value}와 방향 {req.side.value}가 맞지 않음")
        if req.is_risk_increasing and req.source is not RequestSource.SIGNAL:
            raise _Reject(Check.G3, "신규 진입·증액은 전략 신호로만 가능 (운영 요청·정책 요청 불가, R-21)")
        if rt is RequestType.ENTRY and not req.owner:
            raise _Reject(Check.G3, "신규 진입에 소유 전략이 없음")
        if not rt.is_new_order and not req.target_order_key:
            raise _Reject(Check.G3, "취소·정정 대상 주문이 없음")
        if req.valid_until is not None and now > req.valid_until:
            raise _Reject(Check.G3, "신호 유효 기한 만료")

    def _g4_duplicate(self, req: GateRequest, now: datetime, snap: _Snapshot) -> None:
        # 같은 멱등키로 이미 판정했으면 승인·거부와 무관하게 중복 (01 D-016: 거부된 요청은 재판정하지 않음)
        if self._repo.decision_exists(req.idem_key) or self._repo.intent_by_idem(req.idem_key) is not None:
            raise _Reject(Check.G4, "중복 요청 (멱등키)")
        if req.request_type.is_new_order:
            if self._repo.active_new_intents(req.account, req.instrument, req.side.value):
                raise _Reject(Check.G4, "같은 종목·방향에 처리 중인 의도 있음 (INV-15)")
            return
        target = self._repo.broker_order(req.target_order_key or "")
        if target is None or target.state not in _OPEN_ORDER_STATES:
            raise _Reject(Check.G4, "취소·정정 대상 주문이 없거나 종결됨 (INV-17)")
        if self._repo.active_cancel_for(target.order_key):
            raise _Reject(Check.G4, "같은 주문에 처리 중인 취소·정정 있음 (INV-17)")

    def _g6_ownership(self, req: GateRequest, now: datetime, snap: _Snapshot) -> None:
        if not req.request_type.is_new_order:
            return
        own = self._repo.active_ownership(req.account, req.instrument)
        if req.request_type is RequestType.ENTRY:
            if own is not None and own.owner != req.owner:
                raise _Reject(Check.G6, f"다른 전략 소유 종목 ({own.owner})")
            return
        if req.source is RequestSource.OPERATOR:
            return  # 운영 요청의 감액·청산은 소유자와 무관 (01 §2.5)
        if own is None or own.owner != req.owner:
            raise _Reject(Check.G6, "소유하지 않은 종목")

    def _g7_limits(self, req: GateRequest, now: datetime, snap: _Snapshot) -> None:
        if not req.is_risk_increasing:
            return  # 위험 축소에는 한도 거부를 적용하지 않음 (01 D-019)
        limits = self._limits.get(req.account)
        if limits is None:
            raise _Reject(Check.G7, "계좌 한도 설정 없음")
        new_amount = req.quantity * req.price
        symbol, total = Decimal("0"), Decimal("0")
        self._price(req.instrument)  # 요청 종목 시세가 없으면 판정하지 않는다
        for inst, pos in self._repo.positions(req.account).items():
            if pos.quantity == 0:
                continue
            amount = pos.quantity * self._price(inst)
            total += amount
            if inst == req.instrument:
                symbol += amount
        for r in self._repo.active_reservations(req.account):
            total += r.remaining
            if r.instrument == req.instrument:
                symbol += r.remaining
        snap.symbol_after = symbol + new_amount
        snap.total_after = total + new_amount
        snap.symbol_limit = limits.base_amount * limits.max_symbol_ratio
        snap.total_limit = limits.base_amount * limits.max_total_ratio
        if snap.symbol_after > snap.symbol_limit:
            raise _Reject(Check.G7, f"종목 비중 한도 초과 ({snap.symbol_after} > {snap.symbol_limit})")
        if snap.total_after > snap.total_limit:
            raise _Reject(Check.G7, f"총 노출 한도 초과 ({snap.total_after} > {snap.total_limit})")

    def _price(self, instrument: str) -> Decimal:
        """평가 가격. 없거나 이상하면 거부한다 (G11, 불확실하면 주문하지 않음). 신선도 검사는 T-003."""
        try:
            price = self._price_of(instrument)
        except (KeyError, LookupError):
            raise _Reject(Check.G11, f"시세 없음: {instrument}") from None
        if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
            raise _Reject(Check.G11, f"시세 이상: {instrument}")
        return price

    def _g10_quantity_price(self, req: GateRequest, now: datetime, snap: _Snapshot) -> None:
        """수량·가격 유효성 (G10 일부). 호가 단위·수량 단위는 T-003·T-010."""
        if not req.request_type.is_new_order:
            return
        if req.quantity <= 0:
            raise _Reject(Check.G10, f"수량이 0 이하: {req.quantity}")
        if req.quantity != req.quantity.to_integral_value():
            raise _Reject(Check.G10, f"정수 주가 아님: {req.quantity}")
        if req.price <= 0:
            raise _Reject(Check.G10, f"가격이 0 이하: {req.price}")

    def _g9_sell_quantity(self, req: GateRequest, now: datetime, snap: _Snapshot) -> None:
        if not (req.request_type.is_new_order and req.side is Side.SELL):
            return
        held = self._repo.position(req.account, req.instrument).quantity
        pending = sum((i.quantity - i.filled_quantity
                       for i in self._repo.active_new_intents(req.account, req.instrument, Side.SELL.value)),
                      Decimal("0"))
        if req.quantity > held - pending:
            raise _Reject(Check.G9, f"매도 수량 초과 (요청 {req.quantity}, 가능 {held - pending})")

    # --- 기록 ---------------------------------------------------------------
    def _record_decision(self, decision_id: str, req: GateRequest, now: datetime, result: GateResult,
                         failed: Check | None, reason: str | None, passed: list, snap: _Snapshot) -> None:
        def s(v: Decimal | None) -> str | None:
            return None if v is None else str(v)

        self._repo.insert_decision({
            "decision_id": decision_id, "created_at": to_iso(now), "source": req.source.value,
            "request_type": req.request_type.value, "account": req.account, "instrument": req.instrument,
            "side": req.side.value, "quantity": str(req.quantity), "price": str(req.price),
            "idem_key": req.idem_key, "signal_id": req.signal_id, "owner": req.owner,
            "target_order_key": req.target_order_key, "result": result.value,
            "failed_check": failed.value if failed else None, "reason": reason,
            "checks": json.dumps([(c.value, ok) for c, ok in passed]),
            "symbol_after": s(snap.symbol_after) if result is GateResult.APPROVED else None,
            "symbol_limit": s(snap.symbol_limit), "total_after": s(snap.total_after), "total_limit": s(snap.total_limit),
        })
        self._repo.audit(now, "GATE_DECISION", "gate", decision_id, {
            "result": result.value, "failed_check": failed.value if failed else None, "reason": reason,
            "request_type": req.request_type.value, "instrument": req.instrument, "source": req.source.value,
        })

    def _create_intent(self, decision_id: str, req: GateRequest, now: datetime, snap: _Snapshot) -> ApprovedIntent:
        intent_id = new_id("int")
        self._repo.insert_intent({
            "intent_id": intent_id, "decision_id": decision_id, "idem_key": req.idem_key,
            "signal_id": req.signal_id, "source": req.source.value, "request_type": req.request_type.value,
            "account": req.account, "instrument": req.instrument, "side": req.side.value,
            "quantity": str(req.quantity), "order_type": req.order_type.value, "price": str(req.price),
            "owner": req.owner, "target_order_key": req.target_order_key,
            "state": IntentState.PENDING_SEND.value, "filled_quantity": "0", "created_at": to_iso(now),
        })
        if req.is_risk_increasing:
            self._repo.insert_reservation(intent_id, req.account, req.instrument, req.quantity * req.price)
        if req.request_type is RequestType.ENTRY and self._repo.active_ownership(req.account, req.instrument) is None:
            if not req.owner:  # G3에서 이미 거부된다. 최적화(-O)에도 남도록 assert 대신 예외
                raise RuntimeError("소유 전략 없는 진입 의도")
            self._repo.insert_ownership(req.account, req.instrument, req.owner, intent_id, now)
        self._repo.audit(now, "INTENT_STATE", "gate", intent_id, {"state": IntentState.PENDING_SEND.value})
        return ApprovedIntent(intent_id=intent_id, decision_id=decision_id, request=req)
