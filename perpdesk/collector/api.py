"""Small typed wrapper around Hyperliquid's public info API."""

from __future__ import annotations

from typing import Any

import httpx

from .bucket import WeightBucket


class HyperliquidInfo:
    def __init__(self, url: str, bucket: WeightBucket, client: httpx.AsyncClient) -> None:
        self.url = url
        self.bucket = bucket
        self.client = client

    async def _post(self, body: dict[str, Any], weight: int) -> Any:
        await self.bucket.acquire(weight)
        response = await self.client.post(self.url, json=body)
        response.raise_for_status()
        return response.json()

    async def meta(self) -> dict[str, Any]:
        return await self._post({"type": "meta"}, 20)

    async def meta_and_asset_ctxs(self) -> list[Any]:
        return await self._post({"type": "metaAndAssetCtxs"}, 20)

    async def clearinghouse_state(self, address: str) -> dict[str, Any]:
        return await self._post({"type": "clearinghouseState", "user": address}, 2)
