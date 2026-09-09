"""Persistent deduplication of confirmed radio deliveries."""
from contextlib import closing
import hashlib
import json
import os
import sqlite3
from . import radio

CACHE_PATH = os.environ.get("LORA_CACHE_PATH", "/var/lib/lora-notifyd/delivery.sqlite3")

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
            db.execute("CREATE TABLE IF NOT EXISTS deliveries "
                       "(id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT)")
            row = db.execute("SELECT digest, result FROM deliveries WHERE id=?",
                             (request_id,)).fetchone()
            if row and row[0] != digest:
                return 409, {"ok": False, "status": "request_id_conflict"}
            if row and row[1]:
                return 200, json.loads(row[1])
            db.execute("INSERT OR IGNORE INTO deliveries VALUES (?, ?, NULL)",
                       (request_id, digest))
            db.commit()
            status, result = radio.send_notification(message, mode, ack)
            if status == 200:
                db.execute("UPDATE deliveries SET result=? WHERE id=?",
                           (json.dumps(result), request_id))
                db.commit()
            return status, result
    finally:
        radio.send_slot.release()
