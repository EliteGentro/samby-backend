from datetime import timedelta
from decimal import Decimal

from .schema import InputError, day, numeric


def validate_policy(config: dict, product: dict) -> None:
    a = config['assumptions']
    if a.get('order_policy') != 'reorder':
        return
    for key, label in [('reorder_point', 'Reorder point'), ('order_quantity', 'Fixed reorder quantity'), ('lead_time_days', 'Reorder lead time')]:
        value = a.get(key)
        if value is None:
            raise InputError(f'The owner-selected reorder rule needs an explicit {label.lower()}.')
        numeric(value, label, 0)
    if a['order_quantity'] <= 0:
        raise InputError('The fixed reorder quantity must be positive.')
    if config['question'] not in {'Q-REPLENISH', 'Q-EXPLORE'}:
        raise InputError('Use replenishment or exploration for an owner-selected recurring reorder rule.')
    if a.get('receipt_date') or a.get('order_date'):
        raise InputError('The recurring reorder rule determines its own dates. Clear the single planned order/receipt dates or select an explicit purchase.')


def policy_order(config: dict, date_value: str, inventory_position: Decimal) -> list[dict]:
    a = config['assumptions']
    if a.get('order_policy') != 'reorder' or inventory_position > Decimal(str(a['reorder_point'])):
        return []
    receipt_date = (day(date_value) + timedelta(days=max(1, int(a['lead_time_days'])))).isoformat()
    quantity = float(a['order_quantity'])
    source = config['product_id']
    return [
        {'id': f'policy-order-{date_value}', 'date': date_value, 'type': 'purchase_order', 'label': 'Owner-selected fixed-quantity reorder rule', 'quantity': quantity, 'source_id': source},
        {'id': f'policy-receipt-{date_value}', 'date': receipt_date, 'order_date': date_value, 'type': 'receipt', 'label': 'Receipt from the owner-selected reorder rule', 'quantity': quantity, 'source_id': source},
    ]
