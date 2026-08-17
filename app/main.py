"""Standalone FastAPI application; no Streamlit dependency or reused UI."""

from __future__ import annotations

import os
import re
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

from perpdesk.collector.config import Settings

from .data import DatabaseRepository, DemoRepository, serverless


BASE = Path(__file__).parent
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
CANDLE_INTERVAL_MS = {
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    # Only close what was actually opened; a process that served no request
    # should not build a Lakebase pool just to tear it down.
    if repository.cache_info().currsize:
        close = getattr(repository(), "close", None)
        if close is not None:
            close()


app = FastAPI(
    title="PerpDesk",
    description="Coverage-aware exact Hyperliquid liquidation risk",
    version="0.1.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


class Promotion(BaseModel):
    address: str


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes"}


def read_only() -> bool:
    """A public deployment serves live Lakebase reads but must not accept writes
    from anonymous visitors, so serverless defaults to closed. Databricks Apps
    and local runs keep write access unless asked otherwise."""
    load_dotenv()
    return _flag("PERPDESK_READ_ONLY", "true" if serverless() else "false")


@lru_cache(maxsize=1)
def repository():
    load_dotenv()
    return DemoRepository() if _flag("PERPDESK_DEMO_MODE", "true") else DatabaseRepository(
        Settings.from_env()
    )


def _reject_if_read_only() -> None:
    if read_only():
        raise HTTPException(403, "PerpDesk is published read-only; writes are disabled")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(BASE / "static" / "index.html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "mode": repository().mode, "read_only": read_only()}


@app.get("/api/dashboard")
def dashboard(coin: str = Query("BTC", pattern=r"^[A-Z0-9@._-]{1,20}$")) -> dict:
    try:
        return repository().dashboard(coin) | {"read_only": read_only()}
    except KeyError:
        raise HTTPException(404, f"coin {coin} is not tracked") from None


@app.get("/api/history")
def history(
    coin: str = Query("BTC", pattern=r"^[A-Z0-9@._-]{1,20}$"),
    shock_pct: int = Query(5, ge=1, le=50),
) -> dict:
    try:
        return {"coin": coin, "shock_pct": shock_pct, "points": repository().history(coin, shock_pct)}
    except KeyError:
        raise HTTPException(404, f"coin {coin} is not tracked") from None


def _normalize_candles(rows: object, limit: int) -> list[dict]:
    if not isinstance(rows, list):
        raise ValueError("candle response is not a list")
    candles = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            candle = {
                "time": int(row["t"]) // 1000,
                "open": float(row["o"]),
                "high": float(row["h"]),
                "low": float(row["l"]),
                "close": float(row["c"]),
                "volume": float(row["v"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
        if candle["low"] <= min(candle["open"], candle["close"], candle["high"]) and candle[
            "high"
        ] >= max(candle["open"], candle["close"], candle["low"]):
            candles.append(candle)
    unique = {row["time"]: row for row in candles}
    return [unique[key] for key in sorted(unique)][-limit:]


@app.get("/api/candles")
async def candles(
    coin: str = Query("BTC", pattern=r"^[A-Z0-9@._-]{1,20}$"),
    interval: str = Query("15m", pattern=r"^(15m|1h|4h)$"),
    limit: int = Query(96, ge=24, le=500),
) -> dict:
    try:
        repository().dashboard(coin)
    except KeyError:
        raise HTTPException(404, f"coin {coin} is not tracked") from None

    end_time = int(time.time() * 1000)
    start_time = end_time - CANDLE_INTERVAL_MS[interval] * (limit + 1)
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": interval,
            "startTime": start_time,
            "endTime": end_time,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(Settings.from_env().info_url, json=payload)
            response.raise_for_status()
            normalized = _normalize_candles(response.json(), limit)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "Hyperliquid candle data is unavailable") from exc

    return {"coin": coin, "interval": interval, "candles": normalized}


@app.get("/api/alerts")
def alerts() -> dict:
    return {"alerts": repository().alerts(), "read_only": read_only()}


@app.patch("/api/alerts/{alert_id}/acknowledge")
def acknowledge(alert_id: int) -> dict:
    _reject_if_read_only()
    if not repository().acknowledge(alert_id):
        raise HTTPException(404, "open alert not found")
    return {"acknowledged": True}


@app.post("/api/watchlist/promote", status_code=202)
def promote(payload: Promotion) -> dict:
    _reject_if_read_only()
    if not ADDRESS.fullmatch(payload.address):
        raise HTTPException(422, "address must be a 20-byte 0x-prefixed hex address")
    repository().promote(payload.address)
    return {"accepted": True, "address": payload.address.lower(), "selection": "ranked"}
