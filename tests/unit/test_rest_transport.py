"""범용 HTTPS 전송 (T-M2-14 바이낸스 테스트넷 조회용, broker/rest_transport). 네트워크 없음 — requests 세션을 가로챈다.

- 허용 호스트·https·기본 포트가 아니면 세션을 부르기 전에 '전송 전 실패'.
- 연결 수립 전 실패 = 주입한 pre 예외, 그 밖의 전송 오류 = after 예외. 자동 재시도·리다이렉트 없음.
- 오류 문구에 쿼리 문자열(서명)·헤더(API 키)를 넣지 않는다.
"""

from __future__ import annotations

import pytest
import requests
from urllib3.exceptions import NewConnectionError

from trading_system.broker import rest_transport as rt

HOST = "testnet.binance.vision"
SIG = "signature=deadbeefcafe"


class Pre(RuntimeError):
    pass


class After(RuntimeError):
    pass


class _Resp:
    status_code = 200
    headers = {"X-MBX-USED-WEIGHT-1M": "3", "Content-Type": "application/json"}
    text = '{"serverTime": 1}'


def _transport(monkeypatch: pytest.MonkeyPatch, outcome: object) -> tuple[rt.RestTransport, list[dict]]:
    t = rt.RestTransport(frozenset({HOST}), pre_send_error=Pre, after_send_error=After)
    seen: list[dict] = []

    def fake_request(method: str, url: str, **kw: object) -> object:
        seen.append({"method": method, "url": url, **kw})
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    monkeypatch.setattr(t._session, "request", fake_request)   # noqa: SLF001 — 시험용 가로채기
    return t, seen


def test_reply_and_options(monkeypatch: pytest.MonkeyPatch) -> None:
    t, seen = _transport(monkeypatch, _Resp())
    r = t.send("GET", f"https://{HOST}/api/v3/time", {"X-MBX-APIKEY": "k"})
    assert (r.status, r.text, r.headers["X-MBX-USED-WEIGHT-1M"]) == (200, '{"serverTime": 1}', "3")
    assert seen[0]["allow_redirects"] is False and seen[0]["timeout"] == (rt.CONNECT_TIMEOUT, rt.READ_TIMEOUT)
    assert t.calls == 1 and t._session.trust_env is False   # noqa: SLF001


@pytest.mark.parametrize("url", ["https://api.binance.com/api/v3/time", "https://demo-api.binance.com/api/v3/time",
                                 f"http://{HOST}/api/v3/time", f"https://{HOST}:8443/api/v3/time",
                                 f"https://u:p@{HOST}/api/v3/time", "https://127.0.0.1/api/v3/time"])
def test_refuses_before_session(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    t, seen = _transport(monkeypatch, _Resp())
    with pytest.raises(Pre, match="접속 대상"):
        t.send("GET", url, {})
    assert seen == [] and t.calls == 0


def test_constructor_requires_hosts() -> None:
    with pytest.raises(ValueError):
        rt.RestTransport(frozenset())


@pytest.mark.parametrize(("exc", "cls"), [
    (requests.ConnectTimeout("t"), Pre),
    (requests.ConnectionError(NewConnectionError(None, "refused")), Pre),   # type: ignore[arg-type]
    (requests.ConnectionError("reset by peer"), After),
    (requests.ReadTimeout("t"), After),
    (requests.exceptions.SSLError("bad"), After),
])
def test_error_mapping_without_query(monkeypatch: pytest.MonkeyPatch, exc: Exception, cls: type) -> None:
    t, _ = _transport(monkeypatch, exc)
    with pytest.raises(cls) as e:
        t.send("GET", f"https://{HOST}/api/v3/account?{SIG}", {"X-MBX-APIKEY": "secret-key-value"})
    assert SIG not in str(e.value) and "secret-key-value" not in str(e.value)
    assert "/api/v3/account" in str(e.value)


def test_default_error_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    t = rt.RestTransport(frozenset({HOST}))
    with pytest.raises(rt.RestPreSendError):
        t.send("GET", "https://api.binance.com/x", {})
