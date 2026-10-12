"""바이낸스 Spot **테스트넷** REST 클라이언트 (CP-024 D-098·D-110~D-115, BN §2~§7).

경계 규칙
- 접속 대상은 테스트넷 REST(`TESTNET_REST`) 또는 시험용 루프백만. 실전·Demo 도메인 상수는 없다(D-098, 승인 구조 3·4 전).
- 전송은 주입한다(Transport). 이 패키지에는 실제 HTTP 구현이 없다 — 실제 전송은 broker/rest_transport(T-M2-14, 테스트넷
  연결 승인 A-096)를 app이 주입하고, 시작 점검에서는 broker/binance/preflight.PreflightGuard가 허용 목록으로 감싼다.
  Transport는 연결 수립 전 실패면 PreSendError, 그 밖의 전송 중 오류(타임아웃·끊김)면 AfterSendError를 올린다.
- 주문 변경(신규·취소)은 **한 번만** 보낸다. 결과는 4분류(classify.classify_order_reply). 429·418·인증 오류도 재전송 없음.
  · 전송 전 실패: 요청 생성 실패, 예산 보류(418 차단·429 대기·가중치/주문 수 부족), 연결 수립 실패
  · 결과 불명: 전송 후 오류, 응답 해석 불가, 그 밖은 매핑 표(D-112)
- 조회: 429는 `Retry-After` 대기 후 최대 3회(D-113), 전송 오류·5XX·해석 불가는 1·2초 대기 후 최대 3회(03 §5.3 조회 재시도),
  인증·시각 오류는 재시도 없음. 실패하면 BinanceQueryError(kind) — 호출자가 K3·K5 판단. -2013·-2026은 결과(NOT_FOUND·ARCHIVED).
- 418 수신: 즉시 예산을 차단 상태로 바꾸고 K5 경보. 이후 모든 호출은 보내지 않는다(사람 재개).
- -1021: 연속 구간 시작에 시계 경고(CLOCK), 서명 요청(주문·조회) 응답 연속 3회면 K6 판정 요청 경보(D-115). 정지 연결은
  halt/broker_alerts.BrokerAlertHalts(on_alert 자리). 바이낸스 서버 시각 오차 0.5초 이상: 시계 경고 [결정 A-064].
- 로그·오류·경보 문구에 쿼리 문자열(서명 포함)·API 키·응답 원문을 넣지 않는다. 경로·HTTP 상태·코드만.
- 엔드포인트 weight(BN §3~§8, 2026-10-04·05 확인): 주문 POST/DELETE 성공 0·실패 1(예산은 1로 잡음), 주문 조회 4,
  myTrades 20, allOrders 20, 서버 시각 1. [추정] 실패 주문의 weight 1은 문서 weight 표기값 기준.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol
from urllib.parse import urlsplit

from trading_system.domain.enums import SubmitOutcome

from .account import AccountInfo, parse_account
from .classify import Flag, QueryKind, classify_order_reply, classify_query_reply
from .numbers import BinanceParseError
from .orders import (
    NewOrder, OrderBuildError, Params, cancel_params, new_order_params, query_order_params, symbol_params,
    window_params,
)
from .signing import DEFAULT_RECV_WINDOW_MS, Credentials, encode, signed_query
from .stream import Fill, OrderState, parse_my_trades, parse_order_list, parse_order_response
from .weights import CallPriority, WeightBudget

TESTNET_REST = "https://testnet.binance.vision"
ORDER_PATH = "/api/v3/order"
QUERY_ATTEMPTS = 3
QUERY_BACKOFF_S = (1.0, 2.0)
CLOCK_STREAK_ALERT = 3
CLOCK_WARN_MS = 500
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
WEIGHT = {"order": 1, "query_order": 4, "my_trades": 20, "all_orders": 20, "time": 1,
          # T-M2-14 조회 (BN §3·§6·§7): ping 1, exchangeInfo 20, 계좌 20, 미체결(심볼 지정) 6
          "ping": 1, "exchange_info": 20, "account": 20, "open_orders": 6}

Alert = Callable[[str, str], None]


class EndpointError(ValueError):
    pass


class PreSendError(RuntimeError):
    """연결 수립 전 실패. 요청 바이트가 나가지 않았다."""


class AfterSendError(RuntimeError):
    """전송 중 오류(읽기 타임아웃·끊김 등). 서버가 받았는지 모른다."""


@dataclass(frozen=True)
class RawReply:
    status: int
    headers: dict[str, str]
    text: str


class Transport(Protocol):
    def send(self, method: str, url: str, headers: Mapping[str, str]) -> RawReply: ...


class BinanceQueryError(RuntimeError):
    def __init__(self, message: str, kind: QueryKind) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class OrderSubmit:
    outcome: SubmitOutcome
    reason: str
    flag: Flag = Flag.NONE
    code: int | None = None
    order_id: int | None = None
    ack: OrderState | None = None


@dataclass(frozen=True)
class OrderQuery:
    kind: QueryKind                # OK / NOT_FOUND / ARCHIVED
    order: OrderState | None = None


def check_endpoint(url: str) -> str:
    """테스트넷 REST(https) 또는 시험용 루프백만 허용 (모드 값만 바꿔 실전에 닿지 않게, CLAUDE.md §7)."""
    base = url.rstrip("/")
    if base == TESTNET_REST:
        return base
    p = urlsplit(base)
    if p.hostname in _LOOPBACK and p.scheme in ("http", "https") and p.path in ("", "/"):
        return base
    raise EndpointError("허용되지 않은 접속 대상(테스트넷·루프백만)")


@dataclass
class _ClockWatch:
    streak: int = 0
    alerted: bool = field(default=False)


class BinanceSpotClient:
    def __init__(self, base_url: str, credentials: Credentials, transport: Transport, budget: WeightBudget, *,
                 now_ms: Callable[[], int], sleep: Callable[[float], None] = time.sleep, on_alert: Alert | None = None,
                 recv_window_ms: int = DEFAULT_RECV_WINDOW_MS) -> None:
        self._base = check_endpoint(base_url)
        self._creds = credentials
        self._transport = transport
        self._budget = budget
        self._now = now_ms
        self._sleep = sleep
        self._alert = on_alert or (lambda kind, msg: None)
        self._recv_window = recv_window_ms
        self._clock = _ClockWatch()

    def __repr__(self) -> str:
        return f"BinanceSpotClient(base={self._base})"

    # --- 주문 변경 (한 번만) ---
    def place(self, order: NewOrder, priority: CallPriority, *, qty_step: Decimal, tick: Decimal | None) -> OrderSubmit:
        try:
            params = new_order_params(order, qty_step=qty_step, tick=tick)
        except OrderBuildError as exc:
            return OrderSubmit(SubmitOutcome.PRE_SEND_FAILURE, f"요청 생성 실패: {exc}")
        return self._change("POST", params, priority, cancel=False)

    def cancel(self, symbol: str, priority: CallPriority, *, order_id: int | None = None,
               client_order_id: str | None = None) -> OrderSubmit:
        try:
            params = cancel_params(symbol, order_id=order_id, client_order_id=client_order_id)
        except OrderBuildError as exc:
            return OrderSubmit(SubmitOutcome.PRE_SEND_FAILURE, f"요청 생성 실패: {exc}")
        return self._change("DELETE", params, priority, cancel=True)

    def _change(self, method: str, params: Params, priority: CallPriority, *, cancel: bool) -> OrderSubmit:
        adm = self._budget.admit_order(priority)
        if not adm.ok:
            return OrderSubmit(SubmitOutcome.PRE_SEND_FAILURE, f"호출 보류: {adm.reason}")
        try:
            reply = self._send(method, ORDER_PATH, params)
        except PreSendError:
            return OrderSubmit(SubmitOutcome.PRE_SEND_FAILURE, f"{method} {ORDER_PATH}: 연결 수립 실패")
        except AfterSendError:
            return OrderSubmit(SubmitOutcome.UNKNOWN, f"{method} {ORDER_PATH}: 전송 후 오류 — 결과 불명")
        body = _json(reply.text)
        v = classify_order_reply(reply.status, body, cancel=cancel)
        self._after_reply(reply, v.flag)
        self._watch_clock(v.flag is Flag.CLOCK)
        ack = None
        if v.outcome is SubmitOutcome.ACCEPTED:
            try:
                ack = parse_order_response(body)
            except BinanceParseError:
                # 접수 근거(orderId)는 있으나 나머지를 못 읽음 → 접수로 두고 체결은 조회·스트림으로 확정
                ack = None
        return OrderSubmit(v.outcome, f"{method} {ORDER_PATH}: {v.reason}", v.flag, v.code, v.order_id, ack)

    # --- 조회 ---
    def query_order(self, symbol: str, *, order_id: int | None = None, client_order_id: str | None = None,
                    priority: CallPriority = CallPriority.RESOLVE) -> OrderQuery:
        try:
            params = query_order_params(symbol, order_id=order_id, client_order_id=client_order_id)
        except OrderBuildError as exc:
            raise BinanceQueryError(f"주문 조회 요청 생성 실패: {exc}", QueryKind.FAILED) from None
        kind, body = self._query(ORDER_PATH, params, WEIGHT["query_order"], priority,
                                 results=(QueryKind.NOT_FOUND, QueryKind.ARCHIVED))
        if kind is not QueryKind.OK:
            return OrderQuery(kind)
        return OrderQuery(kind, self._parse(parse_order_response, body, ORDER_PATH))

    def my_trades(self, symbol: str, *, start_ms: int, end_ms: int,
                  priority: CallPriority = CallPriority.RESOLVE) -> list[Fill]:
        path = "/api/v3/myTrades"
        params = self._window(symbol, start_ms, end_ms)
        _, body = self._query(path, params, WEIGHT["my_trades"], priority)
        return self._parse(parse_my_trades, body, path)

    def all_orders(self, symbol: str, *, start_ms: int, end_ms: int,
                   priority: CallPriority = CallPriority.RESOLVE) -> list[OrderState]:
        path = "/api/v3/allOrders"
        params = self._window(symbol, start_ms, end_ms)
        _, body = self._query(path, params, WEIGHT["all_orders"], priority)
        return self._parse(parse_order_list, body, path)

    def ping(self) -> None:
        """연결 확인 `GET /api/v3/ping` (서명 없음, T-M2-14)."""
        self._query("/api/v3/ping", [], WEIGHT["ping"], CallPriority.RECONCILE, signed=False)

    def exchange_info(self, symbol: str) -> object:
        """`GET /api/v3/exchangeInfo?symbol=` 응답 본문(서명 없음). 해석은 rules.parse_exchange_info·facts가 한다."""
        path = "/api/v3/exchangeInfo"
        _, body = self._query(path, self._symbol(symbol), WEIGHT["exchange_info"], CallPriority.RECONCILE,
                              signed=False)
        return body

    def account(self) -> AccountInfo:
        """`GET /api/v3/account?omitZeroBalances=true` (서명). 계좌 식별값은 읽지 않는다(account.parse_account)."""
        path = "/api/v3/account"
        _, body = self._query(path, [("omitZeroBalances", "true")], WEIGHT["account"], CallPriority.RECONCILE)
        return self._parse(parse_account, body, path)

    def open_orders(self, symbol: str, *, priority: CallPriority = CallPriority.RECONCILE) -> list[OrderState]:
        """`GET /api/v3/openOrders?symbol=` (서명, 심볼 지정 weight 6)."""
        path = "/api/v3/openOrders"
        _, body = self._query(path, self._symbol(symbol), WEIGHT["open_orders"], priority)
        return self._parse(parse_order_list, body, path)

    def server_time_offset_ms(self) -> int:
        """바이낸스 서버 시각 − 로컬 시각(요청 전후 중간값 기준). 0.5초 이상이면 시계 경고 (D-115)."""
        path = "/api/v3/time"
        sent = self._now()
        _, body = self._query(path, [], WEIGHT["time"], CallPriority.RECONCILE, signed=False)
        st = body.get("serverTime") if isinstance(body, dict) else None
        if isinstance(st, bool) or not isinstance(st, int):
            raise BinanceQueryError(f"{path}: 응답 해석 불가", QueryKind.FAILED)
        offset = st - (sent + self._now()) // 2
        if abs(offset) >= CLOCK_WARN_MS:
            self._alert("CLOCK", f"바이낸스 서버 시각 오차 {offset}ms (경고 기준 {CLOCK_WARN_MS}ms)")
        return offset

    # --- 내부 ---
    @staticmethod
    def _symbol(symbol: str) -> Params:
        try:
            return symbol_params(symbol)
        except OrderBuildError as exc:
            raise BinanceQueryError(f"조회 요청 생성 실패: {exc}", QueryKind.FAILED) from None

    def _window(self, symbol: str, start_ms: int, end_ms: int) -> Params:
        try:
            return window_params(symbol, start_ms=start_ms, end_ms=end_ms)
        except OrderBuildError as exc:
            raise BinanceQueryError(f"조회 요청 생성 실패: {exc}", QueryKind.FAILED) from None

    def _query(self, path: str, params: Params, weight: int, priority: CallPriority, *, signed: bool = True,
               results: tuple[QueryKind, ...] = ()) -> tuple[QueryKind, object]:
        last = ""
        last_kind = QueryKind.FAILED
        sends = waits = 0
        while sends < QUERY_ATTEMPTS:
            adm = self._budget.admit(weight, priority)
            if not adm.ok:
                if self._budget.banned:
                    raise BinanceQueryError(f"GET {path}: {adm.reason}", QueryKind.BANNED)
                if adm.wait_s > 0 and waits < QUERY_ATTEMPTS:      # 429 대기(Retry-After) — 대기 횟수도 제한
                    waits += 1
                    self._sleep(adm.wait_s)
                    continue
                raise BinanceQueryError(f"GET {path}: 호출 보류 — {adm.reason}", QueryKind.RATE_LIMITED)
            sends += 1
            try:
                reply = self._send("GET", path, params, signed=signed)
            except (PreSendError, AfterSendError) as exc:
                last, last_kind = f"GET {path}: 전송 실패({type(exc).__name__})", QueryKind.FAILED
                self._backoff(sends)
                continue
            body = _json(reply.text)
            v = classify_query_reply(reply.status, body)
            self._after_reply(reply, Flag.BANNED if v.kind is QueryKind.BANNED else
                              Flag.RATE_LIMITED if v.kind is QueryKind.RATE_LIMITED else Flag.NONE)
            if signed:   # -1021 연속 판정은 서명 요청 응답 전부(주문·조회)로 센다. 서버 시각 조회(서명 없음)는 제외 (D-115)
                self._watch_clock(v.kind is QueryKind.CLOCK)
            if v.kind is QueryKind.OK or v.kind in results:
                return v.kind, body
            last, last_kind = f"GET {path}: {v.reason}", v.kind
            if v.kind in (QueryKind.BANNED, QueryKind.AUTH, QueryKind.CLOCK, QueryKind.NOT_FOUND,
                          QueryKind.ARCHIVED):
                break
            if v.kind is not QueryKind.RATE_LIMITED:   # 429는 다음 허가에서 쿨다운만큼 기다린다
                self._backoff(sends)
        raise BinanceQueryError(last or f"GET {path}: 조회 실패", last_kind)

    def _backoff(self, sends: int) -> None:
        if sends < QUERY_ATTEMPTS:
            self._sleep(QUERY_BACKOFF_S[min(sends - 1, len(QUERY_BACKOFF_S) - 1)])

    def _parse(self, fn: Callable[[object], object], body: object, path: str):  # type: ignore[no-untyped-def]
        try:
            return fn(body)
        except BinanceParseError as exc:
            raise BinanceQueryError(f"{path}: 응답 해석 불가({exc})", QueryKind.FAILED) from None

    def _send(self, method: str, path: str, params: Params, *, signed: bool = True) -> RawReply:
        if signed:
            query = signed_query(params, self._creds, timestamp_ms=self._now(), recv_window_ms=self._recv_window)
            headers = self._creds.api_key_header()
        else:
            query = encode(params)
            headers = {}
        url = f"{self._base}{path}" + (f"?{query}" if query else "")
        return self._transport.send(method, url, headers)

    def _after_reply(self, reply: RawReply, flag: Flag) -> None:
        self._budget.observe_headers(reply.headers)
        if flag is Flag.BANNED or reply.status == 418:
            self._budget.on_418()
            self._alert("K5", "HTTP 418 IP 차단 — 모든 호출 중단, 사람 재개 필요")
        elif flag is Flag.RATE_LIMITED or reply.status == 429:
            self._budget.on_429(_retry_after(reply.headers))

    def _watch_clock(self, clock_reject: bool) -> None:
        if not clock_reject:
            self._clock = _ClockWatch()
            return
        self._clock.streak += 1
        if self._clock.streak == 1:   # 시계 경고 (D-115) — 연속 구간마다 한 번 [제안: 매 수신이 아니라 구간 시작에만]
            self._alert("CLOCK", "-1021 시각 검증 거부 — 시계 경고")
        if self._clock.streak >= CLOCK_STREAK_ALERT and not self._clock.alerted:
            self._clock.alerted = True
            self._alert("K6", f"-1021 연속 {self._clock.streak}회 — 시계 판정 요청")


def _json(text: str) -> object:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _retry_after(headers: Mapping[str, str]) -> float | None:
    for k, v in headers.items():
        if k.lower() == "retry-after":
            try:
                n = float(v.strip())
            except ValueError:
                return None
            return n if n >= 0 else None
    return None
