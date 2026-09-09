"""Hardware-free regressions for configuration, history, and freshness."""
import asyncio
import copy
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
from piwatch.config import validate_config
from piwatch.config_store import SettingsStore, ConflictError
from piwatch.notifications import outbox
from piwatch.notifications.messages import build_lora_messages
from piwatch.state import StateTracker


def config():
    return validate_config({'targets': [{'id': 'router', 'name': 'Router', 'host': '192.0.2.1',
                                        'enabled': True, 'checks': [{'type': 'ping'}]}]})


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
