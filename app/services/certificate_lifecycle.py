"""Orquestación del ciclo de vida de certificados."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.models.certificate import Certificate
from app.models.certificate_audit import CertificateAudit
from app.models.enums import CertificateAuditAction, CertificateStatus, UserRole
from app.models.user import User
from app.repositories.certificate_repository import certificate_repository
from app.repositories.certificate_type_repository import certificate_type_repository
from app.repositories.user_repository import user_repository
from app.services.certificate_notification import CertificateNotificationService
from app.services.certificate_pdf import CertificatePdfService
from app.services.certificate_storage import CertificateStorageService
from app.services.datetime_utils import compute_certificate_expires_at
from app.utils.helpers import student_display_name

logger = logging.getLogger(__name__)


class CertificateLifecycleService:
    """Coordina almacenamiento, PDF y notificaciones para el ciclo de vida."""

    def __init__(
        self,
        storage: CertificateStorageService | None = None,
        pdf: CertificatePdfService | None = None,
        notification: CertificateNotificationService | None = None,
    ) -> None:
        self._storage = storage or CertificateStorageService()
        self._pdf = pdf or CertificatePdfService()
        self._notification = notification or CertificateNotificationService()

    async def issue_certificate(
        self,
        db: AsyncSession,
        *,
        admin: User,
        user_id: int,
        certificate_type_id: int,
        issued_at: datetime | None = None,
        validity_extension: int | None = None,
        hours: int | None = None,
        background_tasks=None,
    ):
        """Crea certificado, genera PDF, sube a MinIO, audita y notifica."""
        logger.info("Emitiendo certificado — admin=%s, user_id=%s, ct_id=%s",
                     admin.email, user_id, certificate_type_id)
        if admin.role not in (UserRole.superuser.value, UserRole.admin.value):
            logger.warning("Permiso denegado — admin=%s role=%s", admin.email, admin.role)
            raise PermissionError("Solo administradores pueden emitir certificados")

        ct = await certificate_type_repository.get_by_id(db, certificate_type_id)
        if not ct:
            raise ValueError("Tipo de certificado no existe")

        student = await user_repository.get_by_id(db, user_id)
        if not student or student.role != UserRole.student.value:
            raise ValueError("El usuario destino debe ser estudiante")

        if issued_at is not None:
            if issued_at.tzinfo is None:
                issued_at = issued_at.replace(tzinfo=UTC)
        else:
            issued_at = datetime.now(UTC)

        vt = ct.validity_type
        vv = ct.validity_value
        if validity_extension is not None:
            vt = "years"
            vv = validity_extension
        expires_at = compute_certificate_expires_at(issued_at, vt, vv)

        validity_years = None
        if validity_extension is not None:
            validity_years = validity_extension
        elif ct.validity_type == "years":
            validity_years = ct.validity_value

        settings = get_settings()

        base = settings.base_url.rstrip("/")
        api = settings.api_v1_prefix.rstrip("/")

        cert = await certificate_repository.create(
            db,
            certificate_type_id=certificate_type_id,
            user_id=user_id,
            issued_at=issued_at,
            expires_at=expires_at,
            status=CertificateStatus.active.value,
            qr_code_url=None,
            pdf_url=None,
        )
        uid = str(cert.unique_id)
        cert.pdf_url = f"{base}{api}/certificates/view/{uid}"
        cert.qr_code_url = f"{base}{api}/certificates/view/{uid}/qr"
        if hours is not None:
            cert.hours = hours
        if validity_extension is not None:
            cert.validity_years = validity_extension
        await db.flush()
        await db.refresh(cert)

        verify_url = f"{base}/search?identity={student.identity_number}"
        pdf_bytes, qr_bytes = self._pdf.generate(
            student, ct, issued_at, verify_url, settings,
            validity_years=validity_years,
            hours=hours,
        )
        await self._storage.upload_certificate_files(uid, pdf_bytes, qr_bytes)

        db.add(
            CertificateAudit(
                certificate_id=cert.id,
                certificate_unique_id=cert.unique_id,
                action=CertificateAuditAction.issued.value,
                performed_by=admin.id,
            )
        )
        await db.flush()

        if background_tasks:
            self._notification.notify_issued(
                student.email,
                student_display_name(student),
                uid,
                base,
                api,
                background_tasks,
                certificate_type_name=ct.name,
            )

        logger.info("Certificado emitido — uid=%s, student=%s, ct=%s",
                     uid, student.email, ct.name)
        return cert

    async def revoke_certificate(
        self,
        db: AsyncSession,
        *,
        admin: User,
        cert: Certificate,
    ) -> Certificate:
        """Revoca un certificado: cambia estado, marca agua en PDF, audita."""
        uid = str(cert.unique_id)
        logger.info("Revocando certificado — uid=%s, admin=%s", uid, admin.email)
        updated = await certificate_repository.update(
            db, cert, {"status": CertificateStatus.revoked.value}
        )
        db.add(
            CertificateAudit(
                certificate_id=updated.id,
                certificate_unique_id=updated.unique_id,
                action=CertificateAuditAction.revoked.value,
                performed_by=admin.id,
            )
        )
        await db.flush()

        try:
            raw = await self._storage.download_pdf(uid)
            settings = get_settings()
            stamped = CertificatePdfService.apply_watermark(
                raw, settings.certificate_revoked_watermark_text
            )
            await self._storage.upload_pdf(uid, stamped)
            logger.info("Certificado revocado — uid=%s", uid)
        except Exception as exc:
            logger.exception("Error al aplicar marca REVOCADO en pdf — uid=%s", uid)
            msg = "No se pudo actualizar el PDF en MinIO con la marca REVOCADO."
            raise RuntimeError(msg) from exc

        return updated

    async def activate_certificate(
        self,
        db: AsyncSession,
        *,
        admin: User,
        cert: Certificate,
    ) -> Certificate:
        """Reactiva un certificado: cambia estado, regenera PDF, audita."""
        uid = str(cert.unique_id)
        logger.info("Reactivating certificate — uid=%s, admin=%s", uid, admin.email)
        updated = await certificate_repository.update(
            db, cert, {"status": CertificateStatus.active.value}
        )
        db.add(
            CertificateAudit(
                certificate_id=updated.id,
                certificate_unique_id=updated.unique_id,
                action=CertificateAuditAction.active.value,
                performed_by=admin.id,
            )
        )
        await db.flush()

        try:
            pdf_bytes = await self._pdf.regenerate(db, updated)
            await self._storage.upload_pdf(uid, pdf_bytes)
            logger.info("Certificado reactivado — uid=%s", uid)
        except Exception as exc:
            logger.exception("Error al regenerar/restaurar PDF — uid=%s", uid)
            msg = "No se pudo restaurar el PDF en MinIO sin marca de agua."
            raise RuntimeError(msg) from exc

        return updated

    async def renew_certificate(
        self,
        db: AsyncSession,
        *,
        admin: User,
        cert: Certificate,
        issued_at: datetime | None = None,
        validity_extension: int | None = None,
        hours: int | None = None,
        background_tasks=None,
    ) -> Certificate:
        """Renueva un certificado como reemplazo EN SU LUGAR (misma UUID), sin revocar.

        Actualiza fecha de emisión, vigencia e intensidad horaria (override o default
        del tipo) y regenera el PDF sobre el mismo objeto. El certificado original
        nunca pasa a 'revoked' ni se marca con agua.
        """
        if cert.status not in (
            CertificateStatus.active.value,
            CertificateStatus.expired.value,
        ):
            raise ValueError("Solo pueden renovarse certificados activos o expirados")
        if cert.user_id is None or cert.certificate_type_id is None:
            raise ValueError("El certificado no tiene usuario o tipo asociado")

        ct = await certificate_type_repository.get_by_id(db, cert.certificate_type_id)
        if not ct:
            raise ValueError("Tipo de certificado no existe")

        if issued_at is not None:
            if issued_at.tzinfo is None:
                issued_at = issued_at.replace(tzinfo=UTC)
        else:
            issued_at = datetime.now(UTC)

        vt = ct.validity_type
        vv = ct.validity_value
        if validity_extension is not None:
            vt = "years"
            vv = validity_extension
        expires_at = compute_certificate_expires_at(issued_at, vt, vv)

        cert.issued_at = issued_at
        cert.expires_at = expires_at
        if hours is not None:
            cert.hours = hours
        if validity_extension is not None:
            cert.validity_years = validity_extension
        await db.flush()
        await db.refresh(cert)

        uid = str(cert.unique_id)
        pdf_bytes = await self._pdf.regenerate(db, cert)
        await self._storage.upload_pdf(uid, pdf_bytes)

        db.add(
            CertificateAudit(
                certificate_id=cert.id,
                certificate_unique_id=cert.unique_id,
                action=CertificateAuditAction.renewed.value,
                performed_by=admin.id,
            )
        )
        await db.flush()

        student = await user_repository.get_by_id(db, cert.user_id)
        if student and background_tasks:
            settings = get_settings()
            base = settings.base_url.rstrip("/")
            api = settings.api_v1_prefix.rstrip("/")
            self._notification.notify_issued(
                student.email,
                student_display_name(student),
                uid,
                base,
                api,
                background_tasks,
                certificate_type_name=ct.name,
            )

        logger.info("Certificado renovado (en su lugar) — uid=%s, ct=%s",
                     uid, ct.name)
        return cert

    async def reproduce_active_for_student(
        self,
        db: AsyncSession,
        *,
        student_id: int,
        admin: User,
    ) -> int:
        """Reproduce los PDFs de los certificados ACTIVOS del estudiante con sus datos actuales.

        Regenera en su lugar (misma UUID, mismas fechas/status) y audita 'reproduced'.
        Se usa al actualizar nombre o datos de identidad del estudiante.
        """
        certs = await certificate_repository.list_by_user(
            db, student_id, statuses=[CertificateStatus.active.value]
        )
        count = 0
        for cert in certs:
            uid = str(cert.unique_id)
            pdf_bytes = await self._pdf.regenerate(db, cert)
            await self._storage.upload_pdf(uid, pdf_bytes)
            db.add(
                CertificateAudit(
                    certificate_id=cert.id,
                    certificate_unique_id=cert.unique_id,
                    action=CertificateAuditAction.reproduced.value,
                    performed_by=admin.id,
                )
            )
            await db.flush()
            count += 1
        if count:
            logger.info("Certificados reproducidos — student_id=%s, count=%s", student_id, count)
        return count

    async def update_certificate_fields(
        self,
        db: AsyncSession,
        *,
        admin: User,
        cert: Certificate,
        fields: dict[str, object],
    ) -> Certificate:
        """Actualiza metadatos del certificado sin cambiar estado ni tocar MinIO."""
        logger.info("Actualizando campos certificado — uid=%s, fields=%s, admin=%s",
                     str(cert.unique_id), set(fields), admin.email)
        updated = await certificate_repository.update(db, cert, fields)
        logger.info("Campos actualizados — uid=%s", str(updated.unique_id))
        return updated

    async def delete_certificate(
        self,
        db: AsyncSession,
        *,
        admin: User,
        cert: Certificate,
    ) -> None:
        """Elimina certificado: archivos MinIO, auditoría y registro BD."""
        uid = str(cert.unique_id)
        logger.info("Eliminando certificado — uid=%s, admin=%s", uid, admin.email)
        try:
            await self._storage.delete_certificate_files(uid)
        except Exception as exc:
            logger.exception("Error al eliminar archivos MinIO — uid=%s", uid)
            msg = "No se pudieron eliminar los archivos del certificado en MinIO."
            raise RuntimeError(msg) from exc
        db.add(
            CertificateAudit(
                certificate_id=cert.id,
                certificate_unique_id=cert.unique_id,
                action=CertificateAuditAction.deleted.value,
                performed_by=admin.id,
            ),
        )
        await db.flush()
        await certificate_repository.delete(db, cert)
        logger.info("Certificado eliminado — uid=%s", uid)


certificate_lifecycle = CertificateLifecycleService()
