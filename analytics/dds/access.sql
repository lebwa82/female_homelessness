-- One-time reviewed operator template. psql --set=database=<reviewed_database> --file=...
-- No passwords; configure login authentication through the approved secret workflow.
-- Run only after schema deployment and a separate production approval.
BEGIN;
CREATE ROLE dds_analyst NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE dds_reader LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    NOBYPASSRLS CONNECTION LIMIT 2;
GRANT dds_analyst TO dds_reader;
GRANT CONNECT ON DATABASE :"database" TO dds_analyst;
GRANT USAGE ON SCHEMA dds TO dds_analyst;
GRANT SELECT ON TABLE
    dds.dim_contact,
    dds.dim_inbox,
    dds.dim_staff,
    dds.dim_aid,
    dds.dim_certificate_pool,
    dds.dim_label,
    dds.dim_tag,
    dds.bridge_contact_inbox,
    dds.bridge_pool_aid,
    dds.bridge_certificate_request_event,
    dds.bridge_label_tag,
    dds.bridge_object_tag,
    dds.fact_conversation,
    dds.fact_message,
    dds.fact_consultation_request,
    dds.fact_followup,
    dds.fact_certificate,
    dds.fact_certificate_request,
    dds.fact_csat
TO dds_analyst;
ALTER ROLE dds_reader SET default_transaction_read_only='on';
ALTER ROLE dds_reader SET statement_timeout='15s';
ALTER ROLE dds_reader SET idle_in_transaction_session_timeout='30s';
ALTER ROLE dds_reader SET work_mem='4MB';
ALTER ROLE dds_reader SET max_parallel_workers_per_gather=0;
-- Effective access may be inherited through PUBLIC; do not revoke global application grants blindly.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname NOT IN ('dds','pg_catalog','information_schema')
          AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%'
          AND c.relkind IN ('r','v','m','p','f')
          AND (has_table_privilege('dds_reader',c.oid,'SELECT')
            OR has_any_column_privilege('dds_reader',c.oid,'SELECT'))
    ) THEN
        RAISE EXCEPTION 'DDS role can read nonanalytical tables; review PUBLIC and role grants';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog','information_schema')
          AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%'
          AND c.relkind IN ('r','v','m','p','f')
          AND (has_table_privilege('dds_reader',c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
            OR has_any_column_privilege('dds_reader',c.oid,'INSERT,UPDATE,REFERENCES'))
    ) THEN
        RAISE EXCEPTION 'DDS reader inherited write privileges; review PUBLIC and role grants';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND p.prosecdef
          AND has_function_privilege('dds_reader',p.oid,'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Review executable SECURITY DEFINER functions before granting access';
    END IF;
END $$;
COMMIT;
