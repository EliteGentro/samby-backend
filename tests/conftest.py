from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.db.migrations import upgrade_database


class RedactedUrl(str):
    def __repr__(self) -> str:
        return "'postgresql+psycopg://[redacted]'"


@pytest.fixture
def postgres_url():
    """Give every persistence test an isolated schema in the configured PostgreSQL DB."""
    base_url = get_settings().database_url
    schema = f"samby_test_{uuid4().hex}"
    admin = create_engine(base_url)
    with admin.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))

    # Neon's transaction pooler rejects per-connection search_path options;
    # isolated test schemas therefore use the matching direct endpoint.
    direct_url = base_url.replace("-pooler.", ".", 1)
    parsed = urlsplit(direct_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema}"
    isolated_url = RedactedUrl(urlunsplit(parsed._replace(query=urlencode(query))))
    try:
        upgrade_database(isolated_url)
        yield isolated_url
    finally:
        with admin.begin() as connection:
            connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        admin.dispose()
