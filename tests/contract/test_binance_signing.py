"""HMAC-SHA256 서명과 비밀값 취급 (CP-024 D-114, V-158, BN §3).

시험 벡터: 바이낸스 공식 REST 문서 "SIGNED Endpoint examples"의 HMAC 예시(공개 예시 키, 실제 계정 키 아님).
네트워크 없음. 실제 자격정보 없음.
"""

from __future__ import annotations

import pytest

from trading_system.broker.binance import signing as s

# 공식 문서 예시 값 (공개 문서에 실린 설명용 값, 실제 키 아님)
DOC_API_KEY = "vmPUZE6mv9SD5VNHk4HlWFsOr6aKE2zvsw0MuIgwCIPy6utIco14y7Ju91duEh8A"
DOC_SECRET = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
DOC_SIGNATURE = "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"


class _Secret:
    """config.envfile.Secret과 같은 모양(reveal만 공개)."""

    def __init__(self, v: str) -> None:
        self._v = v

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "Secret(***)"


def _creds() -> s.Credentials:
    return s.Credentials(_Secret(DOC_API_KEY), _Secret(DOC_SECRET))


def test_signature_matches_official_example() -> None:
    q = s.signed_query(
        [("symbol", "LTCBTC"), ("side", "BUY"), ("type", "LIMIT"), ("timeInForce", "GTC"),
         ("quantity", "1"), ("price", "0.1")],
        _creds(), timestamp_ms=1499827319559, recv_window_ms=5000)
    assert q == ("symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1"
                 "&recvWindow=5000&timestamp=1499827319559&signature=" + DOC_SIGNATURE)


def test_percent_encoding_is_signed_as_sent() -> None:
    # 2026-01-15부터 서명 전 payload percent-encode 필수 (BN §3, S8). 보내는 문자열 = 서명한 문자열
    params = [("symbol", "BTCUSDT"), ("newClientOrderId", "a b/c")]
    q = s.signed_query(params, _creds(), timestamp_ms=1, recv_window_ms=5000)
    payload, _, sig = q.rpartition("&signature=")
    assert "a%20b%2Fc" in payload
    assert sig == s.hmac_hex(DOC_SECRET, payload)


def test_signature_is_lowercase_hex_64() -> None:
    sig = s.hmac_hex(DOC_SECRET, "timestamp=1")
    assert len(sig) == 64 and sig == sig.lower()


@pytest.mark.parametrize("rw", [0, -1, 60001])
def test_recv_window_range(rw: int) -> None:
    with pytest.raises(ValueError):
        s.signed_query([], _creds(), timestamp_ms=1, recv_window_ms=rw)


def test_rejects_reserved_or_non_string_params() -> None:
    with pytest.raises(TypeError):
        s.signed_query([("price", 0.1)], _creds(), timestamp_ms=1)  # type: ignore[list-item]
    for reserved in ("signature", "timestamp", "recvWindow"):
        with pytest.raises(ValueError):
            s.signed_query([(reserved, "1")], _creds(), timestamp_ms=1)


def test_credentials_never_render_secret_values() -> None:
    c = _creds()
    for text in (repr(c), str(c), f"{c}", f"{c!r}"):
        assert DOC_API_KEY not in text and DOC_SECRET not in text
    assert c.api_key_header() == {"X-MBX-APIKEY": DOC_API_KEY}


def test_credentials_reject_plain_strings() -> None:
    # 비밀값은 reveal()이 있는 Secret 형식으로만 받는다 (평문 str 금지)
    with pytest.raises(TypeError):
        s.Credentials(DOC_API_KEY, DOC_SECRET)  # type: ignore[arg-type]


def test_empty_secret_rejected_without_echo() -> None:
    with pytest.raises(ValueError) as e:
        s.Credentials(_Secret(""), _Secret(DOC_SECRET)).api_key_header()
    assert DOC_SECRET not in str(e.value)


def test_ws_subscribe_signature_request_masks_key() -> None:
    req = s.ws_signed_request("req-1", "userDataStream.subscribe.signature", {}, _creds(), timestamp_ms=1499827319559)
    body = req.to_payload()
    assert body["method"] == "userDataStream.subscribe.signature"
    params = body["params"]
    assert params["apiKey"] == DOC_API_KEY and params["timestamp"] == 1499827319559
    # WS API 서명 payload: signature 외 파라미터를 이름순 정렬해 key=value&...
    assert params["signature"] == s.hmac_hex(DOC_SECRET, f"apiKey={DOC_API_KEY}&timestamp=1499827319559")
    assert DOC_API_KEY not in repr(req) and params["signature"] not in repr(req)
