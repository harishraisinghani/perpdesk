"""Application data layer: live Lakebase in production, deterministic demo locally."""

from __future__ import annotations

import math
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg_pool import ConnectionPool

from perpdesk.collector.config import Settings
from perpdesk.collector.db import create_pool
from perpdesk.margin import Tier, derive_tiers
from perpdesk.shocks import AccountPosition, joint_liquidation_multiplier


SHOCKS = (1, 2, 3, 5, 7, 10, 15, 20)
TOP_MARKET_LIMIT = 10
CLIFF_MAX_DROP_PCT = max(SHOCKS)
TREND_CONFIRMATION_FRACTION = 0.005


def _money(value: float) -> float:
    return round(value, 2)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _joint_closer_by_pp(joint: float, marginal: float | None) -> float | None:
    """Return how much closer joint liquidation is to the current-price multiplier."""
    if marginal is None:
        return None
    return (abs(marginal - 1.0) - abs(joint - 1.0)) * 100


def _top_market_coins(
    marks: dict[str, dict[str, Any]], limit: int = TOP_MARKET_LIMIT
) -> list[str]:
    """Return the largest markets by current open-interest notional."""
    ranked = sorted(
        marks.items(),
        key=lambda item: (
            -float(item[1].get("open_interest_notional") or 0),
            item[0],
        ),
    )
    return [coin for coin, _ in ranked[:limit]]


def _market_action(dominant: str, trend_fraction: float | None) -> tuple[str, str]:
    """Return a momentum-confirmed action and the rule that produced it."""
    if trend_fraction is None:
        return "wait", "Prior-day price unavailable"
    if dominant == "upside" and trend_fraction >= TREND_CONFIRMATION_FRACTION:
        return "long", "Uptrend confirms short-squeeze skew"
    if dominant == "downside" and trend_fraction <= -TREND_CONFIRMATION_FRACTION:
        return "short", "Downtrend confirms downside-cascade skew"
    if dominant == "balanced":
        return "wait", "No clear liquidation skew"
    if abs(trend_fraction) < TREND_CONFIRMATION_FRACTION:
        return "wait", "One-day trend is below 0.5%"
    return "wait", "Trend opposes liquidation skew"


def _build_market_summary(
    accounts: list[dict[str, Any]],
    marks: dict[str, dict[str, Any]],
    tiers: dict[str, tuple[Tier, ...]],
    *,
    mode: str,
    precomputed_roots: dict[tuple[str, str], float] | None = None,
) -> list[dict[str, Any]]:
    """Compare symmetric 5% liquidation exposure across the largest markets."""
    rows = []
    for coin in _top_market_coins(marks):
        dashboard = _build_dashboard(
            accounts,
            marks,
            tiers,
            coin,
            mode=mode,
            precomputed_roots=precomputed_roots,
        )
        downside = next(row for row in dashboard["scenarios"] if row["shock_pct"] == 5)
        upside = dashboard["upside_5"]
        downside_share = float(downside["share_of_tracked"])
        upside_share = float(upside["share_of_tracked"])
        total_scenario_share = downside_share + upside_share
        imbalance = (
            (downside_share - upside_share) / total_scenario_share
            if total_scenario_share
            else 0.0
        )
        if total_scenario_share == 0 or abs(imbalance) < 0.15:
            dominant = "balanced"
            setup = "No directional edge"
        elif imbalance > 0:
            dominant = "downside"
            setup = "Potential short after downside confirmation"
        else:
            dominant = "upside"
            setup = "Potential long after upside confirmation"
        funding = float(marks[coin].get("funding") or 0)
        mark_px = float(marks[coin]["mark_px"])
        prev_day_px = float(marks[coin].get("prev_day_px") or 0)
        trend_fraction = mark_px / prev_day_px - 1.0 if prev_day_px > 0 else None
        action, action_reason = _market_action(dominant, trend_fraction)
        funding_aligned = (action == "short" and funding > 0) or (
            action == "long" and funding < 0
        )
        rows.append(
            {
                "coin": coin,
                "mark_px": dashboard["mark_px"],
                "tracked_notional": dashboard["tracked_notional"],
                "coverage_fraction_open_interest": dashboard[
                    "coverage_fraction_open_interest"
                ],
                "downside_5": downside,
                "upside_5": upside,
                "dominant": dominant,
                "imbalance": imbalance,
                "risk_share_of_tracked": max(downside_share, upside_share),
                "funding": funding,
                "funding_aligned": funding_aligned,
                "prev_day_px": prev_day_px or None,
                "trend_fraction": trend_fraction,
                "action": action,
                "action_reason": action_reason,
                "setup": setup,
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            -row["risk_share_of_tracked"],
            -max(
                row["downside_5"]["liquidatable_notional_tracked"],
                row["upside_5"]["liquidatable_notional_tracked"],
            ),
            row["coin"],
        ),
    )


def _build_dashboard(
    accounts: list[dict[str, Any]],
    marks: dict[str, dict[str, Any]],
    tiers: dict[str, tuple[Tier, ...]],
    coin: str,
    *,
    mode: str,
    precomputed_roots: dict[tuple[str, str], float] | None = None,
) -> dict[str, Any]:
    roots: list[dict[str, Any]] = []
    observed: list[datetime] = []
    tracked_notional = 0.0
    live_notional = 0.0
    for raw_account in accounts:
        positions: list[AccountPosition] = []
        target_raw: dict[str, Any] | None = None
        for raw in raw_account["positions"]:
            if raw["coin"] not in marks or raw["coin"] not in tiers:
                continue
            position = AccountPosition(
                raw["coin"],
                float(raw["szi"]),
                float(marks[raw["coin"]]["mark_px"]),
                tiers[raw["coin"]],
                raw.get("leverage_type", "cross"),
                float(raw["liquidation_px_reported"])
                if raw.get("liquidation_px_reported") not in (None, "")
                else None,
            )
            positions.append(position)
            if raw["coin"] == coin and position.leverage_type == "cross":
                target_raw = raw
        target = next((position for position in positions if position.coin == coin), None)
        if target is None:
            continue
        tracked_notional += target.notional
        if int(raw_account.get("tier", 2)) == 0:
            live_notional += target.notional
        root = (precomputed_roots or {}).get((raw_account["account"], coin))
        if root is None:
            root = joint_liquidation_multiplier(
                float(raw_account["total_raw_usd"]), positions, coin
            )
        if root is None:
            continue
        marginal = (
            target.liquidation_px_reported / target.mark_px
            if target.liquidation_px_reported
            else None
        )
        observed_at = raw_account.get("observed_at")
        if isinstance(observed_at, datetime):
            observed.append(observed_at)
        roots.append(
            {
                "account": raw_account["account"],
                "tier": int(raw_account.get("tier", 2)),
                "direction": "down" if target.szi > 0 else "up",
                "side": "long" if target.szi > 0 else "short",
                "notional": target.notional,
                "root": root,
                "joint_liq_px": root * target.mark_px,
                "marginal": marginal,
                "gap_pct": _joint_closer_by_pp(root, marginal),
                "other_positions": max(0, len(positions) - 1),
                "observed_at": _iso(observed_at) if isinstance(observed_at, datetime) else None,
            }
        )

    mark = float(marks[coin]["mark_px"])
    open_interest_notional = float(marks[coin].get("open_interest_notional") or 0)
    scenarios = []
    for shock in SHOCKS:
        multiplier = 1 - shock / 100
        hit = [row for row in roots if row["direction"] == "down" and multiplier <= row["root"]]
        notional = sum(row["notional"] for row in hit)
        scenarios.append(
            {
                "shock_pct": shock,
                "multiplier": multiplier,
                "price": _money(mark * multiplier),
                "liquidatable_notional_tracked": _money(notional),
                "accounts": len(hit),
                "share_of_tracked": notional / tracked_notional if tracked_notional else 0,
            }
        )

    upside_multiplier = 1.05
    upside_hit = [
        row
        for row in roots
        if row["direction"] == "up" and upside_multiplier >= row["root"]
    ]
    upside_notional = sum(row["notional"] for row in upside_hit)
    upside_5 = {
        "shock_pct": 5,
        "multiplier": upside_multiplier,
        "price": _money(mark * upside_multiplier),
        "liquidatable_notional_tracked": _money(upside_notional),
        "accounts": len(upside_hit),
        "share_of_tracked": (
            upside_notional / tracked_notional if tracked_notional else 0
        ),
    }

    bins: dict[float, list[dict[str, Any]]] = defaultdict(list)
    for row in roots:
        if row["direction"] == "down":
            bins[round(row["root"] * 400) / 400].append(row)
    actionable_cliffs = [
        {
            "multiplier": key,
            "price": _money(mark * key),
            "drop_pct": round((1 - key) * 100, 2),
            "notional": _money(sum(row["notional"] for row in rows)),
            "accounts": len(rows),
        }
        for key, rows in bins.items()
        if 0 < (1 - key) * 100 <= CLIFF_MAX_DROP_PCT
    ]
    # Select material cliffs by size, but present them as an ordered downside
    # ladder so the panel reads from the nearest level to the furthest.
    cliffs = sorted(
        sorted(actionable_cliffs, key=lambda row: row["notional"], reverse=True)[:6],
        key=lambda row: row["drop_pct"],
    )
    as_of = max(observed, default=datetime.now(timezone.utc))
    mark_observed_at = marks[coin].get("observed_at")
    return {
        "mode": mode,
        "as_of": _iso(as_of),
        "coin": coin,
        "coins": _top_market_coins(marks),
        "mark_px": mark,
        # Positions and marks are collected on different cadences, so the mark
        # carries its own timestamp rather than inheriting the position one.
        "mark_as_of": (
            _iso(mark_observed_at) if isinstance(mark_observed_at, datetime) else None
        ),
        "tracked_accounts": len({row["account"] for row in roots}),
        "tracked_notional": _money(tracked_notional),
        "coverage_fraction_open_interest": (
            tracked_notional / open_interest_notional if open_interest_notional else None
        ),
        "share_of_tracked_notional_live": (
            live_notional / tracked_notional if tracked_notional else 0
        ),
        "scenarios": scenarios,
        "upside_5": upside_5,
        "cliffs": cliffs,
        "account_roots": sorted(roots, key=lambda row: row["root"], reverse=True)[:40],
        "limitations": [
            "Figures are lower bounds over tracked accounts, not market totals.",
            "Positions are as of last observation; marks are current at capture.",
            "No liquidity, price-impact, or behavioural-response model is applied.",
        ],
    }


class DemoRepository:
    mode = "demo"

    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.marks = {
            "BTC": {"mark_px": 118_420.0, "prev_day_px": 116_900.0, "open_interest_notional": 5_800_000_000.0, "funding": 0.000018, "observed_at": self.now},
            "ETH": {"mark_px": 4_380.0, "prev_day_px": 4_425.0, "open_interest_notional": 3_100_000_000.0, "funding": -0.000012, "observed_at": self.now},
            "SOL": {"mark_px": 193.4, "prev_day_px": 192.9, "open_interest_notional": 1_250_000_000.0, "funding": 0.000031, "observed_at": self.now},
        }
        self.tiers = {
            "BTC": derive_tiers(
                [
                    {"lowerBound": "0", "maxLeverage": 40},
                    {"lowerBound": "150000000", "maxLeverage": 20},
                ]
            ),
            "ETH": derive_tiers(
                [
                    {"lowerBound": "0", "maxLeverage": 25},
                    {"lowerBound": "100000000", "maxLeverage": 15},
                ]
            ),
            "SOL": derive_tiers([{"lowerBound": "0", "maxLeverage": 20}]),
        }
        specs = [
            ("91af", 0, 7.4, 48, 5_500, 900, 0.983),
            ("2c10", 0, 4.2, -22, 8_000, 2_300, 0.972),
            ("b73e", 0, 3.1, 34, 15_000, 5_200, 0.951),
            ("6dd4", 1, 9.7, 0, 27_000, 10_000, 0.935),
            ("fe82", 1, 2.3, 18, 14_000, 7_800, 0.901),
            ("4a09", 1, 6.0, -40, 42_000, 15_000, 0.862),
            ("cc31", 2, 1.2, 9, 18_000, 8_000, 0.817),
            ("179b", 2, -5.5, 24, 34_000, 12_000, 1.09),
            ("83c6", 2, 12.0, -55, 125_000, 40_000, 0.72),
        ]
        self.accounts = []
        for index, (suffix, tier, btc, eth, cushion, sol, marginal) in enumerate(specs):
            positions = [
                {"coin": "BTC", "szi": btc, "leverage_type": "cross", "liquidation_px_reported": marginal * self.marks["BTC"]["mark_px"]},
                {"coin": "SOL", "szi": sol, "leverage_type": "cross"},
            ]
            if eth:
                positions.append({"coin": "ETH", "szi": eth, "leverage_type": "cross"})
            signed = sum(float(p["szi"]) * self.marks[p["coin"]]["mark_px"] for p in positions)
            current_mm = sum(
                abs(float(p["szi"]) * self.marks[p["coin"]]["mark_px"])
                / (2 * self.tiers[p["coin"]][0].max_leverage)
                for p in positions
            )
            self.accounts.append(
                {
                    "account": "0x" + ("0" * 34) + suffix,
                    "tier": tier,
                    "total_raw_usd": -signed + current_mm + cushion,
                    "observed_at": self.now - timedelta(seconds=index * 17),
                    "positions": positions,
                }
            )
        self._alerts = [
            {"id": 1, "rule": "cliff", "subject": "BTC", "raised_at": _iso(self.now - timedelta(minutes=7)), "detail": "Tracked notional jumps above 3× rolling median near −5%.", "acknowledged": False},
            {"id": 2, "rule": "reconciliation", "subject": "pipeline", "raised_at": _iso(self.now - timedelta(hours=2)), "detail": "MM reconciliation pass rate remains 100% at 1e−6.", "acknowledged": True},
        ]

    def dashboard(self, coin: str) -> dict[str, Any]:
        if coin not in self.marks:
            raise KeyError(coin)
        dashboard = _build_dashboard(
            self.accounts, self.marks, self.tiers, coin, mode=self.mode
        )
        dashboard["markets"] = _build_market_summary(
            self.accounts, self.marks, self.tiers, mode=self.mode
        )
        return dashboard

    def history(self, coin: str, shock_pct: int) -> list[dict[str, Any]]:
        current = next(row for row in self.dashboard(coin)["scenarios"] if row["shock_pct"] == shock_pct)
        base = current["liquidatable_notional_tracked"]
        return [
            {
                "captured_at": _iso(self.now - timedelta(minutes=15 * (15 - index))),
                "notional": _money(base * (0.78 + index * 0.018 + math.sin(index * 0.9) * 0.05)),
            }
            for index in range(16)
        ]

    def alerts(self) -> list[dict[str, Any]]:
        return self._alerts

    def acknowledge(self, alert_id: int) -> bool:
        for alert in self._alerts:
            if alert["id"] == alert_id:
                alert["acknowledged"] = True
                return True
        return False

    def promote(self, address: str) -> None:
        return None


def serverless() -> bool:
    """Vercel sets VERCEL on deployments and under `vercel dev`."""
    default = "true" if os.getenv("VERCEL") else "false"
    return os.getenv("PERPDESK_SERVERLESS", default).lower() in {"1", "true", "yes"}


def _pool_profile() -> dict[str, Any]:
    if not serverless():
        return {}
    # A serverless instance is frozen between requests and gets 500ms after
    # SIGTERM to clean up, so the pool's idle connections are never closed in a
    # way Lakebase observes. Hold few, recycle often, and check liveness on
    # checkout so a connection that died while frozen is replaced, not returned.
    return {
        "min_size": 0,
        "max_size": 2,
        "max_lifetime": 300,
        "check": ConnectionPool.check_connection,
    }


class DatabaseRepository:
    mode = "live"

    def __init__(self, settings: Settings) -> None:
        self.pool = create_pool(settings, **_pool_profile())

    def close(self) -> None:
        self.pool.close()

    def _snapshot(
        self,
    ) -> tuple[
        list[dict],
        dict[str, dict],
        dict[str, tuple[Tier, ...]],
        dict[tuple[str, str], float],
    ]:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT a.account, a.total_raw_usd, a.tier, a.observed_at,
                          p.coin, p.szi, p.leverage_type, p.liquidation_px_reported
                   FROM accounts_current a JOIN positions_current p USING (account)
                   WHERE a.tier IN (0, 1)
                   ORDER BY a.account, p.coin"""
            )
            position_rows = cursor.fetchall()
            cursor.execute(
                """SELECT coin, mark_px, prev_day_px,
                          open_interest * mark_px AS open_interest_notional,
                          funding, observed_at
                   FROM asset_ctx_current"""
            )
            marks = {row["coin"]: dict(row) for row in cursor.fetchall()}
            cursor.execute(
                """SELECT u.coin, t.lower_bound, t.max_leverage
                   FROM meta_universe u JOIN meta_margin_tables t
                     ON t.margin_table_id = u.margin_table_id
                   ORDER BY u.coin, t.tier"""
            )
            raw_tiers: dict[str, list[dict]] = defaultdict(list)
            for row in cursor.fetchall():
                raw_tiers[row["coin"]].append(
                    {"lowerBound": str(row["lower_bound"]), "maxLeverage": row["max_leverage"]}
                )
            cursor.execute(
                "SELECT account, coin, joint_liq_multiplier FROM live_account_joint_liq_px"
            )
            roots = {
                (row["account"], row["coin"]): float(row["joint_liq_multiplier"])
                for row in cursor.fetchall()
            }
        grouped: dict[str, dict[str, Any]] = {}
        for row in position_rows:
            account = grouped.setdefault(
                row["account"],
                {
                    "account": row["account"],
                    "total_raw_usd": row["total_raw_usd"],
                    "tier": row["tier"],
                    "observed_at": row["observed_at"],
                    "positions": [],
                },
            )
            account["positions"].append(dict(row))
        return (
            list(grouped.values()),
            marks,
            {coin: derive_tiers(rows) for coin, rows in raw_tiers.items()},
            roots,
        )

    def dashboard(self, coin: str) -> dict[str, Any]:
        accounts, marks, tiers, roots = self._snapshot()
        if coin not in marks:
            raise KeyError(coin)
        dashboard = _build_dashboard(
            accounts,
            marks,
            tiers,
            coin,
            mode=self.mode,
            precomputed_roots=roots,
        )
        dashboard["markets"] = _build_market_summary(
            accounts,
            marks,
            tiers,
            mode=self.mode,
            precomputed_roots=roots,
        )
        return dashboard

    def history(self, coin: str, shock_pct: int) -> list[dict[str, Any]]:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT to_regclass('public.liquidation_map_history_30d_synced') AS relation"
            )
            if cursor.fetchone()["relation"] is None:
                # The live OLTP map is useful before the Lakeflow pipeline and
                # synced history have been deployed. Treat that deployment
                # stage honestly as "no history yet", not an application error.
                return []
            cursor.execute(
                """SELECT captured_at, liquidatable_notional_tracked AS notional
                   FROM liquidation_map_history_30d_synced
                   WHERE coin = %s AND direction = 'down' AND shock_pct = %s
                   ORDER BY captured_at""",
                (coin, shock_pct),
            )
            return [dict(row) | {"captured_at": _iso(row["captured_at"]), "notional": float(row["notional"])} for row in cursor.fetchall()]

    def alerts(self) -> list[dict[str, Any]]:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM alerts ORDER BY raised_at DESC LIMIT 50")
            return [
                {
                    "id": row["id"], "rule": row["rule"], "subject": row["subject"],
                    "raised_at": _iso(row["raised_at"]), "detail": row["detail"],
                    "acknowledged": row["acknowledged_at"] is not None,
                }
                for row in cursor.fetchall()
            ]

    def acknowledge(self, alert_id: int) -> bool:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE alerts SET acknowledged_at=now(), acknowledged_by=current_user
                   WHERE id=%s AND acknowledged_at IS NULL""",
                (alert_id,),
            )
            return cursor.rowcount == 1

    def promote(self, address: str) -> None:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO accounts_discovered (address, promoted)
                   VALUES (%s, true)
                   ON CONFLICT (address) DO UPDATE SET promoted=true, last_traded=now()""",
                (address.lower(),),
            )
