"""바이낸스 규칙·시세·상태 공급자 → 공통 계약 (CP-024 D-102·D-106·D-107·D-108·D-116·D-119, T-M2-11).

게이트 입력 gate.ports.MarketFacts(quote·instrument·trading_status·session·tick_valid·tick_of)와
SymbolRulesSource(rules·market_prices)를 구조적으로 구현한다. 바이낸스 전용 타입은 이 패키지 안에만 두고(D-119),
밖으로는 domain 값 객체(Quote·InstrumentInfo·TradingStatus·SessionInfo·trading_rules.SymbolRules)만 내보낸다.
네트워크 없음: 응답 본문(합성 또는 T-M2-14의 실제 수신)을 load_·update_ 메서드로 넣는다. 시각은 주입 시계.

규칙 (모르면 "없음" = 위험 확대 거부, CLAUDE.md §8-10)
- exchangeInfo(rules.parse_exchange_info): 전체 실패면 직전 규칙을 버린다 → rules()·trading_status()·rate_limits() None
  (D-106 "직전 적재 실패면 위험 확대 거부, 위험 축소 허용"). 종목 정보(수량 단위)·호가 단위는 마지막 정상 값을 유지한다 —
  위험 축소 요청이 게이트의 공통 수량·호가 단위 검사(G10)를 통과할 수 있게 하기 위함(위반이면 거래소 -1013 명시 거부).
- 유니버스 밖 심볼은 모두 None(규칙이 거래소에 있어도).
- [제안] minQty가 LOT_SIZE stepSize의 배수가 아니거나 minPrice가 tickSize의 배수가 아니면 그 심볼 규칙을 "없음"으로 본다
  (BN §6 LOT_SIZE 식 원문 대조 권장 — domain/trading_rules 주석의 시작 점검 항목). 사유는 rejected에 남긴다.
- 공통 규칙 값 연결(to_domain_rules): 시장가 수량은 MARKET_LOT_SIZE(없으면 LOT_SIZE), 명목가는 NOTIONAL(없으면
  MIN_NOTIONAL, 둘 다 없으면 최소 0·최대 없음). [제안] 둘 다 있으면 NOTIONAL을 쓴다(공존 여부 미확인 B-U4).
  PERCENT_PRICE(_BY_SIDE)·MAX_NUM_ORDERS는 공통 값 객체에 아직 없다(T-M2-03 범위 밖 — 거래소 -1013·-2010은 명시 거부).
- 심볼 상태(D-107, 의미 미확인 B-U13 → 보수 매핑): TRADING(+현물 허용) = 제한 없음(빈 집합), 그 밖(CANCEL_ONLY·
  END_OF_DAY·HALT·BREAK·기타·현물 불허) = {HALTED} — 신규·증액·매도 거부, 취소는 게이트가 막지 않는다(G13).
- 상품 유형: 크립토 현물 유형(02 ENT-02)은 아직 열거에 없다 → tests/support/m2_fakes와 같은 임시 처리(STOCK, 배수 1,
  수량 단위 = LOT_SIZE stepSize). 레버리지 없음(L10 제외).
- 세션(D-108): 매일 UTC 00:00~24:00 한 구간(market_time.session_247).
- 호가 단위: tickSize. 0이면 규칙 비활성(BN §6 확인) → tick_valid True, tick_of None(단위 없음).
- 시세(D-116): ticker/price는 응답에 시각이 없어 수신 시각(주입 시계)을 시세 시각으로 쓴다 — 게이트 G11이 신선도(60초)를
  판정한다. 가격제한폭이 없으므로 상·하한가·기준가는 None. 형식 오류면 그 값을 버린다(모름).
- 시장가 명목가 참조가(market_prices): 아는 값 전부(최근가·avgPrice·referencePrice). 많을수록 "확실한 위반" 판정이
  줄어 위험 축소를 미리 막지 않는 쪽이다(D-102·D-106).
- referencePrice 응답 형식 {"symbol","referencePrice"(문자열 또는 null),"timestamp"}은 원문 미대조 **[미확인]** —
  합성 응답 시험으로만 고정. null 또는 -2043(미설정, 호출자가 clear_reference_price)은 "참조가 없음"(정상).
- klines(1d): 품질 실패(market.KlineDataError)면 그 심볼 봉을 버리고 K10 발동(종목 범위), 성공이면 K10 재개 조건
  data_ok 보고(M1 marketdata.store와 같은 방식). 판정에는 확정 봉만(봉 시작 + 1일 ≤ 판정 시각, V-142).
  [제안] 최근가·평균가·참조가 형식 오류는 K10이 아니라 "그 값 모름"으로만 처리한다(일시 오류로 정지를 남기지 않음).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from trading_system.domain.enums import HaltType
from trading_system.domain.market import InstrumentInfo, InstrumentKind, Quote, SessionInfo, TradingStatus
from trading_system.domain.market_time import session_247
from trading_system.domain.sinks import HaltControl
from trading_system.domain.trading_rules import SymbolRules as DomainRules

from .market import (
    AvgPrice, Kline, KlineDataError, confirmed, parse_avg_price, parse_klines, parse_ticker_price,
)
from .numbers import BinanceParseError, parse_non_negative
from .rules import ExchangeInfoError, ExchangeRules, SymbolRules, TradingState, parse_exchange_info
from .weights import RateLimits

_HALTED = frozenset({TradingStatus.HALTED})


def to_domain_rules(r: SymbolRules) -> DomainRules:
    """바이낸스 심볼 규칙 → 공통 규칙 값 객체 (D-106). 값은 그대로(반올림 없음)."""
    m = r.market_lot_size or r.lot_size
    if r.notional is not None:
        n = r.notional
        min_n, max_n, a_min, a_max = n.min_notional, n.max_notional, n.apply_min_to_market, n.apply_max_to_market
    elif r.min_notional is not None:
        min_n, max_n, a_min, a_max = r.min_notional.min_notional, None, r.min_notional.apply_to_market, False
    else:
        min_n, max_n, a_min, a_max = Decimal("0"), None, False, False
    p = r.price_filter
    return DomainRules(instrument=r.symbol, tick_size=p.tick_size, min_price=p.min_price, max_price=p.max_price,
                       lot_min_qty=r.lot_size.min_qty, lot_max_qty=r.lot_size.max_qty, lot_step=r.lot_size.step_size,
                       market_min_qty=m.min_qty, market_max_qty=m.max_qty, market_step=m.step_size,
                       min_notional=min_n, max_notional=max_n, apply_min_to_market=a_min, apply_max_to_market=a_max)


def _unit_problem(r: SymbolRules) -> str | None:
    if r.lot_size.min_qty % r.lot_size.step_size != 0:
        return "LOT_SIZE minQty가 stepSize의 배수가 아님"
    t = r.price_filter.tick_size
    if t > 0 and r.price_filter.min_price % t != 0:
        return "PRICE_FILTER minPrice가 tickSize의 배수가 아님"
    return None


def _status(state: TradingState) -> frozenset[TradingStatus]:
    return frozenset() if state is TradingState.OPEN else _HALTED


@dataclass
class BinanceMarketFacts:
    now: Callable[[], datetime]
    universe: Sequence[str]
    halts: HaltControl | None = None
    account: str | None = None
    load_error: str | None = None
    rejected: dict[str, str] = field(default_factory=dict)
    _exchange: ExchangeRules | None = None
    _rules: dict[str, DomainRules] = field(default_factory=dict)
    _last_known: dict[str, SymbolRules] = field(default_factory=dict)   # 적재 실패 뒤에도 남는 종목 정보·호가 단위
    _quotes: dict[str, Quote] = field(default_factory=dict)
    _avg: dict[str, AvgPrice] = field(default_factory=dict)
    _ref: dict[str, Decimal] = field(default_factory=dict)
    _bars: dict[str, list[Kline]] = field(default_factory=dict)

    # --- 적재 (공급자 입력) -------------------------------------------------------------------
    def load_exchange_info(self, body: object) -> bool:
        """exchangeInfo 응답 적재. 전체 실패면 False와 함께 직전 규칙을 버린다(위험 확대 거부)."""
        try:
            ex = parse_exchange_info(body)
        except (ExchangeInfoError, BinanceParseError, AttributeError, TypeError) as exc:
            self._exchange, self._rules = None, {}
            self.load_error = f"exchangeInfo 적재 실패: {exc}"
            return False
        rules: dict[str, DomainRules] = {}
        rejected = {s: why for s, why in ex.rejected.items() if s in self.universe}
        for sym in self.universe:
            r = ex.rules(sym)
            if r is None:
                continue
            problem = _unit_problem(r)
            if problem:
                rejected[sym] = problem
                continue
            try:
                rules[sym] = to_domain_rules(r)
            except (TypeError, ValueError) as exc:
                rejected[sym] = f"규칙 값 오류: {exc}"
                continue
            self._last_known[sym] = r
        self._exchange, self._rules, self.rejected, self.load_error = ex, rules, rejected, None
        return True

    def rate_limits(self) -> RateLimits | None:
        return self._exchange.rate_limits if self._exchange is not None else None

    def update_ticker(self, symbol: str, body: object) -> bool:
        if symbol not in self.universe:
            return False
        try:
            q = parse_ticker_price(body, symbol=symbol, received_at=self.now())
        except KlineDataError:
            self._quotes.pop(symbol, None)
            return False
        self._quotes[symbol] = Quote(symbol, q.price, q.as_of)
        return True

    def update_avg_price(self, symbol: str, body: object) -> bool:
        if symbol not in self.universe:
            return False
        try:
            self._avg[symbol] = parse_avg_price(body, symbol=symbol)
        except KlineDataError:
            self._avg.pop(symbol, None)
            return False
        return True

    def update_reference_price(self, symbol: str, body: object) -> bool:
        """[미확인 형식] {"symbol","referencePrice","timestamp"}. referencePrice null = 참조가 없음(정상)."""
        if symbol not in self.universe:
            return False
        self._ref.pop(symbol, None)
        if not isinstance(body, dict) or body.get("symbol") != symbol or "referencePrice" not in body:
            return False
        raw = body["referencePrice"]
        if raw is None:
            return True
        try:
            price = parse_non_negative(raw, "referencePrice")
        except BinanceParseError:
            return False
        if price <= 0:
            return False
        self._ref[symbol] = price
        return True

    def clear_reference_price(self, symbol: str) -> None:
        """참조가 미설정(-2043) 수신 시 호출자가 부른다."""
        self._ref.pop(symbol, None)

    def update_klines(self, symbol: str, rows: object, interval: str = "1d") -> bool:
        if symbol not in self.universe:
            return False
        try:
            bars = parse_klines(rows, symbol=symbol, interval=interval)
        except KlineDataError as exc:
            self._bars.pop(symbol, None)
            if self.halts is not None:
                self.halts.activate(HaltType.K10, "marketdata", f"데이터 품질 실패 {symbol}: {exc}", self.account,
                                    instrument=symbol)
            return False
        self._bars[symbol] = bars
        if self.halts is not None:
            self.halts.report_condition(HaltType.K10, self.account, "data_ok", True, f"klines {interval} {len(bars)}",
                                        instrument=symbol)
        return True

    def daily_bars(self, symbol: str) -> list[Kline]:
        """판정 시각(주입 시계)에 확정된 봉만 (D-108, V-142)."""
        return confirmed(self._bars.get(symbol, []), self.now())

    # --- gate.ports.MarketFacts ---------------------------------------------------------------
    def quote(self, instrument: str) -> Quote | None:
        return self._quotes.get(instrument) if instrument in self.universe else None

    def instrument(self, instrument: str) -> InstrumentInfo | None:
        r = self._last_known.get(instrument) if instrument in self.universe else None
        if r is None:
            return None
        return InstrumentInfo(instrument, InstrumentKind.STOCK, Decimal("1"), r.lot_size.step_size)

    def trading_status(self, instrument: str) -> frozenset[TradingStatus] | None:
        if instrument not in self._rules or self._exchange is None:
            return None
        r = self._exchange.rules(instrument)
        return None if r is None else _status(r.state)

    def session(self, trade_date: date) -> SessionInfo | None:
        return session_247(trade_date)

    def tick_valid(self, instrument: str, price: Decimal) -> bool | None:
        r = self._last_known.get(instrument) if instrument in self.universe else None
        if r is None:
            return None
        t = r.price_filter.tick_size
        return True if t == 0 else price % t == 0

    def tick_of(self, instrument: str, price: Decimal) -> Decimal | None:
        r = self._last_known.get(instrument) if instrument in self.universe else None
        if r is None or r.price_filter.tick_size == 0:
            return None
        return r.price_filter.tick_size

    # --- gate.ports.SymbolRulesSource ---------------------------------------------------------
    def rules(self, instrument: str) -> DomainRules | None:
        return self._rules.get(instrument)

    def market_prices(self, instrument: str) -> tuple[Decimal, ...]:
        out: list[Decimal] = []
        if (q := self._quotes.get(instrument)) is not None:
            out.append(q.price)
        if (a := self._avg.get(instrument)) is not None:
            out.append(a.price)
        if (r := self._ref.get(instrument)) is not None:
            out.append(r)
        return tuple(out)
