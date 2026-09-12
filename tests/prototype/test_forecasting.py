from copy import deepcopy

import pytest

from app.prototype.engine import execute, prepare
from app.prototype.forecasting import errors
from app.prototype.schema import InputError
from conftest import history


@pytest.mark.parametrize('engine', ['lightgbm', 'catboost'])
def test_real_model_trains_on_user_data_and_records_unseen_temporal_evaluation(workspace, config, engine):
    workspace['sales'] = history(84)
    config.update({'product_id': 'p1', 'engine': engine, 'horizon_days': 7})
    result = execute('forecast', config, workspace, 'real-model')
    diagnostics = result['forecast_diagnostics']
    assert diagnostics['engine'] == engine
    assert diagnostics['library_version']
    assert diagnostics['training_rows'] == 70
    assert diagnostics['evaluation_source'] == 'Submitted business observations'
    assert diagnostics['backtest']['training_end'] < diagnostics['backtest']['start_date']
    assert len(diagnostics['backtest']['series']) == 14
    assert len(result['history']) == 84
    assert all(point['demand'] >= 0 for point in result['series'])
    assert [point['demand'] for point in result['series']] != [18, 21, 24, 18, 21, 24, 18]
    repeat = execute('forecast', config, workspace, 'repeat')
    assert repeat['series'] == result['series']
    changed = deepcopy(workspace)
    for row in changed['sales']:
        row['quantity'] *= 2
    altered = execute('forecast', config, changed, 'changed')
    assert altered['series'] != result['series']


@pytest.mark.parametrize('engine', ['lightgbm', 'catboost'])
def test_holdout_targets_never_leak_into_backtest_predictions(workspace, config, engine):
    workspace['sales'] = history(70)
    config.update({'product_id': 'p1', 'engine': engine})
    before = execute('forecast', config, workspace, 'original')['forecast_diagnostics']['backtest']
    for row in workspace['sales'][-14:]:
        row['quantity'] += 1000
    after = execute('forecast', config, workspace, 'heldout-changed')['forecast_diagnostics']['backtest']
    assert [row['predicted'] for row in before['series']] == [row['predicted'] for row in after['series']]
    assert before['metrics'] != after['metrics']


def test_advanced_submission_validates_history_without_training_in_request_handler(workspace, config, monkeypatch):
    workspace['sales'] = history(56)
    config.update({'product_id': 'p1', 'engine': 'lightgbm'})
    monkeypatch.setattr('app.prototype.forecasting.fit_model', lambda *args: pytest.fail('Fitting must occur only in the worker'))
    assert prepare('forecast', config, workspace)['forecast'] == []
    workspace['sales'].pop(10)
    with pytest.raises(InputError, match='56 consecutive'):
        prepare('forecast', config, workspace)


def test_error_formulas_define_zero_denominator_and_positive_overforecast_bias():
    assert errors([0, 0], [1, 3]) == {'mae': 2, 'rmse': 2.236068, 'bias': 2, 'wape_percent': None}



def test_saved_history_flags_scoped_zero_stock_without_adjusting_observed_demand(workspace, config):
    from copy import deepcopy
    from app.prototype.engine import execute
    workspace['serviceObservations'] = [
        {'id': 'known-zero', 'productId': 'p1', 'date': '2026-09-11', 'locationId': None, 'unit': 'pieces', 'availableQuantity': 0, 'phase': 'closing', 'sourceId': 'source1'},
        {'id': 'other-product', 'productId': 'p2', 'date': '2026-09-11', 'locationId': None, 'unit': 'pieces', 'availableQuantity': 0, 'phase': 'opening', 'sourceId': 'source1'},
        {'id': 'unobserved', 'productId': 'p1', 'date': '2026-09-11', 'locationId': None, 'unit': 'pieces', 'availableQuantity': None, 'phase': 'closing', 'sourceId': 'source1'},
    ]
    config.update({'product_id': 'p1', 'output_families': [], 'engine': 'naive'})
    original = deepcopy(workspace)
    baseline = execute('forecast', config, {**workspace, 'serviceObservations': []}, 'unflagged')
    result = execute('forecast', config, workspace, 'flagged')
    assert result['series'] == baseline['series']
    assert result['history'] == [{'date': '2026-09-11', 'actual': 3, 'observed_zero_stock': 1}]
    assert result['forecast_diagnostics']['stockout_observations'] == [{'id': 'known-zero', 'date': '2026-09-11', 'phase': 'closing', 'location_id': None, 'source_id': 'source1'}]
    assert workspace == original
    workspace['serviceObservations'][0]['availableQuantity'] = 5
    assert result['history'][0]['observed_zero_stock'] == 1
