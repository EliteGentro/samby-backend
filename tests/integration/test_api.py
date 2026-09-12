import pytest
from fastapi.testclient import TestClient

from app.main import create_integrated_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_integrated_app(tmp_path / 'integration.sqlite3', start_worker=False)) as client:
        yield client


def test_liveness(client):
    response = client.get('/api/prototype/health')
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'
    assert response.json()['persistence'] == 'sqlite'


def test_protected_route_rejects_missing_token(client):
    assert client.get('/api/v1/auth/me').status_code == 401


def test_register_login_and_database_backed_identity(client):
    registration = client.post('/api/v1/auth/register', json={'email': ' Ada@Example.com ', 'password': 'correct horse', 'name': 'Ada'})
    assert registration.status_code == 201
    body = registration.json()
    assert body['token_type'] == 'bearer'
    assert body['expires_in'] == 2592000
    assert body['user']['email'] == 'ada@example.com'
    assert 'password' not in body['user']
    identity = client.get('/api/v1/auth/me', headers={'Authorization': f'Bearer {body["access_token"]}'})
    assert identity.status_code == 200
    assert identity.json()['id'] == body['user']['id']
    login = client.post('/api/v1/auth/login', json={'email': 'ADA@example.com', 'password': 'correct horse'})
    assert login.status_code == 200
    assert login.json()['user']['id'] == body['user']['id']


def test_duplicate_email_and_bad_password_are_rejected(client):
    payload = {'email': 'user@example.com', 'password': 'strong password'}
    assert client.post('/api/v1/auth/register', json=payload).status_code == 201
    assert client.post('/api/v1/auth/register', json=payload).status_code == 409
    assert client.post('/api/v1/auth/login', json={'email': payload['email'], 'password': 'wrong password'}).status_code == 401


def test_tampered_token_is_rejected(client):
    registration = client.post('/api/v1/auth/register', json={'email': 'session@example.com', 'password': 'strong password'})
    token = registration.json()['access_token']
    tampered = f'{token[:-1]}{"a" if token[-1] != "a" else "b"}'
    assert client.get('/api/v1/auth/me', headers={'Authorization': f'Bearer {tampered}'}).status_code == 401
