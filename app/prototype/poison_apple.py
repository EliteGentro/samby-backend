from datetime import timedelta
from decimal import Decimal
from typing import Any

from .schema import InputError, day, numeric

def dec(value: Any, label: str, minimum: float | None = None) -> Decimal:
    numeric(value, label, minimum)
    return Decimal(str(value))

def number(value: Decimal | float | int) -> float:
    return round(float(value), 6)

def metric(key: str, label: str, value: Any, unit: str) -> dict:
    if value is None:
        val = None
    elif isinstance(value, str):
        val = value
    else:
        val = number(value)
    return {'key': key, 'label': label, 'value': val, 'unit': unit}

def validate_poison_apple(config: dict) -> None:
    assumptions = config.get('assumptions', {})
    if 'poison_order_amount' not in assumptions:
        raise InputError('poison_order_amount must be provided.')
    numeric(assumptions['poison_order_amount'], 'poison_order_amount', 0)
    numeric(assumptions.get('poison_margin_pct', 40), 'poison_margin_pct')
    numeric(assumptions.get('poison_supplier_advance_pct', 50), 'poison_supplier_advance_pct', 0)
    numeric(assumptions.get('poison_supplier_balance_days', 30), 'poison_supplier_balance_days', 0)
    numeric(assumptions.get('poison_customer_days', 60), 'poison_customer_days', 0)
    if 'poison_fixed_daily_costs' not in assumptions:
        raise InputError('poison_fixed_daily_costs must be provided.')
    numeric(assumptions['poison_fixed_daily_costs'], 'poison_fixed_daily_costs', 0)
    
    if 'horizon_days' not in config:
        raise InputError('horizon_days must be provided in config.')
    numeric(config['horizon_days'], 'horizon_days', 1)
    
    if 'start_date' not in config:
        raise InputError('start_date must be provided in config.')
    day(config['start_date'], 'start_date')


def simulate_poison_apple(config: dict, workspace: dict) -> dict:
    validate_poison_apple(config)
    
    assumptions = config.get('assumptions', {})
    order_amount = dec(assumptions['poison_order_amount'], 'poison_order_amount')
    margin_pct = dec(assumptions.get('poison_margin_pct', 40), 'poison_margin_pct')
    advance_pct = dec(assumptions.get('poison_supplier_advance_pct', 50), 'poison_supplier_advance_pct')
    supplier_days = int(assumptions.get('poison_supplier_balance_days', 30))
    customer_days = int(assumptions.get('poison_customer_days', 60))
    daily_fixed_costs = dec(assumptions['poison_fixed_daily_costs'], 'poison_fixed_daily_costs')
    
    start_date = day(config['start_date'])
    horizon = int(config['horizon_days'])
    
    opening_cash = Decimal('0')
    if 'cash_opening_estimate' in assumptions:
        opening_cash = dec(assumptions['cash_opening_estimate'], 'cash_opening_estimate')
    elif 'opening_cash' in assumptions:
        opening_cash = dec(assumptions['opening_cash'], 'opening_cash')
    elif workspace and 'finance' in workspace:
        opening_cash = sum((Decimal(str(r.get('amount', 0))) for r in workspace['finance'] if r.get('kind') == 'cash'), Decimal('0'))
        
    cogs = order_amount * (Decimal('100') - margin_pct) / Decimal('100')
    gross_profit = order_amount - cogs
    supplier_advance = cogs * advance_pct / Decimal('100')
    supplier_balance = cogs - supplier_advance
    
    series = []
    events = []
    warnings = []
    explanations = []
    
    current_cash = opening_cash
    cum_supplier_paid = Decimal('0')
    cum_collected = Decimal('0')
    
    cash_floor = opening_cash
    cash_floor_date = start_date.isoformat()
    insolvency_day = None
    days_cash_negative = 0
    
    for day_num in range(horizon):
        current_date = (start_date + timedelta(days=day_num)).isoformat()
        
        inflow = Decimal('0')
        outflow = Decimal('0')
        
        outflow += daily_fixed_costs
        
        if day_num == 0:
            outflow += supplier_advance
            cum_supplier_paid += supplier_advance
            events.append({'id': f'poison-advance-{current_date}', 'date': current_date, 'type': 'supplier_advance', 'amount': number(supplier_advance)})
            
        if day_num == supplier_days:
            outflow += supplier_balance
            cum_supplier_paid += supplier_balance
            events.append({'id': f'poison-balance-{current_date}', 'date': current_date, 'type': 'supplier_balance', 'amount': number(supplier_balance)})
            
        if day_num == customer_days:
            inflow += order_amount
            cum_collected += order_amount
            events.append({'id': f'poison-collection-{current_date}', 'date': current_date, 'type': 'customer_collection', 'amount': number(order_amount)})
            
        current_cash = current_cash + inflow - outflow
        
        if current_cash < cash_floor:
            cash_floor = current_cash
            cash_floor_date = current_date
            
        if current_cash < 0:
            days_cash_negative += 1
            if insolvency_day is None:
                insolvency_day = day_num
                warnings.append(f'Insolvency reached on day {day_num} ({current_date}) due to cash gap.')
                
        series.append({
            'date': current_date,
            'cash': number(current_cash),
            'inflow': number(inflow),
            'outflow': number(outflow),
            'cumulative_supplier_paid': number(cum_supplier_paid),
            'cumulative_collected': number(cum_collected)
        })
        
    metrics = [
        metric('order_revenue', 'Order Revenue', order_amount, 'currency'),
        metric('cogs', 'COGS', cogs, 'currency'),
        metric('gross_profit', 'Gross Profit', gross_profit, 'currency'),
        metric('gross_margin_pct', 'Gross Margin %', margin_pct, 'percent'),
        metric('supplier_advance', 'Supplier Advance', supplier_advance, 'currency'),
        metric('supplier_balance', 'Supplier Balance', supplier_balance, 'currency'),
        metric('total_supplier_payment', 'Total Supplier Payment', cogs, 'currency'),
        metric('insolvency_day', 'Insolvency Day', insolvency_day, 'days'),
        metric('cash_floor', 'Minimum Cash', cash_floor, 'currency'),
        metric('cash_floor_date', 'Date of Minimum Cash', cash_floor_date, 'date'),
        metric('max_deficit', 'Maximum Deficit', -cash_floor if cash_floor < 0 else Decimal('0'), 'currency'),
        metric('working_capital_required', 'Working Capital Required', max(Decimal('0'), -cash_floor), 'currency'),
        metric('days_cash_negative', 'Days Cash Negative', days_cash_negative, 'days')
    ]
    
    if insolvency_day is not None:
        explanations.append(f'The order requires upfront supplier payments and incurs fixed costs while waiting {customer_days} days for customer collection, resulting in a cash shortfall.')
    else:
        explanations.append('Cash reserves are sufficient to bridge the working capital gap.')
        
    return {
        'series': series,
        'metrics': metrics,
        'events': events,
        'warnings': warnings,
        'explanations': explanations
    }
