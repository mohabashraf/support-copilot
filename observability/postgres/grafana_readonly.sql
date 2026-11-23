-- Read-only role for Grafana (D6). It can read the four tickets_* monitoring VIEWS and nothing else:
-- not the tickets table (masked message, draft, final text), not pii_vault, not the KB.
-- Idempotent: safe to run again. Local dev password; production: a secret from the vault/KMS.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro') THEN
    CREATE ROLE grafana_ro LOGIN PASSWORD 'grafana_ro';
  END IF;
END $$;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;
-- The views are created by the app (tickets/store.py), which also grants SELECT on them. This
-- script runs first in Docker (no views yet) and again in tests: it must re-grant EVERY monitoring
-- view, or the REVOKE above silently breaks the Grafana panels that read them.
DO $$
DECLARE v text;
BEGIN
  FOREACH v IN ARRAY ARRAY['tickets_monitoring', 'tickets_pii_detections', 'tickets_steps',
                           'tickets_risk_signals'] LOOP
    IF EXISTS (SELECT FROM pg_views WHERE viewname = v) THEN
      EXECUTE format('GRANT SELECT ON %I TO grafana_ro', v);
    END IF;
  END LOOP;
END $$;
