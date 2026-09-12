from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import secrets
import sqlite3
from uuid import UUID, uuid4

from .schema import validate_workspace
from .store import ResourceError, Store, dump, now


ROLES = {'administrator', 'owner', 'finance', 'inventory', 'buyer', 'viewer'}
EDIT_FIELDS = {
    'finance': {'finance', 'pendingFinance', 'financeEvents', 'cash', 'budget', 'coverage', 'commitments', 'paymentTerms', 'sources', 'onboarding'},
    'inventory': {'products', 'locations', 'stock', 'movements', 'inventoryPools', 'inventoryHistory', 'serviceObservations', 'inventoryLayers', 'sources', 'onboarding'},
    'buyer': {'suppliers', 'purchases', 'paymentTerms', 'sources', 'onboarding'},
    'viewer': set(),
}
TOKEN_SECONDS = 30 * 24 * 3600


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, maxmem=64 * 1024 * 1024)
    return f'{salt.hex()}:{derived.hex()}'


def password_matches(password: str, encoded: str) -> bool:
    salt, _ = encoded.split(':')
    return hmac.compare_digest(password_hash(password, bytes.fromhex(salt)), encoded)


def public_user(row) -> dict:
    return {key: row[key] for key in ('id', 'email', 'name', 'created_at')}


def validate_document(workspace: dict) -> dict:
    try:
        UUID(workspace.get('id', ''))
        encoded = dump(workspace)
    except (ValueError, TypeError, AttributeError) as error:
        raise ResourceError(422, 'Workspace needs a UUID and finite JSON values.') from error
    if len(encoded.encode()) > 8_000_000:
        raise ResourceError(413, 'Workspace exceeds the 8 MB limit.')
    if any(isinstance(value, list) and len(value) > 20_000 for value in workspace.values()):
        raise ResourceError(413, 'A workspace list exceeds 20,000 records.')
    validate_workspace(workspace)
    return json.loads(encoded)


class Platform:
    def __init__(self, store: Store):
        self.store = store
        with store.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS platform_users (
                    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT,
                    password_hash TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES platform_users(id),
                    expires_at TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_workspaces (
                    id TEXT PRIMARY KEY REFERENCES namespaces(id), document TEXT NOT NULL,
                    revision INTEGER NOT NULL, guest_key_hash TEXT, archived INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_memberships (
                    workspace_id TEXT NOT NULL REFERENCES platform_workspaces(id),
                    user_id TEXT NOT NULL REFERENCES platform_users(id), role TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS platform_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id TEXT NOT NULL,
                    actor TEXT NOT NULL, action TEXT NOT NULL, revision INTEGER, at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_login_failures (
                    identity TEXT PRIMARY KEY, failures INTEGER NOT NULL, window_start TEXT NOT NULL
                );
            ''')

    def _session(self, db, user: dict) -> dict:
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(seconds=TOKEN_SECONDS)).isoformat()
        db.execute('INSERT INTO platform_sessions VALUES(?,?,?,?)', (digest(token), user['id'], expires, now()))
        return {'access_token': token, 'token_type': 'bearer', 'expires_in': TOKEN_SECONDS, 'user': public_user(user)}

    def register(self, email: str, password: str, name: str | None) -> dict:
        hashed = password_hash(password)
        with self.store.transaction() as db:
            user = {'id': str(uuid4()), 'email': email, 'name': name, 'created_at': now()}
            try:
                db.execute('INSERT INTO platform_users VALUES(?,?,?,?,?)', (user['id'], email, name, hashed, user['created_at']))
            except sqlite3.IntegrityError as error:
                raise ResourceError(409, 'An account already exists for this email. Sign in instead.') from error
            return self._session(db, user)

    def login(self, email: str, password: str, client: str) -> dict:
        identity = digest(f'{client}:{email}')
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        with self.store.connection() as db:
            failed = db.execute('SELECT * FROM platform_login_failures WHERE identity=?', (identity,)).fetchone()
            if failed and failed['window_start'] > cutoff and failed['failures'] >= 10:
                raise ResourceError(429, 'Too many failed sign-in attempts. Try again in 15 minutes.')
            user = db.execute('SELECT * FROM platform_users WHERE email=?', (email,)).fetchone()
        correct = password_matches(password, user['password_hash']) if user else password_matches(password, password_hash('unavailable-account-password'))
        if not user or not correct:
            with self.store.transaction() as db:
                db.execute('INSERT INTO platform_login_failures VALUES(?,1,?) ON CONFLICT(identity) DO UPDATE SET failures=CASE WHEN window_start>? THEN failures+1 ELSE 1 END, window_start=CASE WHEN window_start>? THEN window_start ELSE excluded.window_start END', (identity, now(), cutoff, cutoff))
            raise ResourceError(401, 'Email or password is incorrect.')
        with self.store.transaction() as db:
            db.execute('DELETE FROM platform_login_failures WHERE identity=?', (identity,))
            return self._session(db, user)

    def user(self, authorization: str | None) -> dict:
        if not authorization or not authorization.startswith('Bearer '):
            raise ResourceError(401, 'Sign in to continue.')
        with self.store.connection() as db:
            row = db.execute('SELECT u.* FROM platform_sessions s JOIN platform_users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?', (digest(authorization[7:]), now())).fetchone()
        if row is None:
            raise ResourceError(401, 'The account session is invalid or expired. Sign in again.')
        return public_user(row)

    def logout(self, authorization: str):
        self.user(authorization)
        with self.store.transaction() as db:
            db.execute('DELETE FROM platform_sessions WHERE token_hash=?', (digest(authorization[7:]),))

    def access(self, workspace_id: str, authorization: str | None, guest_key: str | None, write: bool = False) -> dict:
        user = self.user(authorization) if authorization else None
        with self.store.connection() as db:
            workspace = db.execute('SELECT * FROM platform_workspaces WHERE id=?', (workspace_id,)).fetchone()
            member = db.execute('SELECT role FROM platform_memberships WHERE workspace_id=? AND user_id=?', (workspace_id, user['id'])).fetchone() if user else None
        if member:
            role, actor = member['role'], user['id']
        elif workspace and guest_key and workspace['guest_key_hash'] and hmac.compare_digest(digest(guest_key), workspace['guest_key_hash']):
            role, actor = 'administrator', f'guest:{workspace_id}'
        else:
            raise ResourceError(404 if authorization or guest_key else 401, 'Workspace access was not found. Sign in or restore its private guest credential.')
        if write and (role == 'viewer' or workspace['archived']):
            raise ResourceError(403, 'This workspace is read-only for your current access.')
        return {'role': role, 'actor': actor, 'user': user, 'workspace_id': workspace_id}

    def create_workspace(self, workspace: dict, user: dict | None = None, guest_key: str | None = None) -> dict:
        document = validate_document(workspace)
        if not user and (not guest_key or len(guest_key) < 32 or len(guest_key) > 256):
            raise ResourceError(422, 'Guest creation needs a cryptographically random X-Workspace-Key of 32 to 256 characters.')
        document['revision'] = 0
        stamp = now()
        with self.store.transaction() as db:
            if db.execute('SELECT 1 FROM namespaces WHERE id=?', (document['id'],)).fetchone():
                existing = db.execute('SELECT * FROM platform_workspaces WHERE id=?', (document['id'],)).fetchone()
                member = db.execute('SELECT role FROM platform_memberships WHERE workspace_id=? AND user_id=?', (document['id'], user['id'])).fetchone() if user else None
                same_owner = bool(member and member['role'] in {'owner', 'administrator'}) or bool(existing and guest_key and existing['guest_key_hash'] and hmac.compare_digest(digest(guest_key), existing['guest_key_hash']))
                if existing and same_owner and existing['revision'] == 0 and existing['document'] == dump(document) and not existing['archived']:
                    return {'workspace': document, 'revision': 0, 'role': member['role'] if member else 'administrator'}
                raise ResourceError(409, 'This workspace ID already exists. Choose a new UUID; existing histories require authenticated ownership.')
            db.execute('INSERT INTO namespaces VALUES(?,?)', (document['id'], document['mode']))
            db.execute('INSERT INTO platform_workspaces VALUES(?,?,0,?,0,?,?)', (document['id'], dump(document), digest(guest_key) if not user else None, stamp, stamp))
            if user:
                db.execute('INSERT INTO platform_memberships VALUES(?,?,?)', (document['id'], user['id'], 'owner'))
            self._audit(db, document['id'], user['id'] if user else f'guest:{document["id"]}', 'workspace.create', 0)
        return {'workspace': document, 'revision': 0, 'role': 'owner' if user else 'administrator'}

    def _audit(self, db, workspace_id: str, actor: str, action: str, revision: int | None = None):
        db.execute('INSERT INTO platform_audit(workspace_id,actor,action,revision,at) VALUES(?,?,?,?,?)', (workspace_id, actor, action, revision, now()))

    def get_workspace(self, access: dict) -> dict:
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM platform_workspaces WHERE id=?', (access['workspace_id'],)).fetchone()
        return {'workspace': json.loads(row['document']), 'revision': row['revision'], 'role': access['role'], 'archived': bool(row['archived'])}

    def list_workspaces(self, user: dict, include_archived: bool = False) -> list[dict]:
        with self.store.connection() as db:
            rows = db.execute('SELECT w.*,m.role FROM platform_workspaces w JOIN platform_memberships m ON m.workspace_id=w.id WHERE m.user_id=? ORDER BY w.updated_at DESC', (user['id'],)).fetchall()
        return [{'id': row['id'], 'name': json.loads(row['document'])['profile'].get('name', ''), 'mode': json.loads(row['document'])['mode'], 'revision': row['revision'], 'role': row['role'], 'archived': bool(row['archived'])} for row in rows if include_archived or not row['archived']]

    def put_workspace(self, access: dict, workspace: dict, expected_revision: int) -> dict:
        document = validate_document(workspace)
        if document['id'] != access['workspace_id']:
            raise ResourceError(422, 'Workspace identity cannot be changed by an update.')
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM platform_workspaces WHERE id=?', (access['workspace_id'],)).fetchone()
            prior = json.loads(row['document'])
            if row['revision'] != expected_revision:
                raise ResourceError(409, 'The workspace changed in another session. Reload its current revision before saving; your changes were not applied.')
            if document['mode'] != prior['mode'] or document['version'] != prior['version']:
                raise ResourceError(422, 'Workspace mode and schema version cannot change in place.')
            changed = {key for key in set(prior) | set(document) if key != 'revision' and prior.get(key) != document.get(key)}
            if access['role'] not in {'owner', 'administrator'} and changed - EDIT_FIELDS[access['role']]:
                raise ResourceError(403, f'Your {access["role"]} role cannot change: {", ".join(sorted(changed - EDIT_FIELDS[access["role"]]))}.')
            document['revision'] = expected_revision + 1
            db.execute('UPDATE platform_workspaces SET document=?,revision=?,updated_at=? WHERE id=?', (dump(document), document['revision'], now(), document['id']))
            self._audit(db, document['id'], access['actor'], f'workspace.update:{",".join(sorted(changed))}', document['revision'])
        return {'workspace': document, 'revision': document['revision'], 'role': access['role']}

    def archive(self, access: dict, archived: bool = True):
        self.require_owner(access)
        with self.store.transaction() as db:
            db.execute('UPDATE platform_workspaces SET archived=?,updated_at=? WHERE id=?', (int(archived), now(), access['workspace_id']))
            self._audit(db, access['workspace_id'], access['actor'], 'workspace.archive' if archived else 'workspace.restore')
        return self.get_workspace(access)

    def claim(self, workspace_id: str, user: dict, guest_key: str | None) -> dict:
        with self.store.transaction() as db:
            row = db.execute('SELECT guest_key_hash FROM platform_workspaces WHERE id=?', (workspace_id,)).fetchone()
            if not row or not guest_key or not row['guest_key_hash'] or not hmac.compare_digest(digest(guest_key), row['guest_key_hash']):
                raise ResourceError(404, 'The guest credential cannot claim this workspace.')
            db.execute('INSERT INTO platform_memberships VALUES(?,?,?)', (workspace_id, user['id'], 'owner'))
            db.execute('UPDATE platform_workspaces SET guest_key_hash=NULL,updated_at=? WHERE id=?', (now(), workspace_id))
            self._audit(db, workspace_id, user['id'], 'workspace.claim')
        return self.get_workspace({'workspace_id': workspace_id, 'role': 'owner'})

    def require_owner(self, access: dict):
        if access['role'] not in {'owner', 'administrator'}:
            raise ResourceError(403, 'Only a workspace owner or administrator can manage access.')

    def require_analysis(self, access: dict, kind: str, config: dict):
        role = access['role']
        if role in {'owner', 'administrator'}:
            return
        families = set(config.get('output_families', []))
        allowed = (role == 'finance' and kind == 'simulation' and bool(families) and families <= {'cash', 'debt'}) or (role in {'inventory', 'buyer'} and (kind == 'forecast' or families == {'inventory'}))
        if not allowed:
            raise ResourceError(403, f'The {role} role cannot execute or change this analysis. Ask an owner to grant the relevant access.')

    def members(self, access: dict) -> list[dict]:
        self.require_owner(access)
        with self.store.connection() as db:
            rows = db.execute('SELECT u.id,u.email,u.name,u.created_at,m.role FROM platform_users u JOIN platform_memberships m ON m.user_id=u.id WHERE m.workspace_id=? ORDER BY u.email', (access['workspace_id'],)).fetchall()
        return [{'user_id': row['id'], **{key: row[key] for key in ('email', 'name', 'created_at', 'role')}} for row in rows]

    def member_change(self, access: dict, role: str | None, email: str | None = None, user_id: str | None = None) -> list[dict]:
        self.require_owner(access)
        if access['actor'].startswith('guest:'):
            raise ResourceError(403, 'Claim the workspace with an account before managing members.')
        if role is not None and role not in ROLES:
            raise ResourceError(422, 'Choose administrator, owner, finance, inventory, buyer or viewer.')
        with self.store.transaction() as db:
            user = db.execute('SELECT id FROM platform_users WHERE email=?' if email else 'SELECT id FROM platform_users WHERE id=?', (email or user_id,)).fetchone()
            if not user:
                raise ResourceError(404, 'The person must register before being added. No invitation message was sent.')
            prior = db.execute('SELECT role FROM platform_memberships WHERE workspace_id=? AND user_id=?', (access['workspace_id'], user['id'])).fetchone()
            owners = db.execute("SELECT COUNT(*) FROM platform_memberships WHERE workspace_id=? AND role IN ('owner','administrator')", (access['workspace_id'],)).fetchone()[0]
            if prior and prior['role'] in {'owner', 'administrator'} and role not in {'owner', 'administrator'} and owners <= 1:
                raise ResourceError(409, 'A workspace must retain at least one owner or administrator.')
            if role is None:
                db.execute('DELETE FROM platform_memberships WHERE workspace_id=? AND user_id=?', (access['workspace_id'], user['id']))
            else:
                db.execute('INSERT INTO platform_memberships VALUES(?,?,?) ON CONFLICT(workspace_id,user_id) DO UPDATE SET role=excluded.role', (access['workspace_id'], user['id'], role))
            self._audit(db, access['workspace_id'], access['actor'], f'member:{user["id"]}:{role or "removed"}')
        return self.members(access)
