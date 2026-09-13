import pytest
from app.prototype.dead_stock_liberator import scan_dead_stock, simulate_liquidation


def test_scan_identifies_high_dio_skus():
    config = {
        'start_date': '2025-01-01',
        'horizon_days': 90,
        'assumptions': {'dio_threshold': 120}
    }
    workspace = {
        'profile': {'currency': 'MXN'},
        'products': [
            {'id': 'p1', 'name': 'Product A', 'unit': 'pz', 'cost': 50, 'price': 100},
            {'id': 'p2', 'name': 'Product B', 'unit': 'pz', 'cost': 50, 'price': 100}
        ],
        'stock': [
            {'productId': 'p1', 'onHand': 100, 'locationId': 'loc1', 'asOf': '2025-01-01', 'quantityBasis': 'on-hand'},
            {'productId': 'p2', 'onHand': 100, 'locationId': 'loc1', 'asOf': '2025-01-01', 'quantityBasis': 'on-hand'}
        ],
        'sales': [
            {'productId': 'p1', 'date': '2025-01-15', 'quantity': 5, 'unit': 'pz', 'unitCost': 50},
            {'productId': 'p2', 'date': '2025-01-15', 'quantity': 200, 'unit': 'pz', 'unitCost': 50}
        ]
    }
    
    result = scan_dead_stock(workspace, config)
    candidates = result['candidates']
    
    assert len(candidates) == 1
    assert candidates[0]['product_id'] == 'p1'
    assert candidates[0]['dio'] == 1800.0  # (5000 / 250) * 90


def test_liquidation_calculates_correct_capital_freed():
    config = {
        'start_date': '2025-01-01',
        'horizon_days': 90,
        'assumptions': {
            'liquidation_discount_pct': 30,
            'liquidation_days': 30,
            'holding_cost_daily_pct': 0.05,
            'cash_opening_estimate': 10000
        }
    }
    workspace = {
        'profile': {'currency': 'MXN'},
        'products': [
            {'id': 'p1', 'name': 'Product A', 'unit': 'pz', 'cost': 50, 'price': 100}
        ]
    }
    candidates = [{
        'product_id': 'p1',
        'product_name': 'Product A',
        'unit': 'pz',
        'on_hand': 100,
        'unit_cost': 50,
        'locked_capital': 5000,
        'dio': 1800.0,
        'daily_demand_rate': 0.055556
    }]
    
    result = simulate_liquidation(workspace, candidates, config)
    metrics = {m['key']: m['value'] for m in result['metrics']}
    
    # price 100, 30% discount -> 70. 100 units -> 7000
    assert metrics['capital_freed'] == 7000.0


def test_holding_cost_savings_calculation():
    config = {
        'start_date': '2025-01-01',
        'horizon_days': 90,
        'assumptions': {
            'liquidation_discount_pct': 30,
            'liquidation_days': 30,
            'holding_cost_daily_pct': 0.05,
            'cash_opening_estimate': 10000
        }
    }
    workspace = {
        'profile': {'currency': 'MXN'},
        'products': [
            {'id': 'p1', 'name': 'Product A', 'unit': 'pz', 'cost': 50, 'price': 100}
        ]
    }
    candidates = [{
        'product_id': 'p1',
        'product_name': 'Product A',
        'unit': 'pz',
        'on_hand': 100,
        'unit_cost': 50,
        'locked_capital': 5000,
        'dio': 1800.0,
        'daily_demand_rate': 0.055556
    }]
    
    result = simulate_liquidation(workspace, candidates, config)
    metrics = {m['key']: m['value'] for m in result['metrics']}
    
    # 5000 locked capital * (0.05 / 100) * 365 = 912.5
    assert metrics['annual_holding_cost_saved'] == 912.5


def test_no_candidates_below_threshold():
    config = {
        'start_date': '2025-01-01',
        'horizon_days': 90,
        'assumptions': {'dio_threshold': 120}
    }
    workspace = {
        'profile': {'currency': 'MXN'},
        'products': [
            {'id': 'p2', 'name': 'Product B', 'unit': 'pz', 'cost': 50, 'price': 100}
        ],
        'stock': [
            {'productId': 'p2', 'onHand': 100, 'locationId': 'loc1', 'asOf': '2025-01-01', 'quantityBasis': 'on-hand'}
        ],
        'sales': [
            {'productId': 'p2', 'date': '2025-01-15', 'quantity': 200, 'unit': 'pz', 'unitCost': 50}
        ]
    }
    
    result = scan_dead_stock(workspace, config)
    candidates = result['candidates']
    
    assert len(candidates) == 0


def test_discount_impact_on_net_benefit():
    def _run_sim(discount):
        config = {
            'start_date': '2025-01-01',
            'horizon_days': 90,
            'assumptions': {
                'liquidation_discount_pct': discount,
                'liquidation_days': 30,
                'holding_cost_daily_pct': 0.05,
                'cash_opening_estimate': 10000
            }
        }
        workspace = {
            'profile': {'currency': 'MXN'},
            'products': [
                {'id': 'p1', 'name': 'Product A', 'unit': 'pz', 'cost': 50, 'price': 100}
            ]
        }
        candidates = [{
            'product_id': 'p1',
            'product_name': 'Product A',
            'unit': 'pz',
            'on_hand': 100,
            'unit_cost': 50,
            'locked_capital': 5000,
            'dio': 1800.0,
            'daily_demand_rate': 0.055556
        }]
        
        result = simulate_liquidation(workspace, candidates, config)
        return {m['key']: m['value'] for m in result['metrics']}
        
    metrics_10 = _run_sim(10)
    metrics_50 = _run_sim(50)
    
    assert metrics_10['net_benefit_90d'] > metrics_50['net_benefit_90d']
