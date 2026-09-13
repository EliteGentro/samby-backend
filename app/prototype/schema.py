from datetime import date, timedelta
import json
from math import isfinite
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


QUESTIONS = {
    'Q-NEW-ORDER', 'Q-REPLENISH', 'Q-CRITICAL-COLLECTION',
    'Q-CASH-SUFFICIENCY', 'Q-DEMAND-CHANGE', 'Q-SLOW-SUPPLIER',
    'Q-CUSTOMER-DEBT', 'Q-SUPPLIER-ORDER-STOCKOUT', 'Q-EXPLORE',
    'Q-POISON-APPLE', 'Q-DEAD-STOCK', 'Q-TREASURY-STRESS',
}
CATEGORIES = ('collections', 'suppliers', 'payroll', 'rent', 'taxes', 'financing', 'other')
ASSUMPTIONS = {
    'payment_id', 'payment_date', 'payment_change_accepted', 'dispatch_date', 'supplier_payment_before_dispatch',
    'daily_demand', 'order_quantity', 'order_date', 'receipt_date', 'lead_time_days',
    'collection_delay_days', 'collection_id', 'demand_multiplier', 'reserve',
    'price', 'unit_cost', 'purchase_id', 'stock_opening_confirmed',
    'cash_opening_estimate', 'forecast_overlap', 'demand_start_date',
    'demand_end_date', 'season_length_days', 'backlog_policy', 'opening_backlog_confirmed',
    'backlog_reservation_overlap', 'supplier_terms_id', 'customer_terms_id', 'terms_accepted', 'terms_no_advance_confirmed',
    'demand_cash_treatment', 'purchase_cash_treatment', 'purchase_paid_amount', 'purchase_invoice_date',
    'invoice_delay_days', 'customer_order_date', 'customer_advance_received', 'new_credit_sales_amount', 'new_credit_sales_date', 'unpaid_share', 'order_policy', 'reorder_point', 'safety_stock', 'service_target', 'discount_percent',
    'asem_stress',
    # Poison Apple (Growth Insolvency)
    'poison_order_amount', 'poison_margin_pct', 'poison_supplier_advance_pct',
    'poison_supplier_balance_days', 'poison_customer_days', 'poison_fixed_daily_costs',
    # Dead Stock Liberator
    'dio_threshold', 'liquidation_discount_pct', 'liquidation_days', 'holding_cost_daily_pct',
    # Treasury Edge Cases
    'payroll_amount', 'payroll_dates', 'payroll_buffer_days',
    'banking_cutoff_apply', 'weekend_shift_apply',
    'paused_supplier_ids', 'spiral_product_ids', 'spiral_restock_penalty_days',
    'disputed_record_ids', 'dispute_resolution_days', 'dispute_recovery_pct',
}



class InputError(ValueError):
    pass


def day(value: Any, label: str = 'Date') -> date:
    if not isinstance(value, str):
        raise InputError(f'{label} must be a YYYY-MM-DD date.')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise InputError(f'{label} must be a valid YYYY-MM-DD date.') from error
    if parsed.isoformat() != value:
        raise InputError(f'{label} must be a YYYY-MM-DD date.')
    return parsed


def numeric(value: Any, label: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise InputError(f'{label} must be a finite number. Unknown is not zero.')
    if minimum is not None and value < minimum:
        raise InputError(f'{label} must be at least {minimum:g}.')
    return float(value)


class AnalysisConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    engine: Literal['naive', 'seasonal-naive', 'lightgbm', 'catboost'] = 'naive'
    question: str = 'Q-EXPLORE'
    start_date: str
    horizon_days: int = Field(ge=1, le=365, strict=True)
    product_id: str | None = None
    location_id: str | None = None
    inventory_pool_id: str | None = None
    output_families: list[Literal['inventory', 'cash', 'debt']] = Field(default_factory=list)
    coverage_reviewed: bool = False
    assumptions: dict[str, Any] = Field(default_factory=dict)
    forecast_run_id: str | None = None
    baseline_run_id: str | None = None

    @model_validator(mode='after')
    def window_valid(self):
        if self.location_id and self.inventory_pool_id:
            raise ValueError('Select either a location or a confirmed inventory pool, not both.')
        try:
            day(self.start_date) + timedelta(days=self.horizon_days - 1)
        except OverflowError as error:
            raise ValueError('The requested end date is outside supported calendar dates.') from error
        return self

    @field_validator('start_date')
    @classmethod
    def date_valid(cls, value: str) -> str:
        day(value, 'Start date')
        return value

    @field_validator('question')
    @classmethod
    def question_valid(cls, value: str) -> str:
        if value not in QUESTIONS:
            raise ValueError('Choose one of the supported focused questions or Q-EXPLORE.')
        return value

    @field_validator('assumptions')
    @classmethod
    def assumptions_valid(cls, values: dict[str, Any]) -> dict[str, Any]:
        unknown = set(values) - ASSUMPTIONS
        if unknown:
            raise ValueError(f'Unsupported assumptions: {", ".join(sorted(unknown))}.')
        for name, value in values.items():
            if value is None:
                continue
            if name in {'order_date', 'receipt_date', 'demand_start_date', 'demand_end_date', 'purchase_invoice_date', 'customer_order_date', 'new_credit_sales_date', 'payment_date', 'dispatch_date'}:
                day(value, name)
            elif name in {'collection_id', 'purchase_id', 'supplier_terms_id', 'customer_terms_id', 'payment_id'}:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f'{name} must identify a supplied record.')
            elif name in {'stock_opening_confirmed', 'opening_backlog_confirmed', 'terms_accepted', 'terms_no_advance_confirmed', 'payment_change_accepted', 'supplier_payment_before_dispatch', 'asem_stress', 'banking_cutoff_apply', 'weekend_shift_apply'}:
                if not isinstance(value, bool):
                    raise ValueError(f'{name} must be true or false.')
            elif name in {'order_policy', 'backlog_policy', 'backlog_reservation_overlap', 'demand_cash_treatment', 'purchase_cash_treatment'}:
                allowed = {'order_policy': {'explicit', 'reorder'}, 'backlog_policy': {'lost_sales', 'carry'}, 'backlog_reservation_overlap': {'included', 'additional'}, 'demand_cash_treatment': {'incremental', 'already_recorded'}, 'purchase_cash_treatment': {'incremental', 'replace_linked', 'already_recorded'}}
                if value not in allowed[name]:
                    raise ValueError(f'Choose an explicit supported {name}.')
            elif name == 'forecast_overlap':
                if value not in {'replacement', 'incremental'}:
                    raise ValueError('forecast_overlap must be replacement or incremental.')
            elif name in {'payroll_dates', 'paused_supplier_ids', 'spiral_product_ids', 'disputed_record_ids'}:
                if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
                    raise ValueError(f'{name} must be a list of nonempty strings.')
                if name == 'payroll_dates':
                    for date_value in value:
                        day(date_value, f'{name} entry')
            else:
                minimum = None if name in {'cash_opening_estimate', 'collection_delay_days'} else 0
                numeric(value, name, minimum)
                if name in {'lead_time_days', 'collection_delay_days', 'season_length_days', 'invoice_delay_days', 'poison_supplier_balance_days', 'poison_customer_days', 'liquidation_days', 'payroll_buffer_days', 'spiral_restock_penalty_days', 'dispute_resolution_days'}:
                    if int(value) != value:
                        raise ValueError(f'{name} must contain whole calendar days.')
                if name in {'service_target', 'discount_percent', 'poison_margin_pct', 'poison_supplier_advance_pct', 'liquidation_discount_pct', 'dispute_recovery_pct'} and value > 100:
                    raise ValueError(f'{name} must be between 0 and 100.')
                if name == 'unpaid_share' and value > 1:
                    raise ValueError('unpaid_share must be between 0 and 1.')
                if name == 'season_length_days' and not 1 <= value <= 365:
                    raise ValueError('season_length_days must be between 1 and 365.')
                if name == 'dio_threshold' and value < 1:
                    raise ValueError('dio_threshold must be at least 1 day.')
        return values


class DefinitionCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=160)
    kind: Literal['forecast', 'simulation']
    config: AnalysisConfig


class DefinitionPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str | None = Field(default=None, min_length=1, max_length=160)
    config: AnalysisConfig | None = None
    archived: bool | None = None


class RunCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    snapshot: dict[str, Any]
    idempotency_key: str = Field(min_length=1, max_length=200)
    retry_of_run_id: str | None = None

    @field_validator('snapshot')
    @classmethod
    def snapshot_valid(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            size = len(json.dumps(value, allow_nan=False))
        except (ValueError, TypeError) as error:
            raise ValueError('Snapshot values must be finite JSON values.') from error
        if size > 8_000_000:
            raise ValueError('The local prototype supports snapshots up to 8 MB. Reduce the selected input scope.')
        return value


def dates(config: dict) -> list[str]:
    start = day(config['start_date'])
    return [(start + timedelta(days=i)).isoformat() for i in range(config['horizon_days'])]


def validate_workspace(snapshot: dict) -> None:
    if snapshot.get('version') != 1:
        raise InputError('Unsupported workspace version. Version 1 is required.')
    if snapshot.get('mode') not in {'business', 'demo'}:
        raise InputError('Workspace mode must be business or demo.')
    profile = snapshot.get('profile')
    if not isinstance(profile, dict):
        raise InputError('Provide the business profile and working currency.')
    currency = profile.get('currency')
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha() or currency != currency.upper():
        raise InputError('Provide a three-letter uppercase working currency.')
    try:
        ZoneInfo(profile.get('timezone', ''))
    except (ZoneInfoNotFoundError, ValueError, TypeError) as error:
        raise InputError('Provide a valid business timezone before running an analysis.') from error
    for key in ('products', 'locations', 'stock', 'sales', 'suppliers', 'purchases', 'finance', 'commitments', 'movements', 'sources'):
        records = snapshot.get(key)
        if not isinstance(records, list):
            raise InputError(f'Workspace {key} must be a list.')
        if len(records) > 20000:
            raise InputError(f'The local prototype supports at most 20,000 {key} records per submitted snapshot. Reduce the selected input scope.')
        ids = set()
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get('id'), str) or not record['id']:
                raise InputError(f'Each {key} record needs a stable identifier.')
            if record['id'] in ids:
                raise InputError(f'Duplicate {key} identifier {record["id"]}. Resolve it before submission.')
            ids.add(record['id'])
    if snapshot['mode'] == 'business' and any(s.get('type') == 'demo' for s in snapshot['sources']):
        raise InputError('Demo sources must remain in the separate demonstration workspace.')
    if not isinstance(snapshot.get('inventoryPools', []), list) or not all(isinstance(pool, dict) for pool in snapshot.get('inventoryPools', [])):
        raise InputError('Confirmed inventory pools must be a list of pool records.')
    pending = snapshot.get('pendingFinance', [])
    if not isinstance(pending, list) or len(pending) > 20000:
        raise InputError('Pending financial records must be a list with at most 20,000 records.')
    identifiers = {record['id'] for record in snapshot['finance']}
    for record in pending:
        if not isinstance(record, dict):
            raise InputError('Each pending financial record must be an identifiable record.')
        identifier = record.get('id')
        if not isinstance(identifier, str) or not identifier.strip() or identifier in identifiers:
            raise InputError('Pending financial records need unique stable identifiers, separate from confirmed financial records.')
        identifiers.add(identifier)
        if record.get('kind') not in {'receivable', 'payable'}:
            raise InputError('Pending financial records must identify a receivable or payable.')
        for field in ('name', 'counterparty', 'sourceId', 'currency'):
            if not isinstance(record.get(field), str):
                raise InputError(f'Pending financial {field} must be a string.')
        if not record['name'].strip() or not record['sourceId'].strip():
            raise InputError('Pending financial records need a name and source identifier.')
        if len(record['currency']) != 3 or not record['currency'].isalpha() or record['currency'] != record['currency'].upper():
            raise InputError('Pending financial currency must use a three-letter uppercase code.')
        for field in ('amount', 'paidAmount'):
            if record.get(field) is not None:
                numeric(record[field], f'Pending financial {field}', 0)
        if record.get('amount') is not None and record.get('paidAmount') is not None and record['paidAmount'] > record['amount']:
            raise InputError('A pending financial paid amount cannot exceed its supplied total.')
        for field in ('dueDate', 'expectedDate'):
            if record.get(field) is not None:
                day(record[field], f'Pending financial {field}')
    observations = snapshot.get('financeEvents', [])
    if not isinstance(observations, list) or len(observations) > 20000:
        raise InputError('Historical financial observations must be a list with at most 20,000 records.')
    financial_records = {record['id']: record for record in snapshot['finance']}
    observation_ids = set()
    observation_kinds = {'customer_collection': 'receivable', 'provider_availability': 'provider_pending', 'supplier_payment': 'payable'}
    for observation in observations:
        if not isinstance(observation, dict):
            raise InputError('Each historical financial observation must be a record.')
        for field in ('id', 'sourceId', 'recordId', 'paymentReference', 'currency'):
            if not isinstance(observation.get(field), str) or not observation[field].strip():
                raise InputError(f'Historical financial observation {field} must be a nonempty string.')
        if observation['id'] in observation_ids:
            raise InputError('Historical financial observation identifiers must be unique.')
        observation_ids.add(observation['id'])
        kind = observation.get('kind')
        record = financial_records.get(observation['recordId'])
        if kind not in observation_kinds or not record or record.get('kind') != observation_kinds[kind]:
            raise InputError('Historical financial observations must link to a confirmed financial record of the matching payment stage.')
        currency = observation['currency']
        if len(currency) != 3 or not currency.isalpha() or currency != currency.upper() or currency != record.get('currency'):
            raise InputError('Historical financial observation currency must match its linked financial record.')
        numeric(observation.get('amount'), 'Historical financial observation amount', 0)
        day(observation.get('date'), 'Historical financial observation date')
