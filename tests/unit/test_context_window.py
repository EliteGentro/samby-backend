from app.core.config import Settings
from app.services.ai.models import ChatMessage
from app.services.context_window import RollingContextWindow


def test_context_keeps_system_and_newest_messages() -> None:
    window = RollingContextWindow(max_tokens=35)
    messages = [
        ChatMessage(role="system", content="Keep this instruction."),
        ChatMessage(role="user", content="old " * 30),
        ChatMessage(role="assistant", content="middle"),
        ChatMessage(role="user", content="newest"),
    ]

    fitted = window.fit(messages, reserve_tokens=5)

    assert fitted[0].role == "system"
    assert fitted[-1].content == "newest"
    assert all(message.content != "old " * 30 for message in fitted)


def test_settings_normalize_hosted_postgresql_urls() -> None:
    settings = Settings(
        database_url=(
            "postgresql://user:password@database/app"
            "?sslmode=require&channel_binding=require"
        )
    )

    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert "ssl=require" in settings.database_url
    assert "sslmode" not in settings.database_url
    assert "channel_binding" not in settings.database_url
