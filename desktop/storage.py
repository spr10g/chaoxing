import configparser
import copy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time


DEFAULTS = {
    'schema': 1, 'username': '', 'password': '', 'use_cookies': False,
    'endpoint': 'https://api.deepseek.com', 'key': '', 'model': 'deepseek-flash',
    'video_workers': 4, 'answer_workers': 2, 'speed': 1.0,
    'video': True, 'answer': True, 'auto_submit': False,
    'request_timeout': 60, 'min_interval': 3.0, 'http_proxy': '',
    'selections': {}, 'geometry': '1180x820', 'legacy': {},
    'selected_courses': [], 'all_chapter_courses': [],
}


def user_data_dir():
    override = os.environ.get('CHAOXING_DESKTOP_DATA')
    if override:
        return Path(override)
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'ChaoxingDesktop'


class Store:
    """One locked SQLite connection; settings and progress survive application upgrades."""
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else user_data_dir()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.directory / 'desktop.sqlite3', check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        self.db.commit()
        if not self.get('settings'):
            self.set('settings', copy.deepcopy(DEFAULTS))
        self.settings = dict(DEFAULTS, **self.get('settings'))
        # A crash during a POST must never turn into a blind automatic resubmission.
        for record in self.tasks():
            if record.get('status') in ('queued', 'running', 'submitting', 'paused', 'removing', 'checking') or record.get('execution_state') in ('queued', 'running', 'removing', 'checking'):
                previous = record['status']
                status = 'unknown' if previous == 'submitting' else previous if previous in ('draft', 'local_saved', 'review', 'submitted', 'platform_done') else 'interrupted'
                record.update(status=status, execution_state='interrupted',
                              reason='上次运行中断；重新运行前会检查平台状态')
                if previous == 'submitting':
                    record['remote_state'] = 'unknown'
                self.save_task(record)

    def get(self, key, table='kv'):
        if table not in ('kv', 'cache'):
            raise ValueError('invalid table')
        with self.lock:
            row = self.db.execute(f'SELECT value FROM {table} WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def set(self, key, value, table='kv'):
        if table not in ('kv', 'cache'):
            raise ValueError('invalid table')
        with self.lock, self.db:
            self.db.execute(f'INSERT OR REPLACE INTO {table} VALUES (?,?)',
                            (key, json.dumps(value, ensure_ascii=False)))

    def save_settings(self, updates):
        with self.lock:
            self.settings.update(updates)
            self.set('settings', self.settings)

    def save_task(self, record):
        with self.lock, self.db:
            record = dict(record, updated=time.time())
            self.db.execute('INSERT OR REPLACE INTO tasks VALUES (?,?)',
                            (record['id'], json.dumps(record, ensure_ascii=False)))

    def task(self, task_id):
        with self.lock:
            row = self.db.execute('SELECT value FROM tasks WHERE id=?', (task_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def tasks(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute('SELECT value FROM tasks')]

    def import_legacy(self, folder, explicit=False):
        folder = Path(folder)
        if self.get('legacy_imported') and not explicit:
            return
        stamp = time.strftime('%Y%m%d-%H%M%S')
        backup = self.directory / 'backups' / stamp
        backup.mkdir(parents=True, exist_ok=True)
        with self.lock:
            destination = sqlite3.connect(backup / 'before-import.sqlite3')
            self.db.backup(destination)
            destination.close()
        config_path = folder / 'config.ini'
        if config_path.exists():
            parser = configparser.ConfigParser(interpolation=None)
            parser.read(config_path, encoding='utf-8-sig')
            legacy = {s: dict(parser.items(s)) for s in parser.sections()}
            updates = {'legacy': dict(self.settings.get('legacy', {}), **legacy)}
            for section, mapping in {
                'common': {'username': 'username', 'password': 'password', 'speed': 'speed',
                           'use_cookies': 'use_cookies'},
                'tiku': {'endpoint': 'endpoint', 'key': 'key', 'model': 'model',
                         'http_proxy': 'http_proxy', 'min_interval_seconds': 'min_interval'},
            }.items():
                for old, new in mapping.items():
                    value = legacy.get(section, {}).get(old, '')
                    if value and value != 'xxx':
                        if new in ('speed', 'min_interval'):
                            value = float(value)
                        elif new == 'use_cookies':
                            value = value.lower() == 'true'
                        updates[new] = value
            self.save_settings(updates)
            shutil.copy2(config_path, backup / 'config.ini')
        for name in ('cookies.txt', 'cache.json'):
            source = folder / name
            if source.exists():
                shutil.copy2(source, backup / name)
                dest = self.directory / ('legacy-cache.json' if name == 'cache.json' else name)
                if not dest.exists() or explicit:
                    shutil.copy2(source, dest)
        # Legacy title-only answers are archived, never trusted as validated cache entries.
        self.set('legacy_imported', True)
