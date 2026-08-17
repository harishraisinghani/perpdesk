-- The history chart reads a Lakebase synced table. First keep the Delta source
-- bounded to the latest 30 days, then create the sync in Catalog Explorer with:
--   source: workspace.perpdesk.gold_liquidation_map_history_30d
--   target catalog/schema: perpdesk_pg.public
--   target table: liquidation_map_history_30d_synced
--   primary key: map_key, captured_at
--   scheduling: snapshot
--   pipeline storage: workspace.perpdesk
--
-- The resulting Postgres relation must be exactly:
--   public.liquidation_map_history_30d_synced
-- Synced tables are owned by a Databricks writer role, so after the first sync
-- grant the app identity access in the Lakebase SQL editor:
--   GRANT SELECT ON TABLE public.liquidation_map_history_30d_synced TO "<client-id>";
CREATE OR REPLACE MATERIALIZED VIEW workspace.perpdesk.gold_liquidation_map_history_30d AS
SELECT *
FROM workspace.perpdesk.gold_liquidation_map_history
WHERE captured_at >= current_timestamp() - INTERVAL 30 DAYS;
