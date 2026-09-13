# Samby local platform contract

Version 1 extends the v0.4 analytical service with durable current workspace records and enforced access. The base is `/api/prototype` on the same integrated service as the analytical endpoints.

## Credentials

- Analytical and header-scoped import routes require `X-Workspace-ID` plus either its private `X-Workspace-Key` or `Authorization: Bearer <account token>` with membership.
- Workspace path routes use their path ID and the same credential choices.
- When an account and guest key are both supplied, valid membership takes precedence; otherwise the valid guest key can access that local demo/business workspace. An invalid or expired supplied account session returns 401.
- Knowing only a UUID never establishes access. Unauthenticated requests return 401; an unavailable workspace under supplied credentials returns 404. Unauthorized mutations return 403.
- `POST /auth/register {email,password,name?}` and `POST /auth/login {email,password}` return `{access_token,token_type:'bearer',expires_in,user:{id,email,name,created_at}}`. Passwords must contain 8–128 characters. Account sessions expire after 30 days.
- `GET /auth/me` returns the user. `POST /auth/logout` revokes that bearer session and returns 204.
- The integrated `app.main` additionally serves the same account handlers under `/api/v1/auth`.

## Workspace lifecycle

`POST /workspaces/guest {workspace}` requires a cryptographically random `X-Workspace-Key` of 32–256 characters. An unused submitted UUID is preserved. Existing namespaces reject takeover; an identical creation retry with the original key returns the original initial document.

`POST /workspaces {workspace}` requires an account and creates owned records. Both creation routes return `{workspace,revision:0,role}`.

`GET /workspaces` requires an account and returns `[{id,name,mode,revision,role,archived}]`. Archived workspaces are omitted unless `?include_archived=true`.

`GET /workspaces/{id}` returns `{workspace,revision,role,archived}`. `PUT /workspaces/{id} {workspace,expected_revision}` compares the expected server revision atomically and returns `{workspace,revision,role}`. A mismatch returns 409 and writes nothing. Client document revision is replaced by the new server revision. Identity, mode and schema version cannot change in place. Unknown additional JSON fields are preserved subject to role restrictions, finite values and size limits.

`DELETE /workspaces/{id}` archives without destructive removal and returns the envelope. `POST /workspaces/{id}/restore` reverses archive. Both require owner/administrator access. Retained analytical records remain readable.

`POST /workspaces/{id}/claim` requires account Bearer plus the current `X-Workspace-Key`; it creates owner membership, revokes guest access and returns `{workspace,revision,role:'owner',archived}`. Existing UUID-only namespaces require the explicit local ownership CLI, never public UUID-based takeover.

## Roles and membership

Owners/administrators can manage all fields and analyses. Finance writes `finance,pendingFinance,financeEvents,cash,budget,coverage,commitments,paymentTerms` and executes cash/debt simulations. Inventory writes `products,locations,stock,movements,inventoryPools,inventoryHistory,serviceObservations,inventoryLayers` and executes forecasts/inventory simulations. Buyers write `suppliers,purchases,paymentTerms` and execute forecasts/inventory simulations. These three roles may also update `sources,onboarding` provenance. Viewer access is read-only. All members can read the workspace and its saved analyses. Mixed-family execution requires an owner/administrator.

Owners/administrators use:

- `GET /workspaces/{id}/members` → `[{user_id,email,name,created_at,role}]`.
- `POST /workspaces/{id}/members {email,role}` adds an existing registered account.
- `PATCH /workspaces/{id}/members/{user_id} {role}` changes its role.
- `DELETE /workspaces/{id}/members/{user_id}` removes membership.

Mutations return the updated list. No messages or invitations are sent. Guests must claim before adding members. Removing the last owner/administrator returns 409.

## Native workbook preview

`POST /imports/preview` or `/workspaces/{id}/imports/preview` accepts multipart `file` and optional `sheet_name`. It returns `{sheets:[{name,rowCount}],selectedSheet,headers:string[],rows:string[][],fingerprint,sourceName,warnings:string[]}`. The first nonempty row supplies unique nonblank headings. The selected sheet is actually parsed, with cached formula values and explicit conversion notices. Preview never saves business records. Reviewed confirmation uses normal revision-checked workspace persistence.

File limit: 5 MB; selected sheet: 10,000 data rows and 200 columns; expanded XLSX: 50 MB. Date cells use ISO format, numerics use stored values, missing formula caches remain blank. No macros, formulas or external links execute.

## Durable boundary

Neon PostgreSQL stores password/session hashes, current Workspace JSON, memberships, access audit events and existing analytical resources. Alembic applies the schema before startup. Immutable run inputs and artifacts retain their prior contract versions through PostgreSQL triggers. Current workspace edits and ownership migration cannot rewrite them. No production payment execution, external account invitation or live AI service is implied by these endpoints.

## Financial observations

`pendingFinance` retains identified receivables/payables with null unknown amounts. Reviewed completion promotes the same record ID into the confirmed financial register. Pending amounts never become numerical zero. `financeEvents` retains actual dated customer collections, provider availability and supplier payments linked to confirmed records. The reporting layer reconciles unique stage references and cumulative paid allocations; collection and later availability remain separate subtotals. Recording a historical observation does not update cash, paid balances or future analytical events. Both optional arrays are durable current-workspace fields and remain captured in immutable analytical snapshots.
