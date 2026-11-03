"""Independent one-second heartbeat publisher. Failed samples are never replayed."""
import argparse
import json
import math
import os
import signal
import threading
import time
from pathlib import Path

from .config_store import SettingsStore
from .health import boot_id
from .notifications.ntfy import NtfyClient, NtfyError, activated

STATUS_PATH = '/run/piwatch-heartbeat/status.json'
SNAPSHOT_PATH = '/run/piwatch/status.json'


def monitor_fresh(snapshot, monotonic=None, current_boot=None):
    monotonic = time.monotonic() if monotonic is None else monotonic
    current_boot = boot_id() if current_boot is None else current_boot
    try:
        age = monotonic - snapshot['generated_monotonic']
        return (snapshot.get('boot_id') == current_boot and
                snapshot.get('schema_version') == 2 and math.isfinite(age) and 0 <= age <= 5)
    except (KeyError, TypeError, ValueError):
        return False


def read_snapshot(path=SNAPSHOT_PATH):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


class Heartbeat:
    def __init__(self, factory=NtfyClient, active=activated):
        self.factory, self.active = factory, active
        self.settings = None
        self.client = None
        self.last_success = None
        self.error = None
        self.accepted = 0
        self.attempted = 0
        self.failed = 0
        self.last_id = None
        self.piwatch_fresh = False
        self.state = 'DISABLED'
        self.enabled = False

    def tick(self, settings, snapshot, now=None):
        explicit_now = now
        self.enabled = settings.get('heartbeat_enabled', False)
        self.piwatch_fresh = monitor_fresh(snapshot)
        if not self.enabled or not self.active():
            self.state = 'STAGED' if self.enabled else 'DISABLED'
            self.error = None
            return
        self.attempted += 1
        try:
            if settings != self.settings:
                self.client = self.factory(settings)
                self.settings = dict(settings)
            # Existing VPS contract: the outer message contains a JSON string.
            message = json.dumps({'piwatch_fresh': self.piwatch_fresh})
            result = self.client.publish(settings['heartbeat_topic'], message, timeout=0.8)
            self.last_success = time.time() if explicit_now is None else explicit_now
            self.last_id = result['id']
            self.accepted += 1
            self.error = None
            self.state = 'UP' if self.piwatch_fresh else 'MONITOR_STALE'
        except Exception as exc:
            self.failed += 1
            self.state = 'ERROR'
            self.error = exc.code if isinstance(exc, NtfyError) else type(exc).__name__

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        return {'state': self.state, 'enabled': self.enabled, 'activated': self.active(),
                'generated_at': now, 'boot_id': boot_id(),
                'last_accepted_at': self.last_success,
                'acceptance_age_seconds': None if self.last_success is None else max(0, now-self.last_success),
                'accepted_samples': self.accepted, 'attempted_samples': self.attempted, 'failed_samples': self.failed, 'last_message_id': self.last_id,
                'piwatch_fresh': self.piwatch_fresh, 'error': self.error,
                'interval_seconds': 1, 'timeout_seconds': 0.8}


def write_status(status, path=STATUS_PATH):
    target = Path(path)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(status, allow_nan=False))
    temporary.chmod(0o644)
    os.replace(temporary, target)


def run(stop=None, store=None, worker=None, snapshot_reader=read_snapshot,
        writer=write_status, monotonic=time.monotonic):
    stop = stop or threading.Event()
    store = store or SettingsStore()
    worker = worker or Heartbeat()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        started = monotonic()
        try:
            _, config = store.read()
            worker.tick(config['gateways']['ntfy'], snapshot_reader())
        except Exception as exc:
            # A bad settings read must stop publishing, not reuse stale credentials/config.
            worker.failed += 1
            worker.state, worker.error = 'ERROR', type(exc).__name__
        writer(worker.snapshot())
        # No backlog/catch-up loop after a slow or failed tick.
        stop.wait(max(0.01, 1 - (monotonic() - started)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-config', action='store_true')
    args = parser.parse_args()
    if args.check_config:
        _, config = SettingsStore().read()
        NtfyClient(config['gateways']['ntfy'])
        print('Native ntfy settings and TLS trust are readable; no request sent.')
    else:
        run()
