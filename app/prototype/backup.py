import argparse
import os
from pathlib import Path
import sqlite3


def backup_database(source: str, destination: str) -> None:
    target = Path(destination)
    if target.exists():
        raise ValueError('Choose a new backup path; existing files are never overwritten.')
    if not Path(source).is_file():
        raise ValueError('The source database does not exist.')
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f'{Path(source).resolve().as_uri()}?mode=ro', uri=True) as original:
        with sqlite3.connect(str(target)) as backup:
            original.backup(backup)
            if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('The backup did not pass SQLite integrity verification.')


def main():
    parser = argparse.ArgumentParser(description='Create a consistent SQLite backup without stopping Samby.')
    parser.add_argument('--database', default=os.environ.get('SAMBY_PROTOTYPE_DB') or str(Path(__file__).parent / 'data' / 'samby.sqlite3'))
    parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    backup_database(args.database, args.destination)
    print(f'Created a verified database backup at {args.destination}.')


if __name__ == '__main__':
    main()
