-- Run once as the Lakebase table owner before restarting the collector or app.
-- Hyperliquid returns prevDayPx in metaAndAssetCtxs; the scanner uses it only
-- as a prior-day reference for a transparent momentum-confirmation rule.

ALTER TABLE asset_ctx_current
  ADD COLUMN IF NOT EXISTS prev_day_px numeric(38,18);

COMMENT ON COLUMN asset_ctx_current.prev_day_px IS
  'Hyperliquid prevDayPx reference from metaAndAssetCtxs; used to calculate the scanner one-day spot trend.';
