"""Auth failure paths for scraper/firebase_auth.py. No real network."""
import asyncio
import time

import httpx
import pytest

from scraper.firebase_auth import AuthSession, firebase_sign_in
from scraper.api_bot import _build_transcript


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(coro):
    return asyncio.run(coro)


def _expired():
    return AuthSession(id_token="old", refresh_token="r", expires_at=time.time() - 10)


def test_refresh_401_raises_http_status_error():
    async def go():
        async with _client(lambda r: httpx.Response(401, json={"error": {"message": "TOKEN_EXPIRED"}})) as c:
            await _expired().ensure_fresh(c)
    with pytest.raises(httpx.HTTPStatusError):
        _run(go())


def test_refresh_network_error_propagates():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    async def go():
        async with _client(handler) as c:
            await _expired().ensure_fresh(c)
    with pytest.raises(httpx.ConnectError):
        _run(go())


def test_refresh_200_without_token_raises_runtime_error():
    async def go():
        async with _client(lambda r: httpx.Response(200, json={"oops": 1})) as c:
            await _expired().ensure_fresh(c)
    with pytest.raises(RuntimeError, match="Token refresh failed"):
        _run(go())


def test_sign_in_bad_password_raises_runtime_error():
    async def go():
        async with _client(lambda r: httpx.Response(400, json={"error": {"message": "INVALID_PASSWORD"}})) as c:
            await firebase_sign_in("a@b.c", "x", c)
    with pytest.raises(RuntimeError, match="INVALID_PASSWORD"):
        _run(go())


def test_sign_in_network_error_propagates():
    def handler(request):
        raise httpx.ConnectTimeout("slow", request=request)

    async def go():
        async with _client(handler) as c:
            await firebase_sign_in("a@b.c", "x", c)
    with pytest.raises(httpx.ConnectTimeout):
        _run(go())


def test_sign_in_non_json_error_is_clear_runtime_error():
    async def go():
        async with _client(lambda r: httpx.Response(502, text="<html>Bad Gateway</html>")) as c:
            await firebase_sign_in("a@b.c", "x", c)
    with pytest.raises(RuntimeError, match="sign-in failed"):
        _run(go())


def test_build_transcript_whitespace_agent_name_falls_back():
    msgs = [{"direction": "OUTGOING", "content": "hi", "createdAt": "2026-01-05T15:00:00Z"}]
    _, parsed = _build_transcript(msgs, "   ")
    assert parsed[0]["sender"] == "Agent"
