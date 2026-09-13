from pathlib import Path

from alembic import command
from alembic.config import Config

from app.core.config import get_settings


def upgrade_database(database_url: str | None = None) -> None:
    """Apply all committed schema migrations before the API begins serving."""
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.attributes["database_url"] = database_url or get_settings().database_url
    command.upgrade(config, "head")
