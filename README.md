# Backend

FastAPI boilerplate with async PostgreSQL/Neon, Redis-backed SSE, native JWT authentication, and a provider-neutral AI layer. Swagger UI is generated at `/docs`; the OpenAPI schema is at `/openapi.json`.

## Requirements and dependencies

- Python 3.12+
- FastAPI + Uvicorn: HTTP application and generated Swagger UI
- SQLAlchemy + asyncpg: asynchronous PostgreSQL ORM access
- Alembic: database migration history
- redis-py: bounded async Redis pool and pub/sub
- PyJWT: application-issued access tokens
- pwdlib + Argon2: one-way password hashing
- HTTPX: AI-provider HTTP client
- Pydantic Settings: typed environment configuration
- pytest + pytest-asyncio: unit, integration, and live health tests

## Setup

```bash
cp .env.example .env
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
```

Set `DATABASE_URL` to the Neon connection string (the settings layer converts common hosted PostgreSQL URL formats to asyncpg) and generate a unique `JWT_SECRET` of at least 32 random characters. Registration stores a normalized email, optional name, Argon2 password hash, account status, and timestamps in the `users` table. Login verifies the hash and returns a short-lived application-signed access token. Access tokens are not persisted in PostgreSQL.

## Routes

- `GET /api/v1/health/live`: process-only liveness
- `GET /api/v1/health/ready`: PostgreSQL and Redis readiness
- `POST /api/v1/auth/register`: create a user and issue a JWT
- `POST /api/v1/auth/login`: verify credentials and issue a JWT
- `GET /api/v1/auth/me`: return the current database-backed user
- `GET|POST /api/v1/examples`: protected SQLAlchemy REST example
- `GET /api/v1/events/stream`: protected Redis pub/sub SSE stream
- `POST /api/v1/events`: publish an SSE example event
- `POST /api/v1/ai/chat/stream`: protected AI stream, normalized to SSE

All routes except health, registration, and login require `Authorization: Bearer <access-token>`. JWT validation checks signature, issuer, audience, timestamps, token type, and subject; protected requests also confirm the user still exists and is active in PostgreSQL.

## AI provider boundary

Routes depend on `AIProvider`, not OpenRouter. Provider-independent request options include model, output-token limit, temperature, whether reasoning may be returned, reasoning effort, and a reasoning-token budget. To add a provider:

1. Implement `AIProvider` under `app/services/ai/`.
2. Translate the normalized options in that adapter.
3. Add its selection to `factory.py` and set `AI_PROVIDER`.

`RollingContextWindow` retains system messages and the newest conversation turns using an intentionally approximate token count. Use the target model's tokenizer before relying on exact limits in production. Thinking/reasoning is only surfaced when both the caller asks for it and the selected model/provider exposes it; some providers intentionally do not return hidden reasoning.

## Redis free-tier guardrails

- The shared pool defaults to five connections and is capped at twenty by validation.
- Use Redis for short-lived coordination, cache entries with a TTL, and pub/sub—not as the source of truth.
- The local container is capped at 32 MB and uses `allkeys-lru` to expose memory assumptions early.
- Pub/sub is transient: clients that disconnect miss events. Persist important events in PostgreSQL first.
- Avoid one Redis connection per SSE client at meaningful scale. Replace this example with one subscriber plus an in-process fan-out or a managed event service as concurrency grows.

## Tests

Normal tests are deterministic and do not call external services:

```bash
.venv/bin/pytest tests/unit tests/integration
```

Live dependency checks are isolated and use `.env`:

```bash
.venv/bin/pytest tests/health -m health -v
```

The OpenRouter health check calls the authenticated `/key` metadata endpoint, not a completion, so it validates the key without intentionally consuming generation credits. Do not run live health checks on every unit-test invocation.

## Structure

```text
backend/
├── app/
│   ├── api/routes/       Thin REST, SSE, auth, AI, and health handlers
│   ├── core/             Typed settings, password hashing, and JWT verification
│   ├── db/               Async engine, sessions, and declarative base
│   ├── models/           SQLAlchemy persistence models
│   ├── schemas/          Pydantic API contracts
│   ├── services/
│   │   ├── ai/           Provider interface, factory, and adapters
│   │   ├── context_window.py
│   │   └── redis.py
│   └── main.py           FastAPI composition root
├── migrations/           Alembic environment and immutable revisions
└── tests/
    ├── unit/             Pure component tests
    ├── integration/      In-process API integration tests
    └── health/           Explicit live dependency checks
```

Keep route handlers thin, put business rules in services, persistence in repositories as the domain grows, and always create a new Alembic revision rather than editing a deployed revision.
