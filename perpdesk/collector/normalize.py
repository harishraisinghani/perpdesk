"""Convert public API responses into stable database records."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from perpdesk.margin import derive_tiers, resolve_margin_table


def _number(value: Any, default: str = "0") -> str:
    return str(default if value in (None, "") else value)


def normalize_meta(meta: dict[str, Any], observed_at: datetime | None = None) -> tuple[list[dict], list[dict]]:
    observed_at = observed_at or datetime.now(timezone.utc)
    raw_tables = {int(key): body["marginTiers"] for key, body in meta.get("marginTables", [])}
    universe: list[dict] = []
    table_rows: dict[tuple[int, int], dict] = {}
    for asset in meta["universe"]:
        table_id = int(asset["marginTableId"])
        synthesised = table_id not in raw_tables
        raw_tiers = resolve_margin_table(table_id, raw_tables)
        tiers = derive_tiers(raw_tiers)
        if synthesised and tiers[0].max_leverage != int(asset["maxLeverage"]):
            raise ValueError(f"{asset['name']}: fallback leverage does not match universe")
        universe.append(
            {
                "coin": asset["name"],
                "sz_decimals": int(asset["szDecimals"]),
                "max_leverage": int(asset["maxLeverage"]),
                "margin_table_id": table_id,
                "only_isolated": bool(asset.get("onlyIsolated", False)),
                "is_delisted": bool(asset.get("isDelisted", False)),
                "fetched_at": observed_at,
            }
        )
        for tier_no, tier in enumerate(tiers):
            table_rows[(table_id, tier_no)] = {
                "margin_table_id": table_id,
                "tier": tier_no,
                "lower_bound": str(tier.lower_bound),
                "max_leverage": tier.max_leverage,
                "synthesised": synthesised,
                "fetched_at": observed_at,
            }
    return universe, list(table_rows.values())


def normalize_asset_contexts(payload: list[Any], observed_at: datetime | None = None) -> list[dict]:
    observed_at = observed_at or datetime.now(timezone.utc)
    meta, contexts = payload
    if len(meta["universe"]) != len(contexts):
        raise ValueError("metaAndAssetCtxs universe/context lengths differ")
    return [
        {
            "coin": asset["name"],
            "mark_px": _number(context["markPx"]),
            "prev_day_px": _number(context.get("prevDayPx")) if context.get("prevDayPx") else None,
            "oracle_px": _number(context.get("oraclePx")) if context.get("oraclePx") else None,
            "funding": _number(context.get("funding")) if context.get("funding") else None,
            "open_interest": _number(context.get("openInterest")) if context.get("openInterest") else None,
            "premium": _number(context.get("premium")) if context.get("premium") else None,
            "day_ntl_volume": _number(context.get("dayNtlVlm")) if context.get("dayNtlVlm") else None,
            "observed_at": observed_at,
        }
        for asset, context in zip(meta["universe"], contexts, strict=True)
    ]


def normalize_account_state(
    address: str,
    state: dict[str, Any],
    observed_at: datetime | None = None,
    *,
    tier: int = 2,
    periodic: bool = False,
) -> tuple[dict, list[dict]]:
    observed_at = observed_at or datetime.now(timezone.utc)
    address = address.lower()
    summary = state.get("crossMarginSummary") or state.get("marginSummary") or {}
    positions: list[dict] = []
    hash_positions: list[dict] = []
    for wrapped in state.get("assetPositions", []):
        position = wrapped.get("position", wrapped)
        szi = _number(position.get("szi"))
        if float(szi) == 0:
            continue
        leverage = position.get("leverage") or {}
        coin = position["coin"]
        row = {
            "account": address,
            "coin": coin,
            "szi": szi,
            "entry_px": position.get("entryPx"),
            "position_value": _number(position.get("positionValue")),
            "margin_used": _number(position.get("marginUsed")),
            "unrealized_pnl": _number(position.get("unrealizedPnl")),
            "leverage_value": leverage.get("value"),
            "leverage_type": leverage.get("type", "cross"),
            "leverage_raw_usd": leverage.get("rawUsd"),
            "max_leverage": position.get("maxLeverage"),
            "liquidation_px_reported": position.get("liquidationPx"),
            "observed_at": observed_at,
        }
        positions.append(row)
        hash_positions.append(
            {
                "coin": coin,
                "szi": szi,
                "entry_px": row["entry_px"],
                "leverage": leverage,
            }
        )
    total_raw_usd = _number(summary.get("totalRawUsd"))
    canonical = json.dumps(
        {"total_raw_usd": total_raw_usd, "positions": sorted(hash_positions, key=lambda x: x["coin"])},
        sort_keys=True,
        separators=(",", ":"),
    )
    book_hash = hashlib.sha256(canonical.encode()).hexdigest()
    cross_mm = state.get("crossMaintenanceMarginUsed", summary.get("totalMarginUsed", "0"))
    account = {
        "account": address,
        "total_raw_usd": total_raw_usd,
        "account_value_reported": _number(summary.get("accountValue")),
        "cross_mm_reported": _number(cross_mm),
        "book_hash": book_hash,
        "tier": tier,
        "cross_notional": str(
            sum(abs(float(p["position_value"])) for p in positions if p["leverage_type"] == "cross")
        ),
        "observed_at": observed_at,
        "is_periodic": periodic,
    }
    return account, positions
