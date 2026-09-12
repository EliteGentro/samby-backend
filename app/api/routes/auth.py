from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    AuthenticatedUser,
    JWTService,
    get_current_user,
    get_jwt_service,
    hash_password,
    verify_password,
)
from app.db.session import get_db_session
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserRead

router = APIRouter()


def token_response(user: User, jwt_service: JWTService) -> TokenResponse:
    return TokenResponse(
        access_token=jwt_service.create_access_token(user),
        expires_in=jwt_service.expires_in_seconds,
        user=UserRead.model_validate(user),
    )


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a database-backed account and issue an access token",
)
async def register(
    payload: RegisterRequest,
    session: AsyncSession = Depends(get_db_session),
    jwt_service: JWTService = Depends(get_jwt_service),
) -> TokenResponse:
    user = User(
        email=payload.email,
        name=payload.name,
        password_hash=hash_password(payload.password),
        last_login_at=datetime.now(UTC),
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        ) from exc
    await session.refresh(user)
    return token_response(user, jwt_service)


@router.post("/login", response_model=TokenResponse, summary="Authenticate with email and password")
async def login(
    payload: LoginRequest,
    session: AsyncSession = Depends(get_db_session),
    jwt_service: JWTService = Depends(get_jwt_service),
) -> TokenResponse:
    user = await session.scalar(select(User).where(User.email == payload.email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")

    user.last_login_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(user)
    return token_response(user, jwt_service)


@router.get("/me", response_model=UserRead, summary="Return the current database-backed user")
async def me(
    identity: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> UserRead:
    user = await session.get(User, UUID(identity.subject))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return UserRead.model_validate(user)
