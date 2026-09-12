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
    return {'key': key, 'label': label, 'value': None if value is None else number(value), 'unit': unit}


def scan_dead_stock(workspace: dict, config: dict) -> dict:
    assumptions = config.get('assumptions', {})
    dio_threshold = numeric(assumptions.get('dio_threshold', 120), 'DIO threshold', 0)
    
    start_iso = config.get('start_date')
    if not start_iso:
        raise InputError('A start_date is required to determine the analysis period.')
        
    horizon_days = config.get('horizon_days')
    if not horizon_days:
        raise InputError('horizon_days is required to determine the analysis period length.')
        
    end_iso = (day(start_iso) + timedelta(days=horizon_days - 1)).isoformat()
    period_days = Decimal(horizon_days)

    products = {p['id']: p for p in workspace.get('products', [])}
    
    stock_by_product = {}
    for s in workspace.get('stock', []):
        pid = s.get('productId')
        if not pid:
            continue
        on_hand = dec(s.get('onHand', 0), 'Stock on hand', 0)
        stock_by_product[pid] = stock_by_product.get(pid, Decimal(0)) + on_hand

    cogs_by_product = {}
    sales_qty_by_product = {}
    for sale in workspace.get('sales', []):
        date_str = sale.get('date')
        if not date_str or not (start_iso <= date_str <= end_iso):
            continue
        pid = sale.get('productId')
        if not pid:
            continue
        qty = dec(sale.get('quantity', 0), 'Sale quantity', 0)
        unit_cost = dec(sale.get('unitCost', 0), 'Sale unit cost', 0)
        cogs_by_product[pid] = cogs_by_product.get(pid, Decimal(0)) + (qty * unit_cost)
        sales_qty_by_product[pid] = sales_qty_by_product.get(pid, Decimal(0)) + qty

    candidates = []
    warnings = []

    for pid, on_hand in stock_by_product.items():
        if on_hand <= 0:
            continue
            
        product = products.get(pid)
        if not product:
            warnings.append(f"Stock found for unknown product {pid}.")
            continue
        
        cost_val = product.get('cost')
        if cost_val is None:
            warnings.append(f"Product '{product.get('name', pid)}' is missing cost data. Cannot compute locked capital.")
            continue
        
        unit_cost = dec(cost_val, f"Cost for {product.get('name', pid)}", 0)
        locked_capital = on_hand * unit_cost

        cogs = cogs_by_product.get(pid, Decimal(0))
        sales_qty = sales_qty_by_product.get(pid, Decimal(0))
        daily_demand_rate = sales_qty / period_days

        if cogs > 0:
            dio = (locked_capital / cogs) * period_days
            dio_float = number(dio)
        else:
            dio_float = None

        if dio_float is None or dio_float > dio_threshold:
            candidates.append({
                'product_id': pid,
                'product_name': product.get('name', pid),
                'unit': product.get('unit', 'units'),
                'on_hand': number(on_hand),
                'unit_cost': number(unit_cost),
                'locked_capital': number(locked_capital),
                'dio': dio_float,
                'daily_demand_rate': number(daily_demand_rate)
            })

    return {'candidates': candidates, 'warnings': warnings}


def simulate_liquidation(workspace: dict, candidates: list[dict], config: dict) -> dict:
    assumptions = config.get('assumptions', {})
    start_iso = config.get('start_date')
    if not start_iso:
        raise InputError('start_date is required for the simulation.')
        
    horizon_days = config.get('horizon_days')
    if not horizon_days:
        raise InputError('horizon_days is required for the simulation.')
        
    opening_cash = dec(assumptions.get('cash_opening_estimate', 0), 'Opening cash')
    
    discount = dec(assumptions.get('liquidation_discount_pct', 30), 'Liquidation discount percentage')
    liquidation_days = int(number(dec(assumptions.get('liquidation_days', 30), 'Liquidation days', 1)))
    holding_cost_daily_pct = dec(assumptions.get('holding_cost_daily_pct', 0.05), 'Holding cost daily percentage')
    
    currency = workspace.get('profile', {}).get('currency', 'USD')
    
    products = {p['id']: p for p in workspace.get('products', [])}
    
    total_locked_capital = Decimal(0)
    total_sale_revenue = Decimal(0)
    total_discount_loss = Decimal(0)
    
    enriched_candidates = []
    
    for c in candidates:
        pid = c['product_id']
        product = products.get(pid, {})
        on_hand = dec(c['on_hand'], 'On hand')
        unit_cost = dec(c['unit_cost'], 'Unit cost')
        
        price_val = product.get('price')
        if price_val is not None:
            price = dec(price_val, 'Price', 0)
            sale_revenue = on_hand * price * (Decimal(1) - discount / Decimal(100))
            discount_loss = on_hand * price * (discount / Decimal(100))
        else:
            sale_revenue = on_hand * unit_cost
            discount_loss = Decimal(0)
            
        locked_capital = on_hand * unit_cost
        total_locked_capital += locked_capital
        total_sale_revenue += sale_revenue
        total_discount_loss += discount_loss
        
        c_enriched = c.copy()
        c_enriched['sale_revenue'] = number(sale_revenue)
        c_enriched['discount_loss'] = number(discount_loss)
        enriched_candidates.append(c_enriched)
        
    cash_hold = opening_cash
    cash_liquidate = opening_cash
    remaining_locked_capital = total_locked_capital
    
    daily_holding_cost_rate = holding_cost_daily_pct / Decimal(100)
    daily_revenue = total_sale_revenue / Decimal(liquidation_days) if liquidation_days > 0 else Decimal(0)
    daily_capital_reduction = total_locked_capital / Decimal(liquidation_days) if liquidation_days > 0 else Decimal(0)
    
    cumulative_freed = Decimal(0)
    series = []
    
    for t in range(horizon_days):
        date_str = (day(start_iso) + timedelta(days=t)).isoformat()
        
        hold_cost = total_locked_capital * daily_holding_cost_rate
        cash_hold -= hold_cost
        
        if t < liquidation_days:
            revenue_today = daily_revenue
            cash_liquidate += revenue_today
            cumulative_freed += revenue_today
            
            liq_hold_cost = remaining_locked_capital * daily_holding_cost_rate
            cash_liquidate -= liq_hold_cost
            
            remaining_locked_capital -= daily_capital_reduction
            if remaining_locked_capital < 0:
                remaining_locked_capital = Decimal(0)
        else:
            revenue_today = Decimal(0)
            liq_hold_cost = Decimal(0)
            cash_liquidate -= liq_hold_cost
            
        series.append({
            'date': date_str,
            'cash_hold': number(cash_hold),
            'cash_liquidate': number(cash_liquidate),
            'daily_liquidation_revenue': number(revenue_today),
            'cumulative_freed': number(cumulative_freed)
        })
        
    finite_dios = [c['dio'] for c in candidates if c['dio'] is not None]
    avg_dio = sum(finite_dios) / len(finite_dios) if finite_dios else None
    
    annual_holding_cost_saved = total_locked_capital * daily_holding_cost_rate * Decimal(365)
    net_benefit_90d = cash_liquidate - cash_hold
    
    metrics = [
        metric('total_locked_capital', 'Total Locked Capital', total_locked_capital, currency),
        metric('total_candidates', 'Total Candidates', len(candidates), 'SKUs'),
        metric('avg_dio', 'Average DIO', avg_dio, 'days'),
        metric('capital_freed', 'Capital Freed', total_sale_revenue, currency),
        metric('discount_cost', 'Discount Cost', total_discount_loss, currency),
        metric('annual_holding_cost_saved', 'Annual Holding Cost Saved', annual_holding_cost_saved, currency),
        metric('net_benefit_90d', 'Net Benefit (End of Horizon)', net_benefit_90d, currency),
        metric('cash_floor_hold', 'Cash Floor (Hold)', min(s['cash_hold'] for s in series) if series else cash_hold, currency),
        metric('cash_floor_liquidate', 'Cash Floor (Liquidate)', min(s['cash_liquidate'] for s in series) if series else cash_liquidate, currency)
    ]
    
    events = []
    warnings = []
    if not candidates:
        warnings.append('No dead stock candidates provided for simulation.')
    if liquidation_days > horizon_days:
        warnings.append(f'Liquidation period ({liquidation_days} days) exceeds simulation horizon ({horizon_days} days).')
        
    explanations = [
        f'Holding cost assumes {number(holding_cost_daily_pct)}% daily on the full cost basis of the locked inventory.',
        f'Liquidation assumes a linear sale of stock over {liquidation_days} days with a {number(discount)}% discount on full price.'
    ]
    
    return {
        'candidates': enriched_candidates,
        'series': series,
        'metrics': metrics,
        'events': events,
        'warnings': warnings,
        'explanations': explanations
    }
