"""Power-event overlays derived from the watchdog's public status snapshot."""
import json
import math
from pathlib import Path
import time

STATUS_PATH = Path('/run/piwatch/status.json')
EVENT_SECONDS = 3.0


def read_status(path=STATUS_PATH):
    """Return only a fresh status snapshot from the current boot."""
    try:
        with Path(path).open(encoding='utf-8') as stream:
            status = json.load(stream)
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        age = time.monotonic() - status.get('generated_monotonic', -1e12)
        if (status.get('boot_id') != boot or age < 0
                or age > status.get('stale_after_seconds', 10)):
            return None
        return status
    except (OSError, ValueError, AttributeError, TypeError):
        return None


class PowerOverlay:
    """Track debounced mains transitions and active shutdown presentation."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.last_mains_state = None
        self.event = None
        self.event_until = 0.0
        self.display_off = False

    @staticmethod
    def _voltage(source):
        value = (source.get('voltage_v', source.get('battery_voltage_v'))
                 if isinstance(source, dict) else None)
        return float(value) if (isinstance(value, (int, float))
                                and not isinstance(value, bool)
                                and math.isfinite(value)) else None

    def update(self, status):
        now = self.clock()
        if not isinstance(status, dict):
            if self.display_off:
                return {'display_off': True}
            return self.event if self.event is not None and now < self.event_until else None
        targets = status.get('targets') or {}
        mains = targets.get('mains') or {}
        mains_state = mains.get('state')
        if mains_state in ('UP', 'DOWN'):
            changed = (self.last_mains_state is not None
                       and mains_state != self.last_mains_state)
            initial_outage = self.last_mains_state is None and mains_state == 'DOWN'
            self.last_mains_state = mains_state
            if changed or initial_outage:
                self.event = {
                    'title': 'MAINS POWER',
                    'message': 'RESTORED' if mains_state == 'UP' else 'DISCONNECT',
                    'voltage_v': self._voltage(mains),
                }
                self.event_until = now + EVENT_SECONDS

        supervisor = status.get('ups_supervisor') or {}
        state = supervisor.get('state')
        if state == 'POWEROFF_FAILED':
            self.display_off = False
            return {'title': 'SHUTDOWN', 'message': 'FAILED',
                    'voltage_v': self._voltage(supervisor)}
        if state in ('DISPLAY_OFF', 'ARMING', 'ARMED', 'ARM_FAILED',
                     'POWEROFF_REQUESTED'):
            self.display_off = True
            return {'display_off': True}
        if self.display_off:
            self.display_off = False
        if state in ('QUALIFYING', 'TEST_COUNTDOWN'):
            remaining = (supervisor.get('remaining_seconds') if state == 'QUALIFYING'
                         else supervisor.get('final_countdown_seconds'))
            if isinstance(remaining, (int, float)) and math.isfinite(remaining):
                if state == 'TEST_COUNTDOWN' or 0 < remaining <= 10:
                    return {
                        'title': 'SHUTTING DOWN',
                        'message': f'IN {max(0, math.ceil(remaining))} SEC',
                        'voltage_v': self._voltage(supervisor),
                    }
        if state in ('TRIGGERED', 'TEST_TRIGGERED', 'NOTIFYING'):
            return {'title': 'SHUTDOWN', 'message': 'PREPARING',
                    'voltage_v': self._voltage(supervisor)}
        if self.event is not None and now < self.event_until:
            return self.event
        self.event = None
        return None
