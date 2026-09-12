from app.prototype.poison_apple import simulate_poison_apple

def base_config():
    return {
        'start_date': '2025-01-01',
        'horizon_days': 90,
        'question': 'Q-POISON-APPLE',
        'assumptions': {
            'poison_order_amount': 1000000,
            'poison_margin_pct': 40,
            'poison_supplier_advance_pct': 50,
            'poison_supplier_balance_days': 30,
            'poison_customer_days': 60,
            'poison_fixed_daily_costs': 5000,
            'cash_opening_estimate': 375000,
        }
    }

def empty_workspace():
    return {'profile': {'currency': 'MXN'}, 'finance': []}

def get_metric(result, key):
    for m in result['metrics']:
        if m['key'] == key:
            return m['value']
    return None

def test_insolvency_detected_at_correct_day():
    config = base_config()
    result = simulate_poison_apple(config, empty_workspace())
    
    insolvency_day = get_metric(result, 'insolvency_day')
    assert insolvency_day == 15

def test_higher_margin_delays_insolvency():
    config1 = base_config()
    result1 = simulate_poison_apple(config1, empty_workspace())
    
    config2 = base_config()
    config2['assumptions']['poison_margin_pct'] = 60
    result2 = simulate_poison_apple(config2, empty_workspace())
    
    insolvency_day1 = get_metric(result1, 'insolvency_day')
    insolvency_day2 = get_metric(result2, 'insolvency_day')
    
    if insolvency_day2 is not None:
        assert insolvency_day2 > insolvency_day1
    else:
        assert insolvency_day2 is None

def test_shorter_customer_terms_avoids_insolvency():
    config = base_config()
    config['assumptions']['poison_customer_days'] = 10
    result = simulate_poison_apple(config, empty_workspace())
    
    insolvency_day = get_metric(result, 'insolvency_day')
    assert insolvency_day is None

def test_no_insolvency_with_sufficient_opening_cash():
    config = base_config()
    config['assumptions']['cash_opening_estimate'] = 1000000
    result = simulate_poison_apple(config, empty_workspace())
    
    insolvency_day = get_metric(result, 'insolvency_day')
    assert insolvency_day is None

def test_metrics_working_capital_gap_correct():
    config = base_config()
    result = simulate_poison_apple(config, empty_workspace())
    
    cash_floor = get_metric(result, 'cash_floor')
    wc_req = get_metric(result, 'working_capital_required')
    
    assert cash_floor < 0
    assert wc_req == -cash_floor
