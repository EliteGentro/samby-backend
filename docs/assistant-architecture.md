# Samby Guide architecture

Samby Guide is a contextual assistant for Home, Inventory, Dashboards, Forecast & Simulate, Finance, Add-ons & Data, and Settings. It is intentionally presented as a page guide instead of a general-purpose chatbot.

## Request flow

1. The frontend sends the current page and the user's message to a conversation inside the active workspace.
2. Workspace authentication is enforced with the same account token or private guest key used by the rest of Samby.
3. The backend retrieves page-relevant chunks from the curated Samby behavior corpus.
4. Deterministic calculators produce the page's baseline inventory, finance, sales, and data-quality facts from the latest saved workspace document.
5. OpenRouter receives the recent conversation, retrieved behavior, and calculated facts. The configured `OPENROUTER_MODEL` can request additional record searches or metric groups through function tools.
6. Tool requests execute against the already-authorized workspace. The final answer and its source labels are persisted in PostgreSQL.

The OpenRouter key remains server-side. The browser never receives provider credentials or raw provider payloads.

## Persistence and access

`assistant_sessions` stores named conversation threads by workspace and actor. `assistant_messages` stores user and assistant turns, page context, source labels, and timestamps. A guest's conversations transfer to the new owner when that guest workspace is claimed. Other workspace members receive their own conversation list.

The API surface is:

- `GET /api/prototype/workspaces/{workspace_id}/assistant/sessions`
- `POST /api/prototype/workspaces/{workspace_id}/assistant/sessions`
- `GET /api/prototype/workspaces/{workspace_id}/assistant/sessions/{session_id}`
- `POST /api/prototype/workspaces/{workspace_id}/assistant/sessions/{session_id}/messages`

Read-only workspace roles may ask questions, but the guide cannot mutate business records, submit forecasts, execute simulations, or perform transactions.

## Retrieval corpus

The first retrieval implementation is local and deterministic: normalized query terms are scored against small, versioned chunks describing the implemented product behavior. Current-page chunks receive a ranking boost. This keeps retrieval inspectable and avoids a separate vector service while the corpus is small. The `retrieve_samby_knowledge` tool is the boundary for replacing the scorer with embeddings or a managed vector index later without changing the UI or conversation API.

## Deterministic tools

- `calculate_workspace_metrics` computes grouped overview, inventory, finance, sales, and data-quality values.
- `search_workspace_records` searches a bounded set of safe fields from products, suppliers, stock, purchases, finance, and sales.
- `retrieve_samby_knowledge` retrieves additional current product-behavior chunks.

Unknown reservations and missing product costs are counted explicitly. Inventory quantities stay grouped by unit, and monetary records stay grouped by currency rather than being added across incompatible values. Financial totals use outstanding amount (`amount - paidAmount`) and exclude invalid records. These values are calculated in application code, not by the language model.

## Frontend behavior

Each page offers a one-time orientation modal for each workspace. Accepting it creates a dedicated page conversation and returns a grounded summary in the same modal. The guide can then remain docked on the right or be minimized to the `Ask Samby` control in the top bar. Conversation selection and new-thread creation are available in the docked panel. The panel becomes an overlay on narrower screens.
