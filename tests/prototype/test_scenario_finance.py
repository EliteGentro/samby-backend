import pytest

from app.prototype.engine import execute
from app.prototype.schema import InputError
from conftest import event, inventory_config


def term(identifier, party, days=1, **extra):
    return {'id': identifier, 'party': party, 'counterparty': 'Customer' if party == 'customer' else 'Box supplier', 'supplierId': 'supplier1' if party == 'supplier' else None, 'days': days, 'startEvent': 'delivery-date' if party == 'customer' else 'receipt-date', 'status': 'agreed', 'reference': 'Accepted contract', 'advancePercent': 0, 'advanceDays': 0, 'sourceId': 'source1', **extra}


def test_purchase_cash_and_fulfilled_demand_collections_are_separate_linked_flows(workspace, config):
    workspace['products'][0]['supplierId'] = 'supplier1'
    workspace['stock'][0]['onHand'] = 0
    workspace['paymentTerms'] = [term('supplier-term', 'supplier'), term('customer-term', 'customer')]
    cfg = inventory_config(config, order_quantity=10, receipt_date='2026-09-12', daily_demand=2, supplier_terms_id='supplier-term', customer_terms_id='customer-term', purchase_cash_treatment='incremental', demand_cash_treatment='incremental')
    cfg['output_families'] = ['inventory', 'cash', 'debt']
    result = execute('simulation', cfg, workspace, 'coupled')
    assert [p['cash'] for p in result['series']] == [100, 90, 100]
    assert [p['receivable'] for p in result['series']] == [10, 10, 10]
    assert [p['planned_payable'] for p in result['series']] == [20, 0, 0]
    assert all(p['payable'] == 0 for p in result['series'])
    assert [p['fulfilled'] for p in result['series']] == [2, 2, 2]
    assert workspace['finance'] == []
    assert workspace['purchases'] == []


def test_supplier_advance_and_remainder_conserve_purchase_total(workspace, config):
    workspace['products'][0]['supplierId'] = 'supplier1'
    workspace['paymentTerms'] = [term('supplier-term', 'supplier', days=2, startEvent='order-date', advancePercent=50, advanceDays=0)]
    cfg = inventory_config(config, order_quantity=10, order_date='2026-09-12', receipt_date='2026-09-13', daily_demand=0, supplier_terms_id='supplier-term', purchase_cash_treatment='incremental')
    cfg['output_families'] = ['inventory', 'cash']
    result = execute('simulation', cfg, workspace, 'advance')
    assert [p['outflow'] for p in result['series']] == [10, 0, 10]
    assert [p['cash'] for p in result['series']] == [90, 90, 80]


def test_changed_purchase_replaces_linked_payable_schedule_once(workspace, config):
    workspace['paymentTerms'] = [term('supplier-term', 'supplier')]
    workspace['purchases'] = [{'id': 'po1', 'productId': 'p1', 'supplierId': 'supplier1', 'quantity': 10, 'receivedQuantity': 0, 'amount': 20, 'paidAmount': 4, 'orderDate': '2026-09-10', 'promisedDate': '2026-09-13', 'plannedPaymentDate': '2026-09-12'}]
    workspace['finance'] = [event('bill1', 20, '2026-09-12', 'payable', category='suppliers', linkedRecordId='po1', paidAmount=4)]
    cfg = inventory_config(config, question='Q-SUPPLIER-ORDER-STOCKOUT', purchase_id='po1', order_quantity=15, supplier_terms_id='supplier-term', purchase_cash_treatment='replace_linked')
    cfg['output_families'] = ['inventory', 'cash']
    result = execute('simulation', cfg, workspace, 'replacement')
    assert [p['outflow'] for p in result['series']] == [0, 0, 26]
    assert [p['payable'] for p in result['series']] == [26, 26, 0]
    assert workspace['finance'][0]['amount'] == 20


def test_terms_never_imply_agreement_or_double_create_existing_customer_invoices(workspace, config):
    workspace['paymentTerms'] = [term('customer-term', 'customer', status='proposed')]
    workspace['finance'] = [event('invoice', 50, '2026-09-13', 'receivable')]
    cfg = inventory_config(config, customer_terms_id='customer-term', demand_cash_treatment='already_recorded')
    cfg['output_families'] = ['inventory', 'cash']
    with pytest.raises(InputError, match='hypothetical'):
        execute('simulation', cfg, workspace, 'unaccepted')
    cfg['assumptions']['terms_accepted'] = True
    result = execute('simulation', cfg, workspace, 'existing')
    assert sum(p['inflow'] for p in result['series']) == 50
    assert not any(e['type'] == 'scenario_invoice' for e in result['events'])


def test_opening_backlog_releases_only_its_declared_reservations_and_carries_new_unmet_units(workspace, config):
    workspace['stock'][0].update({'onHand': 10, 'reserved': 4, 'backordered': 6})
    cfg = inventory_config(config, opening_backlog_confirmed=True, backlog_reservation_overlap='included', backlog_policy='carry', order_quantity=5, receipt_date='2026-09-13')
    result = execute('simulation', cfg, workspace, 'backlog')
    assert [p['fulfilled'] for p in result['series']] == [4, 3, 0]
    assert [p['backorders'] for p in result['series']] == [2, 3, 9]
    assert [p['backlog_fulfilled'] for p in result['series']] == [6, 2, 0]
    assert all(p['lost_units'] == 0 for p in result['series'])
    assert workspace['stock'][0]['reserved'] == 4


def test_new_credit_sales_and_unpaid_share_grow_debt_without_inventory(workspace, config):
    workspace['finance'] = []
    workspace['paymentTerms'] = [term('customer-term', 'customer', days=1, startEvent='invoice-date')]
    config.update({'question': 'Q-CUSTOMER-DEBT', 'output_families': ['cash', 'debt'], 'assumptions': {'collection_delay_days': 0, 'customer_terms_id': 'customer-term', 'demand_cash_treatment': 'incremental', 'new_credit_sales_amount': 200, 'new_credit_sales_date': '2026-09-12', 'invoice_delay_days': 0, 'unpaid_share': 0.25}})
    result = execute('simulation', config, workspace, 'credit')
    assert [p['cash'] for p in result['series']] == [100, 250, 250]
    assert [p['receivable'] for p in result['series']] == [200, 50, 50]
    assert all('inventory' not in point for point in result['series'])


def test_fulfillment_of_new_carried_backlog_recognizes_sales_and_collections_once(workspace, config):
    workspace['stock'][0]['onHand'] = 0
    workspace['paymentTerms'] = [term('customer-term', 'customer', days=0)]
    cfg = inventory_config(config, daily_demand=2, backlog_policy='carry', order_quantity=6, receipt_date='2026-09-13', customer_terms_id='customer-term', demand_cash_treatment='incremental', discount_percent=20)
    cfg['output_families'] = ['inventory', 'cash']
    result = execute('simulation', cfg, workspace, 'carried-sales')
    assert [p['sale_fulfilled'] for p in result['series']] == [0, 4, 2]
    assert [p['inflow'] for p in result['series']] == [0, 16, 8]
    metrics = {m['key']: m['value'] for m in result['metrics']}
    assert metrics['modeled_sales'] == 24
    assert metrics['gross_profit'] == 12
    assert metrics['lost_margin'] == 0


def test_owner_selected_reorder_rule_traces_orders_cash_budget_and_targets(workspace, config):
    workspace['products'][0]['supplierId'] = 'supplier1'
    workspace['stock'][0].update({'onHand': 2, 'backordered': 0})
    workspace['paymentTerms'] = [term('supplier-term', 'supplier', days=0, startEvent='order-date')]
    workspace['budget'] = {'amount': 15, 'startDate': '2026-09-12', 'endDate': '2026-09-14'}
    cfg = inventory_config(config, order_policy='reorder', order_quantity=4, lead_time_days=1, reorder_point=1, safety_stock=2, service_target=100, daily_demand=2, supplier_terms_id='supplier-term', purchase_cash_treatment='incremental')
    cfg['output_families'] = ['inventory', 'cash']
    result = execute('simulation', cfg, workspace, 'rule')
    assert [p['inventory'] for p in result['series']] == [0, 2, 0]
    assert [p['outflow'] for p in result['series']] == [8, 0, 8]
    assert len([e for e in result['events'] if e['id'].startswith('policy-order-')]) == 2
    assert [p['inventory_position'] for p in result['series']] == [4, 2, 4]
    metrics = {m['key']: m['value'] for m in result['metrics']}
    assert metrics['budget_spend'] == 16
    assert metrics['budget_breach'] == 1
    assert metrics['days_below_safety_stock'] == 2
    assert metrics['service_target_gap'] == 0


def test_financial_assumptions_cannot_be_silently_ignored(workspace, config):
    workspace['finance'] = [event('invoice', 50, '2026-09-13', 'receivable')]
    for extra in ({'unpaid_share': .5}, {'new_credit_sales_amount': 200, 'new_credit_sales_date': '2026-09-12'}, {'purchase_paid_amount': 10}):
        config['assumptions'] = extra
        with pytest.raises(InputError, match='require explicitly selected'):
            execute('simulation', config, workspace, 'invalid')


def test_scoped_receipts_use_explicit_purchase_destinations(workspace, config):
    workspace['locations'] = [{'id': 'a'}, {'id': 'b'}]
    workspace['stock'][0]['locationId'] = 'a'
    workspace['purchases'] = [{'id': 'po-a', 'productId': 'p1', 'locationId': 'a', 'quantity': 4, 'receivedQuantity': 0, 'promisedDate': '2026-09-13'}, {'id': 'po-b', 'productId': 'p1', 'locationId': 'b', 'quantity': 1000, 'receivedQuantity': 0, 'promisedDate': '2026-09-12'}]
    cfg = inventory_config(config, daily_demand=0)
    cfg['location_id'] = 'a'
    result = execute('simulation', cfg, workspace, 'location')
    assert [point['inventory'] for point in result['series']] == [10, 14, 14]


def test_cash_only_outgoing_payment_date_changes_once_without_inventory(workspace, config):
    workspace['finance'] = [event('supplier-bill', 60, '2026-09-12', 'payable', category='suppliers')]
    config['assumptions'] = {'payment_id': 'supplier-bill', 'payment_date': '2026-09-14', 'payment_change_accepted': True}
    result = execute('simulation', config, workspace, 'payment-change')
    assert [point['cash'] for point in result['series']] == [100, 100, 40]
    assert [point['payable'] for point in result['series']] == [60, 60, 0]
    assert all('inventory' not in point for point in result['series'])
    assert workspace['finance'][0]['expectedDate'] == '2026-09-12'
    config['assumptions']['payment_change_accepted'] = False
    with pytest.raises(InputError, match='hypothetical schedule'):
        execute('simulation', config, workspace, 'unaccepted')


def test_accepted_payment_before_dispatch_is_enforced(workspace, config):
    workspace['products'][0]['supplierId'] = 'supplier1'
    workspace['paymentTerms'] = [term('supplier-term', 'supplier', days=2, startEvent='order-date')]
    cfg = inventory_config(config, order_quantity=10, order_date='2026-09-12', receipt_date='2026-09-14', supplier_terms_id='supplier-term', purchase_cash_treatment='incremental', supplier_payment_before_dispatch=True, dispatch_date='2026-09-13')
    cfg['output_families'] = ['inventory', 'cash']
    with pytest.raises(InputError, match='payment-before-dispatch'):
        execute('simulation', cfg, workspace, 'late-payment')
    cfg['assumptions']['dispatch_date'] = '2026-09-14'
    result = execute('simulation', cfg, workspace, 'accepted')
    assert sum(point['outflow'] for point in result['series']) == 20


def test_pending_unknown_finance_stays_outside_numbers_but_limits_coverage(workspace, config):
    workspace['finance'] = [event('known', 20, '2026-09-13', 'receivable')]
    workspace['pendingFinance'] = [{'id': 'pending', 'kind': 'payable', 'name': 'Unpriced supplier invoice', 'counterparty': 'Supplier', 'currency': 'MXN', 'sourceId': 'source1', 'amount': None, 'paidAmount': None}]
    result = execute('simulation', config, workspace, 'partial')
    assert [point['cash'] for point in result['series']] == [100, 120, 120]
    assert any('Unpriced supplier invoice' in warning and 'Partial financial scope' in warning for warning in result['warnings'])
    workspace['coverage']['suppliers']['state'] = 'absent'
    with pytest.raises(InputError, match='cannot be confirmed absent'):
        execute('simulation', config, workspace, 'contradiction')


@pytest.mark.parametrize('change', [{'amount': -1}, {'amount': float('inf')}, {'amount': 10, 'paidAmount': 11}, {'kind': 'operating'}, {'expectedDate': '2026-02-30'}, {'sourceId': ''}, {'counterparty': None}])
def test_pending_finance_validates_at_shared_workspace_boundary(workspace, config, change):
    from app.prototype.schema import validate_workspace
    workspace['pendingFinance'] = [{'id': 'pending', 'kind': 'receivable', 'name': 'Incomplete invoice', 'counterparty': '', 'currency': 'MXN', 'amount': None, 'paidAmount': None, 'sourceId': 'source1', **change}]
    with pytest.raises(InputError):
        validate_workspace(workspace)



def test_baseline_rejects_quantity_unit_changes_after_standardization(workspace, config):
    from copy import deepcopy
    cfg = inventory_config(config)
    baseline_result = execute('simulation', cfg, workspace, 'pieces-baseline')
    baseline = {'id': 'pieces-baseline', 'result': baseline_result}
    converted = deepcopy(workspace)
    converted['products'][0]['unit'] = 'boxes'
    converted['stock'][0]['onHand'] /= 10
    converted['products'][0]['cost'] *= 10
    converted['products'][0]['price'] *= 10
    cfg['assumptions']['daily_demand'] /= 10
    with pytest.raises(InputError, match='same quantity unit'):
        execute('simulation', cfg, converted, 'converted', baseline=baseline)
    baseline_result['series_metadata'].pop('quantity_unit')
    with pytest.raises(InputError, match='same quantity unit'):
        execute('simulation', cfg, converted, 'legacy-metadata', baseline=baseline)



def test_unrecorded_advance_needs_explicit_assumption(workspace, config):
    workspace['paymentTerms'] = [term('customer-term', 'customer')]
    workspace['paymentTerms'][0].pop('advancePercent')
    cfg = inventory_config(config, customer_terms_id='customer-term', demand_cash_treatment='incremental')
    cfg['output_families'] = ['inventory', 'cash']
    with pytest.raises(InputError, match='no recorded advance'):
        execute('simulation', cfg, workspace, 'unknown-advance')
    cfg['assumptions']['terms_no_advance_confirmed'] = True
    result = execute('simulation', cfg, workspace, 'accepted-no-advance')
    assert any('terms_no_advance_confirmed: True' == value for value in result['assumptions'])


def test_historical_financial_observations_never_enter_projected_cash_again(workspace, config):
    workspace['finance'] = [event('invoice', 50, '2026-09-13', 'receivable', paidAmount=20)]
    workspace['financeEvents'] = [{'id': 'collection-observation', 'sourceId': 'source1', 'kind': 'customer_collection', 'recordId': 'invoice', 'paymentReference': 'receipt20', 'date': '2026-09-10', 'amount': 20, 'currency': 'MXN'}]
    result = execute('simulation', config, workspace, 'history-is-not-forecast')
    assert [point['cash'] for point in result['series']] == [100, 130, 130]
    workspace['financeEvents'][0]['kind'] = 'supplier_payment'
    with pytest.raises(InputError, match='matching payment stage'):
        execute('simulation', config, workspace, 'bad-link')
