"""Transactional, idempotent writes for current-state tables."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg_pool import ConnectionPool


def upsert_meta(pool: ConnectionPool, universe: list[dict], tiers: list[dict]) -> None:
    with pool.connection() as connection, connection.transaction(), connection.cursor() as cursor:
        cursor.executemany(
            """INSERT INTO meta_universe
               (coin, sz_decimals, max_leverage, margin_table_id, only_isolated, is_delisted, fetched_at)
               VALUES (%(coin)s, %(sz_decimals)s, %(max_leverage)s, %(margin_table_id)s,
                       %(only_isolated)s, %(is_delisted)s, %(fetched_at)s)
               ON CONFLICT (coin) DO UPDATE SET
                 sz_decimals=EXCLUDED.sz_decimals, max_leverage=EXCLUDED.max_leverage,
                 margin_table_id=EXCLUDED.margin_table_id, only_isolated=EXCLUDED.only_isolated,
                 is_delisted=EXCLUDED.is_delisted, fetched_at=EXCLUDED.fetched_at""",
            universe,
        )
        cursor.executemany(
            """INSERT INTO meta_margin_tables
               (margin_table_id, tier, lower_bound, max_leverage, synthesised, fetched_at)
               VALUES (%(margin_table_id)s, %(tier)s, %(lower_bound)s, %(max_leverage)s,
                       %(synthesised)s, %(fetched_at)s)
               ON CONFLICT (margin_table_id, tier) DO UPDATE SET
                 lower_bound=EXCLUDED.lower_bound, max_leverage=EXCLUDED.max_leverage,
                 synthesised=EXCLUDED.synthesised, fetched_at=EXCLUDED.fetched_at""",
            tiers,
        )


def upsert_asset_contexts(pool: ConnectionPool, rows: list[dict]) -> None:
    with pool.connection() as connection, connection.transaction(), connection.cursor() as cursor:
        cursor.executemany(
            """INSERT INTO asset_ctx_current
               (coin, mark_px, prev_day_px, oracle_px, funding, open_interest, premium, day_ntl_volume, observed_at)
               VALUES (%(coin)s, %(mark_px)s, %(prev_day_px)s, %(oracle_px)s, %(funding)s, %(open_interest)s,
                       %(premium)s, %(day_ntl_volume)s, %(observed_at)s)
               ON CONFLICT (coin) DO UPDATE SET
                 mark_px=EXCLUDED.mark_px, prev_day_px=EXCLUDED.prev_day_px,
                 oracle_px=EXCLUDED.oracle_px,
                 funding=EXCLUDED.funding, open_interest=EXCLUDED.open_interest,
                 premium=EXCLUDED.premium, day_ntl_volume=EXCLUDED.day_ntl_volume,
                 observed_at=EXCLUDED.observed_at""",
            rows,
        )


def upsert_account_book(pool: ConnectionPool, account: dict, positions: list[dict]) -> bool:
    """Replace one account's current book atomically; return False when unchanged.

    Closed-position deletion is inside the same transaction as the upserts. This
    prevents a closed position from surviving indefinitely in the risk map.
    """
    with pool.connection() as connection, connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT book_hash FROM accounts_current WHERE account = %s FOR UPDATE",
            (account["account"],),
        )
        existing = cursor.fetchone()
        if existing and existing["book_hash"] == account["book_hash"] and not account["is_periodic"]:
            return False
        cursor.execute(
            """INSERT INTO accounts_current
               (account, total_raw_usd, account_value_reported, cross_mm_reported, book_hash,
                tier, cross_notional, observed_at, is_periodic)
               VALUES (%(account)s, %(total_raw_usd)s, %(account_value_reported)s,
                       %(cross_mm_reported)s, %(book_hash)s, %(tier)s, %(cross_notional)s,
                       %(observed_at)s, %(is_periodic)s)
               ON CONFLICT (account) DO UPDATE SET
                 total_raw_usd=EXCLUDED.total_raw_usd,
                 account_value_reported=EXCLUDED.account_value_reported,
                 cross_mm_reported=EXCLUDED.cross_mm_reported, book_hash=EXCLUDED.book_hash,
                 tier=EXCLUDED.tier, cross_notional=EXCLUDED.cross_notional,
                 observed_at=EXCLUDED.observed_at, is_periodic=EXCLUDED.is_periodic""",
            account,
        )
        coins = [position["coin"] for position in positions]
        cursor.execute(
            "DELETE FROM positions_current WHERE account = %s AND NOT (coin = ANY(%s))",
            (account["account"], coins),
        ) if coins else cursor.execute(
            "DELETE FROM positions_current WHERE account = %s", (account["account"],)
        )
        if positions:
            cursor.executemany(
                """INSERT INTO positions_current
                   (account, coin, szi, entry_px, position_value, margin_used, unrealized_pnl,
                    leverage_value, leverage_type, leverage_raw_usd, max_leverage,
                    liquidation_px_reported, observed_at)
                   VALUES (%(account)s, %(coin)s, %(szi)s, %(entry_px)s, %(position_value)s,
                           %(margin_used)s, %(unrealized_pnl)s, %(leverage_value)s,
                           %(leverage_type)s, %(leverage_raw_usd)s, %(max_leverage)s,
                           %(liquidation_px_reported)s, %(observed_at)s)
                   ON CONFLICT (account, coin) DO UPDATE SET
                     szi=EXCLUDED.szi, entry_px=EXCLUDED.entry_px,
                     position_value=EXCLUDED.position_value, margin_used=EXCLUDED.margin_used,
                     unrealized_pnl=EXCLUDED.unrealized_pnl,
                     leverage_value=EXCLUDED.leverage_value,
                     leverage_type=EXCLUDED.leverage_type,
                     leverage_raw_usd=EXCLUDED.leverage_raw_usd,
                     max_leverage=EXCLUDED.max_leverage,
                     liquidation_px_reported=EXCLUDED.liquidation_px_reported,
                     observed_at=EXCLUDED.observed_at""",
                positions,
            )
        return True


def discovered(pool: ConnectionPool, addresses: list[str]) -> None:
    rows = [{"address": address.lower()} for address in set(addresses)]
    if not rows:
        return
    with pool.connection() as connection, connection.transaction(), connection.cursor() as cursor:
        cursor.executemany(
            """INSERT INTO accounts_discovered (address) VALUES (%(address)s)
               ON CONFLICT (address) DO UPDATE SET
                 last_traded=now(), trade_count=accounts_discovered.trade_count + 1""",
            rows,
        )


def seed_discovered(pool: ConnectionPool, addresses: Sequence[str]) -> int:
    """Bulk seed addresses without pretending they were observed trading.

    COPY into a transaction-local staging table avoids issuing hundreds of
    thousands of individual INSERT statements. Existing discoveries are left
    untouched, including their trade counts and promotion state.
    """
    if not addresses:
        return 0
    with pool.connection() as connection, connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE wallet_seed (address text PRIMARY KEY) ON COMMIT DROP"
        )
        with cursor.copy("COPY wallet_seed (address) FROM STDIN") as copy:
            for address in addresses:
                copy.write_row((address,))
        cursor.execute(
            """INSERT INTO accounts_discovered
                 (address, first_seen, last_traded, trade_count, promoted)
               SELECT address, now(), now(), 0, false
               FROM wallet_seed
               ON CONFLICT (address) DO NOTHING"""
        )
        return cursor.rowcount


def tracked_accounts(pool: ConnectionPool, tier: int, limit: int) -> list[str]:
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT account FROM accounts_current WHERE tier = %s
               ORDER BY observed_at ASC LIMIT %s""",
            (tier, limit),
        )
        return [row["account"] for row in cursor.fetchall()]


def rebalance_account_tiers(
    pool: ConnectionPool, *, tracked_wallets: int, t0_size: int
) -> dict[int, int]:
    """Keep the largest accounts active and divide them between WS and REST.

    Tier 3 is retained as inactive current state so automatically discovered
    wallets do not immediately re-enter the cold-start queue.
    """
    with pool.connection() as connection, connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            """WITH ranked AS (
                   SELECT account,
                          row_number() OVER (
                            ORDER BY cross_notional DESC, account
                          ) AS rank
                   FROM accounts_current
               )
               UPDATE accounts_current AS account
               SET tier = CASE
                   WHEN ranked.rank <= %s THEN 0
                   WHEN ranked.rank <= %s THEN 1
                   ELSE 3
               END
               FROM ranked
               WHERE account.account = ranked.account
                 AND account.tier IS DISTINCT FROM CASE
                   WHEN ranked.rank <= %s THEN 0
                   WHEN ranked.rank <= %s THEN 1
                   ELSE 3
                 END""",
            (t0_size, tracked_wallets, t0_size, tracked_wallets),
        )
        cursor.execute(
            """SELECT tier, count(*) AS count
               FROM accounts_current
               GROUP BY tier
               ORDER BY tier"""
        )
        counts = {int(row["tier"]): int(row["count"]) for row in cursor.fetchall()}
        cursor.execute(
            """DELETE FROM positions_current AS position
               USING accounts_current AS account
               WHERE position.account = account.account
                 AND account.tier = 3"""
        )
        return counts


def top_market_coins(pool: ConnectionPool, limit: int = 10) -> list[str]:
    """Largest active perp markets by public open-interest notional."""
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT coin
               FROM asset_ctx_current
               WHERE open_interest > 0 AND mark_px > 0
               ORDER BY open_interest * mark_px DESC, coin
               LIMIT %s""",
            (limit,),
        )
        return [row["coin"] for row in cursor.fetchall()]


def promoted_accounts(pool: ConnectionPool, limit: int = 25) -> list[str]:
    """New app promotions that do not have an account-state row yet."""
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT d.address
               FROM accounts_discovered d
               LEFT JOIN accounts_current a ON a.account = d.address
               WHERE d.promoted AND a.account IS NULL
               ORDER BY d.last_traded DESC LIMIT %s""",
            (limit,),
        )
        return [row["address"] for row in cursor.fetchall()]


def discovery_candidates(pool: ConnectionPool, limit: int) -> list[str]:
    """Newest active discoveries that have not entered the tracked tail yet."""
    if limit <= 0:
        return []
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT d.address
               FROM accounts_discovered d
               LEFT JOIN accounts_current a ON a.account = d.address
               WHERE NOT d.promoted AND a.account IS NULL
               ORDER BY d.last_traded DESC, d.trade_count DESC
               LIMIT %s""",
            (limit,),
        )
        return [row["address"] for row in cursor.fetchall()]


def mark_discovery_tracked(pool: ConnectionPool, address: str) -> None:
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE accounts_discovered SET promoted=true WHERE address=%s",
            (address.lower(),),
        )


def write_ingest_stats(pool: ConnectionPool, **values: Any) -> None:
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO ingest_stats
               (weight_used_last_min, ws_msgs_last_min, accounts_tracked,
                writes_skipped, writes_applied)
               VALUES (%(weight_used_last_min)s, %(ws_msgs_last_min)s, %(accounts_tracked)s,
                       %(writes_skipped)s, %(writes_applied)s)""",
            values,
        )
