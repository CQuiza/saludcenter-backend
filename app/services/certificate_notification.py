"""Notificaciones por correo relacionadas con certificados."""

from __future__ import annotations

from app.utils.email import send_issued_with_audit


class CertificateNotificationService:
    """Programa envío de correos vía BackgroundTasks."""

    @staticmethod
    def notify_issued(
        student_email: str,
        student_name: str,
        certificate_uid: str,
        base_url: str,
        api_prefix: str,
        background_tasks,
        user_name: str | None = None,
        certificate_type_name: str | None = None,
    ) -> None:
        background_tasks.add_task(
            send_issued_with_audit,
            student_email,
            student_name,
            certificate_uid,
            base_url,
            api_prefix,
            user_name or student_name,
            certificate_type_name,
        )
