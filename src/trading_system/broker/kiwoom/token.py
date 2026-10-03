"""접근토큰 관리 (03 D-049, kiwoom-kr §1). 토큰은 메모리에만 둔다. 디스크·로그·오류 메시지에 남기지 않는다.

- 발급 au10001 `POST /oauth2/token`(grant_type·appkey·secretkey) → token·expires_dt(KST).
- 만료 10분 전부터 다음 요청 때 새로 발급한다. 인증 오류 응답을 받으면 무효로 표시해 다음 요청부터 재발급
  (같은 요청은 다시 보내지 않는다 — 주문 경로, 03 §4 E-13).
- 발급 시도 최대 3회(조회 재시도 정책, 03 §5.3). 연속 실패가 기준에 닿으면 치명 알림(K5 판단은 호출자).
- 발급도 호출 제한기(TR au10001)를 거치고, 시도 사이에 1·2초 대기한다. 발급 전체 시간 예산(기본 20초)을 넘길
  시도는 시작하지 않는다 — 잠금을 쥔 채 최악 약 27초(시도 13초 × 2 + 대기 1초)를 넘지 않는다 (RV-19, D-048).
- 폐기 au10002 `POST /oauth2/revoke`(appkey·secretkey·token)는 종료 시 1회.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from trading_system.domain.clock import Clock

from . import parse
from trading_system.broker.ratelimit import AcquireTimeout, RateLimiter

from .errors import ErrorClass, classify, normalize_code, specific_code
from .http import AfterSendError, KiwoomHttp, PreSendError

REFRESH_BEFORE = timedelta(minutes=10)
ISSUE_ATTEMPTS = 3
ISSUE_BACKOFF = (1.0, 2.0)   # 시도 사이 대기(초) — K-U10 보수값과 같은 지수 대기
ISSUE_BUDGET = 20.0          # 발급 전체 시간 예산(초)


class TokenError(RuntimeError):
    def __init__(self, message: str, error_class: ErrorClass | None = None) -> None:
        super().__init__(message)
        self.error_class = error_class


class TokenManager:
    def __init__(self, http: KiwoomHttp, app_key: Callable[[], str], secret_key: Callable[[], str], clock: Clock,
                 on_critical: Callable[[str, str], None] | None = None, *,
                 limiter: RateLimiter | None = None, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic, issue_budget: float = ISSUE_BUDGET) -> None:
        self._http = http
        self._limiter = limiter
        self._sleep = sleep
        self._mono = monotonic
        self._budget = issue_budget
        self._app_key = app_key
        self._secret = secret_key
        self._clock = clock
        self._critical = on_critical or (lambda kind, msg: None)
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires: datetime | None = None
        self.issued = 0
        self.consecutive_failures = 0

    def token(self) -> str:
        """유효한 토큰. 없거나 만료 임박이면 발급한다. 실패하면 TokenError (요청은 보내지 않은 상태)."""
        with self._lock:
            if self._token is not None and self._expires is not None and \
                    self._clock.now() < self._expires - REFRESH_BEFORE:
                return self._token
            last: TokenError | None = None
            start = self._mono()
            for attempt in range(ISSUE_ATTEMPTS):
                if attempt:
                    pause = ISSUE_BACKOFF[min(attempt - 1, len(ISSUE_BACKOFF) - 1)]
                    if self._mono() - start + pause > self._budget:
                        break  # 시간 예산 초과 — 더 시도하지 않는다 (RV-19)
                    self._sleep(pause)
                try:
                    self._acquire(start)
                    self._issue()
                    self.consecutive_failures = 0
                    assert self._token is not None
                    return self._token
                except TokenError as exc:
                    last = exc
                    self.consecutive_failures += 1
                    if exc.error_class in (ErrorClass.MODE_MISMATCH, ErrorClass.TERMINAL):
                        break  # 다시 해도 같은 결과: 바로 치명 처리
            kind = last.error_class.value if last and last.error_class else "AUTH"
            self._critical(kind, f"토큰 발급 실패 {self.consecutive_failures}회: {last}")
            raise last or TokenError("토큰 발급 실패")

    def _acquire(self, start: float) -> None:
        """발급도 호출 제한기를 거친다 (TR au10001, RV-19). 대기는 남은 예산까지."""
        if self._limiter is None:
            return
        left = self._budget - (self._mono() - start)
        try:
            self._limiter.acquire("au10001", max(left, 0.0))
        except AcquireTimeout as exc:
            raise TokenError(f"au10001 호출 제한 대기 초과: {exc}") from None

    def _issue(self) -> None:
        try:
            r = self._http.post("/oauth2/token", "au10001", {
                "grant_type": "client_credentials", "appkey": self._app_key(), "secretkey": self._secret()}, None)
        except (PreSendError, AfterSendError) as exc:
            raise TokenError(f"au10001 전송 실패: {exc}") from None
        if r.body is None:
            raise TokenError(f"au10001 응답 해석 불가 (HTTP {r.status})")
        code = specific_code(normalize_code(r.body.get("return_code")), r.body.get("return_msg"))
        cls = classify(code if code is not None else 0) if r.status < 400 else classify(code)
        if cls is not ErrorClass.OK:
            raise TokenError(f"au10001 거부 (HTTP {r.status}, 코드 {code})", cls)
        tok = r.body.get("token")
        if not isinstance(tok, str) or not tok:
            raise TokenError("au10001 응답에 토큰 없음")
        try:
            exp = parse.expires(r.body.get("expires_dt"))
        except parse.KiwoomParseError as exc:
            raise TokenError(f"au10001 {exc}") from None
        if exp <= self._clock.now() + REFRESH_BEFORE:
            raise TokenError("au10001 만료 시각이 너무 가까움")
        self._token, self._expires = tok, exp
        self.issued += 1

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires = None

    def revoke(self) -> bool:
        """종료 시 폐기. 실패해도 예외를 올리지 않는다(만료로 정리됨)."""
        with self._lock:
            tok, self._token, self._expires = self._token, None, None
        if tok is None:
            return True
        try:
            r = self._http.post("/oauth2/revoke", "au10002",
                                {"appkey": self._app_key(), "secretkey": self._secret(), "token": tok}, None)
        except (PreSendError, AfterSendError):
            return False
        return r.body is not None and normalize_code(r.body.get("return_code")) in (0, None) and r.status < 400

    @property
    def expires_at(self) -> datetime | None:
        return self._expires
