from copy import deepcopy
from datetime import date, timedelta
from uuid import uuid4

import pytest

from app.prototype.schema import AnalysisConfig


@pytest.fixture
def workspace():
    return {
        'version': 1, 'id': str(uuid4()), 'mode': 'business', 'revision': 0,
        'profile': {'name': 'Test business', 'currency': 'MXN', 'timezone': 'America/Monterrey'},
        'products': [{'id': 'p1', 'name': 'Boxes', 'sku': 'BOX', 'unit': 'pieces', 'cost': 2, 'price': 5, 'moq': None, 'casePack': None}],
        'locations': [], 'stock': [{'id': 'stock1', 'productId': 'p1', 'locationId': None, 'onHand': 10, 'reserved': 0, 'asOf': '2026-09-12', 'quantityBasis': 'on-hand'}],
        'sales': [{'id': 'sale1', 'date': '2026-09-11', 'productId': 'p1', 'quantity': 3, 'amount': 15, 'unit': 'pieces', 'currency': 'MXN', 'locationId': None, 'sourceId': 'source1', 'kind': 'sale'}],
        'suppliers': [{'id': 'supplier1', 'name': 'Box supplier'}], 'purchases': [], 'finance': [], 'commitments': [], 'movements': [],
        'sources': [{'id': 'source1', 'type': 'manual', 'name': 'Manual records'}],
        'cash': {'amount': 100, 'date': '2026-09-12', 'phase': 'opening', 'reserve': 20},
        'coverage': {category: {'state': 'supplied', 'startDate': '2026-09-12', 'endDate': '2027-09-11'} for category in ('collections', 'suppliers', 'payroll', 'rent', 'taxes', 'financing', 'other')},
    }


@pytest.fixture
def config():
    return AnalysisConfig(start_date='2026-09-12', horizon_days=3, question='Q-CASH-SUFFICIENCY', output_families=['cash'], coverage_reviewed=True).model_dump()


def event(identifier, amount, date_value, kind='operating', **kwargs):
    return {'id': identifier, 'kind': kind, 'name': identifier, 'counterparty': identifier, 'amount': amount, 'paidAmount': 0, 'currency': 'MXN', 'dueDate': date_value, 'expectedDate': date_value, 'category': 'collections' if kind in {'receivable', 'provider_pending'} else 'other', 'linkedRecordId': None, 'cashIncluded': False, **kwargs}


def inventory_config(config, question='Q-EXPLORE', **assumptions):
    result = deepcopy(config)
    result.update({'question': question, 'product_id': 'p1', 'output_families': ['inventory'], 'assumptions': {'stock_opening_confirmed': True, 'daily_demand': 6, **assumptions}})
    return result


def history(days=7):
    return [{'id': f's{i}', 'date': (date(2026, 9, 12) - timedelta(days=days - i)).isoformat(), 'productId': 'p1', 'quantity': i + 1, 'unit': 'pieces', 'locationId': None, 'kind': 'sale'} for i in range(days)]
