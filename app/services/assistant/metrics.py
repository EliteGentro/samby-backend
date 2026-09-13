from collections import defaultdict
from typing import Any


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _rounded(values: dict[str, float]) -> dict[str, float]:
    return {key: round(value, 2) for key, value in sorted(values.items())}


def inventory_metrics(workspace: dict) -> dict:
    products = {row.get("id"): row for row in workspace.get("products", [])}
    on_hand: defaultdict[str, float] = defaultdict(float)
    available: defaultdict[str, float] = defaultdict(float)
    value_by_currency: defaultdict[str, float] = defaultdict(float)
    missing_reservations = missing_costs = 0
    currency = workspace.get("profile", {}).get("currency") or "unspecified"

    for position in workspace.get("stock", []):
        product = products.get(position.get("productId"), {})
        unit = product.get("unit") or position.get("unit") or "unspecified"
        quantity = _number(position.get("onHand")) or 0
        reserved = _number(position.get("reserved"))
        on_hand[unit] += quantity
        if reserved is None:
            missing_reservations += 1
            reserved = 0
        available[unit] += quantity - reserved
        cost = _number(product.get("cost"))
        if cost is None:
            missing_costs += 1
        else:
            value_by_currency[currency] += quantity * cost

    return {
        "product_count": len(products),
        "stock_position_count": len(workspace.get("stock", [])),
        "on_hand_by_unit": _rounded(on_hand),
        "available_by_unit": _rounded(available),
        "inventory_value_by_currency": _rounded(value_by_currency),
        "positions_missing_reservations": missing_reservations,
        "positions_missing_product_cost": missing_costs,
        "open_purchase_count": sum(
            1 for row in workspace.get("purchases", [])
            if str(row.get("status", "")).lower() not in {"received", "closed", "cancelled"}
        ),
    }


def finance_metrics(workspace: dict) -> dict:
    totals: defaultdict[str, float] = defaultdict(float)
    outstanding: defaultdict[str, float] = defaultdict(float)
    by_kind: defaultdict[str, int] = defaultdict(int)
    invalid_amounts = 0
    default_currency = workspace.get("profile", {}).get("currency") or "unspecified"

    for row in [*workspace.get("finance", []), *workspace.get("pendingFinance", [])]:
        amount = _number(row.get("amount"))
        if amount is None:
            invalid_amounts += 1
            continue
        currency = row.get("currency") or default_currency
        kind = row.get("kind") or "unspecified"
        paid = _number(row.get("paidAmount")) or 0
        totals[currency] += amount
        outstanding[currency] += amount - paid
        by_kind[kind] += 1

    cash = workspace.get("cash") or {}
    cash_amount = _number(cash.get("amount"))
    return {
        "cash": {"amount": round(cash_amount, 2) if cash_amount is not None else None,
                 "currency": default_currency, "as_of": cash.get("date"),
                 "reserve": _number(cash.get("reserve"))},
        "finance_record_count": sum(by_kind.values()),
        "finance_records_by_kind": dict(sorted(by_kind.items())),
        "recorded_amount_by_currency": _rounded(totals),
        "outstanding_amount_by_currency": _rounded(outstanding),
        "records_with_invalid_amount": invalid_amounts,
        "commitment_count": len(workspace.get("commitments", [])),
    }


def sales_metrics(workspace: dict) -> dict:
    revenue: defaultdict[str, float] = defaultdict(float)
    quantity_by_unit: defaultdict[str, float] = defaultdict(float)
    products = {row.get("id"): row for row in workspace.get("products", [])}
    default_currency = workspace.get("profile", {}).get("currency") or "unspecified"
    dated = []
    for row in workspace.get("sales", []):
        amount = _number(row.get("amount"))
        quantity = _number(row.get("quantity"))
        product = products.get(row.get("productId"), {})
        if amount is not None:
            revenue[row.get("currency") or default_currency] += amount
        if quantity is not None:
            quantity_by_unit[row.get("unit") or product.get("unit") or "unspecified"] += quantity
        if row.get("date"):
            dated.append(row["date"])
    return {
        "sales_record_count": len(workspace.get("sales", [])),
        "revenue_by_currency": _rounded(revenue),
        "quantity_by_unit": _rounded(quantity_by_unit),
        "date_range": {"start": min(dated), "end": max(dated)} if dated else None,
    }


def data_quality_metrics(workspace: dict) -> dict:
    coverage = workspace.get("coverage") or {}
    states: defaultdict[str, int] = defaultdict(int)
    for item in coverage.values():
        states[(item or {}).get("state", "unknown")] += 1
    product_ids = {row.get("id") for row in workspace.get("products", [])}
    stock_without_product = sum(1 for row in workspace.get("stock", []) if row.get("productId") not in product_ids)
    sales_without_product = sum(1 for row in workspace.get("sales", []) if row.get("productId") not in product_ids)
    return {
        "source_count": len(workspace.get("sources", [])),
        "coverage_by_state": dict(sorted(states.items())),
        "stock_rows_without_product": stock_without_product,
        "sales_rows_without_product": sales_without_product,
        "products_missing_cost": sum(1 for row in workspace.get("products", []) if _number(row.get("cost")) is None),
        "products_missing_price": sum(1 for row in workspace.get("products", []) if _number(row.get("price")) is None),
    }


PAGE_GROUPS = {
    "home": ("inventory", "finance", "sales", "data_quality"),
    "inventory": ("inventory", "sales", "data_quality"),
    "dashboards": ("inventory", "finance", "sales"),
    "analysis": ("inventory", "finance", "sales", "data_quality"),
    "finance": ("finance", "sales", "data_quality"),
    "data": ("data_quality", "inventory", "sales"),
    "settings": ("data_quality",),
}


def calculate_workspace_metrics(workspace: dict, page: str | None = None, groups: list[str] | None = None) -> dict:
    calculators = {
        "inventory": inventory_metrics,
        "finance": finance_metrics,
        "sales": sales_metrics,
        "data_quality": data_quality_metrics,
    }
    selected = groups or list(PAGE_GROUPS.get(page or "home", calculators))
    return {name: calculators[name](workspace) for name in selected if name in calculators}


SEARCH_COLLECTIONS = {
    "products": ("id", "name", "sku", "category", "unit"),
    "suppliers": ("id", "name"),
    "sales": ("id", "date", "productId", "quantity", "amount", "currency", "kind"),
    "purchases": ("id", "date", "supplierId", "productId", "quantity", "amount", "currency", "status"),
    "finance": ("id", "name", "counterparty", "amount", "paidAmount", "currency", "dueDate", "kind"),
    "commitments": ("id", "name", "counterparty", "amount", "currency", "date", "kind"),
    "stock": ("id", "productId", "locationId", "onHand", "reserved", "asOf"),
}


def search_workspace_records(workspace: dict, collection: str, query: str = "", limit: int = 8) -> list[dict]:
    fields = SEARCH_COLLECTIONS.get(collection)
    if fields is None:
        return []
    needle = query.casefold().strip()
    results = []
    for row in workspace.get(collection, []):
        public = {field: row.get(field) for field in fields if field in row}
        if not needle or any(needle in str(value).casefold() for value in public.values() if value is not None):
            results.append(public)
        if len(results) >= max(1, min(limit, 20)):
            break
    return results
