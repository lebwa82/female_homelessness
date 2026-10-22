"""Versioned PostgreSQL schema for documents and historical issuance evidence."""

from sqlalchemy import text

SCHEMA_LOCK_KEY = 6823044142847411255
HISTORY_SCHEMA_VERSION = 1


async def initialize_schema(connection) -> None:
    """Migrate atomically on the caller's transaction, serializing app startups."""
    await connection.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": SCHEMA_LOCK_KEY}
    )
    await connection.execute(text("""
        CREATE TABLE IF NOT EXISTS women_help_certificate_schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """))
    applied = (await connection.execute(text("""
        SELECT 1 FROM women_help_certificate_schema_migrations WHERE version = :version
    """), {"version": HISTORY_SCHEMA_VERSION})).first()
    if applied is not None:
        return
    await connection.execute(text("""
        ALTER TABLE women_help_pdf_certificates
            ADD COLUMN record_kind VARCHAR(32) NOT NULL DEFAULT 'certificate',
            ADD COLUMN account_id BIGINT,
            ADD COLUMN contact_id BIGINT,
            ADD COLUMN conversation_id BIGINT,
            ADD COLUMN source_message_id BIGINT,
            ADD COLUMN accepted_at TIMESTAMPTZ,
            ALTER COLUMN pool_id DROP NOT NULL,
            ALTER COLUMN nominal_rubles DROP NOT NULL,
            ALTER COLUMN activation_code DROP NOT NULL,
            ALTER COLUMN serial_number DROP NOT NULL,
            ALTER COLUMN expires_at DROP NOT NULL,
            ALTER COLUMN pdf_bucket DROP NOT NULL,
            ALTER COLUMN pdf_object_key DROP NOT NULL,
            ALTER COLUMN pdf_sha256 DROP NOT NULL,
            ALTER COLUMN pdf_size DROP NOT NULL,
            ALTER COLUMN pdf_filename DROP NOT NULL,
            DROP CONSTRAINT women_help_pdf_certificates_status_check
    """))
    await connection.execute(text("""
        ALTER TABLE women_help_pdf_certificates
            ADD CONSTRAINT women_help_pdf_certificates_record_check CHECK (
                (record_kind = 'certificate'
                 AND pool_id IS NOT NULL AND nominal_rubles IS NOT NULL
                 AND activation_code IS NOT NULL AND serial_number IS NOT NULL
                 AND expires_at IS NOT NULL AND pdf_bucket IS NOT NULL
                 AND pdf_object_key IS NOT NULL AND pdf_sha256 IS NOT NULL
                 AND pdf_size IS NOT NULL AND pdf_filename IS NOT NULL
                 AND status IN ('available','reserved','submitted','delivered','failed')
                 AND account_id IS NULL AND contact_id IS NULL
                 AND conversation_id IS NULL AND source_message_id IS NULL
                 AND accepted_at IS NULL)
                OR
                (record_kind = 'historical_issuance' AND status = 'accepted'
                 AND account_id IS NOT NULL AND account_id > 0
                 AND contact_id IS NOT NULL AND contact_id > 0
                 AND conversation_id IS NOT NULL AND conversation_id > 0
                 AND source_message_id IS NOT NULL AND source_message_id > 0
                 AND accepted_at IS NOT NULL AND recipient_hash IS NOT NULL
                 AND issuance_key IS NULL AND nominal_rubles IS NULL
                 AND activation_code IS NULL AND serial_number IS NULL
                 AND valid_from IS NULL AND expires_at IS NULL
                 AND pdf_bucket IS NULL AND pdf_object_key IS NULL
                 AND pdf_version_id IS NULL AND pdf_sha256 IS NULL
                 AND pdf_size IS NULL AND pdf_filename IS NULL AND NOT is_test
                 AND chatwoot_message_id IS NULL AND reserved_at IS NULL
                 AND submitted_at IS NULL AND delivered_at IS NULL)
            )
    """))
    await connection.execute(text("""
        CREATE UNIQUE INDEX uq_women_help_pdf_certificates_history_source
        ON women_help_pdf_certificates (account_id, source_message_id)
        WHERE record_kind = 'historical_issuance'
    """))
    await connection.execute(text("""
        INSERT INTO women_help_certificate_schema_migrations (version) VALUES (:version)
    """), {"version": HISTORY_SCHEMA_VERSION})
