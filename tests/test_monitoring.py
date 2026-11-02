"""Hardware-free regressions for configuration, history, and freshness."""
import asyncio
import copy
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'watchdog'))
from piwatch import checks, daemon, mqtt_bridge
from piwatch.battery import BatteryMonitor, estimated_battery_percentage, read_ina219
from piwatch.config import validate_config
from piwatch.config_store import SettingsStore, ConflictError
from piwatch.mains import MainsMonitor
from piwatch.notifications import outbox
from piwatch.notifications.messages import build_lora_messages, build_message
from piwatch import ups_supervisor
from piwatch.ups_supervisor import LowBatteryShutdown, PicoSupervisor
from piwatch.state import StateTracker


def config():
    return validate_config({'targets': [{'id': 'router', 'name': 'Router', 'host': '192.0.2.1',
                                        'enabled': True, 'checks': [{'type': 'ping'}]}],
                            'battery': {'enabled': False, 'mains_detection_enabled': False}})


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SettingsStore(Path(self.temp.name) / 'settings.sqlite3')
        self.store.initialize(config())

    def tearDown(self):
        self.temp.cleanup()

    def test_secrets_are_write_only_and_omission_preserves_them(self):
        revision, value = self.store.read()
        value['gateways']['smtp']['password'] = 'synthetic-smtp-password'
        self.store.save(value, revision, 'owner')
        public = self.store.public()
        self.assertNotIn('synthetic-smtp-password', json.dumps(public))
        self.assertTrue(public['configured_secrets']['smtp']['password'])
        public['config']['targets'][0]['name'] = 'Renamed'
        self.store.save(public['config'], public['revision'], 'owner')
        self.assertEqual(self.store.read()[1]['gateways']['smtp']['password'], 'synthetic-smtp-password')
        public = self.store.public()
        public['config']['gateways']['smtp']['password'] = ''
        self.store.save(public['config'], public['revision'], 'owner')
        self.assertEqual(self.store.read()[1]['gateways']['smtp']['password'], '')

    def test_mqtt_password_is_write_only_and_preserved_when_omitted(self):
        revision, value = self.store.read()
        value['mqtt']['password'] = 'synthetic-mqtt-password'
        self.store.save(value, revision, 'owner')
        public = self.store.public()
        self.assertTrue(public['configured_secrets']['mqtt']['password'])
        self.assertNotIn('synthetic-mqtt-password', json.dumps(public))
        public['config']['mqtt']['enabled'] = True
        self.store.save(public['config'], public['revision'], 'owner')
        self.assertEqual(self.store.read()[1]['mqtt']['password'],
                         'synthetic-mqtt-password')

    def test_revision_conflicts_do_not_overwrite_newer_changes(self):
        revision, value = self.store.read()
        self.store.save(value, revision, 'first')
        with self.assertRaises(ConflictError):
            self.store.save(value, revision, 'second')
        self.assertEqual(self.store.read()[0], 2)

    def test_invalid_edits_are_atomic(self):
        revision, value = self.store.read()
        value['targets'][0]['checks'] = [{'type': 'tcp', 'port': 70000}]
        with self.assertRaises(ValueError):
            self.store.save(value, revision, 'owner')
        self.assertEqual(self.store.read(), (revision, config()))

    def test_legacy_config_migrates_without_credentials(self):
        value = validate_config({'router': {'host': '192.0.2.1'},
                                 'super_server': {'host': '192.0.2.2', 'ssh_port': 22},
                                 'home_assistant': {'host': '192.0.2.3', 'port': 8123, 'path': '/'}})
        self.assertEqual([t['id'] for t in value['targets']], ['router','super_server','home_assistant'])
        self.assertEqual(value['gateways']['smtp'], {'enabled': True})

    def test_battery_defaults_and_thresholds_are_validated(self):
        value = validate_config({'targets': []})
        self.assertEqual((value['battery']['bus'], value['battery']['address']), (3, 0x40))
        self.assertEqual((value['battery']['low_voltage_v'], value['battery']['recover_voltage_v']),
                         (3.3, 3.5))
        self.assertEqual(value['battery']['mains_debounce_seconds'], 3.0)
        value['battery']['recover_voltage_v'] = 3.31
        with self.assertRaisesRegex(ValueError, 'at least 0.04 V'):
            validate_config(value)
        value = validate_config({'targets': []})
        value['battery']['mains_debounce_seconds'] = 1.9
        with self.assertRaisesRegex(ValueError, 'between 2 and 3 seconds'):
            validate_config(value)

    def test_mqtt_defaults_and_expiry_are_validated(self):
        value = validate_config({'targets': []})
        self.assertEqual(value['mqtt']['host'], 'localhost')
        self.assertEqual(value['mqtt']['publish_interval_seconds'], 1.0)
        self.assertEqual(value['mqtt']['expire_after_seconds'], 30)
        self.assertEqual(value['mqtt']['sensor_schema_version'], 2)
        value['mqtt']['expire_after_seconds'] = 14
        with self.assertRaisesRegex(ValueError, 'integer from 15'):
            validate_config(value)
        value = validate_config({'targets': []})
        value['mqtt']['topic_prefix'] = 'piwatch/#'
        with self.assertRaisesRegex(ValueError, 'without wildcards'):
            validate_config(value)

    def test_old_mqtt_defaults_migrate_once(self):
        value = validate_config({'targets': [], 'mqtt': {
            'sensors': ['cpu_usage'], 'publish_interval_seconds': 5}})
        self.assertEqual(value['mqtt']['sensors'], ['cpu_usage', 'battery_percentage'])
        self.assertEqual(value['mqtt']['publish_interval_seconds'], 1.0)
        value['mqtt']['sensors'].remove('battery_percentage')
        value['mqtt']['publish_interval_seconds'] = 5
        migrated = validate_config(value)
        self.assertNotIn('battery_percentage', migrated['mqtt']['sensors'])
        self.assertEqual(migrated['mqtt']['publish_interval_seconds'], 5.0)

    def test_old_mains_inference_settings_are_removed_during_migration(self):
        value = validate_config({'targets': [], 'battery': {
            'mains_drop_v': .05, 'mains_recover_rise_v': .05,
            'mains_window_seconds': 10, 'discharge_polarity': 'auto',
            'discharge_shunt_mv': .5}})
        self.assertEqual(value['battery']['mains_debounce_seconds'], 3.0)
        self.assertFalse({'mains_drop_v', 'discharge_polarity'} & value['battery'].keys())

    def test_maintenance_requests_are_durable_and_report_completion(self):
        request_id = self.store.request_maintenance('clear_deliveries', 'owner')
        self.assertEqual(self.store.pending_maintenance(), [
            {'id': request_id, 'action': 'clear_deliveries', 'actor': 'owner'}])
        self.store.finish_maintenance(request_id, {'deliveries': 4, 'incidents': 0})
        self.assertEqual(self.store.pending_maintenance(), [])
        status = self.store.maintenance_status(request_id)
        self.assertEqual(status['status'], 'complete')
        self.assertEqual(status['result']['deliveries'], 4)
        with self.assertRaisesRegex(ValueError, 'Unknown maintenance action'):
            self.store.request_maintenance('reset_everything', 'owner')


class MqttBridgeTests(unittest.TestCase):
    class FakeMqtt:
        class CallbackAPIVersion:
            VERSION2 = 2
        MQTTv311 = 4
        MQTT_ERR_SUCCESS = 0
        clients = []

        class Client:
            def __init__(self, *args, **kwargs):
                self.messages = []
                self.on_connect = None
                self.on_disconnect = None
                MqttBridgeTests.FakeMqtt.clients.append(self)

            def username_pw_set(self, *args):
                self.auth = args

            def tls_set(self):
                self.tls = True

            def will_set(self, *args, **kwargs):
                self.will = args, kwargs

            def reconnect_delay_set(self, **kwargs):
                pass

            def connect_async(self, *args, **kwargs):
                self.connection = args, kwargs

            def loop_start(self):
                self.on_connect(self, None, None, 0, None)

            def loop_stop(self):
                pass

            def disconnect(self):
                pass

            def publish(self, topic, payload, qos, retain):
                self.messages.append((topic, payload, qos, retain))
                return types.SimpleNamespace(rc=0)

    def snapshot(self, generated_at=None):
        return {
            'schema_version': 2, 'generated_at': generated_at or time.time(),
            'stale_after_seconds': 10,
            'monitor': {'state': 'UP'},
            'metrics': {
                'temperature_c': 48.25, 'cpu_percent': 17.5,
                'memory_percent': 31.2, 'disk_percent': 22.0, 'load_1m': .4,
                'network_rx_bps': 2048, 'network_tx_bps': 1024,
                'uptime_seconds': 3600,
            },
            'targets': {
                'battery': {'state': 'UP', 'voltage_v': 4.112},
                'mains': {'state': 'UP'},
            },
            'ups_supervisor': {'state': 'IDLE'},
            'notifications': {
                'lora': {'pending': 1, 'failed': 0},
                'smtp': {'pending': 0, 'failed': 2},
            },
        }

    def test_discovery_uses_delayed_expiry_without_immediate_availability(self):
        settings = validate_config({'targets': []})['mqtt']
        messages = dict(mqtt_bridge.discovery_messages(settings))
        topic = 'homeassistant/sensor/piwatch_watchdog_pi/cpu_temperature/config'
        payload = json.loads(messages[topic])
        self.assertEqual(payload['expire_after'], 30)
        self.assertEqual(payload['state_topic'], 'piwatch/watchdog-pi/state')
        self.assertNotIn('availability_topic', payload)
        self.assertEqual(payload['device']['identifiers'], ['piwatch_watchdog_pi'])
        battery_topic = ('homeassistant/sensor/piwatch_watchdog_pi/'
                         'battery_percentage/config')
        battery = json.loads(messages[battery_topic])
        self.assertEqual(battery['name'], 'Estimated battery')
        self.assertEqual(battery['device_class'], 'battery')

    def test_estimated_battery_percentage_is_bounded_and_piecewise(self):
        self.assertIsNone(estimated_battery_percentage(None))
        self.assertEqual(estimated_battery_percentage(2.9), 0)
        self.assertEqual(estimated_battery_percentage(3.8), 60)
        self.assertEqual(estimated_battery_percentage(4.112), 96)
        self.assertEqual(estimated_battery_percentage(4.3), 100)

    def test_network_rates_are_converted_to_megabits_per_second(self):
        state = mqtt_bridge.metric_state(self.snapshot())
        self.assertEqual(state['network_rx_mbps'], 0.016)
        self.assertEqual(state['network_tx_mbps'], 0.008)

    def test_bridge_publishes_fresh_metrics_and_stops_refreshing_stale_values(self):
        self.FakeMqtt.clients.clear()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SettingsStore(root / 'settings.sqlite3')
            value = config()
            value['mqtt']['enabled'] = True
            value['mqtt']['username'] = 'piwatch'
            value['mqtt']['password'] = 'synthetic-password'
            store.initialize(value)
            status_path = root / 'status.json'
            bridge_status_path = root / 'bridge.json'
            status_path.write_text(json.dumps(self.snapshot()))
            bridge = mqtt_bridge.Bridge(
                store, status_path, bridge_status_path, self.FakeMqtt)
            bridge.step(now=0)
            bridge.step(now=1)
            client = self.FakeMqtt.clients[-1]
            state_topic = 'piwatch/watchdog-pi/state'
            state_messages = [message for message in client.messages
                              if message[0] == state_topic]
            self.assertEqual(len(state_messages), 1)
            state = json.loads(state_messages[0][1])
            self.assertEqual(state['battery_voltage_v'], 4.112)
            self.assertEqual(state['battery_percentage'], 96)
            self.assertEqual(state['network_rx_mbps'], 0.016)
            self.assertEqual(state['mains_power'], 'ON')
            self.assertEqual(state['notifications_failed'], 2)
            self.assertTrue(state_messages[0][3])

            status_path.write_text(json.dumps(
                self.snapshot(generated_at=time.time() - 20)))
            bridge.step(now=7)
            self.assertEqual(len([message for message in client.messages
                                 if message[0] == state_topic]), 1)
            health = json.loads([message for message in client.messages
                                if message[0] == 'piwatch/watchdog-pi/health'][-1][1])
            self.assertEqual(health['data_fresh'], 'OFF')
            self.assertEqual(json.loads(bridge_status_path.read_text())['state'], 'UP')
            bridge.close()


class BatteryTests(unittest.TestCase):
    def settings(self, **updates):
        value = validate_config({'targets': []})['battery']
        value.update(updates)
        return value

    def test_low_voltage_uses_recovery_hysteresis(self):
        readings = iter((3.60, 3.29, 3.40, 3.51))
        monitor = BatteryMonitor(reader=lambda *_: {
            'voltage_v': next(readings)})
        states = [monitor.sample(self.settings())['healthy'] for _ in range(4)]
        self.assertEqual(states, [True, False, False, True])

    def test_sample_includes_estimated_battery_percentage(self):
        monitor = BatteryMonitor(reader=lambda *_: {'voltage_v': 3.8})
        self.assertEqual(monitor.sample(self.settings())['estimated_percentage'], 60)

    def test_ina219_bus_voltage_register_is_big_endian(self):
        with patch('piwatch.battery.os.open', return_value=7), \
             patch('piwatch.battery.fcntl.ioctl'), \
             patch('piwatch.battery.os.write', return_value=1), \
             patch('piwatch.battery.os.read', return_value=bytes.fromhex('1c12')), \
             patch('piwatch.battery.os.close'):
            reading = read_ina219(3, 0x40)
        self.assertEqual(reading, {'voltage_v': 3.592})

    def test_unavailable_sensor_does_not_create_a_failure_sample(self):
        def unavailable(*_):
            raise PermissionError()
        sample = BatteryMonitor(reader=unavailable).sample(self.settings())
        self.assertFalse(sample['sampled'])


class PowerSupervisorTests(unittest.TestCase):
    @staticmethod
    def battery(voltage=3.05, available=True, sampled=True):
        return {'voltage_v': voltage, 'available': available, 'sampled': sampled}

    def test_requires_thirty_continuous_seconds_of_valid_low_voltage(self):
        now = [0.0]
        monitor = LowBatteryShutdown(clock=lambda: now[0])
        for second in range(30):
            now[0] = float(second)
            self.assertFalse(monitor.update(False, self.battery()))
        now[0] = 30.0
        self.assertTrue(monitor.update(False, self.battery()))
        self.assertTrue(monitor.latched)
        now[0] = 31.0
        self.assertFalse(monitor.update(False, self.battery()))

    def test_mains_recovery_invalid_reading_and_voltage_recovery_reset_timer(self):
        now = [0.0]
        monitor = LowBatteryShutdown(clock=lambda: now[0])
        self.assertFalse(monitor.update(False, self.battery()))
        for reset_sample, mains in (
                (self.battery(available=False, sampled=False), False),
                (self.battery(3.051), False),
                (self.battery(), True)):
            now[0] += 20
            self.assertFalse(monitor.update(mains, reset_sample))
            self.assertIsNone(monitor.low_since)
            now[0] += 1
            self.assertFalse(monitor.update(False, self.battery()))
        now[0] += 29.9
        self.assertFalse(monitor.update(False, self.battery()))
        now[0] += 0.1
        self.assertTrue(monitor.update(False, self.battery()))

    def test_latched_supervisor_still_observes_restored_mains(self):
        monitor = LowBatteryShutdown()
        monitor.latched = True
        monitor.action_state = 'POWEROFF_FAILED'
        self.assertFalse(monitor.update(True, self.battery(3.2)))
        self.assertTrue(monitor.mains_present)
        self.assertEqual(monitor.last_voltage_v, 3.2)

    def test_gpio2_pulses_only_low_and_returns_to_input(self):
        durations = []
        requests = []

        class Request:
            def __init__(self):
                self.reconfigured = []
                self.released = False

            def reconfigure_lines(self, settings):
                self.reconfigured.append(settings)

            def release(self):
                self.released = True

        def request_lines(chip, **kwargs):
            request = Request()
            requests.append((chip, kwargs, request))
            return request

        direction = types.SimpleNamespace(OUTPUT='output', INPUT='input')
        value = types.SimpleNamespace(INACTIVE='low')
        fake_gpiod = types.SimpleNamespace(
            LineSettings=lambda **kwargs: kwargs, request_lines=request_lines)
        fake_line = types.SimpleNamespace(Direction=direction, Value=value)
        with patch.dict(sys.modules, {'gpiod': fake_gpiod, 'gpiod.line': fake_line}):
            supervisor = PicoSupervisor(sleeper=durations.append)
            supervisor.disarm()
            supervisor.arm()

        self.assertEqual(durations, [0.150, 1.0])
        self.assertEqual([request[0] for request in requests], ['/dev/gpiochip0'] * 2)
        for _, kwargs, request in requests:
            self.assertEqual(kwargs['config'][2], {
                'direction': 'output', 'output_value': 'low'})
            self.assertEqual(request.reconfigured, [{2: {'direction': 'input'}}])
            self.assertTrue(request.released)

    def test_poweroff_uses_only_the_authorized_noninteractive_command(self):
        with patch.object(ups_supervisor.subprocess, 'run') as run:
            ups_supervisor.request_poweroff()
        run.assert_called_once_with(
            ups_supervisor.POWEROFF_COMMAND, check=True, timeout=10)

    def test_power_input_lock_excludes_a_second_process_handle(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'power-input.lock'
            with ups_supervisor.power_input_lock(path):
                descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(descriptor)

    def test_dry_run_can_use_a_temporary_threshold_without_side_effects(self):
        now = [0.0]
        output = []

        def clock():
            return now[0]

        def sleeper(seconds):
            now[0] += seconds

        battery = self.battery(3.80)
        mains = {'available': True, 'gpio_level': False}
        with patch.object(ups_supervisor, 'live_sample',
                          return_value=(battery, mains)):
            passed = ups_supervisor.dry_run(
                {}, 4.0, qualification_seconds=2, timeout_seconds=5,
                output=output.append, clock=clock, sleeper=sleeper)
        self.assertTrue(passed)
        self.assertIn('DRY RUN PASSED', output[-1])
        self.assertEqual(ups_supervisor.SHUTDOWN_VOLTAGE_V, 3.05)

    def test_pico_test_arms_then_disarms_and_never_requests_poweroff(self):
        events = []
        pico = types.SimpleNamespace(
            arm=lambda: events.append('arm'), disarm=lambda: events.append('disarm'))
        battery = self.battery(3.9)
        mains = {'available': True, 'gpio_level': True}
        with patch.object(ups_supervisor, 'live_sample',
                          return_value=(battery, mains)), \
             patch.object(ups_supervisor, 'PicoSupervisor', return_value=pico), \
             patch.object(ups_supervisor, 'request_poweroff') as poweroff:
            ups_supervisor.pico_command_test(
                {}, output=lambda *_: None,
                sleeper=lambda seconds: events.append(('wait', seconds)))
        self.assertEqual(events, ['arm', ('wait', 2.0), 'disarm'])
        poweroff.assert_not_called()

    def test_pico_test_disarms_even_if_arm_command_is_interrupted(self):
        events = []

        def interrupted_arm():
            events.append('arm')
            raise RuntimeError('interrupted')

        pico = types.SimpleNamespace(
            arm=interrupted_arm, disarm=lambda: events.append('disarm'))
        with patch.object(ups_supervisor, 'live_sample', return_value=(
                self.battery(3.9), {'available': True, 'gpio_level': True})), \
             patch.object(ups_supervisor, 'PicoSupervisor', return_value=pico):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                ups_supervisor.pico_command_test({}, output=lambda *_: None)
        self.assertEqual(events, ['arm', 'disarm'])

    def test_shutdown_test_request_is_one_shot_and_reports_result(self):
        with tempfile.TemporaryDirectory() as temp:
            request_path = Path(temp) / 'request.json'
            result_path = Path(temp) / 'result.json'
            request_id = ups_supervisor.create_shutdown_test_request(request_path)
            self.assertEqual(
                ups_supervisor.take_shutdown_test_request(request_path),
                {'id': request_id, 'action': 'shutdown'})
            self.assertIsNone(
                ups_supervisor.take_shutdown_test_request(request_path))
            ups_supervisor.write_shutdown_test_result(
                request_id, True, 'accepted', result_path)
            result = ups_supervisor.wait_for_shutdown_test_result(
                request_id, request_path, result_path)
            self.assertEqual(result, {
                'id': request_id, 'accepted': True, 'reason': 'accepted'})
            self.assertFalse(result_path.exists())


class ShutdownActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_alert_precedes_arm_and_poweroff(self):
        events = []

        class Box:
            def record(self, trackers, incidents, gateways):
                events.append('notify')
                self.incidents = incidents
                return ['shutdown:ha', 'shutdown:smtp', 'shutdown:lora']

            async def wait_started(self, job_ids, timeout):
                events.append('delivery-started')
                return True

        shutdown = LowBatteryShutdown(clock=lambda: 30)
        shutdown.last_voltage_v = 3.05
        shutdown.latched = True
        pico = types.SimpleNamespace(arm=lambda: events.append('arm'))

        def poweroff():
            events.append('poweroff')

        async def immediate(function, *args):
            return function(*args)

        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)

        with patch.object(daemon, 'request_poweroff', side_effect=poweroff), \
             patch.object(daemon.asyncio, 'sleep', side_effect=sleep), \
             patch.object(daemon.asyncio, 'to_thread', side_effect=immediate):
            await daemon.perform_low_battery_shutdown(
                {}, {}, Box(), {}, shutdown, pico)

        self.assertEqual(events,
                         ['notify', 'delivery-started', 'arm', 'poweroff'])
        self.assertEqual(sleeps, [2.0, 0.25])
        self.assertEqual(shutdown.action_state, 'POWEROFF_REQUESTED')

    async def test_operator_shutdown_test_has_a_ten_second_countdown(self):
        class Box:
            def record(self, *_args):
                return []

            async def wait_started(self, _job_ids, _timeout):
                return True

        shutdown = LowBatteryShutdown(clock=lambda: 5)
        shutdown.last_voltage_v = 4.0
        shutdown.latched = True
        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)

        async def immediate(function, *args):
            return function(*args)

        with patch.object(daemon.asyncio, 'sleep', side_effect=sleep), \
             patch.object(daemon.asyncio, 'to_thread', side_effect=immediate), \
             patch.object(daemon, 'request_poweroff'):
            await daemon.perform_low_battery_shutdown(
                {}, {}, Box(), {}, shutdown,
                types.SimpleNamespace(arm=lambda: None), test=True)

        self.assertEqual(sleeps, [10.0, 2.0, 0.25])

    async def test_running_daemon_accepts_manual_shutdown_only_with_valid_inputs(self):
        stop = asyncio.Event()
        shutdown = LowBatteryShutdown(clock=lambda: 10)
        current = {
            'config': {'battery': {}, 'gateways': {}},
            'trackers': {}, 'results': {},
        }
        battery = types.SimpleNamespace(sample=lambda *_: {
            'available': True, 'sampled': True, 'voltage_v': 3.9})
        mains = types.SimpleNamespace(sample=lambda *_: {
            'available': True, 'gpio_level': False})
        acknowledgements = []

        async def immediate(function, *args):
            return function(*args)

        async def perform(*args, **kwargs):
            self.assertTrue(kwargs['test'])
            stop.set()

        with patch.object(daemon, 'BatteryMonitor', return_value=battery), \
             patch.object(daemon, 'MainsMonitor', return_value=mains), \
             patch.object(daemon, 'live_sample', return_value=(
                 battery.sample({}), mains.sample({}, 3.9))), \
             patch.object(daemon.asyncio, 'to_thread', side_effect=immediate), \
             patch.object(daemon, 'take_shutdown_test_request',
                          return_value={'id': 'a' * 32, 'action': 'shutdown'}), \
             patch.object(daemon, 'write_shutdown_test_result',
                          side_effect=lambda *args: acknowledgements.append(args)), \
             patch.object(daemon, 'perform_low_battery_shutdown',
                          AsyncMock(side_effect=perform)):
            await daemon.low_battery_worker(
                stop, current, object(), shutdown, object(), asyncio.Lock())

        self.assertTrue(shutdown.latched)
        self.assertEqual(acknowledgements,
                         [('a' * 32, True, 'accepted by the running daemon')])

    async def test_running_daemon_rejects_manual_shutdown_while_mains_is_present(self):
        stop = asyncio.Event()
        shutdown = LowBatteryShutdown(clock=lambda: 10)
        current = {
            'config': {'battery': {}, 'gateways': {}},
            'trackers': {}, 'results': {},
        }
        battery = types.SimpleNamespace(sample=lambda *_: {
            'available': True, 'sampled': True, 'voltage_v': 3.9})
        mains = types.SimpleNamespace(sample=lambda *_: {
            'available': True, 'gpio_level': True})
        acknowledgements = []

        async def immediate(function, *args):
            return function(*args)

        def acknowledge(*args):
            acknowledgements.append(args)
            stop.set()

        with patch.object(daemon, 'BatteryMonitor', return_value=battery), \
             patch.object(daemon, 'MainsMonitor', return_value=mains), \
             patch.object(daemon, 'live_sample', return_value=(
                 battery.sample({}), mains.sample({}, 3.9))), \
             patch.object(daemon.asyncio, 'to_thread', side_effect=immediate), \
             patch.object(daemon, 'take_shutdown_test_request',
                          return_value={'id': 'b' * 32, 'action': 'shutdown'}), \
             patch.object(daemon, 'write_shutdown_test_result',
                          side_effect=acknowledge), \
             patch.object(daemon, 'perform_low_battery_shutdown',
                          AsyncMock()) as perform:
            await daemon.low_battery_worker(
                stop, current, object(), shutdown, object(), asyncio.Lock())

        self.assertFalse(shutdown.latched)
        self.assertEqual(
            acknowledgements,
            [('b' * 32, False, 'GPIO17 must be LOW before the test')])
        perform.assert_not_awaited()

    async def test_mains_restore_disarms_and_clears_failed_poweroff_latch(self):
        stop = asyncio.Event()
        shutdown = LowBatteryShutdown()
        shutdown.latched = True
        shutdown.action_state = 'POWEROFF_FAILED'
        shutdown.error = 'CalledProcessError'
        battery = {'available': True, 'sampled': True, 'voltage_v': 3.2}
        mains = {'available': True, 'gpio_level': True}
        calls = []

        def disarm():
            calls.append('disarm')
            stop.set()

        async def immediate(function, *args):
            return function(*args)

        current = {'config': {'battery': {}, 'gateways': {}},
                   'trackers': {}, 'results': {}}
        with patch.object(daemon, 'BatteryMonitor'), \
             patch.object(daemon, 'MainsMonitor'), \
             patch.object(daemon, 'live_sample', return_value=(battery, mains)), \
             patch.object(daemon.asyncio, 'to_thread', side_effect=immediate):
            await daemon.low_battery_worker(
                stop, current, object(), shutdown,
                types.SimpleNamespace(disarm=disarm), asyncio.Lock())

        self.assertEqual(calls, ['disarm'])
        self.assertFalse(shutdown.latched)
        self.assertEqual(shutdown.action_state, 'IDLE')
        self.assertTrue(shutdown.mains_present)
        self.assertIsNone(shutdown.error)


class DeploymentSafetyTests(unittest.TestCase):
    def test_service_sandbox_allows_only_the_sudoers_poweroff(self):
        unit = (ROOT / 'deployment/piwatch.service').read_text()
        sudoers = (ROOT / 'deployment/piwatch-poweroff.sudoers').read_text()
        directives = {line.strip() for line in unit.splitlines()
                      if line.strip() and not line.lstrip().startswith('#')}
        self.assertIn('NoNewPrivileges=false', directives)
        self.assertNotIn('NoNewPrivileges=true', directives)
        self.assertIn(
            'piwatch ALL=(root) NOPASSWD: /usr/bin/systemctl --no-block poweroff',
            sudoers)
        self.assertEqual(ups_supervisor.POWEROFF_COMMAND, (
            '/usr/bin/sudo', '-n', '/usr/bin/systemctl', '--no-block', 'poweroff'))

    def test_mqtt_publisher_has_no_hardware_or_deployment_secret_access(self):
        unit = (ROOT / 'deployment/piwatch-ha-mqtt.service').read_text()
        directives = {line.strip() for line in unit.splitlines()
                      if line.strip() and not line.lstrip().startswith('#')}
        self.assertIn('User=piwatch-mqtt', directives)
        self.assertIn('SupplementaryGroups=piwatch-config', directives)
        self.assertIn('NoNewPrivileges=true', directives)
        self.assertIn('PrivateDevices=true', directives)
        self.assertFalse(any('i2c' in line or 'gpio' in line
                             for line in directives if line.startswith('SupplementaryGroups=')))
        inaccessible = next(line for line in directives
                            if line.startswith('InaccessiblePaths='))
        self.assertIn('/etc/piwatch/secrets.env', inaccessible)
        self.assertIn('/var/lib/piwatch', inaccessible)


class MainsTests(unittest.TestCase):
    settings = staticmethod(lambda: validate_config({'targets': []})['battery'])

    def test_gpio_loss_and_restore_are_debounced_for_three_seconds(self):
        levels = iter((False, False, False, True, True, True))
        times = iter((0, 2.9, 3.0, 4.0, 6.9, 7.0))
        monitor = MainsMonitor(reader=lambda *_: next(levels), stable_state=True,
                               clock=lambda: next(times))
        samples = [monitor.sample(self.settings(), 3.612) for _ in range(6)]
        self.assertEqual([sample['healthy'] for sample in samples],
                         [True, True, False, False, False, True])
        self.assertTrue(samples[0]['debouncing'])
        self.assertFalse(samples[2]['debouncing'])
        self.assertEqual(samples[2]['voltage_v'], 3.612)

    def test_short_gpio_change_is_ignored(self):
        levels = iter((False, True, True))
        times = iter((0, 1, 2))
        monitor = MainsMonitor(reader=lambda *_: next(levels), stable_state=True,
                               clock=lambda: next(times))
        samples = [monitor.sample(self.settings(), voltage) for voltage in (3.7, 3.4, 3.1)]
        self.assertTrue(all(sample['healthy'] for sample in samples))

    def test_gpio_state_is_independent_of_battery_voltage(self):
        monitor = MainsMonitor(reader=lambda *_: True, stable_state=True, clock=lambda: 0)
        self.assertTrue(monitor.sample(self.settings(), 2.9)['healthy'])

    def test_unavailable_gpio_does_not_create_a_failure_sample(self):
        monitor = MainsMonitor(reader=lambda *_: (_ for _ in ()).throw(PermissionError()))
        sample = monitor.sample(self.settings(), 3.5)
        self.assertFalse(sample['available'])
        self.assertFalse(sample['sampled'])


class TrackerReconciliationTests(unittest.TestCase):
    def test_mains_uses_only_the_gpio_debounce_threshold(self):
        value = validate_config({'targets': []})
        tracker = daemon.reconcile_trackers(value, {}, {})['mains']
        self.assertEqual((tracker.suspect_after, tracker.down_after, tracker.recover_after),
                         (1, 1, 1))

    def test_dynamic_targets_reset_evidence_only_for_changed_checks(self):
        old = config()
        trackers = daemon.reconcile_trackers(old, {}, {})
        trackers['router'].state = 'DOWN'
        new = copy.deepcopy(old)
        new['targets'][0]['name'] = 'Gateway'
        renamed = daemon.reconcile_trackers(new, old, trackers)
        self.assertEqual(renamed['router'].state, 'DOWN')
        new['targets'][0]['host'] = '192.0.2.44'
        self.assertEqual(daemon.reconcile_trackers(new, old, trackers)['router'].state, 'UNKNOWN')
        new['targets'] = []
        self.assertNotIn('router', daemon.reconcile_trackers(new, old, trackers))


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_gps_ignores_empty_sky_before_complete_satellite_report(self):
        from piwatch import health

        reports = [
            {'class': 'SKY'},
            {'class': 'TPV', 'mode': 3, 'time': '2026-09-09T00:00:00Z'},
            {'class': 'SKY', 'nSat': 15, 'uSat': 9,
             'satellites': [{'used': index < 9} for index in range(15)]},
        ]

        class Reader:
            async def readline(self):
                return (json.dumps(reports.pop(0)) + '\n').encode() if reports else b''

        class Writer:
            def write(self, value):
                self.value = value

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                pass

        with patch.object(asyncio, 'open_connection', AsyncMock(return_value=(Reader(), Writer()))):
            result = await health.gps_health()
        self.assertEqual((result['state'], result['mode'], result['seen'], result['used']),
                         ('UP', 3, 15, 9))

    def test_system_sampler_calculates_cpu_and_network_rates(self):
        from piwatch import health

        sampler = health.SystemSampler()
        base = {'state': 'UP', 'memory_percent': 25, 'disk_percent': 40, 'load_1m': .2}
        with patch.object(health, '_cpu_times', side_effect=[(1000, 700), (1100, 720)]), \
             patch.object(health, '_network_bytes', side_effect=[(1000, 2000), (2024, 4048)]), \
             patch.object(health, 'system_health', side_effect=lambda: dict(base)), \
             patch.object(health.time, 'monotonic', side_effect=[10, 12]), \
             patch.object(health.time, 'time', return_value=100):
            first = sampler.sample()
            second = sampler.sample()
        self.assertNotIn('cpu_percent', first)
        self.assertEqual(second['cpu_percent'], 80.0)
        self.assertEqual((second['network_rx_bps'], second['network_tx_bps']), (512, 1024))

    async def test_external_cached_results_are_not_new_failure_samples(self):
        external = checks.ExternalChecks(config())
        with patch.object(external, 'internet', AsyncMock(return_value={'healthy': False})), patch.object(external, 'dns', AsyncMock(return_value={'healthy': True})):
            first = await external.collect()
            cached = await external.collect()
        self.assertTrue(first['internet']['sampled'])
        self.assertFalse(cached['internet']['sampled'])

    async def test_internet_and_dns_are_independent_and_allow_partial_success(self):
        external = checks.ExternalChecks(config())
        with patch.object(checks, 'tcp_check', AsyncMock(side_effect=[False, True])), patch.object(checks, 'dns_check', AsyncMock(return_value=False)):
            result = await external.collect()
        self.assertTrue(result['internet']['healthy'])
        self.assertFalse(result['dns']['healthy'])

    async def test_disabled_targets_are_not_probed(self):
        value = config()
        value['targets'][0]['enabled'] = False
        with patch.object(checks, 'ping', AsyncMock()) as probe:
            self.assertEqual(await checks.check_all(value), {})
        probe.assert_not_called()

    async def test_running_monitor_applies_a_new_revision_without_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SettingsStore(Path(temp) / 'settings.sqlite3')
            value = config()
            value['internet']['enabled'] = value['dns']['enabled'] = False
            value['gateways'] = {k: {'enabled': False} for k in ('smtp','lora','home_assistant')}
            store.initialize(value)
            handlers, revisions = {}, []
            def write(results, trackers, box, health, active, started, **kwargs):
                revisions.append(kwargs['revision'])
                if kwargs['revision'] == 1:
                    revision, updated = store.read()
                    updated['targets'].append({'id':'new_server','name':'New Server','host':'192.0.2.2','enabled':True,'checks':[{'type':'ping'}]})
                    store.save(updated, revision, 'test')
                else:
                    self.assertIn('new_server', trackers)
                    handlers[signal.SIGTERM]()
            async def local(active):
                return {target['id']: {'healthy':True, 'ping':True} for target in active['targets']}
            async def health(snapshot, stop, current):
                await stop.wait()
            loop = asyncio.get_running_loop()
            factory = lambda **kw: outbox.Outbox(Path(temp) / 'outbox.sqlite3', **kw)
            battery = types.SimpleNamespace(sample=lambda *_: {
                'available': False, 'sampled': False, 'healthy': None})
            mains = types.SimpleNamespace(sample=lambda *_: {
                'available': True, 'sampled': True, 'healthy': True,
                'gpio_level': True})
            pico_calls = []
            pico = types.SimpleNamespace(
                disarm=lambda: pico_calls.append('disarm'), arm=lambda: None)
            async def immediate(function, *args):
                return function(*args)
            with patch.object(daemon, 'SettingsStore', return_value=store), \
                 patch.object(daemon, 'Outbox', side_effect=factory), \
                 patch.object(daemon, 'BatteryMonitor', return_value=battery), \
                 patch.object(daemon, 'MainsMonitor', return_value=mains), \
                 patch.object(daemon, 'live_sample', return_value=(
                     battery.sample({}), mains.sample({}, None))), \
                 patch.object(daemon, 'PicoSupervisor', return_value=pico), \
                 patch.object(daemon.asyncio, 'to_thread', side_effect=immediate), \
                 patch.object(daemon, 'check_all', side_effect=local), \
                 patch.object(daemon, 'health_worker', side_effect=health), \
                 patch.object(daemon, 'write_status', side_effect=write), \
                 patch.object(loop, 'add_signal_handler',
                              side_effect=lambda sig, fn: handlers.update({sig: fn})), \
                 patch.object(loop, 'remove_signal_handler'):
                await asyncio.wait_for(daemon.run(), 5)
            self.assertEqual(revisions, [1, 2])
            self.assertEqual(pico_calls, ['disarm'])


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'outbox.sqlite3'
        self.box = outbox.Outbox(self.path)
        self.trackers = daemon.reconcile_trackers(config(), {}, {})
        self.results = {key: {'healthy':False,'ping':False} for key in self.trackers}
        self.trackers['router'].state = 'DOWN'

    def tearDown(self):
        self.box.db.close()
        self.temp.cleanup()

    def test_history_and_jobs_are_committed_together_and_survive_restart(self):
        self.box.record(self.trackers, [('router','DOWN',self.results)])
        self.box.db.close()
        self.box = outbox.Outbox(self.path)
        self.assertEqual(self.box.history()[0]['state'], 'DOWN')
        self.assertEqual(self.box.summary()['lora']['pending'], 1)
        self.trackers['router'].last_outage_seconds = 42
        self.box.record(self.trackers, [('router','UP',self.results)])
        self.assertEqual(self.box.history()[0]['outage_seconds'], 42)

    def test_three_day_maintenance_keeps_pending_work_and_recent_terminal_jobs(self):
        now = time.time()
        old = now - outbox.TERMINAL_JOB_RETENTION - 1
        with self.box.db:
            self.box.db.execute('DELETE FROM jobs')
            self.box.db.executemany(
                'INSERT INTO jobs(id, channel, payload, status, created_at, finished_at) '
                'VALUES (?, ?, ?, ?, ?, ?)', (
                    ('pending-old', 'lora', '{}', 'pending', old, None),
                    ('done-old', 'lora', '{}', 'done', old, old),
                    ('failed-old', 'ip', '{}', 'failed', old, old),
                    ('done-recent', 'ip', '{}', 'done', now, now),
                ))
        self.assertTrue(self.box.maintain(now, force=True))
        ids = {row[0] for row in self.box.db.execute('SELECT id FROM jobs')}
        self.assertEqual(ids, {'pending-old', 'done-recent'})
        self.assertFalse(self.box.maintain(now + 1))

    def test_existing_outbox_schema_is_migrated_without_losing_jobs(self):
        path = Path(self.temp.name) / 'legacy.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE jobs (id TEXT PRIMARY KEY, channel TEXT NOT NULL, '
                       'payload TEXT NOT NULL, stage INTEGER NOT NULL DEFAULT 0, '
                       'attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0, '
                       "status TEXT NOT NULL DEFAULT 'pending', last_error TEXT)")
            db.execute("INSERT INTO jobs(id, channel, payload, status) "
                       "VALUES ('existing:lora', 'lora', '{}', 'failed')")
        migrated = outbox.Outbox(path)
        try:
            columns = {row[1] for row in migrated.db.execute('PRAGMA table_info(jobs)')}
            row = migrated.db.execute(
                "SELECT status, created_at, finished_at FROM jobs WHERE id='existing:lora'"
            ).fetchone()
            self.assertTrue({'created_at', 'finished_at'} <= columns)
            self.assertEqual(row[0], 'failed')
            self.assertTrue(row[1] > 0 and row[2] > 0)
        finally:
            migrated.db.close()

    def test_clear_records_preserves_pending_jobs_and_active_tracker_state(self):
        self.box.record(self.trackers, [('router', 'DOWN', self.results)])
        with self.box.db:
            self.box.db.execute("UPDATE jobs SET status='failed', finished_at=? "
                                "WHERE channel='ip'", (time.time(),))
        result = self.box.clear_records('clear_all')
        self.assertEqual(result, {'deliveries': 1, 'incidents': 1})
        self.assertEqual(self.box.summary()['lora']['pending'], 1)
        self.assertEqual(self.box.history(), [])
        restored = daemon.reconcile_trackers(config(), {}, {})
        self.box.restore(restored)
        self.assertEqual(restored['router'].state, 'DOWN')

    def test_daemon_processes_queued_clear_request(self):
        settings = SettingsStore(Path(self.temp.name) / 'commands.sqlite3')
        settings.initialize(config())
        self.box.record(self.trackers, [('router', 'DOWN', self.results)])
        with self.box.db:
            self.box.db.execute("UPDATE jobs SET status='failed', finished_at=?",
                                (time.time(),))
        request_id = settings.request_maintenance('clear_deliveries', 'owner')
        daemon.process_maintenance(settings, self.box)
        self.assertEqual(self.box.summary()['ip']['failed'], 0)
        self.assertEqual(settings.maintenance_status(request_id)['status'], 'complete')

    def test_mains_incident_fans_out_to_every_enabled_gateway(self):
        value = validate_config({'targets': []})
        trackers = daemon.reconcile_trackers(value, {}, {})
        trackers['mains'].state = 'DOWN'
        trackers['mains'].down_since = time.time()
        results = {name: {'healthy': True} for name in trackers}
        results['mains'] = {'healthy': False, 'voltage_v': 3.48,
                            'gpio': 17, 'gpio_level': False, 'source': 'gpio',
                            'outage': True}
        self.box.record(trackers, [('mains', 'DOWN', results)], value['gateways'])
        channels = [row[0] for row in self.box.db.execute(
            'SELECT channel FROM jobs ORDER BY channel')]
        self.assertEqual(channels, ['ha', 'lora', 'smtp'])
        payload = json.loads(self.box.db.execute(
            "SELECT payload FROM jobs WHERE channel='ha'").fetchone()[0])
        self.assertIn('Mains power lost', payload['title'])
        self.assertIn('Battery voltage: 3.480 V', payload['message'])
        self.assertIn('GPIO17: LOW', payload['message'])
        trackers['mains'].state = 'UP'
        restored_title, _ = build_message('mains', 'UP', results, trackers)
        self.assertIn('Mains power restored', restored_title)

    def test_shutdown_notification_is_urgent_and_fans_out_to_all_gateways(self):
        value = validate_config({'targets': []})
        trackers = daemon.reconcile_trackers(value, {}, {})
        shutdown = StateTracker('shutdown', 1, 1, 1)
        shutdown.display = 'Low battery shutdown'
        shutdown.state = 'DOWN'
        event_trackers = {**trackers, 'shutdown': shutdown}
        results = {name: {'healthy': True} for name in event_trackers}
        results['shutdown'] = {
            'healthy': False, 'voltage_v': 3.05, 'low_voltage_v': 3.05,
            'source': 'shutdown',
        }
        job_ids = self.box.record(
            event_trackers, [('shutdown', 'DOWN', results)], value['gateways'])
        rows = self.box.db.execute(
            'SELECT channel, payload, priority FROM jobs ORDER BY channel').fetchall()
        self.assertEqual([row[0] for row in rows], ['ha', 'lora', 'smtp'])
        self.assertTrue(all(row[2] == 1 for row in rows))
        self.assertEqual(len(job_ids), 3)
        payload = json.loads(next(row[1] for row in rows if row[0] == 'ha'))
        self.assertIn('Low battery shutdown initiated', payload['title'])
        self.assertIn('Battery voltage: 3.050 V', payload['message'])

    def test_failure_rolls_back_history_and_state(self):
        with patch.object(outbox, 'build_lora_messages', side_effect=ValueError('test')):
            with self.assertRaises(ValueError):
                self.box.record(self.trackers, [('router','DOWN',self.results)])
        self.assertEqual(self.box.history(), [])
        self.assertEqual(self.box.db.execute('SELECT COUNT(*) FROM trackers').fetchone()[0], 0)

    def test_new_gateway_settings_are_frozen_and_public_metadata_has_no_secret(self):
        gateways = config()['gateways']
        gateways['lora']['token'] = 'synthetic-radio-token'
        self.box.record(self.trackers, [('router','DOWN',self.results)], gateways)
        gateways['lora']['token'] = 'replacement'
        saved = self.box.db.execute("SELECT payload FROM jobs WHERE channel='lora'").fetchone()[0]
        self.assertIn('synthetic-radio-token', saved)
        self.assertNotIn('synthetic-radio-token', json.dumps(self.box.attention()))

    def test_many_custom_targets_do_not_break_radio_byte_limit(self):
        trackers = {f'server{i}': StateTracker(f'server{i}',3,5,3) for i in range(32)}
        for tracker in trackers.values():
            tracker.display = '服务器' * 12
        messages = build_lora_messages('server0', 'DOWN', {'server0':{'ping':False}}, trackers)
        self.assertTrue(all(len(message.encode()) <= 200 for message in messages))

    def test_restart_does_not_restore_old_host_state_after_offline_config_edit(self):
        self.box.record(self.trackers, [('router','DOWN',self.results)])
        changed = config()
        changed['targets'][0]['host'] = '192.0.2.88'
        trackers = daemon.reconcile_trackers(changed, {}, {})
        self.box.restore(trackers)
        self.assertEqual(trackers['router'].state, 'UNKNOWN')

    def test_status_snapshot_is_atomic_and_contains_no_gateway_credentials(self):
        from piwatch.status import write_status
        target = Path(self.temp.name) / 'status.json'
        gateways = config()['gateways']
        gateways['lora']['token'] = 'synthetic-private-gateway-value'
        self.box.record(self.trackers, [('router','DOWN',self.results)], gateways)
        write_status(self.results, self.trackers, self.box, {}, config(), time.monotonic(), target, revision=7)
        text = target.read_text()
        self.assertNotIn('synthetic-private-gateway-value', text)
        self.assertEqual(json.loads(text)['monitor']['settings_revision'], 7)
        self.assertEqual(json.loads(text)['incidents'][0]['name'], 'Router')
        self.assertFalse(target.with_name('status.json.tmp').exists())
