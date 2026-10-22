# pylint: disable=protected-access,unused-argument,missing-function-docstring,missing-class-docstring,redefined-outer-name
"""HTTP failures are classified and connections are always released."""

import asyncio
from unittest.mock import AsyncMock

import aiohttp
import pytest

from gas_station_spain_api import gas_station as api
from gas_station_spain_api.exceptions import (
    GasStationServerUnavailableException,
)


class FakeResponse:
    """Small aiohttp response double suitable for the new GET client."""

    def __init__(self, status=200, result=None, error=None):
        self.status = status
        self.result = result
        self.error = error
        self.released = False

    async def json(self):
        """Return JSON or simulate malformed JSON."""
        if self.error is not None:
            raise self.error
        return self.result

    def release(self):
        """Mark socket response released."""
        self.released = True

    def raise_for_status(self):
        """Raise aiohttp's normal response error for a bad request."""
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=None, history=(), status=self.status
            )


class FakeSession:
    """Record session closure independently of request outcome."""

    def __init__(self, response):
        self.response = response
        self.closed = False
        self.requests = []

    async def get(self, url):
        """Return a fake HTTP response."""
        self.requests.append(url)
        return self.response

    async def close(self):
        """Record cleanup on every exit path."""
        self.closed = True


@pytest.fixture
def fake_http(monkeypatch):
    """Return a factory for a sequence of fake HTTP response sessions."""
    sessions = []

    def install(*responses):
        """Install response sequence and capture created sessions."""
        pending = list(responses)

        def create(headers):
            """Return a new owned session on each attempt."""
            assert headers["Accept"] == "application/json"
            session = FakeSession(pending.pop(0))
            sessions.append(session)
            return session

        monkeypatch.setattr(api, "_create_session", create)

    monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())
    return install, sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 503])
async def test_server_errors_retried_and_sessions_closed(status, fake_http):
    """Retry 5xx exactly three times, closing each response and session."""
    install, sessions = fake_http
    responses = [FakeResponse(status=status) for _ in range(3)]
    install(*responses)
    with pytest.raises(GasStationServerUnavailableException) as exc:
        await api._fetch_json("https://example.invalid/stations")
    assert exc.value.status_code == status
    assert len(sessions) == 3
    assert all(item.closed for item in sessions)
    assert all(item.released for item in responses)


@pytest.mark.asyncio
async def test_404_is_not_retried(fake_http):
    """HTTP client errors are not transient."""
    install, sessions = fake_http
    response = FakeResponse(status=404)
    install(response)
    with pytest.raises(aiohttp.ClientResponseError) as exc:
        await api._fetch_json("https://example.invalid/stations")
    assert exc.value.status == 404
    assert len(sessions) == 1
    assert sessions[0].closed
    assert response.released


@pytest.mark.asyncio
async def test_bad_json_still_closes_session(fake_http):
    """Invalid JSON should not leak the owning client session."""
    install, sessions = fake_http
    response = FakeResponse(error=ValueError("HTML instead of JSON"))
    install(response)
    with pytest.raises(ValueError, match="HTML"):
        await api._fetch_json("https://example.invalid/stations")
    assert len(sessions) == 1
    assert sessions[0].closed
    assert response.released


@pytest.mark.asyncio
async def test_get_timeout_is_retried(monkeypatch):
    """Timeouts should terminate after three attempts without open sockets."""
    sessions = []

    class TimeoutSession(FakeSession):
        """Simulate network read timing out."""

        async def get(self, url):
            """Raise an asyncio timeout before a response exists."""
            raise asyncio.TimeoutError()

    def create(headers):
        """Capture all transient request sessions."""
        session = TimeoutSession(None)
        sessions.append(session)
        return session

    monkeypatch.setattr(api, "_create_session", create)
    monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())
    with pytest.raises(asyncio.TimeoutError):
        await api._fetch_json("https://example.invalid/stations")
    assert len(sessions) == 3
    assert all(item.closed for item in sessions)


@pytest.mark.asyncio
async def test_success_response_is_released(fake_http):
    """Successful JSON must return data and release the response."""
    install, sessions = fake_http
    response = FakeResponse(result={"ListaEESSPrecio": []})
    install(response)
    result = await api._fetch_json("https://example.invalid/stations")
    assert result["ListaEESSPrecio"] == []
    assert sessions[0].closed
    assert response.released


@pytest.mark.asyncio
async def test_wrong_shape_reports_error(monkeypatch):
    """Malformed station structure should fail instead of looking like no fuel."""
    monkeypatch.setattr(api, "_cached_json", AsyncMock(return_value={"oops": []}))
    with pytest.raises(ValueError, match="Unexpected"):
        await api.get_gas_stations(municipality_id=355)


@pytest.mark.asyncio
async def test_empty_official_station_list_is_valid(monkeypatch):
    """An explicit empty Ministry list is not a network error."""
    monkeypatch.setattr(
        api, "_cached_json", AsyncMock(return_value={"ListaEESSPrecio": []})
    )
    assert await api.get_gas_stations(municipality_id=355) == []
