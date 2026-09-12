from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from pwdlib import PasswordHash
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_db_session
from app.models.user import User

bearer_scheme = HTTPBearer(auto_error=False)


class AuthenticatedUser(BaseModel):
    """Only claims the application needs; the complete JWT is never propagated."""

    subject: str
    email: str | None = None
    name: str | None = None
    scopes: set[str] = Field(default_factory=set)
    claims: dict[str, Any] = Field(default_factory=dict)


class JWTService:
    """Issue and verify access tokens signed by this application."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def expires_in_seconds(self) -> int:
        return self.settings.jwt_access_token_expire_minutes * 60

    def create_access_token(self, user: User, scopes: set[str] | None = None) -> str:
        now = datetime.now(UTC)
        granted_scopes = scopes or {"read:example", "write:example"}
        claims = {
            "sub": str(user.id),
            "email": user.email,
            "name": user.name,
            "scope": " ".join(sorted(granted_scopes)),
            "type": "access",
            "iat": now,
            "exp": now + timedelta(minutes=self.settings.jwt_access_token_expire_minutes),
            "iss": self.settings.jwt_issuer,
            "aud": self.settings.jwt_audience,
            "jti": str(uuid4()),
        }
        return jwt.encode(
            claims,
            self.settings.jwt_secret,
            algorithm=self.settings.jwt_algorithm,
        )

    def verify(self, token: str) -> AuthenticatedUser:
        try:
            claims = jwt.decode(
                token,
                self.settings.jwt_secret,
                algorithms=[self.settings.jwt_algorithm],
                audience=self.settings.jwt_audience,
                issuer=self.settings.jwt_issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "type"]},
            )
            if claims.get("type") != "access":
                raise jwt.InvalidTokenError("Unexpected token type")
            UUID(claims["sub"])
        except (KeyError, jwt.PyJWTError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired access token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

        raw_scopes = claims.get("scope", "")
        scopes = set(raw_scopes.split()) if isinstance(raw_scopes, str) else set()
        return AuthenticatedUser(
            subject=claims["sub"],
            email=claims.get("email"),
            name=claims.get("name"),
            scopes=scopes,
            claims=claims,
        )


password_hash = PasswordHash.recommended()
_jwt_service: JWTService | None = None


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, encoded_password: str) -> bool:
    return password_hash.verify(password, encoded_password)


def get_jwt_service() -> JWTService:
    global _jwt_service
    if _jwt_service is None:
        _jwt_service = JWTService(get_settings())
    return _jwt_service


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    jwt_service: JWTService = Depends(get_jwt_service),
    session: AsyncSession = Depends(get_db_session),
) -> AuthenticatedUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer access token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token_user = jwt_service.verify(credentials.credentials)
    user = await session.get(User, UUID(token_user.subject))
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User is inactive or no longer exists",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return AuthenticatedUser(
        subject=str(user.id),
        email=user.email,
        name=user.name,
        scopes=token_user.scopes,
        claims=token_user.claims,
    )
