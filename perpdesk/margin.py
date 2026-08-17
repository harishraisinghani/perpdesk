"""Maintenance-margin reconstruction for Hyperliquid perpetuals.

Pure functions over plain floats and dicts. No Spark, no network, no I/O, no config. That is
deliberate: this module carries the project's load-bearing claim - that the liquidation map is
*computed* from published rules rather than estimated - so it has to be testable in milliseconds
without a cluster, and provable against the exchange's own numbers.

The rule, from the protocol docs:

    mmr(k)       = 1 / (2 * max_leverage(k))      "maintenance margin is half the initial
                                                   margin at max leverage"
    ded(0)       = 0
    ded(k)       = ded(k-1) + lower_bound(k) * (mmr(k) - mmr(k-1))
    mm(notional) = notional * mmr(k) - ded(k)     k = highest tier with lower_bound <= notional

The deduction term is what makes the tiered schedule continuous: without it, an account crossing
a tier boundary would see its maintenance margin jump discontinuously.

Verified against mainnet on 2026-08-15: recomputing cross maintenance margin this way and
comparing to the `crossMaintenanceMarginUsed` the exchange reports matched on 10 of 10 live
accounts, worst relative error 3.75e-07. See tests/test_margin.py.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "Tier",
    "derive_tiers",
    "resolve_margin_table",
    "build_tier_index",
    "tier_for_notional",
    "maintenance_margin",
    "cross_maintenance_margin",
]


@dataclass(frozen=True, slots=True)
class Tier:
    """One row of a margin tier schedule, with the two derived columns precomputed."""

    lower_bound: float  # notional USD at which this tier takes effect
    max_leverage: int
    mmr: float  # maintenance margin rate
    deduction: float  # maintenance deduction, cumulative


def derive_tiers(raw_tiers: list[dict]) -> tuple[Tier, ...]:
    """Turn ``meta.marginTables[i][1]["marginTiers"]`` into tiers with mmr and deduction.

    ``raw_tiers`` is the API shape: ``[{"lowerBound": "0.0", "maxLeverage": 40}, ...]``, ordered
    ascending by lower bound. The API has always returned them ordered, but this sorts anyway -
    the deduction recurrence is order-dependent and a silently mis-ordered schedule would produce
    plausible-looking wrong numbers rather than an error.
    """
    if not raw_tiers:
        raise ValueError("margin table has no tiers")

    ordered = sorted(raw_tiers, key=lambda t: float(t["lowerBound"]))
    tiers: list[Tier] = []
    deduction = 0.0
    prev_mmr: float | None = None

    for raw in ordered:
        lower_bound = float(raw["lowerBound"])
        max_leverage = int(raw["maxLeverage"])
        if max_leverage <= 0:
            raise ValueError(f"non-positive max leverage {max_leverage}")
        mmr = 1.0 / (2.0 * max_leverage)
        if prev_mmr is not None:
            deduction += lower_bound * (mmr - prev_mmr)
        tiers.append(Tier(lower_bound, max_leverage, mmr, deduction))
        prev_mmr = mmr

    if tiers[0].lower_bound != 0.0:
        raise ValueError(f"margin table does not start at zero: {tiers[0].lower_bound}")
    return tuple(tiers)


def resolve_margin_table(margin_table_id: int, margin_tables: dict[int, list[dict]]) -> list[dict]:
    """Resolve a universe entry's ``marginTableId`` to a raw tier list.

    THE TRAP THIS EXISTS FOR: ``meta.marginTables`` does not contain every id that
    ``meta.universe`` references. On 2026-08-15 mainnet::

        available in marginTables: [50, 51, 52, 53, 54, 55, 56]
        referenced by universe:    [3, 5, 10, 20, 51, 52, 53, 54, 55, 56]
        missing:                   [3, 5, 10, 20]

    The convention is that a bare id N means a single untiered schedule at max leverage N - ATOM
    carries ``marginTableId: 5`` and ``maxLeverage: 5``. So the fallback synthesises that row.

    Getting this wrong is expensive precisely because it is quiet: an inner join against
    ``marginTables`` drops most of the 232 coins out of the liquidation map, and every pipeline
    expectation still passes on the coins that remain.
    """
    if margin_table_id in margin_tables:
        return margin_tables[margin_table_id]
    if margin_table_id <= 0:
        raise ValueError(f"cannot synthesise a margin table for id {margin_table_id}")
    return [{"lowerBound": "0.0", "maxLeverage": margin_table_id}]


def build_tier_index(meta: dict) -> dict[str, tuple[Tier, ...]]:
    """Build ``{coin: tiers}`` from a raw ``meta`` info response.

    Every coin in the universe gets an entry, including the ones whose margin table id is absent
    from ``marginTables``. Callers can then treat the mapping as total.
    """
    tables = {int(tid): body["marginTiers"] for tid, body in meta["marginTables"]}
    index: dict[str, tuple[Tier, ...]] = {}

    for entry in meta["universe"]:
        raw = resolve_margin_table(int(entry["marginTableId"]), tables)
        tiers = derive_tiers(raw)
        # A synthesised table asserts that the bottom tier equals the universe's max leverage.
        # If that ever stops holding, the convention has changed and the fallback is now wrong -
        # better to fail loudly here than to publish a map built on a guess.
        if int(entry["marginTableId"]) not in tables:
            if tiers[0].max_leverage != int(entry["maxLeverage"]):
                raise ValueError(
                    f"{entry['name']}: synthesised margin table {entry['marginTableId']} implies "
                    f"max leverage {tiers[0].max_leverage} but universe says {entry['maxLeverage']}"
                )
        index[entry["name"]] = tiers

    return index


def tier_for_notional(notional: float, tiers: tuple[Tier, ...]) -> Tier:
    """The applicable tier for a position of this absolute notional."""
    applicable = tiers[0]
    for tier in tiers:
        if notional >= tier.lower_bound:
            applicable = tier
        else:
            break
    return applicable


def maintenance_margin(notional: float, tiers: tuple[Tier, ...]) -> float:
    """Maintenance margin required for a position of ``notional`` absolute USD value."""
    tier = tier_for_notional(notional, tiers)
    return notional * tier.mmr - tier.deduction


def cross_maintenance_margin(
    positions: list[dict],
    tier_index: dict[str, tuple[Tier, ...]],
) -> float:
    """Total cross maintenance margin for an account.

    ``positions`` are the raw ``position`` objects from ``clearinghouseState``. Isolated positions
    are excluded: they are liquidated against their own margin and notional alone, and the
    exchange's ``crossMaintenanceMarginUsed`` excludes them too - which is what makes the
    comparison in tests/test_margin.py a real check rather than a coincidence.
    """
    total = 0.0
    for position in positions:
        if position["leverage"]["type"] != "cross":
            continue
        notional = abs(float(position["positionValue"]))
        total += maintenance_margin(notional, tier_index[position["coin"]])
    return total
