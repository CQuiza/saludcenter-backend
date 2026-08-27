"""Envío de correos electrónicos usando fastapi-mail."""

import logging
import os
import traceback
from datetime import datetime, timezone

from fastapi_mail import ConnectionConfig, FastMail, MessageSchema

from app.core.settings import get_settings
from app.models.enums import EmailStatus
from app.utils.email_templates import credentials_body, expired_body, issued_body, backup_body

logger = logging.getLogger(__name__)

_TRACEBACK_MAX_LEN = 4000
_BACKUP_ATTACHMENT_MAX_MB = 18


def _root_cause_message(exc: Exception) -> str:
    """Devuelve el mensaje de la excepción original recorriendo la cadena __context__.

    fastapi-mail enmascara el error real con una excepción secundaria (p. ej.
    SMTPServerDisconnected tras un SMTPDataError 550). Este helper recupera el
    primer error de la cadena para que la auditoría muestre la causa real.
    """
    current = exc
    while getattr(current, "__context__", None) is not None:
        current = current.__context__
    return str(current) or str(exc)


_mail_config: ConnectionConfig | None = None


def _get_mail_config() -> ConnectionConfig | None:
    global _mail_config
    if _mail_config is not None:
        return _mail_config
    settings = get_settings()
    if not settings.smtp_host:
        return None
    _mail_config = ConnectionConfig(
        MAIL_USERNAME=settings.smtp_user,
        MAIL_PASSWORD=settings.smtp_password,
        MAIL_FROM=settings.email_from,
        MAIL_PORT=settings.smtp_port,
        MAIL_SERVER=settings.smtp_host,
        MAIL_STARTTLS=settings.smtp_tls,
        MAIL_SSL_TLS=False,
        USE_CREDENTIALS=True,
        VALIDATE_CERTS=True,
    )
    return _mail_config


async def send_credentials_email(email_to: str, password: str) -> None:
    """Envía un correo con las credenciales al usuario recién creado."""
    conf = _get_mail_config()
    if conf is None:
        logger.warning("SMTP no configurado. No se envió correo de credenciales a %s", email_to)
        return

    settings = get_settings()
    app_name = settings.project_name
    login_url = f"{settings.base_url.rstrip('/')}/login"
    message = MessageSchema(
        subject=f"Tus credenciales de acceso — {app_name}",
        recipients=[email_to],
        body=credentials_body(app_name, email_to, password, login_url),
        subtype="html",
    )
    fm = FastMail(conf)
    await fm.send_message(message)
    logger.info("Correo de credenciales enviado a %s", email_to)


async def send_certificate_issued_email(
    email_to: str,
    student_name: str,
    certificate_uid: str,
    base_url: str,
    api_prefix: str = "",
    certificate_type_name: str | None = None,
) -> None:
    """Envía un correo notificando la emisión de un certificado."""
    conf = _get_mail_config()
    if conf is None:
        logger.warning("SMTP no configurado. No se envió correo de certificado emitido a %s", email_to)
        return

    settings = get_settings()
    app_name = settings.project_name
    verify_link = f"{base_url.rstrip('/')}{api_prefix}/certificates/view/{certificate_uid}"
    message = MessageSchema(
        subject=f"Tu certificado ha sido emitido — {app_name}",
        recipients=[email_to],
        body=issued_body(app_name, student_name, verify_link, certificate_type_name),
        subtype="html",
    )
    fm = FastMail(conf)
    await fm.send_message(message)
    logger.info("Correo de certificado emitido enviado a %s", email_to)


async def send_certificate_expired_email(
    email_to: str,
    student_name: str,
    certificate_uid: str,
    base_url: str | None = None,
    certificate_type_name: str | None = None,
) -> None:
    """Envía un correo notificando la expiración de un certificado."""
    conf = _get_mail_config()
    if conf is None:
        logger.warning("SMTP no configurado. No se envió correo de certificado expirado a %s", email_to)
        return

    settings = get_settings()
    app_name = settings.project_name

    message = MessageSchema(
        subject=f"Tu certificado ha expirado — {app_name}",
        recipients=[email_to],
        body=expired_body(app_name, student_name, certificate_type_name),
        subtype="html",
    )
    fm = FastMail(conf)
    await fm.send_message(message)
    logger.info("Correo de certificado expirado enviado a %s", email_to)


# ── Wrappers con auditoría para BackgroundTasks ──────────────

from app.core.database import AsyncSessionLocal, AsyncWorkerSessionLocal
from app.models.email_audit import EmailAudit
from app.utils.email_templates import (
    credentials_body,
    expired_body,
    issued_body,
    backup_body,
)


async def send_credentials_with_audit(
    email_to: str,
    password: str,
    user_name: str | None = None,
) -> None:
    """Envía credenciales y registra resultado en email_audit."""
    status = EmailStatus.failed.value
    error_text: str | None = None
    traceback_text: str | None = None
    try:
        await send_credentials_email(email_to, password)
        status = EmailStatus.sent.value
    except Exception as e:
        error_text = _root_cause_message(e)
        traceback_text = traceback.format_exc()[:_TRACEBACK_MAX_LEN]
        logger.exception("Error en send_credentials_with_audit para %s", email_to)

    metadata_: dict[str, object] = {}
    if traceback_text:
        metadata_["traceback"] = traceback_text

    async with AsyncSessionLocal() as session:
        session.add(EmailAudit(
            user_name=user_name,
            email_to=email_to,
            email_type="credentials",
            status=status,
            error=error_text,
            metadata_=metadata_,
            sent_at=datetime.now(timezone.utc) if status == EmailStatus.sent.value else None,
        ))
        await session.commit()


async def send_issued_with_audit(
    email_to: str,
    student_name: str,
    certificate_uid: str,
    base_url: str,
    api_prefix: str,
    user_name: str | None = None,
    certificate_type_name: str | None = None,
) -> None:
    """Notifica emisión de certificado y registra resultado en email_audit."""
    status = EmailStatus.failed.value
    error_text: str | None = None
    traceback_text: str | None = None
    try:
        await send_certificate_issued_email(
            email_to, student_name, certificate_uid, base_url, api_prefix,
            certificate_type_name=certificate_type_name,
        )
        status = EmailStatus.sent.value
    except Exception as e:
        error_text = _root_cause_message(e)
        traceback_text = traceback.format_exc()[:_TRACEBACK_MAX_LEN]
        logger.exception("Error en send_issued_with_audit para %s", email_to)

    metadata_: dict[str, object] = {"certificate_uid": certificate_uid}
    if traceback_text:
        metadata_["traceback"] = traceback_text

    async with AsyncSessionLocal() as session:
        session.add(EmailAudit(
            user_name=user_name,
            email_to=email_to,
            email_type="certificate_issued",
            status=status,
            error=error_text,
            metadata_=metadata_,
            sent_at=datetime.now(timezone.utc) if status == EmailStatus.sent.value else None,
        ))
        await session.commit()


async def send_expired_with_audit(
    email_to: str,
    student_name: str,
    certificate_uid: str,
    user_name: str | None = None,
    certificate_type_name: str | None = None,
) -> None:
    """Notifica expiración de certificado y registra resultado en email_audit."""
    status = EmailStatus.failed.value
    error_text: str | None = None
    traceback_text: str | None = None
    try:
        await send_certificate_expired_email(
            email_to, student_name, certificate_uid,
            certificate_type_name=certificate_type_name,
        )
        status = EmailStatus.sent.value
    except Exception as e:
        error_text = _root_cause_message(e)
        traceback_text = traceback.format_exc()[:_TRACEBACK_MAX_LEN]
        logger.exception("Error en send_expired_with_audit para %s", email_to)

    metadata_: dict[str, object] = {"certificate_uid": certificate_uid}
    if traceback_text:
        metadata_["traceback"] = traceback_text

    async with AsyncSessionLocal() as session:
        session.add(EmailAudit(
            user_name=user_name,
            email_to=email_to,
            email_type="certificate_expired",
            status=status,
            error=error_text,
            metadata_=metadata_,
            sent_at=datetime.now(timezone.utc) if status == EmailStatus.sent.value else None,
        ))
        await session.commit()


async def send_backup_email_with_audit(
    session,
    *,
    email_to: str,
    filename: str,
    size_bytes: int,
    uploaded: bool,
    attachment_path: str | None = None,
    extra: str | None = None,
) -> bool:
    """Envía notificación/fallback de backup y registra el resultado en email_audit.

    Usa la misma sesión (AsyncWorkerSessionLocal) que invoca la tarea del worker
    para evitar sesiones cross-event-loop. Devuelve True si el correo se envió.
    """
    status = EmailStatus.failed.value
    error_text: str | None = None
    traceback_text: str | None = None
    sent = False
    attach_sent = False

    try:
        conf = _get_mail_config()
        if conf is None:
            raise RuntimeError("SMTP no configurado")

        settings = get_settings()
        app_name = settings.project_name

        attachments = []
        if attachment_path and os.path.isfile(attachment_path) and os.access(attachment_path, os.R_OK):
            size = os.path.getsize(attachment_path)
            if size <= _BACKUP_ATTACHMENT_MAX_MB * 1024 * 1024:
                attachments.append(attachment_path)
                attach_sent = True
            else:
                extra = f"{extra} — " if extra else ""
                extra = f"{extra}adjunto omitido: supera {_BACKUP_ATTACHMENT_MAX_MB} MB"

        message = MessageSchema(
            subject=f"Backup de base de datos — {app_name}",
            recipients=[email_to],
            body=backup_body(
                app_name,
                filename=filename,
                size_bytes=size_bytes,
                uploaded=uploaded,
                extra=extra,
            ),
            subtype="html",
            attachments=attachments,
        )
        fm = FastMail(conf)
        await fm.send_message(message)
        status = EmailStatus.sent.value
        sent = True
        logger.info("Correo de backup enviado a %s (adjunto=%s)", email_to, attach_sent)
    except Exception as e:
        error_text = _root_cause_message(e)
        traceback_text = traceback.format_exc()[:_TRACEBACK_MAX_LEN]
        logger.exception("Error en send_backup_email_with_audit para %s", email_to)

    metadata_: dict[str, object] = {
        "filename": filename,
        "size_bytes": size_bytes,
        "uploaded": uploaded,
        "attachment_sent": attach_sent,
    }
    if traceback_text:
        metadata_["traceback"] = traceback_text

    session.add(EmailAudit(
        user_name="backup",
        email_to=email_to,
        email_type="backup_database",
        status=status,
        error=error_text,
        metadata_=metadata_,
        sent_at=datetime.now(timezone.utc) if sent else None,
    ))
    return sent
