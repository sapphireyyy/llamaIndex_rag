DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'enterprise_rag_app') THEN
    CREATE ROLE enterprise_rag_app LOGIN;
  END IF;
END
$$;

-- The wrapper passes the password through the PostgreSQL client variable
-- `runtime_password`; no credential is stored in this SQL template.
ALTER ROLE enterprise_rag_app PASSWORD :'runtime_password';

-- The application account must never become a privileged role or table owner.
ALTER ROLE enterprise_rag_app NOSUPERUSER NOBYPASSRLS NOINHERIT;
REVOKE ALL ON DATABASE enterprise_rag FROM enterprise_rag_app;

GRANT CONNECT ON DATABASE enterprise_rag TO enterprise_rag_app;
GRANT USAGE ON SCHEMA public TO enterprise_rag_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO enterprise_rag_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO enterprise_rag_app;
ALTER DEFAULT PRIVILEGES FOR ROLE enterprise_rag IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO enterprise_rag_app;
ALTER DEFAULT PRIVILEGES FOR ROLE enterprise_rag IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO enterprise_rag_app;
