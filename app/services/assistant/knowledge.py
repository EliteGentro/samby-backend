from dataclasses import dataclass
import re


@dataclass(frozen=True)
class KnowledgeChunk:
    id: str
    title: str
    pages: tuple[str, ...]
    text: str


PAGE_NAMES = {
    "home": "Home",
    "inventory": "Inventory",
    "dashboards": "Dashboards",
    "analysis": "Forecast & Simulate",
    "finance": "Finance",
    "data": "Add-ons & Data",
    "settings": "Settings",
}


CHUNKS = (
    KnowledgeChunk(
        "workspace-overview",
        "Workspace overview",
        ("home", "dashboards"),
        "Home is the operating overview for the selected business or demo workspace. It combines current records, capability readiness, important dates, and next actions. Demo data is synthetic and never mixed with business data.",
    ),
    KnowledgeChunk(
        "onboarding-review",
        "Reviewed data onboarding",
        ("home", "data"),
        "Samby accepts business profile, products, stock, sales, suppliers, purchases, finance, commitments, and coverage. Imports remain drafts until the user reviews column mapping, amount meaning, units, accepted rows, and exclusions. Unknown values must remain unknown rather than becoming zero.",
    ),
    KnowledgeChunk(
        "inventory-current",
        "Current inventory",
        ("inventory", "dashboards"),
        "Inventory connects products, locations, stock positions, reservations, purchases, movements, shared pools, historical observations, receipt layers, and service observations. Available quantity is on-hand less known reservations unless the source explicitly reports an available basis.",
    ),
    KnowledgeChunk(
        "inventory-history",
        "Inventory performance metrics",
        ("inventory", "dashboards"),
        "Turnover, days inventory outstanding, GMROI, observed fill, in-stock time, aging, and excess require appropriate historical observations. Samby labels accepted constant estimates and does not silently treat missing history as observed history.",
    ),
    KnowledgeChunk(
        "dashboard-scope",
        "Dashboard scope",
        ("dashboards",),
        "Dashboards summarize only the selected workspace and filters. A metric is unavailable when its required source records or dates are missing. Filters change presentation, not the underlying saved records.",
    ),
    KnowledgeChunk(
        "forecasting",
        "Forecast logic",
        ("analysis",),
        "Forecasts use captured inputs saved with each run. Naive and seasonal-naive baselines are available; advanced LightGBM and CatBoost forecasts need at least 56 consecutive observed daily quantities before the start and retain a separate chronological 14-day evaluation. Missing daily coverage is not extrapolated silently.",
    ),
    KnowledgeChunk(
        "simulations",
        "Simulation logic",
        ("analysis",),
        "Simulations answer focused inventory, cash, debt, collection, demand, supplier, and exploratory questions. Submitted inputs, assumptions, dependencies, and completed artifacts remain attached to the run. A forecast dependency must succeed before it can supply numerical demand to a simulation.",
    ),
    KnowledgeChunk(
        "finance",
        "Finance records",
        ("finance", "dashboards", "analysis"),
        "Finance separates receivables, provider availability, payables, financing, operating obligations, recurring commitments, cash, budgets, and dated payment events. Outstanding values are original amount less recorded paid amount. Current balances are never relabeled as historical evidence.",
    ),
    KnowledgeChunk(
        "standardization",
        "Standardization and add-ons",
        ("data", "settings"),
        "Add-ons & Data shows which capabilities have usable inputs and lets authorized users review standardization proposals. Name, SKU, supplier, price, cost, purchasing unit, and unit-conversion changes retain source identity and an audit trail. Unit conversions require an explicit factor and source.",
    ),
    KnowledgeChunk(
        "workspace-access",
        "Workspace access and persistence",
        ("settings",),
        "Owners and administrators manage workspace access. Finance, inventory, buyer, and viewer roles have scoped permissions. Business records and analytical history are saved on the backend; unsaved drafts remain on the device and revision conflicts prevent silent overwrites.",
    ),
    KnowledgeChunk(
        "product-boundaries",
        "Samby boundaries",
        tuple(PAGE_NAMES),
        "Samby records and analyzes operational information. It does not execute banking, accounting, purchasing, warehouse, or payment transactions. Recommendations should identify missing inputs and assumptions, and users should verify consequential business decisions.",
    ),
)


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if len(token) > 2
    }


def retrieve_knowledge(query: str, page: str, limit: int = 4) -> list[KnowledgeChunk]:
    terms = _tokens(query)
    ranked: list[tuple[int, int, KnowledgeChunk]] = []
    for index, chunk in enumerate(CHUNKS):
        searchable = _tokens(f"{chunk.title} {chunk.text}")
        overlap = len(terms & searchable)
        page_score = 6 if page in chunk.pages else 0
        general_score = 1 if len(chunk.pages) == len(PAGE_NAMES) else 0
        ranked.append((page_score + overlap * 2 + general_score, -index, chunk))
    ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
    return [chunk for score, _index, chunk in ranked[:limit] if score > 0]
