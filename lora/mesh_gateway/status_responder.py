"""Authorize status commands and build compact replies from PiWatch snapshots."""
import json
import math
import queue
import time
from pathlib import Path


MAX_REPLY_BYTES = 200
DEFAULT_STATUS_PATH = '/run/piwatch/status.json'


def node_number(value):
    """Return a Meshtastic node number from an integer or !xxxxxxxx string."""
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            raw = value.strip().lower()
            if raw.startswith('!'):
                number = int(raw[1:], 16)
            elif raw.startswith('0x'):
                number = int(raw, 16)
            else:
                number = int(raw, 10)
        else:
            number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= 0xffffffff else None


def packet_text(packet):
    decoded = packet.get('decoded') or {}
    value = decoded.get('text')
    if value is None:
        data = decoded.get('data') or {}
        value = data.get('text')
    if value is None:
        value = decoded.get('payload')
    if isinstance(value, bytes):
        try:
            value = value.decode('utf-8')
        except UnicodeDecodeError:
            return None
    return value if isinstance(value, str) else None


def truncate_utf8(value, limit=MAX_REPLY_BYTES):
    encoded = value.encode('utf-8')
    if len(encoded) <= limit:
        return value
    suffix = '...'
    room = max(0, limit - len(suffix.encode('utf-8')))
    return encoded[:room].decode('utf-8', errors='ignore') + suffix


def _number(value):
    if (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value)):
        return float(value)
    return None


def _state(snapshot, targets, age):
    stale_after = _number(snapshot.get('stale_after_seconds')) or 10
    if age is None or age > stale_after:
        return 'STALE'
    states = {str(value.get('state', 'UNKNOWN')).upper()
              for value in targets.values() if isinstance(value, dict)}
    if 'DOWN' in states:
        return 'DOWN'
    if 'SUSPECT' in states:
        return 'SUSPECT'
    if 'UNKNOWN' in states:
        return 'UNKNOWN'
    monitor = snapshot.get('monitor') or {}
    return 'UP' if monitor.get('state') == 'UP' else 'DEGRADED'


def build_status_reply(snapshot, now=None):
    """Format a useful ASCII status line that always fits one 200-byte payload."""
    if not isinstance(snapshot, dict):
        return 'PiWatch status unavailable'
    now = time.time() if now is None else now
    generated = _number(snapshot.get('generated_at'))
    age = max(0, now - generated) if generated is not None else None
    targets = snapshot.get('targets') or {}
    targets = targets if isinstance(targets, dict) else {}
    states = [str(value.get('state', 'UNKNOWN')).upper()
              for value in targets.values() if isinstance(value, dict)]
    healthy = sum(value == 'UP' for value in states)
    parts = [f"PiWatch {_state(snapshot, targets, age)}",
             f"checks {healthy}/{len(states)}"]

    metrics = snapshot.get('metrics') or {}
    cpu = _number(metrics.get('cpu_percent'))
    temperature = _number(metrics.get('temperature_c'))
    memory = _number(metrics.get('memory_percent'))
    disk = _number(metrics.get('disk_percent'))
    if cpu is not None:
        cpu_text = f"CPU {cpu:.0f}%"
        if temperature is not None:
            cpu_text += f" {temperature:.0f}C"
        parts.append(cpu_text)
    if memory is not None:
        parts.append(f"RAM {memory:.0f}%")
    if disk is not None:
        parts.append(f"disk {disk:.0f}%")

    mains = targets.get('mains') or {}
    if isinstance(mains, dict) and mains.get('state'):
        parts.append(f"mains {str(mains['state']).upper()}")
    battery = targets.get('battery') or {}
    voltage = (_number(battery.get('voltage_v'))
               if isinstance(battery, dict) else None)
    if voltage is not None:
        parts.append(f"batt {voltage:.2f}V")
    if age is not None:
        parts.append(f"age {age:.0f}s")
    return truncate_utf8(' | '.join(parts))


def read_status_reply(path=DEFAULT_STATUS_PATH):
    try:
        snapshot = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return 'PiWatch status unavailable'
    return build_status_reply(snapshot)


class StatusResponder:
    """Keep Meshtastic callbacks short; a worker owns status reply sends."""

    def __init__(self, sender, allowed_senders, command='status', cooldown=15,
                 status_path=DEFAULT_STATUS_PATH, enabled=True, clock=time.monotonic):
        self.sender = sender
        self.allowed_senders = {number for value in allowed_senders
                                if (number := node_number(value)) is not None}
        self.command = command.strip().casefold()
        self.cooldown = max(1.0, float(cooldown))
        self.status_path = status_path
        self.enabled = bool(enabled) and bool(self.allowed_senders) and bool(self.command)
        self.clock = clock
        self.requests = queue.Queue(maxsize=16)
        self.last_request = {}

    def enqueue(self, packet, recipient=None):
        if not self.enabled or not isinstance(packet, dict):
            return 'disabled'
        sender = node_number(packet.get('from'))
        if sender not in self.allowed_senders:
            return 'unauthorized'
        recipient = node_number(recipient)
        if recipient is not None and node_number(packet.get('to')) != recipient:
            return 'ignored'
        text = packet_text(packet)
        if text is None or text.strip().casefold() != self.command:
            return 'ignored'
        now = self.clock()
        if now - self.last_request.get(sender, -math.inf) < self.cooldown:
            return 'rate_limited'
        try:
            self.requests.put_nowait(sender)
        except queue.Full:
            return 'busy'
        self.last_request[sender] = now
        return 'queued'

    def run(self, stop):
        while not stop.is_set():
            try:
                sender = self.requests.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                message = read_status_reply(self.status_path)
                self.sender(message, sender)
            except Exception as exc:
                print(f"[status] reply to !{sender:08x} failed: "
                      f"{type(exc).__name__}", flush=True)
            finally:
                self.requests.task_done()
