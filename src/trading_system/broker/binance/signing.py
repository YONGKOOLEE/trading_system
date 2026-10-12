"""HMAC-SHA256 서명과 자격정보 (CP-024 D-114, BN §3, V-158).

- 키 유형: 테스트넷 HMAC [결정 A-063⑨]. 표준 라이브러리 hmac·hashlib만 쓴다.
- REST 서명: 보내는 쿼리 문자열(percent-encode 적용 후) 그대로를 서명한다. 2026-01-15부터 서명 전 percent-encode
  필수(BN §3, S8). `timestamp`(ms)·`recvWindow`(기본 5000, 최대 60000)를 붙이고 마지막에 `signature`를 붙인다.
- WS API 서명(`userDataStream.subscribe.signature`): signature 외 파라미터를 이름순 정렬해 `key=value&...`로 서명한다.
  [추정] WS API 서명 payload의 percent-encode 적용 범위는 원문 대조 안 함 — 이 시스템의 WS 파라미터(apiKey·timestamp)는
  인코딩해도 바뀌지 않는 ASCII라 영향 없음. HMAC 키로 이 메서드가 동작하는지는 B-U9(PV-M2-06).
- 비밀값은 `reveal()`이 있는 Secret 형식으로만 받는다(config.envfile.Secret과 같은 모양, 의존 규칙상 import하지 않음).
  repr·str·오류 메시지·경보 문구에 키·시크릿·서명을 넣지 않는다.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from typing import Protocol
from urllib.parse import quote

DEFAULT_RECV_WINDOW_MS = 5000
MAX_RECV_WINDOW_MS = 60000
_RESERVED = frozenset({"signature", "timestamp", "recvWindow"})


class SecretLike(Protocol):
    def reveal(self) -> str: ...


class Credentials:
    __slots__ = ("_key", "_secret")

    def __init__(self, api_key: SecretLike, secret: SecretLike) -> None:
        for v in (api_key, secret):
            if isinstance(v, str) or not callable(getattr(v, "reveal", None)):
                raise TypeError("자격정보는 reveal()이 있는 Secret 형식으로만 받는다")
        self._key = api_key
        self._secret = secret

    def _reveal(self, which: SecretLike, name: str) -> str:
        value = which.reveal()
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name}가 비어 있다")
        return value

    def api_key_header(self) -> dict[str, str]:
        return {"X-MBX-APIKEY": self._reveal(self._key, "API 키")}

    def api_key(self) -> str:
        return self._reveal(self._key, "API 키")

    def sign(self, payload: str) -> str:
        return hmac_hex(self._reveal(self._secret, "시크릿"), payload)

    def __repr__(self) -> str:
        return "Credentials(***)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return format(repr(self), spec)


def hmac_hex(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def encode(params: Sequence[tuple[str, str]]) -> str:
    """percent-encode(RFC 3986 비예약 문자 `A-Za-z0-9-_.~`만 그대로)한 쿼리 문자열. 순서는 주어진 대로."""
    out = []
    for k, v in params:
        if not isinstance(k, str) or not isinstance(v, str):
            raise TypeError("파라미터 이름·값은 문자열만 (float 등은 지수 표기 위험)")
        out.append(f"{quote(k, safe='')}={quote(v, safe='')}")
    return "&".join(out)


def _check_recv_window(recv_window_ms: int) -> None:
    if isinstance(recv_window_ms, bool) or not 1 <= recv_window_ms <= MAX_RECV_WINDOW_MS:
        raise ValueError(f"recvWindow는 1~{MAX_RECV_WINDOW_MS}ms")


def signed_query(params: Sequence[tuple[str, str]], creds: Credentials, *, timestamp_ms: int,
                 recv_window_ms: int = DEFAULT_RECV_WINDOW_MS) -> str:
    """서명된 쿼리 문자열 `...&recvWindow=..&timestamp=..&signature=..`. 반환값에는 서명이 있으므로 로그 금지."""
    _check_recv_window(recv_window_ms)
    if any(k in _RESERVED for k, _ in params):
        raise ValueError("signature·timestamp·recvWindow는 서명 단계에서만 붙인다")
    full = [*params, ("recvWindow", str(recv_window_ms)), ("timestamp", str(int(timestamp_ms)))]
    payload = encode(full)
    return f"{payload}&signature={creds.sign(payload)}"


class SignedWsRequest:
    """WS API 서명 요청. 내용에 API 키·서명이 있으므로 repr은 메서드 이름만 보인다."""

    __slots__ = ("_payload",)

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def to_payload(self) -> dict[str, object]:
        return {**self._payload, "params": dict(self._payload["params"])}  # type: ignore[arg-type]

    @property
    def method(self) -> str:
        return str(self._payload["method"])

    def __repr__(self) -> str:
        return f"SignedWsRequest(method={self.method}, params=***)"

    __str__ = __repr__


def ws_signed_request(request_id: str, method: str, params: Mapping[str, str | int], creds: Credentials, *,
                      timestamp_ms: int, recv_window_ms: int | None = None) -> SignedWsRequest:
    if any(k in _RESERVED or k == "apiKey" for k in params):
        raise ValueError("apiKey·signature·timestamp·recvWindow는 서명 단계에서만 붙인다")
    full: dict[str, str | int] = {**params, "apiKey": creds.api_key(), "timestamp": int(timestamp_ms)}
    if recv_window_ms is not None:
        _check_recv_window(recv_window_ms)
        full["recvWindow"] = recv_window_ms
    payload = encode([(k, str(full[k])) for k in sorted(full)])
    full["signature"] = creds.sign(payload)
    return SignedWsRequest({"id": request_id, "method": method, "params": full})
