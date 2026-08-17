-- PerpDesk v3 - Lakebase schema.
-- Run in the Lakebase SQL editor on the project's production branch (NOT the sidebar SQL editor,
-- which talks to the SQL warehouse and will fail in confusing ways).
--
-- Design note, and it is the load-bearing one:
--   Every table here is CURRENT STATE, one row per entity, upserted. Nothing grows with time
--   except accounts_discovered (which has a retention policy at the bottom of this file).
--
--   The reason is that Lakebase CDF cannot start on Free Edition - the workspace catalog is
--   Databricks-managed Default Storage, an unsupported CDF destination - so Postgres reaches
--   Delta through a Unity Catalog foreign catalog instead. A federated read re-scans the whole
--   table on every pipeline refresh. An append-only history table would therefore drag its entire
--   contents across JDBC every few minutes, growing without bound.
--
--   So: Postgres holds current state (~20k rows, flat forever), and history accumulates in Delta
--   where it is cheap. See pipelines/perpdesk.py and the map-history append task.

-- ---------------------------------------------------------------------------------------------
-- Reference data from the `meta` info endpoint. Refreshed hourly by the collector.
-- ---------------------------------------------------------------------------------------------

-- The universe of perpetual assets. margin_table_id is the join key to meta_margin_tables, and
-- resolving it is not a plain join - see the fallback note on that table.
CREATE TABLE IF NOT EXISTS meta_universe (
  coin             text PRIMARY KEY,
  sz_decimals      int         NOT NULL,
  max_leverage     int         NOT NULL,
  margin_table_id  int         NOT NULL,
  only_isolated    boolean     NOT NULL DEFAULT false,
  is_delisted      boolean     NOT NULL DEFAULT false,
  fetched_at       timestamptz NOT NULL DEFAULT now()
);

-- Margin tier tables, exploded from meta.marginTables.
--
-- THE TRAP: meta.marginTables does not contain every id that meta.universe references. On
-- 2026-08-15 mainnet, universe referenced ids [3, 5, 10, 20, 51..56] but marginTables only
-- contained [50..56]. A bare id N means "single untiered tier at max_leverage = N" (ATOM has
-- margin_table_id 5 and max_leverage 5). The collector synthesises those rows on write, so this
-- table is always complete and downstream can use a plain join. See perpdesk/margin.py.
--
-- An inner join against the raw API response instead would silently drop most of the 232 coins
-- from the liquidation map, and the pipeline would stay green while doing it.
CREATE TABLE IF NOT EXISTS meta_margin_tables (
  margin_table_id  int           NOT NULL,
  tier             int           NOT NULL,   -- 0-based
  lower_bound      numeric(38,8) NOT NULL,   -- notional USD at which this tier starts
  max_leverage     int           NOT NULL,
  synthesised      boolean       NOT NULL DEFAULT false,  -- true = fallback, not from marginTables
  fetched_at       timestamptz   NOT NULL DEFAULT now(),
  PRIMARY KEY (margin_table_id, tier)
);

-- ---------------------------------------------------------------------------------------------
-- Live market state. One row per coin, upserted every minute from metaAndAssetCtxs.
-- ---------------------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS asset_ctx_current (
  coin           text PRIMARY KEY REFERENCES meta_universe(coin),
  mark_px        numeric(38,18) NOT NULL,
  prev_day_px    numeric(38,18),
  oracle_px      numeric(38,18),
  -- Mark price derives from validator-published oracle prices every 3s, each a weighted median
  -- across Binance/OKX/Bybit/Kraken/KuCoin/Gate/MEXC and Hyperliquid spot, then a stake-weighted
  -- median across validators. It is not manipulable by this system or any participant in it.
  -- That is what makes this a risk instrument rather than a targeting instrument.
  funding        numeric(38,18),
  open_interest  numeric(38,18),   -- in coin units; multiply by mark_px for notional
  premium        numeric(38,18),
  day_ntl_volume numeric(38,18),
  observed_at    timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------------------------
-- Account state. THE hot tables - read by the app on every page load and scanned by the pipeline
-- on every refresh. Bounded by tracked account count, not by time.
-- ---------------------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS accounts_current (
  account                 text PRIMARY KEY,

  -- Cash leg. Changes only on trade, funding settlement, deposit or withdrawal - NOT on every
  -- mark tick. This is what lets the collector skip ~2,400 pointless writes/min.
  total_raw_usd           numeric(38,18) NOT NULL,

  -- What the exchange reported at observation time. Kept so the pipeline can prove its own
  -- reconstruction against it (see the two expectations in pipelines/perpdesk.py). These are
  -- the ground truth in the reconciliation, not inputs to the model.
  account_value_reported  numeric(38,18) NOT NULL,
  cross_mm_reported       numeric(38,18) NOT NULL,

  -- Hash over (coin, szi, entry_px, leverage, margin_mode) for every position plus
  -- total_raw_usd. The collector writes only when this changes.
  book_hash               text NOT NULL,

  tier                    smallint    NOT NULL DEFAULT 2,  -- 0 = WS, 1/2 = REST, 3 = inactive
  cross_notional          numeric(38,18) NOT NULL DEFAULT 0,  -- tier scoring input
  observed_at             timestamptz NOT NULL DEFAULT now(),
  -- Set when the row came from the periodic full sweep rather than a change event. The
  -- reconciliation expectations filter on this: a periodic row is marked at a known-fresh
  -- moment, so AV drift against the exchange is measurable rather than confounded by staleness.
  is_periodic             boolean     NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS accounts_current_tier_idx      ON accounts_current (tier, observed_at);
CREATE INDEX IF NOT EXISTS accounts_current_notional_idx  ON accounts_current (cross_notional DESC);

CREATE TABLE IF NOT EXISTS positions_current (
  account                  text NOT NULL,
  coin                     text NOT NULL,

  szi                      numeric(38,18) NOT NULL,  -- SIGNED size. negative = short.
  entry_px                 numeric(38,18),
  position_value           numeric(38,18) NOT NULL,  -- absolute notional at observation
  margin_used              numeric(38,18) NOT NULL,
  unrealized_pnl           numeric(38,18) NOT NULL,
  leverage_value           numeric(38,18),
  leverage_type            text NOT NULL,            -- 'cross' | 'isolated'
  leverage_raw_usd         numeric(38,18),           -- isolated margin, when isolated
  max_leverage             int,

  -- The API's own per-position liquidation price. NOT an input to the model - it is what the
  -- joint solver is validated against, and the gap between the two is the project's finding.
  -- It holds every other price fixed, which is wrong for a cross-margin account.
  liquidation_px_reported  numeric(38,18),

  observed_at              timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (account, coin)
);

CREATE INDEX IF NOT EXISTS positions_current_coin_idx ON positions_current (coin, position_value DESC);

-- ---------------------------------------------------------------------------------------------
-- Control plane + discovery.
-- ---------------------------------------------------------------------------------------------

-- Written by the trades WS firehose. The account universe builds itself and skews toward ACTIVE
-- accounts, which are the ones that matter. This is the only table that grows with time; see the
-- retention statement at the bottom.
CREATE TABLE IF NOT EXISTS accounts_discovered (
  address      text PRIMARY KEY,
  first_seen   timestamptz NOT NULL DEFAULT now(),
  last_traded  timestamptz NOT NULL DEFAULT now(),
  trade_count  bigint      NOT NULL DEFAULT 1,
  promoted     boolean     NOT NULL DEFAULT false  -- has it been pulled into accounts_current yet
);

CREATE INDEX IF NOT EXISTS accounts_discovered_promote_idx
  ON accounts_discovered (promoted, last_traded DESC);

-- The collector reads its control plane from here every cycle, so the app can change behaviour
-- and the collector picks it up seconds later. That loop - change a setting in the app, watch the
-- map change - is the demo.
CREATE TABLE IF NOT EXISTS collector_config (
  key         text PRIMARY KEY,
  value       text        NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS poll_cursor (
  tier          smallint PRIMARY KEY,
  last_address  text,
  updated_at    timestamptz NOT NULL DEFAULT now()
);

-- Collector self-reporting. Surfaced in the app so the demo can show the system describing its
-- own health rather than asserting it.
CREATE TABLE IF NOT EXISTS ingest_stats (
  id                    bigserial PRIMARY KEY,
  observed_at           timestamptz NOT NULL DEFAULT now(),
  weight_used_last_min  int,
  ws_msgs_last_min      int,
  accounts_tracked      int,
  writes_skipped        int,   -- book_hash unchanged; the whole point of the change detection
  writes_applied        int
);

CREATE TABLE IF NOT EXISTS alerts (
  id               bigserial PRIMARY KEY,
  raised_at        timestamptz NOT NULL DEFAULT now(),
  rule             text NOT NULL,
  subject          text NOT NULL,   -- coin, or account address
  detail           jsonb,
  acknowledged_by  text,
  acknowledged_at  timestamptz
);

CREATE INDEX IF NOT EXISTS alerts_open_idx ON alerts (raised_at DESC) WHERE acknowledged_at IS NULL;

-- ---------------------------------------------------------------------------------------------
-- Retention. accounts_discovered is the only unbounded table; run this from the collector daily.
-- ---------------------------------------------------------------------------------------------
-- DELETE FROM accounts_discovered
--  WHERE last_traded < now() - interval '30 days' AND NOT promoted;
