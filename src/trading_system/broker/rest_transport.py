"""범용 HTTPS 전송 — 바이낸스 테스트넷 조회용 (T-M2-14, CP-024 D-114 "기존 의존성 requests 재사용", 03 §5.3).

broker/binance 패키지는 네트워크 라이브러리를 import하지 않는다(전송은 주입). 이 모듈이 그 주입 대상이다.
- 허용 호스트 집합(생성 시 지정)·https·기본 포트·사용자 정보 없음이 아니면 세션을 부르기 전에 '전송 전 실패'.
- 자동 재시도 0(HTTPAdapter max_retries=0), 리다이렉트 따라가지 않음, 환경 변수 프록시·인증서 설정 무시(trust_env=False) —
  키움 http.py와 같은 방식(03 D-044).
- 전송 단계 구분: 연결 수립 전 실패(연결 타임아웃·연결 거부·이름 해석 실패) = pre_send_error, 그 밖의 전송 중 오류(읽기
  타임아웃·끊김·TLS) = after_send_error. 예외 형식은 주입한다(바이낸스 클라이언트의 PreSendError·AfterSendError).
- 오류 문구에는 메서드·경로만. 쿼리 문자열(서명)·헤더(API 키)·응답 원문은 넣지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

from .kiwoom.http import CONNECT_TIMEOUT, READ_TIMEOUT, _is_connect_failure


class RestPreSendError(RuntimeError):
    """연결 수립 전 실패(기본 형식)."""


class RestAfterSendError(RuntimeError):
    """전송 중 오류(기본 형식)."""


@dataclass(frozen=True)
class RestReply:
    status: int
    headers: dict[str, str]
    text: str


class RestTransport:
    def __init__(self, allowed_hosts: frozenset[str], *,
                 pre_send_error: type[Exception] = RestPreSendError,
                 after_send_error: type[Exception] = RestAfterSendError,
                 connect_timeout: float = CONNECT_TIMEOUT, read_timeout: float = READ_TIMEOUT) -> None:
        if not allowed_hosts:
            raise ValueError("허용 호스트가 없다")
        self._hosts = frozenset(allowed_hosts)
        self._pre = pre_send_error
        self._after = after_send_error
        self._timeout = (connect_timeout, read_timeout)
        self._session = requests.Session()
        self._session.trust_env = False
        for scheme in ("http://", "https://"):
            self._session.mount(scheme, HTTPAdapter(max_retries=0))
        self.calls = 0   # 실제로 세션에 넘긴 요청 수

    def __repr__(self) -> str:
        return f"RestTransport(hosts={sorted(self._hosts)})"

    def send(self, method: str, url: str, headers: Mapping[str, str]) -> RestReply:
        p = urlsplit(url)
        where = f"{method} {p.path}"
        if p.scheme != "https" or p.hostname not in self._hosts or p.port is not None or p.username or p.password:
            raise self._pre(f"{where}: 허용되지 않은 접속 대상 — 보내지 않는다")
        self.calls += 1
        try:
            r = self._session.request(method, url, headers=dict(headers), timeout=self._timeout,
                                      allow_redirects=False)
        except requests.ConnectTimeout:
            raise self._pre(f"{where}: 연결 타임아웃") from None
        except requests.ConnectionError as exc:
            if isinstance(exc, requests.exceptions.SSLError) or not _is_connect_failure(exc):
                raise self._after(f"{where}: 전송 중 연결 오류 ({type(exc).__name__})") from None
            raise self._pre(f"{where}: 연결 실패") from None
        except requests.RequestException as exc:
            raise self._after(f"{where}: 전송 중 오류 ({type(exc).__name__})") from None
        return RestReply(int(r.status_code), {str(k): str(v) for k, v in r.headers.items()}, r.text)
