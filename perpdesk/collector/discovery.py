"""Discover active accounts from the public trades WebSocket."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

import websockets

from .tls import verified_context

LOGGER = logging.getLogger(__name__)


async def run(
    ws_url: str,
    coins_source: Callable[[], list[str]],
    on_addresses: Callable[[list[str]], None],
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
                    desired = set(await asyncio.to_thread(coins_source))
                    for coin in subscribed - desired:
                        await socket.send(json.dumps({"method": "unsubscribe", "subscription": {"type": "trades", "coin": coin}}))
                        subscribed.remove(coin)
                    for coin in desired - subscribed:
                        await socket.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": coin}}))
                        subscribed.add(coin)
                    try:
                        raw = await asyncio.wait_for(socket.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    message = json.loads(raw)
                    data = message.get("data") or []
                    trades = data if isinstance(data, list) else data.get("trades", [])
                    addresses = [user for trade in trades for user in trade.get("users", [])]
                    if addresses:
                        await asyncio.to_thread(on_addresses, addresses)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("discovery WebSocket disconnected; retrying")
            await asyncio.sleep(2)
