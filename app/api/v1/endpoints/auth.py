"""OAuth2 password flow (token + refresh)."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_user
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_refresh_token,
    refresh_token_expires_at,
)
from app.core.settings import get_settings
from app.models.user import User
from app.repositories.user_repository import user_repository
from app.schemas.token import RefreshRequest, Token
from app.schemas.user import UserRead
from app.services.user_service import user_service

router = APIRouter(tags=["auth"])


@router.post("/auth/token", response_model=Token, summary="Obtener token OAuth2")
@limiter.limit("10/minute")
async def login(
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> Token:
    try:
        user = await user_service.authenticate(db, form.username, form.password)
    except PermissionError as e:
        # Cuenta bloqueada por intentos fallidos: se informa al usuario
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
    if not user:
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_access_token(subject=user.id)
    settings = get_settings()
    refresh_token = generate_refresh_token()
    user.refresh_token_hash = hash_refresh_token(refresh_token)
    user.refresh_token_expires_at = refresh_token_expires_at()
    db.add(user)
    await db.flush()

    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        max_age=settings.access_token_expire_minutes * 60,
        path="/api/v1",
    )
    return Token(access_token=token, refresh_token=refresh_token)


@router.post("/auth/refresh", summary="Rotar access token con refresh token")
@limiter.limit("5/minute")
async def refresh_access_token(
    request: Request,
    body: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    hashed = hash_refresh_token(body.refresh_token)
    r = await db.execute(
        select(User).where(
            User.refresh_token_hash == hashed,
            User.refresh_token_expires_at > datetime.now(UTC),
        )
    )
    user = r.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token inválido o expirado",
        )

    new_access = create_access_token(subject=user.id)
    new_refresh = generate_refresh_token()
    user.refresh_token_hash = hash_refresh_token(new_refresh)
    user.refresh_token_expires_at = refresh_token_expires_at()
    db.add(user)
    await db.flush()

    settings = get_settings()
    content = Token(access_token=new_access, refresh_token=new_refresh).model_dump()
    resp = JSONResponse(content=content)
    resp.set_cookie(
        key="access_token",
        value=new_access,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        max_age=settings.access_token_expire_minutes * 60,
        path="/api/v1",
    )
    return resp


@router.post("/auth/logout", summary="Invalidar refresh token")
async def logout(
    current: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    current.refresh_token_hash = None
    current.refresh_token_expires_at = None
    db.add(current)
    await db.flush()
    settings = get_settings()
    resp = JSONResponse(content={"message": "Sesión cerrada"})
    resp.delete_cookie(key="access_token", path="/api/v1", httponly=True)
    return resp


@router.get("/auth/me", response_model=UserRead, summary="Usuario autenticado")
async def me(current: Annotated[User, Depends(get_current_user)]) -> User:
    return current
