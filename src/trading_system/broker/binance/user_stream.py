"""바이낸스 사용자 데이터 스트림 — WS API `userDataStream.subscribe.signature` 연결 관리 (CP-036 D-205·D-215, T-048).

이 패키지는 네트워크 라이브러리를 import하지 않는다 — WS 연결은 주입(WsConnect: url → WsConn). 실제 연결은
broker/ws_transport(websockets 고정 버전)를 app·scripts가 주입한다. 시험은 메모리 가짜 연결·루프백 가짜 서버만.

흐름 (스레드 하나, run(stop))
1. 연결 → 서명 구독 요청 1건(signing.ws_signed_request: apiKey·timestamp·signature, 키 값은 repr·로그에 없음).
2. 응답 status 200 → 구독됨: 상태 ONLINE(처음) / RECONNECTED(그 뒤) — app이 K5 실시간 경로 정상 보고 + STREAM_RECONNECT
   대조 요청(D-109: 끊긴 사이 통보는 대조로 보완, 전달 보장 미확인 B-U8).
   응답이 200이 아님(서명·키 권한 거부 등) → REFUSED: app이 K5 + 긴급 알림. **같은 서명으로 계속 두드리지 않고 스레드를
   끝낸다** [DP-58] — 사람이 키를 확인한 뒤 재시작(PV-M2-06, A-063⑨ 키 유형 재결정 입력).
   응답 없음(시간 초과)·연결 실패 → OFFLINE, 대기 후 재연결.
3. 수신: executionReport → 어댑터 ingest_stream(큐에만 넣음 — 반영은 판정·실행 스레드, D-215). 해석 불가 메시지 →
   BAD_MESSAGE(경고, 원문 없음 — 반복은 시작 1건·30분마다 요약·정상 메시지 5분 뒤 회복 1건으로 묶는다, DP-67).
   serverShutdown·eventStreamTerminated → 교체 대상: 첫 통보는 바로 새 연결, 안정 구간(STABLE_MS 60초) 없이 연속되면
   재연결 대기(아래 5와 같은 1·2·4…30초)를 둔다 — 구독 직후 종료가 반복돼도 바로 다시 두드리지 않는다 [DP-68].
4. 끊김(수신 예외) → OFFLINE(app: K5 실시간 경로 끊김), 대기 후 재연결. 연결 23시간 경과(SessionPolicy, 24시간 유효 전)
   → 닫고 바로 새 연결(교체) → RECONNECTED → 대조.
5. 재연결 대기: 1·2·4·8·16·30초, 이후 30초 반복(03 D-050과 같은 계속 시도) [제안]. 성공하면 처음부터.
ping/pong: 서버 ping(20초 주기, 60초 안에 pong 없으면 끊음 — BN §4)은 WS 라이브러리가 자동 응답한다(websockets 17.1
sync 클라이언트 — 루프백 가짜 서버 시험으로 확인, tests/scenario/test_t048). 원격 서버 동작은 T-050 관찰(PV-M2-06).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Protocol

from .adapter import BinanceSpotAdapter
from .alert_throttle import ConditionAlerts
from .numbers import BinanceParseError
from .signing import Credentials, ws_signed_request
from .stream import ROTATE_AFTER_MS, ServerShutdown, SessionPolicy, StreamTerminated, parse_event

WS_API_HOST = "ws-api.testnet.binance.vision"
TESTNET_WS_API = f"wss://{WS_API_HOST}/ws-api/v3"
SUBSCRIBE = "userDataStream.subscribe.signature"
BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
REPLY_TIMEOUT_S = 10.0
STABLE_MS = 60_000      # 구독 뒤 이만큼 살아 있던 연결의 종료 통보는 "연속"으로 세지 않는다 [제안, DP-68]


class WsConn(Protocol):
    def send(self, text: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str: ...   # 시간 초과 = TimeoutError, 그 밖의 예외 = 끊김

    def close(self) -> None: ...


WsConnect = Callable[[str], WsConn]


class StreamState(StrEnum):
    ONLINE = "ONLINE"
    RECONNECTED = "RECONNECTED"
    OFFLINE = "OFFLINE"
    REFUSED = "REFUSED"
    BAD_MESSAGE = "BAD_MESSAGE"


class UserStream:
    def __init__(self, connect: WsConnect, credentials: Credentials, adapter: BinanceSpotAdapter, *,
                 now_ms: Callable[[], int], on_state: Callable[[StreamState, str], None],
                 url: str = TESTNET_WS_API, rotate_after_ms: int = ROTATE_AFTER_MS, recv_timeout_s: float = 1.0,
                 reply_timeout_s: float = REPLY_TIMEOUT_S, backoff_s: Sequence[float] = BACKOFF_S,
                 wait: Callable[[threading.Event, float], object] | None = None) -> None:
        self._connect = connect
        self._creds = credentials
        self._adapter = adapter
        self._now = now_ms
        self._on_state = on_state
        self._url = url
        self._rotate = rotate_after_ms
        self._recv_timeout = recv_timeout_s
        self._reply_timeout = reply_timeout_s
        self._backoff = tuple(backoff_s)
        self._wait = wait or (lambda stop, s: stop.wait(s))
        self._seq = 0
        self._terminated = False          # 이번 연결이 서버 종료 통보로 끝났는가
        self._subscribed_at: int | None = None
        self._bad = ConditionAlerts(lambda _k, m: self._state(StreamState.BAD_MESSAGE, m), lambda: self._now() / 1000)
        self.subscriptions = 0
        self.bad_messages = 0
        self.state: StreamState | None = None

    def __repr__(self) -> str:
        return f"UserStream(url={self._url}, subscriptions={self.subscriptions})"

    def _state(self, state: StreamState, message: str) -> None:
        if state is not StreamState.BAD_MESSAGE:
            self.state = state
        try:
            self._on_state(state, message)
        except Exception:  # noqa: BLE001 — 상태 전달 실패가 수신 스레드를 죽이지 않게
            pass

    def run(self, stop: threading.Event) -> None:
        failures = 0
        terminations = 0      # 안정 구간 없이 이어진 서버 종료 통보 수 (DP-68)
        while not stop.is_set():
            out = self._session(stop)
            if out in ("REFUSED", "STOPPED"):
                return
            if out == "TERMINATED":
                failures = 0
                lived = None if self._subscribed_at is None else self._now() - self._subscribed_at
                if lived is not None and lived >= STABLE_MS:
                    terminations = 0
                terminations += 1
                if terminations == 1:
                    continue                  # 첫 통보는 바로 교체 (V-286)
                self._wait(stop, self._backoff[min(terminations - 2, len(self._backoff) - 1)])
                continue
            if out == "ROTATE":
                failures = 0
                terminations = 0
                continue
            if out == "LOST":
                failures = 0
            wait = self._backoff[min(failures, len(self._backoff) - 1)]
            failures += 1
            self._wait(stop, wait)

    def _session(self, stop: threading.Event) -> str:
        try:
            conn = self._connect(self._url)
        except Exception as exc:  # noqa: BLE001 — 연결 실패는 종류만 남긴다
            self._state(StreamState.OFFLINE, f"연결 실패 ({type(exc).__name__})")
            return "FAILED"
        try:
            return self._subscribed(conn, stop)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — 닫기 오류는 무시
                pass

    def _subscribed(self, conn: WsConn, stop: threading.Event) -> str:
        self._seq += 1
        rid = f"m2-sub-{self._seq}"
        try:
            req = ws_signed_request(rid, SUBSCRIBE, {}, self._creds, timestamp_ms=self._now())
            conn.send(json.dumps(req.to_payload()))
            reply = self._reply(conn, rid, stop)
        except Exception as exc:  # noqa: BLE001
            self._state(StreamState.OFFLINE, f"구독 요청 실패 ({type(exc).__name__})")
            return "FAILED"
        if reply is None:
            self._state(StreamState.OFFLINE, "구독 응답 없음")
            return "FAILED"
        status = reply.get("status")
        if status != 200:
            err = reply.get("error") if isinstance(reply.get("error"), dict) else {}
            self._state(StreamState.REFUSED, f"구독 거부 (status {status}, 코드 {err.get('code')}) — 키·서명·권한 확인, "
                                             "자동 재시도 없음")
            return "REFUSED"
        self.subscriptions += 1
        self._subscribed_at = self._now()
        self._terminated = False
        self._state(StreamState.ONLINE if self.subscriptions == 1 else StreamState.RECONNECTED, "구독 성공")
        policy = SessionPolicy(self._now(), self._rotate)
        while not stop.is_set():
            if policy.should_rotate(self._now()):
                return "TERMINATED" if self._terminated else "ROTATE"
            try:
                text = conn.recv(timeout=self._recv_timeout)
            except TimeoutError:
                continue
            except Exception as exc:  # noqa: BLE001 — 끊김
                self._state(StreamState.OFFLINE, f"끊김 ({type(exc).__name__})")
                return "LOST"
            self._handle(text, policy)
        return "STOPPED"

    def _reply(self, conn: WsConn, rid: str, stop: threading.Event) -> dict | None:
        waited = 0.0
        while waited < self._reply_timeout and not stop.is_set():
            try:
                text = conn.recv(timeout=self._recv_timeout)
            except TimeoutError:
                waited += self._recv_timeout
                continue
            msg = _json(text)
            if isinstance(msg, dict) and msg.get("id") == rid:
                return msg
        return None

    def _handle(self, text: str, policy: SessionPolicy) -> None:
        msg = _json(text)
        if isinstance(msg, dict) and "id" in msg and "event" not in msg:
            return   # 다른 요청의 응답(없음) — 무시
        try:
            ev = parse_event(msg)
        except BinanceParseError as exc:
            self.bad_messages += 1
            self._bad.fail("BAD_MESSAGE", StreamState.BAD_MESSAGE.value, f"해석 불가 메시지 ({exc})")
            return
        self._bad.ok("BAD_MESSAGE", "해석 불가 메시지")
        policy.note(ev)
        if isinstance(ev, (ServerShutdown, StreamTerminated)):
            self._terminated = True
            return
        self._adapter.ingest_stream(ev)


def _json(text: object) -> object:
    try:
        return json.loads(text) if isinstance(text, (str, bytes)) else None
    except ValueError:
        return None
