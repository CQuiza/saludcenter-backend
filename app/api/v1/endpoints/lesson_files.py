"""Archivos de lecciones."""

import asyncio
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
from app.models.user import User
from app.repositories.lesson_file_repository import lesson_file_repository
from app.repositories.lesson_repository import lesson_repository
from app.repositories.module_repository import module_repository
from app.schemas.lesson_file import LessonFileCreate, LessonFileRead
from app.services.access import (
    is_super_or_admin,
    is_teacher,
    require_course_visible,
    teacher_owns_module,
)
from app.utils.helpers import validate_file_extension
from app.utils.http_headers import build_content_disposition
from app.utils.minio_client import get_minio_client

router = APIRouter(prefix="/lessons", tags=["lesson_files"])


async def _assert_can_manage(
    db: AsyncSession,
    current: User,
    lesson_id: int,
) -> None:
    lesson = await lesson_repository.get_by_id(db, lesson_id)
    if not lesson:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Lección no encontrada"
        )
    mod = await module_repository.get_by_id(db, lesson.module_id)
    if current.role in (UserRole.superuser.value, UserRole.admin.value):
        return
    if is_teacher(current):
        if mod and await teacher_owns_module(db, current, mod):
            return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Sin permiso para gestionar archivos",
    )


@router.get("/{lesson_id}/files", response_model=list[LessonFileRead])
async def list_lesson_files(
    lesson_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
) -> list:
    lesson = await lesson_repository.get_by_id(db, lesson_id)
    if not lesson:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Lección no encontrada"
        )
    mod = await module_repository.get_by_id(db, lesson.module_id)
    if mod:
        await require_course_visible(db, current, mod.course_id, need_content=True)
    rows = await lesson_file_repository.list_by_lesson(db, lesson_id)
    return list(rows)


@router.post("/{lesson_id}/files", response_model=LessonFileRead, status_code=status.HTTP_201_CREATED)
async def create_lesson_file(
    lesson_id: int,
    body: LessonFileCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
) -> object:
    await _assert_can_manage(db, current, lesson_id)
    return await lesson_file_repository.create(
        db,
        lesson_id=lesson_id,
        original_filename=body.original_filename,
        mime_type=body.mime_type,
        order_index=body.order_index,
    )


@router.post("/{lesson_id}/files/{file_id}/upload", response_model=LessonFileRead)
async def upload_lesson_file(
    lesson_id: int,
    file_id: int,
    file: UploadFile,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
) -> object:
    await _assert_can_manage(db, current, lesson_id)

    lf = await lesson_file_repository.get_by_id(db, file_id)
    if not lf or lf.lesson_id != lesson_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado"
        )

    settings = get_settings()
    max_size = settings.lesson_file_max_upload_size_mb * 1024 * 1024
    data = await file.read()
    if len(data) > max_size:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"El archivo supera el límite de {settings.lesson_file_max_upload_size_mb} MB",
        )
    original_filename = unicodedata.normalize("NFC", file.filename or f"lesson-file-{file_id}")
    ext = validate_file_extension(original_filename)
    object_name = f"{settings.minio_path_lesson_files}/{lesson_id}/{file_id}.{ext}"

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

    return await lesson_file_repository.update(
        db,
        lf,
        {
            "file_url": object_name,
            "original_filename": original_filename,
            "mime_type": file.content_type,
        },
    )


@router.get("/{lesson_id}/files/{file_id}/file")
async def download_lesson_file(
    lesson_id: int,
    file_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
    download: Annotated[bool, Query()] = False,
) -> Response:
    lesson = await lesson_repository.get_by_id(db, lesson_id)
    if not lesson:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Lección no encontrada"
        )
    mod = await module_repository.get_by_id(db, lesson.module_id)
    if mod:
        await require_course_visible(db, current, mod.course_id, need_content=True)

    lf = await lesson_file_repository.get_by_id(db, file_id)
    if not lf or lf.lesson_id != lesson_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado"
        )
    if not lf.file_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Este archivo no tiene contenido subido",
        )

    settings = get_settings()
    try:
        client = get_minio_client(settings)
        data = await asyncio.to_thread(client.download_bytes, lf.file_url)
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

    filename = lf.original_filename or (lf.file_url.rsplit("/", 1)[-1] if "/" in lf.file_url else "file")
    disposition = "attachment" if download else "inline"
    return Response(
        content=data,
        media_type=lf.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": build_content_disposition(filename, disposition),
        },
    )


@router.delete("/{lesson_id}/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_lesson_file(
    lesson_id: int,
    file_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
) -> None:
    await _assert_can_manage(db, current, lesson_id)
    lf = await lesson_file_repository.get_by_id(db, file_id)
    if not lf or lf.lesson_id != lesson_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado"
        )
    await lesson_file_repository.delete(db, lf)
