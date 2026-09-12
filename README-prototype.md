# Run Samby locally

The integrated FastAPI application stores current business workspaces, accounts, memberships, analytical definitions, immutable input snapshots, runs and results in SQLite. The frontend uses private guest credentials or an account session for every workspace and analytical request.

## Install and start

From `samby-backend`:

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --no-access-log
```

On macOS, LightGBM also needs the OpenMP runtime:

```sh
brew install libomp
```

`app.prototype.main:app` remains an equivalent standalone entry point. The integrated entry point also supplies `/api/v1/auth/*` compatibility routes. PostgreSQL and Redis are not required. The previous template's examples, Redis events, health and AI routes are opt-in through `SAMBY_ENABLE_LEGACY_TEMPLATE=1` and require their original dependencies.

The service health endpoint is `http://127.0.0.1:8001/api/prototype/health`. Frontend origins `localhost` and `127.0.0.1` on ports 5173 and 4173 are allowed.

## Storage and access

The database defaults to `app/prototype/data/samby.sqlite3`. Set `SAMBY_PROTOTYPE_DB` to use another path. Keep the database and its SQLite companion files between restarts; database removal would remove local accounts, workspaces and histories.

A new guest workspace requires a client-generated random private `X-Workspace-Key` with at least 32 characters. The server stores only its hash. It accepts an unused UUID and never grants access merely from knowing a UUID. Repeating an identical initial creation with the same key is safe.

Registration and sign-in return a revocable bearer session and the account's identity. Passwords use salted scrypt; access tokens are random and stored only as hashes. Account sessions last 30 days. Guest credentials persist until the owner claims the workspace with an account, which revokes the guest credential. The browser must retain its guest key to resume an unclaimed workspace.

Current records use revision-checked `PUT /api/prototype/workspaces/{id}`. Stale writes return 409 without overwriting another session. Unknown additional Workspace fields are preserved. Analytical submissions capture independent immutable snapshots; changing current records does not rewrite older runs.

Owners and administrators manage membership. Finance can edit financial records and run cash/debt analyses; inventory can edit inventory/history records and run forecasts/inventory analyses; buyers can edit supplier/purchase/terms records and run forecasts/inventory analyses. Viewers can read but cannot mutate or execute. Existing registered people can be added by email; no invitation message is sent. A workspace must retain an owner or administrator. Archive hides current workspaces or analytical records without deleting their retained evidence.

## Recover previously unprotected analytical history

Old UUID-only namespaces are retained but cannot be claimed through a public endpoint. After registering the intended account, an authorized local operator can assign one exact legacy namespace:

```sh
.venv/bin/python -m app.prototype.ownership --workspace-id OLD_UUID --owner-email REGISTERED_EMAIL
```

This restores a current workspace from its latest submitted snapshot and attaches its existing run IDs to the account. It does not change saved inputs or results. Reassignment of an already protected workspace is rejected.

## Reviewed Excel intake

`POST /api/prototype/imports/preview` accepts an authenticated multipart `.xlsx` or `.xls` file and optional `sheet_name`. It reads real workbook cells and returns worksheet choices, headers, rows and a stable file/sheet fingerprint. No business records are committed by preview. The frontend maps and reviews the returned rows before saving confirmed changes.

Limits are 5 MB per workbook, 10,000 data rows and 200 columns per selected sheet. XLSX expanded size is bounded. Encrypted, malformed and oversized workbooks are rejected. Formula results use saved workbook values; missing caches remain blank. Dates use ISO format, and numeric values do not acquire an invented currency or amount meaning.

## Analytical execution

The worker executes persisted jobs independently of browser polling. It retains failures and cancellations, verifies pinned dependencies, protects idempotent submissions and recovers expired leases with at most three attempts. Stopping and restarting the service preserves inputs and results.

Naïve and seasonal-naïve models use compatible supplied observations. LightGBM and CatBoost run real local training on their supported history schema; insufficient or incompatible data is rejected rather than replaced by fixtures. The [current analytical conventions](docs/analytical-contract-v2.md) and [original resource contract (historical)](docs/contracts.md) describe input and result shapes; the [platform contract](docs/platform-contract.md) describes access and current-record APIs.

## Verify

```sh
.venv/bin/python -m pytest -q
```

The suite covers financial and inventory arithmetic, forecast training, dates and links, worker lifecycle and history, workspace conflicts and recovery, account/session handling, authorization, role enforcement, ownership migration and native workbook previews. The default suite covers the current platform plus its `/api/v1/auth` compatibility routes. Retained template unit/live-service tests under `tests/unit` and `tests/health` require `.[legacy,legacy-tests]` and their original external-service configuration; they are not evidence for the current app.

## Retention, export and backup

Local records are retained indefinitely. Archive is reversible; it does not purge current documents or immutable analytical evidence. There is no automatic retention purge. Authenticated `GET /api/prototype/workspaces/{id}/export` downloads the current Workspace JSON and its server revision. A full database backup also retains accounts, memberships and analytical histories:

```sh
.venv/bin/python -m app.prototype.backup --destination /absolute/path/samby-backup.sqlite3
```

The backup uses SQLite's online backup operation, includes committed WAL state, verifies integrity and refuses to overwrite an existing path. To recover, stop the service and start it with `SAMBY_PROTOTYPE_DB` pointing at the verified backup or a copy. Preserve the prior database until verification completes. Keep backups private because they contain business data and credential hashes.

Ten failed sign-ins for one email/client pair within 15 minutes trigger a persistent temporary rate limit. Invalid-account/password errors are generic. No external email/password-recovery provider is configured. Legacy namespace ownership is never assigned automatically; the local CLI requires an exact retained namespace and an already registered target account.
