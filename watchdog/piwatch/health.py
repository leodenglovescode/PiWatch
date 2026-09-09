"""Read-only, bounded health collection, independent of the monitoring loop."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import time
import urllib.request

UNITS = ('piwatch', 'piwatch-oled', 'lora-notifyd', 'gpsd', 'chrony')


def boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return None


async def command(*args, timeout=2):
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, 'LC_ALL': 'C'},
        )
        output, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return proc.returncode, output.decode('utf-8', errors='replace')
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()


def system_health():
    load_1m, load_5m, load_15m = os.getloadavg()
    data = {'load_1m': load_1m, 'load_5m': load_5m, 'load_15m': load_15m}
    try:
        memory = {line.split(':')[0]: int(line.split()[1])
                  for line in Path('/proc/meminfo').read_text().splitlines() if ':' in line}
        data['memory_percent'] = round(100 * (1 - memory['MemAvailable'] / memory['MemTotal']), 1)
        data['uptime_seconds'] = float(Path('/proc/uptime').read_text().split()[0])
    except (OSError, ValueError, KeyError):
        pass
    try:
        data['temperature_c'] = float(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000
    except (OSError, ValueError):
        pass
    disk = shutil.disk_usage('/var/lib/piwatch' if Path('/var/lib/piwatch').exists() else '/')
    data['disk_percent'] = round(100 * (disk.total - disk.free) / disk.total, 1)
    data['state'] = 'DEGRADED' if (data['disk_percent'] >= 90 or
                                  data.get('temperature_c', 0) >= 80 or
                                  data.get('memory_percent', 0) >= 95) else 'UP'
    return data


def _cpu_times(path='/proc/stat'):
    fields = Path(path).read_text().splitlines()[0].split()[1:]
    values = [int(value) for value in fields]
    return sum(values), values[3] + (values[4] if len(values) > 4 else 0)


def _network_bytes(path='/proc/net/dev'):
    received = transmitted = 0
    for line in Path(path).read_text().splitlines()[2:]:
        interface, _, raw = line.partition(':')
        if not raw or interface.strip() == 'lo':
            continue
        fields = raw.split()
        received += int(fields[0])
        transmitted += int(fields[8])
    return received, transmitted


class SystemSampler:
    """Collect lightweight one-second metrics without an external dependency."""

    def __init__(self):
        self.previous_cpu = None
        self.previous_network = None
        self.previous_time = None

    def sample(self):
        now = time.monotonic()
        total, idle = _cpu_times()
        received, transmitted = _network_bytes()
        data = system_health()
        data['checked_at'] = time.time()
        if self.previous_cpu is not None:
            total_delta = total - self.previous_cpu[0]
            idle_delta = idle - self.previous_cpu[1]
            if total_delta > 0:
                data['cpu_percent'] = round(100 * (1 - idle_delta / total_delta), 1)
        if self.previous_network is not None and self.previous_time is not None:
            elapsed = now - self.previous_time
            if elapsed > 0:
                data['network_rx_bps'] = max(0, round((received - self.previous_network[0]) / elapsed))
                data['network_tx_bps'] = max(0, round((transmitted - self.previous_network[1]) / elapsed))
        data['network_rx_bytes'] = received
        data['network_tx_bytes'] = transmitted
        self.previous_cpu = total, idle
        self.previous_network = received, transmitted
        self.previous_time = now
        return data


async def service_health():
    code, output = await command('systemctl', 'is-active', *UNITS)
    states = output.splitlines()
    if len(states) != len(UNITS):
        return {'state': 'UNKNOWN'}
    units = dict(zip(UNITS, states))
    return {'state': 'UP' if all(s == 'active' for s in states) else 'DEGRADED', 'units': units}


async def time_health():
    code, output = await command('chronyc', '-n', 'tracking')
    if code:
        return {'state': 'UNKNOWN'}
    fields = {key.strip(): value.strip() for key, sep, value in
              (line.partition(':') for line in output.splitlines()) if sep}
    leap = fields.get('Leap status', '')
    return {'state': 'UP' if leap in ('Normal', 'Insert second', 'Delete second') else 'DEGRADED',
            'source': fields.get('Reference ID'), 'stratum': fields.get('Stratum'),
            'offset': fields.get('System time'), 'leap_status': leap}


async def gps_health():
    """Read fresh TPV/SKY reports through gpsd; never open the GPS serial port."""
    writer = None
    data = {'state': 'UNKNOWN', 'mode': 0, 'seen': None, 'used': None}
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', 2947), 1)
        writer.write(b'?WATCH={"enable":true,"json":true};\n')
        await writer.drain()
        deadline = time.monotonic() + 2
        have_tpv = False
        have_satellites = False
        while time.monotonic() < deadline:
            line = await asyncio.wait_for(reader.readline(), max(.01, deadline - time.monotonic()))
            if not line:
                break
            try:
                report = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(report, dict):
                continue
            kind = report.get('class')
            if kind == 'TPV':
                have_tpv = True
                data['mode'] = report.get('mode', 0)
                data['state'] = 'UP' if data['mode'] >= 2 else 'DEGRADED'
                data['report_at'] = time.time()
            elif kind == 'SKY':
                satellites = report.get('satellites')
                if 'nSat' in report or 'uSat' in report or isinstance(satellites, list):
                    satellites = satellites if isinstance(satellites, list) else []
                    data['seen'] = report.get('nSat', len(satellites))
                    data['used'] = report.get('uSat', sum(bool(s.get('used')) for s in satellites))
                    data['satellites_at'] = time.time()
                    have_satellites = True
            if have_tpv and have_satellites:
                break
        if data['mode'] >= 2 and not have_satellites:
            data['state'] = 'DEGRADED'
            data['detail'] = 'Satellite report unavailable'
    except (OSError, asyncio.TimeoutError):
        pass
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), .5)
            except (OSError, asyncio.TimeoutError):
                pass
    return data


def radio_health(url):
    # Do not forward the bearer token to this read-only endpoint.
    with urllib.request.urlopen(url.rstrip('/') + '/health', timeout=2) as response:
        report = json.loads(response.read(8192))
    return {'state': 'UP' if report.get('ok') and report.get('radio_connected') else 'DEGRADED',
            'radio_connected': bool(report.get('radio_connected'))}


async def collect_health(radio_url):
    async def collect(name, coroutine):
        try:
            result = await asyncio.wait_for(coroutine, 4)
        except Exception as exc:
            result = {'state': 'UNKNOWN', 'error': type(exc).__name__}
        return name, {**result, 'checked_at': time.time()}
    return dict(await asyncio.gather(
        collect('system', asyncio.to_thread(system_health)),
        collect('services', service_health()),
        collect('time', time_health()),
        collect('gps', gps_health()),
        collect('radio', asyncio.to_thread(radio_health, radio_url)),
    ))


async def health_worker(snapshot, stop, current):
    from .notifications.settings import LORA_API_URL
    while not stop.is_set():
        revision = current['revision']
        config = current['config']
        radio_url = config['gateways']['lora'].get('url', LORA_API_URL)
        snapshot.update(await collect_health(radio_url))
        snapshot['checked_monotonic'] = time.monotonic()
        deadline = time.monotonic() + config['health_interval_seconds']
        while not stop.is_set() and time.monotonic() < deadline and current['revision'] == revision:
            try:
                await asyncio.wait_for(stop.wait(), min(1, max(.01, deadline - time.monotonic())))
            except asyncio.TimeoutError:
                pass
