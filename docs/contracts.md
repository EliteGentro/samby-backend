# Historical analytical resource contract (v1)

This document preserves the first resource-design contract. Its statements about UUID-only access, separate application integration and fixture-only advanced models are superseded. Current access and persistence are defined in [platform-contract.md](platform-contract.md); actual forecasting, simulation and comparison semantics are defined in [analytical-contract-v2.md](analytical-contract-v2.md). The original notes below are retained as design history, not the current implementation status.

Frontend domain lives in `src/domain/workspace.ts`. UI and backend snapshots use the same camelCase Workspace JSON. Analytical resources use the snake_case names below. Root owns the domain file, application composition, styles and shared UI primitives. The backend lane owns `samby-backend/app/prototype/` and tests. Business intake lane owns `src/features/data/`. Analytical UI lane owns `src/features/analysis/` and `src/lib/analysis.ts`.

This historical contract originally described SQLite. The current analytical service uses the configured Neon PostgreSQL database for durable records and its autonomous server worker. It exposes a standalone FastAPI entrypoint at `app.prototype.main:app`, port 8001. No live AI or payment actions occur.

API base is `/api/prototype`. Every request has `X-Workspace-ID`, a generated UUID retained locally. Separate IDs isolate demo and business. Production identity and cross-device ownership are deferred. CORS permits only localhost/127.0.0.1 ports 4173 and 5173.

| Method | Path | Body/result |
|---|---|---|
| POST | /definitions | `{name, kind: 'forecast' or 'simulation', config}` returns Definition. |
| GET | /definitions | Returns Definition array. |
| PATCH | /definitions/{id} | Updates name/config/archived, leaves prior runs unchanged. |
| POST | /definitions/{id}/runs | `{snapshot: Workspace, idempotency_key, retry_of_run_id?}` returns Run with HTTP 202. |
| GET | /runs | Returns Run array, newest first, supports kind/status/question filters. |
| GET | /runs/{id} | Returns Run with result only after success. |
| POST | /runs/{id}/cancel | Returns authoritative Run. |
| GET | /runs/{id}/results | Result or nonfinal state with no numeric artifacts. |
| GET | /runs/{id}/series | Saved daily series or nonfinal state. |
| GET | /runs/{id}/events | Saved event trace or nonfinal state. |
| GET | /runs/{id}/scene-manifest | Persisted manifest, unsupported for Q-EXPLORE. |

Definition fields are id, name, kind, config, version, archived, created_at and updated_at.

AnalysisConfig fields are engine `naive | seasonal-naive | lightgbm`, question `Q-NEW-ORDER | Q-REPLENISH | Q-CRITICAL-COLLECTION | Q-CASH-SUFFICIENCY | Q-DEMAND-CHANGE | Q-SLOW-SUPPLIER | Q-CUSTOMER-DEBT | Q-SUPPLIER-ORDER-STOCKOUT | Q-EXPLORE`, start_date, horizon_days, product_id nullable, location_id nullable, output_families array of `inventory | cash | debt`, coverage_reviewed boolean, assumptions object, forecast_run_id nullable and baseline_run_id nullable. Forecasts need no output_families. End date is start plus horizon_days minus one, 1 through 365 inclusive.

Assumptions may include daily_demand, order_quantity, order_date, receipt_date, lead_time_days, collection_delay_days, collection_id, demand_multiplier, reserve, price, unit_cost, purchase_id, stock_opening_confirmed boolean, cash_opening_estimate number, forecast_overlap `replacement | incremental`, demand_start_date, demand_end_date and season_length_days integer 1 through 365. Seasonal-naive defaults to a disclosed seven-day cycle. Only supported assumptions are accepted. Unknown is null or absent, never defaulted into a factual business record. Engine requirements and honest unsupported errors take precedence over producing all output families.

Run fields are id, definition_id, definition_name, kind, status, phase, created_at, updated_at, started_at, completed_at, config, snapshot, warnings, error nullable, result nullable, retry_of_run_id nullable, attempt integer and provenance. Status is queued, waiting_for_dependency, running, succeeded, failed or cancelled. Capture full config/snapshot in the create transaction. Idempotency is namespace-scoped key plus request fingerprint. Reuse with changed input returns 409. Repeated submissions to the same definition with new keys create separate runs.

Result fields are start_date, end_date, grain `daily`, metrics array, series array, events array, assumptions string array, warnings string array, explanations string array, comparison nullable and scene_manifest. Metric is `{key,label,value:number|null,unit:string}`. A daily point has date and optional demand, inventory, cash, inflow, outflow, receivable, unmet_demand, purchase. Event is `{id,date,type,label,amount?,quantity?,source_id?}`. Comparison pins baseline_run_id and stores compatible absolute deltas, with percentage changes only for positive denominators.

Provenance is `{mode: 'demo' | 'computed', engine, engine_version, currency, snapshot_at, scope}`. Demo forecasts using LightGBM fixtures must never claim actual model training. Business LightGBM execution stays unavailable until a supported engine exists. Naive and seasonal naive can compute from compatible submitted dated sales observations. Scenario consequences are a daily deterministic engine over saved inputs, with separate eligibility for inventory and cash. Browser timers only poll.

The backend implementation declares event ordering, demand overlap, stock bridging, currency validation, category coverage and financial deduplication rules. Unsupported combinations fail with actionable errors. Startup resumes pending work without mutating completed results. Dependency failure/cancellation propagates. Server work continues without the browser. Tests prove idempotency, final immutability, cash arithmetic, inclusive dates, isolated namespaces and recovery.

## Implemented input and retrieval details

All inventory scenarios select one product. They require known scoped stock and `stock_opening_confirmed=true`. Stock with `quantityBasis=available` already contains available units and does not subtract reservations. On-hand stock requires an explicit known reserved quantity. Confirming stock from another date explicitly accepts an estimate at the requested opening boundary. Unallocated purchase receipts cannot establish stock at a specific location, so those scenarios use aggregate scope.

| Question | Additional inputs |
|---|---|
| Q-NEW-ORDER | Positive order_quantity and order_date as the requested fulfillment date. With another demand basis, forecast_overlap declares replacement on that date or explicitly incremental demand. Otherwise the order is standalone and other days contain no declared demand. |
| Q-REPLENISH | Explicit daily_demand or pinned daily forecast, positive order_quantity, and receipt_date or order_date plus lead_time_days. Known MOQ and case-pack constraints are enforced. |
| Q-DEMAND-CHANGE | Demand basis, nonnegative demand_multiplier, and demand_start_date/end_date inside the run window. |
| Q-SLOW-SUPPLIER | Demand basis, purchase_id for an open purchase, and lead_time_days as absolute calendar days from its recorded orderDate. |
| Q-SUPPLIER-ORDER-STOCKOUT | Demand basis, purchase_id and changed total order_quantity and/or receipt_date. Already received quantities remain fulfilled. |
| Q-CRITICAL-COLLECTION | collection_id and collection_delay_days, dated opening cash, timed financial records and coverage_reviewed. |
| Q-CUSTOMER-DEBT | Current customer receivables/pending funds and collection_delay_days, with optional collection_id. The debt family works without opening cash. |
| Q-CASH-SUFFICIENCY | Dated opening cash, at least one timed inflow or outflow, and coverage_reviewed. |
| Q-EXPLORE | Independently supported selected families. Inventory needs a demand basis. No scene manifest is required. |

Cash dated opening on start_date or end-of-day on the immediately preceding date establishes the opening boundary. Other dates/phases require an explicit cash_opening_estimate. The supplied working currency applies to all financial events. Product/location filters apply to inventory and demand; financial records currently have business-wide scope and disclose that distinction. Unknown or uncovered categories make the cash result partial. Due dates do not substitute for expected collection/payment dates.

Collection timing changes in customer debt, cash sufficiency and exploration apply to all outstanding dated receivables/provider-pending amounts unless collection_id selects one record. Undated records remain unscheduled; amounts already included in opening cash and settled amounts are excluded. Selected undated collections require an explicit expected date. Shifting events never changes the supplied source snapshot or payment dates.

Optional StockPosition.backordered remains a separate source quantity. Positive pre-existing backorders produce a material partial-scope warning: the lost-demand model does not carry that backlog or infer its overlap with reservations. It does not subtract backorders a second time; inventory-position and backlog-clearance outputs are unsupported.

Daily cash points persist `zero:0` and an owner-supplied `reserve` reference when available. `provider_pending` is a separate daily balance from `receivable`. `series_metadata` records version, run identity, scope, currency, timezone and flow/closing observation meanings. Comparison is `{baseline_run_id, metrics:[{key,label,unit,baseline,alternative,delta,percentage_change}], series:[{date,inventory_delta?,cash_delta?,receivable_delta?,demand_delta?}]}`. Percentage-valued metric differences use percentage points. Percentage changes need a positive baseline denominator.

The manifest is `{contract_version,scene_manifest_supported,run_id,question_key,start_date,end_date,grain,allowed_asset_ids,asset_metadata:[{asset_id,label,description}],entities:[{id,asset_id,source_id}],events:[{event_id,date,entity_id}],series:[{key,run_id}]}`. Exploratory and forecast results declare unsupported scene rendering with empty assets/entities/events/series. Focused manifests reference the saved result only.

`PATCH /runs/{id}` accepts `{archived:boolean}`. Definitions and runs list active items by default, archived items with `?archived=true`, or all items with `?include_archived=true`. Direct retrieval remains available regardless of archive, current source readiness or a newer run. `GET /runs/{id}/transitions` returns the saved lifecycle trail. Nonfinal result resources return HTTP 202 with state and no numerical artifacts. Failed/cancelled result resources return HTTP 409 with state and error. All validation errors expose a JSON `detail` string.

`AnalysisConfig.inventory_pool_id` is optional and mutually exclusive with location_id. A selected pool must exist in snapshot.inventoryPools with known locationIds. Stock and sales filter to those contributions. The scope saves the pool ID and sorted location IDs. Forecast dependency and baseline checks reject changed membership. Unallocated purchase receipts are unsupported inside a restricted pool, as they are inside a selected location.

Cash results additionally save closing `payable` for confirmed supplier External Debt and `financing_debt` for recorded Financing Debt. Cash and debt results save `customer_concentration` as the largest customer's share of the remaining supplied Internal Debt. A zero total yields null, not an invented percentage. Customer, provider, payable and financing balances remain separate.

When a purchasing budget exists, results add `budget` metadata containing amount, known_spend, spend, headroom, breach, start_date, end_date, basis `gross_order_commitment`, scope, partial, included_purchase_ids and unpriced_or_undated_purchase_ids. Gross purchase commitments use order dates within the budget period and count each purchase once. Paid amounts affect cash separately. Unknown cost or order date makes complete spend/headroom/breach unavailable while preserving the known priced subtotal. Budget deltas require matching budget periods, scope and basis. An owner-selected zero budget remains valid.

Q-EXPLORE supports an optional proposed purchase through order_quantity and receipt_date, or order_date plus lead_time_days. It creates one separate proposed receipt and, when its order date and cost are known, one budget commitment. Changing a recorded PO belongs to Q-SUPPLIER-ORDER-STOCKOUT. No proposed receipt creates inferred cash payment terms. Linked purchase/payable sets must reconcile original and paid totals before cash simulation; partial coverage requires an explicit remaining allocation rather than silently dropping an uninvoiced remainder.
