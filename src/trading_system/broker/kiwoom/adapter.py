"""키움 모의(PAPER) REST 어댑터 — 국내주식 M1 (03 §5·D-041~D-050·D-059·D-094, kiwoom-kr).

경계 규칙
- 접속 대상은 **모의 도메인**(또는 시험용 루프백)만 허용한다. 실전 도메인 상수는 코드에 없다 (승인 구조 3·4 전).
- 주문 변경(kt10000~kt10003)은 한 번만 보낸다. 결과는 4분류(03 §5.2). 인증 오류도 재전송하지 않는다.
  · 전송 전 실패: 토큰 발급 실패·로컬 검증 실패·호출 제한 대기 초과·연결 수립 실패
  · 명시 거부: 응답 본문 수신 + 업무 오류(입력·호출 제한·모의 미지원·기타 업무 거부)
  · 결과 불명: 전송 후 타임아웃·끊김, HTTP 5xx, 본문 해석 불가, 1999, **인증 오류(전송 후, A-028)**, 0인데 주문번호 없음
  · 접수: return_code 0 + 주문번호
- 거래소 구분 KRX 고정(D-045). 대조 조회는 통합 구분이 있는 TR만 통합(kt00009 '%', ka10075 '0').
- 정정 수량은 요청 수량 그대로(게이트가 잔량 전부만 허용, V-120). 취소 수량은 '0'(잔량 전부).
- 응답의 계좌번호는 설정 계좌와 같은지 확인한 뒤 버린다. 다르면 그 레코드를 쓰지 않고 치명 알림(D-059).
- 조회: 페이지마다 호출 제한기 통과. 호출 제한 초과(1700 등)는 대기 후 최대 3회, 인증 오류는 토큰 재발급 후
  같은 3회 안에서 다시 조회(조회는 위험 변경이 아님). 그래도 안 되면 BrokerQueryError(호출자가 K5 정책).
- 해석할 수 없는 행이 하나라도 있으면 조회 전체를 실패로 본다 — "주문 없음"으로 잘못 확정하지 않게(D-047).
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from urllib.parse import urlparse

from trading_system.broker.base import BrokerQueryError
from trading_system.broker.ratelimit import AcquireTimeout, Priority, RateLimiter
from trading_system.domain.clock import KST, Clock
from trading_system.domain.enums import OrderType, SubmitOutcome
from trading_system.domain.market import CashView
from trading_system.domain.models import (
    BrokerOrderRecord, BrokerPosition, FillRecord, OrderEvent, OrderRequest, SubmitResult,
)

from . import parse
from .errors import CRITICAL, ErrorClass, classify, normalize_code, specific_code
from .http import AfterSendError, HttpReply, KiwoomHttp, PreSendError
from .token import TokenError, TokenManager

MOCK_REST = "https://mockapi.kiwoom.com"
MOCK_WS = "wss://mockapi.kiwoom.com:10000"
ORDER_PATH, ACCOUNT_PATH = "/api/dostk/ordr", "/api/dostk/acnt"
QUERY_ATTEMPTS = 3
MAX_PAGES = 50
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class EndpointError(ValueError):
    pass


def check_endpoint(url: str, allowed: str) -> str:
    """모의 도메인 또는 시험용 루프백만 허용 (모드만 바꿔 실전에 닿지 않게, CLAUDE.md §7)."""
    if url.rstrip("/") == allowed:
        return url.rstrip("/")
    p = urlparse(url)
    if p.hostname in _LOOPBACK and p.scheme in ("http", "https", "ws", "wss"):
        return url.rstrip("/")
    raise EndpointError("키움 모의 도메인 또는 시험용 루프백만 허용")


@dataclass(frozen=True)
class KiwoomConfig:
    account_alias: str
    account_no: Callable[[], str]      # 비밀 — 응답 계좌 확인용으로만 메모리에서 비교
    app_key: Callable[[], str]
    secret_key: Callable[[], str]
    rest_url: str = MOCK_REST
    ws_url: str = MOCK_WS
    per_tr_interval: float = 1.1       # 모의 TR당 1초 1회 (kiwoom-kr §2) + 여유
    order_wait: float = 3.0            # 주문 호출 제한 대기 상한 → 넘으면 전송 전 실패
    query_wait: float = 30.0


class KiwoomAdapter:
    """BrokerAdapter 구현 (fake와 같은 계약). 판정·실행 스레드에서 주문, 여러 스레드에서 조회를 호출할 수 있다."""

    def __init__(self, cfg: KiwoomConfig, clock: Clock, *, http: KiwoomHttp | None = None,
                 limiter: RateLimiter | None = None, on_critical: Callable[[str, str], None] | None = None) -> None:
        self.cfg = cfg
        self._clock = clock
        self.http = http or KiwoomHttp(check_endpoint(cfg.rest_url, MOCK_REST))
        check_endpoint(cfg.ws_url, MOCK_WS)
        self.limiter = limiter or RateLimiter(cfg.per_tr_interval)
        self.critical: list[tuple[str, str]] = []
        self._on_critical = on_critical
        self.tokens = TokenManager(self.http, cfg.app_key, cfg.secret_key, clock, self._raise_critical)
        self._events: queue.Queue[OrderEvent] = queue.Queue()
        self._lock = threading.Lock()

    # --- 공통 -------------------------------------------------------------------
    def _raise_critical(self, kind: str, message: str) -> None:
        with self._lock:
            self.critical.append((kind, message))
            del self.critical[:-100]
        if self._on_critical is not None:
            self._on_critical(kind, message)

    def _today(self) -> date:
        return self._clock.now().astimezone(KST).date()

    def _check_account(self, value: object, where: str) -> bool:
        """응답 계좌번호가 설정 계좌인지. 비교만 하고 값은 어디에도 남기지 않는다."""
        if value in (None, ""):
            return True
        if str(value).strip() == self.cfg.account_no().strip():
            return True
        self._raise_critical("ACCOUNT_MISMATCH", f"{where}: 다른 계좌의 레코드 수신 — 버림")
        return False

    # --- 주문 변경 ---------------------------------------------------------------
    def place_order(self, request: OrderRequest) -> SubmitResult:
        try:
            body = self._new_body(request)
        except ValueError as exc:
            return SubmitResult(SubmitOutcome.PRE_SEND_FAILURE, reason=f"로컬 검증 실패: {exc}")
        return self._send("kt10000" if request.side == "BUY" else "kt10001", body)

    def cancel_order(self, request: OrderRequest) -> SubmitResult:
        try:
            self._check_request(request)
            body = {"dmst_stex_tp": "KRX", "orig_ord_no": parse.order_no(request.target_order_no or "", "원주문번호"),
                    "stk_cd": request.instrument, "cncl_qty": "0"}
        except ValueError as exc:
            return SubmitResult(SubmitOutcome.PRE_SEND_FAILURE, reason=f"로컬 검증 실패: {exc}")
        return self._send("kt10003", body)

    def modify_order(self, request: OrderRequest) -> SubmitResult:
        try:
            self._check_request(request)
            if request.order_type is not OrderType.LIMIT:
                raise ValueError("정정은 지정가만")
            body = {"dmst_stex_tp": "KRX", "orig_ord_no": parse.order_no(request.target_order_no or "", "원주문번호"),
                    "stk_cd": request.instrument, "mdfy_qty": _int_text(request.quantity, "정정수량", positive=True),
                    "mdfy_uv": _int_text(request.price, "정정단가", positive=True), "mdfy_cond_uv": ""}
        except ValueError as exc:
            return SubmitResult(SubmitOutcome.PRE_SEND_FAILURE, reason=f"로컬 검증 실패: {exc}")
        return self._send("kt10002", body)

    def _check_request(self, request: OrderRequest) -> None:
        if request.account != self.cfg.account_alias:
            raise ValueError("이 어댑터의 계좌 별칭이 아님")
        if not (len(request.instrument) == 6 and request.instrument.isascii() and request.instrument.isdigit()):
            raise ValueError("종목코드는 6자리 숫자")
        if request.side not in ("BUY", "SELL"):
            raise ValueError("매매 방향")

    def _new_body(self, request: OrderRequest) -> dict:
        self._check_request(request)
        if request.order_type is OrderType.MARKET:
            trde_tp, uv = "3", ""
        elif request.order_type is OrderType.LIMIT:
            trde_tp, uv = "0", _int_text(request.price, "주문단가", positive=True)
        else:
            raise ValueError(f"지원하지 않는 주문 유형 {request.order_type}")
        return {"dmst_stex_tp": "KRX", "stk_cd": request.instrument,
                "ord_qty": _int_text(request.quantity, "주문수량", positive=True),
                "ord_uv": uv, "trde_tp": trde_tp, "cond_uv": ""}

    def _send(self, api_id: str, body: dict) -> SubmitResult:
        try:
            token = self.tokens.token()
        except TokenError as exc:
            return SubmitResult(SubmitOutcome.PRE_SEND_FAILURE, reason=f"토큰 없음: {exc}")
        try:
            self.limiter.acquire(api_id, self.cfg.order_wait, Priority.ORDER)
        except AcquireTimeout as exc:
            return SubmitResult(SubmitOutcome.PRE_SEND_FAILURE, reason=str(exc))
        try:
            reply = self.http.post(ORDER_PATH, api_id, body, token)
        except PreSendError as exc:
            return SubmitResult(SubmitOutcome.PRE_SEND_FAILURE, reason=str(exc))
        except AfterSendError as exc:
            return SubmitResult(SubmitOutcome.UNKNOWN, reason=str(exc))
        return self._classify_order(api_id, reply)

    def _classify_order(self, api_id: str, r: HttpReply) -> SubmitResult:
        if r.status >= 500:
            return SubmitResult(SubmitOutcome.UNKNOWN, reason=f"{api_id}: HTTP {r.status}")
        if r.body is None:
            return SubmitResult(SubmitOutcome.UNKNOWN, reason=f"{api_id}: 응답 해석 불가 (HTTP {r.status})")
        raw = normalize_code(r.body.get("return_code"))
        code = specific_code(raw, r.body.get("return_msg"))
        if r.status == 401 and code in (None, 0):
            code = 8005  # 본문 코드 없는 401: 인증 오류로 본다
        cls = classify(code)   # 코드 없음(None)은 UNEXPECTED → 결과 불명
        reason = f"{api_id}: 코드 {code} ({cls.value}) {str(r.body.get('return_msg') or '')[:80]}"
        if cls is ErrorClass.OK:
            if r.status >= 400:
                return SubmitResult(SubmitOutcome.UNKNOWN, reason=f"{api_id}: HTTP {r.status}인데 성공 코드")
            try:
                no = parse.order_no(r.body.get("ord_no"))
            except parse.KiwoomParseError:
                return SubmitResult(SubmitOutcome.UNKNOWN, reason=f"{api_id}: 성공 코드인데 주문번호 없음")
            self.limiter.succeeded(api_id)
            return SubmitResult(SubmitOutcome.ACCEPTED, order_no=no)
        if cls in CRITICAL:
            self._raise_critical(cls.value, reason)
        if cls in (ErrorClass.AUTH, ErrorClass.TERMINAL, ErrorClass.MODE_MISMATCH):
            self.tokens.invalidate()
            # 인증 오류 때 주문을 받지 않는다는 근거가 없다 → 결과 불명, 조회로 확인 (01 §2.7, A-028, K-U12)
            return SubmitResult(SubmitOutcome.UNKNOWN, reason=reason)
        if cls is ErrorClass.UNEXPECTED:
            return SubmitResult(SubmitOutcome.UNKNOWN, reason=reason)
        if cls is ErrorClass.RATE_LIMIT:
            self.limiter.penalize(api_id)
        return SubmitResult(SubmitOutcome.REJECTED, reason=reason)

    # --- 실시간 통보 -------------------------------------------------------------
    def push_event(self, event: OrderEvent) -> None:
        """실시간 수신 스레드가 정규화한 통보를 넣는다 (realtime.KiwoomRealtime)."""
        self._events.put(event)

    def poll_events(self) -> list[OrderEvent]:
        out: list[OrderEvent] = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    # --- 조회 ---------------------------------------------------------------------
    def query_raw(self, path: str, api_id: str, body: dict, list_key: str | None,
                  enough: Callable[[list[dict]], bool] | None = None) -> tuple[dict, list[dict]]:
        """시장 데이터 공급자용 조회 (T-021). 우선순위는 호출 문맥(시세는 MARKET)을 따른다."""
        return self._query(api_id, body, list_key, path, enough)

    def _query(self, api_id: str, body: dict, list_key: str | None, path: str = ACCOUNT_PATH,
               enough: Callable[[list[dict]], bool] | None = None) -> tuple[dict, list[dict]]:
        """연속조회까지 모아 (첫 페이지 본문, 목록 행 전체)를 돌려준다. enough가 참이면 거기서 멈춘다."""
        rows: list[dict] = []
        first: dict | None = None
        cont: tuple[str | None, str | None] = (None, None)
        for _page in range(MAX_PAGES):
            page = self._query_page(api_id, body, cont, path)
            first = first or page.body
            assert page.body is not None
            if list_key is not None:
                items = page.body.get(list_key)
                if items is None:
                    items = []
                if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                    raise BrokerQueryError(f"{api_id}: 목록 형식 아님")
                rows.extend(items)
            if (page.cont_yn or "").upper() != "Y" or not page.next_key or (enough is not None and enough(rows)):
                return first or {}, rows
            cont = ("Y", page.next_key)
        raise BrokerQueryError(f"{api_id}: 연속조회 {MAX_PAGES}쪽 초과 — 일부만 받은 결과는 쓰지 않음")

    def _query_page(self, api_id: str, body: dict, cont: tuple[str | None, str | None],
                    path: str = ACCOUNT_PATH) -> HttpReply:
        last = ""
        for _attempt in range(QUERY_ATTEMPTS):
            try:
                token = self.tokens.token()
                self.limiter.acquire(api_id, self.cfg.query_wait)
                r = self.http.post(path, api_id, body, token, cont[0], cont[1])
            except (TokenError, AcquireTimeout, PreSendError, AfterSendError) as exc:
                raise BrokerQueryError(f"{api_id}: {exc}") from None
            if r.status >= 500 or r.body is None:
                raise BrokerQueryError(f"{api_id}: HTTP {r.status} 응답 해석 불가")
            code = specific_code(normalize_code(r.body.get("return_code")), r.body.get("return_msg"))
            if r.status == 401 and code in (None, 0):
                code = 8005
            cls = classify(code if code is not None else (0 if r.status < 400 else None))
            if cls is ErrorClass.OK and r.status < 400:
                self.limiter.succeeded(api_id)
                return r
            last = f"{api_id}: 코드 {code} ({cls.value})"
            if cls in CRITICAL:
                self._raise_critical(cls.value, last)
                break
            if cls is ErrorClass.RATE_LIMIT:
                self.limiter.penalize(api_id)
                continue
            if cls is ErrorClass.AUTH:
                self.tokens.invalidate()
                continue
            break
        raise BrokerQueryError(last or f"{api_id}: 조회 실패")

    def query_orders(self, account: str) -> list[BrokerOrderRecord]:
        self._require_account(account)
        day = self._today()
        _, rows = self._query("kt00009", {
            "ord_dt": day.strftime("%Y%m%d"), "stk_bond_tp": "1", "mrkt_tp": "0", "sell_tp": "0", "qry_tp": "0",
            "stk_cd": "", "fr_ord_no": "", "dmst_stex_tp": "%"}, "acnt_ord_cntr_prst_array")
        _, open_rows = self._query("ka10075", {"all_stk_tp": "0", "trde_tp": "0", "stk_cd": "", "stex_tp": "0"},
                                   "oso")
        try:
            return build_orders(rows, [r for r in open_rows if self._check_account(r.get("acnt_no"), "ka10075")])
        except parse.KiwoomParseError as exc:
            raise BrokerQueryError(f"주문 조회 해석 불가: {exc}") from None

    def query_fills(self, account: str) -> list[FillRecord]:
        self._require_account(account)
        day = self._today()
        _, rows = self._query("kt00009", {
            "ord_dt": day.strftime("%Y%m%d"), "stk_bond_tp": "1", "mrkt_tp": "0", "sell_tp": "0", "qry_tp": "1",
            "stk_cd": "", "fr_ord_no": "", "dmst_stex_tp": "%"}, "acnt_ord_cntr_prst_array")
        try:
            return build_fills(rows, day)
        except parse.KiwoomParseError as exc:
            raise BrokerQueryError(f"체결 조회 해석 불가: {exc}") from None

    def query_positions(self, account: str) -> list[BrokerPosition]:
        self._require_account(account)
        _, rows = self._query("kt00018", {"qry_tp": "1", "dmst_stex_tp": "KRX"}, "acnt_evlt_remn_indv_tot")
        out: dict[str, BrokerPosition] = {}
        try:
            for r in rows:
                code = parse.stock_code(r.get("stk_cd"))
                q = parse.qty(r.get("rmnd_qty"), "rmnd_qty")
                if code in out:
                    raise parse.KiwoomParseError(f"잔고 종목 중복 {code}")
                if q > 0:
                    out[code] = BrokerPosition(code, q, parse.price(r.get("pur_pric"), "pur_pric"))
        except parse.KiwoomParseError as exc:
            raise BrokerQueryError(f"잔고 조회 해석 불가: {exc}") from None
        return [out[k] for k in sorted(out)]

    def query_cash(self, account: str) -> CashView:
        self._require_account(account)
        # kt00001 예수금상세현황(qry_tp 3 추정조회, 명세 예시값). kt00005는 모의 미제공(실측 2026-10-03, RC9000)
        body, _ = self._query("kt00001", {"qry_tp": "3"}, None)
        try:
            return CashView(parse.dec(body.get("entr"), "entr"), parse.dec(body.get("ord_alow_amt"), "ord_alow_amt"))
        except parse.KiwoomParseError as exc:
            raise BrokerQueryError(f"예수금 조회 해석 불가: {exc}") from None

    def _require_account(self, account: str) -> None:
        if account != self.cfg.account_alias:
            raise BrokerQueryError("이 어댑터의 계좌 별칭이 아님")

    def close(self) -> bool:
        """종료: 토큰 폐기(au10002)."""
        return self.tokens.revoke()


# --- 정규화 (순수 함수 — 계약 시험 대상) ----------------------------------------------------------

def _int_text(v: Decimal, field: str, positive: bool) -> str:
    if v != v.to_integral_value() or (positive and v <= 0) or v < 0:
        raise ValueError(f"{field}는 {'양의 ' if positive else ''}정수")
    return str(int(v))


def _order_type(text: object) -> OrderType:
    return OrderType.MARKET if "시장가" in str(text or "") else OrderType.LIMIT


def build_orders(rows: list[dict], open_rows: list[dict]) -> list[BrokerOrderRecord]:
    """kt00009(당일 전체) + ka10075(미체결) → 주문 레코드. 행이 체결 단위로 여러 개여도 주문번호로 묶는다.

    상태: 미체결 목록에 있으면 ACCEPTED/PARTIAL(체결 = 주문 − 미체결). 아니면 전량 체결 FILLED, 취소 주문이
    가리키면 CANCELLED, 정정 주문이 가리키면 MODIFIED, 접수구분에 '거부'면 REJECTED, 그 밖에는 CLOSED_UNKNOWN
    (대조에서 상태 차이로 잡혀 사람이 확인). 취소 주문 자체는 CONFIRMED.
    """
    by_no: dict[str, dict] = {}
    fills: dict[str, dict[str, Decimal]] = {}
    for r in rows:
        no = parse.order_no(r.get("ord_no"))
        kind = parse.kind_from_text(r.get("mdfy_cncl_tp"), r.get("io_tp_nm"))
        rec = {"kind": kind, "instrument": parse.stock_code(r.get("stk_cd")),
               "side": parse.side_from_text(r.get("io_tp_nm"), "io_tp_nm"),
               "quantity": parse.qty(r.get("ord_qty"), "ord_qty"), "price": parse.price(r.get("ord_uv"), "ord_uv"),
               "order_type": _order_type(r.get("trde_tp")),
               "original": parse.optional_order_no(r.get("orig_ord_no"), "orig_ord_no"),
               "rejected": "거부" in str(r.get("acpt_tp") or "")}
        prev = by_no.get(no)
        if prev is not None and any(prev[k] != rec[k] for k in ("kind", "instrument", "side", "quantity")):
            raise parse.KiwoomParseError(f"주문 {no}의 행 내용이 서로 다름")
        by_no[no] = rec if prev is None else {**prev, "rejected": prev["rejected"] or rec["rejected"]}
        cntr = str(r.get("cntr_no") or "").strip()
        cq = parse.qty(r.get("cntr_qty") or "0", "cntr_qty")
        if cntr and cntr.strip("0") and cq > 0:
            fills.setdefault(no, {})[cntr] = cq
    open_by_no: dict[str, dict] = {}
    for r in open_rows:
        no = parse.order_no(r.get("ord_no"))
        open_by_no[no] = {"quantity": parse.qty(r.get("ord_qty"), "ord_qty"),
                          "left": parse.qty(r.get("oso_qty"), "oso_qty"),
                          "instrument": parse.stock_code(r.get("stk_cd")),
                          "side": parse.side_from_text(r.get("io_tp_nm"), "io_tp_nm"),
                          "kind": parse.kind_from_text(r.get("io_tp_nm")),
                          "price": parse.price(r.get("ord_pric"), "ord_pric"),
                          "order_type": _order_type(r.get("trde_tp")),
                          "original": parse.optional_order_no(r.get("orig_ord_no"), "orig_ord_no")}
    cancelled = {v["original"] for v in by_no.values() if v["kind"] == "CANCEL" and v["original"]}
    modified = {v["original"] for v in by_no.values() if v["kind"] == "MODIFY" and v["original"]}
    out: list[BrokerOrderRecord] = []
    for no in sorted(set(by_no) | set(open_by_no)):
        base = by_no.get(no) or open_by_no[no]
        o = open_by_no.get(no)
        filled = sum(fills.get(no, {}).values(), Decimal("0"))
        if filled > base["quantity"]:
            raise parse.KiwoomParseError(f"주문 {no} 체결 합계가 주문 수량 초과")
        if base["kind"] == "CANCEL":
            state = "CONFIRMED"
        elif o is not None and o["left"] > 0:
            filled = o["quantity"] - o["left"]
            state = "PARTIAL" if filled > 0 else "ACCEPTED"
        elif filled == base["quantity"]:
            state = "FILLED"
        elif no in cancelled:
            state = "CANCELLED"
        elif no in modified:
            state = "MODIFIED"
        elif base.get("rejected"):
            state = "REJECTED"
        else:
            state = "CLOSED_UNKNOWN"
        out.append(BrokerOrderRecord(order_no=no, kind=base["kind"], instrument=base["instrument"], side=base["side"],
                                     quantity=base["quantity"], filled_quantity=filled, price=base["price"],
                                     order_type=base["order_type"], state=state, original_order_no=base["original"]))
    return out


def build_fills(rows: list[dict], day: date) -> list[FillRecord]:
    """kt00009 체결 행 → 체결 레코드. 체결번호가 없는 행은 쓰지 않는다 (D-094)."""
    seen: dict[tuple[str, str], FillRecord] = {}
    for r in rows:
        cntr = str(r.get("cntr_no") or "").strip()
        q = parse.qty(r.get("cntr_qty") or "0", "cntr_qty")
        if not cntr or not cntr.strip("0") or q <= 0:
            continue
        no = parse.order_no(r.get("ord_no"))
        f = FillRecord(no, cntr, q, parse.price(r.get("cntr_uv"), "cntr_uv"), parse.hms(r.get("cntr_tm"), day, "cntr_tm"))
        prev = seen.get((no, cntr))
        if prev is not None and prev != f:
            raise parse.KiwoomParseError(f"체결 {no}/{cntr} 행 내용이 서로 다름")
        seen[(no, cntr)] = f
    return list(seen.values())
