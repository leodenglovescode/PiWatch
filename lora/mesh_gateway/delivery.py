"""Persistent deduplication of confirmed radio deliveries."""
from contextlib import closing
import hashlib
import json
import os
import sqlite3
import time
from . import radio

CACHE_PATH = os.environ.get("LORA_CACHE_PATH", "/var/lib/lora-notifyd/delivery.sqlite3")
MAINTENANCE_INTERVAL = 3 * 86400
INCOMPLETE_RETENTION = 3 * 86400
CONFIRMED_RETENTION = 7 * 86400
MAX_DELIVERIES = 10000


def prepare_cache(db, now=None):
    """Migrate, bound, and periodically compact the delivery cache."""
    now = time.time() if now is None else now
    db.execute("CREATE TABLE IF NOT EXISTS deliveries "
               "(id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT, "
               "created_at REAL NOT NULL DEFAULT 0, confirmed_at REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS maintenance "
               "(name TEXT PRIMARY KEY, completed_at REAL NOT NULL)")
    columns = {row[1] for row in db.execute('PRAGMA table_info(deliveries)')}
    if 'created_at' not in columns:
        db.execute('ALTER TABLE deliveries ADD COLUMN created_at REAL NOT NULL DEFAULT 0')
    if 'confirmed_at' not in columns:
        db.execute('ALTER TABLE deliveries ADD COLUMN confirmed_at REAL')
    db.execute('UPDATE deliveries SET created_at=? WHERE created_at IS NULL OR created_at=0',
               (now,))
    db.execute('UPDATE deliveries SET confirmed_at=created_at '
               'WHERE result IS NOT NULL AND confirmed_at IS NULL')
    row = db.execute("SELECT completed_at FROM maintenance WHERE name='trim'").fetchone()
    if row and now - row[0] < MAINTENANCE_INTERVAL:
        db.commit()
        return False
    db.execute('DELETE FROM deliveries WHERE result IS NULL AND created_at < ?',
               (now - INCOMPLETE_RETENTION,))
    db.execute('DELETE FROM deliveries WHERE result IS NOT NULL AND confirmed_at < ?',
               (now - CONFIRMED_RETENTION,))
    db.execute('DELETE FROM deliveries WHERE id IN (SELECT id FROM deliveries '
               'ORDER BY COALESCE(confirmed_at, created_at) DESC, rowid DESC '
               'LIMIT -1 OFFSET ?)', (MAX_DELIVERIES,))
    db.execute("INSERT INTO maintenance(name, completed_at) VALUES ('trim', ?) "
               "ON CONFLICT(name) DO UPDATE SET completed_at=excluded.completed_at", (now,))
    db.commit()
    try:
        db.execute('PRAGMA optimize')
        db.execute('VACUUM')
    except sqlite3.Error:
        # Pruning is committed; cache compaction must not block radio delivery.
        pass
    return True

def cached_send(message, mode, ack, request_id=None):
    # No hidden queue: callers retry busy responses through their durable outbox.
    if not radio.send_slot.acquire(blocking=False):
        return 503, {"ok": False, "status": "busy", "retry_after_s": 5}
    try:
        if request_id is None:
            return radio.send_notification(message, mode, ack)
        digest = hashlib.sha256(json.dumps(
            [radio.DEST, message, mode, ack], ensure_ascii=False
        ).encode()).hexdigest()
        with closing(sqlite3.connect(CACHE_PATH)) as db:
            prepare_cache(db)
            row = db.execute("SELECT digest, result FROM deliveries WHERE id=?",
                             (request_id,)).fetchone()
            if row and row[0] != digest:
                return 409, {"ok": False, "status": "request_id_conflict"}
            if row and row[1]:
                return 200, json.loads(row[1])
            db.execute("INSERT OR IGNORE INTO deliveries "
                       "(id, digest, result, created_at) VALUES (?, ?, NULL, ?)",
                       (request_id, digest, time.time()))
            db.commit()
            status, result = radio.send_notification(message, mode, ack)
            if status == 200:
                db.execute("UPDATE deliveries SET result=?, confirmed_at=? WHERE id=?",
                           (json.dumps(result), time.time(), request_id))
                db.commit()
            return status, result
    finally:
        radio.send_slot.release()
