"""Publish the public PiWatch snapshot through Home Assistant MQTT Discovery."""
import json
import math
import os
from pathlib import Path
import signal
import threading
import time

from .config import MQTT_SENSOR_IDS
from .config_store import SettingsStore


STATUS_PATH = os.environ.get('PIWATCH_STATUS_PATH', '/run/piwatch/status.json')
BRIDGE_STATUS_PATH = os.environ.get(
    'PIWATCH_MQTT_STATUS_PATH', '/run/piwatch-ha-mqtt/status.json')
DEVICE_ID = 'piwatch_watchdog_pi'

SENSORS = {
    'cpu_temperature': {
        'component': 'sensor', 'name': 'CPU temperature', 'key': 'cpu_temperature_c',
        'device_class': 'temperature', 'unit_of_measurement': '°C',
        'state_class': 'measurement', 'icon': 'mdi:thermometer',
    },
    'cpu_usage': {
        'component': 'sensor', 'name': 'CPU usage', 'key': 'cpu_usage_percent',
        'unit_of_measurement': '%', 'state_class': 'measurement',
        'icon': 'mdi:cpu-64-bit',
    },
    'memory_usage': {
        'component': 'sensor', 'name': 'Memory usage', 'key': 'memory_usage_percent',
        'unit_of_measurement': '%', 'state_class': 'measurement',
        'icon': 'mdi:memory',
    },
    'disk_usage': {
        'component': 'sensor', 'name': 'Disk usage', 'key': 'disk_usage_percent',
        'unit_of_measurement': '%', 'state_class': 'measurement',
        'icon': 'mdi:harddisk',
    },
    'load_1m': {
        'component': 'sensor', 'name': 'Load average 1 minute', 'key': 'load_1m',
        'state_class': 'measurement', 'icon': 'mdi:gauge',
    },
    'network_rx': {
        'component': 'sensor', 'name': 'Network receive rate', 'key': 'network_rx_bps',
        'device_class': 'data_rate', 'unit_of_measurement': 'B/s',
        'state_class': 'measurement', 'icon': 'mdi:download-network-outline',
    },
    'network_tx': {
        'component': 'sensor', 'name': 'Network transmit rate', 'key': 'network_tx_bps',
        'device_class': 'data_rate', 'unit_of_measurement': 'B/s',
        'state_class': 'measurement', 'icon': 'mdi:upload-network-outline',
    },
    'uptime': {
        'component': 'sensor', 'name': 'System uptime', 'key': 'uptime_seconds',
        'device_class': 'duration', 'unit_of_measurement': 's',
        'state_class': 'total_increasing', 'icon': 'mdi:timer-outline',
    },
    'battery_voltage': {
        'component': 'sensor', 'name': 'UPS battery voltage', 'key': 'battery_voltage_v',
        'device_class': 'voltage', 'unit_of_measurement': 'V',
        'state_class': 'measurement', 'icon': 'mdi:battery',
    },
    'mains_power': {
        'component': 'binary_sensor', 'name': 'Mains power', 'key': 'mains_power',
        'device_class': 'power', 'icon': 'mdi:transmission-tower',
    },
    'ups_state': {
        'component': 'sensor', 'name': 'UPS supervisor state', 'key': 'ups_state',
        'icon': 'mdi:shield-battery-outline',
    },
    'monitor_state': {
        'component': 'sensor', 'name': 'Monitor state', 'key': 'monitor_state',
        'icon': 'mdi:heart-pulse',
    },
    'data_fresh': {
        'component': 'binary_sensor', 'name': 'Data fresh', 'key': 'data_fresh',
        'device_class': 'connectivity', 'icon': 'mdi:database-clock-outline',
        'health': True,
    },
    'snapshot_age': {
        'component': 'sensor', 'name': 'Snapshot age', 'key': 'snapshot_age_seconds',
        'device_class': 'duration', 'unit_of_measurement': 's',
        'state_class': 'measurement', 'icon': 'mdi:clock-outline', 'health': True,
    },
    'notifications_pending': {
        'component': 'sensor', 'name': 'Notifications pending',
        'key': 'notifications_pending', 'state_class': 'measurement',
        'icon': 'mdi:message-processing-outline',
    },
    'notifications_failed': {
        'component': 'sensor', 'name': 'Notifications failed',
        'key': 'notifications_failed', 'state_class': 'measurement',
        'icon': 'mdi:message-alert-outline',
    },
}

if set(SENSORS) != set(MQTT_SENSOR_IDS):
    raise RuntimeError('MQTT sensor definitions do not match validated settings')


def finite(value):
    return (float(value) if isinstance(value, (int, float)) and
            not isinstance(value, bool) and math.isfinite(value) else None)


def read_snapshot(path=STATUS_PATH):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) and value.get('schema_version') == 2 else None


def snapshot_age(snapshot, now=None):
    generated = finite(snapshot.get('generated_at')) if isinstance(snapshot, dict) else None
    if generated is None:
        return None
    age = (time.time() if now is None else now) - generated
    return None if age < -5 else max(0.0, age)


def snapshot_is_fresh(snapshot, now=None):
    age = snapshot_age(snapshot, now)
    stale_after = finite(snapshot.get('stale_after_seconds')) if isinstance(snapshot, dict) else None
    return age is not None and age <= (stale_after or 10)


def _notification_total(snapshot, field):
    values = snapshot.get('notifications') or {}
    return sum(int(value.get(field, 0)) for value in values.values()
               if isinstance(value, dict) and isinstance(value.get(field, 0), int))


def metric_state(snapshot):
    """Flatten stable public fields into one bounded Home Assistant state object."""
    metrics = snapshot.get('metrics') or {}
    targets = snapshot.get('targets') or {}
    battery = targets.get('battery') or {}
    mains = targets.get('mains') or {}
    supervisor = snapshot.get('ups_supervisor') or {}
    battery_voltage = finite(battery.get('voltage_v'))
    if battery_voltage is None:
        battery_voltage = finite(supervisor.get('battery_voltage_v'))
    outage = mains.get('outage')
    mains_power = ('OFF' if outage is True else 'ON' if outage is False else
                   'ON' if str(mains.get('state', '')).upper() == 'UP' else
                   'OFF' if str(mains.get('state', '')).upper() == 'DOWN' else None)
    state = {
        'generated_at': snapshot.get('generated_at'),
        'cpu_temperature_c': finite(metrics.get('temperature_c')),
        'cpu_usage_percent': finite(metrics.get('cpu_percent')),
        'memory_usage_percent': finite(metrics.get('memory_percent')),
        'disk_usage_percent': finite(metrics.get('disk_percent')),
        'load_1m': finite(metrics.get('load_1m')),
        'network_rx_bps': finite(metrics.get('network_rx_bps')),
        'network_tx_bps': finite(metrics.get('network_tx_bps')),
        'uptime_seconds': finite(metrics.get('uptime_seconds')),
        'battery_voltage_v': battery_voltage,
        'mains_power': mains_power,
        'ups_state': str(supervisor.get('state', 'UNKNOWN')).upper(),
        'monitor_state': str((snapshot.get('monitor') or {}).get('state', 'UNKNOWN')).upper(),
        'notifications_pending': _notification_total(snapshot, 'pending'),
        'notifications_failed': _notification_total(snapshot, 'failed'),
    }
    return state


def health_state(snapshot, now=None):
    age = snapshot_age(snapshot, now)
    return {
        'data_fresh': 'ON' if snapshot_is_fresh(snapshot, now) else 'OFF',
        'snapshot_age_seconds': round(age, 1) if age is not None else None,
    }


def discovery_messages(settings):
    """Return every discovery topic so disabled entities can be removed cleanly."""
    enabled = set(settings['sensors'])
    base = settings['topic_prefix']
    discovery = settings['discovery_prefix']
    device = {
        'identifiers': [DEVICE_ID], 'name': 'PiWatch Watchdog Pi',
        'manufacturer': 'PiWatch', 'model': 'Raspberry Pi watchdog',
    }
    messages = []
    for sensor_id, definition in SENSORS.items():
        component = definition['component']
        topic = f'{discovery}/{component}/{DEVICE_ID}/{sensor_id}/config'
        if sensor_id not in enabled:
            messages.append((topic, ''))
            continue
        payload = {
            'name': definition['name'],
            'unique_id': f'{DEVICE_ID}_{sensor_id}',
            'default_entity_id': f'{component}.{DEVICE_ID}_{sensor_id}',
            'state_topic': f"{base}/{'health' if definition.get('health') else 'state'}",
            'value_template': '{{ value_json.' + definition['key'] + ' }}',
            'expire_after': settings['expire_after_seconds'],
            'force_update': False,
            'device': device,
        }
        for key in ('device_class', 'unit_of_measurement', 'state_class', 'icon'):
            if key in definition:
                payload[key] = definition[key]
        if component == 'binary_sensor':
            payload.update(payload_on='ON', payload_off='OFF')
        messages.append((topic, json.dumps(payload, separators=(',', ':'))))
    return messages


def write_bridge_status(value, path=BRIDGE_STATUS_PATH):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.tmp')
    temporary.write_text(json.dumps(value, separators=(',', ':')), encoding='utf-8')
    os.chmod(temporary, 0o644)
    os.replace(temporary, target)


class Publisher:
    """Own one Paho client; the bridge loop controls configuration changes."""

    def __init__(self, settings, mqtt_module):
        self.settings = settings
        self.mqtt = mqtt_module
        self.connected = threading.Event()
        self.discovery_needed = threading.Event()
        self.error = None
        self.client = mqtt_module.Client(
            mqtt_module.CallbackAPIVersion.VERSION2,
            client_id=DEVICE_ID, protocol=mqtt_module.MQTTv311)
        if settings['username']:
            self.client.username_pw_set(settings['username'], settings['password'] or None)
        if settings['tls']:
            self.client.tls_set()
        self.client.will_set(
            f"{settings['topic_prefix']}/availability", 'offline', qos=1, retain=True)
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            self.error = None
            self.connected.set()
            self.discovery_needed.set()
        else:
            self.error = f'Connection rejected: {reason_code}'
            self.connected.clear()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self.connected.clear()
        if reason_code != 0:
            self.error = f'Connection lost: {reason_code}'

    def start(self):
        self.client.connect_async(
            self.settings['host'], self.settings['port'], keepalive=30)
        self.client.loop_start()

    def stop(self):
        self.client.disconnect()
        self.client.loop_stop()
        self.connected.clear()

    def publish(self, topic, payload, retain=True):
        result = self.client.publish(topic, payload, qos=1, retain=retain)
        if result.rc != self.mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f'MQTT publish failed with code {result.rc}')

    def publish_discovery(self):
        for topic, payload in discovery_messages(self.settings):
            self.publish(topic, payload)
        self.publish(f"{self.settings['topic_prefix']}/availability", 'online')
        self.discovery_needed.clear()


class Bridge:
    def __init__(self, store=None, snapshot_path=STATUS_PATH,
                 bridge_status_path=BRIDGE_STATUS_PATH, mqtt_module=None):
        self.store = store or SettingsStore()
        self.snapshot_path = snapshot_path
        self.bridge_status_path = bridge_status_path
        self.mqtt_module = mqtt_module
        self.publisher = None
        self.settings = None
        self.revision = None
        self.next_publish = 0.0
        self.last_publish_at = None

    def _status(self, state, error=None, snapshot=None):
        write_bridge_status({
            'state': state, 'connected': bool(self.publisher and self.publisher.connected.is_set()),
            'settings_revision': self.revision, 'last_publish_at': self.last_publish_at,
            'snapshot_age_seconds': snapshot_age(snapshot), 'error': error,
            'generated_at': time.time(),
        }, self.bridge_status_path)

    def _replace_publisher(self, settings):
        if self.publisher:
            self.publisher.stop()
            self.publisher = None
        self.settings = settings
        self.next_publish = 0.0
        if not settings['enabled']:
            self._status('DISABLED')
            return
        mqtt_module = self.mqtt_module
        if mqtt_module is None:
            import paho.mqtt.client as mqtt_module
        self.publisher = Publisher(settings, mqtt_module)
        self.publisher.start()
        self._status('CONNECTING')

    def step(self, now=None):
        now = time.monotonic() if now is None else now
        try:
            revision, config = self.store.read()
            settings = config['mqtt']
        except Exception as exc:
            self._status('ERROR', f'Settings unavailable: {type(exc).__name__}')
            return
        if settings != self.settings:
            self.revision = revision
            try:
                self._replace_publisher(settings)
            except Exception as exc:
                self.settings = None
                self._status('ERROR', f'MQTT setup failed: {type(exc).__name__}')
            return
        self.revision = revision
        if not self.publisher:
            return
        if not self.publisher.connected.is_set():
            self._status('CONNECTING', self.publisher.error)
            return
        try:
            if self.publisher.discovery_needed.is_set():
                self.publisher.publish_discovery()
            if now < self.next_publish:
                return
            snapshot = read_snapshot(self.snapshot_path)
            health = health_state(snapshot)
            self.publisher.publish(
                f"{settings['topic_prefix']}/health",
                json.dumps(health, separators=(',', ':')))
            if snapshot_is_fresh(snapshot):
                self.publisher.publish(
                    f"{settings['topic_prefix']}/state",
                    json.dumps(metric_state(snapshot), separators=(',', ':')))
            self.last_publish_at = time.time()
            self.next_publish = now + settings['publish_interval_seconds']
            self._status('UP', snapshot=snapshot)
        except Exception as exc:
            self._status('ERROR', f'Publish failed: {type(exc).__name__}')

    def close(self):
        if self.publisher:
            self.publisher.stop()
            self.publisher = None

    def run(self, stop):
        while not stop.is_set():
            self.step()
            stop.wait(1)
        self.close()


def main():
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    bridge = Bridge()
    try:
        bridge.run(stop)
    finally:
        bridge.close()
