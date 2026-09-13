from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.core.config import get_settings
from .engine import VERSION, prepare, scope
from .schema import InputError, validate_workspace


TERMINAL = {'succeeded', 'failed', 'cancelled'}


class ResourceError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dump(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


class Database:
    """Small compatibility adapter while repository SQL uses positional placeholders."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, statement: str, parameters=()):
        return self._connection.execute(statement.replace('?', '%s'), parameters)


def psycopg_url(database_url: str) -> str:
    return database_url.replace('postgresql+psycopg://', 'postgresql://', 1)


class Store:
    def __init__(self, database_url: str | None = None):
        settings = get_settings()
        self.database_url = database_url or settings.database_url
        self.pool = ConnectionPool(
            conninfo=psycopg_url(self.database_url),
            min_size=0,
            max_size=settings.database_pool_max_connections,
            timeout=settings.database_connect_timeout_seconds,
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
            open=True,
        )

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def connection(self):
        with self.pool.connection() as connection:
            yield Database(connection)

    @contextmanager
    def transaction(self):
        with self.pool.connection() as connection:
            with connection.transaction():
                db = Database(connection)
                yield db

    def _definition(self, db, namespace: str, definition_id: str) -> dict:
        row = db.execute('SELECT * FROM definitions WHERE id=? AND namespace=?', (definition_id, namespace)).fetchone()
        if row is None:
            raise ResourceError(404, 'Definition was not found in this workspace.')
        return {'id': row['id'], 'name': row['name'], 'kind': row['kind'], 'config': json.loads(row['config']), 'version': row['version'], 'archived': bool(row['archived']), 'created_at': row['created_at'], 'updated_at': row['updated_at']}

    def create_definition(self, namespace: str, body: dict) -> dict:
        definition_id, timestamp = str(uuid4()), now()
        with self.transaction() as db:
            db.execute('INSERT INTO namespaces(id) VALUES(?) ON CONFLICT(id) DO NOTHING', (namespace,))
            db.execute('INSERT INTO definitions(id,namespace,name,kind,config,version,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?)', (definition_id, namespace, body['name'], body['kind'], dump(body['config']), timestamp, timestamp))
            return self._definition(db, namespace, definition_id)

    def get_definition(self, namespace: str, definition_id: str) -> dict:
        with self.connection() as db:
            return self._definition(db, namespace, definition_id)

    def list_definitions(self, namespace: str, include_archived: bool = False, archived: bool = False) -> list[dict]:
        with self.connection() as db:
            rows = db.execute('SELECT id,archived FROM definitions WHERE namespace=? ORDER BY created_at DESC,id DESC', (namespace,)).fetchall()
            return [self._definition(db, namespace, row['id']) for row in rows if include_archived or bool(row['archived']) == archived]

    def patch_definition(self, namespace: str, definition_id: str, body: dict) -> dict:
        with self.transaction() as db:
            current = self._definition(db, namespace, definition_id)
            name = body.get('name') if body.get('name') is not None else current['name']
            config = body.get('config') if body.get('config') is not None else current['config']
            archived = body.get('archived') if body.get('archived') is not None else current['archived']
            version = current['version'] + (config != current['config'] or name != current['name'])
            db.execute('UPDATE definitions SET name=?, config=?, archived=?, version=?, updated_at=? WHERE id=?', (name, dump(config), int(archived), version, now(), definition_id))
            return self._definition(db, namespace, definition_id)

    def _run(self, db, namespace: str, run_id: str, with_result: bool = True) -> dict:
        row = db.execute('SELECT r.*,s.config,s.workspace,s.definition_version FROM runs r JOIN snapshots s ON r.snapshot_id=s.id WHERE r.id=? AND r.namespace=?', (run_id, namespace)).fetchone()
        if row is None:
            raise ResourceError(404, 'Run was not found in this workspace.')
        result = None
        if row['status'] == 'succeeded' and with_result:
            artifact = db.execute('SELECT result FROM artifacts WHERE run_id=?', (run_id,)).fetchone()
            result = json.loads(artifact['result']) if artifact else None
        return {**{key: row[key] for key in ('id', 'definition_id', 'definition_name', 'kind', 'status', 'phase', 'created_at', 'updated_at', 'started_at', 'completed_at', 'retry_of_run_id', 'attempt', 'definition_version')}, 'config': json.loads(row['config']), 'snapshot': json.loads(row['workspace']), 'warnings': json.loads(row['warnings']), 'error': row['error'], 'result': result, 'provenance': json.loads(row['provenance']), 'archived': bool(row['archived'])}

    def get_run(self, namespace: str, run_id: str) -> dict:
        with self.connection() as db:
            return self._run(db, namespace, run_id)

    def list_runs(self, namespace: str, kind: str | None = None, status: str | None = None, question: str | None = None, include_archived: bool = False, archived: bool = False) -> list[dict]:
        with self.connection() as db:
            rows = db.execute('SELECT id,kind,status,archived FROM runs WHERE namespace=? ORDER BY created_at DESC,id DESC', (namespace,)).fetchall()
            runs = [self._run(db, namespace, row['id']) for row in rows if (not kind or row['kind'] == kind) and (not status or row['status'] == status) and (include_archived or bool(row['archived']) == archived)]
            return [run for run in runs if not question or run['config']['question'] == question]

    def submit(self, namespace: str, definition_id: str, body: dict) -> dict:
        fingerprint = hashlib.sha256(dump({'definition_id': definition_id, 'snapshot': body['snapshot'], 'retry_of_run_id': body.get('retry_of_run_id')}).encode()).hexdigest()
        with self.transaction() as db:
            db.execute('SELECT id FROM namespaces WHERE id=? FOR UPDATE', (namespace,)).fetchone()
            prior = db.execute('SELECT fingerprint,run_id FROM idempotency WHERE namespace=? AND key=?', (namespace, body['idempotency_key'])).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise ResourceError(409, 'This idempotency key already identifies different submitted inputs. Use a new key for a changed scenario.')
                return self._run(db, namespace, prior['run_id'])
            definition = self._definition(db, namespace, definition_id)
            if definition['archived']:
                raise ResourceError(409, 'Unarchive the definition before starting a new run. Previous runs remain readable.')
            snapshot = body['snapshot']
            if snapshot.get('id') != namespace:
                raise InputError('The snapshot workspace ID must match X-Workspace-ID.')
            validate_workspace(snapshot)
            namespace_row = db.execute('SELECT mode FROM namespaces WHERE id=?', (namespace,)).fetchone()
            if namespace_row['mode'] and namespace_row['mode'] != snapshot.get('mode'):
                raise ResourceError(409, 'Business and demonstration data require separate workspace identities.')
            if body.get('retry_of_run_id'):
                retry = self._run(db, namespace, body['retry_of_run_id'])
                if retry['kind'] != definition['kind']:
                    raise InputError('A retry must preserve the original run kind.')
            config = definition['config']
            dependency, baseline = None, None
            quantity_unit = next((product.get('unit') for product in snapshot['products'] if product['id'] == config.get('product_id')), None)
            dependency_id = config.get('forecast_run_id')
            if dependency_id:
                if definition['kind'] != 'simulation':
                    raise InputError('Only simulations can depend on a forecast run.')
                dependency = self._run(db, namespace, dependency_id)
                if dependency['kind'] != 'forecast':
                    raise InputError('forecast_run_id must identify a forecast run.')
                if dependency['status'] in {'failed', 'cancelled'}:
                    raise InputError('The selected forecast failed or was cancelled. Select a usable dependency before submitting.')
                if scope(config, snapshot) != scope(dependency['config'], dependency['snapshot']):
                    raise InputError('The selected forecast scope differs from the scenario scope.')
                if dependency['provenance']['currency'] != snapshot.get('profile', {}).get('currency'):
                    raise InputError('The selected forecast currency differs from the scenario currency.')
                dependency_unit = next((product.get('unit') for product in dependency['snapshot']['products'] if product['id'] == dependency['config'].get('product_id')), None)
                if quantity_unit != dependency_unit:
                    raise InputError('The selected forecast quantity unit differs from the scenario. Run a new forecast after a unit conversion, or use the original snapshot.')
            if config.get('baseline_run_id'):
                baseline = self._run(db, namespace, config['baseline_run_id'])
                if baseline['status'] != 'succeeded' or baseline['kind'] != definition['kind']:
                    raise InputError('A baseline must be a succeeded run of the same analytical kind.')
                if any(config.get(k) != baseline['config'].get(k) for k in ('start_date', 'horizon_days', 'product_id', 'location_id')) or set(config.get('output_families', [])) != set(baseline['config'].get('output_families', [])):
                    raise InputError('The baseline must use compatible scope, dates, daily cadence and result families.')
                if scope(config, snapshot) != scope(baseline['config'], baseline['snapshot']):
                    raise InputError('The baseline and scenario must preserve the same confirmed inventory-pool membership.')
                if baseline['provenance']['currency'] != snapshot.get('profile', {}).get('currency'):
                    raise InputError('The comparison currency must match the pinned baseline.')
                baseline_unit = next((product.get('unit') for product in baseline['snapshot']['products'] if product['id'] == baseline['config'].get('product_id')), None)
                if quantity_unit != baseline_unit:
                    raise InputError('The comparison quantity unit must match the pinned baseline. Use a compatible baseline or the original snapshot after a unit conversion.')
            pending = dependency is not None and dependency['status'] != 'succeeded'
            prepared = prepare(definition['kind'], config, snapshot, dependency if not pending else None, pending)
            run_id, snapshot_id, timestamp = str(uuid4()), str(uuid4()), now()
            provenance = {'mode': snapshot['mode'] if snapshot['mode'] == 'demo' else 'computed', 'engine': config['engine'] if definition['kind'] == 'forecast' else 'deterministic-daily-simulation', 'engine_version': VERSION, 'currency': snapshot['profile']['currency'], 'snapshot_at': timestamp, 'scope': scope(config, snapshot)}
            status = 'waiting_for_dependency' if pending else 'queued'
            db.execute('UPDATE namespaces SET mode=? WHERE id=?', (snapshot['mode'], namespace))
            db.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?)', (snapshot_id, namespace, definition['version'], dump(config), dump(snapshot), hashlib.sha256(dump(snapshot).encode()).hexdigest()))
            db.execute('INSERT INTO runs(id,namespace,definition_id,definition_name,kind,snapshot_id,status,phase,created_at,updated_at,warnings,retry_of_run_id,provenance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (run_id, namespace, definition_id, definition['name'], definition['kind'], snapshot_id, status, 'Waiting for pinned forecast' if pending else 'Queued for the local worker', timestamp, timestamp, dump(prepared['warnings']), body.get('retry_of_run_id'), dump(provenance)))
            db.execute('INSERT INTO idempotency VALUES(?,?,?,?)', (namespace, body['idempotency_key'], fingerprint, run_id))
            db.execute('INSERT INTO transitions(run_id,status,at,detail) VALUES(?,?,?,?)', (run_id, status, timestamp, 'Input snapshot captured before accepting the run.'))
            return self._run(db, namespace, run_id)

    def _transition(self, db, run_id: str, previous: str, status: str, phase: str, error: str | None = None) -> None:
        timestamp = now()
        db.execute('UPDATE runs SET status=?,phase=?,updated_at=?,completed_at=?,error=? WHERE id=?', (status, phase, timestamp, timestamp if status in TERMINAL else None, error, run_id))
        db.execute('INSERT INTO transitions(run_id,previous_status,status,at,detail) VALUES(?,?,?,?,?)', (run_id, previous, status, timestamp, error or phase))

    def cancel(self, namespace: str, run_id: str) -> dict:
        with self.transaction() as db:
            db.execute('SELECT id FROM runs WHERE id=? AND namespace=? FOR UPDATE', (run_id, namespace)).fetchone()
            current = self._run(db, namespace, run_id)
            if current['status'] not in TERMINAL:
                self._transition(db, run_id, current['status'], 'cancelled', 'Cancelled by the owner')
            return self._run(db, namespace, run_id)

    def archive(self, namespace: str, run_id: str, archived: bool) -> dict:
        with self.transaction() as db:
            db.execute('SELECT id FROM runs WHERE id=? AND namespace=? FOR UPDATE', (run_id, namespace)).fetchone()
            self._run(db, namespace, run_id)
            db.execute('UPDATE runs SET archived=? WHERE id=?', (int(archived), run_id))
            return self._run(db, namespace, run_id)

    def maintain(self, max_attempts: int = 3) -> None:
        with self.transaction() as db:
            rows = db.execute("SELECT id,status,attempt FROM runs WHERE status='running' AND lease_until<? FOR UPDATE SKIP LOCKED", (now(),)).fetchall()
            for row in rows:
                failed = row['attempt'] >= max_attempts
                self._transition(db, row['id'], 'running', 'failed' if failed else 'queued', 'Worker recovery exhausted' if failed else 'Recovered unchanged inputs after an expired worker lease', 'The worker stopped repeatedly. Retry this run after checking the local service.' if failed else None)
            waiting = db.execute("SELECT r.id,r.namespace,s.config FROM runs r JOIN snapshots s ON r.snapshot_id=s.id WHERE r.status='waiting_for_dependency' FOR UPDATE OF r SKIP LOCKED").fetchall()
            for row in waiting:
                dependency = self._run(db, row['namespace'], json.loads(row['config'])['forecast_run_id'])
                if dependency['status'] == 'succeeded':
                    self._transition(db, row['id'], 'waiting_for_dependency', 'queued', 'Pinned forecast succeeded; queued for simulation')
                elif dependency['status'] in {'failed', 'cancelled'}:
                    self._transition(db, row['id'], 'waiting_for_dependency', 'failed', 'Pinned forecast dependency failed', f'Forecast {dependency["id"]} {dependency["status"]}. Save a new scenario dependency and retry as a new run.')

    def claim(self, worker_id: str, lease_seconds: float = 30) -> tuple[str, dict] | None:
        with self.transaction() as db:
            row = db.execute("SELECT id,namespace FROM runs WHERE status='queued' ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED").fetchone()
            if row is None:
                return None
            self._transition(db, row['id'], 'queued', 'running', 'Computing saved inputs')
            db.execute('UPDATE runs SET worker_id=?,lease_until=?,attempt=attempt+1,started_at=COALESCE(started_at,?) WHERE id=?', (worker_id, (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat(), now(), row['id']))
            return row['namespace'], self._run(db, row['namespace'], row['id'])

    def finish(self, namespace: str, run_id: str, worker_id: str, result: dict | None = None, error: str | None = None) -> dict:
        with self.transaction() as db:
            current = db.execute('SELECT status,worker_id FROM runs WHERE id=? AND namespace=? FOR UPDATE', (run_id, namespace)).fetchone()
            if current is None:
                raise ResourceError(404, 'Run was not found in this workspace.')
            if current['status'] == 'running' and current['worker_id'] == worker_id:
                if error:
                    self._transition(db, run_id, 'running', 'failed', 'Execution failed', error)
                else:
                    if result is None:
                        raise ValueError('A successful run requires persisted result artifacts.')
                    db.execute('INSERT INTO artifacts VALUES(?,?)', (run_id, dump(result)))
                    db.execute('UPDATE runs SET warnings=? WHERE id=?', (dump(result['warnings']), run_id))
                    self._transition(db, run_id, 'running', 'succeeded', 'Complete; immutable artifacts saved')
            return self._run(db, namespace, run_id)

    def release_worker(self, worker_id: str) -> None:
        with self.transaction() as db:
            rows = db.execute("SELECT id,attempt FROM runs WHERE status='running' AND worker_id=?", (worker_id,)).fetchall()
            for row in rows:
                if row['attempt'] >= 3:
                    self._transition(db, row['id'], 'running', 'failed', 'Worker recovery exhausted', 'Execution was interrupted three times. Check the service and retry as a new run.')
                else:
                    self._transition(db, row['id'], 'running', 'queued', 'Worker shutdown; unchanged inputs retained for restart')

    def transitions(self, namespace: str, run_id: str) -> list[dict]:
        with self.connection() as db:
            self._run(db, namespace, run_id, with_result=False)
            return [dict(row) for row in db.execute('SELECT previous_status,status,at,detail FROM transitions WHERE run_id=? ORDER BY id', (run_id,))]
