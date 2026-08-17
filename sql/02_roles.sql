-- PerpDesk v3 - service principal Postgres role.
-- Run in the Lakebase SQL editor AFTER creating the service principal in
-- Settings -> Identity and access -> Service principals -> Add (name it `perpdesk-collector`,
-- enable Workspace access, generate an OAuth secret - the secret is shown once).
--
-- Why this file exists as a separate step from creating the principal: those are two different
-- systems. Creating the principal registers the identity in Databricks. This file teaches
-- Postgres that it exists. Skip this and you will authenticate to Databricks successfully,
-- receive a valid token, and then watch Postgres reject it because the role does not exist.
--
-- The payoff, and it belongs in the write-up: there is no database password anywhere in this
-- system. Unity Catalog is the Postgres access control. The SDK exchanges the principal's OAuth
-- secret for a 60-minute database credential that is minted fresh on every connection - which is
-- why perpdesk/collector/db.py subclasses the psycopg connection rather than building a
-- connection string once at startup.

\set client_id '00000000-0000-0000-0000-000000000000'  -- <-- replace with your client ID

CREATE EXTENSION IF NOT EXISTS databricks_auth;

SELECT databricks_create_role(:'client_id', 'SERVICE_PRINCIPAL');

GRANT CONNECT ON DATABASE databricks_postgres TO :"client_id";
GRANT USAGE  ON SCHEMA public                 TO :"client_id";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"client_id";

-- Load-bearing. alerts and ingest_stats use bigserial, and without USAGE on the sequence the
-- insert fails with a permission error that does not mention sequences and takes twenty minutes
-- to diagnose.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"client_id";

ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"client_id";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO :"client_id";

-- Check: connect with the collector and run
--   SELECT current_user, current_database();
-- You should see the client ID UUID and databricks_postgres. If current_user is your email
-- address instead, the SDK picked up your personal credentials rather than the service
-- principal - confirm all three DATABRICKS_* vars are actually in the environment.
