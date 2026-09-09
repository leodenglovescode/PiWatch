"""Single-owner accounts, revocable sessions, and single-use second factors."""
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

import pyotp
from werkzeug.security import check_password_hash, generate_password_hash


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def validate_account(username, password):
    if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}', username):
        raise ValueError('Username must contain 1–64 letters, digits, or . _ @ -')
    if not isinstance(password, str) or not 15 <= len(password) <= 256:
        raise ValueError('Use a password or passphrase of 15–256 characters')


class AuthStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS owner (id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL,
                    password TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                    totp TEXT, last_totp INTEGER NOT NULL DEFAULT -1,
                    email TEXT, recovery TEXT NOT NULL DEFAULT '[]', idle_minutes INTEGER NOT NULL DEFAULT 30);
                CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                    created REAL NOT NULL, seen REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (bucket TEXT NOT NULL, timestamp REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_attempts_bucket_time ON attempts(bucket, timestamp);
                CREATE TABLE IF NOT EXISTS challenges (id TEXT PRIMARY KEY, purpose TEXT NOT NULL,
                    value TEXT NOT NULL, expires REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    recipient TEXT, binding TEXT NOT NULL);
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create_owner(self, username, password):
        validate_account(username, password)
        hashed = generate_password_hash(password, method='scrypt:32768:8:3')
        with self.db() as db:
            if db.execute('SELECT 1 FROM owner').fetchone():
                raise ValueError('An owner already exists; use the account settings or local recovery command')
            db.execute('INSERT INTO owner(id, username, password) VALUES (1, ?, ?)', (username, hashed))

    def owner(self):
        with self.db() as db:
            row = db.execute('SELECT * FROM owner WHERE id=1').fetchone()
        return dict(row) if row else None

    def rate_limit(self, bucket, limit=8, window=300):
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM attempts WHERE timestamp < ?', (now - 900,))
            count = db.execute('SELECT COUNT(*) FROM attempts WHERE bucket=? AND timestamp>?', (bucket, now - window)).fetchone()[0]
            if count >= limit:
                return False
            db.execute('INSERT INTO attempts VALUES (?, ?)', (bucket, now))
        return True

    def password_ok(self, username, password):
        owner = self.owner()
        if not owner or not isinstance(password, str) or len(password) > 256:
            return False
        # Verify the expensive hash even for an incorrect username.
        correct = check_password_hash(owner['password'], password)
        return correct and hmac.compare_digest(owner['username'], username)

    def new_session(self):
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE created < ?', (now - 43200,))
            db.execute('INSERT INTO sessions VALUES (?, ?, ?, ?)', (digest(token), self.owner()['version'], now, now))
        return token

    def session_owner(self, token):
        if not token:
            return None
        now = time.time()
        with self.db() as db:
            row = db.execute('SELECT * FROM sessions WHERE id=?', (digest(token),)).fetchone()
            owner = db.execute('SELECT * FROM owner WHERE id=1').fetchone()
            if not row or not owner or row['version'] != owner['version'] or now - row['created'] > 43200 or now - row['seen'] > owner['idle_minutes'] * 60 or now < row['created'] - 5:
                db.execute('DELETE FROM sessions WHERE id=?', (digest(token),))
                return None
            db.execute('UPDATE sessions SET seen=? WHERE id=?', (now, digest(token)))
        return dict(owner)

    def logout(self, token):
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE id=?', (digest(token or ''),))

    def challenge(self, purpose, value, binding, recipient=None, ttl=300):
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute('DELETE FROM challenges WHERE expires < ? OR (purpose=? AND binding=?)', (time.time(), purpose, binding))
            db.execute('INSERT INTO challenges VALUES (?, ?, ?, ?, 0, ?, ?)',
                       (token, purpose, value, time.time() + ttl, recipient, binding))
        return token

    def consume_email(self, token, purpose, code, binding):
        valid = False
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM challenges WHERE id=? AND purpose=? AND binding=?', (token, purpose, binding)).fetchone()
            if row and row['expires'] > time.time() and row['attempts'] < 5:
                db.execute('UPDATE challenges SET attempts=attempts+1 WHERE id=?', (token,))
                valid = hmac.compare_digest(row['value'], digest(code))
                if valid:
                    db.execute('DELETE FROM challenges WHERE id=?', (token,))
        return row['recipient'] if valid else None

    def verify_factor(self, code, method='totp', challenge=None, purpose='login', binding=''):
        if not isinstance(code, str) or len(code) > 128:
            return False
        if method == 'email':
            owner = self.owner()
            recipient = self.consume_email(challenge, purpose, code, binding)
            return bool(owner['email'] and recipient == owner['email'])
        valid = False
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            owner = db.execute('SELECT * FROM owner WHERE id=1').fetchone()
            if method == 'recovery':
                codes = json.loads(owner['recovery'])
                hashed = digest(code.strip().replace('-', ''))
                if hashed in codes:
                    codes.remove(hashed)
                    db.execute('UPDATE owner SET recovery=? WHERE id=1', (json.dumps(codes),))
                    valid = True
            elif method == 'totp' and owner['totp']:
                step = int(time.time()) // 30
                otp = pyotp.TOTP(owner['totp'])
                for counter in (step - 1, step, step + 1):
                    if counter > owner['last_totp'] and hmac.compare_digest(otp.at(counter * 30), code):
                        db.execute('UPDATE owner SET last_totp=? WHERE id=1', (counter,))
                        valid = True
                        break
        return valid

    def enable_totp(self, challenge, code, binding):
        codes = [secrets.token_hex(10) for _ in range(8)]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM challenges WHERE id=? AND purpose=? AND binding=?', (challenge, 'totp_setup', binding)).fetchone()
            if not row or row['expires'] < time.time() or row['attempts'] >= 5:
                return None
            db.execute('UPDATE challenges SET attempts=attempts+1 WHERE id=?', (challenge,))
            step = int(time.time()) // 30
            matched = next((counter for counter in (step-1, step, step+1)
                            if hmac.compare_digest(pyotp.TOTP(row['value']).at(counter * 30), code)), None)
            if matched is None:
                return None
            db.execute('UPDATE owner SET totp=?, last_totp=?, recovery=?, version=version+1 WHERE id=1',
                       (row['value'], matched, json.dumps([digest(c) for c in codes])))
            db.execute('DELETE FROM challenges WHERE id=?', (challenge,))
            db.execute('DELETE FROM sessions')
        return codes

    def enable_email(self, email):
        codes = [secrets.token_hex(10) for _ in range(8)]
        with self.db() as db:
            db.execute('UPDATE owner SET email=?, recovery=?, version=version+1 WHERE id=1',
                       (email, json.dumps([digest(c) for c in codes])))
            db.execute('DELETE FROM sessions')
        return codes

    def update_account(self, username, password, idle_minutes):
        owner = self.owner()
        validate_account(username, password or 'unchanged-placeholder')
        if isinstance(idle_minutes, bool) or not isinstance(idle_minutes, int) or not 5 <= idle_minutes <= 120:
            raise ValueError('Session timeout must be 5–120 minutes')
        hashed = generate_password_hash(password, method='scrypt:32768:8:3') if password else owner['password']
        with self.db() as db:
            db.execute('UPDATE owner SET username=?, password=?, idle_minutes=?, version=version+1 WHERE id=1', (username, hashed, idle_minutes))
            db.execute('DELETE FROM sessions')
            db.execute('DELETE FROM challenges')

    def disable_factor(self, method):
        column = 'totp' if method == 'totp' else 'email'
        with self.db() as db:
            db.execute(f'UPDATE owner SET {column}=NULL, version=version+1 WHERE id=1')
            db.execute('DELETE FROM sessions')
            db.execute('DELETE FROM challenges')
            owner = db.execute('SELECT totp, email FROM owner').fetchone()
            if not owner['totp'] and not owner['email']:
                db.execute("UPDATE owner SET recovery='[]' WHERE id=1")
