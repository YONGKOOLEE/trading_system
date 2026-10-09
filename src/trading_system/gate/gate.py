"""중앙 주문 통제·리스크 게이트 (R-03, R-04, R-05, R-14, R-21, R-27, R-28, 01 §2.2~§2.5·§2.8, 03 §6.1·§6.2).

- 모든 요청을 하나씩 판정한다 (저장소 트랜잭션 + 잠금으로 직렬화, 01 D-022).
- 검사·판정·의도·한도 예약·소유권 예약·멱등키를 한 저장 단위로 기록한다 (02 D-030, INV-03).
- 요청을 바꾸지 않는다. 승인 또는 거부만 한다 (00 D-009, 01 D-016).
- 검사는 G1~G15 번호 순서로 하고 처음 실패한 검사를 거부 사유로 기록한다. 이후 검사 결과도 진단용으로 기록한다.
- 판정에 필요한 사실(시세·종목·달력·거래 상태·현금·손익·시계·전략 상태)을 모르면 위험 확대를 거부한다.

G2는 정지 유형별 허용표(01 §2.6, domain/halts.py)를 따른다. 계좌 전체 대조 불일치(G5 계좌 범위)는 K4 정지로 막는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from trading_system.domain.clock import Clock, to_iso
from trading_system.domain.enums import (
    Check, GateResult, HaltType, IntentState, Mode, OrderType, RequestSource, RequestType, Side,
)
from trading_system.domain.halts import HaltView, RequestView, halt_rejection
from trading_system.domain.limits import AccountLimits
from trading_system.domain.market import BLOCKS_RISK_REDUCTION, InstrumentInfo, Quote, estimate_cost, price_limits
from trading_system.domain.models import ApprovedIntent, GateDecision, GateRequest
from trading_system.domain.trading_rules import filter_violation
from trading_system.storage.repository import BrokerOrderRow, Repository, new_id

from .ports import GateInputs, GatePolicy

# 모드별 허용 브로커 환경 (R-01)
_ALLOWED_ENV = {Mode.BACKTEST: {"fake"}, Mode.PAPER: {"mock"}, Mode.LIVE: {"real"}}
_OPEN_ORDER_STATES = {"ACCEPTED", "PARTIAL"}
_ZERO = Decimal("0")


class _Reject(Exception):
    def __init__(self, check: Check, reason: str) -> None:
        super().__init__(reason)
        self.check = check
        self.reason = reason


@dataclass
class _Ctx:
    """한 판정 안에서만 쓰는 계산 결과."""

    req: GateRequest
    now: datetime
    limits: AccountLimits | None = None
    exposure_add: Decimal = _ZERO     # 이 요청이 새로 예약할 노출 (× |배수|)
    cash_add: Decimal = _ZERO         # 이 요청이 새로 예약할 현금 (주문 금액 + 추정 비용)
    notional: Decimal | None = None   # 예약 가격 기준 주문 금액
    snapshot: dict[str, str] = field(default_factory=dict)


class Gate:
    def __init__(self, repo: Repository, clock: Clock, inputs: GateInputs, policy: GatePolicy) -> None:
        self._repo = repo
        self._clock = clock
        self._in = inputs
        self._policy = policy

    # --- 공개 API ---------------------------------------------------------------
    def decide(self, req: GateRequest) -> GateDecision:
        """요청을 판정하고 기록한다. 저장 실패 시 StorageFailure를 그대로 올린다 (G15, 전송 없음)."""
        with self._repo.tx():
            ctx = _Ctx(req, self._clock.now())
            ctx.limits = self._in.limits.effective_limits(req.account, ctx.now)
            results: list[tuple[Check, bool]] = []
            first: _Reject | None = None
            for check, fn in self._checks():
                try:
                    fn(ctx)
                    results.append((check, True))
                except _Reject as rej:
                    results.append((check, False))
                    first = first or rej
                except Exception:
                    if first is None:
                        raise
                    results.append((check, False))  # 이미 거부된 판정의 진단 검사 오류는 결과를 바꾸지 않는다
            decision_id = new_id("gd")
            if first is not None:
                self._record_decision(decision_id, ctx, GateResult.REJECTED, first, results)
                return GateDecision(decision_id, GateResult.REJECTED, first.check, first.reason, None, tuple(results))
            self._record_decision(decision_id, ctx, GateResult.APPROVED, None, results)
            approved = self._create_intent(decision_id, ctx)
            return GateDecision(decision_id, GateResult.APPROVED, None, None, approved, tuple(results))

    def _checks(self) -> tuple:
        return (
            (Check.G1, self._g1_mode),
            (Check.G2, self._g2_halt),
            (Check.G3, self._g3_format_and_expiry),
            (Check.G4, self._g4_duplicate),
            (Check.G5, self._g5_unknown),
            (Check.G6, self._g6_strategy_and_ownership),
            (Check.G7, self._g7_limits),
            (Check.G8, self._g8_cash),
            (Check.G9, self._g9_sell_quantity),
            (Check.G10, self._g10_quantity_price),
            (Check.G11, self._g11_freshness),
            (Check.G12, self._g12_session),
            (Check.G13, self._g13_trading_status),
            (Check.G14, self._g14_clock),
        )

    # --- G1~G6 ----------------------------------------------------------------
    def _g1_mode(self, ctx: _Ctx) -> None:
        acct = self._repo.account(ctx.req.account)
        if acct is None:
            raise _Reject(Check.G1, "등록되지 않은 계좌")
        if acct.mode is not self._repo.mode or acct.broker_env not in _ALLOWED_ENV[self._repo.mode]:
            raise _Reject(Check.G1, "모드·브로커 환경 불일치")

    def _g2_halt(self, ctx: _Ctx) -> None:
        """정지 유형별 허용 규칙 (01 §2.6). 해제되지 않은 모든 정지가 허용해야 통과 (D-021)."""
        req = ctx.req
        view = RequestView(req.source, req.request_type, req.side, req.instrument, req.owner)
        for row in self._repo.halt_rows(req.account):
            h = HaltView(HaltType(row["halt_type"]), row["scope_instrument"], row["scope_owner"], row["variant"])
            reason = halt_rejection(h, view)
            if reason:
                raise _Reject(Check.G2, reason)

    def _g3_format_and_expiry(self, ctx: _Ctx) -> None:
        """요청 형식·출처 권한·기한 (01 §2.1·§2.2 G3, R-21)."""
        req, rt = ctx.req, ctx.req.request_type
        if rt.is_new_order and req.side is not rt.default_side:
            raise _Reject(Check.G3, f"요청 유형 {rt.value}와 방향 {req.side.value}가 맞지 않음")
        if rt in (RequestType.ENTRY, RequestType.INCREASE) and req.source is not RequestSource.SIGNAL:
            raise _Reject(Check.G3, "신규 진입·증액은 전략 신호로만 가능 (운영 요청·정책 요청 불가, R-21)")
        if rt is RequestType.MODIFY and req.source is not RequestSource.EXEC_POLICY:
            raise _Reject(Check.G3, "정정은 실행 정책(미체결 정책)으로만 가능 (01 §2.1)")
        if rt is RequestType.CANCEL and req.source is RequestSource.SIGNAL:
            raise _Reject(Check.G3, "전략 신호는 취소를 낼 수 없음 (00 D-005)")
        if rt is RequestType.ENTRY and not req.owner:
            raise _Reject(Check.G3, "신규 진입에 소유 전략이 없음")
        if not rt.is_new_order and not req.target_order_key:
            raise _Reject(Check.G3, "취소·정정 대상 주문이 없음")
        if req.valid_until is not None and ctx.now > req.valid_until:
            raise _Reject(Check.G3, "신호 유효 기한 만료")
        if req.stop_price is not None:
            if rt not in (RequestType.ENTRY, RequestType.INCREASE):
                raise _Reject(Check.G3, "손절가는 진입·증액 신호에만 쓸 수 있음 (00 §2.5)")
            if req.stop_price <= 0 or req.stop_price >= req.price:
                raise _Reject(Check.G3, f"손절가 형식 오류: 진입 기준가({req.price})보다 낮은 양수여야 함")

    def _g4_duplicate(self, ctx: _Ctx) -> None:
        req = ctx.req
        # 같은 멱등키로 이미 판정했으면 승인·거부와 무관하게 중복 (01 D-016: 거부된 요청은 재판정하지 않음)
        if self._repo.decision_exists(req.idem_key) or self._repo.intent_by_idem(req.idem_key) is not None:
            raise _Reject(Check.G4, "중복 요청 (멱등키)")
        if req.request_type.is_new_order:
            if self._repo.active_new_intents(req.account, req.instrument, req.side.value):
                raise _Reject(Check.G4, "같은 종목·방향에 처리 중인 의도 있음 (INV-15)")
            return
        target = self._target(ctx)
        if target is None or target.state not in _OPEN_ORDER_STATES:
            raise _Reject(Check.G4, "취소·정정 대상 주문이 없거나 종결됨 (INV-17)")
        if target.account != req.account or target.instrument != req.instrument or target.side != req.side.value:
            raise _Reject(Check.G4, "취소·정정 요청의 계좌·종목·방향이 대상 주문과 다름")
        if req.source is not RequestSource.OPERATOR:
            # 정책 요청은 대상 주문 소유 전략의 이름으로만 낼 수 있다 (소유자를 비워 K9를 우회하는 것 방지, CP-018 R-8)
            target_owner = self._repo.intent(target.intent_id).owner if target.intent_id else None
            if req.owner != target_owner:
                raise _Reject(Check.G4, f"취소·정정 요청의 소유자({req.owner})가 대상 주문 소유자({target_owner})와 다름")
        if self._repo.active_cancel_for(target.order_key):
            raise _Reject(Check.G4, "같은 주문에 처리 중인 취소·정정 있음 (INV-17)")

    def _g5_unknown(self, ctx: _Ctx) -> None:
        """결과 불명 (01 D-018, §2.5, INV-02). 취소는 허용한다."""
        req = ctx.req
        if req.request_type is RequestType.CANCEL:
            return
        unknown = self._repo.unknown_intents(req.account, req.instrument)
        if not unknown:
            return
        if req.is_risk_increasing:
            raise _Reject(Check.G5, "같은 종목에 결과 불명 의도가 있어 위험 확대 거부 (D-018)")
        if any(u.side == Side.SELL.value and RequestType(u.request_type).is_new_order for u in unknown):
            raise _Reject(Check.G5, "같은 종목에 결과 불명 매도가 있어 매도 거부 (이중 매도 방지)")

    def _g6_strategy_and_ownership(self, ctx: _Ctx) -> None:
        req = ctx.req
        if not req.request_type.is_new_order:
            return
        if req.source is RequestSource.SIGNAL:
            if not req.owner:
                raise _Reject(Check.G6, "전략 신호에 소유 전략이 없음")
            reason = self._in.strategies.signal_rejection(req.owner, req.request_type)
            if reason:
                raise _Reject(Check.G6, f"전략 상태: {reason}")
        own = self._repo.active_ownership(req.account, req.instrument)
        if req.request_type is RequestType.ENTRY:
            if own is not None and own.owner != req.owner:
                raise _Reject(Check.G6, f"다른 소유자의 종목 ({own.owner})")
            return
        if req.source is RequestSource.OPERATOR and not req.is_risk_increasing:
            return  # 운영 요청의 감액·청산은 소유자와 무관 (01 §2.5)
        if own is None or own.owner != req.owner:
            raise _Reject(Check.G6, "소유하지 않은 종목")

    # --- G7 한도 ----------------------------------------------------------------
    def _g7_limits(self, ctx: _Ctx) -> None:
        """L1~L7·L10 (01 §2.3). L8은 G12. 위험 축소에는 적용하지 않는다 (01 D-019)."""
        req = ctx.req
        if not req.is_risk_increasing:
            return
        lim = ctx.limits
        if lim is None:
            raise _Reject(Check.G7, "계좌 한도 설정 없음")
        daily = self._in.account.daily_risk(req.account)
        if daily is None:
            raise _Reject(Check.G7, "일일 리스크 상태 없음 (기준 금액 모름)")
        if not daily.evaluable:
            raise _Reject(Check.G7, "평가 불능 (시세 신선도 초과, 01 §2.4)")
        base = daily.base_amount
        reserve_price = self._reserve_price(ctx)  # 시세가 없으면 여기서 G11로 거부
        notional = req.quantity * reserve_price
        info = self._info(req.instrument, Check.G7)
        exposure_new = notional * info.exposure_multiplier
        cash_new = notional + estimate_cost(notional, False, self._policy.cost_rates)
        target_rsv = None
        if req.request_type is RequestType.MODIFY:
            target = self._target(ctx)
            target_rsv = self._repo.reservation_for(target.intent_id) if target and target.intent_id else None
        old_exposure = target_rsv.remaining if target_rsv else _ZERO
        old_cash = target_rsv.cash_remaining if target_rsv else _ZERO
        # 정정은 원주문과 정정 후 주문 중 하나만 남으므로 최악 = 둘 중 큰 쪽. 더 커지는 만큼만 새로 예약한다.
        ctx.exposure_add = max(_ZERO, exposure_new - old_exposure)
        ctx.cash_add = max(_ZERO, cash_new - old_cash)
        ctx.notional = notional

        symbol = total = leveraged = _ZERO
        held_symbols: set[str] = set()
        for inst, pos in self._repo.positions(req.account).items():
            if pos.quantity == 0:
                continue
            held_symbols.add(inst)
            i = self._info(inst, Check.G7)
            amount = pos.quantity * self._valuation_price(inst, ctx) * i.exposure_multiplier
            total += amount
            leveraged += amount if i.is_leveraged else _ZERO
            symbol += amount if inst == req.instrument else _ZERO
        entry_symbols: set[str] = set()
        for r in self._repo.active_reservations(req.account):
            total += r.remaining
            symbol += r.remaining if r.instrument == req.instrument else _ZERO
            if self._info(r.instrument, Check.G7).is_leveraged:
                leveraged += r.remaining
            if self._repo.intent(r.intent_id).request_type == RequestType.ENTRY.value:
                entry_symbols.add(r.instrument)

        s = ctx.snapshot
        s.update(base_amount=str(base), notional=str(notional), exposure_add=str(ctx.exposure_add),
                 symbol_after=str(symbol + ctx.exposure_add), symbol_limit=str(base * lim.l2_max_symbol_ratio),
                 total_after=str(total + ctx.exposure_add), total_limit=str(base * lim.l3_max_total_ratio),
                 loss_ratio=str(daily.loss_ratio))

        # L1 1회 최대 손실: 손절가가 있으면 (진입 기준가 − 손절가) × 수량 × |배수|, 없으면 주문 금액 전체 × |배수|
        # 진입 기준가 = 예약 가격(지정가는 주문가, 시장가는 상한가) (01 L1·D-020 v0.5)
        if req.stop_price is not None:
            order_risk = max(_ZERO, reserve_price - req.stop_price) * req.quantity * info.exposure_multiplier
        else:
            order_risk = exposure_new
        s["order_risk"] = str(order_risk)
        if order_risk > base * lim.l1_max_loss_ratio:
            raise _Reject(Check.G7, f"L1 1회 최대 손실 초과 ({order_risk} > {base * lim.l1_max_loss_ratio})")
        if symbol + ctx.exposure_add > base * lim.l2_max_symbol_ratio:
            raise _Reject(Check.G7, f"L2 종목 비중 한도 초과 ({s['symbol_after']} > {s['symbol_limit']})")
        if total + ctx.exposure_add > base * lim.l3_max_total_ratio:
            raise _Reject(Check.G7, f"L3 총 노출 한도 초과 ({s['total_after']} > {s['total_limit']})")
        # L4 일일 손실: 도달하면 거부 (01 §2.3 "도달 시 정지 K2")
        if daily.loss_ratio >= lim.l4_daily_loss_ratio:
            raise _Reject(Check.G7, f"L4 일일 손실 한도 도달 ({daily.loss_ratio} ≥ {lim.l4_daily_loss_ratio})")
        if req.request_type in (RequestType.ENTRY, RequestType.INCREASE):
            day_start = self._policy.market_time.last_at_or_before(ctx.now, lim.daily_reference_time)
            count = self._repo.risk_increasing_order_count(req.account, day_start)
            s["orders_today"] = str(count)
            if count + 1 > lim.l5_daily_new_orders:
                raise _Reject(Check.G7, f"L5 일일 신규 주문 수 초과 ({count + 1} > {lim.l5_daily_new_orders})")
        if req.request_type is RequestType.ENTRY:
            slots = held_symbols | entry_symbols
            if req.instrument not in slots and len(slots) + 1 > lim.l6_max_positions:
                raise _Reject(Check.G7, f"L6 보유 종목 수 초과 ({len(slots) + 1} > {lim.l6_max_positions})")
        if notional > lim.l7_max_order_amount:
            raise _Reject(Check.G7, f"L7 1회 주문 최대 금액 초과 ({notional} > {lim.l7_max_order_amount})")
        if info.is_leveraged and lim.l10_leverage_total_ratio is not None:
            cap = base * lim.l10_leverage_total_ratio
            if leveraged + ctx.exposure_add > cap:
                raise _Reject(Check.G7, f"L10 레버리지 상품 총 노출 초과 ({leveraged + ctx.exposure_add} > {cap})")
        if req.source is RequestSource.SIGNAL and req.owner:
            self._strategy_limits(ctx, base, order_risk, symbol, notional)

    def _strategy_limits(self, ctx: _Ctx, base: Decimal, order_risk: Decimal, symbol: Decimal,
                         notional: Decimal) -> None:
        """전략별 한도 (01 §2.3.1-8): 실제 한도 = min(전략 한도, 계좌 한도). 계좌 한도는 이미 통과."""
        req = ctx.req
        sl = self._in.strategies.strategy_limits(req.owner or "")
        if sl is None:
            return
        if sl.l1_max_loss_ratio is not None and order_risk > base * sl.l1_max_loss_ratio:
            raise _Reject(Check.G7, f"전략 L1 초과 ({order_risk} > {base * sl.l1_max_loss_ratio})")
        if sl.l2_max_symbol_ratio is not None and symbol + ctx.exposure_add > base * sl.l2_max_symbol_ratio:
            raise _Reject(Check.G7, f"전략 L2 초과 ({symbol + ctx.exposure_add} > {base * sl.l2_max_symbol_ratio})")
        if sl.l7_max_order_amount is not None and notional > sl.l7_max_order_amount:
            raise _Reject(Check.G7, f"전략 L7 초과 ({notional} > {sl.l7_max_order_amount})")
        if sl.l3_max_total_ratio is not None:
            owned = self._owned_exposure(ctx, req.owner or "")
            if owned + ctx.exposure_add > base * sl.l3_max_total_ratio:
                raise _Reject(Check.G7, f"전략 L3 초과 ({owned + ctx.exposure_add} > {base * sl.l3_max_total_ratio})")

    def _owned_exposure(self, ctx: _Ctx, owner: str) -> Decimal:
        """이 전략이 소유한 종목의 보유 노출 + 이 전략 의도의 활성 예약."""
        req = ctx.req
        total = _ZERO
        for inst, pos in self._repo.positions(req.account).items():
            own = self._repo.active_ownership(req.account, inst)
            if pos.quantity > 0 and own is not None and own.owner == owner:
                info = self._info(inst, Check.G7)
                total += pos.quantity * self._valuation_price(inst, ctx) * info.exposure_multiplier
        for r in self._repo.active_reservations(req.account):
            if self._repo.intent(r.intent_id).owner == owner:
                total += r.remaining
        return total

    def _g8_cash(self, ctx: _Ctx) -> None:
        """주문 가능 현금 = min(브로커 주문가능현금, 예수금 − 활성 매수 예약 현금) (03 §6.2)."""
        req = ctx.req
        if not req.is_risk_increasing:
            return
        cash = self._in.account.cash(req.account)
        if cash is None:
            raise _Reject(Check.G8, "주문 가능 현금 모름")
        if ctx.notional is None:  # G7이 실패한 진단 경로
            raise _Reject(Check.G8, "주문 금액 계산 불가")
        reserved = sum((r.cash_remaining for r in self._repo.active_reservations(req.account)), _ZERO)
        available = min(cash.broker_orderable, cash.deposit - reserved)
        ctx.snapshot.update(cash_available=str(available), cash_need=str(ctx.cash_add))
        if ctx.cash_add > available:
            raise _Reject(Check.G8, f"주문 가능 현금 부족 (필요 {ctx.cash_add}, 가능 {available})")

    def _g9_sell_quantity(self, ctx: _Ctx) -> None:
        req = ctx.req
        if not (req.request_type.is_new_order and req.side is Side.SELL):
            return
        held = self._repo.position(req.account, req.instrument).quantity
        pending = sum((i.quantity - i.filled_quantity
                       for i in self._repo.active_new_intents(req.account, req.instrument, Side.SELL.value)), _ZERO)
        if req.quantity > held - pending:
            raise _Reject(Check.G9, f"매도 수량 초과 (요청 {req.quantity}, 가능 {held - pending})")

    # --- G10~G14 ---------------------------------------------------------------
    def _g10_quantity_price(self, ctx: _Ctx) -> None:
        """수량·가격·호가 단위 (01 G10)."""
        req = ctx.req
        if req.request_type is RequestType.CANCEL:
            return
        if self._in.spot_rules is not None and self._g10_spot(ctx):
            return
        if req.quantity <= 0:
            raise _Reject(Check.G10, f"수량이 0 이하: {req.quantity}")
        info = self._info(req.instrument, Check.G10)
        if req.quantity % info.lot_size != 0:
            raise _Reject(Check.G10, f"수량 단위({info.lot_size})의 배수가 아님: {req.quantity}")
        if req.request_type is RequestType.MODIFY:
            target = self._target(ctx)
            if target is None:
                raise _Reject(Check.G10, "정정 대상 주문 없음")
            left = target.quantity - target.filled_quantity
            if req.quantity > left:
                raise _Reject(Check.G10, f"수량 증가 정정 불가 (요청 {req.quantity}, 미체결 {left})")
            if req.quantity != left:
                # 일부 수량 정정 시 원주문 잔량 처리가 미확인(K-U11, PV-14)이라 잔량 전부만 허용 (01 §2.1 v0.5)
                raise _Reject(Check.G10, f"정정 수량은 미체결 잔량 전부만 허용 (요청 {req.quantity}, 미체결 {left})")
        if req.price <= 0:
            raise _Reject(Check.G10, f"가격이 0 이하: {req.price}")
        if req.order_type is not OrderType.LIMIT:
            return
        tick = self._in.market.tick_valid(req.instrument, req.price)
        if tick is None:
            raise _Reject(Check.G10, "호가 단위 규칙 없음")
        if not tick:
            raise _Reject(Check.G10, f"호가 단위에 맞지 않는 가격: {req.price}")
        self._g10_price_band(req)

    def _g10_price_band(self, req: GateRequest) -> None:
        """지정가 상·하한가 사전 검사 (01 G10, CP-032 D-174). 게이트는 가격을 고치지 않는다(맞춤은 의도를 만들 때).

        - 아는 상·하한가 밖이면 거부 (확실한 위반은 요청 유형과 무관).
        - 상·하한가 중 하나라도 모르면: 위험 확대(신규·증액·매수 정정)는 거부. 위험 축소는 기준가(전일 종가)가 있으면
          기준가 × (1 ± 가격제한폭)을 호가 단위로 맞춘 범위(상한 내림·하한 올림)로 검사, 기준가도 모르면 허용
          (§8-3 위험 축소를 미리 막지 않음 — 위반이면 브로커 거부로 명시 거부 종결).
        """
        q = self._in.market.quote(req.instrument)
        upper = q.upper_limit if q is not None else None
        lower = q.lower_limit if q is not None else None
        if (upper is not None and req.price > upper) or (lower is not None and req.price < lower):
            raise _Reject(Check.G10, f"가격제한폭 밖의 가격: {req.price}")
        if upper is not None and lower is not None:
            return
        if req.is_risk_increasing:
            raise _Reject(Check.G10, "상·하한가 모름 — 위험 확대 지정가 거부 (CP-032 D-174)")
        base = q.base_price if q is not None else None
        if base is None:
            return
        band = self._base_band(req.instrument, base)
        if band is None:
            return    # 기준가 범위의 호가 단위를 모름 = 기준가 범위 모름 → 기준가 모름과 같게 허용
        hi, lo = band
        if req.price > hi or req.price < lo:
            raise _Reject(Check.G10, f"기준가 {base} ±{self._policy.price_limit_ratio} 범위({lo}~{hi}) 밖의 가격: "
                                     f"{req.price} (상·하한가 모름)")

    def _base_band(self, instrument: str, base: Decimal) -> tuple[Decimal, Decimal] | None:
        ticks: dict[Decimal, Decimal | None] = {}

        def tick_at(p: Decimal) -> Decimal:
            ticks[p] = self._in.market.tick_of(instrument, p)
            return ticks[p] or Decimal("1")

        hi, lo = price_limits(base, self._policy.price_limit_ratio, tick_at)
        return None if any(t is None for t in ticks.values()) else (hi, lo)

    def _g10_spot(self, ctx: _Ctx) -> bool:
        """M2 현물 거래 규칙 사전 검사 (CP-024 D-104·D-106, V-133·V-134). 판정을 끝냈으면 True.

        규칙이 없으면(미적재·적재 실패) 위험 확대는 거부, 위험 축소는 기존 수량 단위 검사로 넘긴다(False).
        """
        req = ctx.req
        spot = self._in.spot_rules
        if spot is None:
            return False
        if req.request_type is RequestType.MODIFY:
            raise _Reject(Check.G10, "M2는 정정 미지원 (D-104)")
        if req.quantity <= 0:
            raise _Reject(Check.G10, f"수량이 0 이하: {req.quantity}")
        if req.price <= 0:
            raise _Reject(Check.G10, f"가격이 0 이하: {req.price}")
        rules = spot.rules(req.instrument)
        if rules is None:
            if req.is_risk_increasing:
                raise _Reject(Check.G10, f"거래 규칙 미적재 — 위험 확대 거부 (D-106): {req.instrument}")
            return False
        reason = filter_violation(rules, req.side, req.order_type, req.quantity, req.price,
                                  spot.market_prices(req.instrument))
        if reason:
            raise _Reject(Check.G10, f"거래 규칙 위반 {reason}")
        return True

    def _g11_freshness(self, ctx: _Ctx) -> None:
        """시세 신선도 (01 G11, Q-C): 위험 확대는 지연 시 거부, 위험 축소는 시장가만 거부."""
        req = ctx.req
        if req.request_type is RequestType.CANCEL:
            return
        if not req.is_risk_increasing and req.order_type is OrderType.LIMIT:
            return
        self._fresh_quote(req.instrument, ctx)

    def _g12_session(self, ctx: _Ctx) -> None:
        """거래 가능 시간·시장 달력 (01 G12, L8, R-14)."""
        req = ctx.req
        local = self._policy.market_time.local(ctx.now)
        session = self._in.market.session(local.date())
        if session is None:
            raise _Reject(Check.G12, "시장 달력 정보 없음")
        if not session.is_open:
            raise _Reject(Check.G12, "휴장일")
        if not session.in_regular(ctx.now):
            raise _Reject(Check.G12, "정규장 시간 밖")
        if req.is_risk_increasing:
            lim = ctx.limits
            if lim is None:
                raise _Reject(Check.G12, "주문 허용 시간대 설정 없음")
            t = local.time()
            if not (lim.order_window_start <= t < lim.order_window_end):
                raise _Reject(Check.G12, f"L8 주문 허용 시간대 밖 ({t:%H:%M:%S})")

    def _g13_trading_status(self, ctx: _Ctx) -> None:
        """종목 거래 상태 (01 §2.8, R-27). 취소는 막지 않는다 (거래정지 중 취소 가능 여부는 브로커 사양 미확인)."""
        req = ctx.req
        if req.request_type is RequestType.CANCEL:
            return
        statuses = self._in.market.trading_status(req.instrument)
        if req.is_risk_increasing:
            if statuses is None:
                raise _Reject(Check.G13, "종목 거래 상태 모름")
            if statuses:
                raise _Reject(Check.G13, f"거래 상태로 위험 확대 거부: {','.join(sorted(statuses))}")
            return
        if statuses and statuses & BLOCKS_RISK_REDUCTION:
            raise _Reject(Check.G13, f"거래 상태로 매도 불가: {','.join(sorted(statuses & BLOCKS_RISK_REDUCTION))}")

    def _g14_clock(self, ctx: _Ctx) -> None:
        """시계 오차 (R-24, 01 K6 Q-H): 위험 확대 거부, 위험 축소는 운영 요청(사람)만 허용."""
        req = ctx.req
        if req.request_type is RequestType.CANCEL:
            return
        if self._in.clock_health.clock_ok() is True:
            return
        if not req.is_risk_increasing and req.source is RequestSource.OPERATOR:
            return
        raise _Reject(Check.G14, "시계 오차 초과 또는 판정 불가")

    # --- 조회 도우미 ------------------------------------------------------------
    def _target(self, ctx: _Ctx) -> BrokerOrderRow | None:
        return self._repo.broker_order(ctx.req.target_order_key or "")

    def _info(self, instrument: str, check: Check) -> InstrumentInfo:
        info = self._in.market.instrument(instrument)
        if info is None:
            raise _Reject(check, f"종목 정보 없음: {instrument}")
        return info

    def _fresh_quote(self, instrument: str, ctx: _Ctx) -> Quote:
        q = self._in.market.quote(instrument)
        if q is None:
            raise _Reject(Check.G11, f"시세 없음: {instrument}")
        if q.price <= 0:
            raise _Reject(Check.G11, f"시세 이상: {instrument}")
        if ctx.now - q.at > self._policy.quote_max_age or q.at > ctx.now + self._policy.quote_max_age:
            raise _Reject(Check.G11, f"시세 신선도 초과: {instrument} ({to_iso(q.at)})")
        return q

    def _valuation_price(self, instrument: str, ctx: _Ctx) -> Decimal:
        """보유 평가 가격: 최근 체결가, 신선하지 않으면 평가 불능 → 거부 (01 §2.4)."""
        return self._fresh_quote(instrument, ctx).price

    def _reserve_price(self, ctx: _Ctx) -> Decimal:
        """예약 가격: 지정가는 주문가, 시장가는 상한가(없으면 최근가 × (1 + 가격제한폭)) (03 §6.2)."""
        req = ctx.req
        if req.order_type is OrderType.LIMIT:
            return req.price
        q = self._fresh_quote(req.instrument, ctx)
        if q.upper_limit is not None:
            return q.upper_limit
        return q.price * (1 + self._policy.price_limit_ratio)

    # --- 기록 ---------------------------------------------------------------
    def _record_decision(self, decision_id: str, ctx: _Ctx, result: GateResult, rej: _Reject | None,
                         results: list[tuple[Check, bool]]) -> None:
        req, s = ctx.req, ctx.snapshot
        approved = result is GateResult.APPROVED
        self._repo.insert_decision({
            "decision_id": decision_id, "created_at": to_iso(ctx.now), "source": req.source.value,
            "request_type": req.request_type.value, "account": req.account, "instrument": req.instrument,
            "side": req.side.value, "quantity": str(req.quantity), "price": str(req.price),
            "idem_key": req.idem_key, "signal_id": req.signal_id, "rule_id": req.rule_id, "owner": req.owner,
            "target_order_key": req.target_order_key, "result": result.value,
            "failed_check": rej.check.value if rej else None, "reason": rej.reason if rej else None,
            "checks": json.dumps([(c.value, ok) for c, ok in results]),
            "symbol_after": s.get("symbol_after") if approved else None,
            "symbol_limit": s.get("symbol_limit"), "total_after": s.get("total_after"),
            "total_limit": s.get("total_limit"),
            "settings_hash": ctx.limits.version_hash() if ctx.limits else None,
            "limit_snapshot": json.dumps(s, ensure_ascii=False, sort_keys=True) if s else None,
        })
        self._repo.audit(ctx.now, "GATE_DECISION", "gate", decision_id, {
            "result": result.value, "failed_check": rej.check.value if rej else None,
            "reason": rej.reason if rej else None, "request_type": req.request_type.value,
            "instrument": req.instrument, "source": req.source.value,
        })

    def _create_intent(self, decision_id: str, ctx: _Ctx) -> ApprovedIntent:
        req, now = ctx.req, ctx.now
        intent_id = new_id("int")
        self._repo.insert_intent({
            "intent_id": intent_id, "decision_id": decision_id, "idem_key": req.idem_key,
            "signal_id": req.signal_id, "source": req.source.value, "request_type": req.request_type.value,
            "account": req.account, "instrument": req.instrument, "side": req.side.value,
            "quantity": str(req.quantity), "order_type": req.order_type.value, "price": str(req.price),
            "owner": req.owner, "target_order_key": req.target_order_key,
            "stop_price": None if req.stop_price is None else str(req.stop_price),
            "state": IntentState.PENDING_SEND.value, "filled_quantity": "0", "created_at": to_iso(now),
        })
        if req.is_risk_increasing and ctx.exposure_add > 0:
            self._repo.insert_reservation(intent_id, req.account, req.instrument, ctx.exposure_add, ctx.cash_add)
        if req.request_type is RequestType.ENTRY and self._repo.active_ownership(req.account, req.instrument) is None:
            if not req.owner:  # G3에서 이미 거부된다. 최적화(-O)에도 남도록 assert 대신 예외
                raise RuntimeError("소유 전략 없는 진입 의도")
            self._repo.insert_ownership(req.account, req.instrument, req.owner, intent_id, now)
        self._repo.audit(now, "INTENT_STATE", "gate", intent_id, {"state": IntentState.PENDING_SEND.value})
        return ApprovedIntent(intent_id=intent_id, decision_id=decision_id, request=req)
