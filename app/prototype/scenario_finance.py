from datetime import timedelta
from copy import deepcopy
from decimal import Decimal

from .schema import InputError, day, numeric


def amount(value, label, minimum=0):
    numeric(float(value) if isinstance(value, Decimal) else value, label, minimum)
    return Decimal(str(value))


def selected_terms(config: dict, workspace: dict, party: str) -> dict | None:
    identifier = config['assumptions'].get(f'{party}_terms_id')
    if not identifier:
        return None
    term = next((item for item in workspace.get('paymentTerms', []) if item.get('id') == identifier), None)
    if not term or term.get('party') != party:
        raise InputError(f'Select supplied {party} payment terms with their contractual starting event.')
    if term.get('status') not in {'agreed', 'proposed'}:
        raise InputError('Payment terms need an agreed or explicitly proposed status.')
    if term['status'] == 'proposed' and not config['assumptions'].get('terms_accepted'):
        raise InputError('Explicitly accept proposed payment terms as hypothetical before running. No agreement is implied.')
    for field in ('days', 'advanceDays'):
        value = term.get(field, 0)
        numeric(value, f'{party} {field}', None if field == 'advanceDays' else 0)
        if int(value) != value:
            raise InputError('Payment terms use whole calendar days.')
    treatment_key = 'purchase_cash_treatment' if party == 'supplier' else 'demand_cash_treatment'
    if term.get('advancePercent') is None and config['assumptions'].get(treatment_key) != 'already_recorded' and not config['assumptions'].get('terms_no_advance_confirmed'):
        raise InputError('These payment terms have no recorded advance percentage. Explicitly accept a no-advance scenario assumption or record the actual advance terms; unknown is not zero.')
    advance = amount(term.get('advancePercent', 0), 'Advance percent')
    if advance > 100:
        raise InputError('Advance percent cannot exceed 100.')
    return term


def payment_schedule(term: dict, total: Decimal, event_dates: dict, already_paid: Decimal, opening: str) -> list[tuple[str, Decimal]]:
    basis = term.get('startEvent')
    if basis not in {'invoice-date', 'order-date', 'receipt-date', 'delivery-date'}:
        raise InputError('Payment terms require an explicit supported starting event.')
    base = event_dates.get(basis)
    if not base:
        raise InputError(f'The selected terms start at {basis}; supply that event date for this scenario.')
    advance = total * amount(term.get('advancePercent', 0), 'Advance percent') / 100
    slices = []
    if advance:
        slices.append(((day(base) + timedelta(days=int(term.get('advanceDays', 0)))).isoformat(), advance))
    if total - advance:
        slices.append(((day(base) + timedelta(days=int(term['days']))).isoformat(), total - advance))
    if already_paid > total:
        raise InputError('Already-paid allocation exceeds the scenario amount. Reconcile advances before changing the order.')
    remaining_paid = already_paid
    output = []
    for payment_date, value in sorted(slices):
        allocation = min(value, remaining_paid)
        value -= allocation
        remaining_paid -= allocation
        if value and payment_date < opening:
            raise InputError('A modeled payment falls before the opening boundary and is not covered by its explicit already-paid allocation. Align the dates or confirm the paid amount.')
        if value:
            output.append((payment_date, value))
    return output


def validate_financial_assumptions(config: dict) -> None:
    a = config['assumptions']
    families = set(config.get('output_families', []))
    customer_controls = {'demand_cash_treatment', 'invoice_delay_days', 'customer_order_date', 'customer_advance_received', 'new_credit_sales_amount', 'new_credit_sales_date', 'unpaid_share'}
    supplier_controls = {'purchase_cash_treatment', 'purchase_paid_amount', 'purchase_invoice_date', 'dispatch_date', 'supplier_payment_before_dispatch'}
    if any(a.get(key) is not None for key in customer_controls) and not a.get('customer_terms_id'):
        raise InputError('Customer financial assumptions require explicitly selected customer terms.')
    if any(a.get(key) is not None for key in supplier_controls) and not a.get('supplier_terms_id'):
        raise InputError('Purchase financial assumptions require explicitly selected supplier terms.')
    if a.get('supplier_terms_id') and ('cash' not in families or 'inventory' not in families):
        raise InputError('Supplier payment terms require inventory and cash results for the modeled purchase.')
    if a.get('new_credit_sales_date') is not None and a.get('new_credit_sales_amount') is None:
        raise InputError('A new credit sale date requires an explicit additional sales amount.')
    if a.get('new_credit_sales_amount') is not None and (not a.get('new_credit_sales_date') or a.get('demand_cash_treatment') != 'incremental'):
        raise InputError('Additional credit sales require an exact date and incremental customer invoice treatment.')
    if a.get('unpaid_share') is not None and a.get('demand_cash_treatment') != 'incremental':
        raise InputError('The unpaid share applies only to new incremental sales, not to already-recorded invoices.')
    if a.get('terms_no_advance_confirmed') and not (a.get('customer_terms_id') or a.get('supplier_terms_id')):
        raise InputError('A no-advance assumption needs selected payment terms.')
    if a.get('payment_id') or a.get('payment_date'):
        if 'cash' not in families or not a.get('payment_id') or not a.get('payment_date') or not a.get('payment_change_accepted'):
            raise InputError('A changed outgoing payment requires cash results, a selected obligation, an exact date and acceptance as a hypothetical schedule.')
        if a.get('supplier_terms_id') and a.get('purchase_cash_treatment') == 'replace_linked':
            raise InputError('Change a linked purchase through one payment assumption at a time: replacement supplier terms or a selected payment date.')
        if a['payment_date'] < config['start_date']:
            raise InputError('A changed payment cannot precede the reviewed opening cash boundary.')
    if a.get('supplier_payment_before_dispatch') and a.get('purchase_cash_treatment') == 'already_recorded':
        raise InputError('A dispatch prerequisite needs an explicitly modeled supplier payment schedule; existing records alone do not establish it.')
    if a.get('dispatch_date') is not None and not a.get('supplier_payment_before_dispatch'):
        raise InputError('Dispatch timing is used only with the explicit supplier-payment-before-dispatch prerequisite.')
    if a.get('supplier_payment_before_dispatch') and not a.get('dispatch_date'):
        raise InputError('The payment-before-dispatch prerequisite needs an explicit dispatch date.')
    if a.get('customer_terms_id') and not families.intersection({'cash', 'debt'}):
        raise InputError('Customer terms require cash or debt results.')


def apply_scenario_finance(config: dict, workspace: dict, inventory: dict | None, series: list[dict], finance: dict) -> None:
    a, start = config['assumptions'], config['start_date']
    currency = workspace['profile']['currency']
    product = inventory['product'] if inventory else {}
    supplier_term = selected_terms(config, workspace, 'supplier')
    customer_term = selected_terms(config, workspace, 'customer')
    finance.setdefault('planned_payable', Decimal(0))
    if supplier_term and inventory and a.get('order_policy') == 'reorder':
        for receipt in inventory['events']:
            if receipt['type'] != 'receipt' or not receipt['id'].startswith('policy-receipt-'):
                continue
            order_config = deepcopy(config)
            order_config['assumptions'].update({'order_policy': 'explicit', 'order_quantity': receipt['quantity'], 'order_date': receipt['order_date'], 'receipt_date': receipt['date'], '_policy_receipt_id': receipt['id']})
            order_config['assumptions'].pop('customer_terms_id', None)
            order_config['assumptions'].pop('purchase_paid_amount', None)
            apply_scenario_finance(order_config, workspace, inventory, series, finance)
        supplier_term = None
    if supplier_term and not inventory:
        raise InputError('Supplier payment terms require a modeled inventory purchase.')
    if supplier_term:
        treatment = a.get('purchase_cash_treatment')
        if treatment not in {'incremental', 'replace_linked', 'already_recorded'}:
            raise InputError('Declare whether the proposed supplier cash replaces linked records, is a separate purchase, or is already recorded.')
        if treatment != 'already_recorded':
            purchase = next((p for p in workspace['purchases'] if p['id'] == a.get('purchase_id')), None)
            if bool(purchase) != (treatment == 'replace_linked'):
                raise InputError('A changed recorded purchase must replace its linked cash schedule. A separate planned purchase uses incremental cash.')
            supplier_id = purchase.get('supplierId') if purchase else product.get('supplierId')
            if supplier_term.get('supplierId') and supplier_id != supplier_term['supplierId']:
                raise InputError('The selected supplier terms do not match the product or purchase supplier.')
            receipt = next((event for event in inventory['events'] if event['type'] == 'receipt' and event['id'] == (f'receipt-{purchase["id"]}' if purchase else a.get('_policy_receipt_id', 'assumed-replenishment'))), None)
            if not receipt:
                raise InputError('Supplier payment propagation needs an identifiable recorded or proposed receipt event.')
            quantity = amount(a.get('order_quantity') if a.get('order_quantity') is not None else purchase.get('quantity') if purchase else None, 'Scenario purchase quantity')
            original_unit_cost = amount(purchase['amount'], 'Recorded purchase amount') / amount(purchase['quantity'], 'Recorded purchase quantity') if purchase and purchase.get('amount') is not None and purchase['quantity'] > 0 else product.get('cost')
            cost = amount(a.get('unit_cost') if a.get('unit_cost') is not None else original_unit_cost, 'Scenario purchase unit cost')
            total = quantity * cost
            paid = amount(purchase.get('paidAmount', 0) if purchase else a.get('purchase_paid_amount', 0), 'Already paid purchase allocation')
            order_date = a.get('order_date') or (purchase.get('orderDate') if purchase else None)
            invoice_date = a.get('purchase_invoice_date')
            event_dates = {'order-date': order_date, 'receipt-date': receipt['date'], 'delivery-date': receipt['date'], 'invoice-date': invoice_date}
            schedule = payment_schedule(supplier_term, total, event_dates, paid, start)
            if a.get('supplier_payment_before_dispatch'):
                if a['dispatch_date'] > receipt['date']:
                    raise InputError('Dispatch must occur on or before the modeled receipt date.')
                if any(payment_date > a['dispatch_date'] for payment_date, _ in schedule):
                    raise InputError('The selected payment schedule violates the accepted full-payment-before-dispatch prerequisite. Change the payment or dispatch dates explicitly.')
                finance['warnings'].append(f'Full supplier payment is required by dispatch on {a["dispatch_date"]}; the selected schedule satisfies this accepted prerequisite. No intraday clearing order is inferred.')
            linked = [record for record in workspace['finance'] if record.get('kind') == 'payable' and purchase and record.get('linkedRecordId') == purchase['id']]
            if purchase:
                replaced_ids = {purchase['id'], *(record['id'] for record in linked)}
                finance['events'] = [event for event in finance['events'] if event.get('source_id') not in replaced_ids]
            source = purchase['id'] if purchase else a.get('_policy_receipt_id', 'scenario-purchase')
            outstanding = total - paid
            if linked:
                old_outstanding = sum((amount(record['amount'], 'Linked supplier amount') - amount(record['paidAmount'], 'Linked paid amount') for record in linked), Decimal(0))
                finance['payable'] += outstanding - old_outstanding
                balance_key = '_payable'
            else:
                recognition = max(start, order_date or receipt['date'])
                before_recognition = sum((value for d, value in schedule if d < recognition), Decimal(0))
                finance['events'].append({'id': f'{source}-planned-liability', 'date': recognition, 'type': 'planned_supplier_obligation', 'label': 'Scenario supplier obligation, separate from confirmed External Debt', 'amount': float(outstanding - before_recognition), 'source_id': source, '_direction': 'none', '_category': 'suppliers', '_new_planned_payable': float(outstanding - before_recognition)})
                balance_key = '_planned_payable'
            for i, (payment_date, value) in enumerate(schedule):
                applies_balance = bool(linked) or payment_date >= recognition
                finance['events'].append({'id': f'{source}-scenario-payment-{i}', 'date': payment_date, 'type': 'suppliers_payment', 'label': f'Scenario supplier payment · {supplier_term["counterparty"]}', 'amount': float(value), 'source_id': source, 'terms_id': supplier_term['id'], '_direction': 'out', '_category': 'suppliers', balance_key: float(value) if applies_balance else 0})
            finance['warnings'].append(f'Supplier cash uses {supplier_term["status"]} terms {supplier_term["id"]}, starting at {supplier_term["startEvent"]}. {paid:g} {currency} is already paid. Existing linked purchase/payable events are replaced once; source records are unchanged.')
    if customer_term:
        treatment = a.get('demand_cash_treatment')
        if treatment not in {'incremental', 'already_recorded'}:
            raise InputError('Declare modeled sales as new incremental invoice cohorts or already-recorded financial flows before deriving collections.')
        if treatment == 'incremental':
            price = amount(a.get('price') if a.get('price') is not None else product.get('price'), 'Scenario unit selling price') if inventory else Decimal(0)
            price *= 1 - amount(a.get('discount_percent', 0), 'Scenario discount percent') / 100
            unpaid_share = amount(a.get('unpaid_share', 0), 'Assumed unpaid share')
            if unpaid_share > 1:
                raise InputError('Unpaid share must be between zero and one; it is a scenario assumption, not a default probability.')
            if customer_term['startEvent'] == 'order-date' and not a.get('customer_order_date'):
                raise InputError('Order-based customer terms need an explicit customer order date.')
            if customer_term['startEvent'] == 'invoice-date' and a.get('invoice_delay_days') is None:
                raise InputError('Invoice-based customer terms need an explicit invoice delay from fulfillment; zero means invoice on fulfillment.')
            advance_remaining = amount(a.get('customer_advance_received', 0), 'Already received customer advance')
            cohorts = [{'date': point['date'], 'amount': amount(point.get('sale_fulfilled', point.get('fulfilled', 0)), 'Newly fulfilled units') * price} for point in series] if inventory else []
            if a.get('new_credit_sales_amount') is not None:
                credit_date = day(a.get('new_credit_sales_date'), 'New credit sale date').isoformat()
                if not start <= credit_date <= series[-1]['date']:
                    raise InputError('New credit sales must be dated inside the scenario window.')
                cohorts.append({'date': credit_date, 'amount': amount(a['new_credit_sales_amount'], 'Explicit new credit sales amount')})
            for index, point in enumerate(cohorts):
                total = point['amount']
                if not total:
                    continue
                d = point['date']
                invoice_date = (day(d) + timedelta(days=int(a.get('invoice_delay_days', 0)))).isoformat()
                paid = min(advance_remaining, total)
                advance_remaining -= paid
                schedule = payment_schedule(customer_term, total * (1 - unpaid_share), {'invoice-date': invoice_date, 'delivery-date': d, 'receipt-date': d, 'order-date': a.get('customer_order_date') or d}, paid, start)
                before_invoice = sum((value for payment_date, value in schedule if payment_date < invoice_date), Decimal(0))
                receivable_amount = total - paid - before_invoice
                customer = customer_term['counterparty']
                finance['events'].append({'id': f'scenario-invoice-{d}-{index}', 'date': invoice_date, 'type': 'scenario_invoice', 'label': f'Modeled new invoice · {customer}', 'amount': float(receivable_amount), 'source_id': f'scenario-sale-{d}-{index}', '_direction': 'none', '_category': 'collections', '_new_receivable': float(receivable_amount), '_new_customer': customer})
                for i, (payment_date, value) in enumerate(schedule):
                    invoice_settlement = value if payment_date >= invoice_date else Decimal(0)
                    finance['events'].append({'id': f'scenario-collection-{d}-{index}-{i}', 'date': payment_date, 'type': 'collection', 'label': f'Modeled collection from new fulfillment · {customer}', 'amount': float(value), 'source_id': f'scenario-sale-{d}-{index}', 'terms_id': customer_term['id'], '_direction': 'in', '_category': 'collections', '_receivable': float(invoice_settlement), '_customer': customer if invoice_settlement else None, '_customer_payment': float(invoice_settlement)})
            if advance_remaining:
                raise InputError('The declared customer advance exceeds the modeled fulfilled sales. Review quantities or retain it as a separate source balance.')
            finance['warnings'].append(f'New fulfilled demand creates incremental invoice cohorts using {customer_term["status"]} customer terms {customer_term["id"]}. Opening backlog is excluded because it may already be invoiced. Existing receivables and provider links are not recreated. {unpaid_share * 100:g}% is explicitly assumed to remain uncollected; this is not a probability estimate.')
    for event in finance['events']:
        state = (workspace.get('coverage') or {}).get(event['_category'], {})
        if state.get('state') == 'absent' and start <= event['date'] <= series[-1]['date'] and event['_direction'] != 'none':
            raise InputError(f'{event["_category"]} was marked absent but contains a derived payment or collection. Review its coverage.')
