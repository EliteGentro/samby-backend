from app.services.assistant.knowledge import retrieve_knowledge
from app.services.assistant.metrics import (
    calculate_metrics,
    finance_metrics,
    inventory_metrics,
    search_workspace_records,
)


def sample_workspace() -> dict:
    return {
        "version": 1,
        "id": "workspace",
        "mode": "business",
        "revision": 4,
        "profile": {"name": "Northwind", "currency": "MXN"},
        "products": [
            {"id": "p1", "sku": "BOX", "name": "Shipping box", "unit": "pieces", "cost": 4, "price": 9},
            {"id": "p2", "sku": "TAPE", "name": "Packing tape", "unit": "pieces", "cost": None, "price": 3},
        ],
        "stock": [
            {"id": "s1", "productId": "p1", "onHand": 20, "reserved": 3},
            {"id": "s2", "productId": "p2", "onHand": 10, "reserved": None},
        ],
        "purchases": [
            {"id": "po1", "quantity": 12, "receivedQuantity": 2},
            {"id": "po2", "quantity": 5, "receivedQuantity": 5},
        ],
        "finance": [
            {"id": "r1", "kind": "receivable", "amount": 100, "paidAmount": 25, "currency": "MXN"},
            {"id": "b1", "kind": "payable", "amount": 80, "paidAmount": 10, "currency": "MXN"},
        ],
        "cash": {"amount": 50, "date": "2026-09-12"},
        "sales": [],
        "suppliers": [],
        "commitments": [],
        "movements": [],
        "sources": [{"id": "src1"}],
        "coverage": {},
        "muted": [],
    }


def test_inventory_metrics_are_deterministic_and_label_unknown_inputs() -> None:
    result = inventory_metrics(sample_workspace())

    assert result["on_hand_units"] == 30
    assert result["estimated_available_units"] == 27
    assert result["on_hand_by_unit"] == {"pieces": 30}
    assert result["inventory_value_at_known_unit_cost"] == 68
    assert result["positions_with_unknown_reservations"] == 1
    assert result["positions_without_known_product_cost"] == 1
    assert result["open_purchase_count"] == 1


def test_finance_metrics_use_outstanding_amounts() -> None:
    result = finance_metrics(sample_workspace())

    assert result["outstanding_receivables"] == 75
    assert result["outstanding_obligations"] == 70
    assert result["net_cash_plus_receivables_less_obligations"] == 55


def test_retrieval_and_record_search_stay_scoped_to_relevant_content() -> None:
    chunks = retrieve_knowledge("How is available stock calculated?", "inventory")
    records = search_workspace_records(sample_workspace(), "products", "box")

    assert any(chunk.id == "inventory-current" for chunk in chunks)
    assert records == [
        {"id": "p1", "sku": "BOX", "name": "Shipping box", "unit": "pieces", "cost": 4, "price": 9}
    ]
    assert calculate_metrics(sample_workspace(), ["overview"])["overview"]["revision"] == 4
