from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from .schema import InputError, day, numeric


def dec(value: Any, label: str, minimum: float | None = None) -> Decimal:
    numeric(value, label, minimum)
    return Decimal(str(value))


def number(value: Decimal | float | int) -> float:
    return round(float(value), 6)


def metric(key: str, label: str, value: Any, unit: str) -> dict:
    val = value if unit == 'date' or value is None else number(value)
    return {'key': key, 'label': label, 'value': val, 'unit': unit}


def payroll_cliff(series: list[dict], config: dict, workspace: dict) -> dict:
    a = config.get('assumptions', {})
    payroll_amount = dec(a.get('payroll_amount', 0), 'payroll_amount', 0)
    payroll_dates = sorted([day(d, 'payroll_date') for d in a.get('payroll_dates', [])])
    payroll_buffer_days = int(a.get('payroll_buffer_days', 3))
    
    warnings = []
    events = []
    
    payroll_dates_at_risk = 0
    first_payroll_risk_date = None
    days_below_reserve = 0
    payroll_reserve_required = payroll_amount
    
    for point in series:
        current_date = day(point['date'])
        
        next_payroll = None
        for pd in payroll_dates:
            if pd >= current_date:
                next_payroll = pd
                break
                
        if next_payroll:
            reserve = payroll_amount
        else:
            reserve = Decimal(0)
            
        cash = dec(point.get('cash', 0), 'cash')
        available_cash = cash - reserve
        
        point['payroll_reserve'] = number(reserve)
        point['available_after_payroll'] = number(available_cash)
        
        if next_payroll and (next_payroll - current_date).days <= payroll_buffer_days:
            if available_cash < 0:
                days_below_reserve += 1
                if current_date == next_payroll:
                    payroll_dates_at_risk += 1
                    events.append({
                        'id': f'payroll-risk-{current_date.isoformat()}',
                        'date': current_date.isoformat(),
                        'type': 'payroll_risk',
                        'label': 'Payroll at risk',
                        'amount': number(available_cash)
                    })
                if first_payroll_risk_date is None:
                    first_payroll_risk_date = current_date.isoformat()
                    
    metrics = [
        metric('payroll_reserve_required', 'Payroll Reserve Required', payroll_reserve_required, 'currency'),
        metric('first_payroll_risk_date', 'First Payroll Risk Date', first_payroll_risk_date, 'date'),
        metric('days_below_reserve', 'Days Below Reserve', days_below_reserve, 'days'),
        metric('payroll_dates_at_risk', 'Payroll Dates at Risk', payroll_dates_at_risk, 'count')
    ]
    
    if payroll_dates_at_risk > 0:
        warnings.append('One or more payroll dates are at risk of missing the required reserve.')
        
    return {
        'series': series,
        'metrics': metrics,
        'events': events,
        'warnings': warnings,
        'explanations': []
    }


def weekend_cutoff(series: list[dict], config: dict, workspace: dict) -> dict:
    a = config.get('assumptions', {})
    banking_cutoff_apply = a.get('banking_cutoff_apply', False)
    weekend_shift_apply = a.get('weekend_shift_apply', False)
    
    if not (banking_cutoff_apply and weekend_shift_apply):
        return {'series': series, 'metrics': [], 'events': [], 'warnings': [], 'explanations': []}
        
    warnings = []
    
    phantom_liquidity_days = 0
    worst_weekend_gap = Decimal(0)
    friday_inflow_total = Decimal(0)
    
    shifted_inflows = {}
    
    for idx, point in enumerate(series):
        d = day(point['date'])
        weekday = d.weekday()
        
        original_cash = dec(point.get('cash', 0), 'cash')
        if idx == 0:
            inflow = Decimal(0)
        else:
            prev_cash = dec(series[idx-1].get('cash', 0), 'cash')
            delta = original_cash - prev_cash
            inflow = delta if delta > 0 else Decimal(0)
        
        if weekday == 4:
            friday_inflow_total += inflow
            point['friday_inflow_phantom'] = True
        elif weekday in (5, 6):
            if inflow > 0:
                monday = d + timedelta(days=7 - weekday)
                shifted_inflows[monday] = shifted_inflows.get(monday, Decimal(0)) + inflow
                point['inflow_adjusted'] = 0

    cash_banking = dec(series[0].get('cash', 0), 'cash') if series else Decimal(0)
    
    for idx, point in enumerate(series):
        d = day(point['date'])
        original_cash = dec(point.get('cash', 0), 'cash')
        
        if idx == 0:
            inflow = Decimal(0)
            outflow = Decimal(0)
        else:
            prev_original = dec(series[idx-1].get('cash', 0), 'cash')
            delta = original_cash - prev_original
            inflow = delta if delta > 0 else Decimal(0)
            outflow = -delta if delta < 0 else Decimal(0)
            
        adjusted_inflow = inflow
        if d.weekday() in (5, 6):
            adjusted_inflow = Decimal(0)
            
        adjusted_inflow += shifted_inflows.get(d, Decimal(0))
        
        if idx != 0:
             cash_banking = cash_banking + adjusted_inflow - outflow
        
        phantom_liquidity = original_cash - cash_banking
        
        if phantom_liquidity > 0:
            phantom_liquidity_days += 1
            if phantom_liquidity > worst_weekend_gap:
                worst_weekend_gap = phantom_liquidity
                
        point['cash_banking'] = number(cash_banking)
        point['phantom_liquidity'] = number(phantom_liquidity)
        
    metrics = [
        metric('phantom_liquidity_days', 'Phantom Liquidity Days', phantom_liquidity_days, 'days'),
        metric('worst_weekend_gap', 'Worst Weekend Gap', worst_weekend_gap, 'currency'),
        metric('friday_inflow_total', 'Friday Inflow Total', friday_inflow_total, 'currency')
    ]
    
    if phantom_liquidity_days > 0:
        warnings.append('Phantom liquidity detected due to weekend banking cutoffs.')
        
    return {
        'series': series,
        'metrics': metrics,
        'events': [],
        'warnings': warnings,
        'explanations': []
    }


def supplier_credit_spiral(series: list[dict], config: dict, workspace: dict) -> dict:
    a = config.get('assumptions', {})
    paused_supplier_ids = a.get('paused_supplier_ids', [])
    spiral_product_ids = a.get('spiral_product_ids', [])
    spiral_restock_penalty_days = int(a.get('spiral_restock_penalty_days', 15))
    
    warnings = []
    events = []
    
    paused_purchases = [p for p in workspace.get('purchases', []) if p.get('supplierId') in paused_supplier_ids]
    affected_product_ids = {p.get('productId') for p in paused_purchases}
    star_products_affected = affected_product_ids.intersection(set(spiral_product_ids))
    
    star_stock = Decimal(0)
    for st in workspace.get('stock', []):
        if st.get('productId') in star_products_affected:
            star_stock += dec(st.get('quantity', 0), 'stock_quantity')
            
    sales_total = Decimal(0)
    sales_qty = Decimal(0)
    sales_dates = set()
    for s in workspace.get('sales', []):
        if s.get('productId') in star_products_affected:
            sales_total += dec(s.get('amount', 0), 'sale_amount')
            sales_qty += dec(s.get('quantity', 1), 'sale_quantity')
            if 'date' in s:
                sales_dates.add(s['date'])
                
    sales_days = max(len(sales_dates), 1)
    daily_demand = sales_qty / Decimal(sales_days)
    star_daily_revenue = sales_total / Decimal(sales_days)
    
    start_date = day(config['start_date'])
    penalty_date = start_date + timedelta(days=spiral_restock_penalty_days)
    
    spiral_depth_days = 0
    cumulative_revenue_loss = Decimal(0)
    original_vs_spiral_floor_delta = Decimal(0)
    
    original_floor = None
    spiral_floor = None
    
    spiral_cash = dec(series[0].get('cash', 0), 'cash') if series else Decimal(0)
    current_stock = star_stock
    stockout_date = None
    
    for idx, point in enumerate(series):
        current_date = day(point['date'])
        original_cash = dec(point.get('cash', 0), 'cash')
        
        revenue_loss = Decimal(0)
        blocked = False
        
        if current_date >= penalty_date and star_products_affected:
            blocked = True
            if current_date == penalty_date:
                events.append({
                    'id': f'supplier-block-{current_date.isoformat()}',
                    'date': current_date.isoformat(),
                    'type': 'supplier_block',
                    'label': 'Supplier deliveries frozen',
                    'amount': 0
                })
                
        current_stock -= daily_demand
        
        if blocked and current_stock <= 0:
            revenue_loss = star_daily_revenue
            cumulative_revenue_loss += revenue_loss
            if stockout_date is None:
                stockout_date = current_date
                events.append({
                    'id': f'stockout-{current_date.isoformat()}',
                    'date': current_date.isoformat(),
                    'type': 'stockout',
                    'label': 'Star products stockout',
                    'amount': 0
                })
                
        if idx == 0:
            delta = Decimal(0)
        else:
            prev_original = dec(series[idx-1].get('cash', 0), 'cash')
            delta = original_cash - prev_original
            
        spiral_cash = spiral_cash + delta - revenue_loss
        
        point['cash_spiral'] = number(spiral_cash)
        point['blocked_products'] = blocked
        point['revenue_loss_daily'] = number(revenue_loss)
        
        if original_floor is None or original_cash < original_floor:
            original_floor = original_cash
        if spiral_floor is None or spiral_cash < spiral_floor:
            spiral_floor = spiral_cash
            
        if spiral_cash < original_cash:
            spiral_depth_days += 1
            
    if original_floor is not None and spiral_floor is not None:
        original_vs_spiral_floor_delta = original_floor - spiral_floor
        
    metrics = [
        metric('spiral_depth_days', 'Spiral Depth Days', spiral_depth_days, 'days'),
        metric('cumulative_revenue_loss', 'Cumulative Revenue Loss', cumulative_revenue_loss, 'currency'),
        metric('original_vs_spiral_floor_delta', 'Original vs Spiral Floor Delta', original_vs_spiral_floor_delta, 'currency'),
        metric('products_affected', 'Products Affected', len(star_products_affected), 'count')
    ]
    
    if cumulative_revenue_loss > 0:
        warnings.append('Supplier credit spiral detected, impacting revenue.')
        
    return {
        'series': series,
        'metrics': metrics,
        'events': events,
        'warnings': warnings,
        'explanations': []
    }


def payment_dispute_hold(series: list[dict], config: dict, workspace: dict) -> dict:
    a = config.get('assumptions', {})
    disputed_record_ids = a.get('disputed_record_ids', [])
    dispute_resolution_days = int(a.get('dispute_resolution_days', 45))
    dispute_recovery_pct = dec(a.get('dispute_recovery_pct', 80), 'recovery_pct')
    
    warnings = []
    events = []
    
    disputed_records = [r for r in workspace.get('finance', []) if r.get('id') in disputed_record_ids]
    
    frozen_amount = Decimal(0)
    expected_recovery = Decimal(0)
    permanent_loss = Decimal(0)
    
    day_shifts = {}
    
    for r in disputed_records:
        if r.get('kind') == 'receivable':
            amount = dec(r.get('amount', 0), 'amount')
            expected_date = r.get('expectedDate') or r.get('dueDate')
            if expected_date:
                expected_date_parsed = day(expected_date)
                frozen_amount += amount
                recovery = amount * (dispute_recovery_pct / Decimal(100))
                loss = amount - recovery
                expected_recovery += recovery
                permanent_loss += loss
                
                resolved_date = expected_date_parsed + timedelta(days=dispute_resolution_days)
                
                day_shifts[expected_date_parsed] = day_shifts.get(expected_date_parsed, Decimal(0)) - amount
                day_shifts[resolved_date] = day_shifts.get(resolved_date, Decimal(0)) + recovery
                
    cash_floor_with_disputes = None
    additional_deficit = Decimal(0)
    
    cumulative_shift = Decimal(0)
    resolution_date = None
    
    for point in series:
        current_date = day(point['date'])
        
        shift = day_shifts.get(current_date, Decimal(0))
        cumulative_shift += shift
        
        original_cash = dec(point.get('cash', 0), 'cash')
        cash_disputed = original_cash + cumulative_shift
        
        point['cash_disputed'] = number(cash_disputed)
        
        if cash_floor_with_disputes is None or cash_disputed < cash_floor_with_disputes:
            cash_floor_with_disputes = cash_disputed
            
        if cash_disputed < 0 and original_cash >= 0:
            additional_deficit += -cash_disputed
            
        if current_date in day_shifts and day_shifts[current_date] > 0:
            resolution_date = current_date.isoformat()
            
    metrics = [
        metric('frozen_amount', 'Frozen Amount', frozen_amount, 'currency'),
        metric('expected_recovery', 'Expected Recovery', expected_recovery, 'currency'),
        metric('permanent_loss', 'Permanent Loss', permanent_loss, 'currency'),
        metric('cash_floor_with_disputes', 'Cash Floor With Disputes', cash_floor_with_disputes, 'currency'),
        metric('additional_deficit', 'Additional Deficit', additional_deficit, 'currency'),
        metric('resolution_date', 'Resolution Date', resolution_date, 'date')
    ]
    
    if frozen_amount > 0:
        warnings.append('Payment disputes are holding back cash and may cause permanent loss.')
        
    return {
        'series': series,
        'metrics': metrics,
        'events': events,
        'warnings': warnings,
        'explanations': []
    }


def validate_treasury_stress(config: dict) -> None:
    a = config.get('assumptions', {})
    
    if 'payroll_amount' in a:
        numeric(a['payroll_amount'], 'payroll_amount', 0)
    if 'payroll_dates' in a:
        if not isinstance(a['payroll_dates'], list):
            raise InputError('payroll_dates must be a list of dates.')
        for d in a['payroll_dates']:
            day(d, 'payroll_date')
    if 'payroll_buffer_days' in a:
        numeric(a['payroll_buffer_days'], 'payroll_buffer_days', 0)
        
    for key in ['banking_cutoff_apply', 'weekend_shift_apply']:
        if key in a and not isinstance(a[key], bool):
            raise InputError(f'{key} must be a boolean.')
            
    if 'paused_supplier_ids' in a:
        if not isinstance(a['paused_supplier_ids'], list):
            raise InputError('paused_supplier_ids must be a list.')
    if 'spiral_product_ids' in a:
        if not isinstance(a['spiral_product_ids'], list):
            raise InputError('spiral_product_ids must be a list.')
    if 'spiral_restock_penalty_days' in a:
        numeric(a['spiral_restock_penalty_days'], 'spiral_restock_penalty_days', 0)
        
    if 'disputed_record_ids' in a:
        if not isinstance(a['disputed_record_ids'], list):
            raise InputError('disputed_record_ids must be a list.')
    if 'dispute_resolution_days' in a:
        numeric(a['dispute_resolution_days'], 'dispute_resolution_days', 0)
    if 'dispute_recovery_pct' in a:
        numeric(a['dispute_recovery_pct'], 'dispute_recovery_pct', 0)
