from collections.abc import Iterable
from typing import Any


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _round(value: float) -> float:
    return round(value, 2)


def inventory_metrics(workspace: dict) -> dict:
    products = {item.get("id"): item for item in workspace.get("products", [])}
    on_hand_by_unit: dict[str, float] = {}
    available_by_unit: dict[str, float] = {}
    valued = 0.0
    unknown_reservations = 0
    unknown_cost_positions = 0
    for position in workspace.get("stock", []):
        quantity = _number(position.get("onHand"))
        if quantity is None:
            continue
        product = products.get(position.get("productId"), {})
        unit = str(product.get("unit") or "unknown unit")
        on_hand_by_unit[unit] = on_hand_by_unit.get(unit, 0.0) + quantity
        reserved = _number(position.get("reserved"))
        if position.get("quantityBasis") == "available":
            position_available = quantity
        elif reserved is None:
            unknown_reservations += 1
            position_available = quantity
        else:
            position_available = max(0.0, quantity - reserved)
        available_by_unit[unit] = available_by_unit.get(unit, 0.0) + position_available
        cost = _number(product.get("cost"))
        if cost is None:
            unknown_cost_positions += 1
        else:
            valued += position_available * cost
    rounded_on_hand = {unit: _round(value) for unit, value in on_hand_by_unit.items()}
    rounded_available = {unit: _round(value) for unit, value in available_by_unit.items()}
    return {
        "product_count": len(products),
        "stock_position_count": len(workspace.get("stock", [])),
        "on_hand_units": next(iter(rounded_on_hand.values())) if len(rounded_on_hand) == 1 else None,
        "estimated_available_units": next(iter(rounded_available.values())) if len(rounded_available) == 1 else None,
        "on_hand_by_unit": rounded_on_hand,
        "estimated_available_by_unit": rounded_available,
        "inventory_value_at_known_unit_cost": _round(valued),
        "positions_with_unknown_reservations": unknown_reservations,
        "positions_without_known_product_cost": unknown_cost_positions,
        "open_purchase_count": sum(
            1
            for purchase in workspace.get("purchases", [])
            if _number(purchase.get("receivedQuantity")) is not None
            and _number(purchase.get("quantity")) is not None
            and float(purchase["receivedQuantity"]) < float(purchase["quantity"])
        ),
    }


def finance_metrics(workspace: dict) -> dict:
    kinds = ("receivable", "payable", "financing", "operating", "provider_pending")
    currency = workspace.get("profile", {}).get("currency") or "not set"
    totals_by_currency: dict[str, dict[str, float]] = {}
    counts = {kind: 0 for kind in kinds}
    unknown = 0
    for record in workspace.get("finance", []):
        kind = record.get("kind")
        amount = _number(record.get("amount"))
        paid = _number(record.get("paidAmount"))
        if kind not in kinds or amount is None or paid is None or paid > amount:
            unknown += 1
            continue
        record_currency = str(record.get("currency") or currency)
        totals = totals_by_currency.setdefault(record_currency, {item: 0.0 for item in kinds})
        totals[kind] += max(0.0, amount - paid)
        counts[kind] += 1
    primary = totals_by_currency.get(currency, {kind: 0.0 for kind in kinds})
    cash = workspace.get("cash") or {}
    cash_amount = _number(cash.get("amount"))
    obligations = primary["payable"] + primary["financing"] + primary["operating"]
    net_position = None if cash_amount is None else cash_amount + primary["receivable"] - obligations
    return {
        "currency": currency,
        "cash_balance": _round(cash_amount) if cash_amount is not None else None,
        "cash_as_of": cash.get("date"),
        "outstanding_receivables": _round(primary["receivable"]),
        "outstanding_obligations": _round(obligations),
        "provider_pending": _round(primary["provider_pending"]),
        "net_cash_plus_receivables_less_obligations": _round(net_position) if net_position is not None else None,
        "outstanding_by_currency": {
            unit: {kind: _round(value) for kind, value in totals.items()}
            for unit, totals in totals_by_currency.items()
        },
        "record_counts": counts,
        "records_excluded_from_totals": unknown,
    }


def sales_metrics(workspace: dict) -> dict:
    currency = workspace.get("profile", {}).get("currency") or "not set"
    revenue_by_currency: dict[str, float] = {}
    units = 0.0
    unknown_amounts = 0
    unknown_units = 0
    dates: list[str] = []
    for sale in workspace.get("sales", []):
        sign = -1 if sale.get("kind") == "return" else 1
        amount = _number(sale.get("amount"))
        quantity = _number(sale.get("quantity"))
        if amount is None:
            unknown_amounts += 1
        else:
            record_currency = str(sale.get("currency") or currency)
            revenue_by_currency[record_currency] = revenue_by_currency.get(record_currency, 0.0) + sign * amount
        if quantity is None:
            unknown_units += 1
        else:
            units += sign * quantity
        if isinstance(sale.get("date"), str):
            dates.append(sale["date"])
    return {
        "sales_record_count": len(workspace.get("sales", [])),
        "currency": currency,
        "net_recorded_revenue": _round(revenue_by_currency.get(currency, 0.0)),
        "net_recorded_revenue_by_currency": {
            unit: _round(value) for unit, value in revenue_by_currency.items()
        },
        "net_recorded_units": _round(units),
        "date_range": [min(dates), max(dates)] if dates else None,
        "records_with_unknown_amount": unknown_amounts,
        "records_with_unknown_quantity": unknown_units,
    }


def data_quality_metrics(workspace: dict) -> dict:
    collections = (
        "products", "sales", "stock", "suppliers", "purchases", "finance",
        "commitments", "movements", "sources",
    )
    counts = {name: len(workspace.get(name, [])) for name in collections}
    supplied_coverage = sum(
        1 for item in workspace.get("coverage", {}).values() if item.get("state") == "supplied"
    )
    return {
        "record_counts": counts,
        "source_count": counts["sources"],
        "coverage_categories_supplied": supplied_coverage,
        "coverage_categories_total": len(workspace.get("coverage", {})),
        "pending_finance_count": len(workspace.get("pendingFinance", [])),
        "muted_capability_count": len(workspace.get("muted", [])),
    }


def calculate_metrics(workspace: dict, groups: Iterable[str]) -> dict:
    available = {
        "inventory": inventory_metrics,
        "finance": finance_metrics,
        "sales": sales_metrics,
        "data_quality": data_quality_metrics,
    }
    result = {}
    for group in groups:
        if group == "overview":
            result["overview"] = {
                "business_name": workspace.get("profile", {}).get("name") or "My business",
                "mode": workspace.get("mode"),
                "revision": workspace.get("revision"),
                "product_count": len(workspace.get("products", [])),
                "source_count": len(workspace.get("sources", [])),
            }
        elif group in available:
            result[group] = available[group](workspace)
    return result


PAGE_METRIC_GROUPS = {
    "home": ("overview", "inventory", "finance"),
    "inventory": ("inventory", "sales"),
    "dashboards": ("overview", "inventory", "finance", "sales"),
    "analysis": ("inventory", "finance", "sales"),
    "finance": ("finance",),
    "data": ("data_quality",),
    "settings": ("overview", "data_quality"),
}


SEARCH_FIELDS = {
    "products": ("id", "sku", "name", "category", "brand", "unit", "cost", "price", "supplierId", "leadTimeDays", "reorderPoint", "safetyStock"),
    "suppliers": ("id", "name", "active"),
    "stock": ("id", "productId", "locationId", "onHand", "reserved", "asOf", "quantityBasis", "backordered"),
    "purchases": ("id", "productId", "supplierId", "quantity", "receivedQuantity", "amount", "orderDate", "promisedDate", "receivedDate"),
    "finance": ("id", "kind", "name", "counterparty", "amount", "paidAmount", "currency", "dueDate", "expectedDate", "category"),
    "sales": ("id", "date", "productId", "quantity", "amount", "currency", "kind"),
}


def search_workspace_records(workspace: dict, entity: str, query: str = "", limit: int = 8) -> list[dict]:
    if entity not in SEARCH_FIELDS:
        return []
    terms = query.lower().split()
    matches = []
    for record in workspace.get(entity, []):
        haystack = " ".join(str(value).lower() for value in record.values() if value is not None)
        if terms and not all(term in haystack for term in terms):
            continue
        matches.append({field: record.get(field) for field in SEARCH_FIELDS[entity] if field in record})
        if len(matches) >= max(1, min(limit, 20)):
            break
    return matches
