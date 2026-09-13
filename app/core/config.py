from functools import lru_cache
from pathlib import Path
from typing import Literal
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed settings; secrets stay server-side."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Base Monolith API"
    app_env: Literal["development", "test", "staging", "production"] = "development"
    api_v1_prefix: str = "/api/v1"
    frontend_origin: str = "http://localhost:5173"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://app:app@localhost:5432/app"
    database_connect_timeout_seconds: int = Field(default=5, ge=1, le=30)
    database_pool_max_connections: int = Field(default=5, ge=1, le=20)

    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = Field(default=5, ge=1, le=20)
    redis_default_ttl_seconds: int = Field(default=300, ge=1)
    redis_event_channel: str = "app:events"

    jwt_secret: str = "development-only-change-me-32-characters-minimum"
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_issuer: str = "base-monolith-api"
    jwt_audience: str = "base-monolith-web"
    jwt_access_token_expire_minutes: int = Field(default=60, ge=5, le=10080)

    ai_provider: Literal["openrouter", "gemini"] = "openrouter"
    ai_provider_fallback: Literal["openrouter", "gemini"] | None = None
    ai_context_max_tokens: int = Field(default=12000, ge=100)
    openrouter_api_key: str = ""
    openrouter_model: str = "openai/gpt-4.1-mini"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_site_url: str = "http://localhost:5173"
    openrouter_app_name: str = "Base Monolith"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    eleven_labs_api_key: str = ""
    eleven_labs_voice_id: str = "JBFqnCBsd6RMkjVDRZzb"
    eleven_labs_model: str = "eleven_flash_v2_5"
    eleven_labs_base_url: str = "https://api.elevenlabs.io/v1"

    @field_validator("ai_provider_fallback", mode="before")
    @classmethod
    def blank_fallback_is_disabled(cls, value):
        return None if value == "" else value

    @field_validator("database_url", mode="before")
    @classmethod
    def use_psycopg_driver(cls, value: str) -> str:
        """Normalize hosted PostgreSQL/Neon URLs for SQLAlchemy and psycopg."""
        if value.startswith("postgres://"):
            value = value.replace("postgres://", "postgresql+psycopg://", 1)
        elif value.startswith("postgresql://"):
            value = value.replace("postgresql://", "postgresql+psycopg://", 1)
        elif value.startswith("postgresql+asyncpg://"):
            value = value.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
        if not value.startswith("postgresql+psycopg://"):
            raise ValueError("DATABASE_URL must be a PostgreSQL connection URL")
        return value

    @model_validator(mode="after")
    def require_secure_production_jwt_secret(self) -> "Settings":
        if len(self.jwt_secret) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 characters")
        if self.app_env == "production" and self.jwt_secret.startswith("development-only"):
            raise ValueError("JWT_SECRET must be changed in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
