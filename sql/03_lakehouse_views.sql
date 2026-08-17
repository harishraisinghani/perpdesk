-- PerpDesk v3 - the contract exposed to Unity Catalog federation.
--
-- Lakebase CDF is unavailable on Free Edition (the workspace catalog is Databricks-managed
-- Default Storage, an unsupported CDF destination). So Postgres reaches Delta through a read-only
-- Unity Catalog foreign catalog instead:
--
--     Catalog -> External Data -> Connections -> new PostgreSQL connection to this Lakebase
--     endpoint, then create a foreign catalog `perpdesk_pg` over it.
--
-- The pipeline reads THESE VIEWS, never the base tables. A view is a stable contract: the tables
-- underneath can be reshaped without breaking the pipeline, and the pipeline cannot accidentally
-- become coupled to a column that was meant to be internal.
--
-- Everything here is numeric(38,x) or text, both of which federate to Spark cleanly. (ChainPulse
-- needed a view to hide a numeric(78,0) column that exceeds Spark's 38-digit DECIMAL ceiling.
-- Hyperliquid returns decimal strings, so PerpDesk has no such column - the view earns its place
-- on the stable-contract argument alone.)

CREATE OR REPLACE VIEW positions_lakehouse AS
SELECT
  p.account,
  p.coin,
  p.szi,
  p.entry_px,
  p.position_value,
  p.margin_used,
  p.unrealized_pnl,
  p.leverage_value,
  p.leverage_type,
  p.leverage_raw_usd,
  p.liquidation_px_reported,
  p.observed_at,
  u.margin_table_id,
  u.sz_decimals
FROM positions_current p
JOIN accounts_current a ON a.account = p.account AND a.tier IN (0, 1)
JOIN meta_universe u ON u.coin = p.coin;

CREATE OR REPLACE VIEW accounts_lakehouse AS
SELECT
  account,
  total_raw_usd,
  account_value_reported,
  cross_mm_reported,
  tier,
  cross_notional,
  observed_at,
  is_periodic
FROM accounts_current
WHERE tier IN (0, 1);

CREATE OR REPLACE VIEW asset_ctx_lakehouse AS
SELECT
  a.coin,
  a.mark_px,
  a.prev_day_px,
  a.oracle_px,
  a.funding,
  a.open_interest,
  a.open_interest * a.mark_px AS open_interest_notional,
  a.premium,
  a.day_ntl_volume,
  a.observed_at,
  u.max_leverage,
  u.margin_table_id,
  u.only_isolated,
  u.is_delisted
FROM asset_ctx_current a
JOIN meta_universe u ON u.coin = a.coin;

-- Always complete: the collector synthesises the single-tier fallback rows for margin table ids
-- that meta.marginTables omits, so downstream can plain-join this and trust it.
CREATE OR REPLACE VIEW margin_tiers_lakehouse AS
SELECT margin_table_id, tier, lower_bound, max_leverage, synthesised, fetched_at
FROM meta_margin_tables;

-- Check, from a Databricks notebook once the foreign catalog exists:
--   SELECT count(*) FROM perpdesk_pg.public.positions_lakehouse;
--   SELECT count(*) FROM perpdesk_pg.public.margin_tiers_lakehouse;
-- Do this BEFORE building the collector. Registering a foreign catalog is a connection and a
-- grant, and finding out it reads your views correctly while the schema is still cheap to change
-- is worth twenty minutes.
