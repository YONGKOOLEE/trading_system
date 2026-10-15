"""run(엔진 실행)용 전송 경계 — 테스트넷 REST만, 주문 전송 스위치가 꺼지면 주문 계열을 보내기 전에 막는다 (CP-036 D-204, T-047).

PreflightGuard(시작 점검, 조회만)와 같은 자리의 run판이다. 전송(Transport) 직전 마지막 경계로, 어댑터의 스위치 검사와 별개로
한 번 더 막는다(이중 차단).
- 접속 대상: https + 테스트넷 REST 호스트(preflight.TESTNET_HOST)만. 포트·사용자 정보 없음. 실전·Demo·평문·루프백 거부.
- 주문 변경: POST·DELETE /api/v3/order 두 가지만 다룬다. `orders_enabled`가 False면 **보내지 않고** PreSendError
  (클라이언트는 '전송 전 실패'로 본다 — 결과 불명 아님). True여도 그 밖의 주문 계열(order/test·OCO·orderList·cancelReplace·
  amend·SOR·미체결 일괄 취소·사용자 스트림 키 발급 등)은 쓰지 않으므로 항상 막는다.
- 조회: GET 허용 목록(시작 점검 7개 + 단건 주문 조회 GET /api/v3/order + 시세 경로 market_paths)만.
- 기록: 메서드·경로·HTTP 상태만(쿼리 문자열·서명·헤더·응답 원문 없음). run은 며칠씩 돌므로 호출마다 쌓지 않는다
  (재검토: 메모리) — (메서드, 경로)별 건수 `calls`·전체 `sent` + 최근 RECENT_MAX건 `recent`(오래된 것부터 버림).
  `blocked`(전송 전 차단)도 최근 RECENT_MAX건만 — 차단은 설계상 일어나지 않아야 하므로 건수 `blocked_total`을 함께 둔다.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from .client import AfterSendError, PreSendError, RawReply, Transport
from .preflight import QUERY_PATHS, TESTNET_HOST

ORDER_PATH = "/api/v3/order"
ORDER_CHANGE = frozenset({("POST", ORDER_PATH), ("DELETE", ORDER_PATH)})
RUN_QUERY_PATHS = QUERY_PATHS | {ORDER_PATH}
# 시세·규칙 공급(D-206, T-048) — 서명 없는 공개 조회
MARKET_PATHS = frozenset({"/api/v3/ticker/price", "/api/v3/klines", "/api/v3/avgPrice", "/api/v3/referencePrice"})
RECENT_MAX = 200


@dataclass(frozen=True)
class GuardRecord:
    method: str
    path: str
    status: int | None


class RunGuard:
    def __init__(self, inner: Transport, *, orders_enabled: bool,
                 market_paths: frozenset[str] = MARKET_PATHS) -> None:
        self._inner = inner
        self._orders = bool(orders_enabled)
        self._get = RUN_QUERY_PATHS | market_paths
        self.calls: Counter[tuple[str, str]] = Counter()
        self.recent: deque[GuardRecord] = deque(maxlen=RECENT_MAX)
        self.sent = 0
        self.blocked: list[str] = []          # 최근 RECENT_MAX건 (목록 — 기존 검사·보고 형식 유지)
        self.blocked_total = 0
        self.order_sent = 0

    def __repr__(self) -> str:
        return f"RunGuard(orders_enabled={self._orders}, sent={self.sent}, blocked={self.blocked_total})"

    @property
    def orders_enabled(self) -> bool:
        return self._orders

    def send(self, method: str, url: str, headers: Mapping[str, str]) -> RawReply:
        p = urlsplit(url)
        m = method.upper()
        if p.scheme != "https" or p.hostname != TESTNET_HOST or p.port is not None or p.username or p.password:
            self._block(f"{m} {p.hostname or '?'}{p.path}")
            raise PreSendError("허용되지 않은 접속 대상(테스트넷 REST만) — 보내지 않는다")
        if (m, p.path) in ORDER_CHANGE:
            if not self._orders:
                self._block(f"{m} {p.path}")
                raise PreSendError(f"{m} {p.path}: 주문 전송 스위치 꺼짐(D-204) — 보내지 않는다")
            self.order_sent += 1
        elif m != "GET" or p.path not in self._get:
            self._block(f"{m} {p.path}")
            raise PreSendError(f"{m} {p.path}: run 허용 목록 밖 — 보내지 않는다")
        try:
            r = self._inner.send(method, url, headers)
        except (PreSendError, AfterSendError):
            self._record(GuardRecord(m, p.path, None))
            raise
        self._record(GuardRecord(m, p.path, int(r.status)))
        return RawReply(int(r.status), dict(r.headers), str(r.text))

    def _record(self, rec: GuardRecord) -> None:
        self.sent += 1
        self.calls[(rec.method, rec.path)] += 1
        self.recent.append(rec)

    def _block(self, what: str) -> None:
        self.blocked_total += 1
        self.blocked.append(what)
        del self.blocked[:-RECENT_MAX]
