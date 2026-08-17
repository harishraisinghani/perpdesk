-- Job task 2, after the Lakeflow pipeline refresh task succeeds.
-- Keep history in Delta; never append history to federated Postgres current-state tables.
INSERT INTO workspace.perpdesk.gold_liquidation_map_history
SELECT * FROM workspace.perpdesk.gold_liquidation_map;

-- The bounded source used by the Lakebase synced table is a standalone
-- materialized view, so refresh it after every append before the sync task runs.
REFRESH MATERIALIZED VIEW workspace.perpdesk.gold_liquidation_map_history_30d SYNC;
