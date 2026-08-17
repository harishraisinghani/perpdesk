"""Validation and bulk loading for externally supplied wallet universes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


@dataclass(frozen=True, slots=True)
class WalletFile:
    addresses: tuple[str, ...]
    total_lines: int
    duplicate_lines: int


def read_wallet_file(path: str | Path) -> WalletFile:
    """Read, validate, lowercase, and de-duplicate one address per line."""
    source = Path(path)
    addresses: list[str] = []
    seen: set[str] = set()
    total_lines = 0
    duplicate_lines = 0

    with source.open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            total_lines += 1
            address = raw.strip()
            if not ADDRESS.fullmatch(address):
                raise ValueError(
                    f"{source}:{line_number}: expected a 20-byte 0x-prefixed hex address"
                )
            address = address.lower()
            if address in seen:
                duplicate_lines += 1
                continue
            seen.add(address)
            addresses.append(address)

    if not addresses:
        raise ValueError(f"{source}: wallet file is empty")
    return WalletFile(tuple(addresses), total_lines, duplicate_lines)
