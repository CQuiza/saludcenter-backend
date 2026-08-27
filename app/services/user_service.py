"""Usuarios y credenciales."""

import logging
import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_password_hash, verify_password
from app.models.enums import UserRole
from app.models.user import User
from app.repositories.user_repository import user_repository
from app.schemas.user import UserCreate, UserUpdate
from app.utils.email import send_credentials_with_audit

logger = logging.getLogger(__name__)

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


class UserService:
    @staticmethod
    def _is_placeholder_phone(value: str | None) -> bool:
        """True si el teléfono está vacío o es solo ceros (placeholder)."""
        digits = (value or "").strip().replace("+", "").replace("-", "")
        return not digits or set(digits) == {"0"}

    @staticmethod
    async def _generate_unique_phone_number(db: AsyncSession) -> str:
        """Genera un teléfono sintético único (+000 + 10 dígitos aleatorios)."""
        rng = random.SystemRandom()
        for _ in range(100):
            candidate = f"+000{rng.randint(0, 9_999_999_999):010d}"
            if not await user_repository.get_by_phone_number(db, candidate):
                return candidate
        raise ValueError("No se pudo generar un número de teléfono único")

    async def authenticate(self, db: AsyncSession, email: str, password: str) -> User | None:
        user = await user_repository.get_by_email(db, email)
        if not user or not user.is_active:
            logger.warning("Auth fallida — email=%s: usuario no encontrado o inactivo", email)
            return None

        if user.locked_until and user.locked_until > datetime.now(UTC):
            remaining = user.locked_until - datetime.now(UTC)
            minutes = max(1, (remaining.seconds + 59) // 60)
            logger.warning("Auth bloqueada — email=%s: cuenta bloqueada hasta %s", email, user.locked_until)
            msg = (
                f"Cuenta bloqueada por intentos fallidos. "
                f"Intente de nuevo en {minutes} minuto{'s' if minutes != 1 else ''}."
            )
            raise PermissionError(msg)

        if not verify_password(password, user.password_hash):
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
                user.locked_until = datetime.now(UTC) + timedelta(minutes=LOCKOUT_MINUTES)
                logger.warning("Cuenta bloqueada — email=%s: %s intentos fallidos", email, user.failed_login_attempts)
            db.add(user)
            await db.flush()
            logger.warning("Auth fallida — email=%s: contraseña incorrecta (intento %s)", email, user.failed_login_attempts)
            return None

        user.failed_login_attempts = 0
        user.locked_until = None
        db.add(user)
        await db.flush()
        logger.info("Auth exitosa — email=%s, id=%s", email, user.id)
        return user

    async def create_user(self, db: AsyncSession, data: UserCreate, *,
                          background_tasks: object | None = None,
                          requesting_role: str | None = None) -> User:
        logger.info("Creando usuario — email=%s, role=%s, requested_by_role=%s",
                     data.email, data.role.value, requesting_role)
        if data.role == UserRole.superuser and requesting_role != UserRole.superuser.value:
            logger.warning("Intento de crear superuser sin permiso — email=%s", data.email)
            raise ValueError("Solo un superusuario puede crear otro superusuario")
        existing_email = await user_repository.get_by_email(db, data.email)
        if existing_email:
            logger.warning("Email duplicado — email=%s", data.email)
            raise ValueError("El correo ya está registrado")

        existing_identity = await user_repository.get_by_identity_number(db, data.identity_number)
        if existing_identity:
            logger.warning("Identidad duplicada — identity=%s", data.identity_number)
            raise ValueError("El número de identidad ya está registrado")

        phone = data.phone_number
        if self._is_placeholder_phone(phone):
            phone = await self._generate_unique_phone_number(db)
        else:
            existing_phone = await user_repository.get_by_phone_number(db, phone)
            if existing_phone:
                logger.warning("Teléfono duplicado — phone=%s", phone)
                raise ValueError("El número de teléfono ya está registrado")

        hashed = get_password_hash(data.password)
        user = await user_repository.create(
            db,
            email=data.email,
            password_hash=hashed,
            name=data.name,
            first_last_name=data.first_last_name,
            second_last_name=data.second_last_name,
            role=data.role.value,
            identity_type=data.identity_type.value,
            identity_number=data.identity_number,
            phone_number=phone,
            is_active=data.is_active,
        )
        if background_tasks:
            background_tasks.add_task(
                send_credentials_with_audit,
                data.email, data.password, user.name,
            )
        logger.info("Usuario creado — id=%s, email=%s, role=%s", user.id, user.email, user.role)
        return user

    async def update_user(self, db: AsyncSession, user: User, data: UserUpdate, *, requesting_role: str | None = None) -> User:
        payload = data.model_dump(exclude_unset=True)
        logger.info("Actualizando usuario — id=%s, email=%s, fields=%s",
                     user.id, user.email, set(payload))

        if user.role == UserRole.superuser.value and requesting_role != UserRole.superuser.value:
            logger.warning("Intento de modificar superuser sin permiso — target=%s", user.email)
            raise ValueError("Solo un superusuario puede modificar a otro superusuario")

        if "role" in payload:
            new_role = payload["role"]
            if isinstance(new_role, UserRole):
                new_role = new_role.value
            if new_role == UserRole.superuser.value and requesting_role != UserRole.superuser.value:
                logger.warning("Intento de asignar superuser sin permiso — target=%s", user.email)
                raise ValueError("Solo un superusuario puede asignar el rol superusuario")

        if "email" in payload and payload["email"] != user.email:
            existing = await user_repository.get_by_email(db, payload["email"])
            if existing:
                logger.warning("Email duplicado al actualizar — email=%s", payload["email"])
                raise ValueError("El correo ya está registrado")

        if "identity_number" in payload and payload["identity_number"] != user.identity_number:
            existing = await user_repository.get_by_identity_number(db, payload["identity_number"])
            if existing:
                logger.warning("Identidad duplicada al actualizar — identity=%s", payload["identity_number"])
                raise ValueError("El número de identidad ya está registrado")

        if "phone_number" in payload:
            new_phone = payload["phone_number"]
            if self._is_placeholder_phone(new_phone):
                payload.pop("phone_number")
            elif new_phone != user.phone_number:
                existing = await user_repository.get_by_phone_number(db, new_phone)
                if existing:
                    logger.warning("Teléfono duplicado al actualizar — phone=%s", new_phone)
                    raise ValueError("El número de teléfono ya está registrado")

        if "password" in payload:
            payload["password_hash"] = get_password_hash(payload.pop("password"))
        if "role" in payload and payload["role"] is not None:
            payload["role"] = payload["role"].value
        if "identity_type" in payload and payload["identity_type"] is not None:
            payload["identity_type"] = payload["identity_type"].value
        updated = await user_repository.update(db, user, payload)
        logger.info("Usuario actualizado — id=%s, email=%s", updated.id, updated.email)
        return updated

    async def delete_user(self, db: AsyncSession, user: User, *, deleted_by: int | None = None) -> None:
        """Elimina un usuario y sus relaciones, previa desasignación de cursos."""
        logger.info("Eliminando usuario — id=%s, email=%s, deleted_by=%s",
                     user.id, user.email, deleted_by)
        from sqlalchemy import select
        from sqlalchemy.exc import IntegrityError, OperationalError
        from sqlalchemy.orm import joinedload

        from app.models.certificate import Certificate
        from app.models.course import Course, CourseEnrollment
        from app.models.progress import UserProgress
        from app.models.user_audit import UserAudit

        # 1. Construir snapshot de toda la información del usuario antes de borrar
        user_data = {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "first_last_name": user.first_last_name,
            "second_last_name": user.second_last_name,
            "role": user.role,
            "identity_type": user.identity_type,
            "identity_number": user.identity_number,
            "phone_number": user.phone_number,
            "is_active": user.is_active,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "updated_at": user.updated_at.isoformat() if user.updated_at else None,
        }

        r = await db.execute(select(Certificate).where(Certificate.user_id == user.id))
        certificates = [
            {
                "id": c.id,
                "unique_id": str(c.unique_id),
                "certificate_type_id": c.certificate_type_id,
                "issued_at": c.issued_at.isoformat() if c.issued_at else None,
                "expires_at": c.expires_at.isoformat() if c.expires_at else None,
                "status": c.status,
                "qr_code_url": c.qr_code_url,
                "pdf_url": c.pdf_url,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in r.scalars().all()
        ]

        r = await db.execute(
            select(CourseEnrollment)
            .where(CourseEnrollment.user_id == user.id)
            .options(joinedload(CourseEnrollment.course))
        )
        enrollments = [
            {
                "course_id": e.course_id,
                "course_title": e.course.title if e.course else None,
                "course_status": e.course.status if e.course else None,
                "enrolled_at": e.enrolled_at.isoformat() if e.enrolled_at else None,
            }
            for e in r.scalars().all()
        ]

        r = await db.execute(select(UserProgress).where(UserProgress.user_id == user.id))
        progress = [
            {
                "lesson_id": p.lesson_id,
                "completed": p.completed,
                "completed_at": p.completed_at.isoformat() if p.completed_at else None,
            }
            for p in r.scalars().all()
        ]

        r = await db.execute(select(Course).where(Course.teacher_id == user.id))
        courses_as_teacher = [
            {
                "id": c.id,
                "title": c.title,
                "status": c.status,
                "certificate_type_id": c.certificate_type_id,
            }
            for c in r.scalars().all()
        ]

        snapshot = {
            "user": user_data,
            "certificates": certificates,
            "enrollments": enrollments,
            "progress": progress,
            "courses_as_teacher": courses_as_teacher,
        }

        audit_entry = UserAudit(
            user_id=user.id,
            deleted_by=deleted_by,
            snapshot=snapshot,
        )
        db.add(audit_entry)
        await db.flush()

        # 2. Desasignar cursos y eliminar usuario
        try:
            await db.execute(
                text("UPDATE courses SET teacher_id = NULL WHERE teacher_id = :uid"),
                {"uid": user.id},
            )
            await db.flush()
            await user_repository.delete(db, user)
            await db.flush()
            logger.info("Usuario eliminado — id=%s, email=%s", user.id, user.email)
        except IntegrityError as e:
            await db.rollback()
            logger.error("IntegrityError al eliminar usuario %s: %s", user.id, e)
            raise RuntimeError(
                "No se puede eliminar el usuario porque tiene registros de auditoría "
                "o tipos de certificado asociados. "
                "Elimine primero los registros dependientes."
            ) from e
        except OperationalError as e:
            await db.rollback()
            logger.error("OperationalError al eliminar usuario %s: %s", user.id, e)
            raise RuntimeError(
                "Error de conexión con la base de datos. Intente nuevamente."
            ) from e


user_service = UserService()
