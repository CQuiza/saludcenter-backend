"""Entregas de tareas por estudiantes."""

import asyncio
import re
import unicodedata
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from minio.error import S3Error
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_user
from app.core.database import get_db
from app.core.settings import get_settings
from app.models.enums import UserRole
from app.models.user import User
from app.repositories.enrollment_repository import course_enrollment_repository
from app.repositories.lesson_repository import lesson_repository
from app.repositories.lesson_task_repository import lesson_task_repository
from app.repositories.module_repository import module_repository
from app.repositories.task_submission_repository import (
    task_submission_repository,
)
from app.schemas.task_submission import (
    TaskSubmissionRead,
    TaskSubmissionWithUserRead,
)
from app.services.access import (
    is_student,
    is_super_or_admin,
    is_teacher,
    require_course_visible,
)
from app.utils.http_headers import build_content_disposition
from app.utils.minio_client import get_minio_client

router = APIRouter(tags=["task-submissions"])


async def _assert_enrolled_in_task_course(
    db: AsyncSession,
    user: User,
    task_id: int,
) -> None:
    task = await lesson_task_repository.get_by_id(db, task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tarea no encontrada"
        )
    lesson = await lesson_repository.get_by_id(db, task.lesson_id)
    if not lesson:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Lección no encontrada"
        )
    mod = await module_repository.get_by_id(db, lesson.module_id)
    if not mod:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Módulo no encontrado"
        )
    if is_super_or_admin(user) or is_teacher(user):
        return
    enr = await course_enrollment_repository.get_by_user_course(
        db, user.id, mod.course_id
    )
    if not enr:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No inscrito en este curso",
        )


@router.post(
    "/tasks/{task_id}/submit",
    response_model=TaskSubmissionRead,
    status_code=status.HTTP_201_CREATED,
)
async def submit_task(
    task_id: int,
    file: UploadFile,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
) -> object:
    if not is_student(current):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo estudiantes pueden entregar tareas",
        )
    await _assert_enrolled_in_task_course(db, current, task_id)

    task = await lesson_task_repository.get_by_id(db, task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tarea no encontrada"
        )

    ext = Path(file.filename or "").suffix.lower()
    if ext != ".pdf":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Solo se permiten archivos PDF",
        )

    settings = get_settings()
    max_size = settings.task_max_upload_size_mb * 1024 * 1024
    data = await file.read()
    if len(data) > max_size:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"El archivo supera el límite de {settings.task_max_upload_size_mb} MB",
        )

    existing = await task_submission_repository.get_by_task_and_user(
        db, task_id, current.id
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya has entregado esta tarea. Solo se permite un intento.",
        )

    sanitized_title = re.sub(r"[^\w\s]", "", task.title).strip().replace(" ", "_")
    original_filename = unicodedata.normalize(
        "NFC", f"{current.identity_number}_{sanitized_title}.pdf"
    )
    object_name = f"{settings.minio_path_task_submissions}/{task_id}/{current.id}.pdf"

    try:
        client = get_minio_client(settings)
        client.ensure_bucket()
        await asyncio.to_thread(
            client.upload_bytes,
            object_name,
            data,
            content_type="application/pdf",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Error al subir archivo: {e}",
        )

    submission = await task_submission_repository.create(
        db,
        task_id=task_id,
        user_id=current.id,
        file_url=object_name,
        original_filename=original_filename,
        mime_type="application/pdf",
    )
    return submission


@router.get(
    "/tasks/{task_id}/submissions",
    response_model=list[TaskSubmissionWithUserRead],
)
async def list_task_submissions(
    task_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
) -> list:
    if not is_super_or_admin(current) and not is_teacher(current):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Sin permiso"
        )
    await _assert_enrolled_in_task_course(db, current, task_id)

    submissions = await task_submission_repository.list_by_task(db, task_id)
    result = []
    for s in submissions:
        result.append(
            TaskSubmissionWithUserRead(
                id=s.id,
                task_id=s.task_id,
                user_id=s.user_id,
                file_url=s.file_url,
                original_filename=s.original_filename,
                mime_type=s.mime_type,
                submitted_at=s.submitted_at,
                user_name=s.user.name or "",
                user_email=s.user.email,
            )
        )
    return result


@router.get(
    "/tasks/{task_id}/my-submission",
    response_model=TaskSubmissionRead | None,
)
async def get_my_submission(
    task_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
) -> object:
    await _assert_enrolled_in_task_course(db, current, task_id)
    sub = await task_submission_repository.get_by_task_and_user(
        db, task_id, current.id
    )
    return sub


@router.get("/submissions/{submission_id}/file")
async def download_submission_file(
    submission_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
) -> Response:
    sub = await task_submission_repository.get_by_id(db, submission_id)
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Entrega no encontrada",
        )

    if is_student(current) and sub.user_id != current.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Sin permiso"
        )

    task = await lesson_task_repository.get_by_id(db, sub.task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tarea no encontrada"
        )
    lesson = await lesson_repository.get_by_id(db, task.lesson_id)
    if lesson:
        mod = await module_repository.get_by_id(db, lesson.module_id)
        if mod:
            await require_course_visible(
                db, current, mod.course_id, need_content=True
            )

    settings = get_settings()
    try:
        client = get_minio_client(settings)
        data = await asyncio.to_thread(client.download_bytes, sub.file_url)
    except S3Error as e:
        code = str(getattr(e, "code", "") or "").lower()
        if code in ("nosuchkey", "notfound"):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Archivo no encontrado",
            )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        )

    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": build_content_disposition(sub.original_filename, "attachment"),
        },
    )
