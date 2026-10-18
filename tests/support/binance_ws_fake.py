"""가짜 바이낸스 WS API (CP-036 T-048 시험용, 네트워크 없음).

- FakeWsApi: 메모리 연결 공장(connect(url) → 연결). 서명 구독 요청에 응답하고, 합성 거래소(ExchangeSim)의 사용자 스트림
  이벤트를 `{"subscriptionId": n, "event": {...}}`로 흘린다. 끊김·구독 거부·serverShutdown(구독 직후 포함)·연결 실패를 주입한다.
- LoopbackWsServer: websockets(고정 버전) 동기 서버를 127.0.0.1 임의 포트에 띄운다 — 실제 WS 프레임·ping/pong 확인용.
  외부 연결 없음.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable
from urllib.parse import urlsplit

from tests.support.binance_exchange_sim import ExchangeSim


class FakeConn:
    def __init__(self, api: FakeWsApi, url: str) -> None:
        self.api = api
        self.url = url
        self.inbox: queue.Queue[str | Exception] = queue.Queue()
        self.sent: list[dict] = []
        self.closed = False
        self.subscribed = False

    def send(self, text: str) -> None:
        if self.closed:
            raise ConnectionResetError("closed")
        msg = json.loads(text)
        self.sent.append(msg)
        if msg.get("method") == "userDataStream.subscribe.signature":
            if self.api.refuse:
                self.inbox.put(json.dumps({"id": msg["id"], "status": 401,
                                           "error": {"code": -1022, "msg": "Signature for this request is not valid."}}))
            elif not self.api.silent:
                self.subscribed = True
                self.inbox.put(json.dumps({"id": msg["id"], "status": 200, "result": {"subscriptionId": 0}}))
                if self.api.terminate_on_subscribe:   # 구독 직후 서버 종료 통보 (재검토: 연속 종료 백오프, DP-68)
                    self.inbox.put(json.dumps({"subscriptionId": 0, "event": {"e": "serverShutdown", "E": 0}}))

    def recv(self, timeout: float | None = None) -> str:
        if self.closed:
            raise ConnectionResetError("closed")
        if self.subscribed:
            self.api.pump(self)
        try:
            item = self.inbox.get(timeout=timeout if timeout is not None else 5)
        except queue.Empty:
            raise TimeoutError from None
        if isinstance(item, Exception):
            self.closed = True
            raise item
        return item

    def close(self) -> None:
        self.closed = True

    def drop(self) -> None:
        self.inbox.put(ConnectionResetError("server dropped"))

    def push(self, payload: object) -> None:
        self.inbox.put(payload if isinstance(payload, str) else json.dumps(payload))


class FakeWsApi:
    __test__ = False

    def __init__(self, sim: ExchangeSim | None = None) -> None:
        self.sim = sim
        self.conns: list[FakeConn] = []
        self.refuse = False
        self.silent = False
        self.fail_connects = 0
        self.terminate_on_subscribe = False
        self._lock = threading.Lock()
        self._cursor = 0

    def connect(self, url: str) -> FakeConn:
        assert urlsplit(url).hostname == "ws-api.testnet.binance.vision", url
        if self.fail_connects > 0:
            self.fail_connects -= 1
            raise ConnectionRefusedError("fake refused")
        c = FakeConn(self, url)
        self.conns.append(c)
        return c

    def pump(self, conn: FakeConn) -> None:
        """합성 거래소에 새로 생긴 사용자 스트림 이벤트를 현재 연결로 (끊긴 사이 이벤트는 유실 — 전달 보장 없음 B-U8)."""
        if self.sim is None:
            return
        with self._lock:
            new = self.sim.events[self._cursor:]
            self._cursor = len(self.sim.events)
        for ev in new:
            conn.push(ev)

    def skip_pending(self) -> None:
        """끊긴 사이 이벤트 유실 흉내."""
        with self._lock:
            self._cursor = len(self.sim.events) if self.sim else 0

    @property
    def current(self) -> FakeConn:
        return self.conns[-1]


def wait_until(cond: Callable[[], bool], timeout: float = 5.0) -> bool:
    ev = threading.Event()
    step = 0.01
    waited = 0.0
    while waited < timeout:
        if cond():
            return True
        ev.wait(step)
        waited += step
    return cond()
