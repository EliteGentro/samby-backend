import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.session import get_db_session
from app.main import app


@pytest.fixture
async def client():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def test_session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db_session] = test_session
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_liveness(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_protected_route_rejects_missing_token(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401


async def test_register_login_and_database_backed_identity(client: AsyncClient) -> None:
    registration = await client.post(
        "/api/v1/auth/register",
        json={"email": " Ada@Example.com ", "password": "correct horse", "name": "Ada"},
    )

    assert registration.status_code == 201
    registration_body = registration.json()
    assert registration_body["token_type"] == "bearer"
    assert registration_body["expires_in"] == 3600
    assert registration_body["user"]["email"] == "ada@example.com"
    assert "password" not in registration_body["user"]

    token = registration_body["access_token"]
    identity = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert identity.status_code == 200
    assert identity.json()["id"] == registration_body["user"]["id"]
    assert identity.json()["email"] == "ada@example.com"

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "ADA@example.com", "password": "correct horse"},
    )
    assert login.status_code == 200
    assert login.json()["user"]["id"] == registration_body["user"]["id"]


async def test_duplicate_email_and_bad_password_are_rejected(client: AsyncClient) -> None:
    payload = {"email": "user@example.com", "password": "strong password"}
    assert (await client.post("/api/v1/auth/register", json=payload)).status_code == 201
    assert (await client.post("/api/v1/auth/register", json=payload)).status_code == 409

    bad_login = await client.post(
        "/api/v1/auth/login",
        json={"email": payload["email"], "password": "wrong password"},
    )
    assert bad_login.status_code == 401


async def test_tampered_token_is_rejected(client: AsyncClient) -> None:
    registration = await client.post(
        "/api/v1/auth/register",
        json={"email": "jwt@example.com", "password": "strong password"},
    )
    token = registration.json()["access_token"]
    tampered = f"{token[:-1]}{'a' if token[-1] != 'a' else 'b'}"

    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tampered}"}
    )
    assert response.status_code == 401
