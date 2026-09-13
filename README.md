# Samby backend

`app.main:app` is the integrated Samby API. It runs durable workspaces, guest/account access, enforced member roles, native Excel preview and asynchronous forecasting/simulation on PostgreSQL. The configured `DATABASE_URL` is the authoritative store; Redis is not required.

## Run locally

Requires Python 3.12 or newer (verified on 3.13) and a reachable PostgreSQL database.

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env          # then set DATABASE_URL and the OpenRouter key
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --no-access-log
```

On Windows the interpreter lives in `.venv\Scripts\` instead of `.venv/bin/`. On macOS install LightGBM's runtime first with `brew install libomp`.

Every startup runs `alembic upgrade head` before accepting traffic. If the database is unreachable or a migration fails the process stops; there is no local fallback store.

Swagger documentation is at `/docs`. The standalone `app.prototype.main:app` exposes the same `/api/prototype` platform. The original main entry point also exposes account-compatible `/api/v1/auth/*` routes.

The service accepts browser requests only from `localhost` and `127.0.0.1` on ports 5173 and 4173, which is where the frontend dev server runs. Run one instance at a time: it owns the run queue and the worker that resumes unfinished work after a restart.

See [local service instructions](README-prototype.md) for credential handling, workbook limits, worker recovery and legacy ownership migration. The [platform contract](docs/platform-contract.md) specifies routes and role boundaries; the [analytical contract](docs/analytical-contract-v2.md) specifies numerical inputs and persisted results.

### Environment

`.env` is read from the repository root. These are the settings the default application uses; the remaining keys in `.env.example` belong to the optional template services.

| Variable | Required | Notes |
| --- | --- | --- |
| `DATABASE_URL` | Yes | Must be a PostgreSQL URL. `postgres://`, `postgresql://` and `postgresql+asyncpg://` are normalized to the psycopg driver |
| `OPENROUTER_API_KEY` | For Samby Guide | Without it the guide cannot answer; the rest of the platform runs |
| `OPENROUTER_MODEL` | No | Defaults to `openai/gpt-4.1-mini` |
| `AI_PROVIDER` | No | Defaults to `openrouter`, the only adapter wired today |
| `AI_CONTEXT_MAX_TOKENS` | No | Defaults to 12000 |
| `DATABASE_CONNECT_TIMEOUT_SECONDS`, `DATABASE_POOL_MAX_CONNECTIONS` | No | Connection tuning, default 5 each |

## External dependencies

### Python packages

| Package | Why it is here |
| --- | --- |
| `fastapi`, `uvicorn` | HTTP application and server |
| `psycopg[binary,pool]`, `sqlalchemy`, `alembic` | PostgreSQL access, pooling and migrations |
| `lightgbm`, `catboost`, `scikit-learn`, `numpy` | Forecast engines and their evaluation |
| `openpyxl`, `xlrd`, `python-multipart` | Native XLSX and XLS worksheet preview, upload handling |
| `httpx` | Outbound OpenRouter calls |
| `pydantic-settings` | Environment-backed configuration |

`.[dev]` adds `pytest` and `xlwt` for the test suite. `prototype-requirements.txt` pins the same runtime and test set exactly, as an alternative to the editable install.

`.[legacy]` (`PyJWT`, `pwdlib[argon2]`, `redis`) and `.[legacy-tests]` are only for the optional template modules described at the end of this file. The default application never imports them.

### External services

| Service | Required | Notes |
| --- | --- | --- |
| PostgreSQL, hosted on Neon | Yes | Authoritative store for records, runs, artifacts and guide conversations |
| OpenRouter | Only for Samby Guide | Server-side calls; the key never reaches the browser |
| `libomp` | macOS only | Native runtime LightGBM links against |

No banking, accounting, purchasing, payment, email or external deployment integration is performed.

## File structure

```
samby-backend/
├─ app/
│  ├─ main.py                    Integrated ASGI application (app.main:app)
│  ├─ prototype/                 The business platform
│  │  ├─ main.py                 App factory, lifespan, CORS, analytical worker loop
│  │  ├─ platform.py             Workspaces, guests, accounts, member roles, revisions
│  │  ├─ platform_routes.py      /api/prototype routes
│  │  ├─ store.py                PostgreSQL pool, durable records, run queue
│  │  ├─ schema.py               Request and response contracts
│  │  ├─ engine.py               Shared simulation over captured inputs
│  │  ├─ forecasting.py          Naïve, seasonal-naïve, LightGBM and CatBoost engines
│  │  ├─ inventory_policy.py     Replenishment timing and quantity
│  │  ├─ scenario_finance.py     Cash, receivable and debt consequences
│  │  ├─ poison_apple.py         Insolvency scenario
│  │  ├─ dead_stock_liberator.py Dead-stock scenario
│  │  ├─ treasury_edge_cases.py  Treasury stress scenarios
│  │  ├─ imports.py              XLS and XLSX worksheet preview
│  │  ├─ assistant_routes.py     Samby Guide routes
│  │  └─ ownership.py            Local CLI that attaches legacy workspaces to an account
│  ├─ services/
│  │  ├─ assistant/              Guide service, knowledge retrieval, metric tools, storage
│  │  └─ ai/                     Provider interface and OpenRouter adapter
│  ├─ core/                      Settings and security helpers
│  ├─ db/                        SQLAlchemy session and migration runner
│  └─ api/, models/, schemas/    Optional template modules, off by default
├─ migrations/                   Alembic revisions, applied on every startup
├─ tests/
│  ├─ prototype/                 Platform, engine, models, access and lifecycle
│  ├─ integration/               Integrated app lifespan and account aliases
│  └─ unit/, health/             Optional template services
├─ docs/                         Platform and analytical contracts, assistant architecture
├─ pyproject.toml                Dependencies, extras and pytest selection
├─ prototype-requirements.txt    Exact pinned alternative to the editable install
└─ alembic.ini                   Migration configuration
```

| Path | Responsibility |
| --- | --- |
| `app/prototype/store.py` | The only module that talks to PostgreSQL |
| `app/prototype/platform.py` | Access boundary: who may read or write a workspace |
| `app/prototype/engine.py` | Turns a captured input snapshot into persisted results |
| `app/services/assistant/metrics.py` | Deterministic calculators behind the guide's numbers |
| `docs/platform-contract.md` | Routes and role boundaries |
| `docs/analytical-contract-v2.md` | Numerical inputs and persisted result shapes |

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

The default selection is `tests/prototype` plus `tests/integration`. Tests cover arithmetic and training, async lifecycle, immutable history, cancellation/recovery, namespace access, role enforcement, revision conflicts, native XLSX/XLS parsing and export.

## Optional template services

The prior PostgreSQL/Redis/AI template modules remain under `app/api`, `app/core`, `app/db` and `app/services`. `SAMBY_ENABLE_LEGACY_TEMPLATE=1` mounts their optional example, health, Redis and AI routes after installing `.[legacy]` and configuring their external services. They are separate from the local platform's account sessions and are not required or advertised as part of its business workflow. No provider call, bank transaction, email invitation or external deployment is performed by the default app.

Retained `tests/unit` and `tests/health` concern optional template services and require `.[legacy,legacy-tests]` plus their external configuration.
