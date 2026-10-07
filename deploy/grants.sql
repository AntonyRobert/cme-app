-- Applied by deploy.sh after every migrate, as the owner role, with
-- :app set to the app role. Idempotent. The app role (what gunicorn runs as)
-- gets exactly what serving requests needs and no DDL:
--   every table: SELECT, INSERT, UPDATE, DELETE
--   except the audit log: SELECT and INSERT only. Audit rows are never changed
--   or removed by the application, and the database says so too.
--   every sequence: USAGE (for the few integer ids left) and SELECT.
GRANT USAGE ON SCHEMA public TO :"app";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"app";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"app";
REVOKE UPDATE, DELETE, TRUNCATE ON TABLE audit_auditlog FROM :"app";
-- Tables a future migrate creates get the same grants without a new line here;
-- the audit exception is re-applied by the REVOKE above on the next deploy.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO :"app";
