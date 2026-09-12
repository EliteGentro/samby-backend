from datetime import date, timedelta
from decimal import Decimal

from app.prototype.treasury_edge_cases import (
    payroll_cliff,
    weekend_cutoff,
    supplier_credit_spiral,
    payment_dispute_hold
)

def test_payroll_reserve_blocks_available_cash():
    series = [
        {'date': '2025-01-10', 'cash': 100000},
        {'date': '2025-01-11', 'cash': 100000},
        {'date': '2025-01-12', 'cash': 100000},
        {'date': '2025-01-13', 'cash': 100000},
        {'date': '2025-01-14', 'cash': 100000},
        {'date': '2025-01-15', 'cash': 100000},
    ]
    config = {
        'start_date': '2025-01-10',
        'assumptions': {
            'payroll_amount': 80000,
            'payroll_dates': ['2025-01-15'],
            'payroll_buffer_days': 5
        }
    }
    
    result = payroll_cliff(series, config, {})
    
    for point in result['series']:
        if point['date'] <= '2025-01-15':
            assert point['available_after_payroll'] == 20000.0
            assert point['payroll_reserve'] == 80000.0


def test_payroll_date_triggers_cliff():
    series = [
        {'date': '2025-01-13', 'cash': 50000},
        {'date': '2025-01-14', 'cash': 50000},
        {'date': '2025-01-15', 'cash': 50000},
    ]
    config = {
        'start_date': '2025-01-13',
        'assumptions': {
            'payroll_amount': 60000,
            'payroll_dates': ['2025-01-15'],
            'payroll_buffer_days': 3
        }
    }
    
    result = payroll_cliff(series, config, {})
    
    for point in result['series']:
        assert point['available_after_payroll'] == -10000.0
        
    dates_at_risk_metric = next(m for m in result['metrics'] if m['key'] == 'payroll_dates_at_risk')
    assert dates_at_risk_metric['value'] >= 1
    assert 'One or more payroll dates are at risk of missing the required reserve.' in result['warnings']


def test_friday_inflows_shift_to_monday():
    # 2025-01-10 is a Friday
    series = [
        {'date': '2025-01-10', 'cash': 10000}, # Friday
        {'date': '2025-01-11', 'cash': 15000}, # Saturday, inflow of 5000
    ]
    config = {
        'start_date': '2025-01-10',
        'assumptions': {
            'banking_cutoff_apply': True,
            'weekend_shift_apply': True
        }
    }
    
    result = weekend_cutoff(series, config, {})
    
    sat_point = result['series'][1]
    assert sat_point['cash_banking'] == 10000.0
    assert sat_point['phantom_liquidity'] == 5000.0


def test_phantom_liquidity_detected():
    # 2025-01-10 is Friday
    series = [
        {'date': '2025-01-10', 'cash': -5000}, # Friday
        {'date': '2025-01-11', 'cash': 5000},  # Saturday, inflow of 10000
    ]
    config = {
        'start_date': '2025-01-10',
        'assumptions': {
            'banking_cutoff_apply': True,
            'weekend_shift_apply': True
        }
    }
    
    result = weekend_cutoff(series, config, {})
    
    sat_point = result['series'][1]
    assert sat_point['cash'] == 5000
    assert sat_point['cash_banking'] == -5000.0
    assert sat_point['phantom_liquidity'] == 10000.0
    
    phantom_days_metric = next(m for m in result['metrics'] if m['key'] == 'phantom_liquidity_days')
    assert phantom_days_metric['value'] > 0


def test_paused_supplier_blocks_delivery():
    series = [
        {'date': '2025-01-01', 'cash': 10000},
        {'date': '2025-01-02', 'cash': 10000},
        {'date': '2025-01-03', 'cash': 10000},
    ]
    config = {
        'start_date': '2025-01-01',
        'assumptions': {
            'paused_supplier_ids': ['sup1'],
            'spiral_product_ids': ['prod1'],
            'spiral_restock_penalty_days': 2
        }
    }
    workspace = {
        'purchases': [{'supplierId': 'sup1', 'productId': 'prod1'}],
        'stock': [{'productId': 'prod1', 'quantity': 100}],
        'sales': [{'productId': 'prod1', 'amount': 1000, 'quantity': 10, 'date': '2025-01-01'}]
    }
    
    result = supplier_credit_spiral(series, config, workspace)
    
    assert result['series'][0]['blocked_products'] is False
    assert result['series'][1]['blocked_products'] is False
    assert result['series'][2]['blocked_products'] is True


def test_revenue_drops_after_stockout():
    series = [
        {'date': '2025-01-01', 'cash': 10000},
        {'date': '2025-01-02', 'cash': 10000},
        {'date': '2025-01-03', 'cash': 10000},
    ]
    config = {
        'start_date': '2025-01-01',
        'assumptions': {
            'paused_supplier_ids': ['sup1'],
            'spiral_product_ids': ['prod1'],
            'spiral_restock_penalty_days': 0
        }
    }
    workspace = {
        'purchases': [{'supplierId': 'sup1', 'productId': 'prod1'}],
        'stock': [{'productId': 'prod1', 'quantity': 10}],
        'sales': [{'productId': 'prod1', 'amount': 1000, 'quantity': 10, 'date': '2025-01-01'}]
    }
    
    result = supplier_credit_spiral(series, config, workspace)
    
    # Day 0: stock=10 - 10 = 0. blocked is True, so stockout occurs.
    assert result['series'][0]['revenue_loss_daily'] == 1000.0
    assert result['series'][1]['revenue_loss_daily'] == 1000.0
    
    rev_loss_metric = next(m for m in result['metrics'] if m['key'] == 'cumulative_revenue_loss')
    assert rev_loss_metric['value'] > 0


def test_disputed_amount_frozen():
    series = [
        {'date': '2025-01-01', 'cash': 50000},
    ]
    config = {
        'start_date': '2025-01-01',
        'assumptions': {
            'disputed_record_ids': ['rec1'],
            'dispute_recovery_pct': 100
        }
    }
    workspace = {
        'finance': [
            {'id': 'rec1', 'kind': 'receivable', 'amount': 10000, 'expectedDate': '2025-01-10'}
        ]
    }
    
    result = payment_dispute_hold(series, config, workspace)
    
    frozen_metric = next(m for m in result['metrics'] if m['key'] == 'frozen_amount')
    assert frozen_metric['value'] == 10000.0


def test_partial_recovery_after_resolution():
    series = [
        {'date': '2025-01-01', 'cash': 50000},
    ]
    config = {
        'start_date': '2025-01-01',
        'assumptions': {
            'disputed_record_ids': ['rec1'],
            'dispute_recovery_pct': 80
        }
    }
    workspace = {
        'finance': [
            {'id': 'rec1', 'kind': 'receivable', 'amount': 100000, 'expectedDate': '2025-01-10'}
        ]
    }
    
    result = payment_dispute_hold(series, config, workspace)
    
    expected_recovery_metric = next(m for m in result['metrics'] if m['key'] == 'expected_recovery')
    assert expected_recovery_metric['value'] == 80000.0
    
    permanent_loss_metric = next(m for m in result['metrics'] if m['key'] == 'permanent_loss')
    assert permanent_loss_metric['value'] == 20000.0
