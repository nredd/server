"""
Owned loopback HTTP delays with deliberately incomparable client clock origins.

References:
    https://docs.aiohttp.org/en/stable/testing.html
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from aiohttp import ClientTimeout, web

from music_assistant.controllers.webserver.controller import WebserverController
from tests.providers.airplay.test_private_transport_chain import PLAYER, TOKEN, URL, Chain


@pytest.mark.parametrize(("offset", "drop_mode"), [(-1e9, "none"), (1e9, "play"), (1e9, "arm")])
async def test_real_loopback_ack_loss_and_offset_clocks(
    monkeypatch: pytest.MonkeyPatch,
    aiohttp_client: Any,
    offset: float,
    drop_mode: str,
) -> None:
    """Server expiry remains causal when either acknowledgement is unavailable."""
    chain = Chain(monkeypatch)
    chain.web._localized_json_response = lambda result, _locale: web.json_response(result)

    async def handler(request: web.Request) -> web.StreamResponse:
        """Invoke the actual HTTP resolver, then delay only the response delivery."""
        response = await WebserverController._handle_jsonrpc_api_command(chain.web, request)
        if request.headers["X-Test-Mode"] == drop_mode:
            await asyncio.sleep(0.15)
        return response

    app = web.Application()
    app.router.add_post("/api", handler)
    client = await aiohttp_client(app)

    async def post(mode: str) -> Any:
        """Use one actual HTTP request without retry or replacement input."""
        request = {"mode": mode, "attempt_id": TOKEN}
        if mode == "arm":
            request |= {"player_id": PLAYER, "media": URL}
        async with client.post(
            "/api",
            json={"command": "airplay/private_trial_transport", "args": {"request": request}},
            headers={"X-Test-Mode": mode},
            timeout=ClientTimeout(total=0.05 if mode == drop_mode else 1),
        ) as response:
            assert response.status == 200
            return await response.json()

    before_arm = time.monotonic() + offset
    dispatch = before_arm
    if drop_mode == "arm":
        with pytest.raises(TimeoutError):
            await post("arm")
    else:
        ack = await post("arm")
        assert ack["outcome"] == "armed"
        after_ack = time.monotonic() + offset
        assert 1.75 - (after_ack - before_arm) - 0.05 >= 0.25
        dispatch = time.monotonic() + offset
        if drop_mode == "play":
            with pytest.raises(TimeoutError):
                await post("play")
        else:
            await post("play")
    attempt = chain.provider._private_transport_attempt
    await attempt.watchdog
    observed = time.monotonic() + offset
    assert attempt.revoked
    assert attempt.snapshot()["cleanup_complete"] is True
    if drop_mode == "arm":
        assert not attempt.consumed
    else:
        assert observed <= dispatch + 5
        assert attempt.start_task.cancelled()
    chain.assert_drained()


async def test_delayed_play_delivery_cannot_reset_armed_deadline(
    monkeypatch: pytest.MonkeyPatch,
    aiohttp_client: Any,
) -> None:
    """Deliver a real PLAY HTTP request only after the unused server attempt expired."""
    chain = Chain(monkeypatch)
    await chain.private("arm")
    attempt = chain.provider._private_transport_attempt
    deadline = attempt.cancel_at
    delivered = asyncio.Event()

    async def handler(request: web.Request) -> web.StreamResponse:
        """Delay receipt processing beyond expiry, not an invented pre-lock race."""
        await attempt.watchdog
        delivered.set()
        try:
            return await WebserverController._handle_jsonrpc_api_command(chain.web, request)
        except asyncio.CancelledError:
            return web.Response(status=409)

    app = web.Application()
    app.router.add_post("/api", handler)
    client = await aiohttp_client(app)
    async with client.post(
        "/api",
        json={
            "command": "airplay/private_trial_transport",
            "args": {"request": {"mode": "play", "attempt_id": TOKEN}},
        },
    ) as response:
        assert response.status == 409
    assert delivered.is_set()
    assert not attempt.consumed
    assert not chain.entered.is_set()
    assert attempt.cancel_at == deadline
