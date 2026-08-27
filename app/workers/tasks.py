"""
Ejecuta las tareas diarias de expiración de certificados y backups de base de datos.
"""

import anyio
import logging
import os
import subprocess
from datetime import datetime, timezone

from celery import shared_task
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.database import AsyncWorkerSessionLocal
from app.core.settings import get_settings
from app.models import LessonTask  # noqa: F401 — asegura registro del mapper
from app.models.certificate import Certificate
from app.models.certificate_audit import CertificateAudit
from app.models.email_audit import EmailAudit
from app.models.enums import CertificateStatus, CertificateAuditAction, EmailStatus, WorkerStatus
from app.repositories.user_repository import user_repository
from app.services.certificate_storage import CertificateStorageService
from app.utils.certificate_editor import apply_revoked_watermark_pdf
from app.utils.email import send_backup_email_with_audit, send_certificate_expired_email
from app.utils.minio_client import get_backup_minio_client
from app.utils.worker_audit import log_worker_action

logger = logging.getLogger(__name__)

@shared_task(name="app.workers.tasks.check_expired_certificates")
def check_expired_certificates():
    """
    Revisa certificados expirados, aplica marca de agua y actualiza la BD.
    """
    anyio.run(_async_check_expired_certificates)


async def _async_check_expired_certificates():
    settings = get_settings()
    now = datetime.now(timezone.utc)
    started_at = now
    task_name = "check_expired_certificates"
    processed = 0
    errors = []

    async with AsyncWorkerSessionLocal() as session:
        await log_worker_action(
            session, task_name=task_name, status=WorkerStatus.running.value,
            started_at=started_at,
        )

        stmt = select(Certificate).options(
            selectinload(Certificate.user),
            selectinload(Certificate.certificate_type),
        ).where(
            Certificate.status == CertificateStatus.active.value,
            Certificate.expires_at <= now
        )
        result = await session.execute(stmt)
        certificates = result.scalars().all()

        if not certificates:
            await log_worker_action(
                session, task_name=task_name, status=WorkerStatus.success.value,
                started_at=started_at, finished_at=datetime.now(timezone.utc),
                details="No se encontraron certificados expirados",
            )
            await session.commit()
            return

        storage = CertificateStorageService(settings)
        system_user = await user_repository.get_by_email(session, settings.system_bot_user_email)
        system_bot_id = system_user.id if system_user else None

        for cert in certificates:
            uid_str = str(cert.unique_id)

            try:
                raw_pdf = await storage.download_pdf(uid_str)
                watermarked_pdf = apply_revoked_watermark_pdf(raw_pdf, watermark_text="EXPIRADO")
                await storage.upload_pdf(uid_str, watermarked_pdf)
            except Exception as e:
                errors.append(f"Certificado {cert.id}: {e}")
                continue

            cert.status = CertificateStatus.expired.value

            audit = CertificateAudit(
                certificate_id=cert.id,
                certificate_unique_id=cert.unique_id,
                action=CertificateAuditAction.expired.value,
                performed_by=system_bot_id,
            )
            session.add(audit)

            student_user = cert.user
            if student_user and student_user.email:
                email_audit = EmailAudit(
                    user_name=student_user.name,
                    email_to=student_user.email,
                    email_type="certificate_expired",
                )
                session.add(email_audit)
                await session.flush()
                try:
                    await send_certificate_expired_email(
                        email_to=student_user.email,
                        student_name=student_user.name or "Estudiante",
                        certificate_uid=uid_str,
                        certificate_type_name=(
                            cert.certificate_type.name if cert.certificate_type else None
                        ),
                    )
                    email_audit.status = EmailStatus.sent.value
                    email_audit.sent_at = func.now()
                except Exception as e:
                    email_audit.status = EmailStatus.failed.value
                    email_audit.error = str(e)
                    logger.exception("Error enviando correo de expiración a %s", student_user.email)

            processed += 1

        await session.flush()

        details_parts = [f"{processed} certificados expirados"]
        if errors:
            details_parts.append(f"Errores: {'; '.join(errors)}")

        await log_worker_action(
            session, task_name=task_name, status=WorkerStatus.success.value,
            started_at=started_at, finished_at=datetime.now(timezone.utc),
            details=" | ".join(details_parts),
        )

        await session.commit()


@shared_task(name="app.workers.tasks.backup_database_to_minio")
def backup_database_to_minio():
    """
    Realiza un backup de la base de datos usando pg_dump y lo sube a MinIO.
    """
    anyio.run(_async_backup_database_to_minio)


async def _async_backup_database_to_minio():
    settings = get_settings()
    started_at = datetime.now(timezone.utc)
    task_name = "backup_database_to_minio"
    status = WorkerStatus.running.value
    details: str | None = None

    host = settings.postgres_host
    port = settings.postgres_port
    user = settings.postgres_user
    password = settings.postgres_password
    db = settings.postgres_db

    timestamp = started_at.strftime("%Y%m%d_%H%M%S")
    filename = f"backup_{db}_{timestamp}.sql"
    filepath = f"/tmp/{filename}"

    env = os.environ.copy()
    if password:
        env["PGPASSWORD"] = password

    cmd = [
        "pg_dump",
        "-h", str(host),
        "-p", str(port),
        "-U", str(user),
        "-d", str(db),
        "-F", "c",
        "-f", filepath,
    ]

    try:
        async with AsyncWorkerSessionLocal() as session:
            try:
                await log_worker_action(
                    session, task_name=task_name, status=WorkerStatus.running.value,
                    started_at=started_at,
                )
                await session.flush()

                logger.info("Iniciando pg_dump para backup %s", filename)
                subprocess.run(cmd, env=env, check=True, capture_output=True)
                logger.info("pg_dump completado para %s", filename)

                file_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
                email_to = settings.backup_email_to or settings.superuser_email

                backup_client = get_backup_minio_client(settings)
                uploaded = False
                upload_error: str | None = None

                if backup_client is None:
                    upload_error = "MinIO de backup no configurado (MINIO_BACKUP_ACCESS_KEY/SECRET_KEY)"
                    logger.warning(upload_error)
                else:
                    try:
                        backup_client.ensure_bucket()
                        object_name = f"{settings.minio_backup_path_db.strip('/')}/{filename}"
                        backup_client.client.fput_object(
                            backup_client.bucket,
                            object_name,
                            filepath,
                            metadata={
                                "backup_created_at": started_at.isoformat(),
                                "backup_filename": filename,
                            },
                        )
                        uploaded = True
                        details = f"Backup {filename} subido a MinIO externo ({file_size} bytes)"
                        logger.info(details)
                    except Exception as e:
                        upload_error = f"{e}"
                        logger.exception("Error subiendo backup al MinIO externo — %s", filename)

                email_sent = await send_backup_email_with_audit(
                    session,
                    email_to=email_to,
                    filename=filename,
                    size_bytes=file_size,
                    uploaded=uploaded,
                    attachment_path=filepath if not uploaded else None,
                    extra=upload_error,
                )
                await session.flush()

                if uploaded:
                    status = WorkerStatus.success.value
                else:
                    status = WorkerStatus.failed.value
                    details = f"Error en backup: {upload_error}" + (
                        f" | correo de fallback enviado" if email_sent else " | correo de fallback NO enviado"
                    )
                    logger.error(details)

            except subprocess.CalledProcessError as e:
                detail_str = e.stderr.decode("utf-8", errors="replace")
                status = WorkerStatus.failed.value
                details = f"pg_dump falló: {detail_str}"
                logger.error(details)

            except Exception as e:
                status = WorkerStatus.failed.value
                details = f"Error en backup: {e}"
                logger.exception(details)

            await log_worker_action(
                session, task_name=task_name, status=status,
                started_at=started_at, finished_at=datetime.now(timezone.utc),
                details=details,
            )
            await session.commit()

    finally:
        if os.path.exists(filepath):
            os.remove(filepath)
            logger.debug("Archivo temporal %s eliminado", filepath)

    if status == WorkerStatus.failed.value:
        logger.error("Backup falló: %s", details)
