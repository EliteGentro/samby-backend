# Samby backend

`app.main:app` is the integrated Samby API. It runs durable workspaces, guest/account access, enforced member roles, native Excel preview and asynchronous forecasting/simulation on PostgreSQL. The configured `DATABASE_URL` is the authoritative store; Redis is not required.

## Run locally

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --no-access-log
```

On macOS, install LightGBM's runtime with `brew install libomp`. The standalone `app.prototype.main:app` exposes the same `/api/prototype` platform. The original main entry point also exposes account-compatible `/api/v1/auth/*` routes. Swagger documentation is at `/docs`.

See [local service instructions](README-prototype.md) for credential handling, workbook limits, worker recovery and legacy ownership migration. The [platform contract](docs/platform-contract.md) specifies routes and role boundaries; the [analytical contract](docs/analytical-contract-v2.md) specifies numerical inputs and persisted results.

## State and recovery

Set `DATABASE_URL` to the Neon PostgreSQL URL. Hosted URLs using `postgres://`, `postgresql://`, `postgresql+asyncpg://`, or `postgresql+psycopg://` are normalized to the psycopg driver. On every startup the application runs `alembic upgrade head` before accepting traffic. Startup fails if Neon is unreachable or migrations fail; it never falls back to a local database.

Current business records and analytical resources are durable in Neon; browser storage is only a credential/cache/draft convenience. Submitted run inputs and completed artifacts remain immutable after current-data edits. Use Neon branches, point-in-time restore, or a PostgreSQL-native dump for database backup and recovery.

Samby Guide conversations are also durable in Neon and scoped to the authorized workspace member or guest. The guide uses the configured OpenRouter model, retrieves page-relevant product behavior, and runs numerical workspace questions through deterministic metric tools. See [`docs/assistant-architecture.md`](docs/assistant-architecture.md) for the request flow, access boundary, and API routes.

Records are retained indefinitely. Archive is reversible and does not delete snapshots, referenced results or current documents. No automatic retention purge or destructive per-record analytical deletion runs. Workspace JSON export is available through authenticated `GET /api/prototype/workspaces/{id}/export`; it includes the current document and revision, not account credentials or the full run history.

Guest access requires its private key. Account sessions last 30 days and can be revoked by logout. Passwords use salted scrypt. Ten failed logins for an email/client pair within 15 minutes trigger a persistent temporary limit; errors do not disclose whether an email exists. Owners manage registered members without sending invitation messages. Account email delivery, external identity providers and external deployment are not configured.

Old unprotected UUID-only histories require explicit local assignment to an already registered account:

```sh
.venv/bin/python -m app.prototype.ownership --workspace-id OLD_UUID --owner-email REGISTERED_EMAIL
```

This attaches retained history without changing saved inputs or results. No real account or namespace is automatically selected for migration, and no public endpoint can claim history from knowing its ID.

## Verify

```sh
.venv/bin/python -m pytest -q
```

Tests cover arithmetic and training, async lifecycle, immutable history, cancellation/recovery, namespace access, role enforcement, revision conflicts, native XLSX/XLS parsing and export.

## Optional template services

The prior PostgreSQL/Redis/AI template modules remain under `app/api`, `app/core`, `app/db` and `app/services`. `SAMBY_ENABLE_LEGACY_TEMPLATE=1` mounts their optional example, health, Redis and AI routes after installing `.[legacy]` and configuring their external services. They are separate from the local platform's account sessions and are not required or advertised as part of its business workflow. No provider call, bank transaction, email invitation or external deployment is performed by the default app.

The default test selection is `tests/prototype` plus `tests/integration`; the latter now exercises the integrated app lifespan and account aliases. Retained `tests/unit` and `tests/health` concern optional template services and require `.[legacy,legacy-tests]` plus their external configuration. `prototype-requirements.txt` remains an exact pinned alternative for the same local runtime/test dependencies.
