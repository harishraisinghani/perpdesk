"""T0 account-state subscriptions: highest notional at ~4 second freshness."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import websockets

from .tls import verified_context

LOGGER = logging.getLogger(__name__)


async def run(
    ws_url: str,
    addresses_source: Callable[[], list[str]],
    on_state: Callable[[str, dict], Awaitable[None]],
) -> None:
    while True:
        try:
            async with websockets.connect(
                ws_url,
                ssl=verified_context() if ws_url.startswith("wss://") else None,
                ping_interval=20,
                ping_timeout=20,
            ) as socket:
                subscribed: set[str] = set()
                while True:
                    # Re-read the control plane every five seconds. App promotions
                    # join the live tier without restarting this process.
                    desired = set(await asyncio.to_thread(addresses_source))
                    for address in subscribed - desired:
                        await socket.send(json.dumps({"method": "unsubscribe", "subscription": {"type": "clearinghouseState", "user": address}}))
                        subscribed.remove(address)
                    for address in desired:
                        if address in subscribed:
                            continue
                        await socket.send(json.dumps({"method": "subscribe", "subscription": {"type": "clearinghouseState", "user": address}}))
                        subscribed.add(address)
                    try:
                        raw = await asyncio.wait_for(socket.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    message = json.loads(raw)
                    if message.get("channel") != "clearinghouseState":
                        continue
                    data = message.get("data") or {}
                    address = (data.get("user") or "").lower()
                    state = data.get("clearinghouseState", data)
                    if address:
                        await on_state(address, state)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("state WebSocket disconnected; retrying")
            await asyncio.sleep(2)
