"""바이낸스 Spot 테스트넷 시작 점검 — 조회만, 주문 API 전송 전 차단 (T-M2-14, 승인 A-096, CP-024 D-097·D-098·D-106·D-113·D-115).

이 모듈은 바이낸스 쪽 부품만 갖는다(D-119): HTTP 경계 가드, 조회 순서, 응답 해석·분류. 연결 조건 검사(모드·설정·시작 일치
표·접속 스위치·비밀 키)·시계(SNTP)·시작 대조·결과 파일은 app/m2_preflight가 하고, 실제 전송(Transport)은 그쪽이 주입한다
(이 패키지는 네트워크 라이브러리를 import하지 않는다).

가드(PreflightGuard) — 전송 직전 마지막 경계
- 접속 대상: https 테스트넷 REST 호스트 TESTNET_HOST (포트·사용자 정보 없음)만. 실전·Demo·대체 테스트넷(api1.)·루프백·평문은 거부.
- 허용 목록: GET 조회 7개(QUERY_PATHS). 그 밖의 모든 요청(GET 외 메서드, 주문 신규·취소·정정·order/test·OCO·orderList·
  cancelReplace·amend·SOR·미체결 일괄 취소·사용자 스트림, 이번 범위 밖 조회 GET /api/v3/order 포함)은 **보내기 전에**
  PreSendError — 클라이언트는 이를 '전송 전 실패'로 본다(결과 불명 아님). 차단 사실은 blocked에 남는다.
- 기록: 경로별 HTTP 상태·오류 코드·조회 분류만. 쿼리 문자열(서명)·헤더(API 키)·응답 원문은 남기지 않는다.

조회 순서 (CP-024 §2.2 정상 흐름 1)
  서버 시각(time) → ping → exchangeInfo(심볼별) → [호출 한도 적재] → 계좌 → 미체결 → 최근 24시간 주문(allOrders)·체결(myTrades)
- 처음 세 호출은 부트스트랩 예산(BOOTSTRAP_RATE_LIMITS [제안] 1200/분의 50%)으로 보낸다 — 실제 한도는 exchangeInfo에서 읽는다.
  rateLimits를 못 읽으면 서명 조회를 하지 않는다(D-113 "못 읽으면 시작 거부").
- 418(IP 차단)·429(Retry-After 대기 최대 3회 뒤에도 제한) → 나머지 호출 전부 중단(D-113). 인증(-1022·-2015)·시각(-1021)
  오류 → 재시도 없음, 나머지 서명 조회 건너뜀(같은 오류가 반복될 뿐).
- 서버 시각 오차의 경고 판정은 설정 값(server_time_warn_ms)으로 호출자가 한다 — 여기서는 측정값만 돌려준다.
- 결과는 JSON으로 옮길 수 있는 dict(문자열·정수·참거짓). 금액·수량은 지수 표기 없는 문자열. 계좌 식별값 없음.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .classify import QueryKind, classify_query_reply
from .client import (
    TESTNET_REST, AfterSendError, BinanceQueryError, BinanceSpotClient, EndpointError, PreSendError, RawReply,
    Transport, _json,
)
from .facts import BinanceMarketFacts
from .numbers import plain
from .orders import MAX_WINDOW_MS
from .readonly import BinanceReadOnlyQueries
from .rules import ExchangeInfoError, ExchangeRules, SymbolRules, TradingState, parse_exchange_info
from .signing import Credentials
from .signing import SecretLike as _SecretLike
from .weights import BUDGET_FRACTION_PERCENT, RateLimitConfigError, RateLimits, WeightBudget, parse_rate_limits

TESTNET_HOST = "testnet.binance.vision"
QUERY_PATHS = frozenset({"/api/v3/time", "/api/v3/ping", "/api/v3/exchangeInfo", "/api/v3/account",
                         "/api/v3/openOrders", "/api/v3/allOrders", "/api/v3/myTrades"})
SIGNED_PATHS = frozenset({"/api/v3/account", "/api/v3/openOrders", "/api/v3/allOrders", "/api/v3/myTrades"})
_ORDER_PREFIXES = ("/api/v3/order", "/api/v3/openOrder", "/api/v3/sor/", "/api/v3/cancelReplace")
# [제안] exchangeInfo 전 호출(time·ping·exchangeInfo = weight 22)용 보수 한도. 문서 예시(6000/분)보다 작게 잡는다.
BOOTSTRAP_RATE_LIMITS = [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 1200}]

Alert = tuple[str, str]
MakeTransport = Callable[[type[Exception], type[Exception]], Transport]


def is_order_family(method: str, path: str) -> bool:
    """주문 변경 계열(또는 주문 단건·목록 조회 경로)인지 — 보고용 분류. 차단 여부는 허용 목록이 정한다."""
    if path.startswith(_ORDER_PREFIXES) and path not in QUERY_PATHS:
        return True     # order·order/test·order/oco·order/cancelReplace·order/amend·orderList·openOrderList·sor
    return path == "/api/v3/openOrders" and method.upper() != "GET"   # 미체결 일괄 취소


@dataclass(frozen=True)
class CallRecord:
    method: str
    path: str
    status: int | None       # 응답이 없으면 None (전송 오류)
    code: int | None         # 바이낸스 오류 코드
    kind: str                # QueryKind 값 또는 TRANSPORT


class PreflightGuard:
    """Transport를 감싸 허용 목록 밖 요청을 전송 전에 막는다."""

    def __init__(self, inner: Transport) -> None:
        self._inner = inner
        self.records: list[CallRecord] = []
        self.blocked: list[str] = []

    def __repr__(self) -> str:
        return f"PreflightGuard(sent={len(self.records)}, blocked={len(self.blocked)})"

    @property
    def order_sent(self) -> int:
        """실제로 보낸 주문 계열 요청 수 — 허용 목록상 항상 0이어야 한다(보고서 '주문 API 전송')."""
        return sum(1 for r in self.records if r.path not in QUERY_PATHS or r.method != "GET")

    def send(self, method: str, url: str, headers: Mapping[str, str]) -> RawReply:
        p = urlsplit(url)
        if p.scheme != "https" or p.hostname != TESTNET_HOST or p.port is not None or p.username or p.password:
            self.blocked.append(f"{method} {p.hostname or '?'}{p.path}")
            raise PreSendError("허용되지 않은 접속 대상(테스트넷 REST만) — 보내지 않는다")
        if method != "GET" or p.path not in QUERY_PATHS:
            self.blocked.append(f"{method} {p.path}")
            raise PreSendError(f"{method} {p.path}: 시작 점검 허용 목록 밖(주문 계열 포함) — 보내지 않는다")
        try:
            r = self._inner.send(method, url, headers)
        except (PreSendError, AfterSendError):
            self.records.append(CallRecord(method, p.path, None, None, "TRANSPORT"))
            raise
        reply = RawReply(int(r.status), dict(r.headers), str(r.text))
        v = classify_query_reply(reply.status, _json(reply.text))
        self.records.append(CallRecord(method, p.path, reply.status, v.code, v.kind.value))
        return reply


# --- 오류 설명 ---------------------------------------------------------------------------------------------------

def _explain(kind: QueryKind, code: int | None) -> str:
    if kind is QueryKind.AUTH:
        if code == -1022:
            return "서명 거부(-1022) — 시크릿 값 확인(재시도 없음)"
        return "API 키·IP·권한 거부(-2015) — 키 값, 테스트넷 키 권한, IP 제한 확인(재시도 없음)"
    if kind is QueryKind.CLOCK:
        return "시각 검증 거부(-1021, recvWindow 5000ms) — PC 시계 동기화 확인(재시도 없음, D-115)"
    if kind is QueryKind.BANNED:
        return "HTTP 418 IP 차단 — 모든 호출 중단, 자동 재개 없음(사람 재개, D-113). 차단 해제 전 다시 실행하지 않는다"
    if kind is QueryKind.RATE_LIMITED:
        return "호출 제한(429·-1003) — Retry-After 대기 후 최대 3회 뒤에도 제한, 나머지 호출 중단(D-113)"
    return "조회 실패(5XX·응답 해석 불가·그 밖의 오류)"


_STOP_ALL = (QueryKind.BANNED, QueryKind.RATE_LIMITED)
_STOP_SIGNED = (QueryKind.AUTH, QueryKind.CLOCK)


def _filters(r: SymbolRules) -> dict[str, dict[str, str | bool | int] | None]:
    pf, lot, m = r.price_filter, r.lot_size, r.market_lot_size
    return {
        "PRICE_FILTER": {"minPrice": plain(pf.min_price), "maxPrice": plain(pf.max_price),
                         "tickSize": plain(pf.tick_size)},
        "LOT_SIZE": {"minQty": plain(lot.min_qty), "maxQty": plain(lot.max_qty), "stepSize": plain(lot.step_size)},
        "MARKET_LOT_SIZE": None if m is None else {"minQty": plain(m.min_qty), "maxQty": plain(m.max_qty),
                                                   "stepSize": plain(m.step_size)},
        "NOTIONAL": None if r.notional is None else {
            "minNotional": plain(r.notional.min_notional), "applyMinToMarket": r.notional.apply_min_to_market,
            "maxNotional": plain(r.notional.max_notional), "applyMaxToMarket": r.notional.apply_max_to_market,
            "avgPriceMins": r.notional.avg_price_mins},
        "MIN_NOTIONAL": None if r.min_notional is None else {
            "minNotional": plain(r.min_notional.min_notional), "applyToMarket": r.min_notional.apply_to_market,
            "avgPriceMins": r.min_notional.avg_price_mins},
    }


def _rate_rows(raw: object) -> list[dict[str, str | int]]:
    rows: list[dict[str, str | int]] = []
    unit = {"SECOND": 1_000, "MINUTE": 60_000, "HOUR": 3_600_000, "DAY": 86_400_000}
    for item in raw if isinstance(raw, list) else []:
        kind, interval, num, limit = (item.get(k) for k in ("rateLimitType", "interval", "intervalNum", "limit"))
        ms = unit[str(interval)] * int(num)  # type: ignore[arg-type]  # parse_rate_limits가 이미 검증
        row: dict[str, str | int] = {"type": str(kind), "interval_ms": ms, "limit": int(limit)}  # type: ignore[arg-type]
        if kind in ("REQUEST_WEIGHT", "ORDERS"):
            row["budget"] = int(limit) * BUDGET_FRACTION_PERCENT // 100  # type: ignore[arg-type]
        rows.append(row)
    return rows


@dataclass
class _Run:
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stop_all: bool = False
    stop_signed: bool = False


class TestnetSession:
    """시작 점검 한 번. 연결 조건 검사를 통과한 뒤에만 만든다(app/m2_preflight)."""

    def __init__(self, base_url: str, api_key: _SecretLike, secret: _SecretLike, make_transport: MakeTransport, *,
                 now_ms: Callable[[], int], sleep: Callable[[float], None]) -> None:
        if base_url.rstrip("/") != TESTNET_REST:
            raise EndpointError("시작 점검 접속 대상은 테스트넷 REST만")
        self._base = TESTNET_REST
        self._creds = Credentials(api_key, secret)
        self._now = now_ms
        self._sleep = sleep
        self._alerts: list[Alert] = []
        self.guard = PreflightGuard(make_transport(PreSendError, AfterSendError))
        self._client = self._new_client(parse_rate_limits(BOOTSTRAP_RATE_LIMITS))

    def __repr__(self) -> str:
        return f"TestnetSession(base={self._base}, {self.guard!r})"

    def _new_client(self, limits: RateLimits) -> BinanceSpotClient:
        return BinanceSpotClient(self._base, self._creds, self.guard, WeightBudget(limits, now_ms=self._now),
                                 now_ms=self._now, sleep=self._sleep, on_alert=self._on_alert)

    def _on_alert(self, kind: str, message: str) -> None:
        if kind == "CLOCK" and message.startswith("바이낸스 서버 시각 오차"):
            return   # 서버 시각 경고는 설정 값으로 호출자가 판정 (중복 방지)
        self._alerts.append((kind, message))

    # --- 조회 단계 ---
    def _step(self, run: _Run, name: str, path: str, fn: Callable[[], object], *, signed: bool) -> object | None:
        if run.stop_all or (signed and run.stop_signed):
            return None
        try:
            return fn()
        except BinanceQueryError as exc:
            code = next((r.code for r in reversed(self.guard.records) if r.path == path), None)
            run.failures.append(f"{name} 실패: {exc} — {_explain(exc.kind, code)}")
            if exc.kind in _STOP_ALL:
                run.stop_all = True
                run.failures.append("나머지 호출 중단(D-113)")
            elif exc.kind in _STOP_SIGNED:
                run.stop_signed = True
            return None

    def run(self, symbols: Sequence[str], quote_asset: str) -> dict:
        run = _Run()
        res: dict = {"server_time_offset_ms": None, "exchange": {}, "rate_limits": [], "account": None,
                     "open_orders": {}, "recent_orders": {}, "recent_fills": {}, "exchange_ok": False,
                     "signed_ok": False}
        res["server_time_offset_ms"] = self._step(run, "서버 시각 /api/v3/time", "/api/v3/time",
                                                  self._client.server_time_offset_ms, signed=False)
        self._step(run, "ping /api/v3/ping", "/api/v3/ping", self._client.ping, signed=False)

        limits: RateLimits | None = None
        all_ok = True
        for sym in symbols:
            body = self._step(run, f"exchangeInfo {sym}", "/api/v3/exchangeInfo",
                              lambda s=sym: self._client.exchange_info(s), signed=False)
            info, sym_limits = self._exchange(run, sym, body, symbols, quote_asset, res)
            all_ok = all_ok and info
            if sym_limits is not None and limits is None:
                limits = sym_limits
                res["rate_limits"] = _rate_rows(body.get("rateLimits") if isinstance(body, dict) else None)
        res["exchange_ok"] = all_ok and bool(symbols)
        if limits is None:
            if not run.stop_all:
                run.failures.append("호출 한도(rateLimits) 적재 실패 — 서명 조회 건너뜀(D-113 못 읽으면 시작 거부)")
            run.stop_signed = True
        else:
            self._client = self._new_client(limits)

        acct = self._step(run, "계좌 /api/v3/account", "/api/v3/account", self._client.account, signed=True)
        if acct is not None:
            res["account"] = {"can_trade": acct.can_trade, "account_type": acct.account_type,  # type: ignore[attr-defined]
                              "permissions": list(acct.permissions),  # type: ignore[attr-defined]
                              "balances": [{"asset": b.asset, "free": plain(b.free), "locked": plain(b.locked)}
                                           for b in acct.balances]}  # type: ignore[attr-defined]
            if not acct.can_trade:  # type: ignore[attr-defined]
                run.warnings.append("계좌 canTrade = false — 주문 불가 상태(테스트넷 키 권한 확인)")
        end = self._now()
        start = end - MAX_WINDOW_MS
        signed_ok = acct is not None
        for sym in symbols:
            oo = self._step(run, f"미체결 {sym}", "/api/v3/openOrders", lambda s=sym: self._client.open_orders(s),
                            signed=True)
            ao = self._step(run, f"최근 주문 {sym}", "/api/v3/allOrders",
                            lambda s=sym: self._client.all_orders(s, start_ms=start, end_ms=end), signed=True)
            mt = self._step(run, f"최근 체결 {sym}", "/api/v3/myTrades",
                            lambda s=sym: self._client.my_trades(s, start_ms=start, end_ms=end), signed=True)
            for key, val in (("open_orders", oo), ("recent_orders", ao), ("recent_fills", mt)):
                res[key][sym] = None if val is None else len(val)  # type: ignore[arg-type]
            signed_ok = signed_ok and None not in (oo, ao, mt)
        res["signed_ok"] = signed_ok
        res["failures"], res["warnings"] = run.failures, run.warnings
        res["alerts"] = [f"{k}: {m}" for k, m in self._alerts]
        return res

    def _exchange(self, run: _Run, sym: str, body: object, universe: Sequence[str], quote_asset: str,
                  res: dict) -> tuple[bool, RateLimits | None]:
        if body is None:
            res["exchange"][sym] = {"found": None}
            return False, None
        try:
            ex: ExchangeRules = parse_exchange_info(body)
        except (ExchangeInfoError, RateLimitConfigError) as exc:
            run.failures.append(f"exchangeInfo {sym} 해석 실패: {exc}")
            res["exchange"][sym] = {"found": None}
            return False, None
        r = ex.rules(sym)
        if r is None:
            why = ex.rejected.get(sym)
            res["exchange"][sym] = {"found": why is not None, "rules_ok": False, "rejected": why}
            run.failures.append(f"{sym}: 거래 규칙 해석 실패 — {why}" if why else
                                f"{sym}: exchangeInfo에 심볼 없음 (PV-M2-03, D-105 유니버스 재확인)")
            return False, ex.rate_limits
        facts = BinanceMarketFacts(now=lambda: datetime.fromtimestamp(self._now() / 1000, UTC), universe=[sym])
        facts.load_exchange_info(body)
        domain = facts.rules(sym)
        trading = r.state is TradingState.OPEN
        info = {"found": True, "status": r.status, "trading": trading, "base_asset": r.base_asset, "quote_asset": r.quote_asset,
                "quote_asset_precision": r.quote_asset_precision, "order_types": sorted(r.order_types),
                "filters": _filters(r), "unhandled_filters": list(r.unhandled_filters),
                "rules_ok": domain is not None, "rejected": facts.rejected.get(sym),
                "min_notional": None if domain is None else plain(domain.min_notional)}
        res["exchange"][sym] = info
        ok = True
        if not trading:
            ok = False
            reason = "isSpotTradingAllowed = false" if r.status == "TRADING" else f"상태 {r.status}"
            run.failures.append(f"{sym}: {reason} — TRADING 아님(신규·증액 불가, D-107)")
        if domain is None:
            ok = False
            run.failures.append(f"{sym}: 거래 규칙 해석 실패 — {facts.rejected.get(sym) or facts.load_error}")
        if r.quote_asset != quote_asset:
            ok = False
            run.failures.append(f"{sym}: 호가 자산 {r.quote_asset} ≠ 설정 {quote_asset}")
        if r.notional is None and r.min_notional is None:
            run.warnings.append(f"{sym}: NOTIONAL·MIN_NOTIONAL 필터 없음 — 최소 주문 금액 0으로 봄(D-106)")
        return ok, ex.rate_limits

    # --- 대조·보고 ---
    def queries(self, symbols: Sequence[str]) -> BinanceReadOnlyQueries:
        """시작 대조용 조회 전용 어댑터(같은 가드·예산). 주문 변경 메서드 없음."""
        return BinanceReadOnlyQueries(self._client, symbols, self._now)

    def calls(self) -> list[dict[str, str | int | None]]:
        """경로별 호출 요약(보낸 것만): 호출 수, 마지막 상태·코드·분류."""
        out: dict[tuple[str, str], dict[str, str | int | None]] = {}
        for r in self.guard.records:
            row = out.setdefault((r.method, r.path), {"method": r.method, "path": r.path, "calls": 0, "ok": 0})
            row["calls"] = int(row["calls"] or 0) + 1
            row["ok"] = int(row["ok"] or 0) + (1 if r.kind == QueryKind.OK.value else 0)
            row.update(last_status=r.status, last_code=r.code, last_kind=r.kind)
        return list(out.values())


def open_testnet_session(base_url: str, api_key: _SecretLike, secret: _SecretLike, make_transport: MakeTransport,
                         *, now_ms: Callable[[], int], sleep: Callable[[float], None]) -> TestnetSession:
    """app/m2_preflight가 연결 조건 검사 뒤 부른다. 접속 대상이 테스트넷 REST가 아니면 전송 객체를 만들기 전에 거부."""
    return TestnetSession(base_url, api_key, secret, make_transport, now_ms=now_ms, sleep=sleep)
