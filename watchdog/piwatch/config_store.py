"""Revisioned local settings. Web writes atomically; the monitor reads snapshots."""
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import sqlite3
import time

from .config import validate_config, SECRET_FIELDS

SETTINGS_PATH = os.environ.get('PIWATCH_SETTINGS_PATH', '/var/lib/piwatch-config/settings.sqlite3')


class ConflictError(ValueError):
    pass


class SettingsStore:
    def __init__(self, path=SETTINGS_PATH):
        self.path = Path(path)

    def initialize(self, config):
        config = validate_config(config)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1),
                    revision INTEGER NOT NULL, value TEXT NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, timestamp REAL NOT NULL,
                    actor TEXT NOT NULL, revision INTEGER NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO settings VALUES (1, 1, ?, ?)', (json.dumps(config), time.time()))
        os.chmod(self.path, 0o640)

    def read(self):
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            row = db.execute('SELECT revision, value FROM settings WHERE id=1').fetchone()
        if row is None:
            raise ValueError('Settings have not been initialized')
        return row[0], validate_config(json.loads(row[1]))

    def public(self):
        revision, config = self.read()
        configured = {}
        for name, fields in SECRET_FIELDS.items():
            configured[name] = {}
            for field in fields:
                configured[name][field] = bool(config['gateways'][name].pop(field, ''))
        return {'revision': revision, 'config': config, 'configured_secrets': configured}

    def save(self, config, expected_revision, actor):
        with closing(sqlite3.connect(self.path, timeout=3)) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                revision, raw = db.execute('SELECT revision, value FROM settings WHERE id=1').fetchone()
                if expected_revision != revision:
                    raise ConflictError('Settings changed in another session. Reload before saving.')
                previous = json.loads(raw)
                config = copy.deepcopy(config)
                # Omitted secret fields mean keep; an explicit empty string clears an override.
                for name, fields in SECRET_FIELDS.items():
                    gateway = config.setdefault('gateways', {}).setdefault(name, {})
                    for field in fields:
                        if field not in gateway and field in previous['gateways'][name]:
                            gateway[field] = previous['gateways'][name][field]
                config = validate_config(config)
                revision += 1
                db.execute('UPDATE settings SET revision=?, value=?, updated_at=? WHERE id=1',
                           (revision, json.dumps(config), time.time()))
                db.execute('INSERT INTO audit(timestamp, actor, revision) VALUES (?, ?, ?)', (time.time(), actor, revision))
                db.execute('DELETE FROM audit WHERE id IN (SELECT id FROM audit ORDER BY id DESC LIMIT -1 OFFSET 1000)')
                db.commit()
                return revision
            except Exception:
                db.rollback()
                raise
