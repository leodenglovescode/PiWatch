"""Revisioned local settings. Web writes atomically; the monitor reads snapshots."""
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .config import validate_config, SECRET_FIELDS

SETTINGS_PATH = os.environ.get('PIWATCH_SETTINGS_PATH', '/var/lib/piwatch-config/settings.sqlite3')
MAINTENANCE_ACTIONS = {'clear_deliveries', 'clear_incidents', 'clear_all'}


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
                CREATE TABLE IF NOT EXISTS maintenance_requests (
                    id TEXT PRIMARY KEY, requested_at REAL NOT NULL, actor TEXT NOT NULL,
                    action TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    completed_at REAL, result TEXT);
            ''')
            db.execute('INSERT OR IGNORE INTO settings VALUES (1, 1, ?, ?)', (json.dumps(config), time.time()))
        os.chmod(self.path, 0o660)

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
            section = config['mqtt'] if name == 'mqtt' else config['gateways'][name]
            for field in fields:
                configured[name][field] = bool(section.pop(field, ''))
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
                    gateway = (config.setdefault('mqtt', {}) if name == 'mqtt' else
                               config.setdefault('gateways', {}).setdefault(name, {}))
                    previous_gateway = (previous.get('mqtt', {}) if name == 'mqtt' else
                                        previous['gateways'][name])
                    for field in fields:
                        if field not in gateway and field in previous_gateway:
                            gateway[field] = previous_gateway[field]
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

    @staticmethod
    def _ensure_maintenance(db):
        db.execute('''CREATE TABLE IF NOT EXISTS maintenance_requests (
            id TEXT PRIMARY KEY, requested_at REAL NOT NULL, actor TEXT NOT NULL,
            action TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            completed_at REAL, result TEXT)''')

    def request_maintenance(self, action, actor):
        if action not in MAINTENANCE_ACTIONS:
            raise ValueError('Unknown maintenance action')
        request_id = uuid.uuid4().hex
        with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            self._ensure_maintenance(db)
            db.execute('INSERT INTO maintenance_requests '
                       '(id, requested_at, actor, action) VALUES (?, ?, ?, ?)',
                       (request_id, time.time(), actor, action))
            db.execute("DELETE FROM maintenance_requests WHERE id IN "
                       "(SELECT id FROM maintenance_requests WHERE status != 'pending' "
                       "ORDER BY completed_at DESC LIMIT -1 OFFSET 100)")
        return request_id

    def pending_maintenance(self, limit=10):
        with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            self._ensure_maintenance(db)
            rows = db.execute(
                "SELECT id, action, actor FROM maintenance_requests "
                "WHERE status='pending' ORDER BY requested_at LIMIT ?", (limit,)
            ).fetchall()
        return [{'id': row[0], 'action': row[1], 'actor': row[2]} for row in rows]

    def finish_maintenance(self, request_id, result=None, failed=False):
        with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            self._ensure_maintenance(db)
            db.execute(
                "UPDATE maintenance_requests SET status=?, completed_at=?, result=? "
                "WHERE id=? AND status='pending'",
                ('failed' if failed else 'complete', time.time(),
                 json.dumps(result or {}), request_id))

    def maintenance_status(self, request_id):
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro',
                                     uri=True, timeout=2)) as db:
            row = db.execute(
                'SELECT action, status, completed_at, result FROM maintenance_requests WHERE id=?',
                (request_id,)).fetchone()
        if row is None:
            return None
        return {'action': row[0], 'status': row[1], 'completed_at': row[2],
                'result': json.loads(row[3]) if row[3] else None}
