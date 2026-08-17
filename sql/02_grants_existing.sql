-- Run as the Lakebase admin after 01_schema.sql when the tables were created by the admin.
-- Replace <client-id> in every quoted identifier with the PGUSER UUID from .env.

GRANT CONNECT ON DATABASE databricks_postgres TO "<client-id>";
GRANT USAGE ON SCHEMA public TO "<client-id>";

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "<client-id>";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "<client-id>";

-- Preserve access for future tables and sequences created by this same admin role.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "<client-id>";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO "<client-id>";
