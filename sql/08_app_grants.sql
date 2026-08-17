-- Run once in the Lakebase SQL editor as the database owner after the
-- `perpdesk` Databricks App has been created.
--
-- Find the app service-principal client ID with:
--   databricks apps get perpdesk --output json |
--     jq -r '.service_principal_client_id'
-- Replace every <app-client-id> below with that UUID.

GRANT CONNECT ON DATABASE databricks_postgres TO "<app-client-id>";
GRANT USAGE ON SCHEMA public TO "<app-client-id>";

-- The dashboard reads current state, computed views, alerts, and synced
-- history. Read access to other public tables is harmless and avoids a new
-- migration each time a read-only dashboard view is added.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO "<app-client-id>";

-- These are the only two write paths exposed by the FastAPI application.
GRANT INSERT, UPDATE ON TABLE accounts_discovered TO "<app-client-id>";
GRANT UPDATE ON TABLE alerts TO "<app-client-id>";

-- Preserve read access for future tables created by the current admin role.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO "<app-client-id>";
