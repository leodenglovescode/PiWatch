"""Authorize compact, read-only PiWatch commands received over Meshtastic."""
import json
import math
import queue
import time
from datetime import datetime
from pathlib import Path


MAX_REPLY_BYTES = 200
DEFAULT_STATUS_PATH = '/run/piwatch/status.json'
COMMANDS = (
    'help', 'status', 'power', 'problems', 'check', 'incidents', 'incident',
    'alerts', 'system', 'radio', 'nodes', 'node', 'version', 'ping',
)


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


def node_id(value):
    number = node_number(value)
    return f'!{number:08x}' if number is not None else '!????????'


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


def _clean(value, fallback='?'):
    text = ' '.join(str(value).split()) if value is not None else ''
    return text or fallback


def _number(value):
    if (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value)):
        return float(value)
    return None


def _integer(value, default=1):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(1, number)


def _duration(seconds):
    value = _number(seconds)
    if value is None:
        return '?'
    value = max(0, int(value))
    if value < 60:
        return f'{value}s'
    if value < 3600:
        return f'{value // 60}m'
    if value < 86400:
        return f'{value // 3600}h{value % 3600 // 60:02d}m'
    return f'{value // 86400}d{value % 86400 // 3600:02d}h'


def _rate(value):
    number = _number(value)
    if number is None:
        return '?'
    if number >= 1024 * 1024:
        return f'{number / 1024 / 1024:.1f}MB/s'
    if number >= 1024:
        return f'{number / 1024:.1f}KB/s'
    return f'{number:.0f}B/s'


def _time_text(value):
    if not isinstance(value, str):
        return '?'
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.strftime('%m-%d %H:%M')
    except ValueError:
        return _clean(value)[:16]


def _age(snapshot, now=None):
    generated = _number(snapshot.get('generated_at')) if isinstance(snapshot, dict) else None
    if generated is None:
        return None
    return max(0.0, (time.time() if now is None else now) - generated)


def _age_text(snapshot, now=None):
    age = _age(snapshot, now)
    return f'age {_duration(age)}' if age is not None else 'age ?'


def _snapshot_stale(snapshot, now=None):
    age = _age(snapshot, now)
    stale_after = _number(snapshot.get('stale_after_seconds')) or 10
    return age is None or age > stale_after


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


def read_snapshot(path=DEFAULT_STATUS_PATH):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def build_status_reply(snapshot, now=None):
    """Format a useful ASCII status line that always fits one radio message."""
    if not isinstance(snapshot, dict):
        return 'PiWatch status unavailable'
    age = _age(snapshot, now)
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
    parts.append(_age_text(snapshot, now))
    return truncate_utf8(' | '.join(parts))


def read_status_reply(path=DEFAULT_STATUS_PATH):
    return build_status_reply(read_snapshot(path))


def build_help_replies(sender, gateway, cooldown=15):
    ids = f'GW {node_id(gateway)} | PORTABLE {node_id(sender)}'
    return (
        'HELP 1/3 | status: overall health; power: mains, battery and shutdown; '
        'problems: unhealthy checks; check NAME: one configured target.',
        'HELP 2/3 | incidents [PAGE]: history; incident N: event detail; alerts: '
        'delivery queue; system: Pi metrics; version: boot/config; ping: link test.',
        'HELP 3/3 | radio: LoRa state/config; nodes [PAGE]: heard nodes; node ID: '
        f'one node. {ids} | DM only; wait {cooldown:g}s. Missed page: help N.',
    )


def _power_reply(snapshot, now=None):
    targets = snapshot.get('targets') or {}
    mains = targets.get('mains') or {}
    battery = targets.get('battery') or {}
    supervisor = snapshot.get('ups_supervisor') or {}
    mains_state = str(mains.get('state', 'UNKNOWN')).upper()
    voltage = _number(battery.get('voltage_v'))
    if voltage is None:
        voltage = _number(supervisor.get('battery_voltage_v'))
    voltage_text = f'{voltage:.3f}V' if voltage is not None else 'invalid'
    state = str(supervisor.get('state', 'UNKNOWN')).upper()
    threshold = _number(supervisor.get('shutdown_voltage_v'))
    qualify = _number(supervisor.get('qualification_seconds'))
    low_for = _number(supervisor.get('low_for_seconds'))
    parts = [f'POWER mains {mains_state}', f'batt {voltage_text}', f'shutdown {state}']
    if threshold is not None and qualify is not None:
        parts.append(f'limit {threshold:.2f}V/{qualify:.0f}s')
    if state in ('QUALIFYING', 'TEST_COUNTDOWN') and low_for is not None:
        parts.append(f'low {_duration(low_for)}')
    if supervisor.get('error'):
        parts.append(f"error {_clean(supervisor['error'])}")
    parts.append(_age_text(snapshot, now))
    return truncate_utf8(' | '.join(parts))


def _failed_probes(target):
    names = [key for key, value in target.items()
             if isinstance(value, bool) and not value
             and key not in ('healthy', 'sampled')]
    endpoints = target.get('endpoints')
    if isinstance(endpoints, list):
        names.extend(_clean(item.get('target')) for item in endpoints
                     if isinstance(item, dict) and item.get('ok') is False)
    return names


def _problems_reply(snapshot, now=None):
    targets = snapshot.get('targets') or {}
    bad = []
    for key, target in targets.items():
        if not isinstance(target, dict):
            continue
        state = str(target.get('state', 'UNKNOWN')).upper()
        if state == 'UP':
            continue
        name = _clean(target.get('name'), key)
        text = f'{name} {state}'
        outage = _number(target.get('outage_seconds'))
        if outage is not None:
            text += f' {_duration(outage)}'
        failed = _failed_probes(target)
        if failed:
            text += f" ({','.join(failed[:2])})"
        bad.append(text)
    if not bad:
        return truncate_utf8(f'PROBLEMS none | all checks UP | {_age_text(snapshot, now)}')
    return truncate_utf8(f"PROBLEMS {len(bad)} | " + ' | '.join(bad)
                         + f' | {_age_text(snapshot, now)}')


def _find_target(snapshot, requested):
    wanted = requested.strip().casefold()
    for key, target in (snapshot.get('targets') or {}).items():
        if not isinstance(target, dict):
            continue
        if wanted in (str(key).casefold(), str(target.get('name', '')).casefold()):
            return key, target
    return None, None


def _check_reply(snapshot, requested, now=None):
    if not requested.strip():
        return 'Usage: check NAME'
    key, target = _find_target(snapshot, requested)
    if target is None:
        names = ','.join(str(key) for key in (snapshot.get('targets') or {}))
        return truncate_utf8(f'Unknown check {_clean(requested)} | available: {names}')
    state = str(target.get('state', 'UNKNOWN')).upper()
    parts = [f"CHECK {_clean(target.get('name'), key)} [{key}] {state}"]
    probes = [f'{name} {"OK" if value else "FAIL"}'
              for name, value in target.items()
              if isinstance(value, bool) and name not in ('healthy', 'sampled')]
    if probes:
        parts.append(' '.join(probes))
    if target.get('failures') is not None:
        parts.append(f"failures {target['failures']}")
    outage = _number(target.get('outage_seconds'))
    if outage is not None:
        parts.append(f'outage {_duration(outage)}')
    parts.append(_age_text(snapshot, now))
    return truncate_utf8(' | '.join(parts))


def _incident_summary(incident, number):
    name = _clean(incident.get('name'), incident.get('target', '?'))
    state = str(incident.get('state', 'UNKNOWN')).upper()
    return f'{number}:{name} {state} {_time_text(incident.get("timestamp"))}'


def _incidents_reply(snapshot, requested):
    incidents = snapshot.get('incidents') or []
    incidents = incidents if isinstance(incidents, list) else []
    if not incidents:
        return 'INCIDENTS none'
    page = _integer(requested, 1)
    page_size = 3
    start = (page - 1) * page_size
    selected = incidents[start:start + page_size]
    pages = max(1, math.ceil(len(incidents) / page_size))
    if not selected:
        return f'INCIDENTS page {page}/{pages} empty'
    details = [_incident_summary(item, start + offset + 1)
               for offset, item in enumerate(selected) if isinstance(item, dict)]
    return truncate_utf8(f'INCIDENTS {page}/{pages} | ' + ' | '.join(details))


def _incident_reply(snapshot, requested):
    if not requested.strip() or not requested.strip().isdigit():
        return 'Usage: incident N (use incidents to list numbers)'
    number = int(requested.strip())
    incidents = snapshot.get('incidents') or []
    if number < 1 or number > len(incidents):
        return f'Incident {number} not found'
    incident = incidents[number - 1]
    parts = [f'INCIDENT {number}', _clean(incident.get('name'), incident.get('target', '?')),
             str(incident.get('state', 'UNKNOWN')).upper(), _time_text(incident.get('timestamp'))]
    outage = _number(incident.get('outage_seconds'))
    if outage is not None:
        parts.append(f'outage {_duration(outage)}')
    probes = incident.get('probes') or {}
    if isinstance(probes, dict):
        failed = _failed_probes(probes)
        if failed:
            parts.append(f"failed {','.join(failed[:4])}")
    return truncate_utf8(' | '.join(parts))


def _alerts_reply(snapshot):
    summary = snapshot.get('notifications') or {}
    parts = []
    pending = failed = done = 0
    for channel, counts in summary.items():
        if not isinstance(counts, dict):
            continue
        channel_pending = int(_number(counts.get('pending')) or 0)
        channel_failed = int(_number(counts.get('failed')) or 0)
        channel_done = int(_number(counts.get('done')) or 0)
        pending += channel_pending
        failed += channel_failed
        done += channel_done
        if channel_pending or channel_failed:
            parts.append(f'{channel} p{channel_pending}/f{channel_failed}')
    jobs = snapshot.get('notification_jobs') or []
    result = f'ALERTS pending {pending} failed {failed} done {done}'
    if parts:
        result += ' | ' + ' '.join(parts)
    if jobs and isinstance(jobs[0], dict):
        job = jobs[0]
        result += (f" | latest {_clean(job.get('channel'))} {_clean(job.get('status'))}"
                   f" try {job.get('attempts', '?')}")
        if job.get('last_error'):
            result += f" {_clean(job['last_error'])}"
    return truncate_utf8(result)


def _system_reply(snapshot, now=None):
    metrics = snapshot.get('metrics') or {}
    monitor = snapshot.get('monitor') or {}
    parts = [f"SYSTEM uptime {_duration(monitor.get('uptime_seconds'))}"]
    cpu = _number(metrics.get('cpu_percent'))
    temp = _number(metrics.get('temperature_c'))
    memory = _number(metrics.get('memory_percent'))
    disk = _number(metrics.get('disk_percent'))
    load = _number(metrics.get('load_1m'))
    if cpu is not None:
        parts.append(f'CPU {cpu:.0f}%')
    if temp is not None:
        parts.append(f'{temp:.1f}C')
    if load is not None:
        parts.append(f'load {load:.2f}')
    if memory is not None:
        parts.append(f'RAM {memory:.0f}%')
    if disk is not None:
        parts.append(f'disk {disk:.0f}%')
    parts.append(f"net {_rate(metrics.get('network_rx_bps'))} down/"
                 f"{_rate(metrics.get('network_tx_bps'))} up")
    parts.append(_age_text(snapshot, now))
    return truncate_utf8(' | '.join(parts))


def _version_reply(snapshot, now=None):
    monitor = snapshot.get('monitor') or {}
    boot = _clean(snapshot.get('boot_id'))
    parts = [f"PIWATCH schema {snapshot.get('schema_version', '?')}",
             f"settings rev {monitor.get('settings_revision', '?')}",
             f'boot {boot[:8]}', f"uptime {_duration(monitor.get('uptime_seconds'))}",
             _age_text(snapshot, now)]
    if monitor.get('config_error'):
        parts.append(f"config error {_clean(monitor['config_error'])}")
    return truncate_utf8(' | '.join(parts))


def _radio_reply(info):
    if not isinstance(info, dict) or not info.get('connected'):
        return 'RADIO disconnected'
    parts = ['RADIO UP']
    frequency = _number(info.get('frequency_mhz'))
    if frequency is not None:
        parts.append(f'{frequency:.3f}MHz')
    if info.get('region'):
        parts.append(_clean(info['region']))
    if info.get('preset'):
        parts.append(_clean(info['preset']))
    bandwidth = _number(info.get('bandwidth_khz'))
    spread = _number(info.get('spread_factor'))
    if bandwidth is not None:
        parts.append(f'BW{bandwidth:g}')
    if spread is not None:
        parts.append(f'SF{spread:g}')
    last = info.get('last_rx') or {}
    rssi = _number(last.get('rssi'))
    snr = _number(last.get('snr'))
    if rssi is not None or snr is not None:
        signal = 'last RX'
        if rssi is not None:
            signal += f' {rssi:.0f}dBm'
        if snr is not None:
            signal += f' SNR {snr:+.1f}dB'
        if last.get('via_mqtt'):
            signal += ' MQTT'
        parts.append(signal)
    return truncate_utf8(' | '.join(parts))


def _node_summary(item):
    name = _clean(item.get('short_name') or item.get('long_name'), '?')
    result = f"{name} {node_id(item.get('num'))}"
    age = _number(item.get('age_seconds'))
    if age is not None:
        result += f' {_duration(age)} ago'
    return result


def _nodes_reply(info, requested):
    nodes = info.get('nodes') if isinstance(info, dict) else None
    nodes = nodes if isinstance(nodes, list) else []
    if not nodes:
        return 'NODES unavailable'
    page = _integer(requested, 1)
    page_size = 3
    start = (page - 1) * page_size
    selected = nodes[start:start + page_size]
    pages = max(1, math.ceil(len(nodes) / page_size))
    if not selected:
        return f'NODES page {page}/{pages} empty'
    return truncate_utf8(f'NODES {page}/{pages} | '
                         + ' | '.join(_node_summary(item) for item in selected))


def _node_reply(info, requested):
    if not requested.strip():
        return 'Usage: node ID (or exact short/long name)'
    wanted_num = node_number(requested)
    wanted_text = requested.strip().casefold()
    found = None
    for item in info.get('nodes', []) if isinstance(info, dict) else []:
        names = (str(item.get('short_name', '')).casefold(),
                 str(item.get('long_name', '')).casefold())
        if ((wanted_num is not None and wanted_num == node_number(item.get('num')))
                or wanted_text in names):
            found = item
            break
    if found is None:
        return f'Node {_clean(requested)} not found'
    parts = [f"NODE {_clean(found.get('long_name') or found.get('short_name'))}",
             node_id(found.get('num'))]
    age = _number(found.get('age_seconds'))
    if age is not None:
        parts.append(f'heard {_duration(age)} ago')
    snr = _number(found.get('snr'))
    rssi = _number(found.get('rssi'))
    if rssi is not None:
        parts.append(f'RSSI {rssi:.0f}dBm')
    if snr is not None:
        parts.append(f'SNR {snr:+.1f}dB')
    if found.get('hops') is not None:
        parts.append(f"hops {found['hops']}")
    if found.get('via_mqtt'):
        parts.append('via MQTT')
    return truncate_utf8(' | '.join(parts))


def parse_command(text, status_alias='status'):
    if not isinstance(text, str) or len(text.encode('utf-8')) > 80:
        return None
    normalized = ' '.join(text.strip().split())
    if not normalized:
        return None
    name, separator, argument = normalized.partition(' ')
    name = name.casefold()
    alias = status_alias.strip().casefold()
    if alias and name == alias:
        name = 'status'
    return name, argument if separator else ''


def build_command_replies(command, argument, snapshot, sender, gateway,
                          radio_info=None, now=None, cooldown=15):
    if command == 'help':
        replies = build_help_replies(sender, gateway, cooldown)
        page = argument.strip()
        if page:
            if page in ('1', '2', '3'):
                replies = (replies[int(page) - 1],)
            else:
                replies = ('Usage: help [1|2|3]',)
    elif command == 'ping':
        replies = (f'PONG | GW {node_id(gateway)} | PORTABLE {node_id(sender)} | '
                   f'{_age_text(snapshot, now) if snapshot else "PiWatch unavailable"}',)
    elif command == 'radio':
        replies = (_radio_reply(radio_info),)
    elif command == 'nodes':
        replies = (_nodes_reply(radio_info or {}, argument),)
    elif command == 'node':
        replies = (_node_reply(radio_info or {}, argument),)
    elif command not in COMMANDS:
        replies = ('Unknown command. Send help for the command list.',)
    elif not isinstance(snapshot, dict):
        replies = ('PiWatch status unavailable',)
    elif command == 'status':
        replies = (build_status_reply(snapshot, now),)
    elif command == 'power':
        replies = (_power_reply(snapshot, now),)
    elif command == 'problems':
        replies = (_problems_reply(snapshot, now),)
    elif command == 'check':
        replies = (_check_reply(snapshot, argument, now),)
    elif command == 'incidents':
        replies = (_incidents_reply(snapshot, argument),)
    elif command == 'incident':
        replies = (_incident_reply(snapshot, argument),)
    elif command == 'alerts':
        replies = (_alerts_reply(snapshot),)
    elif command == 'system':
        replies = (_system_reply(snapshot, now),)
    elif command == 'version':
        replies = (_version_reply(snapshot, now),)
    else:
        replies = ('Unknown command. Send help for the command list.',)
    snapshot_commands = {
        'power', 'problems', 'check', 'incidents', 'incident', 'alerts',
        'system', 'version',
    }
    if (command in snapshot_commands and isinstance(snapshot, dict)
            and _snapshot_stale(snapshot, now)):
        replies = tuple(f'STALE | {reply}' for reply in replies)
    return tuple(truncate_utf8(reply) for reply in replies)


class CommandResponder:
    """Keep Meshtastic callbacks short; one worker owns command reply sends."""

    def __init__(self, sender, allowed_senders, command='status', cooldown=15,
                 status_path=DEFAULT_STATUS_PATH, enabled=True, clock=time.monotonic,
                 radio_provider=None, page_delay=3):
        self.sender = sender
        self.allowed_senders = {number for value in allowed_senders
                                if (number := node_number(value)) is not None}
        self.command = command.strip().casefold()
        self.cooldown = max(1.0, float(cooldown))
        self.status_path = status_path
        self.enabled = bool(enabled) and bool(self.allowed_senders)
        self.clock = clock
        self.radio_provider = radio_provider or (lambda: {})
        self.page_delay = max(0.0, float(page_delay))
        self.requests = queue.Queue(maxsize=16)
        self.last_request = {}

    def enqueue(self, packet, recipient=None):
        if not self.enabled or not isinstance(packet, dict):
            return 'disabled'
        sender = node_number(packet.get('from'))
        if sender not in self.allowed_senders:
            return 'unauthorized'
        recipient = node_number(recipient)
        if recipient is None or node_number(packet.get('to')) != recipient:
            return 'ignored'
        parsed = parse_command(packet_text(packet), self.command)
        if parsed is None:
            return 'ignored'
        now = self.clock()
        if now - self.last_request.get(sender, -math.inf) < self.cooldown:
            return 'rate_limited'
        try:
            self.requests.put_nowait((sender, recipient, *parsed))
        except queue.Full:
            return 'busy'
        self.last_request[sender] = now
        return 'queued'

    def run(self, stop):
        while not stop.is_set():
            try:
                sender, gateway, command, argument = self.requests.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                snapshot = read_snapshot(self.status_path)
                radio_info = (self.radio_provider()
                              if command in ('radio', 'nodes', 'node') else None)
                replies = build_command_replies(
                    command, argument, snapshot, sender, gateway,
                    radio_info=radio_info, cooldown=self.cooldown)
                for index, message in enumerate(replies):
                    self.sender(message, sender)
                    if index + 1 < len(replies) and stop.wait(self.page_delay):
                        break
            except Exception as exc:
                print(f"[command] reply to !{sender:08x} failed: "
                      f"{type(exc).__name__}", flush=True)
            finally:
                self.requests.task_done()
