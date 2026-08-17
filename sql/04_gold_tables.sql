-- Run once after the pipeline creates its gold materialized views.
-- gold_liquidation_map is owned by Lakeflow and is a materialized view, not a
-- mutable Delta table. Lakeflow manages its backing-table features, so do not
-- apply ALTER TABLE properties or constraints to it here.

CREATE TABLE IF NOT EXISTS workspace.perpdesk.gold_liquidation_map_history
USING DELTA
TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true')
AS SELECT * FROM workspace.perpdesk.gold_liquidation_map WHERE false;

COMMENT ON COLUMN workspace.perpdesk.gold_liquidation_map.liquidatable_notional_tracked IS
  'Lower bound over tracked public accounts whose cross account value falls below maintenance margin at this shock; not expected liquidation volume and not a market total.';
COMMENT ON COLUMN workspace.perpdesk.gold_liquidation_map.coverage_fraction_open_interest IS
  'Tracked liquidatable notional divided by current public open-interest notional; use this beside every risk number.';
