"""Verified TLS context shared by Hyperliquid WebSocket clients."""

from __future__ import annotations

import ssl

import certifi


def verified_context() -> ssl.SSLContext:
    """Use certifi explicitly for Python installations without macOS CA wiring."""
    return ssl.create_default_context(cafile=certifi.where())
