"""Pure liquidation math used by the collector, pipeline, app, and tests.

The exchange's reported liquidation price for a position holds the rest of an
account's marks fixed.  Cross-margin liquidation does not work that way during a
market move: every leg contributes to the shared account value and maintenance
requirement.  This module computes that joint state directly from public inputs.

There is deliberately no Spark, database, or network dependency here.  The
closed-form solver is checked against :func:`liquidation_state`, a small brute
force oracle, in the unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Mapping, Sequence

from .margin import Tier, maintenance_margin

__all__ = [
    "AccountPosition",
    "LiquidationState",
    "account_value",
    "cross_maintenance_margin_at_marks",
    "joint_liquidation_multiplier",
    "joint_liquidation_price",
    "liquidation_state",
    "scenario_liquidation_state",
]


@dataclass(frozen=True, slots=True)
class AccountPosition:
    """The minimum position state needed by the solvency model."""

    coin: str
    szi: float
    mark_px: float
    tiers: tuple[Tier, ...]
    leverage_type: str = "cross"
    liquidation_px_reported: float | None = None

    @property
    def signed_notional(self) -> float:
        return self.szi * self.mark_px

    @property
    def notional(self) -> float:
        return abs(self.signed_notional)


@dataclass(frozen=True, slots=True)
class LiquidationState:
    """Account solvency after applying a set of price multipliers."""

    account_value: float
    maintenance_margin: float

    @property
    def equity_buffer(self) -> float:
        return self.account_value - self.maintenance_margin

    @property
    def liquidatable(self) -> bool:
        return self.equity_buffer < 0.0


def _cross(positions: Sequence[AccountPosition]) -> tuple[AccountPosition, ...]:
    return tuple(p for p in positions if p.leverage_type == "cross" and p.szi != 0.0)


def account_value(total_raw_usd: float, positions: Sequence[AccountPosition]) -> float:
    """Reconstruct account value from the cash leg and current signed notionals."""
    return total_raw_usd + sum(p.signed_notional for p in positions)


def cross_maintenance_margin_at_marks(positions: Sequence[AccountPosition]) -> float:
    """Cross maintenance requirement at the positions' current marks."""
    return sum(maintenance_margin(p.notional, p.tiers) for p in _cross(positions))


def scenario_liquidation_state(
    total_raw_usd: float,
    positions: Sequence[AccountPosition],
    multipliers: Mapping[str, float],
) -> LiquidationState:
    """Evaluate cross-account solvency under an arbitrary correlated scenario.

    Coins omitted from ``multipliers`` remain at their current mark. Isolated
    positions are intentionally excluded: they have their own margin pool and
    must be reported as a separate series rather than blended into cross risk.
    """
    cross_positions = _cross(positions)
    value = total_raw_usd
    mm = 0.0
    for position in cross_positions:
        multiplier = float(multipliers.get(position.coin, 1.0))
        if multiplier < 0 or not isfinite(multiplier):
            raise ValueError(f"invalid multiplier for {position.coin}: {multiplier}")
        shocked_signed_notional = position.signed_notional * multiplier
        value += shocked_signed_notional
        mm += maintenance_margin(abs(shocked_signed_notional), position.tiers)
    return LiquidationState(value, mm)


def liquidation_state(
    total_raw_usd: float,
    positions: Sequence[AccountPosition],
    shock_coin: str,
    multiplier: float,
) -> LiquidationState:
    """Brute-force oracle for a shock to one coin while other marks stay fixed."""
    return scenario_liquidation_state(total_raw_usd, positions, {shock_coin: multiplier})


def joint_liquidation_multiplier(
    total_raw_usd: float,
    positions: Sequence[AccountPosition],
    shock_coin: str,
    *,
    min_multiplier: float = 0.0,
    max_multiplier: float = 5.0,
) -> float | None:
    """Solve the exact adverse single-asset liquidation multiplier.

    For a long, the adverse interval is ``[min_multiplier, 1]``; for a short it
    is ``[1, max_multiplier]``. Within each notional tier, account value minus
    maintenance margin is linear in the multiplier. We solve each segment and
    retain the unique root in the adverse interval.

    ``None`` means no liquidation root exists inside the requested interval.
    ``1.0`` means the account is already liquidatable at current marks.
    """
    if not (0.0 <= min_multiplier <= 1.0 <= max_multiplier):
        raise ValueError("expected min_multiplier <= 1 <= max_multiplier")

    cross_positions = _cross(positions)
    targets = [p for p in cross_positions if p.coin == shock_coin]
    if not targets:
        return None
    if len(targets) != 1:
        raise ValueError(f"duplicate {shock_coin} positions are not supported")

    target = targets[0]
    now = scenario_liquidation_state(total_raw_usd, cross_positions, {})
    if now.liquidatable:
        return 1.0

    base_notional = target.notional
    if base_notional <= 0.0 or target.mark_px <= 0.0:
        return None

    mm_rest = sum(
        maintenance_margin(p.notional, p.tiers)
        for p in cross_positions
        if p.coin != shock_coin
    )
    av_now = account_value(total_raw_usd, cross_positions)
    adverse_low, adverse_high = (
        (min_multiplier, 1.0) if target.szi > 0 else (1.0, max_multiplier)
    )

    candidates: list[float] = []
    tiers = target.tiers
    for index, tier in enumerate(tiers):
        segment_low = tier.lower_bound / base_notional
        segment_high = (
            tiers[index + 1].lower_bound / base_notional
            if index + 1 < len(tiers)
            else float("inf")
        )
        low = max(adverse_low, segment_low)
        high = min(adverse_high, segment_high)
        if low > high:
            continue

        # f(s) = AV(s) - MM(s) = intercept + slope*s.
        intercept = av_now - target.signed_notional - mm_rest + tier.deduction
        slope = target.signed_notional - base_notional * tier.mmr
        if slope == 0.0:
            continue
        root = -intercept / slope
        tolerance = 1e-12 * max(1.0, abs(root))
        if low - tolerance <= root <= high + tolerance:
            candidates.append(min(max(root, adverse_low), adverse_high))

    if not candidates:
        return None

    # The piecewise function is continuous and monotone. Numerical noise at a
    # tier boundary can produce the same root twice, so select the closest
    # adverse root to the current price.
    return max(candidates) if target.szi > 0 else min(candidates)


def joint_liquidation_price(
    total_raw_usd: float,
    positions: Sequence[AccountPosition],
    shock_coin: str,
    **kwargs: float,
) -> float | None:
    """Return the joint liquidation mark price instead of its multiplier."""
    target = next((p for p in positions if p.coin == shock_coin and p.szi != 0), None)
    if target is None:
        return None
    multiplier = joint_liquidation_multiplier(
        total_raw_usd, positions, shock_coin, **kwargs
    )
    return None if multiplier is None else target.mark_px * multiplier
