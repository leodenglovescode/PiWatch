"""Atomic incident history, tracker checkpoints, and ordered delivery jobs."""
import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid

from .messages import build_message, build_lora_messages
from .home_assistant import send_ha_webhook
from .email import send_email
from .lora_client import lora_api_send

log = logging.getLogger("piwatch")
OUTBOX_PATH = os.environ.get("PIWATCH_OUTBOX_PATH", "/var/lib/piwatch/outbox.sqlite3")
MAX_ATTEMPTS = 5

class Outbox:
    """Persist frozen messages and tracker state together before dispatch.

    Each channel has its own worker. Confirmed LoRa stages are checkpointed
    separately; exhausted jobs remain in the database for operator review.
    """

    def __init__(self, path=OUTBOX_PATH, is_running=lambda: True):
        self.is_running = is_running
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS trackers (name TEXT PRIMARY KEY, data TEXT);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, channel TEXT NOT NULL, payload TEXT NOT NULL,
                stage INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending', last_error TEXT);
            CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY, timestamp REAL NOT NULL,
                target TEXT NOT NULL, state TEXT NOT NULL,
                outage_seconds REAL, probes TEXT NOT NULL, target_name TEXT);
            CREATE INDEX IF NOT EXISTS idx_incidents_timestamp ON incidents(timestamp);
        """)
        if 'target_name' not in {row[1] for row in self.db.execute('PRAGMA table_info(incidents)')}:
            self.db.execute('ALTER TABLE incidents ADD COLUMN target_name TEXT')
        self.db.commit()

    def restore(self, trackers):
        for name, data in self.db.execute("SELECT name, data FROM trackers"):
            if name in trackers:
                saved = json.loads(data)
                if saved.get('definition') and saved['definition'] != getattr(trackers[name], 'definition', None):
                    continue
                for key in ("state", "last_change", "down_since", "last_outage_seconds"):
                    setattr(trackers[name], key, saved[key])
                if saved.get('detected_polarity') in ('positive', 'negative'):
                    trackers[name].detected_polarity = saved['detected_polarity']
                # Thresholds come from current config; consecutive samples restart.
                trackers[name].failures = trackers[name].successes = 0

    def record(self, trackers, incidents, gateways=None):
        with self.db:
            for name, tracker in trackers.items():
                self.db.execute("INSERT OR REPLACE INTO trackers VALUES (?, ?)",
                                (name, json.dumps(vars(tracker))))
            for name, state, results in incidents:
                title, message = build_message(name, state, results, trackers)
                primary, secondary = build_lora_messages(name, state, results, trackers)
                incident_id = uuid.uuid4().hex
                self.db.execute(
                    "INSERT INTO incidents VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (incident_id, time.time(), name, state,
                     trackers[name].last_outage_seconds if state == "UP" else None,
                     json.dumps({key: value for key, value in results[name].items()
                                 if isinstance(value, bool) or key in (
                                     'voltage_v', 'low_voltage_v', 'voltage_drop_v',
                                     'shunt_voltage_mv', 'detection_method', 'discharge_polarity')}),
                     getattr(trackers[name], 'display', name)),
                )
                severity = "critical" if state == "DOWN" else "recovery"
                lora_payload = {"messages": [primary, secondary],
                                "modes": ["alert" if state == "DOWN" else "text", "text"]}
                if name == 'mains':
                    # A power event fans out independently so one failed gateway
                    # never suppresses or duplicates another successful delivery.
                    payloads = {
                        "ha": {"title": title, "message": message, "severity": severity},
                        "smtp": {"title": title, "message": message},
                        "lora": lora_payload,
                    }
                else:
                    payloads = {
                        "ip": {"title": title, "message": message, "severity": severity,
                               "use_ha": ("home_assistant" not in trackers or trackers["home_assistant"].state == "UP")
                               and not (name == "home_assistant" and state == "DOWN")},
                        "lora": lora_payload,
                    }
                for channel, payload in payloads.items():
                    if gateways is not None:
                        if channel == 'lora' and not gateways['lora']['enabled']:
                            continue
                        if channel == 'ip' and not (gateways['smtp']['enabled'] or gateways['home_assistant']['enabled']):
                            continue
                        if channel == 'ha' and not gateways['home_assistant']['enabled']:
                            continue
                        if channel == 'smtp' and not gateways['smtp']['enabled']:
                            continue
                        payload['gateways'] = gateways
                    self.db.execute("INSERT INTO jobs (id, channel, payload) VALUES (?, ?, ?)",
                                    (incident_id + ":" + channel, channel, json.dumps(payload)))
            if incidents:
                # Bound history independently of delivery records; never delete jobs here.
                self.db.execute("DELETE FROM incidents WHERE timestamp < ?", (time.time() - 90 * 86400,))
                self.db.execute("DELETE FROM incidents WHERE id IN "
                                "(SELECT id FROM incidents ORDER BY timestamp DESC, rowid DESC LIMIT -1 OFFSET 10000)")

    def history(self, limit=100):
        rows = self.db.execute(
            "SELECT id, timestamp, target, state, outage_seconds, probes, target_name FROM incidents "
            "ORDER BY timestamp DESC, rowid DESC LIMIT ?", (limit,),
        )
        return [dict(id=row[0], timestamp=row[1], target=row[2], state=row[3],
                     outage_seconds=row[4], probes=json.loads(row[5]), name=row[6] or row[2]) for row in rows]

    def summary(self):
        counts = {channel: {status: 0 for status in ("pending", "done", "failed")}
                  for channel in ("ip", "ha", "smtp", "lora")}
        for channel, status, count in self.db.execute(
            "SELECT channel, status, COUNT(*) FROM jobs GROUP BY channel, status"
        ):
            counts[channel][status] = count
        return counts

    def attention(self):
        # Publish operational metadata only, never message bodies or credentials.
        rows = self.db.execute(
            "SELECT id, channel, status, stage, attempts, next_attempt, last_error FROM jobs "
            "WHERE status != 'done' ORDER BY rowid DESC LIMIT 50"
        )
        keys = ("id", "channel", "status", "stage", "attempts", "next_attempt", "last_error")
        return [dict(zip(keys, row)) for row in rows]

    async def work(self, channel):
        while self.is_running():
            # Preserve incident order within a channel, including retry backoff.
            row = self.db.execute(
                "SELECT id, payload, stage, attempts, next_attempt FROM jobs "
                "WHERE channel=? AND status='pending' ORDER BY rowid LIMIT 1",
                (channel,),
            ).fetchone()
            if row is None or row[4] > time.time():
                await asyncio.sleep(0.5)
                continue
            job_id, raw, stage, attempts, _ = row
            payload = json.loads(raw)
            # Count attempts before I/O so process crashes cannot retry forever.
            if attempts >= MAX_ATTEMPTS:
                with self.db:
                    self.db.execute("UPDATE jobs SET status='failed' WHERE id=?", (job_id,))
                continue
            with self.db:
                self.db.execute("UPDATE jobs SET attempts=attempts+1 WHERE id=?", (job_id,))
            try:
                if channel == "lora":
                    await asyncio.to_thread(
                        lora_api_send, payload["messages"][stage],
                        payload["modes"][stage], True, job_id + ":" + str(stage),
                        **({"settings": payload["gateways"]["lora"]} if "gateways" in payload else {}),
                    )
                    stage += 1
                    with self.db:
                        self.db.execute(
                            "UPDATE jobs SET stage=?, attempts=0, next_attempt=0, "
                            "status=?, last_error=NULL WHERE id=?",
                            (stage, "done" if stage == 2 else "pending", job_id),
                        )
                elif channel == 'ha':
                    await send_ha_webhook(
                        payload['title'], payload['message'], payload['severity'],
                        **({'settings': payload['gateways']['home_assistant']}
                           if 'gateways' in payload else {}),
                    )
                    with self.db:
                        self.db.execute("UPDATE jobs SET status='done', last_error=NULL WHERE id=?",
                                        (job_id,))
                elif channel == 'smtp':
                    await send_email(
                        payload['title'], payload['message'],
                        **({'settings': payload['gateways']['smtp']}
                           if 'gateways' in payload else {}),
                    )
                    with self.db:
                        self.db.execute("UPDATE jobs SET status='done', last_error=NULL WHERE id=?",
                                        (job_id,))
                else:
                    delivered = False
                    if payload["use_ha"]:
                        try:
                            await send_ha_webhook(payload["title"], payload["message"],
                                                  payload["severity"],
                                                  **({"settings": payload["gateways"]["home_assistant"]} if "gateways" in payload else {}))
                            delivered = True
                        except Exception:
                            pass
                    if not delivered:
                        await send_email(payload["title"], payload["message"],
                                         **({"settings": payload["gateways"]["smtp"]} if "gateways" in payload else {}))
                    with self.db:
                        self.db.execute("UPDATE jobs SET status='done', last_error=NULL WHERE id=?",
                                        (job_id,))
                log.info("Notification checkpoint: channel=%s job=%s stage=%s", channel, job_id, stage)
            except Exception as exc:
                # Store only the exception type: URLs/errors can contain credentials.
                attempts += 1
                status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
                with self.db:
                    self.db.execute(
                        "UPDATE jobs SET status=?, next_attempt=?, last_error=? WHERE id=?",
                        (status, time.time() + min(300, 5 * 2 ** (attempts - 1)),
                         type(exc).__name__, job_id),
                    )
                log.error("Notification %s: channel=%s job=%s attempt=%s error=%s",
                          status, channel, job_id, attempts, type(exc).__name__)
