"""Shared construction of certificate storage services for admin commands."""

from app.certificate_documents import S3CertificateObjectStore
from app.chatwoot.certificates import CertificateInventory, database_url
from app.config import settings


def services() -> tuple[CertificateInventory, S3CertificateObjectStore]:
    if not settings.certificate_database_password or not settings.certificate_identity_key:
        raise RuntimeError("certificate database and identity keys are required")
    if error := settings.certificate_s3_configuration_error():
        raise RuntimeError(error)
    inventory = CertificateInventory(
        database_url(settings.certificate_database_password),
        identity_key=settings.certificate_identity_key,
        account_id=settings.chatwoot_account_id,
    )
    store = S3CertificateObjectStore(
        bucket=settings.certificate_s3_bucket,
        endpoint_url=settings.certificate_s3_endpoint,
        region=settings.certificate_s3_region,
        access_key_id=settings.certificate_s3_access_key_id,
        secret_access_key=settings.certificate_s3_secret_access_key,
    )
    return inventory, store
