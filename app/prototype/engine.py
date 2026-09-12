from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Any

from .inventory_policy import policy_order, validate_policy
from .forecasting import ADVANCED_ENGINES, build_forecast, validate_advanced
from .scenario_finance import apply_scenario_finance, selected_terms, validate_financial_assumptions
from .schema import CATEGORIES, InputError, dates, day, numeric, validate_workspace


VERSION = 'samby-daily-v2'
ASSETS = {
    'Q-NEW-ORDER': ['customer_order', 'warehouse', 'sku_stack', 'supplier_node', 'delivery_truck', 'cash_account', 'calendar_marker'],
    'Q-REPLENISH': ['warehouse', 'sku_stack', 'supplier_node', 'purchase_order', 'delivery_truck', 'stockout_marker', 'calendar_marker'],
    'Q-CRITICAL-COLLECTION': ['customer_node', 'invoice', 'receivable_balance', 'cash_account', 'cash_inflow', 'cash_outflow', 'calendar_marker'],
    'Q-CASH-SUFFICIENCY': ['cash_account', 'payroll_group', 'supplier_payable', 'rent_location', 'cash_inflow', 'cash_outflow', 'calendar_marker'],
    'Q-DEMAND-CHANGE': ['customer_demand', 'calendar_season', 'warehouse', 'sku_stack', 'purchase_order', 'stockout_marker', 'calendar_marker'],
    'Q-SLOW-SUPPLIER': ['supplier_node', 'delivery_truck', 'delay_marker', 'warehouse', 'sku_stack', 'stockout_marker', 'calendar_marker'],
    'Q-CUSTOMER-DEBT': ['customer_node', 'invoice', 'receivable_balance', 'cash_account', 'cash_inflow', 'calendar_marker'],
    'Q-SUPPLIER-ORDER-STOCKOUT': ['supplier_node', 'purchase_order', 'delivery_truck', 'warehouse', 'sku_stack', 'stockout_marker', 'calendar_marker'],
    'Q-EXPLORE': [],
}
ASSET_MEANINGS = {
    'warehouse': 'Known inventory location or declared aggregate scope.',
    'sku_stack': 'Saved available stock for an identifiable product and unit.',
    'supplier_node': 'A supplied supplier reference, without an inferred reliability score.',
    'delivery_truck': 'A dated receipt event under the saved timing assumptions.',
    'purchase_order': 'A recorded or explicitly proposed supplier order.',
    'customer_order': 'An explicitly declared customer order and requested quantity.',
    'customer_node': 'A supplied customer reference for the represented balances.',
    'customer_demand': 'Demand from the pinned forecast or accepted daily assumption.',
    'invoice': 'A supplied invoice reference, distinct from its collection.',
    'receivable_balance': 'Unpaid customer amount, separate from provider-pending funds.',
    'cash_account': 'Saved opening and closing cash for included financial events.',
    'cash_inflow': 'A dated expected collection or provider-availability event.',
    'cash_outflow': 'A dated expected payment included in the saved scenario.',
    'supplier_payable': 'A confirmed supplier obligation, distinct from expected commitments.',
    'payroll_group': 'A supplied dated payroll obligation.',
    'rent_location': 'A supplied dated rent obligation, without inferred premises.',
    'calendar_marker': 'A dated event from the persisted result trace.',
    'calendar_season': 'The owner-selected interval for a demand-change assumption.',
    'delay_marker': 'A modeled date shift, without a claim about historical reliability.',
    'stockout_marker': 'A saved zero-stock or unmet-demand observation under the daily convention.',
}


def dec(value: Any, label: str, minimum: float | None = None) -> Decimal:
    numeric(value, label, minimum)
    return Decimal(str(value))


def number(value: Decimal | float | int) -> float:
    return round(float(value), 6)


def metric(key: str, label: str, value: Any, unit: str) -> dict:
    return {'key': key, 'label': label, 'value': None if value is None else number(value), 'unit': unit}


def get_product(config: dict, workspace: dict) -> dict:
    product = next((p for p in workspace['products'] if p['id'] == config.get('product_id')), None)
    if product is None:
        raise InputError('Select one identifiable product for unit demand or inventory analysis.')
    if not product.get('unit'):
        raise InputError('Confirm the selected product unit before calculating quantities.')
    if config.get('location_id') and not any(l['id'] == config['location_id'] for l in workspace['locations']):
        raise InputError('The selected location does not exist in the submitted workspace.')
    scope(config, workspace)
    return product


def scope(config: dict, workspace: dict) -> dict:
    result = {'product_id': config.get('product_id'), 'location_id': config.get('location_id')}
    if config.get('inventory_pool_id'):
        if config.get('location_id'):
            raise InputError('Select either a location or an inventory pool.')
        pool = next((p for p in workspace.get('inventoryPools', []) if p.get('id') == config['inventory_pool_id']), None)
        if pool is None or not pool.get('locationIds'):
            raise InputError('Select a confirmed inventory pool with known contributing locations.')
        valid_ids = {location['id'] for location in workspace['locations']}
        if not isinstance(pool['locationIds'], list) or not all(isinstance(location, str) and location in valid_ids for location in pool['locationIds']):
            raise InputError('Every confirmed pool contribution must identify a known location.')
        if len(set(pool['locationIds'])) != len(pool['locationIds']):
            raise InputError('An inventory pool cannot count the same location twice.')
        result.update({'inventory_pool_id': pool['id'], 'location_ids': sorted(pool['locationIds'])})
    return result


def matches_location(record: dict, config: dict, workspace: dict) -> bool:
    if config.get('location_id'):
        return record.get('locationId') == config['location_id']
    if config.get('inventory_pool_id'):
        return record.get('locationId') in scope(config, workspace)['location_ids']
    return True


def forecast_history(config: dict, workspace: dict) -> tuple[dict[str, float], list[str]]:
    product = get_product(config, workspace)
    observations: dict[str, Decimal] = defaultdict(Decimal)
    warnings = ['Observed sales may be censored by stockouts and do not establish unconstrained demand.']
    for sale in workspace['sales']:
        if sale.get('productId') != product['id']:
            continue
        if not matches_location(sale, config, workspace):
            continue
        sale_date = day(sale.get('date'), 'Sale date').isoformat()
        if sale_date >= config['start_date']:
            continue
        if sale.get('kind') == 'return':
            warnings.append('Returns are excluded from the unit-demand baseline and retain their original meaning.')
            continue
        if sale.get('quantity') is None:
            continue
        if sale.get('unit') != product['unit']:
            raise InputError('Sales units do not match the product. Confirm conversions in import review first.')
        observations[sale_date] += dec(sale['quantity'], 'Sold quantity', 0)
    if not observations:
        raise InputError('Provide at least one comparable prior dated product quantity before the forecast start.')
    if len(observations) < 28:
        warnings.append(f'Short history. Only {len(observations)} observed daily periods support this baseline.')
    return {d: number(value) for d, value in observations.items()}, warnings


def forecast_values(config: dict, workspace: dict) -> tuple[list[float], list[str]]:
    observations, warnings = forecast_history(config, workspace)
    if config['engine'] in ADVANCED_ENGINES:
        validate_advanced(observations, config['start_date'])
        warnings.append('This engine trains a real local model on supplied demand lags and calendar drivers in the asynchronous worker. Held-out evaluation is separate from final fitting.')
        return [], warnings
    if config['engine'] == 'naive':
        last = max(observations)
        warnings.append(f'Naive baseline repeats the last observed daily total from {last}. Missing days were not filled with zero.')
        return [observations[last]] * config['horizon_days'], warnings
    lag = int(config.get('assumptions', {}).get('season_length_days') or 7)
    cycle_dates = [(day(config['start_date']) - timedelta(days=lag - i)).isoformat() for i in range(lag)]
    missing = [d for d in cycle_dates if d not in observations]
    if missing:
        raise InputError(f'Seasonal naive needs every comparable daily observation in its {lag}-day prior cycle. Missing {missing[0]}.')
    warnings.append(f'Seasonal naive repeats the supplied {lag}-day cycle. No missing seasonal observations are manufactured.')
    return [observations[cycle_dates[i % lag]] for i in range(config['horizon_days'])], warnings


def finance_inputs(config: dict, workspace: dict, families: set[str]) -> dict:
    assumptions = config['assumptions']
    start, end = dates(config)[0], dates(config)[-1]
    currency = workspace['profile']['currency']
    warnings: list[str] = []
    events: list[dict] = []
    pending_finance = workspace.get('pendingFinance') or []
    relevant_pending = [record for record in pending_finance if 'cash' in families or record.get('kind') == 'receivable']
    if relevant_pending:
        names = ', '.join(str(record.get('name') or record.get('id') or 'Unidentified record') for record in relevant_pending)
        warnings.append(f'Partial financial scope: {len(relevant_pending)} pending financial records have unresolved monetary allocations and are excluded from numerical balances: {names}. Supplied category coverage does not make these unknown amounts zero or establish complete cash sufficiency.')
        for record in relevant_pending:
            category = 'collections' if record.get('kind') == 'receivable' else 'suppliers'
            if (workspace.get('coverage') or {}).get(category, {}).get('state') == 'absent':
                raise InputError(f'{category} cannot be confirmed absent while identifiable pending financial records remain unresolved.')
    records = {r['id']: r for r in workspace['finance']}
    purchases = {r['id']: r for r in workspace['purchases']}
    if config.get('product_id') or config.get('location_id') or config.get('inventory_pool_id'):
        warnings.append('Financial results use the supplied business-wide financial records. The selected product/location filters apply only to inventory and demand because financial records have no location allocation.')
    selected_collection = assumptions.get('collection_id')
    delay = int(assumptions.get('collection_delay_days') or 0)
    question = config['question']
    change_all_collections = not selected_collection and assumptions.get('collection_delay_days') is not None and question in {'Q-CUSTOMER-DEBT', 'Q-CASH-SUFFICIENCY', 'Q-EXPLORE'}
    if question == 'Q-CRITICAL-COLLECTION' and (not selected_collection or assumptions.get('collection_delay_days') is None):
        raise InputError('Select a collection and enter its changed collection delay in calendar days.')
    if question == 'Q-CUSTOMER-DEBT' and assumptions.get('collection_delay_days') is None:
        raise InputError('Enter the explicit customer collection-delay assumption.')
    if selected_collection and selected_collection not in records:
        raise InputError('The selected collection is missing from the submitted snapshot.')
    linked_pending: dict[str, Decimal] = defaultdict(Decimal)
    linked_payables: dict[str, Decimal] = defaultdict(Decimal)
    linked_paid: dict[str, Decimal] = defaultdict(Decimal)
    for record in records.values():
        if record.get('kind') == 'provider_pending' and record.get('linkedRecordId'):
            invoice = records.get(record['linkedRecordId'])
            if invoice is None or invoice.get('kind') != 'receivable':
                raise InputError('Provider-pending funds must link to their customer invoice.')
            linked_pending[invoice['id']] += dec(record.get('amount'), 'Provider pending amount', 0)
        if 'cash' in families and record.get('kind') == 'payable' and record.get('linkedRecordId'):
            purchase = purchases.get(record['linkedRecordId'])
            if purchase is None:
                raise InputError('Supplier payables must link to an existing purchase when a purchase link is provided.')
            linked_payables[purchase['id']] += dec(record.get('amount'), 'Linked payable amount', 0)
            linked_paid[purchase['id']] += dec(record.get('paidAmount'), 'Linked payable paid amount', 0)
    for invoice_id, pending in linked_pending.items():
        if pending > dec(records[invoice_id].get('paidAmount'), 'Invoice collected amount', 0):
            raise InputError(f'Pending-provider links exceed collected funds for {invoice_id}. Reconcile the payment stages first.')
    for purchase_id, payable in linked_payables.items():
        if purchases[purchase_id].get('amount') is None:
            raise InputError(f'Purchase {purchase_id} has an unknown amount. Confirm its total before reconciling linked payables for cash.')
        purchase_amount = dec(purchases[purchase_id]['amount'], 'Linked purchase amount', 0)
        if payable > purchase_amount:
            raise InputError(f'Linked supplier payables exceed purchase {purchase_id}. Reconcile duplicate or partial invoice allocations before summing payments.')
        if payable < purchase_amount:
            raise InputError(f'Linked payables cover only part of purchase {purchase_id}. The remaining purchase needs explicit amount/payment allocation before a cash projection can avoid double counting.')
        if linked_paid[purchase_id] != dec(purchases[purchase_id].get('paidAmount'), 'Linked purchase paid amount', 0):
            raise InputError(f'Paid allocations for purchase {purchase_id} and its linked payables disagree. Reconcile advances and partial payments first.')
    opening_receivable = Decimal(0)
    opening_provider = Decimal(0)
    opening_payable = Decimal(0)
    opening_financing = Decimal(0)
    customer_balances: dict[str, Decimal] = defaultdict(Decimal)
    selected_found = False
    for record in records.values():
        kind = record.get('kind')
        incoming = kind in {'receivable', 'provider_pending'}
        if 'cash' not in families and not incoming:
            continue
        if kind not in {'receivable', 'provider_pending', 'payable', 'operating', 'financing'}:
            raise InputError(f'Unsupported financial kind on {record["id"]}.')
        if record.get('currency') != currency:
            raise InputError('Financial records must use the working currency. No currency conversion is assumed.')
        amount = dec(record.get('amount'), 'Financial amount', 0)
        paid = dec(record.get('paidAmount'), 'Paid amount', 0)
        if paid > amount:
            raise InputError(f'Paid amount exceeds the source amount on {record["id"]}. Reconcile the record first.')
        outstanding = amount - paid
        if incoming and record.get('cashIncluded'):
            warnings.append(f'{record.get("name", record["id"])} is already included in opening cash and is excluded from future collections and pending balances.')
            continue
        if kind == 'receivable':
            opening_receivable += outstanding
        if kind == 'payable':
            opening_payable += outstanding
        if kind == 'financing':
            opening_financing += outstanding
        if incoming:
            customer = record.get('counterparty') or 'Unknown counterparty'
            customer_balances[customer] += outstanding
            if customer == 'Unknown counterparty':
                warnings.append('Customer concentration includes an unassigned counterparty bucket. The supplied population is incomplete.')
        if kind == 'provider_pending':
            opening_provider += outstanding
            if not record.get('linkedRecordId'):
                candidates = [r for r in records.values() if r.get('kind') == 'receivable' and r.get('counterparty') == record.get('counterparty') and r.get('paidAmount') == record.get('amount')]
                if candidates:
                    raise InputError('A provider-pending amount may duplicate a collected invoice. Confirm its invoice link before summing it.')
                warnings.append(f'{record.get("name", record["id"])} has no invoice link. Only the separately declared pending amount is included.')
        if not outstanding:
            continue
        if record.get('cashIncluded'):
            warnings.append(f'{record.get("name", record["id"])} is already reflected in opening cash and is not paid twice.')
            continue
        event_date = record.get('expectedDate')
        if not incoming and assumptions.get('payment_id') == record['id']:
            event_date = assumptions['payment_date']
        if event_date:
            day(event_date, 'Expected financial event date')
        applies_delay = incoming and ((selected_collection == record['id']) or (change_all_collections and event_date is not None))
        if applies_delay:
            if not event_date:
                raise InputError('The changed collection requires an expected date. Its due date is not a collection guarantee.')
            event_date = (day(event_date) + timedelta(days=delay)).isoformat()
            if event_date < start:
                raise InputError('A changed collection before the simulation start needs a reviewed opening-cash bridge. Choose an in-window date.')
            selected_found = True
        if not event_date:
            warnings.append(f'Unscheduled {record.get("name", record["id"])}: {number(outstanding):g} {currency} remains outside the timed projection.')
            continue
        if event_date < start:
            warnings.append(f'{record.get("name", record["id"])} has a past expected date and remains outstanding pending rescheduling.')
            continue
        if event_date > end:
            warnings.append(f'{record.get("name", record["id"])} remains outstanding beyond {end}, expected {event_date}.')
        category = record.get('category')
        if category not in CATEGORIES:
            raise InputError('Every financial event needs a recognized coverage category.')
        event_type = 'collection' if kind == 'receivable' else 'provider_availability' if kind == 'provider_pending' else f'{category}_payment'
        events.append({'id': f'finance-{record["id"]}', 'date': event_date, 'type': event_type, 'label': record.get('name') or record['id'], 'amount': number(outstanding), 'source_id': record['id'], '_direction': 'in' if incoming else 'out', '_category': category, '_receivable': number(outstanding) if kind == 'receivable' else 0, '_provider': number(outstanding) if kind == 'provider_pending' else 0, '_payable': number(outstanding) if kind == 'payable' else 0, '_financing': number(outstanding) if kind == 'financing' else 0, '_customer': (record.get('counterparty') or 'Unknown counterparty') if incoming else None})
    if selected_collection and not selected_found:
        raise InputError('The selected collection must be an outstanding dated receivable or provider-pending amount.')
    if assumptions.get('customer_terms_id') and not any(record.get('kind') in {'receivable', 'provider_pending'} for record in records.values()):
        warnings.append('No current customer receivable register was supplied. The debt trajectory covers only the explicitly modeled new sales; it does not establish the total business receivable balance.')
    if 'debt' in families and not assumptions.get('customer_terms_id') and not any(r.get('kind') in {'receivable', 'provider_pending'} for r in records.values()):
        raise InputError('Provide an identifiable customer receivable or provider-pending amount for debt results.')
    if 'cash' in families:
        linked_purchases = {r.get('linkedRecordId') for r in records.values() if r.get('kind') == 'payable'}
        for purchase in purchases.values():
            if purchase['id'] in linked_purchases:
                continue
            amount = purchase.get('amount')
            if amount is None:
                warnings.append(f'Purchase {purchase["id"]} has unknown cost and contributes no invented cash amount.')
                continue
            outstanding = dec(amount, 'Purchase amount', 0) - dec(purchase.get('paidAmount'), 'Purchase paid amount', 0)
            if outstanding < 0:
                raise InputError('Purchase paid amount exceeds its amount.')
            payment_date = assumptions['payment_date'] if assumptions.get('payment_id') == purchase['id'] else purchase.get('plannedPaymentDate')
            if outstanding and not payment_date:
                warnings.append(f'Purchase {purchase["id"]} has no planned payment date and is omitted from timed cash.')
            if outstanding and payment_date:
                day(payment_date, 'Purchase payment date')
                if payment_date < start:
                    warnings.append(f'Purchase {purchase["id"]} needs its past planned payment rescheduled.')
                    continue
                supplier = next((s for s in workspace['suppliers'] if s['id'] == purchase.get('supplierId')), {})
                duplicate = any(r.get('kind') == 'payable' and not r.get('linkedRecordId') and r.get('counterparty') == supplier.get('name') and r.get('expectedDate') == payment_date and r.get('amount') == amount for r in records.values())
                if duplicate:
                    raise InputError('A purchase and supplier payable may describe one payment. Confirm their link before summing them.')
                events.append({'id': f'purchase-payment-{purchase["id"]}', 'date': payment_date, 'type': 'suppliers_payment', 'label': f'Purchase payment {purchase["id"]}', 'amount': number(outstanding), 'source_id': purchase['id'], '_direction': 'out', '_category': 'suppliers'})
        for commitment in workspace['commitments']:
            if commitment.get('linkedPayableId'):
                if commitment['linkedPayableId'] not in records or records[commitment['linkedPayableId']].get('kind') != 'payable':
                    raise InputError('A recurring commitment links to a missing payable. Restore or reconcile that link.')
                warnings.append(f'{commitment.get("name", commitment["id"])} uses its linked payable once; no extra recurring payment is added.')
                continue
            if commitment.get('payment') == 'paid':
                continue
            if commitment.get('currency') != currency:
                raise InputError('Recurring commitments must use the projection currency.')
            if commitment.get('amount') is None or not commitment.get('nextDate'):
                warnings.append(f'{commitment.get("name", commitment["id"])} has unknown amount or timing and is omitted from timed cash.')
                continue
            amount = dec(commitment['amount'], 'Commitment amount', 0)
            event_date = day(commitment['nextDate'], 'Next commitment date').isoformat()
            warnings.append(f'{commitment.get("name", commitment["id"])} includes only its supplied next occurrence. Further recurring periods are not inferred.')
            if event_date >= start:
                events.append({'id': f'commitment-{commitment["id"]}', 'date': event_date, 'type': 'suppliers_payment', 'label': commitment.get('name') or commitment['id'], 'amount': number(amount), 'source_id': commitment['id'], '_direction': 'out', '_category': 'suppliers'})
        if assumptions.get('payment_id'):
            changed = next((event for event in events if event.get('source_id') == assumptions['payment_id'] and event['_direction'] == 'out'), None)
            if not changed:
                raise InputError('The selected payment has no outstanding outgoing amount. Select its linked payable when a purchase is already invoiced.')
            warnings.append(f'Outgoing payment {assumptions["payment_id"]} moves to {assumptions["payment_date"]} as an accepted hypothetical schedule. The source obligation and commercial agreement are unchanged.')
        if not config.get('coverage_reviewed'):
            raise InputError('Review included and omitted financial categories before requesting a cash projection.')
        coverage = workspace.get('coverage') or {}
        if not isinstance(coverage, dict):
            raise InputError('Review financial coverage as category records before requesting cash results.')
        for category in CATEGORIES:
            state = coverage.get(category, {})
            if not isinstance(state, dict):
                raise InputError(f'Review the {category} coverage state and its period.')
            if state.get('startDate'):
                day(state['startDate'], f'{category} coverage start')
            if state.get('endDate'):
                day(state['endDate'], f'{category} coverage end')
            covered = state.get('startDate', '') <= start and state.get('endDate', '') >= end
            if state.get('state') == 'absent' and any(e.get('_category') == category and start <= e['date'] <= end for e in events):
                raise InputError(f'{category} is marked absent but has a timed event. Reconcile the coverage review.')
            if state.get('state') not in {'supplied', 'absent'} or not covered:
                warnings.append(f'Partial projection. {category} is omitted, unknown or not reviewed for the full window.')
        cash = workspace.get('cash')
        if assumptions.get('cash_opening_estimate') is not None:
            opening_cash = dec(assumptions['cash_opening_estimate'], 'Accepted opening cash estimate')
            warnings.append(f'Opening cash uses the owner-accepted estimate of {number(opening_cash):g} {currency} at {start}.')
        elif cash:
            cash_date = day(cash.get('date'), 'Cash snapshot date')
            boundary_valid = (cash_date.isoformat() == start and cash.get('phase') == 'opening') or (cash_date + timedelta(days=1) == day(start) and cash.get('phase') == 'end-of-day')
            if not boundary_valid:
                raise InputError('The cash snapshot does not establish opening cash on the start date. Supply an explicit opening estimate or align the window.')
            opening_cash = dec(cash.get('amount'), 'Opening cash')
        else:
            raise InputError('Absolute cash results need dated opening cash. Debt and collection-timing results can run separately.')
        derived_flows = (assumptions.get('supplier_terms_id') and assumptions.get('purchase_cash_treatment') in {'incremental', 'replace_linked'}) or (assumptions.get('customer_terms_id') and assumptions.get('demand_cash_treatment') == 'incremental')
        if not derived_flows and not any(start <= e['date'] <= end for e in events):
            raise InputError('A cash projection needs at least one usable timed inflow or outflow within the selected window.')
        warnings.append('Daily cash uses net receipts and payments. Same-day clearing order and unrecorded obligations are not known.')
    else:
        opening_cash = None
    return {'events': events, 'warnings': warnings, 'opening_cash': opening_cash, 'receivable': opening_receivable, 'provider': opening_provider, 'payable': opening_payable, 'financing': opening_financing, 'customers': dict(customer_balances)}


def inventory_inputs(config: dict, workspace: dict, dependency: dict | None, pending: bool) -> dict:
    product = get_product(config, workspace)
    assumptions = config['assumptions']
    validate_policy(config, product)
    if assumptions.get('order_policy') == 'reorder':
        check_constraints(product, dec(assumptions['order_quantity'], 'Fixed reorder quantity', 0))
    timeline = dates(config)
    start, end = timeline[0], timeline[-1]
    positions = [s for s in workspace['stock'] if s.get('productId') == product['id'] and matches_location(s, config, workspace)]
    if not positions:
        raise InputError('Provide starting stock for the selected product and location or declared aggregate scope.')
    if any(s.get('locationId') is None for s in positions) and any(s.get('locationId') is not None for s in positions):
        raise InputError('Aggregate and location stock overlap. Reconcile the shared pool before calculating availability.')
    if not assumptions.get('stock_opening_confirmed'):
        raise InputError('Confirm the stock quantities represent the selected start boundary. Dated snapshots do not prove opening stock.')
    warnings = ['Inventory applies receipts, then previously backordered demand, then new requested demand. This is a versioned daily convention, not observed intraday ordering.']
    opening_backlog = Decimal(0)
    reserved_backlog = Decimal(0)
    retained_reserved = Decimal(0)
    all_on_hand_known = True
    backlog_complete = all(position.get('backordered') is not None for position in positions)
    if not backlog_complete:
        warnings.append('Backorder observations are incomplete. Modeled backlog covers only supplied quantities; a complete inventory position is unavailable.')
    stock = Decimal(0)
    for position in positions:
        as_of = day(position.get('asOf'), 'Stock snapshot date').isoformat()
        on_hand = dec(position.get('onHand'), 'Stock quantity')
        if position.get('quantityBasis', 'on-hand') not in {'on-hand', 'available'}:
            raise InputError('Confirm whether the stock quantity is on-hand or already available.')
        reserved = Decimal(0)
        if position.get('quantityBasis', 'on-hand') == 'available':
            available = on_hand
            all_on_hand_known = False
        else:
            reserved = dec(position.get('reserved'), 'Reserved stock', 0)
            available = on_hand - reserved
        if available < 0:
            raise InputError('Negative available stock is a source exception. Reconcile it before an absolute inventory simulation.')
        if position.get('backordered') is not None:
            backordered = dec(position['backordered'], 'Backordered stock', 0)
            opening_backlog += backordered
            if backordered > 0:
                if not assumptions.get('opening_backlog_confirmed') or assumptions.get('backlog_reservation_overlap') not in {'included', 'additional'}:
                    raise InputError('Review opening backorders and declare whether reservations are included in that backlog or additional before running inventory.')
                if assumptions['backlog_reservation_overlap'] == 'included':
                    if position.get('quantityBasis') == 'available':
                        raise InputError('Backlog reservations need on-hand and reserved quantities; an available-only snapshot cannot release reserved stock twice.')
                    reserved_backlog += min(reserved, backordered)
        stock += available
        retained_reserved += reserved
        if as_of != start:
            warnings.append(f'Stock dated {as_of} is explicitly accepted as the estimated opening quantity on {start}; no missing movements are invented.')
    if any(s.get('locationId') is None for s in positions):
        warnings.append('Stock scope is aggregate with unknown physical distribution. Local fulfillment is not established.')
    if dependency:
        source_config = dependency['config']
        if source_config.get('product_id') != config.get('product_id') or source_config.get('location_id') != config.get('location_id') or source_config.get('inventory_pool_id') != config.get('inventory_pool_id'):
            raise InputError('The pinned forecast product and location must match the scenario.')
        if dependency.get('snapshot') and scope(config, workspace) != scope(source_config, dependency['snapshot']):
            raise InputError('The pinned forecast pool membership differs from the submitted scenario scope.')
        by_date = {p['date']: p['demand'] for p in dependency['result']['series']}
        if any(d not in by_date for d in timeline):
            raise InputError('The pinned forecast does not cover every requested daily date. No extrapolation or allocation is assumed.')
        demand = {d: dec(by_date[d], 'Forecast demand', 0) for d in timeline}
        warnings.extend(dependency.get('warnings', []))
    elif config.get('forecast_run_id') and pending:
        demand = None
    elif assumptions.get('daily_demand') is not None:
        quantity = dec(assumptions['daily_demand'], 'Daily demand', 0)
        demand = dict.fromkeys(timeline, quantity)
        warnings.append(f'Uniform daily demand of {number(quantity):g} {product["unit"]} is an explicit scenario assumption.')
    elif config['question'] == 'Q-NEW-ORDER':
        demand = dict.fromkeys(timeline, Decimal(0))
        warnings.append('Standalone declared-order analysis includes no other demand. It is not a sales forecast.')
    else:
        raise InputError('Provide an explicit daily demand assumption or a pinned forecast covering every date.')
    events: list[dict] = []
    question = config['question']
    selected_purchase = assumptions.get('purchase_id')
    open_purchases = [p for p in workspace['purchases'] if p.get('productId') == product['id'] and numeric(p.get('receivedQuantity'), 'Received quantity', 0) < numeric(p.get('quantity'), 'Purchase quantity', 0)]
    if config.get('location_id') or config.get('inventory_pool_id'):
        if any(not purchase.get('locationId') for purchase in open_purchases):
            raise InputError('An open purchase has no destination location. Assign its destination or use aggregate scope; restricted scope cannot assume where it arrives.')
        open_purchases = [purchase for purchase in open_purchases if matches_location(purchase, config, workspace)]
    if question in {'Q-SLOW-SUPPLIER', 'Q-SUPPLIER-ORDER-STOCKOUT'} and not any(p['id'] == selected_purchase for p in open_purchases):
        raise InputError('Select an open purchase for this supplier-order scenario.')
    for purchase in open_purchases:
        quantity = dec(purchase['quantity'], 'Purchase quantity', 0) - dec(purchase['receivedQuantity'], 'Already received quantity', 0)
        receipt_date = purchase.get('promisedDate')
        changed = purchase['id'] == selected_purchase
        if changed and question == 'Q-SLOW-SUPPLIER':
            if assumptions.get('lead_time_days') is None:
                raise InputError('Provide the changed supplier lead time in calendar days from its order date.')
            receipt_date = (day(purchase.get('orderDate'), 'Supplier order date') + timedelta(days=int(assumptions['lead_time_days']))).isoformat()
            warnings.append('Changed supplier lead time is counted from the recorded order date. It does not describe historical supplier reliability.')
        if changed and question == 'Q-SUPPLIER-ORDER-STOCKOUT':
            if not any(assumptions.get(key) is not None for key in ('order_quantity', 'receipt_date', 'order_date', 'lead_time_days')):
                raise InputError('Change the supplier order quantity or its receipt date.')
            if assumptions.get('order_quantity') is not None:
                quantity = dec(assumptions['order_quantity'], 'Changed total purchase quantity', 0) - dec(purchase['receivedQuantity'], 'Already received quantity', 0)
                if quantity < 0:
                    raise InputError('Changed total order quantity cannot be below its already received amount.')
            changed_order_date = assumptions.get('order_date') or purchase.get('orderDate')
            if assumptions.get('receipt_date'):
                receipt_date = assumptions['receipt_date']
            elif assumptions.get('lead_time_days') is not None:
                receipt_date = (day(changed_order_date, 'Changed supplier order date') + timedelta(days=int(assumptions['lead_time_days']))).isoformat()
            elif assumptions.get('order_date'):
                if not purchase.get('promisedDate') or not purchase.get('orderDate'):
                    raise InputError('Changing the order date requires a supplied receipt date or explicit lead time.')
                original_lead = day(purchase['promisedDate']) - day(purchase['orderDate'])
                if original_lead.days < 0:
                    raise InputError('The original receipt precedes its order date. Correct the source dates.')
                receipt_date = (day(changed_order_date) + original_lead).isoformat()
                warnings.append('The changed order date retains the original recorded order-to-promised-receipt interval as a scenario assumption.')
            if changed_order_date and receipt_date and changed_order_date > receipt_date:
                raise InputError('The changed supplier receipt cannot precede its order commitment date.')
            if assumptions.get('order_date'):
                events.append({'id': f'order-{purchase["id"]}', 'date': assumptions['order_date'], 'type': 'purchase_order', 'label': f'Changed supplier order date {purchase["id"]}', 'quantity': number(quantity), 'source_id': purchase['id']})
        if not receipt_date:
            warnings.append(f'Purchase {purchase["id"]} is unscheduled and supplies no invented receipt.')
            continue
        day(receipt_date, 'Purchase receipt date')
        if receipt_date < start:
            if changed:
                raise InputError('The changed receipt falls before the opening stock boundary. Align the window or review a new stock estimate.')
            warnings.append(f'Overdue purchase {purchase["id"]} is excluded from future supply until its receipt is rescheduled.')
            continue
        if changed:
            check_constraints(product, quantity + dec(purchase['receivedQuantity'], 'Received quantity', 0))
        events.append({'id': f'receipt-{purchase["id"]}', 'date': receipt_date, 'type': 'receipt', 'label': f'Supplier receipt {purchase["id"]}', 'quantity': number(quantity), 'source_id': purchase['id']})
    if assumptions.get('order_policy') != 'reorder' and (question == 'Q-REPLENISH' or (question == 'Q-EXPLORE' and assumptions.get('order_quantity') is not None)):
        quantity = dec(assumptions.get('order_quantity'), 'Planned replenishment quantity', 0)
        if quantity <= 0:
            raise InputError('Enter a positive planned replenishment quantity. The engine does not choose a purchasing policy.')
        check_constraints(product, quantity)
        receipt_date = assumptions.get('receipt_date')
        order_date = assumptions.get('order_date')
        if not receipt_date:
            if not order_date or assumptions.get('lead_time_days') is None:
                raise InputError('Provide a replenishment receipt date or an order date and lead time.')
            receipt_date = (day(order_date) + timedelta(days=int(assumptions['lead_time_days']))).isoformat()
        if receipt_date < start:
            raise InputError('Planned replenishment must arrive at or after the opening boundary.')
        events.append({'id': 'assumed-replenishment', 'date': receipt_date, 'type': 'receipt', 'label': 'Assumed replenishment receipt', 'quantity': number(quantity), 'source_id': product['id']})
        warnings.append('Planned purchase timing and quantity are owner-selected assumptions. No automatic reorder policy was selected.')
        if order_date:
            day(order_date, 'Planned purchase order date')
            if order_date > receipt_date:
                raise InputError('A proposed purchase cannot arrive before its order date.')
            events.append({'id': 'assumed-purchase-order', 'date': order_date, 'type': 'purchase_order', 'label': 'Assumed supplier order commitment', 'quantity': number(quantity), 'source_id': product['id']})
    if question == 'Q-NEW-ORDER':
        quantity = dec(assumptions.get('order_quantity'), 'Declared order quantity', 0)
        if quantity <= 0:
            raise InputError('Provide a positive declared customer-order quantity.')
        order_date = day(assumptions.get('order_date'), 'Requested fulfillment date').isoformat()
        if not start <= order_date <= end:
            raise InputError('The declared order date must be inside the selected simulation window.')
        has_base = config.get('forecast_run_id') or assumptions.get('daily_demand') is not None
        if has_base and not assumptions.get('forecast_overlap'):
            raise InputError('Choose replacement or explicitly incremental demand before combining an order with forecast/daily demand.')
        if demand is not None:
            if assumptions.get('forecast_overlap') == 'incremental':
                demand[order_date] += quantity
            else:
                demand[order_date] = quantity
        events.append({'id': 'declared-order', 'date': order_date, 'type': 'customer_order', 'label': 'Declared customer order', 'quantity': number(quantity), 'source_id': product['id']})
        warnings.append(f'Declared order demand treatment is {assumptions.get("forecast_overlap") or "standalone"}. The source order is unchanged.')
    if question == 'Q-DEMAND-CHANGE' or assumptions.get('demand_multiplier') is not None:
        multiplier = dec(assumptions.get('demand_multiplier'), 'Demand multiplier', 0)
        change_start = day(assumptions.get('demand_start_date'), 'Demand-change start').isoformat()
        change_end = day(assumptions.get('demand_end_date'), 'Demand-change end').isoformat()
        if not start <= change_start <= change_end <= end:
            raise InputError('The demand-change interval must be ordered and inside the simulation window.')
        if demand is not None:
            demand = {d: q * multiplier if change_start <= d <= change_end else q for d, q in demand.items()}
        warnings.append(f'Demand multiplier {number(multiplier):g} applies from {change_start} through {change_end}. It is an assumption, not a calibrated causal forecast.')
    return {'product': product, 'opening_stock': stock, 'opening_backlog': opening_backlog, 'reserved_backlog': reserved_backlog, 'retained_reserved': retained_reserved, 'on_hand_known': all_on_hand_known, 'backlog_complete': backlog_complete, 'backlog_policy': assumptions.get('backlog_policy', 'lost_sales'), 'demand': demand, 'events': events, 'warnings': warnings}


def check_constraints(product: dict, quantity: Decimal) -> None:
    if product.get('moq') is not None and quantity < dec(product['moq'], 'Minimum order quantity', 0):
        raise InputError('The proposed order quantity is below the supplied minimum order quantity.')
    if product.get('casePack') is not None:
        pack = dec(product['casePack'], 'Case pack', 0)
        if pack <= 0:
            raise InputError('Case pack must be positive when supplied.')
        if quantity % pack:
            raise InputError('The proposed order quantity must be a whole multiple of its confirmed case pack.')


def purchasing_budget(config: dict, workspace: dict) -> dict | None:
    budget = workspace.get('budget')
    if budget is None:
        return None
    amount = dec(budget.get('amount'), 'Purchasing budget', 0)
    start = day(budget.get('startDate'), 'Budget start date').isoformat()
    end = day(budget.get('endDate'), 'Budget end date').isoformat()
    if start > end:
        raise InputError('The purchasing budget start must be at or before its end date.')
    known_spend = Decimal(0)
    unknown = []
    included = []
    warnings = []
    assumptions = config['assumptions']
    selected = assumptions.get('purchase_id')
    currency = workspace['profile']['currency']
    for purchase in workspace['purchases']:
        order_date = assumptions.get('order_date') if purchase['id'] == selected and config['question'] == 'Q-SUPPLIER-ORDER-STOCKOUT' and assumptions.get('order_date') else purchase.get('orderDate')
        if not order_date:
            unknown.append(purchase['id'])
            warnings.append(f'Purchase {purchase["id"]} has no order date; its budget-period eligibility is unknown.')
            continue
        day(order_date, 'Purchase order commitment date')
        if not start <= order_date <= end:
            continue
        purchase_amount = purchase.get('amount')
        if purchase['id'] == selected and config['question'] == 'Q-SUPPLIER-ORDER-STOCKOUT' and assumptions.get('order_quantity') is not None:
            quantity = dec(assumptions['order_quantity'], 'Changed purchase quantity', 0)
            unit_cost = assumptions.get('unit_cost')
            if unit_cost is not None:
                purchase_amount = number(quantity * dec(unit_cost, 'Assumed purchase unit cost', 0))
            elif purchase_amount is not None and numeric(purchase.get('quantity'), 'Original purchase quantity', 0) > 0:
                unit_cost = dec(purchase_amount, 'Recorded purchase amount', 0) / dec(purchase['quantity'], 'Recorded purchase quantity', 0)
                purchase_amount = number(quantity * unit_cost)
                warnings.append(f'Budget comparison assumes purchase {purchase["id"]} retains its recorded unit cost of {number(unit_cost):g} {currency} when quantity changes.')
            else:
                purchase_amount = None
        if purchase_amount is None:
            unknown.append(purchase['id'])
            warnings.append(f'Purchase {purchase["id"]} has unknown compatible cost and is not treated as zero budget spend.')
            continue
        known_spend += dec(purchase_amount, 'Gross purchase commitment', 0)
        included.append(purchase['id'])
    planned = assumptions.get('order_policy') != 'reorder' and config['question'] in {'Q-REPLENISH', 'Q-EXPLORE'} and assumptions.get('order_quantity') is not None
    if planned:
        order_date = assumptions.get('order_date')
        if not order_date:
            unknown.append('assumed-replenishment')
            warnings.append('Proposed replenishment has no order commitment date. Receipt timing does not establish its budget period.')
        elif start <= order_date <= end:
            product = get_product(config, workspace)
            cost = assumptions.get('unit_cost') if assumptions.get('unit_cost') is not None else product.get('cost')
            if cost is None:
                unknown.append('assumed-replenishment')
                warnings.append('Proposed replenishment has unknown unit cost and supplies no invented purchase amount.')
            else:
                known_spend += dec(assumptions['order_quantity'], 'Proposed purchase quantity', 0) * dec(cost, 'Proposed purchase unit cost', 0)
                included.append('assumed-replenishment')
                warnings.append(f'Proposed replenishment uses the {"owner-selected" if assumptions.get("unit_cost") is not None else "supplied product"} unit cost in {currency}; no payment date is inferred.')
    partial = bool(unknown)
    return {'amount': number(amount), 'known_spend': number(known_spend), 'spend': None if partial else number(known_spend), 'headroom': None if partial else number(amount - known_spend), 'breach': None if partial else number(max(Decimal(0), known_spend - amount)), 'start_date': start, 'end_date': end, 'basis': 'gross_order_commitment', 'scope': 'supplied business-wide purchases', 'partial': partial, 'included_purchase_ids': included, 'unpriced_or_undated_purchase_ids': unknown, 'warnings': warnings}


def prepare(kind: str, config: dict, workspace: dict, dependency: dict | None = None, pending: bool = False) -> dict:
    validate_workspace(workspace)
    if kind == 'forecast':
        values, warnings = forecast_values(config, workspace)
        return {'forecast': values, 'warnings': warnings}
    families = set(config.get('output_families') or [])
    if not families:
        raise InputError('Select at least one supported result family.')
    inventory_questions = {'Q-NEW-ORDER', 'Q-REPLENISH', 'Q-DEMAND-CHANGE', 'Q-SLOW-SUPPLIER', 'Q-SUPPLIER-ORDER-STOCKOUT'}
    if config['question'] in inventory_questions and 'inventory' not in families:
        raise InputError('This focused question requires the inventory result family. Select exploration for unrelated cash-only results.')
    if config['question'] in {'Q-CASH-SUFFICIENCY', 'Q-CRITICAL-COLLECTION'} and 'cash' not in families:
        raise InputError('This focused question requires cash results. Customer-debt exploration can show timing without opening cash.')
    if config['question'] == 'Q-CUSTOMER-DEBT' and not families.intersection({'cash', 'debt'}):
        raise InputError('Customer-debt analysis requires the debt or cash family.')
    validate_financial_assumptions(config)
    prepared: dict[str, Any] = {'warnings': []}
    if config['question'] == 'Q-EXPLORE' and config['assumptions'].get('order_quantity') is None and any(config['assumptions'].get(k) is not None for k in ('order_date', 'receipt_date', 'lead_time_days')):
        raise InputError('Exploratory purchasing changes need an explicit proposed order quantity; timing controls alone do not identify a purchase.')
    if config['question'] == 'Q-EXPLORE' and config['assumptions'].get('purchase_id'):
        raise InputError('Use the supplier-order-change question to change a recorded purchase. Exploratory order controls create one separate proposed purchase.')
    if config['assumptions'].get('demand_multiplier') is not None and config['question'] not in {'Q-DEMAND-CHANGE', 'Q-EXPLORE'}:
        raise InputError('Use the demand-change question with an explicit dated interval for a demand multiplier.')
    if 'inventory' in families:
        prepared['inventory'] = inventory_inputs(config, workspace, dependency, pending)
        prepared['warnings'].extend(prepared['inventory']['warnings'])
    if families.intersection({'cash', 'debt'}):
        prepared['finance'] = finance_inputs(config, workspace, families)
        prepared['warnings'].extend(prepared['finance']['warnings'])
    assumptions_terms = config['assumptions'].get('supplier_terms_id') or config['assumptions'].get('customer_terms_id')
    if assumptions_terms:
        if ('inventory' not in families and config['assumptions'].get('new_credit_sales_amount') is None) or not families.intersection({'cash', 'debt'}):
            raise InputError('Selected scenario payment terms need inventory fulfillment plus a cash or debt result family.')
        selected_terms(config, workspace, 'supplier')
        selected_terms(config, workspace, 'customer')
    if 'inventory' in families and 'cash' in families and not assumptions_terms:
        if config['question'] in {'Q-NEW-ORDER', 'Q-REPLENISH', 'Q-SUPPLIER-ORDER-STOCKOUT', 'Q-EXPLORE'} and any(config['assumptions'].get(k) is not None for k in ('order_quantity', 'price', 'unit_cost')):
            prepared['warnings'].append('Partial financial scope. Inventory/order changes do not infer new payment or collection terms. Cash includes only separately supplied timed events.')
    prepared['budget'] = purchasing_budget(config, workspace)
    if prepared['budget']:
        prepared['warnings'].extend(prepared['budget']['warnings'])
    return prepared


def execute(kind: str, config: dict, workspace: dict, run_id: str, dependency: dict | None = None, baseline: dict | None = None) -> dict:
    prepared = prepare(kind, config, workspace, dependency)
    timeline = dates(config)
    currency = workspace['profile']['currency']
    series = [{'date': d} for d in timeline]
    metrics: list[dict] = []
    events: list[dict] = []
    explanations: list[str] = []
    forecast_artifact = None
    if kind == 'forecast':
        observed, _ = forecast_history(config, workspace)
        forecast_artifact = build_forecast(config, observed, workspace['mode'])
        product = get_product(config, workspace)
        stockout_observations = [{'id': observation['id'], 'date': observation['date'], 'phase': observation.get('phase', 'unspecified'), 'location_id': observation.get('locationId'), 'source_id': observation.get('sourceId')} for observation in workspace.get('serviceObservations', []) if observation.get('productId') == product['id'] and observation.get('unit') == product['unit'] and observation.get('availableQuantity') == 0 and matches_location(observation, config, workspace) and min(observed) <= observation.get('date', '') < config['start_date']]
        stockout_observations.sort(key=lambda observation: (observation['date'], observation['id']))
        forecast_artifact['diagnostics']['stockout_observations'] = stockout_observations
        stockout_dates = {observation['date'] for observation in stockout_observations}
        for point in forecast_artifact['history']:
            if point['date'] in stockout_dates:
                point['observed_zero_stock'] = 1
        if stockout_observations:
            prepared['warnings'].append(f'{len(stockout_observations)} recorded zero-stock observations across {len(stockout_dates)} dated periods are flagged in saved history. Location and opening/closing phase remain explicit. Observed sales are unchanged; no lost-demand adjustment or full-day stockout duration is inferred.')
        prepared['forecast'] = forecast_artifact['values']
        prepared['warnings'].extend(forecast_artifact['diagnostics']['limitations'])
        unit = get_product(config, workspace)['unit']
        for point, value in zip(series, prepared['forecast'], strict=True):
            point['demand'] = value
        metrics.append(metric('forecast_demand', 'Forecast demand across the selected daily window', sum(Decimal(str(v)) for v in prepared['forecast']), unit))
        explanations.append(f'The saved {config["engine"]} daily forecast covers {timeline[0]} through {timeline[-1]}.')
    if 'inventory' in prepared:
        inventory = prepared['inventory']
        stock = inventory['opening_stock']
        backlog = inventory['opening_backlog']
        reserved_backlog = inventory['reserved_backlog']
        retained_reserved = inventory['retained_reserved']
        backlog_total_fulfilled = Decimal(0)
        new_fulfilled_total = Decimal(0)
        backlog_cohorts = [[None, backlog]] if backlog else []
        minimum = stock
        demand_total = Decimal(0)
        unmet_total = Decimal(0)
        zero_days = 0
        receipts: dict[str, Decimal] = defaultdict(Decimal)
        for event in inventory['events']:
            events.append(event)
            if event['type'] == 'receipt':
                receipts[event['date']] += Decimal(str(event['quantity']))
        for point in series:
            d = point['date']
            purchased = receipts[d]
            stock += purchased
            released = min(reserved_backlog, backlog)
            stock += released
            retained_reserved -= released
            reserved_backlog -= released
            prior_fulfilled = min(stock, backlog)
            stock -= prior_fulfilled
            backlog -= prior_fulfilled
            backlog_total_fulfilled += prior_fulfilled
            to_apply = prior_fulfilled
            new_backlog_fulfilled = Decimal(0)
            for cohort in backlog_cohorts:
                filled = min(cohort[1], to_apply)
                if cohort[0] is not None:
                    new_backlog_fulfilled += filled
                cohort[1] -= filled
                to_apply -= filled
            requested = inventory['demand'][d]
            unmet = max(Decimal(0), requested - stock)
            fulfilled = requested - unmet
            stock -= fulfilled
            new_fulfilled_total += fulfilled + new_backlog_fulfilled
            lost = unmet if inventory['backlog_policy'] == 'lost_sales' else Decimal(0)
            if inventory['backlog_policy'] == 'carry' and unmet:
                backlog += unmet
                backlog_cohorts.append([d, unmet])
            demand_total += requested
            unmet_total += unmet
            minimum = min(minimum, stock)
            zero_days += stock == 0
            point.update({'demand': number(requested), 'fulfilled': number(fulfilled), 'sale_fulfilled': number(fulfilled + new_backlog_fulfilled), 'inventory': number(stock), 'unmet_demand': number(unmet), 'purchase': number(purchased), 'lost_units': number(lost), 'backorders': number(backlog), 'backlog_fulfilled': number(prior_fulfilled), 'inventory_position': number(stock + sum((q for receipt_day, q in receipts.items() if receipt_day > d), Decimal(0)) - backlog)})
            if not inventory['backlog_complete']:
                point.pop('inventory_position', None)
            if inventory['on_hand_known']:
                point['on_hand'] = number(stock + retained_reserved)
            dated_backlog = [cohort[0] for cohort in backlog_cohorts if cohort[1] > 0 and cohort[0]]
            point['oldest_modeled_backlog_age_days'] = (day(d) - day(min(dated_backlog))).days if dated_backlog else None
            if prior_fulfilled:
                events.append({'id': f'backlog-fulfilled-{d}', 'date': d, 'type': 'backlog_fulfillment', 'label': 'Prior backorders fulfilled before new demand', 'quantity': number(prior_fulfilled), 'source_id': config['product_id']})
            closing_position = stock + sum((q for receipt_day, q in receipts.items() if receipt_day > d), Decimal(0)) - backlog
            policy_events = policy_order(config, d, closing_position)
            for planned_event in policy_events:
                events.append(planned_event)
                inventory['events'].append(planned_event)
                if planned_event['type'] == 'receipt':
                    receipts[planned_event['date']] += Decimal(str(planned_event['quantity']))
                    if 'inventory_position' in point:
                        point['inventory_position'] += planned_event['quantity']
            if requested:
                events.append({'id': f'demand-{d}', 'date': d, 'type': 'demand', 'label': 'Requested units under the saved demand basis', 'quantity': number(requested), 'source_id': config['product_id']})
            if unmet:
                events.append({'id': f'unmet-{d}', 'date': d, 'type': 'unmet_demand', 'label': 'New lost units' if inventory['backlog_policy'] == 'lost_sales' else 'New units added to carried backlog', 'quantity': number(unmet), 'source_id': config['product_id']})
        unit = inventory['product']['unit']
        metrics += [metric('opening_inventory', 'Opening available inventory', inventory['opening_stock'], unit), metric('ending_inventory', 'Ending available inventory', stock, unit), metric('minimum_inventory', 'Minimum observed available inventory', minimum, unit), metric('unmet_demand', 'Unfulfilled requested units', unmet_total, unit), metric('zero_stock_days', 'Days with zero closing stock', zero_days, 'days'), metric('fill_rate', 'Initial unit fill rate under the daily event convention', (demand_total - unmet_total) / demand_total * 100 if demand_total > 0 else None, '%')]
        metrics += [metric('opening_backorders', 'Supplied opening backorders', inventory['opening_backlog'] if inventory['backlog_complete'] or inventory['opening_backlog'] else None, unit), metric('ending_backorders', 'Closing carried backorders', backlog, unit), metric('backorders_fulfilled', 'Prior backorders fulfilled during the window', backlog_total_fulfilled, unit), metric('lost_units', 'New demand lost rather than carried', sum(Decimal(str(p['lost_units'])) for p in series), unit), metric('average_inventory', 'Average daily closing available inventory', sum(Decimal(str(p['inventory'])) for p in series) / len(series), unit), metric('maximum_inventory', 'Maximum daily closing available inventory', max(p['inventory'] for p in series), unit)]
        prepared['warnings'].append(f'New unmet demand policy: {inventory["backlog_policy"]}. Existing backorders are served first. Their original age and prior invoicing are unknown; no new sales or collections are inferred for opening backlog.')
        if demand_total == 0:
            prepared['warnings'].append('Unit fill rate is unavailable because requested demand is zero.')
        assumptions = config['assumptions']
        price = assumptions.get('price') if assumptions.get('price') is not None else inventory['product'].get('price')
        cost = assumptions.get('unit_cost') if assumptions.get('unit_cost') is not None else inventory['product'].get('cost')
        if price is not None:
            price = number(dec(price, 'Unit selling price', 0) * (1 - dec(assumptions.get('discount_percent', 0), 'Scenario discount percent', 0) / 100))
            metrics.append(metric('modeled_sales', 'Modeled sales on newly fulfilled demand', dec(price, 'Unit price', 0) * new_fulfilled_total, currency))
        if price is not None and cost is not None:
            gross_profit = (dec(price, 'Unit price', 0) - dec(cost, 'Unit cost', 0)) * new_fulfilled_total
            metrics.append(metric('gross_profit', 'Modeled gross profit on newly fulfilled units, excluding operating costs', gross_profit, currency))
            metrics.append(metric('lost_margin', 'Modeled margin from lost sales', (dec(price, 'Unit price', 0) - dec(cost, 'Unit cost', 0)) * sum(Decimal(str(p['lost_units'])) for p in series), currency))
        else:
            prepared['warnings'].append('Gross profit is unavailable for missing compatible unit price or cost. Missing values are not zero.')
        safety_stock = assumptions.get('safety_stock') if assumptions.get('safety_stock') is not None else inventory['product'].get('safetyStock')
        service_target = assumptions.get('service_target') if assumptions.get('service_target') is not None else inventory['product'].get('serviceTarget')
        if safety_stock is not None:
            metrics.append(metric('days_below_safety_stock', 'Days closing available stock is below the supplied safety-stock target', sum(p['inventory'] < safety_stock for p in series), 'days'))
        if service_target is not None and demand_total > 0:
            achieved = (demand_total - unmet_total) / demand_total * 100
            metrics += [metric('service_target', 'Owner-selected immediate unit-fill target', service_target, '%'), metric('service_target_gap', 'Immediate unit fill minus the selected service target', achieved - Decimal(str(service_target)), 'percentage points')]
        policy_orders = [e for e in events if e['type'] == 'purchase_order']
        metrics += [metric('scenario_order_count', 'Modeled purchase order events in the window', sum(timeline[0] <= e['date'] <= timeline[-1] for e in policy_orders), 'orders'), metric('scenario_order_quantity', 'Modeled ordered units in the window', sum((Decimal(str(e['quantity'])) for e in policy_orders if timeline[0] <= e['date'] <= timeline[-1]), Decimal(0)), unit)]
        if assumptions.get('order_policy') == 'reorder':
            prepared['warnings'].append('The owner-selected fixed-quantity rule orders at daily close when inventory position is at or below the explicit reorder point. Receipts occur after the stated lead time, with a minimum next-day receipt for zero-day lead time. The engine does not choose a policy or label it optimal.')
        if config['assumptions'].get('order_policy') == 'reorder' and workspace.get('budget'):
            cost_basis = config['assumptions'].get('unit_cost') if config['assumptions'].get('unit_cost') is not None else inventory['product'].get('cost')
            policy_purchases = [{'id': e['id'], 'productId': config['product_id'], 'orderDate': e['date'], 'quantity': e['quantity'], 'amount': e['quantity'] * cost_basis if cost_basis is not None else None} for e in policy_orders]
            budget_config = {**config, 'assumptions': {k: v for k, v in config['assumptions'].items() if k not in {'order_quantity', 'order_date', 'receipt_date'}}}
            prepared['budget'] = purchasing_budget(budget_config, {**workspace, 'purchases': workspace['purchases'] + policy_purchases})
        first_shortfall = next((p for p in series if p.get('unmet_demand', 0) > 0), None)
        explanations.append(f'Closing inventory on {timeline[-1]} is {number(stock):g} {unit}.')
        if first_shortfall:
            explanations.append(f'First modeled unmet demand occurs on {first_shortfall["date"]} for {first_shortfall["unmet_demand"]:g} {unit}.')
    if 'finance' in prepared:
        finance = prepared['finance']
        if 'inventory' in prepared or config['assumptions'].get('new_credit_sales_amount') is not None:
            apply_scenario_finance(config, workspace, prepared.get('inventory'), series, finance)
            prepared['warnings'].extend(finance['warnings'])
        if 'cash' in config['output_families'] and not any(event['_direction'] in {'in', 'out'} and timeline[0] <= event['date'] <= timeline[-1] for event in finance['events']):
            raise InputError('The selected cash window has no usable timed payment or collection after applying scenario assumptions. Extend the horizon or request debt-only results.')
        planned_payable = finance.get('planned_payable', Decimal(0))
        cash = finance['opening_cash']
        selected_reserve = config['assumptions'].get('reserve')
        if selected_reserve is None and workspace.get('cash'):
            selected_reserve = workspace['cash'].get('reserve')
        if selected_reserve is not None:
            selected_reserve = dec(selected_reserve, 'Owner-selected reserve', 0)
        receivable = finance['receivable']
        provider = finance['provider']
        payable = finance['payable']
        financing = finance['financing']
        customers = finance['customers'].copy()
        cash_floor = cash
        floor_date, floor_phase = timeline[0], 'opening'
        first_gap = {'date': timeline[0], 'phase': 'opening', 'amount': number(-cash)} if cash is not None and cash < 0 else None
        inflows: dict[str, Decimal] = defaultdict(Decimal)
        outflows: dict[str, Decimal] = defaultdict(Decimal)
        paid_receivable: dict[str, Decimal] = defaultdict(Decimal)
        available_provider: dict[str, Decimal] = defaultdict(Decimal)
        paid_payable: dict[str, Decimal] = defaultdict(Decimal)
        paid_financing: dict[str, Decimal] = defaultdict(Decimal)
        new_receivable: dict[str, Decimal] = defaultdict(Decimal)
        new_planned_payable: dict[str, Decimal] = defaultdict(Decimal)
        paid_planned_payable: dict[str, Decimal] = defaultdict(Decimal)
        new_customers: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        customer_payments: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        for event in finance['events']:
            d = event['date']
            amount = Decimal(str(event['amount']))
            if event['_direction'] == 'in':
                inflows[d] += amount
            elif event['_direction'] == 'out':
                outflows[d] += amount
            new_receivable[d] += Decimal(str(event.get('_new_receivable', 0)))
            new_planned_payable[d] += Decimal(str(event.get('_new_planned_payable', 0)))
            paid_planned_payable[d] += Decimal(str(event.get('_planned_payable', 0)))
            if event.get('_new_customer'):
                new_customers[d][event['_new_customer']] += Decimal(str(event.get('_new_receivable', 0)))
            paid_receivable[d] += Decimal(str(event.get('_receivable', 0)))
            available_provider[d] += Decimal(str(event.get('_provider', 0)))
            paid_payable[d] += Decimal(str(event.get('_payable', 0)))
            paid_financing[d] += Decimal(str(event.get('_financing', 0)))
            if event.get('_customer'):
                customer_payments[d][event['_customer']] += Decimal(str(event.get('_customer_payment', amount)))
            events.append({k: v for k, v in event.items() if not k.startswith('_')})
        for point in series:
            d = point['date']
            receivable += new_receivable[d] - paid_receivable[d]
            planned_payable += new_planned_payable[d] - paid_planned_payable[d]
            provider -= available_provider[d]
            payable -= paid_payable[d]
            financing -= paid_financing[d]
            for customer, added in new_customers[d].items():
                customers[customer] = customers.get(customer, Decimal(0)) + added
            for customer, paid in customer_payments[d].items():
                customers[customer] = customers.get(customer, Decimal(0)) - paid
            customer_total = sum(customers.values(), Decimal(0))
            concentration = max(customers.values()) / customer_total * 100 if customer_total > 0 else None
            point.update({'inflow': number(inflows[d]), 'outflow': number(outflows[d]), 'receivable': number(receivable), 'provider_pending': number(provider)})
            point['customer_concentration'] = number(concentration) if concentration is not None else None
            if cash is not None:
                point['payable'] = number(payable)
                point['planned_payable'] = number(planned_payable)
                point['financing_debt'] = number(financing)
                cash += inflows[d] - outflows[d]
                point['cash'] = number(cash)
                point['zero'] = 0
                if selected_reserve is not None:
                    point['reserve'] = number(selected_reserve)
                if cash < cash_floor:
                    cash_floor, floor_date, floor_phase = cash, d, 'closing'
                if cash < 0 and first_gap is None:
                    first_gap = {'date': d, 'phase': 'closing', 'amount': number(-cash)}
        metrics += [metric('receivable_outstanding', 'Closing customer receivables, unpaid stage', receivable, currency), metric('provider_pending', 'Closing customer funds pending provider availability', provider, currency), metric('internal_debt', 'Closing Internal Debt across distinct unpaid and provider-pending stages', receivable + provider, currency)]
        metrics.append(metric('planned_payable', 'Closing modeled supplier obligations without confirmed invoices', planned_payable, currency))
        metrics.append(metric('customer_concentration', 'Largest customer share of closing supplied Internal Debt', series[-1]['customer_concentration'], '%'))
        if series[-1]['customer_concentration'] is None:
            prepared['warnings'].append('Closing customer concentration is unavailable because the remaining supplied customer balance is zero. Zero is not a percentage denominator.')
        if cash is not None:
            metrics += [metric('external_debt', 'Closing confirmed supplier External Debt', payable, currency), metric('financing_debt', 'Closing recorded Financing Debt', financing, currency)]
            metrics += [metric('ending_cash', 'Closing cash for included events', cash, currency), metric('cash_floor', f'Cash floor at {floor_phase} on {floor_date}', cash_floor, currency), metric('first_gap', f'First cash gap{(" at " + first_gap["phase"] + " on " + first_gap["date"]) if first_gap else " not observed"}', first_gap['amount'] if first_gap else 0, currency), metric('maximum_deficit', 'Maximum modeled zero-cash deficit', max(Decimal(0), -cash_floor), currency)]
            reserve = selected_reserve
            if reserve is not None:
                metrics += [metric('reserve_headroom', 'Headroom above the owner-selected reserve', cash_floor - reserve, currency), metric('reserve_shortfall', 'Additional opening liquidity to preserve the selected reserve', max(Decimal(0), reserve - cash_floor), currency)]
            explanations.append(f'The cash floor is {number(cash_floor):g} {currency} at {floor_phase} on {floor_date}. This describes included daily observations only.')
            if first_gap:
                explanations.append(f'The first recorded gap is {first_gap["amount"]:g} {currency} at {first_gap["phase"]} on {first_gap["date"]}. No payment-priority allocation is assumed.')
            else:
                explanations.append('No negative daily observation appears for included events. This does not establish complete-business sufficiency or intraday payment clearance.')
        else:
            prepared['warnings'].append('Debt and flow timing only. Opening cash was not requested; no absolute cash balance, floor or sufficiency is calculated.')
        explanations.append(f'Unpaid customer receivables end at {number(receivable):g} {currency} on {timeline[-1]}; separately pending provider funds end at {number(provider):g} {currency}.')
    budget = prepared.get('budget')
    if budget:
        period = f'{budget["start_date"]} through {budget["end_date"]}'
        metrics += [metric('purchasing_budget', f'Purchasing budget for {period}', budget['amount'], currency), metric('budget_known_spend', f'Known gross purchase commitments for {period}', budget['known_spend'], currency), metric('budget_spend', f'Gross purchase commitments for {period}', budget['spend'], currency), metric('budget_headroom', f'Purchasing-budget headroom for {period}', budget['headroom'], currency), metric('budget_breach', f'Purchasing-budget breach for {period}', budget['breach'], currency)]
        explanations.append(f'Purchasing budget uses gross order commitments dated {period}. Paid amounts do not reduce committed purchasing spend. Budget is separate from available cash.')
        if budget['partial']:
            prepared['warnings'].append('Partial purchasing-budget comparison. Unpriced or undated purchases prevent a complete spend, headroom or breach result. Known priced commitments remain visible.')
    warnings = list(dict.fromkeys(prepared['warnings']))
    result = {'start_date': timeline[0], 'end_date': timeline[-1], 'grain': 'daily', 'metrics': metrics, 'series': series, 'events': sorted(events, key=lambda event: (event['date'], event['id'])), 'assumptions': [f'{key}: {value}' for key, value in config['assumptions'].items() if value is not None], 'warnings': warnings, 'explanations': explanations, 'comparison': None, 'scene_manifest': None, 'series_metadata': {'run_id': run_id, 'calculation_version': VERSION, 'currency': currency, 'timezone': workspace['profile']['timezone'], 'scope': scope(config, workspace), 'observations': {'inventory': 'closing', 'cash': 'closing', 'receivable': 'closing', 'provider_pending': 'closing', 'payable': 'closing confirmed supplier balance', 'financing_debt': 'closing recorded financing balance', 'customer_concentration': 'largest share of closing supplied customer balance, percent', 'demand': 'flow', 'inflow': 'flow', 'outflow': 'flow', 'purchase': 'receipt flow', 'unmet_demand': 'new demand not immediately fulfilled', 'fulfilled': 'new demand fulfilled on request date', 'backorders': 'closing carried backlog', 'backlog_fulfilled': 'prior backlog fulfillment flow', 'lost_units': 'new lost-demand flow', 'on_hand': 'closing physical quantity including remaining reservations', 'inventory_position': 'closing available plus future confirmed receipts minus backlog'}}}
    result['series_metadata']['quantity_unit'] = get_product(config, workspace)['unit'] if kind == 'forecast' or 'inventory' in prepared else None
    result['budget'] = budget
    if forecast_artifact:
        result['forecast_diagnostics'] = forecast_artifact['diagnostics']
        result['history'] = forecast_artifact['history']
    if baseline:
        result['comparison'] = compare(result, baseline)
    result['scene_manifest'] = scene_manifest(result, config, run_id, kind)
    return result


def compare(result: dict, baseline: dict) -> dict:
    previous = baseline['result']
    if result['start_date'] != previous['start_date'] or result['end_date'] != previous['end_date'] or result['grain'] != previous['grain']:
        raise InputError('Comparison requires identical daily date windows. Choose a compatible succeeded baseline.')
    metadata = result['series_metadata']
    previous_metadata = previous['series_metadata']
    if any(metadata[key] != previous_metadata[key] for key in ('scope', 'currency', 'calculation_version', 'timezone')):
        raise InputError('Comparison requires matching scope, currency, timezone and calculation version.')
    old_metrics = {m['key']: m for m in previous['metrics']}
    def quantity_unit(artifact):
        stored = artifact['series_metadata'].get('quantity_unit')
        if stored is not None:
            return stored
        return next((item['unit'] for item in artifact['metrics'] if item['key'] in {'opening_inventory', 'forecast_demand'}), None)
    if quantity_unit(result) != quantity_unit(previous):
        raise InputError('Comparison requires the same quantity unit. This product was standardized after the baseline; choose a baseline in the current unit or rerun the original snapshot.')
    if any(item['key'] in old_metrics and item['unit'] != old_metrics[item['key']]['unit'] for item in result['metrics']):
        raise InputError('Comparison requires compatible metric units. No automatic conversion of saved results is applied.')
    deltas = []
    for item in result['metrics']:
        if item['key'].startswith('budget_') or item['key'] == 'purchasing_budget':
            current_budget, previous_budget = result.get('budget'), previous.get('budget')
            if not current_budget or not previous_budget or any(current_budget.get(k) != previous_budget.get(k) for k in ('start_date', 'end_date', 'basis', 'scope')):
                result['warnings'].append('Purchasing-budget deltas are unavailable for different budget periods, scopes or commitment bases.')
                continue
        before = old_metrics.get(item['key'])
        if before is None or before['unit'] != item['unit'] or before['value'] is None or item['value'] is None:
            continue
        delta = item['value'] - before['value']
        deltas.append({'key': item['key'], 'label': item['label'], 'unit': 'percentage points' if item['unit'] == '%' else item['unit'], 'baseline': before['value'], 'alternative': item['value'], 'delta': round(delta, 6), 'percentage_change': round(delta / before['value'] * 100, 6) if before['value'] > 0 and item['unit'] != '%' else None})
    old_points = {p['date']: p for p in previous['series']}
    points = []
    for point in result['series']:
        prior = old_points[point['date']]
        points.append({'date': point['date'], **{f'{key}_delta': round(point[key] - prior[key], 6) for key in ('inventory', 'cash', 'receivable', 'provider_pending', 'payable', 'financing_debt', 'customer_concentration', 'demand') if point.get(key) is not None and prior.get(key) is not None}})
    return {'baseline_run_id': baseline['id'], 'metrics': deltas, 'series': points}


def scene_manifest(result: dict, config: dict, run_id: str, kind: str) -> dict:
    supported = config['question'] != 'Q-EXPLORE' and kind == 'simulation'
    allowed = ASSETS[config['question']] if supported else []
    entities, events = [], []
    if supported:
        if 'inventory' in config.get('output_families', []) and 'sku_stack' in allowed:
            entities.append({'id': 'inventory', 'asset_id': 'sku_stack', 'source_id': config['product_id']})
        if any('cash' in p for p in result['series']) and 'cash_account' in allowed:
            entities.append({'id': 'cash', 'asset_id': 'cash_account', 'source_id': run_id})
        if any('receivable' in p for p in result['series']) and 'receivable_balance' in allowed:
            entities.append({'id': 'receivables', 'asset_id': 'receivable_balance', 'source_id': run_id})
        event_assets = {'customer_order': 'customer_order', 'purchase_order': 'purchase_order', 'receipt': 'delivery_truck', 'demand': 'customer_demand', 'unmet_demand': 'stockout_marker', 'collection': 'cash_inflow', 'provider_availability': 'cash_inflow', 'suppliers_payment': 'supplier_payable', 'payroll_payment': 'payroll_group', 'rent_payment': 'rent_location'}
        for event in result['events']:
            candidate = event_assets.get(event['type'], 'cash_outflow')
            asset = candidate if candidate in allowed else 'calendar_marker'
            if asset in allowed:
                entity_id = f'event-{event["id"]}'
                entities.append({'id': entity_id, 'asset_id': asset, 'source_id': event.get('source_id') or run_id})
                events.append({'event_id': event['id'], 'date': event['date'], 'entity_id': entity_id})
    keys = sorted({key for point in result['series'] for key in point if key != 'date'})
    return {'contract_version': '1', 'scene_manifest_supported': supported, 'run_id': run_id, 'question_key': config['question'], 'start_date': result['start_date'], 'end_date': result['end_date'], 'grain': 'daily', 'allowed_asset_ids': allowed, 'asset_metadata': [{'asset_id': asset, 'label': asset.replace('_', ' ').capitalize(), 'description': ASSET_MEANINGS[asset]} for asset in allowed], 'entities': entities, 'events': events, 'series': [{'key': key, 'run_id': run_id} for key in keys] if supported else []}
