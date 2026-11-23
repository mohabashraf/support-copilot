-- Read-only role for Grafana (D6). It can read the tickets_monitoring VIEW and nothing else:
-- not the tickets table (masked message, draft, final text), not pii_vault, not the KB.
-- Idempotent: safe to run again. Local dev password; production: a secret from the vault/KMS.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro') THEN
    CREATE ROLE grafana_ro LOGIN PASSWORD 'grafana_ro';
  END IF;
END $$;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;
-- The view is created by the app (tickets/store.py), which also grants SELECT on it to this role.
DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_views WHERE viewname = 'tickets_monitoring') THEN
    GRANT SELECT ON tickets_monitoring TO grafana_ro;
  END IF;
END $$;
