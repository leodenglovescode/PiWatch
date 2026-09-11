"""Hardware-free regressions for configuration, history, and freshness."""
import asyncio
import copy
from contextlib import closing
import json
from pathlib import Path
import signal
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'watchdog'))
from piwatch import checks, daemon
from piwatch.battery import BatteryMonitor, read_ina219
from piwatch.config import validate_config
from piwatch.config_store import SettingsStore, ConflictError
from piwatch.notifications import outbox
from piwatch.notifications.messages import build_lora_messages
from piwatch.state import StateTracker


def config():
    return validate_config({'targets': [{'id': 'router', 'name': 'Router', 'host': '192.0.2.1',
                                        'enabled': True, 'checks': [{'type': 'ping'}]}],
                            'battery': {'enabled': False}})


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
        value['battery']['recover_voltage_v'] = 3.31
        with self.assertRaisesRegex(ValueError, 'at least 0.04 V'):
            validate_config(value)


class BatteryTests(unittest.TestCase):
    def settings(self, **updates):
        value = validate_config({'targets': []})['battery']
        value.update(updates)
        return value

    def test_low_voltage_uses_recovery_hysteresis(self):
        readings = iter((3.60, 3.29, 3.40, 3.51))
        monitor = BatteryMonitor(reader=lambda *_: {
            'voltage_v': next(readings), 'shunt_voltage_mv': 0})
        states = [monitor.sample(self.settings())['battery']['healthy'] for _ in range(4)]
        self.assertEqual(states, [True, False, False, True])

    def test_ina219_registers_are_big_endian_and_shunt_is_signed(self):
        with patch('piwatch.battery.os.open', return_value=7), \
             patch('piwatch.battery.fcntl.ioctl'), \
             patch('piwatch.battery.os.write', return_value=1), \
             patch('piwatch.battery.os.read', side_effect=(bytes.fromhex('1c12'),
                                                           bytes.fromhex('ff9c'))), \
             patch('piwatch.battery.os.close'):
            reading = read_ina219(3, 0x40)
        self.assertEqual(reading, {'voltage_v': 3.592, 'shunt_voltage_mv': -1.0})

    def test_mains_detection_learns_discharge_direction_then_recovers(self):
        readings = iter(((3.70, 0), (3.55, -10), (3.54, -9), (3.64, 0)))
        monitor = BatteryMonitor(reader=lambda *_: dict(
            zip(('voltage_v', 'shunt_voltage_mv'), next(readings))))
        with patch('piwatch.battery.time.monotonic', side_effect=(0, 1, 2, 3)):
            samples = [monitor.sample(self.settings())['mains'] for _ in range(4)]
        self.assertTrue(samples[0]['healthy'])
        self.assertEqual((samples[1]['healthy'], samples[1]['discharge_polarity']),
                         (False, 'negative'))
        self.assertEqual(samples[2]['detection_method'], 'shunt-direction')
        self.assertTrue(samples[3]['healthy'])

    def test_configured_direction_detects_discharge_without_voltage_drop(self):
        monitor = BatteryMonitor(reader=lambda *_: {
            'voltage_v': 3.7, 'shunt_voltage_mv': 12})
        sample = monitor.sample(self.settings(discharge_polarity='positive'))['mains']
        self.assertFalse(sample['healthy'])
        self.assertEqual(sample['detection_method'], 'shunt-direction')

    def test_voltage_drop_fallback_recovers_without_shunt_signal(self):
        readings = iter(((3.70, 0), (3.55, 0), (3.56, 0), (3.64, 0)))
        monitor = BatteryMonitor(reader=lambda *_: dict(
            zip(('voltage_v', 'shunt_voltage_mv'), next(readings))))
        with patch('piwatch.battery.time.monotonic', side_effect=(0, 1, 2, 3)):
            samples = [monitor.sample(self.settings())['mains'] for _ in range(4)]
        self.assertEqual([sample['healthy'] for sample in samples], [True, False, False, True])
        self.assertEqual(samples[1]['detection_method'], 'voltage-drop')
        self.assertEqual(samples[1]['discharge_polarity'], 'unlearned')

    def test_unavailable_sensor_does_not_create_a_failure_sample(self):
        def unavailable(*_):
            raise PermissionError()
        sample = BatteryMonitor(reader=unavailable).sample(self.settings())
        self.assertFalse(sample['battery']['sampled'])
        self.assertFalse(sample['mains']['sampled'])


class TrackerReconciliationTests(unittest.TestCase):
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
            with patch.object(daemon, 'SettingsStore', return_value=store), patch.object(daemon, 'Outbox', side_effect=factory), patch.object(daemon, 'check_all', side_effect=local), patch.object(daemon, 'health_worker', side_effect=health), patch.object(daemon, 'write_status', side_effect=write), patch.object(loop, 'add_signal_handler', side_effect=lambda sig, fn: handlers.update({sig:fn})), patch.object(loop, 'remove_signal_handler'):
                await asyncio.wait_for(daemon.run(), 5)
            self.assertEqual(revisions, [1, 2])


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

    def test_mains_incident_fans_out_to_every_enabled_gateway(self):
        value = validate_config({'targets': []})
        trackers = daemon.reconcile_trackers(value, {}, {})
        trackers['mains'].state = 'DOWN'
        trackers['mains'].down_since = time.time()
        trackers['mains'].detected_polarity = 'negative'
        results = {name: {'healthy': True} for name in trackers}
        results['mains'] = {'healthy': False, 'voltage_v': 3.48,
                            'voltage_drop_v': .14, 'outage': True}
        self.box.record(trackers, [('mains', 'DOWN', results)], value['gateways'])
        channels = [row[0] for row in self.box.db.execute(
            'SELECT channel FROM jobs ORDER BY channel')]
        self.assertEqual(channels, ['ha', 'lora', 'smtp'])
        payload = json.loads(self.box.db.execute(
            "SELECT payload FROM jobs WHERE channel='ha'").fetchone()[0])
        self.assertIn('Mains power OUTAGE', payload['title'])
        self.assertIn('Observed drop: 0.140 V', payload['message'])
        restored = daemon.reconcile_trackers(value, {}, {})
        self.box.restore(restored)
        self.assertEqual(restored['mains'].detected_polarity, 'negative')

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
