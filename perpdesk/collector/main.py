"""Collector entry point. ``cycle`` is single-shot; ``main`` owns repetition."""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .api import HyperliquidInfo
from .bucket import WeightBucket
from .config import Settings
from .db import create_pool
from .discovery import run as run_discovery
from .normalize import normalize_account_state, normalize_asset_contexts, normalize_meta
from .state_ws import run as run_state_ws
from .writer import (
    discovery_candidates,
    discovered,
    mark_discovery_tracked,
    promoted_accounts,
    rebalance_account_tiers,
    seed_discovered,
    top_market_coins,
    tracked_accounts,
    upsert_account_book,
    upsert_asset_contexts,
    upsert_meta,
    write_ingest_stats,
)
from .wallets import read_wallet_file

LOGGER = logging.getLogger(__name__)


async def cycle(
    settings: Settings,
    api: HyperliquidInfo,
    pool,
    *,
    refresh_meta: bool = False,
    periodic: bool = False,
) -> dict[str, int]:
    """Perform one bounded REST pass and return collector counters."""
    await asyncio.to_thread(
        rebalance_account_tiers,
        pool,
        tracked_wallets=settings.tracked_wallets,
        t0_size=settings.t0_size,
    )
    if refresh_meta:
        universe, tiers = normalize_meta(await api.meta())
        await asyncio.to_thread(upsert_meta, pool, universe, tiers)
    contexts = normalize_asset_contexts(await api.meta_and_asset_ctxs())
    await asyncio.to_thread(upsert_asset_contexts, pool, contexts)

    new_t0 = await asyncio.to_thread(promoted_accounts, pool)
    # T1_SIZE is the population covered over five minutes, not the amount to
    # pull every minute. Ordering by oldest observation makes this round-robin.
    t1_population = min(
        settings.t1_size,
        max(0, settings.tracked_wallets - settings.t0_size),
    )
    t1_batch = math.ceil(t1_population * settings.cycle_sec / 300)
    t1 = await asyncio.to_thread(tracked_accounts, pool, 1, t1_batch)
    base_weight = 20 + (20 if refresh_meta else 0)
    base_t1_count = len(t1)
    tail_limit = max(
        0,
        (
            settings.weight_budget
            - base_weight
            - 2 * len(new_t0)
            - base_t1_count * 2
        )
        // 2,
    )
    # Give a configurable share of the tail budget to never-scanned addresses.
    # Unused cold-start capacity immediately falls back to T2 refreshes below.
    discovery_limit = math.floor(tail_limit * settings.new_account_share)
    candidates = await asyncio.to_thread(
        discovery_candidates, pool, discovery_limit
    )
    unused_discovery = discovery_limit - len(candidates)
    if unused_discovery > 0:
        t1 = await asyncio.to_thread(
            tracked_accounts,
            pool,
            1,
            min(t1_population, base_t1_count + unused_discovery),
        )
    extra_t1_count = len(t1) - base_t1_count
    t2 = await asyncio.to_thread(
        tracked_accounts,
        pool,
        2,
        max(0, tail_limit - len(candidates) - extra_t1_count),
    )
    semaphore = asyncio.Semaphore(settings.rest_concurrency)
    counters = {"applied": 0, "skipped": 0}

    async def pull(address: str, tier: int) -> None:
        async with semaphore:
            state = await api.clearinghouse_state(address)
        account, positions = normalize_account_state(
            address, state, tier=tier, periodic=periodic
        )
        changed = await asyncio.to_thread(upsert_account_book, pool, account, positions)
        if address in candidates:
            await asyncio.to_thread(mark_discovery_tracked, pool, address)
        counters["applied" if changed else "skipped"] += 1

    results = await asyncio.gather(
        *(
            pull(address, tier)
            for tier, addresses in (
                (0, new_t0),
                (1, t1),
                (2, candidates + t2),
            )
            for address in addresses
        ),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, Exception):
            LOGGER.error("account pull failed", exc_info=result)
    tier_counts = await asyncio.to_thread(
        rebalance_account_tiers,
        pool,
        tracked_wallets=settings.tracked_wallets,
        t0_size=settings.t0_size,
    )
    await asyncio.to_thread(
        write_ingest_stats,
        pool,
        weight_used_last_min=base_weight
        + 2 * (len(new_t0) + len(t1) + len(candidates) + len(t2)),
        ws_msgs_last_min=0,
        accounts_tracked=sum(
            count for tier, count in tier_counts.items() if tier < 3
        ),
        writes_skipped=counters["skipped"],
        writes_applied=counters["applied"],
    )
    return counters


async def run_forever(settings: Settings, *, once: bool = False) -> None:
    pool = create_pool(settings)
    bucket = WeightBucket(settings.weight_budget)
    async with httpx.AsyncClient(timeout=15) as client:
        api = HyperliquidInfo(settings.info_url, bucket, client)
        refresh_count = 0
        background: list[asyncio.Task] = []
        try:
            if not once:
                background.append(
                    asyncio.create_task(
                        run_discovery(
                            settings.ws_url,
                            lambda: top_market_coins(pool),
                            lambda rows: discovered(pool, rows),
                        )
                    )
                )

                async def state_update(address: str, state: dict) -> None:
                    account, positions = normalize_account_state(address, state, tier=0)
                    await asyncio.to_thread(upsert_account_book, pool, account, positions)

                background.append(
                    asyncio.create_task(
                        run_state_ws(
                            settings.ws_url,
                            lambda: tracked_accounts(pool, 0, settings.t0_size),
                            state_update,
                        )
                    )
                )
            while True:
                started = datetime.now(timezone.utc)
                periodic_every = max(
                    1, settings.periodic_snapshot_sec // settings.cycle_sec
                )
                await cycle(
                    settings,
                    api,
                    pool,
                    refresh_meta=refresh_count % 60 == 0,
                    periodic=refresh_count % periodic_every == 0,
                )
                refresh_count += 1
                if once:
                    break
                elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                await asyncio.sleep(max(0, settings.cycle_sec - elapsed))
        finally:
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect public Hyperliquid risk state")
    parser.add_argument("--once", action="store_true", help="run one REST cycle and exit")
    parser.add_argument(
        "--wallet-file",
        type=Path,
        help="seed a newline-delimited public-address file before collecting",
    )
    parser.add_argument(
        "--import-only",
        action="store_true",
        help="load --wallet-file into Lakebase and exit without an API cycle",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.import_only and args.wallet_file is None:
        parser.error("--import-only requires --wallet-file")
    settings = Settings.from_env()
    try:
        if args.wallet_file is not None:
            wallet_file = read_wallet_file(args.wallet_file)
            pool = create_pool(settings)
            try:
                inserted = seed_discovered(pool, wallet_file.addresses)
            finally:
                pool.close()
            LOGGER.info(
                "wallet seed complete: lines=%d unique=%d duplicates=%d inserted=%d",
                wallet_file.total_lines,
                len(wallet_file.addresses),
                wallet_file.duplicate_lines,
                inserted,
            )
        if not args.import_only:
            asyncio.run(run_forever(settings, once=args.once))
    except KeyboardInterrupt:
        LOGGER.info("collector stopped")


if __name__ == "__main__":
    main()
