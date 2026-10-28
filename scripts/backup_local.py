#!/usr/bin/env python3
"""Back up the SQLite wiki, runtime settings, uploads, and committed Git history.
Stop the wiki before running so uploads/settings and database form one snapshot.
"""
from contextlib import closing
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess


def Backup_state(repo, destination):
    """Copy durable editor state; refuse an active/changing writer."""
    state = Path(subprocess.check_output(['git', 'rev-parse', '--path-format=absolute', '--git-path',
                                          'devlog-wiki'], cwd=repo, text=True).strip())
    if not state.exists():
        return False
    if list(state.rglob('*.lock')):
        raise RuntimeError('wiki worker is active or has a stale lock; finish/recover it before backup')
    files = {str(p.relative_to(state)): p.read_bytes() for p in state.rglob('*.json')
             if not any(part.startswith('generate-') for part in p.relative_to(state).parts)}
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, data in files.items():
        target = destination/name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes(data)
        target.chmod(0o600)
    current = {str(p.relative_to(state)): p.read_bytes() for p in state.rglob('*.json')
               if not any(part.startswith('generate-') for part in p.relative_to(state).parts)}
    if files != current or list(state.rglob('*.lock')):
        raise RuntimeError('wiki state changed during backup; do not restore this incomplete snapshot')
    manifest = {'version': 1, 'sha256': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    (destination/'state-manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    (destination/'state-manifest.json').chmod(0o600)
    return True


def Restore_state(snapshot, repo):
    """Restore a checked state snapshot into an empty Git state directory only."""
    manifest = json.loads((snapshot/'state-manifest.json').read_text())
    if manifest.get('version') != 1 or not isinstance(manifest.get('sha256'), dict):
        raise RuntimeError('invalid state manifest')
    restored = {}
    for name, digest in manifest['sha256'].items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:
            raise RuntimeError('invalid state path')
        path = snapshot/relative
        if path.is_symlink() or not path.resolve().is_relative_to(snapshot.resolve()):
            raise RuntimeError('invalid state path')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError('state checksum mismatch')
        restored[name] = data
    state = Path(subprocess.check_output(['git', 'rev-parse', '--path-format=absolute', '--git-path',
                                          'devlog-wiki'], cwd=repo, text=True).strip())
    if state.exists() and any(state.iterdir()):
        raise RuntimeError('target state is not empty; preserve it before restoring')
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, data in restored.items():
        target = state/name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes(data)
        target.chmod(0o600)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-repo', action='append', type=Path, default=[], help='additional development repository to back up')
    parser.add_argument('--restore-state', type=Path, help='state snapshot directory to restore')
    parser.add_argument('--repo', type=Path, default=Path.cwd(), help='target repository for state restore')
    args = parser.parse_args()
    if args.restore_state:
        Restore_state(args.restore_state, args.repo)
        print('State restored; check local tool paths before resuming the worker.')
        return
    project_root = Path(__file__).resolve().parents[1]
    settings_path = project_root / 'data' / 'set.json'
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    database_type = os.environ.get('NAMU_DB_TYPE', settings.get('db_type', 'sqlite'))
    if database_type != 'sqlite':
        raise SystemExit('This script supports SQLite only; use a database-specific backup for MySQL.')
    database_name = os.environ.get('NAMU_DB', settings.get('db', 'data'))
    database_path = project_root / (database_name + '.db')
    if not database_path.is_file():
        raise SystemExit('Configured SQLite database does not exist.')
    backup_root = project_root / '.local-backups'
    backup_root.mkdir(exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    backup_dir = backup_root / stamp
    backup_dir.mkdir(mode=0o700)
    database_backup = backup_dir / 'wiki.db'
    with closing(sqlite3.connect(database_path.as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(database_backup)) as destination:
            source.backup(destination)
            destination.execute('PRAGMA journal_mode=DELETE')
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise SystemExit('SQLite integrity check failed; backup is incomplete.')
    data_dir = project_root / 'data'
    for config_path in data_dir.glob('*.json'):
        target_path = backup_dir / 'data' / config_path.name
        target_path.parent.mkdir(exist_ok=True)
        shutil.copy2(config_path, target_path)
    image_dir = data_dir / 'images'
    if image_dir.exists():
        shutil.copytree(image_dir, backup_dir / 'data' / 'images')
    subprocess.run(['git', 'bundle', 'create', str(backup_dir / 'source.bundle'), '--all'], cwd=project_root, check=True)
    state_backups = []
    for index, repo in enumerate(dict.fromkeys([project_root]+[p.resolve() for p in args.state_repo])):
        folder = 'devlog-wiki' if index == 0 else 'devlog-wiki-'+str(index)
        if Backup_state(repo, backup_dir/folder):
            state_backups.append({'repository': str(repo), 'folder': folder})
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=project_root, text=True).strip()
    dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=project_root, text=True).strip())
    checksums = {str(p.relative_to(backup_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in backup_dir.rglob('*') if p.is_file()}
    manifest = {'created_utc': stamp, 'source_commit': commit, 'uncommitted_changes_not_in_bundle': dirty,
                'database_restore_path': str(database_path.relative_to(project_root)) if database_path.is_relative_to(project_root) else str(database_path),
                'sha256': checksums, 'editor_states': state_backups,
                'note': 'Contains private wiki data/settings. Keep private; do not commit or upload automatically.'}
    (backup_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(backup_dir)
    if dirty:
        print('Note: source.bundle contains committed history only; commit source changes before the final backup.')


if __name__ == '__main__':
    main()
