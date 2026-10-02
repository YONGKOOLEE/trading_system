"""시험용 키움 모의 서버 (루프백 HTTP + WebSocket). 응답 형태는 키움 명세(S2, 2026-10-03 확인)의 필드 이름을 따른
합성 데이터다. 실제 서버 동작(체결 방식·오류 형태)은 재현하지 않는다 — PAPER-0에서 실측(PV-xx).

모든 값은 가상이다: 계좌번호 9999999999, 앱키·시크릿·토큰은 가상 문자열.
"""

from __future__ import annotations

import itertools
import json
import queue
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from websockets.sync.server import serve

ACCOUNT_NO = "9999999999"
APP_KEY, SECRET = "virtual-app-key", "virtual-secret"
KST_FMT = "%Y%m%d%H%M%S"


@dataclass
class SimOrder:
    no: str
    kind: str            # NEW / CANCEL / MODIFY
    side: str            # BUY / SELL
    code: str
    qty: Decimal
    price: Decimal
    market: bool
    orig: str = "0000000"
    filled: Decimal = Decimal("0")
    open: bool = True
    rejected: bool = False


@dataclass
class Scripted:
    """다음 호출 한 번에 쓸 응답. mode: body / status / timeout / close / nonjson."""
    mode: str
    status: int = 200
    body: dict | None = None
    apply: bool = False   # True면 응답은 망가뜨리되 주문은 실제로 처리(응답 유실 재현)


@dataclass
class Sim:
    expires_in: timedelta = timedelta(hours=24)
    now: Callable[[], datetime] = field(default=lambda: datetime.now())
    auto_fill: bool = False
    page_size: int = 100
    always_cont: bool = False
    orders: dict[str, SimOrder] = field(default_factory=dict)
    fills: list[tuple[str, str, Decimal, Decimal, str]] = field(default_factory=list)  # 주문, 체결번호, 수량, 가격, 시각
    positions: dict[str, tuple[Decimal, Decimal]] = field(default_factory=dict)
    entr: Decimal = Decimal("10000000")
    log: list[tuple[str, dict, dict]] = field(default_factory=list)       # (api-id, 본문, 헤더)
    scripts: dict[str, list[Scripted]] = field(default_factory=dict)
    valid_tokens: set[str] = field(default_factory=set)
    token_fail: int = 0          # 다음 N회 토큰 발급 실패
    token_code: int = 8001
    ws_out: "queue.Queue[str]" = field(default_factory=queue.Queue)
    ws_in: list[Any] = field(default_factory=list)
    ws_logins: int = 0
    ws_reject_login: bool = False
    ws_reject_reg: bool = False
    extra_rows: list[dict] = field(default_factory=list)   # kt00009에 덧붙일 행 (외부·이상 행)
    extra_open: list[dict] = field(default_factory=list)   # ka10075에 덧붙일 행

    def __post_init__(self) -> None:
        self._seq = itertools.count(1)
        self._cntr = itertools.count(1)
        self._tok = itertools.count(1)
        self._lock = threading.RLock()
        self._ws_conns: list[Any] = []
        self._start_http()
        self._start_ws()

    # --- 서버 ----------------------------------------------------------------------------
    def _start_http(self) -> None:
        sim = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = json.loads(raw or b"{}")
                api = self.headers.get("api-id", "")
                hdr = {k.lower(): v for k, v in self.headers.items()}
                sim.log.append((api, body, hdr))
                sc = sim.scripts.get(api) or []
                script = sc.pop(0) if sc else None
                if script is not None and script.mode == "close":
                    if script.apply:
                        sim._handle(api, body, hdr)
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if script is not None and script.mode == "timeout":
                    if script.apply:
                        sim._handle(api, body, hdr)
                    time.sleep(1.0)
                    return
                if script is not None and script.mode in ("status", "nonjson", "body"):
                    if script.apply:
                        sim._handle(api, body, hdr)
                    if script.mode == "nonjson":
                        return self._raw(script.status, b"<html>oops</html>", {})
                    return self._send(script.status, script.body or {}, {})
                status, out, headers = sim._handle(api, body, hdr)
                self._send(status, out, headers)

            def _send(self, status: int, obj: dict, headers: dict) -> None:
                self._raw(status, json.dumps(obj, ensure_ascii=False).encode(), headers)

            def _raw(self, status: int, data: bytes, headers: dict) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json;charset=UTF-8")
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def _start_ws(self) -> None:
        sim = self

        def handler(ws: Any) -> None:
            sim._ws_conns.append(ws)
            try:
                login = json.loads(ws.recv(timeout=5))
                sim.ws_logins += 1
                ok = not sim.ws_reject_login and login.get("trnm") == "LOGIN" and login.get("token") in sim.valid_tokens
                ws.send(json.dumps({"trnm": "LOGIN", "return_code": 0 if ok else 3,
                                    "return_msg": "" if ok else "[8005:Token이 유효하지 않습니다]"}))
                if not ok:
                    return
                while True:
                    try:
                        msg = ws.recv(timeout=0.02)
                        parsed = json.loads(msg) if msg.strip().startswith("{") else msg
                        sim.ws_in.append(parsed)
                        if isinstance(parsed, dict) and parsed.get("trnm") == "REG":
                            ws.send(json.dumps({"trnm": "REG", "return_code": 1 if sim.ws_reject_reg else 0,
                                                "return_msg": ""}))
                    except TimeoutError:
                        pass
                    while True:
                        try:
                            ws.send(sim.ws_out.get_nowait())
                        except queue.Empty:
                            break
            except Exception:  # noqa: BLE001 — 끊김
                return

        self.ws_server = serve(handler, "127.0.0.1", 0, ping_interval=None)
        self.ws_url = f"ws://127.0.0.1:{self.ws_server.socket.getsockname()[1]}"
        threading.Thread(target=self.ws_server.serve_forever, daemon=True).start()

    def drop_ws(self) -> None:
        for c in list(self._ws_conns):
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        self._ws_conns.clear()

    def close(self) -> None:
        self.http.shutdown()
        self.http.server_close()
        self.ws_server.shutdown()

    # --- 처리 ----------------------------------------------------------------------------
    def script(self, api: str, *items: Scripted) -> None:
        self.scripts.setdefault(api, []).extend(items)

    def _authed(self, hdr: dict) -> bool:
        a = hdr.get("authorization", "")
        return a.startswith("Bearer ") and a[7:] in self.valid_tokens

    def _handle(self, api: str, body: dict, hdr: dict) -> tuple[int, dict, dict]:
        with self._lock:
            if api == "au10001":
                if self.token_fail > 0:
                    self.token_fail -= 1
                    return 200, {"return_code": self.token_code, "return_msg": "가상 발급 실패"}, {}
                if body.get("appkey") != APP_KEY or body.get("secretkey") != SECRET:
                    return 200, {"return_code": 8001, "return_msg": "검증 실패"}, {}
                tok = f"virtual-token-{next(self._tok)}"
                self.valid_tokens.add(tok)
                exp = (self.now() + self.expires_in).strftime(KST_FMT)
                return 200, {"expires_dt": exp, "token_type": "bearer", "token": tok,
                             "return_code": 0, "return_msg": "정상"}, {}
            if api == "au10002":
                self.valid_tokens.discard(body.get("token"))
                return 200, {"return_code": 0, "return_msg": "폐기"}, {}
            if not self._authed(hdr):
                return 200, {"return_code": 3, "return_msg": "[8005:Token이 유효하지 않습니다]"}, {}
            h = getattr(self, f"_{api}", None)
            if h is None:
                return 200, {"return_code": 1505, "return_msg": "없는 API"}, {}
            return h(body, hdr)

    def _new_no(self) -> str:
        return f"{next(self._seq):07d}"

    def _push00(self, o: SimOrder, state: str, kind_text: str, extra: dict | None = None) -> None:
        values = {"9201": ACCOUNT_NO, "9203": o.no, "9001": o.code, "913": state, "900": str(o.qty),
                  "901": str(o.price), "902": str(o.qty - o.filled), "904": o.orig, "905": kind_text,
                  "907": "2" if o.side == "BUY" else "1", "908": self.now().strftime("%H%M%S"),
                  "909": "", "910": "", "911": "", "914": "", "915": "", "919": "0"}
        values.update(extra or {})
        self.ws_out.put(json.dumps({"trnm": "REAL", "data": [{"type": "00", "name": "주문체결", "item": o.code,
                                                               "values": values}]}, ensure_ascii=False))

    def _place(self, body: dict, side: str) -> tuple[int, dict, dict]:
        o = SimOrder(self._new_no(), "NEW", side, body["stk_cd"], Decimal(body["ord_qty"]),
                     Decimal(body["ord_uv"] or "0"), body["trde_tp"] == "3")
        self.orders[o.no] = o
        self._push00(o, "접수", "+매수" if side == "BUY" else "-매도")
        if self.auto_fill:
            self.fill(o.no, price=o.price if not o.market else Decimal("70000"))
        return 200, {"ord_no": o.no.lstrip("0") or "0", "dmst_stex_tp": "KRX", "return_code": 0,
                     "return_msg": "정상적으로 처리되었습니다"}, {}

    def _kt10000(self, body: dict, hdr: dict) -> tuple[int, dict, dict]:
        return self._place(body, "BUY")

    def _kt10001(self, body: dict, hdr: dict) -> tuple[int, dict, dict]:
        return self._place(body, "SELL")

    def _change(self, body: dict, kind: str) -> tuple[int, dict, dict]:
        t = self.orders.get(body["orig_ord_no"])
        if t is None or not t.open:
            return 200, {"return_code": 20, "return_msg": "가상: 정정/취소 가능 수량 없음"}, {}
        left = t.qty - t.filled
        side_ko = "매수" if t.side == "BUY" else "매도"
        if kind == "CANCEL":
            c = SimOrder(self._new_no(), "CANCEL", t.side, t.code, left, Decimal("0"), t.market, orig=t.no, open=False)
            t.open = False
            self.orders[c.no] = c
            self._push00(c, "확인", f"{side_ko}취소")
            return 200, {"ord_no": c.no, "base_orig_ord_no": t.no, "cncl_qty": f"{int(left):012d}", "return_code": 0,
                         "return_msg": "취소 완료"}, {}
        q = Decimal(body["mdfy_qty"])
        if q > left:
            return 200, {"return_code": 20, "return_msg": "가상: 정정 수량 초과"}, {}
        m = SimOrder(self._new_no(), "MODIFY", t.side, t.code, q, Decimal(body["mdfy_uv"]), False, orig=t.no)
        t.open = False
        self.orders[m.no] = m
        self._push00(m, "확인", f"{side_ko}정정")
        return 200, {"ord_no": m.no, "base_orig_ord_no": t.no, "mdfy_qty": f"{int(q):012d}", "return_code": 0,
                     "return_msg": "정정 완료"}, {}

    def _kt10002(self, body: dict, hdr: dict) -> tuple[int, dict, dict]:
        return self._change(body, "MODIFY")

    def _kt10003(self, body: dict, hdr: dict) -> tuple[int, dict, dict]:
        return self._change(body, "CANCEL")

    def fill(self, no: str, qty: Decimal | None = None, price: Decimal | None = None) -> str:
        with self._lock:
            o = self.orders[no]
            q = (o.qty - o.filled) if qty is None else qty
            px = price if price is not None else (o.price if not o.market else Decimal("70000"))
            o.filled += q
            if o.filled == o.qty:
                o.open = False
            cntr = f"{next(self._cntr):07d}"
            self.fills.append((no, cntr, q, px, self.now().strftime("%H:%M:%S")))
            held, avg = self.positions.get(o.code, (Decimal("0"), Decimal("0")))
            sign = 1 if o.side == "BUY" else -1
            new = held + sign * q
            avg = ((held * avg + q * px) / new) if o.side == "BUY" and new else (avg if new else Decimal("0"))
            self.positions[o.code] = (new, avg)
            self.entr -= sign * q * px
            self._push00(o, "체결", "+매수" if o.side == "BUY" else "-매도",
                         {"909": cntr, "910": str(px), "911": str(o.filled), "914": str(px), "915": str(q),
                          "902": str(o.qty - o.filled)})
            return cntr

    def _io(self, o: SimOrder) -> str:
        side = "매수" if o.side == "BUY" else "매도"
        return {"NEW": f"현금{side}", "CANCEL": f"{side}취소", "MODIFY": f"{side}정정"}[o.kind]

    def _rows00009(self, only_fills: bool) -> list[dict]:
        rows: list[dict] = []
        for o in self.orders.values():
            base = {"stk_bond_tp": "1", "ord_no": o.no, "stk_cd": "A" + o.code,
                    "trde_tp": "시장가" if o.market else "보통", "io_tp_nm": self._io(o),
                    "ord_qty": f"{int(o.qty):010d}", "ord_uv": f"{int(o.price):010d}", "cnfm_qty": f"{0:010d}",
                    "acpt_tp": "거부" if o.rejected else "접수", "orig_ord_no": o.orig,
                    "mdfy_cncl_tp": {"NEW": "", "CANCEL": "취소", "MODIFY": "정정"}[o.kind],
                    "cntr_no": "", "cntr_qty": f"{0:010d}", "cntr_uv": f"{0:010d}", "cntr_tm": "", "dmst_stex_tp": "KRX"}
            fs = [f for f in self.fills if f[0] == o.no]
            if not fs and not only_fills:
                rows.append(base)
            for _, cntr, q, px, tm in fs:
                rows.append({**base, "cntr_no": cntr, "cntr_qty": f"{int(q):010d}", "cntr_uv": f"{int(px):010d}", "cntr_tm": tm})
        return rows + list(self.extra_rows)

    def _paged(self, rows: list[dict], key: str, hdr: dict, extra: dict | None = None) -> tuple[int, dict, dict]:
        start = int(hdr.get("next-key") or 0)
        page = rows[start:start + self.page_size]
        more = self.always_cont or start + self.page_size < len(rows)
        headers = {"cont-yn": "Y", "next-key": str(start + self.page_size)} if more else {"cont-yn": "N"}
        return 200, {**(extra or {}), key: page, "return_code": 0, "return_msg": "조회 완료"}, headers

    def _kt00009(self, body: dict, hdr: dict) -> tuple[int, dict, dict]:
        return self._paged(self._rows00009(body.get("qry_tp") == "1"), "acnt_ord_cntr_prst_array", hdr)

    def _ka10075(self, body: dict, hdr: dict) -> tuple[int, dict, dict]:
        rows = [{"acnt_no": ACCOUNT_NO, "ord_no": o.no, "stk_cd": o.code, "ord_stt": "접수",
                 "ord_qty": str(o.qty), "ord_pric": str(o.price), "oso_qty": str(o.qty - o.filled),
                 "orig_ord_no": o.orig, "io_tp_nm": ("+" if o.side == "BUY" else "-") + self._io(o)[-2:]
                 if o.kind == "NEW" else self._io(o), "trde_tp": "시장가" if o.market else "보통", "tm": "090000"}
                for o in self.orders.values() if o.open and o.kind != "CANCEL"]
        return self._paged(rows + list(self.extra_open), "oso", hdr)

    def _kt00018(self, body: dict, hdr: dict) -> tuple[int, dict, dict]:
        rows = [{"stk_cd": "A" + c, "stk_nm": "가상", "rmnd_qty": f"{int(q):015d}", "trde_able_qty": f"{int(q):015d}",
                 "pur_pric": f"{int(a):015d}"} for c, (q, a) in self.positions.items() if q]
        return self._paged(rows, "acnt_evlt_remn_indv_tot", hdr, {"tot_pur_amt": f"{0:015d}"})

    def _kt00005(self, body: dict, hdr: dict) -> tuple[int, dict, dict]:
        e = f"{int(self.entr):012d}"
        return 200, {"entr": e, "ord_alowa": e, "return_code": 0, "return_msg": "조회 완료"}, {}


# --- 시장 데이터 (T-021) — Sim에 붙이는 처리기 ---------------------------------------------------------

def _md_init(self: Sim) -> None:
    if not hasattr(self, "daily"):
        self.daily: dict[str, list[dict]] = {}       # 종목 → 일봉 행(최신 먼저)
        self.info: dict[str, dict] = {}              # 종목 → ka10001 본문
        self.master: dict[str, list[dict]] = {}      # mrkt_tp → 행
        self.vi_rows: list[dict] = []


def _ka10081(self: Sim, body: dict, hdr: dict) -> tuple[int, dict, dict]:
    _md_init(self)
    rows = self.daily.get(body["stk_cd"], [])
    return self._paged(rows, "stk_dt_pole_chart_qry", hdr, {"stk_cd": body["stk_cd"]})


def _ka10001(self: Sim, body: dict, hdr: dict) -> tuple[int, dict, dict]:
    _md_init(self)
    b = self.info.get(body["stk_cd"])
    if b is None:
        return 200, {"return_code": 1902, "return_msg": "종목 정보 없음"}, {}
    return 200, {**b, "stk_cd": body["stk_cd"], "return_code": 0, "return_msg": "ok"}, {}


def _ka10099(self: Sim, body: dict, hdr: dict) -> tuple[int, dict, dict]:
    _md_init(self)
    return self._paged(self.master.get(body["mrkt_tp"], []), "list", hdr)


def _ka10054(self: Sim, body: dict, hdr: dict) -> tuple[int, dict, dict]:
    _md_init(self)
    return self._paged(self.vi_rows, "motn_stk", hdr)


for _n, _f in (("_ka10081", _ka10081), ("_ka10001", _ka10001), ("_ka10099", _ka10099), ("_ka10054", _ka10054)):
    setattr(Sim, _n, _f)


def push_real(sim: Sim, rtype: str, item: str, values: dict) -> None:
    sim.ws_out.put(json.dumps({"trnm": "REAL", "data": [{"type": rtype, "item": item, "values": values}]},
                              ensure_ascii=False))
