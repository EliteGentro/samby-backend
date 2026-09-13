import pytest
from app.prototype.engine import execute
from app.prototype.schema import AnalysisConfig
from conftest import event


def test_asem_stress_toggle_shifts_collection_by_76_days_and_warns(workspace, config):
    workspace['finance'] = [
        event('inv1', 100, '2026-09-12', 'receivable'),
    ]
    # Set question to Q-CUSTOMER-DEBT with asem_stress
    config.update({
        'question': 'Q-CUSTOMER-DEBT',
        'output_families': ['debt'],
        'horizon_days': 100,
        'assumptions': {
            'asem_stress': True,
            'collection_delay_days': 0,
        },
    })
    result = execute('simulation', config, workspace, 'run-asem')
    # The collection event should be shifted by 0 + 76 = 76 days
    collection_event = next(e for e in result['events'] if e['type'] == 'collection')
    assert collection_event['date'] == '2026-11-27'  # 2026-09-12 + 76 days
    assert any('ASEM stress applied (+76 days)' in w for w in result['warnings'])


def test_asem_stress_combines_with_empirical_p50_delay(workspace, config):
    workspace['finance'] = [
        event('inv1', 200, '2026-09-12', 'receivable'),
    ]
    # Empirical P50 delay of 10 days + ASEM stress of 76 days = 86 days
    config.update({
        'question': 'Q-CUSTOMER-DEBT',
        'output_families': ['debt'],
        'horizon_days': 100,
        'assumptions': {
            'collection_delay_days': 10,
            'asem_stress': True,
        },
    })
    result = execute('simulation', config, workspace, 'run-p50-asem')
    collection_event = next(e for e in result['events'] if e['type'] == 'collection')
    assert collection_event['date'] == '2026-12-07'  # 2026-09-12 + 86 days


def test_analysis_config_validates_asem_stress_boolean():
    valid = AnalysisConfig(
        engine='naive',
        question='Q-CUSTOMER-DEBT',
        start_date='2026-09-12',
        horizon_days=30,
        output_families=['debt'],
        assumptions={'asem_stress': True},
    )
    assert valid.assumptions['asem_stress'] is True

    with pytest.raises(Exception):
        AnalysisConfig(
            engine='naive',
            question='Q-CUSTOMER-DEBT',
            start_date='2026-09-12',
            horizon_days=30,
            output_families=['debt'],
            assumptions={'asem_stress': 'invalid'},
        )
