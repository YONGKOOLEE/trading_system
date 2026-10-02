"""키움 실시간 (WebSocket) — 전용 IO 스레드 1개, 세션 1개 (03 D-050·D-038 v0.5, kiwoom-kr §5).

프로토콜 (S2 공식 클라이언트 ws_client.py·명세 JSON, 2026-10-03 확인):
- 접속 `<ws_url>/api/dostk/websocket` → `{"trnm":"LOGIN","token":...}` → LOGIN 응답 return_code 0 확인(10초).
- 서버 PING(`{"trnm":"PING",...}` 또는 문자열 "PING")은 받은 그대로 돌려보낸다.
- 등록 `{"trnm":"REG","grp_no":"1","refresh":"1","data":[{"item":[...],"type":[...]}]}` → 응답 return_code 0.
  item·type은 목록(S2 공식 예제 subscribe_domestic_order_fill_async.py — 00은 item 빈 목록). 등록 응답이 0이
  아니면 실패로 처리해 조용히 넘어가지 않는다.
- 실시간 수신 `{"trnm":"REAL","data":[{"type":"00","item":...,"values":{FID:값}}]}`.
끊기면: 상태 OFFLINE 알림(호출자가 위험 확대 거부) → 지수 대기(1·2·4…최대 60초) 재접속 최대 10회 →
성공하면 RECONNECTED 알림(호출자가 조회 대조로 누락분 보완), 10회 실패면 FAILED + 치명 알림(K5).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from trading_system.domain.clock import KST, Clock
from trading_system.domain.models import OrderEvent

from . import parse
from .errors import classify, ErrorClass, normalize_code, specific_code
from .token import TokenError, TokenManager

WS_PATH = "/api/dostk/websocket"
LOGIN_TIMEOUT = 10.0
MAX_RECONNECTS = 10
BACKOFF_CAP = 60.0


class WsState(StrEnum):
    CONNECTING = "CONNECTING"
    ONLINE = "ONLINE"
    RECONNECTED = "RECONNECTED"   # 재접속 성공 — 조회 대조 필요
    OFFLINE = "OFFLINE"
    FAILED = "FAILED"             # 재접속 한도 초과 → K5
    STOPPED = "STOPPED"


# --- 00 주문체결 → 통보 (순수 함수) --------------------------------------------------------------

def parse_order_event(values: dict, day: date, alias: str) -> OrderEvent | None:
    """00 값 → 통보. 접수는 통보 없음(주문 응답으로 앎). 해석 불가면 KiwoomParseError.

    체결 수량·가격은 이번 체결분(단위체결량 915·단위체결가 914)을 쓴다. 911·910은 누계로 보고 쓰지 않는다
    (K-U16: PAPER-0에서 911 의미 확인).
    """
    state = str(values.get("913") or "").strip()
    no = parse.order_no(values.get("9203"), "9203")
    original = parse.optional_order_no(values.get("904"), "904")
    at = parse.hms(values.get("908"), day, "908")
    trade_date = day.isoformat()
    kind_text = values.get("905")
    if state == "접수":
        return None
    if state == "체결":
        fill_id = str(values.get("909") or "").strip()
        if not fill_id:
            raise parse.KiwoomParseError("체결 통보에 체결번호(909) 없음")
        q = parse.qty(values.get("915"), "915")
        if q <= 0:
            raise parse.KiwoomParseError("체결 통보 단위체결량(915)이 0")
        return OrderEvent(kind="FILL", order_no=no, fill_id=fill_id, quantity=q,
                          price=parse.price(values.get("914"), "914"), at=at, account=alias, trade_date=trade_date)
    if state == "거부":
        return OrderEvent(kind="REJECTED", order_no=no, at=at, account=alias, trade_date=trade_date)
    if state in ("확인", "취소"):
        kind = parse.kind_from_text(kind_text)
        if kind == "CANCEL" or (state == "취소" and kind == "NEW"):
            target = original or no
            return OrderEvent(kind="CANCELLED", order_no=target, quantity=parse.qty(values.get("900") or "0", "900"),
                              at=at, account=alias, trade_date=trade_date)
        if kind == "MODIFY":
            if original is None:
                raise parse.KiwoomParseError("정정 확인에 원주문번호(904) 없음")
            return OrderEvent(kind="MODIFIED", order_no=original, new_order_no=no,
                              quantity=parse.qty(values.get("900"), "900"), price=parse.price(values.get("901"), "901"),
                              at=at, account=alias, trade_date=trade_date)
    raise parse.KiwoomParseError(f"알 수 없는 주문상태 '{state}'")


@dataclass(frozen=True)
class BalanceTick:
    """04 잔고 통보 (계좌번호 제거). 체결 후 내부 포지션과 비교용 (03 §6.3)."""

    instrument: str
    quantity: Decimal
    avg_price: Decimal
    at_day: date


def parse_balance(values: dict, day: date) -> BalanceTick:
    return BalanceTick(parse.stock_code(values.get("9001"), "9001"), parse.qty(values.get("930"), "930"),
                       parse.price(values.get("931") or "0", "931"), day)


# --- 수신 스레드 ----------------------------------------------------------------------------------

Handler = Callable[[str, dict], None]   # (item, values)


class KiwoomRealtime:
    def __init__(self, ws_url: str, tokens: TokenManager, clock: Clock, *,
                 registrations: list[tuple[str, list[str]]],
                 handlers: dict[str, Handler],
                 on_state: Callable[[WsState, str], None] | None = None,
                 on_critical: Callable[[str, str], None] | None = None,
                 connect: Callable[..., Any] | None = None,
                 backoff_base: float = 1.0, backoff_cap: float = BACKOFF_CAP,
                 max_reconnects: int = MAX_RECONNECTS, login_timeout: float = LOGIN_TIMEOUT) -> None:
        self._url = ws_url.rstrip("/") + WS_PATH
        self._tokens = tokens
        self._clock = clock
        self._regs = registrations
        self._handlers = handlers
        self._on_state = on_state or (lambda s, m: None)
        self._on_critical = on_critical or (lambda k, m: None)
        if connect is None:
            from websockets.sync.client import connect as ws_connect

            def connect(url: str, **kw: Any) -> Any:  # 직접 호출 + 닫기는 이 클래스가 책임 (websockets 17 legacy)
                return ws_connect(url, legacy=True, **kw)
        self._connect = connect
        self._base = backoff_base
        self._cap = backoff_cap
        self._max = max_reconnects
        self._login_timeout = login_timeout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws: Any = None
        self.state = WsState.STOPPED
        self.last_message_at = None
        self.reconnects = 0
        self.parse_errors: list[str] = []

    # 상태 ------------------------------------------------------------------------------
    def _set(self, state: WsState, msg: str = "") -> None:
        self.state = state
        self._on_state(state, msg)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="kiwoom-ws", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 — 종료 중 오류는 무시
                pass
        if self._thread is not None:
            self._thread.join(timeout)
        self._set(WsState.STOPPED)

    # 본체 ------------------------------------------------------------------------------
    def _run(self) -> None:
        failures = 0
        ever_online = False
        while not self._stop.is_set():
            self._set(WsState.CONNECTING)
            try:
                self._session_open()
                failures = 0
                if ever_online:
                    self.reconnects += 1
                    self._set(WsState.RECONNECTED, "재접속 — 조회 대조 필요")
                else:
                    self._set(WsState.ONLINE)
                ever_online = True
                self._pump()
            except Exception as exc:  # noqa: BLE001 — 어떤 실패든 재접속 정책으로
                if self._stop.is_set():
                    break
                failures += 1
                self._set(WsState.OFFLINE, f"{type(exc).__name__}: {str(exc)[:120]}")
                if failures >= self._max:
                    self._set(WsState.FAILED, f"재접속 {failures}회 실패")
                    self._on_critical("WS_FAILED", f"실시간 재접속 {failures}회 실패")
                    return
                self._stop.wait(min(self._base * 2 ** (failures - 1), self._cap))
            finally:
                ws, self._ws = self._ws, None
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:  # noqa: BLE001
                        pass

    def _session_open(self) -> None:
        try:
            token = self._tokens.token()
        except TokenError as exc:
            raise ConnectionError(f"토큰 없음: {exc}") from None
        self._ws = self._connect(self._url, open_timeout=5, ping_interval=None, proxy=None, close_timeout=2)
        self._send({"trnm": "LOGIN", "token": token})
        msg = self._recv_control("LOGIN", self._login_timeout)
        self._check_ack(msg, "LOGIN", auth=True)
        for rtype, items in self._regs:
            self._send({"trnm": "REG", "grp_no": "1", "refresh": "1", "data": [{"item": items, "type": [rtype]}]})
            self._check_ack(self._recv_control("REG", self._login_timeout), f"REG {rtype}", auth=False)

    def _check_ack(self, msg: dict, what: str, auth: bool) -> None:
        code = specific_code(normalize_code(msg.get("return_code")), msg.get("return_msg"))
        if code in (0, None):
            return
        cls = classify(code)
        if auth and cls in (ErrorClass.AUTH, ErrorClass.TERMINAL, ErrorClass.MODE_MISMATCH):
            self._tokens.invalidate()
        if cls in (ErrorClass.MODE_MISMATCH, ErrorClass.TERMINAL):
            self._on_critical(cls.value, f"{what} 거부 코드 {code}")
        raise ConnectionError(f"{what} 거부 (코드 {code})")

    def _send(self, obj: object) -> None:
        self._ws.send(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False))

    def _recv_control(self, trnm: str, timeout: float) -> dict:
        """지정 trnm 응답이 올 때까지 받는다(PING은 응답, REAL은 처리)."""
        while True:
            msg = self._recv(timeout)
            if isinstance(msg, dict) and str(msg.get("trnm", "")).upper() == trnm:
                return msg
            self._handle(msg)

    def _recv(self, timeout: float | None) -> Any:
        raw = self._ws.recv(timeout=timeout)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        text = raw.strip() if isinstance(raw, str) else raw
        try:
            return json.loads(text) if isinstance(text, str) and text[:1] in "{[" else text
        except json.JSONDecodeError:
            return text

    def _pump(self) -> None:
        while not self._stop.is_set():
            try:
                msg = self._recv(1.0)
            except TimeoutError:
                continue
            self._handle(msg)

    def _handle(self, msg: Any) -> None:
        self.last_message_at = self._clock.now()
        if (isinstance(msg, str) and msg.upper() == "PING") or \
                (isinstance(msg, dict) and str(msg.get("trnm", "")).upper() == "PING"):
            self._send(msg)
            return
        if not isinstance(msg, dict) or str(msg.get("trnm", "")).upper() != "REAL":
            return
        for d in msg.get("data") or []:
            if not isinstance(d, dict):
                continue
            h = self._handlers.get(str(d.get("type")))
            values = d.get("values")
            if h is None or not isinstance(values, dict):
                continue
            try:
                h(str(d.get("item") or ""), values)
            except parse.KiwoomParseError as exc:
                self.parse_errors.append(f"{d.get('type')}: {exc}")
                del self.parse_errors[:-100]


def order_handlers(push: Callable[[OrderEvent], None], alias: str, clock: Clock,
                   account_ok: Callable[[object], bool],
                   on_balance: Callable[[BalanceTick], None] | None = None) -> dict[str, Handler]:
    """00·04 처리기. 계좌번호(9201)는 설정 계좌인지 확인만 하고 버린다 (D-059)."""

    def day() -> date:
        return clock.now().astimezone(KST).date()

    def h00(item: str, values: dict) -> None:
        if not account_ok(values.get("9201")):
            return
        ev = parse_order_event(values, day(), alias)
        if ev is not None:
            push(ev)

    def h04(item: str, values: dict) -> None:
        if not account_ok(values.get("9201")):
            return
        if on_balance is not None:
            on_balance(parse_balance(values, day()))

    return {"00": h00, "04": h04}
