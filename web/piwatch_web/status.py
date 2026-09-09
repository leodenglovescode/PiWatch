"""Freshness is evaluated by the reader, even when the writer has stopped."""
import json
import math
import time
from pathlib import Path

from piwatch.health import boot_id


def read_status(path):
    try:
        with Path(path).open(encoding='utf-8') as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or data.get('schema_version') != 2:
            return {'available': False, 'fresh': False, 'reason': 'Waiting for the updated watchdog'}
        now = time.time()
        same_boot = data.get('boot_id') is not None and data['boot_id'] == boot_id()
        age = time.monotonic() - float(data['generated_monotonic']) if same_boot else now - float(data['generated_at'])
        fresh = same_boot and math.isfinite(age) and 0 <= age <= float(data['stale_after_seconds'])
        health_age = time.monotonic() - data.get('self_health', {}).get('checked_monotonic', -1e12)
        data.update(available=True, fresh=fresh, age_seconds=max(0, age),
                    health_fresh=fresh and 0 <= health_age <= data['health_stale_after_seconds'])
        if not fresh:
            data['reason'] = 'Watchdog status is stale'
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return {'available': False, 'fresh': False, 'reason': 'Watchdog status unavailable'}
