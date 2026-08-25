DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'enterprise_rag_app') THEN
    CREATE ROLE enterprise_rag_app LOGIN PASSWORD 'enterprise_rag_app_dev';
  END IF;
END
$$;

GRANT CONNECT ON DATABASE enterprise_rag TO enterprise_rag_app;
GRANT USAGE ON SCHEMA public TO enterprise_rag_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO enterprise_rag_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO enterprise_rag_app;
ALTER DEFAULT PRIVILEGES FOR ROLE enterprise_rag IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO enterprise_rag_app;
ALTER DEFAULT PRIVILEGES FOR ROLE enterprise_rag IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO enterprise_rag_app;
