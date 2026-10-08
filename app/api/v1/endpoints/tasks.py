"""Tareas de lecciones."""

import asyncio
import mimetypes
import unicodedata
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from minio.error import S3Error
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_user
from app.core.database import get_db
from app.core.settings import get_settings
from app.models.enums import UserRole
from app.models.lesson import Lesson
from app.models.user import User
from app.repositories.lesson_repository import lesson_repository
from app.repositories.lesson_task_repository import lesson_task_repository
from app.repositories.module_repository import module_repository
from app.schemas.lesson_task import (
    LessonTaskCreate,
    LessonTaskRead,
    LessonTaskUpdate,
)
from app.services.access import (
    is_super_or_admin,
    is_teacher,
    require_course_visible,
    teacher_owns_module,
)
from app.utils.helpers import validate_file_extension
from app.utils.http_headers import build_content_disposition
from app.utils.minio_client import get_minio_client

router = APIRouter(prefix="/tasks", tags=["tasks"])


async def _assert_can_manage(
    db: AsyncSession,
    current: User,
    lesson: Lesson,
) -> None:
    """Verifica que el usuario pueda crear/editar/eliminar tareas en esta lección."""
    if current.role in (UserRole.superuser.value, UserRole.admin.value):
        return
    if is_teacher(current):
        mod = await module_repository.get_by_id(db, lesson.module_id)
        if mod and await teacher_owns_module(db, current, mod):
            return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Sin permiso para gestionar tareas",
    )


@router.get("/by-lesson/{lesson_id}", response_model=list[LessonTaskRead])
async def list_tasks(
    lesson_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list:
    lesson = await lesson_repository.get_by_id(db, lesson_id)
    if not lesson:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Lección no encontrada"
        )
    mod = await module_repository.get_by_id(db, lesson.module_id)
    if mod:
        await require_course_visible(db, current, mod.course_id, need_content=True)
    rows = await lesson_task_repository.list_by_lesson(
        db, lesson_id, skip=skip, limit=limit
    )
    return list(rows)


@router.post("", response_model=LessonTaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(
    body: LessonTaskCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
) -> object:
    lesson = await lesson_repository.get_by_id(db, body.lesson_id)
    if not lesson:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Lección no encontrada"
        )
    await _assert_can_manage(db, current, lesson)
    return await lesson_task_repository.create(
        db,
        lesson_id=body.lesson_id,
        title=body.title,
        description=body.description,
        file_type=body.file_type,
        file_url=body.file_url,
        google_drive_link=body.google_drive_link,
        order_index=body.order_index,
    )


@router.patch("/{task_id}", response_model=LessonTaskRead)
async def update_task(
    task_id: int,
    body: LessonTaskUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
) -> object:
    task = await lesson_task_repository.get_by_id(db, task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tarea no encontrada"
        )
    lesson = await lesson_repository.get_by_id(db, task.lesson_id)
    if lesson:
        await _assert_can_manage(db, current, lesson)
    payload = body.model_dump(exclude_unset=True)
    return await lesson_task_repository.update(db, task, payload)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
) -> None:
    task = await lesson_task_repository.get_by_id(db, task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tarea no encontrada"
        )
    lesson = await lesson_repository.get_by_id(db, task.lesson_id)
    if lesson:
        await _assert_can_manage(db, current, lesson)
    await lesson_task_repository.delete(db, task)


@router.post("/{task_id}/upload", response_model=LessonTaskRead)
async def upload_task_file(
    task_id: int,
    file: UploadFile,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
) -> object:
    task = await lesson_task_repository.get_by_id(db, task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tarea no encontrada"
        )
    lesson = await lesson_repository.get_by_id(db, task.lesson_id)
    if lesson:
        await _assert_can_manage(db, current, lesson)

    settings = get_settings()
    max_size = settings.task_max_upload_size_mb * 1024 * 1024
    data = await file.read()
    if len(data) > max_size:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"El archivo supera el límite de {settings.task_max_upload_size_mb} MB",
        )
    original_filename = unicodedata.normalize("NFC", file.filename or f"task-{task_id}")
    ext = validate_file_extension(original_filename)
    object_name = f"{settings.minio_path_tasks}/{task_id}/{task_id}.{ext}"

    try:
        client = get_minio_client(settings)
        client.ensure_bucket()
        await asyncio.to_thread(
            client.upload_bytes,
            object_name,
            data,
            content_type=file.content_type or "application/octet-stream",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Error al subir archivo: {e}",
        )

    return await lesson_task_repository.update(
        db,
        task,
        {
            "file_url": object_name,
            "original_filename": original_filename,
            "file_type": "upload",
        },
    )


@router.get("/{task_id}/file")
async def download_task_file(
    task_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
    download: Annotated[bool, Query()] = False,
) -> Response:
    task = await lesson_task_repository.get_by_id(db, task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tarea no encontrada"
        )
    if not task.file_url or task.file_type != "upload":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Esta tarea no tiene archivo subido",
        )

    lesson = await lesson_repository.get_by_id(db, task.lesson_id)
    if lesson:
        mod = await module_repository.get_by_id(db, lesson.module_id)
        if mod:
            await require_course_visible(db, current, mod.course_id, need_content=True)

    settings = get_settings()
    try:
        client = get_minio_client(settings)
        data = await asyncio.to_thread(client.download_bytes, task.file_url)
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

    filename = task.original_filename or (task.file_url.rsplit("/", 1)[-1] if "/" in task.file_url else "file")
    mime_type, _ = mimetypes.guess_type(filename)
    disposition = "attachment" if download else "inline"
    return Response(
        content=data,
        media_type=mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": build_content_disposition(filename, disposition),
        },
    )
