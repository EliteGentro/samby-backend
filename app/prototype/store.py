from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

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


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS namespaces (
                    id TEXT PRIMARY KEY, mode TEXT
                );
                CREATE TABLE IF NOT EXISTS definitions (
                    id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
                    name TEXT NOT NULL, kind TEXT NOT NULL, config TEXT NOT NULL,
                    version INTEGER NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS definition_namespace ON definitions(namespace, kind, archived);
                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY, namespace TEXT NOT NULL, definition_version INTEGER NOT NULL,
                    config TEXT NOT NULL, workspace TEXT NOT NULL, hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, namespace TEXT NOT NULL, definition_id TEXT NOT NULL,
                    definition_name TEXT NOT NULL, kind TEXT NOT NULL, snapshot_id TEXT NOT NULL,
                    status TEXT NOT NULL, phase TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
                    warnings TEXT NOT NULL, error TEXT, retry_of_run_id TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0, provenance TEXT NOT NULL,
                    worker_id TEXT, lease_until TEXT, archived INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(snapshot_id) REFERENCES snapshots(id)
                );
                CREATE INDEX IF NOT EXISTS run_namespace ON runs(namespace, kind, status, archived);
                CREATE TABLE IF NOT EXISTS idempotency (
                    namespace TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    run_id TEXT NOT NULL, PRIMARY KEY(namespace, key)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    run_id TEXT PRIMARY KEY, result TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );
                CREATE TABLE IF NOT EXISTS transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    previous_status TEXT, status TEXT NOT NULL, at TEXT NOT NULL, detail TEXT
                );
                CREATE TRIGGER IF NOT EXISTS immutable_snapshot_update BEFORE UPDATE ON snapshots
                    BEGIN SELECT RAISE(ABORT, 'Submitted snapshots are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_snapshot_delete BEFORE DELETE ON snapshots
                    BEGIN SELECT RAISE(ABORT, 'Submitted snapshots are retained'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_artifact_update BEFORE UPDATE ON artifacts
                    BEGIN SELECT RAISE(ABORT, 'Completed artifacts are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_artifact_delete BEFORE DELETE ON artifacts
                    BEGIN SELECT RAISE(ABORT, 'Completed artifacts are retained'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_run_inputs
                    BEFORE UPDATE OF snapshot_id, namespace, definition_id, definition_name, kind,
                        created_at, retry_of_run_id, provenance ON runs
                    BEGIN SELECT RAISE(ABORT, 'Submitted run identity and inputs are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS terminal_run_status BEFORE UPDATE OF status ON runs
                    WHEN OLD.status IN ('succeeded', 'failed', 'cancelled')
                    BEGIN SELECT RAISE(ABORT, 'Terminal run state is immutable'); END;
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=10000')
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _definition(self, db, namespace: str, definition_id: str) -> dict:
        row = db.execute('SELECT * FROM definitions WHERE id=? AND namespace=?', (definition_id, namespace)).fetchone()
        if row is None:
            raise ResourceError(404, 'Definition was not found in this workspace.')
        return {'id': row['id'], 'name': row['name'], 'kind': row['kind'], 'config': json.loads(row['config']), 'version': row['version'], 'archived': bool(row['archived']), 'created_at': row['created_at'], 'updated_at': row['updated_at']}

    def create_definition(self, namespace: str, body: dict) -> dict:
        definition_id, timestamp = str(uuid4()), now()
        with self.transaction() as db:
            db.execute('INSERT OR IGNORE INTO namespaces(id) VALUES(?)', (namespace,))
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
            current = self._run(db, namespace, run_id)
            if current['status'] not in TERMINAL:
                self._transition(db, run_id, current['status'], 'cancelled', 'Cancelled by the owner')
            return self._run(db, namespace, run_id)

    def archive(self, namespace: str, run_id: str, archived: bool) -> dict:
        with self.transaction() as db:
            self._run(db, namespace, run_id)
            db.execute('UPDATE runs SET archived=? WHERE id=?', (int(archived), run_id))
            return self._run(db, namespace, run_id)

    def maintain(self, max_attempts: int = 3) -> None:
        with self.transaction() as db:
            rows = db.execute("SELECT id,status,attempt FROM runs WHERE status='running' AND lease_until<?", (now(),)).fetchall()
            for row in rows:
                failed = row['attempt'] >= max_attempts
                self._transition(db, row['id'], 'running', 'failed' if failed else 'queued', 'Worker recovery exhausted' if failed else 'Recovered unchanged inputs after an expired worker lease', 'The worker stopped repeatedly. Retry this run after checking the local service.' if failed else None)
            waiting = db.execute("SELECT r.id,r.namespace,s.config FROM runs r JOIN snapshots s ON r.snapshot_id=s.id WHERE r.status='waiting_for_dependency'").fetchall()
            for row in waiting:
                dependency = self._run(db, row['namespace'], json.loads(row['config'])['forecast_run_id'])
                if dependency['status'] == 'succeeded':
                    self._transition(db, row['id'], 'waiting_for_dependency', 'queued', 'Pinned forecast succeeded; queued for simulation')
                elif dependency['status'] in {'failed', 'cancelled'}:
                    self._transition(db, row['id'], 'waiting_for_dependency', 'failed', 'Pinned forecast dependency failed', f'Forecast {dependency["id"]} {dependency["status"]}. Save a new scenario dependency and retry as a new run.')

    def claim(self, worker_id: str, lease_seconds: float = 30) -> tuple[str, dict] | None:
        with self.transaction() as db:
            row = db.execute("SELECT id,namespace FROM runs WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            self._transition(db, row['id'], 'queued', 'running', 'Computing saved inputs')
            db.execute('UPDATE runs SET worker_id=?,lease_until=?,attempt=attempt+1,started_at=COALESCE(started_at,?) WHERE id=?', (worker_id, (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat(), now(), row['id']))
            return row['namespace'], self._run(db, row['namespace'], row['id'])

    def finish(self, namespace: str, run_id: str, worker_id: str, result: dict | None = None, error: str | None = None) -> dict:
        with self.transaction() as db:
            current = db.execute('SELECT status,worker_id FROM runs WHERE id=? AND namespace=?', (run_id, namespace)).fetchone()
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
