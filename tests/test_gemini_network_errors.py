import httpx

from app.services.gemini_provider import _is_network_like_error


def test_httpx_read_error_is_network_like():
    assert _is_network_like_error(httpx.ReadError("stream reset"), "httpx.ReadError")


def test_protocol_and_pool_errors_are_network_like():
    assert _is_network_like_error(httpx.RemoteProtocolError("server disconnected"), "")
    assert _is_network_like_error(httpx.PoolTimeout("pool exhausted"), "")
