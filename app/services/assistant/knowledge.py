import re
from dataclasses import dataclass


@dataclass(frozen=True)
class KnowledgeChunk:
    id: str
    title: str
    pages: tuple[str, ...]
    content: str


PAGE_NAMES = {
    "home": "Home and onboarding",
    "inventory": "Inventory",
    "dashboards": "Dashboards",
    "analysis": "Simulations and forecasts",
    "finance": "Finance",
    "data": "Data and imports",
    "settings": "Settings and access",
}


CHUNKS = (
    KnowledgeChunk(
        "workspace-overview",
        "How Samby uses workspace data",
        tuple(PAGE_NAMES),
        "Samby keeps one workspace document containing the business profile, products, stock, sales, purchases, finance events, cash, commitments, sources and data coverage. Insights must distinguish recorded values from assumptions and missing data.",
    ),
    KnowledgeChunk(
        "onboarding",
        "Onboarding and coverage",
        ("home", "data"),
        "Onboarding establishes the business currency, timezone, inventory and financial records, then asks the user to confirm coverage. Supplied means records were provided for a period; absent means reviewed and none exist; unknown means the question still needs review. Unknown coverage should never be treated as zero.",
    ),
    KnowledgeChunk(
        "inventory-basics",
        "Inventory quantities and value",
        ("inventory", "dashboards", "analysis"),
        "Available inventory is on-hand less reserved quantity when reservations are known. Inventory value is calculated deterministically from quantity and product unit cost. Quantities with different units must not be added together. Missing costs or reservations should be called out explicitly.",
    ),
    KnowledgeChunk(
        "inventory-history",
        "Inventory history and replenishment",
        ("inventory", "analysis"),
        "Forecast and replenishment quality depends on dated demand history, supplier lead time, minimum order quantity, case pack, safety stock and service targets. A forecast is a scenario based on a frozen snapshot, not a promise about future demand.",
    ),
    KnowledgeChunk(
        "dashboard-reading",
        "Reading dashboards",
        ("dashboards", "home"),
        "Dashboard cards summarize the current workspace. Users should inspect the underlying date, currency, unit and coverage before acting on a headline number. A useful explanation states what changed, why it matters and which source records support it.",
    ),
    KnowledgeChunk(
        "simulation-forecast",
        "Simulations and forecasts",
        ("analysis",),
        "Every analytical run uses an immutable workspace snapshot and explicit assumptions. Forecasts estimate a baseline; simulations apply events or changed assumptions. Compare runs only when their horizon, question, scope and units are compatible.",
    ),
    KnowledgeChunk(
        "finance-basics",
        "Finance, cash and obligations",
        ("finance", "dashboards", "analysis"),
        "Cash is a point-in-time balance. Receivables, payables and commitments are future or pending movements and should not be mixed with current cash. Outstanding value is amount less paid amount. Amounts in different currencies must be reported separately unless an explicit exchange rate is supplied.",
    ),
    KnowledgeChunk(
        "data-quality",
        "Data sources and quality",
        ("data", "home", "settings"),
        "Imported and manually entered records retain source references where available. Data quality checks focus on missing product links, units, costs, currencies, dates and coverage. The guide can explain gaps but must not invent missing business facts.",
    ),
    KnowledgeChunk(
        "access-sessions",
        "Access and saved conversations",
        ("settings",),
        "Workspace access can be guest-based or account-based with roles. Guide conversations belong to the current workspace and actor. When a guest workspace is claimed, its saved conversations move to the new owner account.",
    ),
)


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def retrieve_knowledge(query: str, page: str, limit: int = 4) -> list[KnowledgeChunk]:
    query_tokens = _tokens(query)
    ranked: list[tuple[int, KnowledgeChunk]] = []
    for chunk in CHUNKS:
        body_tokens = _tokens(f"{chunk.title} {chunk.content}")
        score = len(query_tokens & body_tokens) * 3 + (5 if page in chunk.pages else 0)
        ranked.append((score, chunk))
    ranked.sort(key=lambda item: (-item[0], item[1].id))
    return [chunk for score, chunk in ranked[:limit] if score > 0]
