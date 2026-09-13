"""Atomic public snapshots: enough for the panel without database access."""
import json
import os
import time
from datetime import datetime
from pathlib import Path

from .health import boot_id

STATUS_PATH = os.environ.get('PIWATCH_STATUS_PATH', '/run/piwatch/status.json')


def write_status(results, trackers, outbox, health, config, started, path=STATUS_PATH,
                 revision=0, config_error=None, metrics=None, ups_supervisor=None):
    now = time.time()
    data = {
        'schema_version': 2,
        'timestamp': datetime.now().astimezone().isoformat(),
        'generated_at': now,
        'generated_monotonic': time.monotonic(),
        'boot_id': boot_id(),
        'stale_after_seconds': 10,
        'health_stale_after_seconds': max(15, 3 * config['health_interval_seconds']),
        'external_stale_after_seconds': max(10, 3 * config['external_interval_seconds']),
        'monitor': {'state': 'DEGRADED' if config_error else 'UP', 'uptime_seconds': time.monotonic() - started, 'settings_revision': revision, 'config_error': config_error},
        'targets': {},
        'self_health': health,
        'metrics': metrics or {},
        'ups_supervisor': ups_supervisor or {},
        'notifications': outbox.summary(),
        'notification_jobs': outbox.attention(),
        'incidents': outbox.history(),
    }
    for name, tracker in trackers.items():
        result = {key: value for key, value in results[name].items() if key != 'sampled'}
        data['targets'][name] = {**result, 'name': tracker.display, 'state': tracker.state, 'failures': tracker.failures,
                      'successes': tracker.successes, 'last_change': tracker.last_change,
                      'down_since': tracker.down_since,
                      'outage_seconds': max(0, now - tracker.down_since) if tracker.down_since is not None else None,
                      'last_outage_seconds': tracker.last_outage_seconds}
    for name in ('router', 'super_server', 'home_assistant', 'internet', 'dns'):
        if name in data['targets']:
            data[name] = data['targets'][name]
    target = Path(path)
    temporary = target.with_name(target.name + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    os.chmod(temporary, 0o644)
    os.replace(temporary, target)
