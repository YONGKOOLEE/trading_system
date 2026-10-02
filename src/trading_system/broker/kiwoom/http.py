"""키움 REST 얇은 클라이언트 (03 D-044). 자동 재시도·재전송 없음. 토큰·앱키는 오류 메시지에 넣지 않는다.

전송 단계 구분 (03 §5.2 4분류의 입력):
- PreSendError: 연결 수립 전 실패(연결 거부·이름 해석 실패·연결 타임아웃). 요청 바이트가 나가지 않았다.
- AfterSendError: 그 밖의 전송 중 오류(읽기 타임아웃, 연결 끊김, TLS 오류 등). 서버가 받았는지 모른다.
응답이 오면 HttpReply(상태 코드, 본문(JSON 해석 결과 또는 None), 연속조회 헤더)를 돌려준다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import NewConnectionError

CONNECT_TIMEOUT, READ_TIMEOUT = 3.0, 10.0   # 03 §5.3


class PreSendError(RuntimeError):
    pass


class AfterSendError(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpReply:
    status: int
    body: dict | None          # JSON 객체가 아니면 None (해석 불가)
    cont_yn: str | None = None
    next_key: str | None = None


class KiwoomHttp:
    def __init__(self, base_url: str, connect_timeout: float = CONNECT_TIMEOUT,
                 read_timeout: float = READ_TIMEOUT) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = (connect_timeout, read_timeout)
        self._session = requests.Session()
        self._session.trust_env = False  # 환경 변수 프록시·인증서 설정을 따르지 않는다(예측 가능한 경로)
        for scheme in ("http://", "https://"):
            self._session.mount(scheme, HTTPAdapter(max_retries=0))
        self.calls = 0  # 실제로 보낸 요청 수 (재전송 0 검증용, V-50)

    def post(self, path: str, api_id: str, body: dict, token: str | None,
             cont_yn: str | None = None, next_key: str | None = None) -> HttpReply:
        headers = {"Content-Type": "application/json;charset=UTF-8", "api-id": api_id}
        if token is not None:
            headers["authorization"] = f"Bearer {token}"
        if cont_yn:
            headers["cont-yn"] = cont_yn
        if next_key:
            headers["next-key"] = next_key
        self.calls += 1
        try:
            r = self._session.post(self._base + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                   headers=headers, timeout=self._timeout, allow_redirects=False)
        except requests.ConnectTimeout:
            raise PreSendError(f"{api_id}: 연결 타임아웃") from None
        except requests.ConnectionError as exc:
            if _is_connect_failure(exc):
                raise PreSendError(f"{api_id}: 연결 실패") from None
            raise AfterSendError(f"{api_id}: 전송 중 연결 오류 ({type(exc).__name__})") from None
        except requests.RequestException as exc:
            raise AfterSendError(f"{api_id}: 전송 중 오류 ({type(exc).__name__})") from None
        try:
            parsed = r.json()
        except ValueError:
            parsed = None
        return HttpReply(r.status_code, parsed if isinstance(parsed, dict) else None,
                         r.headers.get("cont-yn"), r.headers.get("next-key"))


def _is_connect_failure(exc: BaseException) -> bool:
    """연결 수립 전 실패인지 (urllib3 NewConnectionError — 이름 해석 실패 포함)."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, NewConnectionError):
            return True
        reason = getattr(cur, "reason", None)
        if isinstance(reason, BaseException):
            cur = reason
            continue
        nxt = cur.args[0] if cur.args and isinstance(cur.args[0], BaseException) else None
        cur = nxt or cur.__cause__ or cur.__context__
    return False
