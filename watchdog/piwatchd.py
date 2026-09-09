#!/usr/bin/env python3

import asyncio
import sqlite3
import uuid
import json
import logging
import os
import signal
import smtplib
import ssl
import time
import urllib.request
from datetime import datetime
from email.message import EmailMessage


CONFIG_PATH = "/etc/piwatch/config.json"
STATUS_PATH = "/run/piwatch/status.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger("piwatch")

running = True


# ============================================================
# Environment / secrets
# ============================================================

HA_BASE_URL = os.environ.get(
    "HA_BASE_URL",
    "http://127.0.0.1:8123",
).rstrip("/")

HA_WEBHOOK_ID = os.environ.get("HA_WEBHOOK_ID", "")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_SECURITY = os.environ.get("SMTP_SECURITY", "ssl").lower()
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USERNAME)
SMTP_TO = os.environ.get("SMTP_TO", SMTP_USERNAME)

LORA_API_URL = os.environ.get(
    "LORA_API_URL",
    "http://127.0.0.1:8989",
).rstrip("/")

LORA_TOKEN = os.environ.get("LORA_TOKEN", "")


# ============================================================
# Helpers
# ============================================================

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def yesno(value):
    return "OK" if value else "FAIL"


def format_duration(seconds):
    if seconds is None:
        return "unknown"

    seconds = int(seconds)

    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}h {minutes}m {seconds}s"

    if minutes:
        return f"{minutes}m {seconds}s"

    return f"{seconds}s"


# ============================================================
# Network checks
# ============================================================

async def ping(host):
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping",
            "-n",
            "-c", "1",
            "-W", "1",
            host,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        try:
            await asyncio.wait_for(
                proc.wait(),
                timeout=1.5,
            )

        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False

        return proc.returncode == 0

    except Exception as exc:
        log.error(
            "ping(%s) exception: %s",
            host,
            exc,
        )
        return False


async def tcp_check(host, port, timeout=1.0):
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host,
                port,
            ),
            timeout=timeout,
        )

        writer.close()

        try:
            await writer.wait_closed()
        except Exception:
            pass

        return True

    except Exception:
        return False


async def http_check(
    host,
    port,
    path="/",
    timeout=1.5,
):
    writer = None

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host,
                port,
            ),
            timeout=timeout,
        )

        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "User-Agent: piwatchd/1.4\r\n"
            "Connection: close\r\n"
            "\r\n"
        )

        writer.write(
            request.encode("ascii")
        )

        await writer.drain()

        first_line = await asyncio.wait_for(
            reader.readline(),
            timeout=timeout,
        )

        try:
            parts = first_line.decode(
                "ascii",
                errors="replace",
            ).split()

            status = int(parts[1])

            return 200 <= status < 400

        except (IndexError, ValueError):
            return False

    except Exception:
        return False

    finally:
        if writer is not None:
            writer.close()

            try:
                await writer.wait_closed()
            except Exception:
                pass


# ============================================================
# State tracking
# ============================================================

class StateTracker:

    def __init__(
        self,
        name,
        suspect_after,
        down_after,
        recover_after,
    ):
        self.name = name
        self.state = "UNKNOWN"

        self.failures = 0
        self.successes = 0

        self.suspect_after = suspect_after
        self.down_after = down_after
        self.recover_after = recover_after

        self.last_change = time.time()
        self.down_since = None
        self.last_outage_seconds = None


    def update(self, healthy):

        old_state = self.state

        if healthy:

            self.failures = 0
            self.successes += 1

            if self.state == "UNKNOWN":
                self.state = "UP"

            elif self.state in (
                "SUSPECT",
                "DOWN",
            ):

                if (
                    self.successes
                    >= self.recover_after
                ):

                    self.state = "UP"

                    if (
                        old_state == "DOWN"
                        and self.down_since is not None
                    ):
                        self.last_outage_seconds = (
                            time.time()
                            - self.down_since
                        )

                        self.down_since = None

        else:

            self.successes = 0
            self.failures += 1

            if (
                self.failures
                >= self.down_after
            ):

                if self.state != "DOWN":
                    self.down_since = time.time()

                self.state = "DOWN"

            elif (
                self.state != "DOWN"
                and self.failures >= self.suspect_after
            ):

                self.state = "SUSPECT"

        if self.state != old_state:

            self.last_change = time.time()

            if self.state == "UP":

                log.info(
                    "%s: %s -> UP",
                    self.name,
                    old_state,
                )

            elif self.state == "SUSPECT":

                log.warning(
                    "%s: %s -> SUSPECT (%d failures)",
                    self.name,
                    old_state,
                    self.failures,
                )

            elif self.state == "DOWN":

                log.error(
                    "%s: %s -> DOWN (%d failures)",
                    self.name,
                    old_state,
                    self.failures,
                )

            return (
                old_state,
                self.state,
            )

        return None


# ============================================================
# Home Assistant notification
# ============================================================

def send_ha_webhook_sync(
    title,
    message,
    severity,
):

    if not HA_WEBHOOK_ID:
        raise RuntimeError(
            "HA_WEBHOOK_ID is not configured"
        )

    url = (
        f"{HA_BASE_URL}/api/webhook/"
        f"{HA_WEBHOOK_ID}"
    )

    payload = json.dumps({
        "title": title,
        "message": message,
        "severity": severity,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "piwatchd/1.4",
        },
    )

    with urllib.request.urlopen(
        req,
        timeout=3,
    ) as response:

        if not (
            200
            <= response.status
            < 400
        ):
            raise RuntimeError(
                f"HA webhook HTTP "
                f"{response.status}"
            )


async def send_ha_webhook(
    title,
    message,
    severity,
):

    await asyncio.to_thread(
        send_ha_webhook_sync,
        title,
        message,
        severity,
    )


# ============================================================
# SMTP notification
# ============================================================

def send_email_sync(
    title,
    message,
):

    if not SMTP_HOST:
        raise RuntimeError(
            "SMTP_HOST is not configured"
        )

    if not SMTP_TO:
        raise RuntimeError(
            "SMTP_TO is not configured"
        )

    mail = EmailMessage()

    mail["Subject"] = title
    mail["From"] = SMTP_FROM
    mail["To"] = SMTP_TO

    mail.set_content(message)

    context = ssl.create_default_context()

    if SMTP_SECURITY == "ssl":

        with smtplib.SMTP_SSL(
            SMTP_HOST,
            SMTP_PORT,
            timeout=10,
            context=context,
        ) as smtp:

            if SMTP_USERNAME:
                smtp.login(
                    SMTP_USERNAME,
                    SMTP_PASSWORD,
                )

            smtp.send_message(mail)

    else:

        with smtplib.SMTP(
            SMTP_HOST,
            SMTP_PORT,
            timeout=10,
        ) as smtp:

            smtp.ehlo()

            if SMTP_SECURITY == "starttls":
                smtp.starttls(
                    context=context
                )
                smtp.ehlo()

            if SMTP_USERNAME:
                smtp.login(
                    SMTP_USERNAME,
                    SMTP_PASSWORD,
                )

            smtp.send_message(mail)


async def send_email(
    title,
    message,
):

    await asyncio.to_thread(
        send_email_sync,
        title,
        message,
    )


# ============================================================
# LoRa / Meshtastic API
# ============================================================

def lora_api_send(
    message,
    mode="text",
    ack=True,
    request_id=None,
):

    if not LORA_TOKEN:
        raise RuntimeError(
            "LORA_TOKEN is not configured"
        )

    if len(
        message.encode("utf-8")
    ) > 200:
        raise RuntimeError(
            "LoRa message exceeded "
            "200-byte PiWatch limit"
        )

    payload = json.dumps({
        "message": message,
        "mode": mode,
        "ack": ack,
        "request_id": request_id,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{LORA_API_URL}/v1/notify",
        data=payload,
        method="POST",
        headers={
            "Authorization":
                f"Bearer {LORA_TOKEN}",
            "Content-Type":
                "application/json",
            "User-Agent":
                "piwatchd/1.4",
        },
    )

    with urllib.request.urlopen(
        req,
        timeout=65,
    ) as response:

        body = response.read().decode(
            "utf-8",
            errors="replace",
        )

    result = json.loads(body)

    if not result.get("ok"):

        raise RuntimeError(
            f"LoRa API error: {result}"
        )

    if (
        ack
        and not result.get("delivered")
    ):

        raise RuntimeError(
            "LoRa destination did not ACK: "
            f"{result}"
        )

    return result


# ============================================================
# Semantic LoRa messages
# ============================================================

def build_lora_messages(
    name,
    new_state,
    results,
    trackers,
):

    now = (
        datetime.now()
        .astimezone()
        .strftime("%H:%M:%S %Z")
    )

    down = new_state == "DOWN"

    if name == "super_server":

        if down:
            primary = (
                "🚨 PiWatch: Super Server DOWN\n"
                f"Time: {now}\n"
                f"Ping: {yesno(results[name]['ping'])}\n"
                f"SSH: {yesno(results[name]['ssh'])}"
            )

        else:
            primary = (
                "✅ PiWatch: Super Server RECOVERED\n"
                f"Time: {now}\n"
                f"Outage: "
                f"{format_duration(trackers[name].last_outage_seconds)}\n"
                f"Ping: {yesno(results[name]['ping'])}\n"
                f"SSH: {yesno(results[name]['ssh'])}"
            )

        secondary = (
            "Other device states:\n"
            f"Router: "
            f"{trackers['router'].state}\n"
            f"Home Assistant: "
            f"{trackers['home_assistant'].state}"
        )


    elif name == "home_assistant":

        if down:
            primary = (
                "🚨 PiWatch: Home Assistant DOWN\n"
                f"Time: {now}\n"
                f"Ping: {yesno(results[name]['ping'])}\n"
                f"HTTP: {yesno(results[name]['http'])}"
            )

        else:
            primary = (
                "✅ PiWatch: Home Assistant RECOVERED\n"
                f"Time: {now}\n"
                f"Outage: "
                f"{format_duration(trackers[name].last_outage_seconds)}\n"
                f"Ping: {yesno(results[name]['ping'])}\n"
                f"HTTP: {yesno(results[name]['http'])}"
            )

        secondary = (
            "Other device states:\n"
            f"Router: "
            f"{trackers['router'].state}\n"
            f"Super Server: "
            f"{trackers['super_server'].state}"
        )


    elif name == "router":

        if down:
            primary = (
                "🚨 PiWatch: Router DOWN\n"
                f"Time: {now}\n"
                f"Ping: {yesno(results[name]['ping'])}"
            )

        else:
            primary = (
                "✅ PiWatch: Router RECOVERED\n"
                f"Time: {now}\n"
                f"Outage: "
                f"{format_duration(trackers[name].last_outage_seconds)}\n"
                f"Ping: {yesno(results[name]['ping'])}"
            )

        secondary = (
            "Other device states:\n"
            f"Super Server: "
            f"{trackers['super_server'].state}\n"
            f"Home Assistant: "
            f"{trackers['home_assistant'].state}"
        )


    else:

        raise ValueError(
            f"Unknown LoRa target: {name}"
        )


    if len(
        primary.encode("utf-8")
    ) > 200:
        raise RuntimeError(
            "Primary LoRa message too large"
        )

    if len(
        secondary.encode("utf-8")
    ) > 200:
        raise RuntimeError(
            "Secondary LoRa message too large"
        )


    return (
        primary,
        secondary,
    )


# ============================================================
# Full report for HA / email
# ============================================================

DISPLAY_NAMES = {
    "router": "Router",
    "super_server": "Super Server",
    "home_assistant": "Home Assistant",
}


def build_message(
    name,
    new_state,
    results,
    trackers,
):

    display = DISPLAY_NAMES[name]

    now = (
        datetime.now()
        .astimezone()
        .strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        )
    )

    if new_state == "DOWN":
        title = (
            f"🚨 PiWatch: "
            f"{display} DOWN"
        )

    else:
        title = (
            f"✅ PiWatch: "
            f"{display} RECOVERED"
        )

    lines = [
        "Node: watchdog-pi",
        f"Time: {now}",
        "",
    ]

    if name == "super_server":

        lines.extend([
            f"Ping: "
            f"{yesno(results[name]['ping'])}",

            f"SSH: "
            f"{yesno(results[name]['ssh'])}",
        ])


    elif name == "home_assistant":

        lines.extend([
            f"Ping: "
            f"{yesno(results[name]['ping'])}",

            f"HTTP: "
            f"{yesno(results[name]['http'])}",
        ])


    elif name == "router":

        lines.append(
            f"Ping: "
            f"{yesno(results[name]['ping'])}"
        )


    lines.extend([
        "",
        (
            "Router state: "
            f"{trackers['router'].state}"
        ),
        (
            "Super Server state: "
            f"{trackers['super_server'].state}"
        ),
        (
            "Home Assistant state: "
            f"{trackers['home_assistant'].state}"
        ),
    ])


    if new_state == "UP":

        lines.extend([
            "",
            (
                "Outage duration: "
                f"{format_duration(trackers[name].last_outage_seconds)}"
            ),
        ])


    return (
        title,
        "\n".join(lines),
    )


# ============================================================
# Notification routing
# ============================================================

OUTBOX_PATH = os.environ.get("PIWATCH_OUTBOX_PATH", "/var/lib/piwatch/outbox.sqlite3")
MAX_ATTEMPTS = 5


class Outbox:
    """Persist frozen messages and tracker state together before dispatch.

    Each channel has its own worker. Confirmed LoRa stages are checkpointed
    separately; exhausted jobs remain in the database for operator review.
    """

    def __init__(self, path=OUTBOX_PATH):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS trackers (name TEXT PRIMARY KEY, data TEXT);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, channel TEXT NOT NULL, payload TEXT NOT NULL,
                stage INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending', last_error TEXT);
        """)

    def restore(self, trackers):
        for name, data in self.db.execute("SELECT name, data FROM trackers"):
            if name in trackers:
                saved = json.loads(data)
                for key in ("state", "last_change", "down_since", "last_outage_seconds"):
                    setattr(trackers[name], key, saved[key])
                # Thresholds come from current config; consecutive samples restart.
                trackers[name].failures = trackers[name].successes = 0

    def record(self, trackers, incidents):
        with self.db:
            for name, tracker in trackers.items():
                self.db.execute("INSERT OR REPLACE INTO trackers VALUES (?, ?)",
                                (name, json.dumps(vars(tracker))))
            for name, state, results in incidents:
                title, message = build_message(name, state, results, trackers)
                primary, secondary = build_lora_messages(name, state, results, trackers)
                incident_id = uuid.uuid4().hex
                payloads = {
                    "ip": {"title": title, "message": message,
                           "severity": "critical" if state == "DOWN" else "recovery",
                           "use_ha": trackers["home_assistant"].state == "UP"
                           and not (name == "home_assistant" and state == "DOWN")},
                    "lora": {"messages": [primary, secondary],
                             "modes": ["alert" if state == "DOWN" else "text", "text"]},
                }
                for channel, payload in payloads.items():
                    self.db.execute("INSERT INTO jobs (id, channel, payload) VALUES (?, ?, ?)",
                                    (incident_id + ":" + channel, channel, json.dumps(payload)))

    async def work(self, channel):
        while running:
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
                    )
                    stage += 1
                    with self.db:
                        self.db.execute(
                            "UPDATE jobs SET stage=?, attempts=0, next_attempt=0, "
                            "status=?, last_error=NULL WHERE id=?",
                            (stage, "done" if stage == 2 else "pending", job_id),
                        )
                else:
                    delivered = False
                    if payload["use_ha"]:
                        try:
                            await send_ha_webhook(payload["title"], payload["message"],
                                                  payload["severity"])
                            delivered = True
                        except Exception:
                            pass
                    if not delivered:
                        await send_email(payload["title"], payload["message"])
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


# ============================================================
# Monitoring
# ============================================================

async def check_all(config):

    router_host = (
        config["router"]["host"]
    )

    super_host = (
        config["super_server"]["host"]
    )

    super_ssh = (
        config[
            "super_server"
        ]["ssh_port"]
    )

    ha_host = (
        config[
            "home_assistant"
        ]["host"]
    )

    ha_port = (
        config[
            "home_assistant"
        ]["port"]
    )

    ha_path = (
        config[
            "home_assistant"
        ]["path"]
    )


    (
        router_ping,
        super_ping,
        super_ssh_ok,
        ha_ping,
        ha_http,
    ) = await asyncio.gather(

        ping(
            router_host
        ),

        ping(
            super_host
        ),

        tcp_check(
            super_host,
            super_ssh,
        ),

        ping(
            ha_host
        ),

        http_check(
            ha_host,
            ha_port,
            ha_path,
        ),
    )


    return {

        "router": {
            "healthy":
                router_ping,

            "ping":
                router_ping,
        },


        "super_server": {

            "healthy": (
                super_ping
                and super_ssh_ok
            ),

            "ping":
                super_ping,

            "ssh":
                super_ssh_ok,
        },


        "home_assistant": {

            "healthy": (
                ha_ping
                and ha_http
            ),

            "ping":
                ha_ping,

            "http":
                ha_http,
        },
    }


def write_status(
    results,
    trackers,
    outbox=None,
):

    data = {

        "timestamp": (
            datetime.now()
            .astimezone()
            .isoformat()
        ),


        "router": {

            **results["router"],

            "state":
                trackers[
                    "router"
                ].state,

            "failures":
                trackers[
                    "router"
                ].failures,
        },


        "super_server": {

            **results[
                "super_server"
            ],

            "state":
                trackers[
                    "super_server"
                ].state,

            "failures":
                trackers[
                    "super_server"
                ].failures,
        },


        "home_assistant": {

            **results[
                "home_assistant"
            ],

            "state":
                trackers[
                    "home_assistant"
                ].state,

            "failures":
                trackers[
                    "home_assistant"
                ].failures,
        },
    }


    if outbox is not None:
        data["notifications"] = {
            channel: {status: count for status, count in outbox.db.execute(
                "SELECT status, COUNT(*) FROM jobs WHERE channel=? GROUP BY status",
                (channel,),
            )} for channel in ("ip", "lora")
        }

    temp = (
        STATUS_PATH
        + ".tmp"
    )

    with open(
        temp,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            data,
            f,
            indent=2,
        )


    os.replace(
        temp,
        STATUS_PATH,
    )


# ============================================================
# Main loop
# ============================================================

async def main():

    global running

    config = load_config()


    suspect = config[
        "suspect_after_failures"
    ]

    down = config[
        "down_after_failures"
    ]

    recover = config[
        "recover_after_successes"
    ]


    trackers = {

        "router":
            StateTracker(
                "router",
                suspect,
                down,
                recover,
            ),

        "super_server":
            StateTracker(
                "super_server",
                suspect,
                down,
                recover,
            ),

        "home_assistant":
            StateTracker(
                "home_assistant",
                suspect,
                down,
                recover,
            ),
    }


    outbox = Outbox()
    outbox.restore(trackers)
    workers = [asyncio.create_task(outbox.work(channel)) for channel in ("ip", "lora")]

    interval = float(
        config[
            "interval_seconds"
        ]
    )


    log.info(
        "piwatchd 1.4 starting"
    )

    log.info(
        "Targets: router=%s "
        "super=%s HA=%s:%s",

        config[
            "router"
        ]["host"],

        config[
            "super_server"
        ]["host"],

        config[
            "home_assistant"
        ]["host"],

        config[
            "home_assistant"
        ]["port"],
    )


    while running:

        started = (
            time.monotonic()
        )


        results = await check_all(
            config
        )


        transitions = []


        for name in trackers:

            transition = (
                trackers[name]
                .update(
                    results[name][
                        "healthy"
                    ]
                )
            )

            if transition:

                transitions.append(
                    (
                        name,
                        transition,
                    )
                )


        if transitions:
            incidents = [
                (name, new_state, results)
                for name, (old_state, new_state) in transitions
                if new_state == "DOWN" or (old_state == "DOWN" and new_state == "UP")
            ]
            outbox.record(trackers, incidents)

        write_status(results, trackers, outbox)
        # A dead delivery worker must not leave an apparently healthy monitor.
        for worker in workers:
            if running and worker.done():
                worker.result()
                raise RuntimeError("notification worker exited unexpectedly")

        elapsed = (
            time.monotonic()
            - started
        )


        await asyncio.sleep(
            max(
                0.0,
                interval - elapsed,
            )
        )


    # Finish/checkpoint current I/O before closing the durable queue.
    await asyncio.gather(*workers)
    outbox.db.close()
    log.info("piwatchd stopping")


def stop_handler(*_):

    global running
    running = False


if __name__ == "__main__":

    signal.signal(
        signal.SIGTERM,
        stop_handler,
    )

    signal.signal(
        signal.SIGINT,
        stop_handler,
    )

    asyncio.run(
        main()
    )
