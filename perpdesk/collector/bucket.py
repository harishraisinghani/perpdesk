"""Async token bucket for Hyperliquid's per-IP REST weight budget."""

from __future__ import annotations

import asyncio
import time


class WeightBucket:
    def __init__(self, capacity: int, refill_period: float = 60.0) -> None:
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.rate = self.capacity / refill_period
        self.updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, weight: int) -> None:
        if weight <= 0 or weight > self.capacity:
            raise ValueError(f"weight must be in [1, {int(self.capacity)}]")
        while True:
            async with self._lock:
                now = time.monotonic()
                self.tokens = min(
                    self.capacity, self.tokens + (now - self.updated_at) * self.rate
                )
                self.updated_at = now
                if self.tokens >= weight:
                    self.tokens -= weight
                    return
                wait = (weight - self.tokens) / self.rate
            await asyncio.sleep(wait)
