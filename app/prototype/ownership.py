import argparse
import json
from pathlib import Path

from .platform import Platform
from .store import Store, ResourceError, dump, now


def assign_legacy_owner(database: str, workspace_id: str, email: str) -> None:
    store = Store(database)
    platform = Platform(store)
    with store.transaction() as db:
        user = db.execute('SELECT id FROM platform_users WHERE email=?', (email.strip().lower(),)).fetchone()
        if not user:
            raise ResourceError(404, 'Register the target account before assigning ownership.')
        if db.execute('SELECT 1 FROM platform_workspaces WHERE id=?', (workspace_id,)).fetchone():
            raise ResourceError(409, 'This workspace already has platform access. Manage membership through its owner.')
        latest = db.execute('SELECT s.workspace FROM runs r JOIN snapshots s ON s.id=r.snapshot_id WHERE r.namespace=? ORDER BY r.created_at DESC LIMIT 1', (workspace_id,)).fetchone()
        if not latest:
            raise ResourceError(404, 'No retained analytical snapshot exists for this legacy namespace.')
        document = json.loads(latest['workspace'])
        document['revision'] = 0
        db.execute('INSERT INTO platform_workspaces VALUES(?,?,0,NULL,0,?,?)', (workspace_id, dump(document), now(), now()))
        db.execute('INSERT INTO platform_memberships VALUES(?,?,?)', (workspace_id, user['id'], 'owner'))
        platform._audit(db, workspace_id, 'local-cli', f'legacy.ownership:{user["id"]}', 0)


def main():
    parser = argparse.ArgumentParser(description='Explicit local-only ownership migration for one retained UUID-only analytical namespace.')
    parser.add_argument('--database', default=str(Path(__file__).parent / 'data' / 'samby.sqlite3'))
    parser.add_argument('--workspace-id', required=True)
    parser.add_argument('--owner-email', required=True)
    args = parser.parse_args()
    assign_legacy_owner(args.database, args.workspace_id, args.owner_email)
    print(f'Assigned retained workspace {args.workspace_id} to {args.owner_email}. Saved runs and snapshots were unchanged.')


if __name__ == '__main__':
    main()
