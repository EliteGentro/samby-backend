from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import secrets
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from psycopg import Error as PostgresError

from app.prototype.engine import execute
from app.prototype.main import create_app
from app.prototype.schema import InputError
from app.prototype.store import ResourceError, Store
from conftest import event, inventory_config


def definition(store, workspace, config, kind='simulation'):
    return store.create_definition(workspace['id'], {'name': 'Saved scenario', 'kind': kind, 'config': config})


def submit(store, workspace, saved, key='submit1', retry=None):
    return store.submit(workspace['id'], saved['id'], {'snapshot': workspace, 'idempotency_key': key, 'retry_of_run_id': retry})


def complete_next(store):
    claimed = store.claim('test-worker')
    assert claimed is not None
    namespace, run = claimed
    dependency = store.get_run(namespace, run['config']['forecast_run_id']) if run['config'].get('forecast_run_id') else None
    baseline = store.get_run(namespace, run['config']['baseline_run_id']) if run['config'].get('baseline_run_id') else None
    result = execute(run['kind'], run['config'], run['snapshot'], run['id'], dependency, baseline)
    return store.finish(namespace, run['id'], 'test-worker', result)


def test_identical_concurrent_submissions_return_one_durable_run(postgres_url, workspace, config):
    store = Store(postgres_url)
    workspace['finance'] = [event('payment', 10, '2026-09-12')]
    saved = definition(store, workspace, config)
    with ThreadPoolExecutor(max_workers=5) as pool:
        runs = list(pool.map(lambda _: submit(store, workspace, saved), range(5)))
    assert len({run['id'] for run in runs}) == 1
    assert len(store.list_runs(workspace['id'])) == 1
    changed = deepcopy(workspace)
    changed['cash']['amount'] = 200
    with pytest.raises(ResourceError) as caught:
        submit(store, changed, saved)
    assert caught.value.status == 409
    assert complete_next(store)['result']['series'][-1]['cash'] == 90


def test_snapshot_and_prior_results_survive_definition_edits_and_restart(postgres_url, workspace, config):
    path = postgres_url
    store = Store(path)
    workspace['finance'] = [event('payment', 10, '2026-09-12')]
    saved = definition(store, workspace, config)
    original = submit(store, workspace, saved)
    completed = complete_next(store)
    workspace['cash']['amount'] = 900
    updated_config = {**config, 'horizon_days': 2}
    store.patch_definition(workspace['id'], saved['id'], {'name': 'Edited', 'config': updated_config})
    store = Store(path)
    retrieved = store.get_run(workspace['id'], original['id'])
    assert retrieved['definition_name'] == 'Saved scenario'
    assert retrieved['config']['horizon_days'] == 3
    assert retrieved['snapshot']['cash']['amount'] == 100
    assert retrieved['result'] == completed['result']
    assert retrieved['result']['series'][-1]['cash'] == 90
    assert store.get_definition(workspace['id'], saved['id'])['version'] == 2
    rerun = submit(store, workspace, saved, key='new-run', retry=original['id'])
    assert rerun['retry_of_run_id'] == original['id']
    assert rerun['id'] != original['id']
    assert rerun['snapshot']['cash']['amount'] == 900


def test_database_guards_keep_snapshots_and_artifacts_immutable(postgres_url, workspace, config):
    store = Store(postgres_url)
    workspace['finance'] = [event('payment', 10, '2026-09-12')]
    run = submit(store, workspace, definition(store, workspace, config))
    complete_next(store)
    with pytest.raises(PostgresError, match='snapshots are immutable'):
        with store.connection() as db:
            db.execute("UPDATE snapshots SET workspace='{}'")
    with pytest.raises(PostgresError, match='artifacts are immutable'):
        with store.connection() as db:
            db.execute("UPDATE artifacts SET result='{}'")
    with pytest.raises(PostgresError, match='Terminal run state'):
        with store.connection() as db:
            db.execute("UPDATE runs SET status='queued' WHERE id=?", (run['id'],))
    assert store.cancel(workspace['id'], run['id'])['status'] == 'succeeded'
    assert store.get_run(workspace['id'], run['id'])['result']['series'][-1]['cash'] == 90


def test_archive_is_retrievable_and_does_not_cancel_work(postgres_url, workspace, config):
    store = Store(postgres_url)
    workspace['finance'] = [event('payment', 10, '2026-09-12')]
    run = submit(store, workspace, definition(store, workspace, config))
    assert store.archive(workspace['id'], run['id'], True)['status'] == 'queued'
    assert store.list_runs(workspace['id']) == []
    assert store.list_runs(workspace['id'], archived=True)[0]['id'] == run['id']
    assert complete_next(store)['status'] == 'succeeded'
    assert store.get_run(workspace['id'], run['id'])['result']['series'][-1]['cash'] == 90
    assert store.archive(workspace['id'], run['id'], False)['archived'] is False


def test_forecast_dependency_waits_then_uses_pinned_daily_values(postgres_url, workspace, config):
    store = Store(postgres_url)
    forecast_config = {**config, 'product_id': 'p1', 'output_families': []}
    forecast_definition = definition(store, workspace, forecast_config, 'forecast')
    forecast = submit(store, workspace, forecast_definition, 'forecast')
    cfg = inventory_config(config)
    cfg['assumptions'].pop('daily_demand')
    cfg['forecast_run_id'] = forecast['id']
    simulation = submit(store, workspace, definition(store, workspace, cfg), 'simulation')
    assert simulation['status'] == 'waiting_for_dependency'
    assert simulation['result'] is None
    workspace['sales'][0]['quantity'] = 1000
    assert complete_next(store)['kind'] == 'forecast'
    store.maintain()
    result = complete_next(store)
    assert result['kind'] == 'simulation'
    assert [point['demand'] for point in result['result']['series']] == [3, 3, 3]
    assert [point['inventory'] for point in result['result']['series']] == [7, 4, 1]
    assert result['config']['forecast_run_id'] == forecast['id']


@pytest.mark.parametrize('dependency_status', ['failed', 'cancelled'])
def test_dependency_failure_is_persistent_without_fake_results(postgres_url, workspace, config, dependency_status):
    store = Store(postgres_url)
    forecast = submit(store, workspace, definition(store, workspace, {**config, 'product_id': 'p1'}, 'forecast'), 'forecast')
    cfg = inventory_config(config)
    cfg['forecast_run_id'] = forecast['id']
    simulation = submit(store, workspace, definition(store, workspace, cfg), 'simulation')
    if dependency_status == 'cancelled':
        store.cancel(workspace['id'], forecast['id'])
    else:
        store.claim('failed-worker')
        store.finish(workspace['id'], forecast['id'], 'failed-worker', error='Test worker failed while computing saved inputs.')
    store.maintain()
    result = Store(postgres_url).get_run(workspace['id'], simulation['id'])
    assert result['status'] == 'failed'
    assert dependency_status in result['error']
    assert result['result'] is None


def test_cancel_wins_against_late_worker_finalization(postgres_url, workspace, config):
    store = Store(postgres_url)
    workspace['finance'] = [event('payment', 10, '2026-09-12')]
    run = submit(store, workspace, definition(store, workspace, config))
    store.claim('worker1')
    computed = execute('simulation', config, workspace, run['id'])
    store.cancel(workspace['id'], run['id'])
    late = store.finish(workspace['id'], run['id'], 'worker1', computed)
    assert late['status'] == 'cancelled'
    assert late['result'] is None
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) AS count FROM artifacts').fetchone()['count'] == 0


def test_expired_worker_recovers_same_inputs_and_records_attempts(postgres_url, workspace, config):
    store = Store(postgres_url)
    workspace['finance'] = [event('payment', 10, '2026-09-12')]
    run = submit(store, workspace, definition(store, workspace, config))
    store.claim('lost-worker')
    with store.transaction() as db:
        db.execute("UPDATE runs SET lease_until='1970-01-01T00:00:00+00:00' WHERE id=?", (run['id'],))
    store = Store(postgres_url)
    store.maintain()
    assert store.get_run(workspace['id'], run['id'])['status'] == 'queued'
    completed = complete_next(store)
    assert completed['id'] == run['id']
    assert completed['attempt'] == 2
    assert completed['snapshot'] == workspace
    assert completed['result']['series'][-1]['cash'] == 90
    assert [t['status'] for t in store.transitions(workspace['id'], run['id'])] == ['queued', 'running', 'queued', 'running', 'succeeded']


def test_repeated_worker_loss_becomes_actionable_failure(postgres_url, workspace, config):
    store = Store(postgres_url)
    workspace['finance'] = [event('payment', 10, '2026-09-12')]
    run = submit(store, workspace, definition(store, workspace, config))
    for index in range(3):
        assert store.claim(f'lost{index}') is not None
        with store.transaction() as db:
            db.execute("UPDATE runs SET lease_until='1970-01-01T00:00:00+00:00' WHERE id=?", (run['id'],))
        store.maintain()
    failed = store.get_run(workspace['id'], run['id'])
    assert failed['status'] == 'failed'
    assert failed['attempt'] == 3
    assert 'Retry this run' in failed['error']
    assert failed['result'] is None


def test_namespace_and_mode_isolation(postgres_url, workspace, config):
    store = Store(postgres_url)
    workspace['finance'] = [event('payment', 10, '2026-09-12')]
    saved = definition(store, workspace, config)
    run = submit(store, workspace, saved)
    other = deepcopy(workspace)
    other['id'] = str(uuid4())
    assert store.list_runs(other['id']) == []
    with pytest.raises(ResourceError) as missing:
        store.get_run(other['id'], run['id'])
    assert missing.value.status == 404
    with pytest.raises(ResourceError) as missing:
        store.get_definition(other['id'], saved['id'])
    assert missing.value.status == 404
    demo = deepcopy(workspace)
    demo['mode'] = 'demo'
    with pytest.raises(ResourceError, match='separate workspace'):
        submit(store, demo, saved, key='demo')
    second = submit(store, other, definition(store, other, config))
    assert second['id'] != run['id']


def test_comparison_pins_completed_baseline_after_newer_runs(postgres_url, workspace, config):
    store = Store(postgres_url)
    cfg = inventory_config(config, daily_demand=0)
    saved = definition(store, workspace, cfg)
    baseline = submit(store, workspace, saved, 'baseline')
    complete_next(store)
    changed = inventory_config(config, daily_demand=10)
    changed['baseline_run_id'] = baseline['id']
    alternate = submit(store, workspace, definition(store, workspace, changed), 'alternate')
    store.patch_definition(workspace['id'], saved['id'], {'config': inventory_config(config, daily_demand=100)})
    complete = complete_next(store)
    assert complete['id'] == alternate['id']
    assert complete['result']['comparison']['baseline_run_id'] == baseline['id']
    delta = next(m for m in complete['result']['comparison']['metrics'] if m['key'] == 'unmet_demand')
    assert delta['baseline'] == 0
    assert delta['alternative'] == 20


def test_api_reports_async_states_resources_and_validation_strings(postgres_url, workspace, config):
    workspace['finance'] = [event('payment', 10, '2026-09-12')]
    app = create_app(postgres_url, start_worker=False)
    headers = {'X-Workspace-ID': workspace['id'], 'X-Workspace-Key': secrets.token_urlsafe(32)}
    with TestClient(app) as client:
        assert client.post('/api/prototype/workspaces/guest', headers=headers, json={'workspace': workspace}).status_code == 201
        response = client.post('/api/prototype/definitions', headers=headers, json={'name': 'Cash plan', 'kind': 'simulation', 'config': config})
        assert response.status_code == 201
        saved = response.json()
        response = client.post(f'/api/prototype/definitions/{saved["id"]}/runs', headers=headers, json={'snapshot': workspace, 'idempotency_key': 'api'})
        assert response.status_code == 202
        run = response.json()
        assert run['status'] == 'queued'
        assert run['result'] is None
        for resource in ('results', 'series', 'events', 'scene-manifest'):
            response = client.get(f'/api/prototype/runs/{run["id"]}/{resource}', headers=headers)
            assert response.status_code == 202
            assert response.json()['status'] == 'queued'
            assert 'series' not in response.json()
        complete_next(app.state.store)
        assert client.get(f'/api/prototype/runs/{run["id"]}/series', headers=headers).json()[-1]['cash'] == 90
        assert client.get(f'/api/prototype/runs/{run["id"]}/results', headers=headers).json()['end_date'] == '2026-09-14'
        assert client.get('/api/prototype/runs', headers=headers).json()[0]['result']['series'][-1]['cash'] == 90
        assert client.patch(f'/api/prototype/runs/{run["id"]}', headers=headers, json={'archived': True}).json()['archived'] is True
        assert client.get('/api/prototype/runs', headers=headers).json() == []
        assert len(client.get('/api/prototype/runs?archived=true', headers=headers).json()) == 1
        assert client.get(f'/api/prototype/runs/{run["id"]}', headers={**headers, 'X-Workspace-ID': str(uuid4())}).status_code == 404
        invalid = client.post('/api/prototype/definitions', headers=headers, json={'name': 'Bad dates', 'kind': 'simulation', 'config': {**config, 'horizon_days': 366}})
        assert invalid.status_code == 422
        assert isinstance(invalid.json()['detail'], str)
        assert client.get('/api/prototype/runs').status_code == 422


def test_autonomous_worker_completes_without_status_polling_and_history_reopens(postgres_url, workspace, config):
    path = postgres_url
    workspace['finance'] = [event('payment', 10, '2026-09-12')]
    headers = {'X-Workspace-ID': workspace['id'], 'X-Workspace-Key': secrets.token_urlsafe(32)}
    with TestClient(create_app(path)) as client:
        assert client.post('/api/prototype/workspaces/guest', headers=headers, json={'workspace': workspace}).status_code == 201
        saved = client.post('/api/prototype/definitions', headers=headers, json={'name': 'Autonomous', 'kind': 'simulation', 'config': config}).json()
        accepted = client.post(f'/api/prototype/definitions/{saved["id"]}/runs', headers=headers, json={'snapshot': workspace, 'idempotency_key': 'worker'}).json()
        assert accepted['status'] == 'queued'
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with client.app.state.store.connection() as db:
                status = db.execute('SELECT status FROM runs WHERE id=?', (accepted['id'],)).fetchone()['status']
            if status == 'succeeded':
                break
            time.sleep(0.01)
        assert status == 'succeeded'
    with TestClient(create_app(path)) as restarted:
        recovered = restarted.get(f'/api/prototype/runs/{accepted["id"]}', headers=headers).json()
        assert recovered['status'] == 'succeeded'
        assert recovered['result']['series'][-1]['cash'] == 90
        assert recovered['attempt'] == 1


def test_api_unsupported_user_data_creates_no_fixture_run(postgres_url, workspace, config):
    config.update({'engine': 'lightgbm', 'product_id': 'p1'})
    headers = {'X-Workspace-ID': workspace['id'], 'X-Workspace-Key': secrets.token_urlsafe(32)}
    with TestClient(create_app(postgres_url, start_worker=False)) as client:
        assert client.post('/api/prototype/workspaces/guest', headers=headers, json={'workspace': workspace}).status_code == 201
        saved = client.post('/api/prototype/definitions', headers=headers, json={'name': 'Unsupported model', 'kind': 'forecast', 'config': config}).json()
        response = client.post(f'/api/prototype/definitions/{saved["id"]}/runs', headers=headers, json={'snapshot': workspace, 'idempotency_key': 'unsupported'})
        assert response.status_code == 422
        assert '56 consecutive' in response.json()['detail']
        assert client.get('/api/prototype/runs', headers=headers).json() == []


def test_local_and_configured_frontend_origins_receive_cors_permission(postgres_url, monkeypatch):
    from app.core.config import get_settings

    hosted_origin = 'https://samby.example.com'
    monkeypatch.setenv('FRONTEND_ORIGIN', f'{hosted_origin}/')
    get_settings.cache_clear()
    try:
        with TestClient(create_app(postgres_url, start_worker=False)) as client:
            local = client.options('/api/prototype/runs', headers={'Origin': 'http://127.0.0.1:5173', 'Access-Control-Request-Method': 'GET', 'Access-Control-Request-Headers': 'x-workspace-id'})
            assert local.headers['access-control-allow-origin'] == 'http://127.0.0.1:5173'
            hosted = client.options('/api/prototype/runs', headers={'Origin': hosted_origin, 'Access-Control-Request-Method': 'GET', 'Access-Control-Request-Headers': 'x-workspace-id'})
            assert hosted.headers['access-control-allow-origin'] == hosted_origin
            bad = client.options('/api/prototype/runs', headers={'Origin': 'https://example.com', 'Access-Control-Request-Method': 'GET'})
            assert 'access-control-allow-origin' not in bad.headers
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize('failure', ['exception', 'timeout'])
def test_worker_failure_and_timeout_end_in_persistent_failure(postgres_url, workspace, config, monkeypatch, failure):
    import app.prototype.main as service

    def faulty_engine(*_args):
        if failure == 'timeout':
            time.sleep(0.08)
            return {}
        raise RuntimeError('injected engine failure')

    monkeypatch.setattr(service, 'execute', faulty_engine)
    workspace['finance'] = [event('payment', 10, '2026-09-12')]
    headers = {'X-Workspace-ID': workspace['id'], 'X-Workspace-Key': secrets.token_urlsafe(32)}
    path = postgres_url
    with TestClient(create_app(path, poll_seconds=0.005, timeout_seconds=0.01)) as client:
        assert client.post('/api/prototype/workspaces/guest', headers=headers, json={'workspace': workspace}).status_code == 201
        saved = client.post('/api/prototype/definitions', headers=headers, json={'name': 'Failure test', 'kind': 'simulation', 'config': config}).json()
        accepted = client.post(f'/api/prototype/definitions/{saved["id"]}/runs', headers=headers, json={'snapshot': workspace, 'idempotency_key': 'fail'}).json()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            run = client.get(f'/api/prototype/runs/{accepted["id"]}', headers=headers).json()
            if run['status'] == 'failed':
                break
            time.sleep(0.01)
        assert run['status'] == 'failed'
        assert run['result'] is None
        assert 'time limit' in run['error'] if failure == 'timeout' else 'unexpected error' in run['error']
    retained = Store(path).get_run(workspace['id'], accepted['id'])
    assert retained['status'] == 'failed'
    assert retained['snapshot']['cash']['amount'] == 100


def test_pool_membership_cannot_change_under_a_pinned_forecast(postgres_url, workspace, config):
    store = Store(postgres_url)
    workspace['locations'] = [{'id': 'a'}, {'id': 'b'}]
    workspace['inventoryPools'] = [{'id': 'pool', 'locationIds': ['a']}]
    workspace['sales'][0]['locationId'] = 'a'
    workspace['stock'][0]['locationId'] = 'a'
    cfg = {**config, 'product_id': 'p1', 'inventory_pool_id': 'pool'}
    forecast = submit(store, workspace, definition(store, workspace, cfg, 'forecast'), 'forecast')
    workspace['inventoryPools'][0]['locationIds'] = ['a', 'b']
    cfg = inventory_config(config)
    cfg.update({'inventory_pool_id': 'pool', 'forecast_run_id': forecast['id']})
    with pytest.raises(InputError, match='scope differs'):
        submit(store, workspace, definition(store, workspace, cfg), 'changed-pool')


@pytest.mark.parametrize('link', ['baseline', 'forecast'])
def test_converted_quantity_unit_cannot_reuse_old_analytical_inputs(postgres_url, workspace, config, link):
    store = Store(postgres_url)
    original_config = inventory_config(config) if link == 'baseline' else {**config, 'product_id': 'p1', 'output_families': []}
    original = submit(store, workspace, definition(store, workspace, original_config, 'simulation' if link == 'baseline' else 'forecast'), 'original')
    complete_next(store)
    converted = deepcopy(workspace)
    converted['products'][0]['unit'] = 'case'
    scenario = inventory_config(config)
    scenario['baseline_run_id' if link == 'baseline' else 'forecast_run_id'] = original['id']
    saved = definition(store, converted, scenario)
    with pytest.raises(InputError, match='quantity unit'):
        submit(store, converted, saved, 'converted')
    assert len(store.list_runs(workspace['id'])) == 1
    assert store.get_run(workspace['id'], original['id'])['snapshot']['products'][0]['unit'] == workspace['products'][0]['unit']
