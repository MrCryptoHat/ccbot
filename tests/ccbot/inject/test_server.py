"""Integration tests for inject.server using aiohttp's test client.

Covers the full status-code matrix: token auth, bad JSON / bad text,
allowlist gate (403 forbidden_agent) vs not-running (503 not_running),
empty-after-sanitize, reject-if-busy, unavailable, send failure, and the
success path for both docker and host-tmux bindings (including that the
text actually pushed is the sanitized form — leading "!" defused).

Agent-name → binding resolution is stubbed via
``session_manager.resolve_agent_binding`` (its own logic is unit-tested in
test_session.py); these tests pin only what the HTTP layer does with each
resolution outcome.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from ccbot.config import InjectConfig
from ccbot.inject.server import build_app

_TOKEN = "s3cret"
_HDR = {"X-Inject-Token": _TOKEN}
_RESOLVE = "ccbot.inject.server.session_manager.resolve_agent_binding"
_CAPTURE = "ccbot.inject.server.session_manager.capture_pane"
_SEND = "ccbot.inject.server.session_manager.send_to_window"


def _cfg(
    tmp_path: Path, *, agents: frozenset[str] = frozenset({"assistant", "example.com"})
) -> InjectConfig:
    return InjectConfig(
        token=_TOKEN,
        socket_path=tmp_path / "inject.sock",
        allowed_agents=agents,
    )


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncGenerator[TestClient, None]:
    app = build_app(_cfg(tmp_path))
    async with TestClient(TestServer(app)) as c:
        yield c


@pytest.mark.asyncio
async def test_missing_token_unauthorized(client: TestClient) -> None:
    r = await client.post("/inject", json={"agent": "assistant", "text": "hi"})
    assert r.status == 401


@pytest.mark.asyncio
async def test_wrong_token_unauthorized(client: TestClient) -> None:
    r = await client.post(
        "/inject",
        json={"agent": "assistant", "text": "hi"},
        headers={"X-Inject-Token": "nope"},
    )
    assert r.status == 401


@pytest.mark.asyncio
async def test_bad_json_returns_400(client: TestClient) -> None:
    r = await client.post(
        "/inject",
        data=b"not-json",
        headers={**_HDR, "Content-Type": "application/json"},
    )
    assert r.status == 400


@pytest.mark.asyncio
async def test_non_string_text_returns_400(client: TestClient) -> None:
    r = await client.post(
        "/inject", json={"agent": "assistant", "text": 123}, headers=_HDR
    )
    assert r.status == 400
    assert (await r.json())["error"] == "bad_text"


@pytest.mark.asyncio
async def test_agent_not_in_allowlist_forbidden(client: TestClient) -> None:
    r = await client.post(
        "/inject", json={"agent": "ccbot", "text": "hi"}, headers=_HDR
    )
    assert r.status == 403
    assert (await r.json())["error"] == "forbidden_agent"


@pytest.mark.asyncio
async def test_allowlisted_but_not_running_returns_503(client: TestClient) -> None:
    # In the allowlist, but resolves to no binding (no docker agent, no live
    # tmux window) → the agent isn't up. Distinct from forbidden_agent.
    with patch(_RESOLVE, new=AsyncMock(return_value=None)):
        r = await client.post(
            "/inject", json={"agent": "example.com", "text": "hi"}, headers=_HDR
        )
    assert r.status == 503
    assert (await r.json())["error"] == "not_running"


@pytest.mark.asyncio
async def test_empty_after_sanitize_returns_400(client: TestClient) -> None:
    with patch(_RESOLVE, new=AsyncMock(return_value="docker:assistant")):
        r = await client.post(
            "/inject", json={"agent": "assistant", "text": "\x1b\r\x00"}, headers=_HDR
        )
    assert r.status == 400
    assert (await r.json())["error"] == "empty_text"


@pytest.mark.asyncio
async def test_busy_returns_409(client: TestClient) -> None:
    with (
        patch(_RESOLVE, new=AsyncMock(return_value="docker:assistant")),
        patch(_CAPTURE, new=AsyncMock(return_value="… (5s · esc to interrupt)")),
        patch(
            "ccbot.inject.server.terminal_parser.is_claude_working", return_value=True
        ),
    ):
        r = await client.post(
            "/inject", json={"agent": "assistant", "text": "hi"}, headers=_HDR
        )
    assert r.status == 409
    assert (await r.json())["error"] == "busy"


@pytest.mark.asyncio
async def test_unreachable_pane_returns_503_unavailable(client: TestClient) -> None:
    with (
        patch(_RESOLVE, new=AsyncMock(return_value="docker:assistant")),
        patch(_CAPTURE, new=AsyncMock(return_value=None)),
    ):
        r = await client.post(
            "/inject", json={"agent": "assistant", "text": "hi"}, headers=_HDR
        )
    assert r.status == 503
    assert (await r.json())["error"] == "unavailable"


@pytest.mark.asyncio
async def test_send_failure_returns_503_unavailable(client: TestClient) -> None:
    with (
        patch(_RESOLVE, new=AsyncMock(return_value="docker:assistant")),
        patch(_CAPTURE, new=AsyncMock(return_value="idle")),
        patch(_SEND, new=AsyncMock(return_value=(False, "Container not running"))),
    ):
        r = await client.post(
            "/inject", json={"agent": "assistant", "text": "hi"}, headers=_HDR
        )
    assert r.status == 503
    assert (await r.json())["error"] == "unavailable"


@pytest.mark.asyncio
async def test_success_docker_injects_sanitized_text(client: TestClient) -> None:
    send_mock = AsyncMock(return_value=(True, "ok"))
    with (
        patch(_RESOLVE, new=AsyncMock(return_value="docker:assistant")),
        patch(_CAPTURE, new=AsyncMock(return_value="idle prompt")),
        patch(_SEND, new=send_mock),
    ):
        # Leading "!" must be defused before it reaches send_to_window.
        r = await client.post(
            "/inject",
            json={"agent": "assistant", "text": "!купи молоко"},
            headers=_HDR,
        )
    assert r.status == 200
    assert (await r.json())["ok"] is True
    send_mock.assert_awaited_once()
    assert send_mock.await_args is not None
    binding, text = send_mock.await_args.args
    assert binding == "docker:assistant"
    assert text == " !купи молоко"
    assert not text.startswith("!")


@pytest.mark.asyncio
async def test_success_tmux_binding_sent(client: TestClient) -> None:
    # A host-tmux agent resolves to an @<id> binding; the send path is the
    # same transport-agnostic send_to_window.
    send_mock = AsyncMock(return_value=(True, "ok"))
    with (
        patch(_RESOLVE, new=AsyncMock(return_value="@7")),
        patch(_CAPTURE, new=AsyncMock(return_value="idle prompt")),
        patch(_SEND, new=send_mock),
    ):
        r = await client.post(
            "/inject",
            json={"agent": "example.com", "text": "deploy please"},
            headers=_HDR,
        )
    assert r.status == 200
    assert send_mock.await_args is not None
    binding, text = send_mock.await_args.args
    assert binding == "@7"
    assert text == "deploy please"
