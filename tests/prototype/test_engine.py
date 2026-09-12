import pytest
from pydantic import ValidationError

from app.prototype.engine import execute, prepare
from app.prototype.schema import AnalysisConfig, InputError
from conftest import event, history, inventory_config


def values(result):
    return {m['key']: m['value'] for m in result['metrics']}


def test_cash_floor_first_gap_and_reserve_have_distinct_arithmetic(workspace, config):
    workspace['finance'] = [event('payment1', 150, '2026-09-12'), event('payment2', 100, '2026-09-13'), event('collection', 200, '2026-09-14', 'receivable')]
    result = execute('simulation', config, workspace, 'run1')
    assert [p['cash'] for p in result['series']] == [-50, -150, 50]
    assert values(result)['first_gap'] == 50
    assert values(result)['cash_floor'] == -150
    assert values(result)['maximum_deficit'] == 150
    assert values(result)['reserve_headroom'] == -170
    assert values(result)['reserve_shortfall'] == 170
    assert [p['reserve'] for p in result['series']] == [20, 20, 20]
    assert [p['zero'] for p in result['series']] == [0, 0, 0]


def test_opening_negative_balance_is_earliest_gap(workspace, config):
    workspace['cash']['amount'] = -50
    workspace['finance'] = [event('collection', 100, '2026-09-12', 'receivable')]
    result = execute('simulation', config, workspace, 'run1')
    assert values(result)['cash_floor'] == -50
    assert values(result)['first_gap'] == 50
    assert 'opening on 2026-09-12' in next(m['label'] for m in result['metrics'] if m['key'] == 'first_gap')


def test_outflow_only_and_same_day_netting(workspace, config):
    workspace['finance'] = [event('payment', 120, '2026-09-12')]
    workspace['coverage']['collections']['state'] = 'absent'
    assert execute('simulation', config, workspace, 'outflow')['series'][0]['cash'] == -20
    workspace['cash']['amount'] = 0
    workspace['coverage']['collections']['state'] = 'supplied'
    workspace['finance'] = [event('receipt', 100, '2026-09-12', 'receivable'), event('payment', 100, '2026-09-12')]
    result = execute('simulation', config, workspace, 'netting')
    assert [p['cash'] for p in result['series']] == [0, 0, 0]
    assert any('clearing order' in warning for warning in result['warnings'])


def test_provider_pending_and_invoice_count_one_payment(workspace, config):
    workspace['finance'] = [event('invoice', 100, '2026-09-13', 'receivable', paidAmount=40), event('pending', 40, '2026-09-12', 'provider_pending', linkedRecordId='invoice')]
    result = execute('simulation', config, workspace, 'dedup')
    assert [p['inflow'] for p in result['series']] == [40, 60, 0]
    assert [p['cash'] for p in result['series']] == [140, 200, 200]
    assert [p['receivable'] for p in result['series']] == [60, 0, 0]
    assert [p['provider_pending'] for p in result['series']] == [0, 0, 0]


def test_cash_already_in_opening_is_not_received_again(workspace, config):
    workspace['finance'] = [event('received', 500, '2026-09-12', 'receivable', cashIncluded=True), event('payment', 10, '2026-09-13')]
    result = execute('simulation', config, workspace, 'included')
    assert [p['cash'] for p in result['series']] == [100, 90, 90]
    assert values(result)['receivable_outstanding'] == 0
    assert any('already included' in w for w in result['warnings'])


def test_pending_allocation_cannot_exceed_collected_invoice(workspace, config):
    workspace['finance'] = [event('invoice', 100, '2026-09-13', 'receivable', paidAmount=20), event('pending', 40, '2026-09-12', 'provider_pending', linkedRecordId='invoice')]
    with pytest.raises(InputError, match='exceed collected'):
        execute('simulation', config, workspace, 'bad-links')


def test_linked_purchase_and_recurring_commitment_use_payable_once(workspace, config):
    workspace['purchases'] = [{'id': 'po1', 'amount': 30, 'paidAmount': 0, 'plannedPaymentDate': '2026-09-12', 'supplierId': 'supplier1'}]
    workspace['finance'] = [event('bill1', 30, '2026-09-12', 'payable', category='suppliers', linkedRecordId='po1')]
    workspace['commitments'] = [{'id': 'commit1', 'linkedPayableId': 'bill1', 'name': 'Weekly boxes'}]
    result = execute('simulation', config, workspace, 'links')
    assert [p['outflow'] for p in result['series']] == [30, 0, 0]
    assert result['series'][-1]['cash'] == 70


def test_unknown_and_beyond_horizon_events_stay_outstanding(workspace, config):
    workspace['finance'] = [event('undated', 50, None, 'receivable'), event('later', 70, '2026-10-10', 'receivable'), event('payment', 1, '2026-09-12')]
    workspace['coverage']['payroll']['state'] = 'unknown'
    result = execute('simulation', config, workspace, 'pending')
    assert values(result)['receivable_outstanding'] == 120
    assert result['series'][-1]['cash'] == 99
    assert any('Unscheduled' in w for w in result['warnings'])
    assert any('beyond 2026-09-14' in w for w in result['warnings'])
    assert any('Partial projection. payroll' in w for w in result['warnings'])


def test_debt_only_needs_no_opening_cash(workspace, config):
    workspace['cash'] = None
    workspace['finance'] = [event('invoice', 75, '2026-09-12', 'receivable')]
    config.update({'question': 'Q-CUSTOMER-DEBT', 'output_families': ['debt'], 'assumptions': {'collection_delay_days': 2}})
    result = execute('simulation', config, workspace, 'debt')
    assert [p['receivable'] for p in result['series']] == [75, 75, 0]
    assert all('cash' not in p for p in result['series'])
    assert 'cash_floor' not in values(result)
    assert any('Debt and flow timing only' in w for w in result['warnings'])


@pytest.mark.parametrize('question', ['Q-CASH-SUFFICIENCY', 'Q-EXPLORE'])
def test_all_eligible_collection_delay_changes_cash_and_pending_balances(workspace, config, question):
    workspace['finance'] = [
        event('invoice', 100, '2026-09-12', 'receivable', paidAmount=40),
        event('provider', 40, '2026-09-12', 'provider_pending', linkedRecordId='invoice'),
        event('payment', 150, '2026-09-12'),
        event('undated', 30, None, 'receivable'),
        event('later', 30, '2026-09-14', 'receivable'),
        event('settled', 20, '2026-09-12', 'receivable', paidAmount=20),
        event('included', 70, '2026-09-12', 'receivable', cashIncluded=True),
    ]
    config['question'] = question
    original = execute('simulation', config, workspace, 'original')
    assert [p['cash'] for p in original['series']] == [50, 50, 80]
    config['assumptions'] = {'collection_delay_days': 2}
    result = execute('simulation', config, workspace, 'delayed')
    assert [p['cash'] for p in result['series']] == [-50, -50, 50]
    assert [p['outflow'] for p in result['series']] == [150, 0, 0]
    assert [p['receivable'] for p in result['series']] == [120, 120, 60]
    assert [p['provider_pending'] for p in result['series']] == [40, 40, 0]
    assert any('Unscheduled undated' in warning for warning in result['warnings'])
    assert next(e['date'] for e in result['events'] if e['source_id'] == 'later') == '2026-09-16'
    assert workspace['finance'][0]['expectedDate'] == '2026-09-12'
    config['assumptions']['collection_id'] = 'invoice'
    selected = execute('simulation', config, workspace, 'selected')
    assert [p['cash'] for p in selected['series']] == [-10, -10, 80]
    assert [p['provider_pending'] for p in selected['series']] == [0, 0, 0]


@pytest.mark.parametrize('change,message', [
    ({'coverage_reviewed': False}, 'Review included'),
    ({'start_date': '2026-09-13'}, 'snapshot does not establish'),
])
def test_cash_requires_reviewed_boundary(workspace, config, change, message):
    workspace['finance'] = [event('payment', 10, '2026-09-13')]
    config.update(change)
    with pytest.raises(InputError, match=message):
        execute('simulation', config, workspace, 'invalid')


def test_cash_estimate_is_explicit_and_budget_never_substitutes(workspace, config):
    workspace['finance'] = [event('payment', 10, '2026-09-13')]
    workspace['cash'] = None
    workspace['budget'] = {'amount': 10000, 'startDate': '2026-09-12', 'endDate': '2026-09-14'}
    with pytest.raises(InputError, match='dated opening cash'):
        execute('simulation', config, workspace, 'budget')
    config['assumptions']['cash_opening_estimate'] = 25
    result = execute('simulation', config, workspace, 'estimate')
    assert result['series'][-1]['cash'] == 15
    assert 'reserve_shortfall' not in values(result)


def test_mixed_currencies_fail_without_conversion(workspace, config):
    workspace['finance'] = [event('payment', 10, '2026-09-13', currency='USD')]
    with pytest.raises(InputError, match='working currency'):
        execute('simulation', config, workspace, 'currency')


def test_naive_repeats_actual_prior_observation_without_filling_missing_days(workspace, config):
    config.update({'product_id': 'p1', 'engine': 'naive'})
    workspace['sales'][0]['date'] = '2026-09-01'
    result = execute('forecast', config, workspace, 'forecast')
    assert [p['demand'] for p in result['series']] == [3, 3, 3]
    assert any('2026-09-01' in w for w in result['warnings'])


def test_seasonal_naive_repeats_actual_cycle_and_rejects_a_gap(workspace, config):
    workspace['sales'] = history(7)
    config.update({'engine': 'seasonal-naive', 'product_id': 'p1', 'horizon_days': 9})
    result = execute('forecast', config, workspace, 'seasonal')
    assert [p['demand'] for p in result['series']] == [1, 2, 3, 4, 5, 6, 7, 1, 2]
    workspace['sales'].pop(3)
    with pytest.raises(InputError, match='Missing 2026-09-08'):
        execute('forecast', config, workspace, 'gap')


def test_advanced_engine_requires_documented_history_for_every_environment(workspace, config):
    config.update({'product_id': 'p1', 'engine': 'lightgbm'})
    for mode in ('business', 'demo'):
        workspace['mode'] = mode
        with pytest.raises(InputError, match='56 consecutive'):
            execute('forecast', config, workspace, mode)


@pytest.mark.parametrize('horizon,ending', [(1, '2026-09-12'), (365, '2027-09-11')])
def test_daily_dates_are_inclusive(workspace, config, horizon, ending):
    config.update({'product_id': 'p1', 'horizon_days': horizon})
    result = execute('forecast', config, workspace, 'dates')
    assert len(result['series']) == horizon
    assert result['start_date'] == '2026-09-12'
    assert result['end_date'] == ending
    assert len({p['date'] for p in result['series']}) == horizon


def test_366_days_and_unknown_assumptions_rejected():
    with pytest.raises(ValidationError, match='365'):
        AnalysisConfig(start_date='2026-09-12', horizon_days=366)
    with pytest.raises(ValidationError, match='Unsupported assumptions'):
        AnalysisConfig(start_date='2026-09-12', horizon_days=3, assumptions={'magic_cash': 100})


def test_available_stock_is_not_reserved_twice(workspace, config):
    workspace['stock'][0].update({'quantityBasis': 'available', 'onHand': 10, 'reserved': None})
    result = execute('simulation', inventory_config(config), workspace, 'inventory')
    assert [p['inventory'] for p in result['series']] == [4, 0, 0]
    assert values(result)['unmet_demand'] == 8
    workspace['stock'][0]['quantityBasis'] = 'on-hand'
    with pytest.raises(InputError, match='Reserved stock'):
        execute('simulation', inventory_config(config), workspace, 'unknown-reserves')


@pytest.mark.parametrize('basis,on_hand,reserved', [('on-hand', 10, 4), ('available', 6, None)])
def test_preexisting_backorders_need_explicit_reservation_overlap(workspace, config, basis, on_hand, reserved):
    workspace['stock'][0].update({'quantityBasis': basis, 'onHand': on_hand, 'reserved': reserved, 'backordered': 5})
    with pytest.raises(InputError, match='declare whether reservations'):
        execute('simulation', inventory_config(config), workspace, 'backordered')
    assert workspace['stock'][0]['backordered'] == 5
    workspace['stock'][0]['backordered'] = None
    assert [p['inventory'] for p in execute('simulation', inventory_config(config), workspace, 'unknown-backorders')['series']] == [0, 0, 0]


def test_inventory_boundary_and_aggregate_overlap_rejected(workspace, config):
    cfg = inventory_config(config, stock_opening_confirmed=False)
    with pytest.raises(InputError, match='Confirm the stock'):
        prepare('simulation', cfg, workspace)
    cfg['assumptions']['stock_opening_confirmed'] = True
    workspace['stock'].append({**workspace['stock'][0], 'id': 'other', 'locationId': 'branch'})
    with pytest.raises(InputError, match='overlap'):
        prepare('simulation', cfg, workspace)


def test_declared_order_without_forecast_has_no_other_demand(workspace, config):
    cfg = inventory_config(config, 'Q-NEW-ORDER', order_quantity=14, order_date='2026-09-13')
    cfg['assumptions'].pop('daily_demand')
    result = execute('simulation', cfg, workspace, 'order')
    assert [p['demand'] for p in result['series']] == [0, 14, 0]
    assert [p['inventory'] for p in result['series']] == [10, 0, 0]
    assert values(result)['unmet_demand'] == 4
    assert workspace['stock'][0]['onHand'] == 10


def test_order_and_base_demand_need_declared_overlap(workspace, config):
    cfg = inventory_config(config, 'Q-NEW-ORDER', order_quantity=4, order_date='2026-09-13')
    with pytest.raises(InputError, match='replacement or explicitly incremental'):
        execute('simulation', cfg, workspace, 'overlap')
    cfg['assumptions']['forecast_overlap'] = 'replacement'
    assert [p['demand'] for p in execute('simulation', cfg, workspace, 'replacement')['series']] == [6, 4, 6]
    cfg['assumptions']['forecast_overlap'] = 'incremental'
    assert [p['demand'] for p in execute('simulation', cfg, workspace, 'incremental')['series']] == [6, 10, 6]


def test_replenishment_receives_before_same_day_demand(workspace, config):
    cfg = inventory_config(config, 'Q-REPLENISH', order_quantity=12, receipt_date='2026-09-13')
    result = execute('simulation', cfg, workspace, 'replenishment')
    assert [p['inventory'] for p in result['series']] == [4, 10, 4]
    assert values(result)['unmet_demand'] == 0
    workspace['products'][0]['casePack'] = 5
    with pytest.raises(InputError, match='whole multiple'):
        execute('simulation', cfg, workspace, 'pack')


def test_demand_multiplier_changes_only_declared_interval(workspace, config):
    cfg = inventory_config(config, 'Q-DEMAND-CHANGE', demand_multiplier=2, demand_start_date='2026-09-13', demand_end_date='2026-09-13')
    result = execute('simulation', cfg, workspace, 'change')
    assert [p['demand'] for p in result['series']] == [6, 12, 6]


def test_supplier_delay_and_quantity_change_use_selected_purchase(workspace, config):
    workspace['purchases'] = [{'id': 'po1', 'productId': 'p1', 'supplierId': 'supplier1', 'quantity': 12, 'receivedQuantity': 0, 'orderDate': '2026-09-10', 'promisedDate': '2026-09-13'}]
    cfg = inventory_config(config, 'Q-SLOW-SUPPLIER', purchase_id='po1', lead_time_days=4)
    result = execute('simulation', cfg, workspace, 'delay')
    assert [p['inventory'] for p in result['series']] == [4, 0, 6]
    assert values(result)['unmet_demand'] == 2
    cfg = inventory_config(config, 'Q-SUPPLIER-ORDER-STOCKOUT', purchase_id='po1', order_quantity=24, receipt_date='2026-09-12')
    result = execute('simulation', cfg, workspace, 'changed-order')
    assert [p['inventory'] for p in result['series']] == [28, 22, 16]
    assert workspace['purchases'][0]['quantity'] == 12


def test_critical_collection_changes_timing_without_inventory(workspace, config):
    workspace['products'] = []
    workspace['stock'] = []
    workspace['finance'] = [event('invoice', 50, '2026-09-12', 'receivable'), event('payment', 120, '2026-09-12')]
    config.update({'question': 'Q-CRITICAL-COLLECTION', 'assumptions': {'collection_id': 'invoice', 'collection_delay_days': 2}})
    result = execute('simulation', config, workspace, 'critical')
    assert [p['cash'] for p in result['series']] == [-20, -20, 30]
    assert values(result)['first_gap'] == 20


def test_scene_allowlist_and_exploration_unsupported(workspace, config):
    cfg = inventory_config(config, 'Q-NEW-ORDER', order_quantity=4, order_date='2026-09-13', forecast_overlap='replacement')
    result = execute('simulation', cfg, workspace, 'scene')
    manifest = result['scene_manifest']
    assert manifest['run_id'] == 'scene'
    assert manifest['scene_manifest_supported'] is True
    assert set(entity['asset_id'] for entity in manifest['entities']) <= {'customer_order', 'warehouse', 'sku_stack', 'supplier_node', 'delivery_truck', 'cash_account', 'calendar_marker'}
    assert set(e['event_id'] for e in manifest['events']) == set(e['id'] for e in result['events'])
    assert {asset['asset_id'] for asset in manifest['asset_metadata']} == set(manifest['allowed_asset_ids'])
    assert all(asset['description'] for asset in manifest['asset_metadata'])
    result = execute('simulation', inventory_config(config), workspace, 'explore')
    assert result['scene_manifest']['scene_manifest_supported'] is False
    assert result['scene_manifest']['allowed_asset_ids'] == []
    assert len(result['series']) == 3


def test_pinned_comparison_uses_absolute_deltas_for_zero_baseline(workspace, config):
    cfg = inventory_config(config, daily_demand=0)
    baseline_result = execute('simulation', cfg, workspace, 'baseline')
    changed = inventory_config(config, daily_demand=10)
    result = execute('simulation', changed, workspace, 'changed', baseline={'id': 'baseline', 'result': baseline_result})
    delta = next(m for m in result['comparison']['metrics'] if m['key'] == 'unmet_demand')
    assert delta['baseline'] == 0
    assert delta['delta'] == 20
    assert delta['percentage_change'] is None
    assert result['comparison']['baseline_run_id'] == 'baseline'


def test_forecast_missing_daily_coverage_is_never_extrapolated(workspace, config):
    cfg = inventory_config(config)
    cfg['forecast_run_id'] = 'forecast'
    dependency = {'config': {'product_id': 'p1', 'location_id': None}, 'result': {'series': [{'date': '2026-09-12', 'demand': 1}]}}
    with pytest.raises(InputError, match='does not cover every'):
        execute('simulation', cfg, workspace, 'no-fill', dependency)


def test_duplicate_record_ids_and_demo_injection_are_rejected(workspace, config):
    workspace['finance'] = [event('duplicate', 10, '2026-09-12'), event('duplicate', 10, '2026-09-12')]
    with pytest.raises(InputError, match='Duplicate finance'):
        execute('simulation', config, workspace, 'duplicate')
    workspace['finance'].pop()
    workspace['sources'][0]['type'] = 'demo'
    with pytest.raises(InputError, match='separate demonstration'):
        execute('simulation', config, workspace, 'demo-injection')


def test_budget_counts_gross_commitments_once_inside_its_period(workspace, config):
    workspace['budget'] = {'amount': 100, 'startDate': '2026-09-12', 'endDate': '2026-09-14'}
    workspace['purchases'] = [
        {'id': 'before', 'productId': 'p1', 'orderDate': '2026-09-11', 'amount': 500, 'paidAmount': 500, 'quantity': 1, 'receivedQuantity': 1},
        {'id': 'inside', 'productId': 'p1', 'orderDate': '2026-09-12', 'amount': 40, 'paidAmount': 35, 'quantity': 10, 'receivedQuantity': 10},
        {'id': 'after', 'productId': 'p1', 'orderDate': '2026-09-15', 'amount': 900, 'paidAmount': 0, 'quantity': 1, 'receivedQuantity': 1},
    ]
    workspace['finance'] = [event('bill', 40, '2026-09-13', 'payable', paidAmount=35, linkedRecordId='inside')]
    result = execute('simulation', inventory_config(config, daily_demand=0), workspace, 'budget')
    assert values(result)['budget_spend'] == 40
    assert values(result)['budget_headroom'] == 60
    assert values(result)['budget_breach'] == 0
    assert result['budget']['included_purchase_ids'] == ['inside']
    assert result['budget']['basis'] == 'gross_order_commitment'
    assert 'cash_floor' not in values(result)


def test_budget_changes_only_selected_supplier_order_and_compares(workspace, config):
    workspace['budget'] = {'amount': 100, 'startDate': '2026-09-10', 'endDate': '2026-09-14'}
    workspace['purchases'] = [
        {'id': 'po1', 'productId': 'p1', 'supplierId': 'supplier1', 'quantity': 10, 'receivedQuantity': 0, 'orderDate': '2026-09-10', 'promisedDate': '2026-09-13', 'amount': 40, 'paidAmount': 20},
        {'id': 'po2', 'productId': 'p1', 'supplierId': 'supplier1', 'quantity': 5, 'receivedQuantity': 0, 'orderDate': '2026-09-11', 'promisedDate': '2026-09-14', 'amount': 30, 'paidAmount': 0},
    ]
    cfg = inventory_config(config, 'Q-SUPPLIER-ORDER-STOCKOUT', purchase_id='po1', order_quantity=10)
    baseline = execute('simulation', cfg, workspace, 'baseline')
    cfg['assumptions']['order_quantity'] = 20
    result = execute('simulation', cfg, workspace, 'changed', baseline={'id': 'baseline', 'result': baseline})
    assert values(result)['budget_spend'] == 110
    assert values(result)['budget_headroom'] == -10
    assert values(result)['budget_breach'] == 10
    delta = next(m for m in result['comparison']['metrics'] if m['key'] == 'budget_spend')
    assert delta['baseline'] == 70
    assert delta['alternative'] == 110
    assert delta['delta'] == 40
    assert workspace['purchases'][0]['amount'] == 40


def test_unknown_budget_cost_stays_partial_and_zero_budget_is_valid(workspace, config):
    workspace['budget'] = {'amount': 0, 'startDate': '2026-09-12', 'endDate': '2026-09-14'}
    workspace['purchases'] = [{'id': 'unknown', 'productId': 'p1', 'orderDate': '2026-09-12', 'amount': None, 'quantity': 1, 'receivedQuantity': 1}]
    result = execute('simulation', inventory_config(config), workspace, 'partial-budget')
    assert values(result)['purchasing_budget'] == 0
    assert values(result)['budget_spend'] is None
    assert values(result)['budget_headroom'] is None
    assert result['budget']['partial'] is True
    assert result['budget']['unpriced_or_undated_purchase_ids'] == ['unknown']


def test_exploratory_proposed_order_affects_supply_and_budget(workspace, config):
    workspace['budget'] = {'amount': 20, 'startDate': '2026-09-12', 'endDate': '2026-09-14'}
    cfg = inventory_config(config, order_quantity=12, order_date='2026-09-12', receipt_date='2026-09-13')
    result = execute('simulation', cfg, workspace, 'exploratory-order')
    assert [p['inventory'] for p in result['series']] == [4, 10, 4]
    assert values(result)['budget_spend'] == 24
    assert values(result)['budget_breach'] == 4
    assert result['budget']['included_purchase_ids'] == ['assumed-replenishment']
    assert any(e['type'] == 'purchase_order' and e['date'] == '2026-09-12' for e in result['events'])
    assert result['scene_manifest']['scene_manifest_supported'] is False


def test_budget_period_changes_do_not_create_incompatible_deltas(workspace, config):
    workspace['budget'] = {'amount': 20, 'startDate': '2026-09-12', 'endDate': '2026-09-14'}
    cfg = inventory_config(config, order_quantity=12, order_date='2026-09-12', receipt_date='2026-09-13')
    baseline = execute('simulation', cfg, workspace, 'baseline')
    workspace['budget']['endDate'] = '2026-10-01'
    result = execute('simulation', cfg, workspace, 'different-budget', baseline={'id': 'baseline', 'result': baseline})
    assert not any(m['key'].startswith('budget_') for m in result['comparison']['metrics'])
    assert any('different budget periods' in w for w in result['warnings'])


def test_payables_financing_and_customer_concentration_remain_distinct(workspace, config):
    workspace['finance'] = [
        event('customer-a', 60, '2026-09-14', 'receivable', counterparty='Customer A'),
        event('customer-b', 40, '2026-09-13', 'receivable', counterparty='Customer B'),
        event('supplier', 30, '2026-09-13', 'payable', category='suppliers'),
        event('loan', 20, '2026-09-14', 'financing', category='financing'),
    ]
    result = execute('simulation', config, workspace, 'balances')
    assert [p['payable'] for p in result['series']] == [30, 0, 0]
    assert [p['financing_debt'] for p in result['series']] == [20, 20, 0]
    assert [p['customer_concentration'] for p in result['series']] == [60, 100, None]
    assert values(result)['external_debt'] == 0
    assert values(result)['financing_debt'] == 0
    assert values(result)['customer_concentration'] is None


def test_partial_purchase_linkage_requires_explicit_remaining_allocation(workspace, config):
    workspace['purchases'] = [{'id': 'po1', 'amount': 1000, 'paidAmount': 0, 'plannedPaymentDate': '2026-09-12', 'supplierId': 'supplier1'}]
    workspace['finance'] = [event('bill1', 400, '2026-09-12', 'payable', category='suppliers', linkedRecordId='po1')]
    with pytest.raises(InputError, match='cover only part'):
        execute('simulation', config, workspace, 'partial-link')
    workspace['finance'].append(event('bill2', 600, '2026-09-13', 'payable', category='suppliers', linkedRecordId='po1'))
    result = execute('simulation', config, workspace, 'fully-linked')
    assert [p['outflow'] for p in result['series']] == [400, 600, 0]
    assert [p['payable'] for p in result['series']] == [600, 0, 0]


def test_confirmed_pool_filters_known_contributions_and_forecast_scope(workspace, config):
    workspace['locations'] = [{'id': 'a'}, {'id': 'b'}, {'id': 'c'}]
    workspace['inventoryPools'] = [{'id': 'pool', 'name': 'Confirmed A and B', 'locationIds': ['a', 'b'], 'channelNames': ['Online']}]
    workspace['stock'] = [{**workspace['stock'][0], 'id': 'a-stock', 'locationId': 'a', 'onHand': 10}, {**workspace['stock'][0], 'id': 'b-stock', 'locationId': 'b', 'onHand': 20}, {**workspace['stock'][0], 'id': 'c-stock', 'locationId': 'c', 'onHand': 1000}]
    workspace['sales'] = [{**workspace['sales'][0], 'id': 'a-sale', 'locationId': 'a', 'quantity': 3}, {**workspace['sales'][0], 'id': 'b-sale', 'locationId': 'b', 'quantity': 4}, {**workspace['sales'][0], 'id': 'c-sale', 'locationId': 'c', 'quantity': 1000}]
    cfg = inventory_config(config)
    cfg['inventory_pool_id'] = 'pool'
    result = execute('simulation', cfg, workspace, 'pool')
    assert values(result)['opening_inventory'] == 30
    assert [p['inventory'] for p in result['series']] == [24, 18, 12]
    assert result['series_metadata']['scope']['location_ids'] == ['a', 'b']
    forecast = execute('forecast', cfg, workspace, 'pool-forecast')
    assert [p['demand'] for p in forecast['series']] == [7, 7, 7]


def test_pool_cannot_assume_location_for_an_unallocated_purchase(workspace, config):
    workspace['locations'] = [{'id': 'a'}]
    workspace['inventoryPools'] = [{'id': 'pool', 'locationIds': ['a']}]
    workspace['stock'][0]['locationId'] = 'a'
    workspace['purchases'] = [{'id': 'po', 'productId': 'p1', 'quantity': 10, 'receivedQuantity': 0}]
    cfg = inventory_config(config)
    cfg['inventory_pool_id'] = 'pool'
    with pytest.raises(InputError, match='no destination location'):
        execute('simulation', cfg, workspace, 'unallocated')


def test_execute_poison_apple_end_to_end(workspace, config):
    config['question'] = 'Q-POISON-APPLE'
    config['horizon_days'] = 90
    config['assumptions'] = {
        'poison_order_amount': 1000000,
        'poison_margin_pct': 40,
        'poison_supplier_advance_pct': 50,
        'poison_supplier_balance_days': 30,
        'poison_customer_days': 60,
        'poison_fixed_daily_costs': 5000,
        'cash_opening_estimate': 375000,
    }
    config['output_families'] = ['cash']
    result = execute('simulation', config, workspace, 'poison-run')
    assert values(result)['insolvency_day'] == 15
    assert values(result)['gross_margin_pct'] == 40
    assert result['series'][15]['cash'] < 0


def test_execute_dead_stock_end_to_end(workspace, config):
    config['question'] = 'Q-DEAD-STOCK'
    config['assumptions'] = {
        'dio_threshold': 120,
        'liquidation_discount_pct': 30,
        'liquidation_days': 30,
        'holding_cost_daily_pct': 0.05,
        'cash_opening_estimate': 50000,
    }
    config['output_families'] = ['cash']
    # p1 in workspace stock has 10 onHand, cost 2. If 0 sales in period, DIO is infinite -> candidate!
    workspace['sales'] = []
    result = execute('simulation', config, workspace, 'dead-stock-run')
    assert values(result)['total_candidates'] == 1
    assert values(result)['total_locked_capital'] == 20
    assert len(result['series']) == config['horizon_days']


def test_execute_treasury_stress_with_payroll_cliff(workspace, config):
    config['question'] = 'Q-TREASURY-STRESS'
    config['assumptions'] = {
        'payroll_amount': 200,
        'payroll_dates': ['2026-09-13'],
        'payroll_buffer_days': 2,
    }
    config['output_families'] = ['cash']
    workspace['cash']['amount'] = 100
    workspace['finance'] = [event('collection1', 500, '2026-09-13', 'receivable')]
    result = execute('simulation', config, workspace, 'treasury-stress-run')
    assert values(result)['payroll_reserve_required'] == 200
    assert any('payroll' in explanation.lower() for explanation in result['explanations'])
