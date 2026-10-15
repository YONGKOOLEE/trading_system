"""범용 WebSocket 연결 — 바이낸스 테스트넷 WS API 사용자 스트림용 (CP-036 D-205, T-048). 기존 의존성 websockets(17.1 고정) 재사용.

broker/binance 패키지는 네트워크 라이브러리를 import하지 않는다(전송은 주입). 이 모듈이 그 주입 대상이다(rest_transport와 같은 자리).
- 허용 호스트 집합(생성 시 지정) + wss + 기본 포트 + 사용자 정보 없음이 아니면 연결하기 전에 거부(ConnectionRefusedError 아님 —
  WsTargetError). 시험용 루프백(127.0.0.1·ws)은 `loopback_for_tests=True`일 때만.
- 프록시 환경 변수 무시(proxy=None), 압축 끔, 메시지 크기 상한 1 MiB [제안]. 자동 재연결 없음(재연결은 호출자 정책).
- ping/pong: websockets sync 클라이언트가 서버 ping에 자동으로 pong한다(루프백 가짜 서버 시험으로 확인 — 원격 동작은 T-050).
- 보내는 메시지 허용 목록(재검토, DP-69): WS API 요청은 JSON 객체의 `method`가 WS_API_METHODS(사용자 데이터 스트림
  구독·구독 해제·ping)일 때만 보낸다. 그 밖(order.place·order.cancel 등 주문 계열 포함)·JSON 아님·method 없음은
  **보내기 전에** WsMethodRefused. 이 전송은 주문 경로가 아니다 — 주문은 REST(RunGuard·주문 전송 스위치 D-204)로만.
  WS 제어 프레임 ping/pong은 라이브러리가 처리하므로 이 검사와 무관하다.
- 오류 문구에 URL 쿼리·헤더·메시지 원문을 넣지 않는다.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

OPEN_TIMEOUT_S = 10.0
MAX_MESSAGE_BYTES = 1 << 20
# 바이낸스 WS API 중 이 시스템이 보내는 메서드 — 사용자 데이터 스트림(서명 구독·해제)과 연결 확인만 (CP-036 D-205)
WS_API_METHODS = frozenset({"userDataStream.subscribe.signature", "userDataStream.unsubscribe", "ping"})


class WsTargetError(ValueError):
    """허용되지 않은 접속 대상 — 연결하지 않았다."""


class WsClosed(RuntimeError):
    """연결이 끊겼다(수신·송신 실패)."""


class WsMethodRefused(ValueError):
    """허용 목록 밖 WS API 요청 — 보내지 않았다."""


def check_ws_request(text: str) -> None:
    """보낼 WS API 요청이 허용 메서드인지 확인한다(아니면 WsMethodRefused). 문구에 메시지 원문을 넣지 않는다."""
    try:
        msg = json.loads(text)
    except (TypeError, ValueError):
        raise WsMethodRefused("WS API 요청이 JSON이 아니다 — 보내지 않는다") from None
    method = msg.get("method") if isinstance(msg, dict) else None
    if not isinstance(method, str) or method not in WS_API_METHODS:
        shown = method if isinstance(method, str) and len(method) <= 64 else "?"
        raise WsMethodRefused(f"WS API 메서드 {shown}: 허용 목록(사용자 데이터 스트림 구독·해제·ping) 밖 — 보내지 않는다")


class WsConnection:
    def __init__(self, conn: ClientConnection) -> None:
        self._c = conn

    def send(self, text: str) -> None:
        check_ws_request(text)
        try:
            self._c.send(text)
        except ConnectionClosed as exc:
            raise WsClosed(f"송신 실패: 연결 닫힘 ({exc.rcvd.code if exc.rcvd else '?'})") from None

    def recv(self, timeout: float | None = None) -> str:
        try:
            msg = self._c.recv(timeout=timeout)
        except TimeoutError:
            raise
        except ConnectionClosed as exc:
            raise WsClosed(f"수신 실패: 연결 닫힘 ({exc.rcvd.code if exc.rcvd else '?'})") from None
        return msg if isinstance(msg, str) else msg.decode("utf-8", errors="replace")

    def close(self) -> None:
        self._c.close()


class WsTransport:
    def __init__(self, allowed_hosts: frozenset[str], *, loopback_for_tests: bool = False,
                 open_timeout: float = OPEN_TIMEOUT_S) -> None:
        if not allowed_hosts:
            raise ValueError("허용 호스트가 없다")
        self._hosts = frozenset(allowed_hosts)
        self._loopback = loopback_for_tests
        self._timeout = open_timeout
        self.connects = 0

    def __repr__(self) -> str:
        return f"WsTransport(hosts={sorted(self._hosts)})"

    def check(self, url: str) -> None:
        p = urlsplit(url)
        if p.username or p.password or p.query or p.fragment:
            raise WsTargetError("WS 접속 대상 형식 아님(사용자 정보·쿼리 없음)")
        if self._loopback and p.scheme == "ws" and p.hostname == "127.0.0.1" and p.hostname in self._hosts:
            return
        if p.scheme != "wss" or p.hostname not in self._hosts or p.port is not None:
            raise WsTargetError("허용되지 않은 WS 접속 대상 — 연결하지 않는다")

    def connect(self, url: str) -> WsConnection:
        self.check(url)
        self.connects += 1
        conn = connect(url, legacy=True, open_timeout=self._timeout, max_size=MAX_MESSAGE_BYTES, compression=None,
                       proxy=None)   # 직접 연결 + 닫기는 호출자 책임 (websockets 17 legacy — kiwoom realtime과 같음)
        return WsConnection(conn)
