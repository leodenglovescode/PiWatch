"""Native ntfy and heartbeat regressions: synthetic secrets, no external I/O."""
import asyncio
from contextlib import closing
import copy
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'watchdog'))
from piwatch.config import validate_config
from piwatch.config_store import SettingsStore
from piwatch.daemon import reconcile_trackers, process_maintenance
from piwatch.heartbeat import Heartbeat, monitor_fresh, run
from piwatch.notifications import ntfy
from piwatch.notifications.outbox import Outbox


def configuration():
    return validate_config({'targets': [{'id': 'router', 'name': 'Routeur 家', 'host': 'router.example.test',
        'enabled': True, 'checks': [{'type': 'ping'}]}], 'gateways': {
        'lora': {'enabled': False}, 'home_assistant': {'enabled': False}, 'smtp': {'enabled': False},
        'ntfy': {'enabled': True, 'heartbeat_enabled': True, 'url': 'https://192.0.2.10:9443',
                 'topic': 'alerts', 'heartbeat_topic': 'heartbeat', 'username': 'synthetic-user',
                 'password': 'synthetic-private-password', 'tls_trust': 'system'}}})


class ConfigurationTests(unittest.TestCase):
    def test_old_settings_gain_disabled_channel_without_a_write(self):
        value = validate_config({'targets': []})
        self.assertFalse(value['gateways']['ntfy']['enabled'])
        self.assertFalse(value['gateways']['ntfy']['heartbeat_enabled'])

    def test_secrets_are_write_only_and_omitted_values_survive_old_database(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SettingsStore(Path(folder)/'settings.sqlite3')
            store.initialize(configuration())
            public = store.public()
            text = json.dumps(public)
            self.assertNotIn('synthetic-user', text)
            self.assertNotIn('synthetic-private-password', text)
            self.assertTrue(public['configured_secrets']['ntfy']['password'])
            store.save(public['config'], public['revision'], 'test')
            self.assertEqual(store.read()[1]['gateways']['ntfy']['password'], 'synthetic-private-password')
            # Migration from a settings row written before ntfy existed.
            with closing(sqlite3.connect(store.path)) as db, db:
                old = configuration(); old['gateways'].pop('ntfy')
                db.execute('UPDATE settings SET value=? WHERE id=1', (json.dumps(old),))
            public = store.public()
            store.save(public['config'], public['revision'], 'test')
            self.assertFalse(store.read()[1]['gateways']['ntfy']['enabled'])

    def test_explicit_clear_is_preserved_only_when_disabled(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SettingsStore(Path(folder)/'settings.sqlite3'); store.initialize(configuration())
            public = store.public(); value = public['config']['gateways']['ntfy']
            value.update(enabled=False, heartbeat_enabled=False, password='')
            store.save(public['config'], public['revision'], 'test')
            self.assertEqual(store.read()[1]['gateways']['ntfy']['password'], '')

    def test_bad_endpoints_credentials_and_topics_are_rejected(self):
        for key, bad in [('url','http://192.0.2.10'),('url','https://example.test'),
                         ('url','https://192.0.2.10/path'),('url','https://user:pass@192.0.2.10'),
                         ('url','https://192.0.2.10?token=x'),('tls_trust','insecure'),
                         ('topic','bad/topic'),('heartbeat_topic','alerts'),('password',''),
                         ('username','invalid:name'),('heartbeat_enabled','yes')]:
            with self.subTest(key=key, value=bad):
                value = configuration(); value['gateways']['ntfy'][key] = bad
                with self.assertRaises(ValueError): validate_config(value)

    def test_network_thresholds_match_dashboard_and_do_not_change_power(self):
        value = configuration(); trackers = reconcile_trackers(value, {}, {})
        network = trackers['router']
        self.assertEqual((network.suspect_after, network.down_after, network.recover_after), (1,3,1))
        self.assertEqual((trackers['battery'].down_after, trackers['battery'].recover_after), (5,3))
        self.assertEqual((trackers['mains'].down_after, trackers['mains'].recover_after), (1,1))
        for count in range(1,4):
            network.update(False)
            self.assertEqual(network.state, 'DOWN' if count==3 else 'SUSPECT')
        network.update(True); self.assertEqual(network.state, 'UP')
        bad=copy.deepcopy(value);bad['network_policy']['down_after_failures']=5
        with self.assertRaises(ValueError):validate_config(bad)


class FakeSocket:
    def __init__(self, response, clock, drip=0):
        self.response=io.BytesIO(response);self.clock=clock;self.drip=drip;self.sent=b'';self.timeout=99;self.closed=False
    def settimeout(self, value):self.timeout=value
    def sendall(self, value):self.sent+=value
    def recv_into(self, buffer):
        if self.drip > self.timeout:raise TimeoutError()
        self.clock[0]+=self.drip
        value=self.response.read(1 if self.drip else len(buffer))
        buffer[:len(value)]=value;return len(value)
    def close(self):self.closed=True


class TransportTests(unittest.TestCase):
    def publish(self, code=200, body=None, drip=0):
        body=json.dumps(body if body is not None else {'id':'native123','time':123}).encode()
        response=f'HTTP/1.1 {code} Status\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode()+body
        clock=[0.0]; connection=FakeSocket(response, clock, drip)
        context=Mock();context.wrap_socket.return_value=connection
        client=ntfy.NtfyClient(configuration()['gateways']['ntfy']);client.context=context
        with patch.object(ntfy.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(ntfy.socket,'create_connection',return_value=connection) as connect:
            result=client.publish('alerts','Unicode 家 message',title='PiWatch 家',priority=5,timeout=.8)
        return result,connection,context,connect

    def test_json_unicode_priorities_and_verified_ip(self):
        result,connection,context,connect=self.publish()
        self.assertEqual(result['id'],'native123')
        payload=json.loads(connection.sent.split(b'\r\n\r\n',1)[1])
        self.assertEqual(payload['title'],'PiWatch 家');self.assertEqual(payload['priority'],5)
        context.wrap_socket.assert_called_once_with(connection,server_hostname='192.0.2.10')
        connect.assert_called_once_with(('192.0.2.10',9443),timeout=.8)
        self.assertTrue(connection.closed)

    def test_redirects_and_rate_limits_are_sanitized_and_not_followed(self):
        for code in (302,401,429,500):
            with self.subTest(code=code),self.assertRaises(ntfy.NtfyError) as error:self.publish(code=code,body={'password':'do-not-echo'})
            self.assertEqual(error.exception.code,'HTTP'+str(code))
            self.assertNotIn('do-not-echo',str(error.exception))

    def test_deadline_does_not_reset_when_server_drips_headers(self):
        with self.assertRaises(ntfy.NtfyError) as error:self.publish(drip=.03)
        self.assertEqual(error.exception.code,'Timeout')

    def test_missing_acceptance_id_is_not_success(self):
        with self.assertRaises(ntfy.NtfyError):self.publish(body={'message':'not an acceptance'})

    def test_private_ca_uses_existing_public_certificate(self):
        with patch.object(ntfy.ssl,'create_default_context') as context:
            value=configuration()['gateways']['ntfy'];value['tls_trust']='private_ca'
            ntfy.NtfyClient(value)
            context.assert_called_once_with(cafile=ntfy.CA_PATH)


class HeartbeatTests(unittest.TestCase):
    def test_freshness_requires_current_boot_and_monotonic_age(self):
        good={'schema_version':2,'boot_id':'boot','generated_monotonic':10}
        for now,expected in [(10,True),(15,True),(15.01,False),(9,False),(float('nan'),False)]:
            self.assertEqual(monitor_fresh(good,now,'boot'),expected)
        self.assertFalse(monitor_fresh(good,10,'other'))
        self.assertFalse(monitor_fresh({},10,'boot'))

    def test_wire_nesting_no_replay_and_acceptance_is_not_freshness(self):
        client=Mock();client.publish.side_effect=[{'id':'a'},ntfy.NtfyError('HTTP429'),{'id':'c'}]
        worker=Heartbeat(factory=lambda _:client,active=lambda:True)
        value=configuration()['gateways']['ntfy']
        with patch('piwatch.heartbeat.monitor_fresh',side_effect=[True,False,True]):
            for stamp in (1,2,3):worker.tick(value,{},now=stamp)
        messages=[json.loads(call.args[1]) for call in client.publish.call_args_list]
        self.assertEqual(messages,[{'piwatch_fresh':True},{'piwatch_fresh':False},{'piwatch_fresh':True}])
        self.assertEqual(worker.accepted,2);self.assertEqual(worker.last_success,3)
        self.assertEqual(client.publish.call_count,3)
        for call in client.publish.call_args_list:self.assertEqual(call.kwargs,{'timeout':.8})
        public=json.dumps(worker.snapshot(now=3));self.assertNotIn('synthetic-private-password',public)

    def test_disabled_and_staged_never_publish(self):
        factory=Mock();value=configuration()['gateways']['ntfy']
        worker=Heartbeat(factory=factory,active=lambda:False);worker.tick(value,{})
        self.assertEqual(worker.state,'STAGED');factory.assert_not_called()
        worker=Heartbeat(factory=factory,active=lambda:True);value['heartbeat_enabled']=False;worker.tick(value,{})
        self.assertEqual(worker.state,'DISABLED');factory.assert_not_called()

    def test_stale_monitor_is_reported_as_false_even_if_vps_accepts(self):
        client=Mock();client.publish.return_value={'id':'accepted'}
        worker=Heartbeat(factory=lambda _:client,active=lambda:True)
        worker.tick(configuration()['gateways']['ntfy'],{})
        self.assertEqual(worker.state,'MONITOR_STALE')
        self.assertFalse(json.loads(client.publish.call_args.args[1])['piwatch_fresh'])

    def test_cadence_survives_an_independently_blocked_delivery(self):
        release=threading.Event();blocked=threading.Thread(target=lambda:release.wait(4),daemon=True);blocked.start()
        stop=threading.Event();samples=[];client=Mock();client.publish.return_value={'id':'accepted'}
        worker=Heartbeat(factory=lambda _:client,active=lambda:True)
        store=Mock();store.read.return_value=(1,configuration())
        def write(_):
            samples.append(time.monotonic())
            if len(samples)==3:stop.set()
        try:
            run(stop=stop,store=store,worker=worker,snapshot_reader=lambda:{},writer=write)
            self.assertTrue(blocked.is_alive());self.assertEqual(len(samples),3)
            self.assertTrue(all(.9<=b-a<=1.3 for a,b in zip(samples,samples[1:])))
        finally:release.set();blocked.join(1)

    def test_settings_error_does_not_publish_cached_configuration(self):
        stop=threading.Event();store=Mock();store.read.side_effect=ValueError('never print supplied data')
        worker=Heartbeat(factory=Mock(),active=lambda:True)
        def write(status):
            self.assertEqual(status['state'],'ERROR');self.assertEqual(status['error'],'ValueError');stop.set()
        run(stop=stop,store=store,worker=worker,writer=write)
        worker.factory.assert_not_called()


class OutboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder=tempfile.TemporaryDirectory();self.running=True
        self.box=Outbox(str(Path(self.folder.name)/'outbox.sqlite3'),lambda:self.running)
        self.config=configuration();self.trackers=reconcile_trackers(self.config,{}, {})
        self.results={key:{'healthy':False,'ping':False} for key in self.trackers}

    async def asyncTearDown(self):
        self.running=False;self.box.db.close();self.folder.cleanup()

    async def until(self, predicate):
        for _ in range(150):
            if predicate():return
            await asyncio.sleep(.01)
        self.fail('Worker did not reach expected checkpoint')

    async def test_failure_restart_retry_checkpoint_and_frozen_credentials(self):
        ids=self.box.record(self.trackers,[('router','DOWN',self.results)],self.config['gateways'])
        self.assertEqual(len(ids),1);job=ids[0]
        self.config['gateways']['ntfy']['password']='changed-after-incident'
        with patch('piwatch.notifications.outbox.activated',return_value=True), \
             patch('piwatch.notifications.outbox.send_ntfy',side_effect=ntfy.NtfyError('HTTP429')):
            task=asyncio.create_task(self.box.work('ntfy'))
            await self.until(lambda:self.box.db.execute('SELECT attempts FROM jobs WHERE id=?',(job,)).fetchone()[0]==1)
            self.running=False;await task
        self.assertEqual(self.box.attention()[0]['last_error'],'HTTP429')
        filename=self.box.db.execute('PRAGMA database_list').fetchone()[2]
        self.box.db.close();self.running=True;self.box=Outbox(filename,lambda:self.running)
        self.box.db.execute('UPDATE jobs SET next_attempt=0');self.box.db.commit()
        with patch('piwatch.notifications.outbox.activated',return_value=True),patch('piwatch.notifications.outbox.send_ntfy',return_value={'id':'accepted'}) as send:
            task=asyncio.create_task(self.box.work('ntfy'))
            await self.until(lambda:self.box.summary()['ntfy']['done']==1)
            self.running=False;await task
            self.assertEqual(send.call_args.args[-1]['password'],'synthetic-private-password')
            self.assertEqual(send.call_args.args[3],job.split(':')[0])
        public=json.dumps([self.box.summary(),self.box.attention(),self.box.ntfy_deliveries(),self.box.history()])
        self.assertNotIn('synthetic-private-password',public)
        self.assertEqual(self.box.ntfy_deliveries()[0]['status'],'done')

    async def test_restored_down_state_does_not_replay_history_and_recovers_once(self):
        tracker = self.trackers['router']
        for _ in range(3): tracker.update(False)
        self.box.record(self.trackers, [('router','DOWN',self.results)], self.config['gateways'])
        original_id = self.box.history()[0]['id']
        restored = reconcile_trackers(self.config, {}, {})
        self.box.restore(restored)
        self.assertIsNone(restored['router'].update(False))
        self.assertEqual(self.box.history()[0]['id'], original_id)
        self.assertEqual(self.box.summary()['ntfy']['pending'], 1)
        self.assertEqual(restored['router'].update(True), ('DOWN','UP'))
        self.box.record(restored, [('router','UP',self.results)], self.config['gateways'])
        self.assertEqual(len(self.box.history()), 2)
        self.assertEqual(self.box.summary()['ntfy']['pending'], 2)

    async def test_existing_incident_history_is_not_backfilled_into_new_channel(self):
        old = dict(self.config['gateways']); old['ntfy'] = {'enabled': False}
        self.box.record(self.trackers, [('router','DOWN',self.results)], old)
        self.assertEqual(len(self.box.history()), 1)
        self.assertEqual(self.box.summary()['ntfy']['pending'], 0)

    async def test_test_request_is_deduplicated_and_has_no_fake_incident(self):
        a=self.box.queue_ntfy_test('request',self.config['gateways']['ntfy'])
        b=self.box.queue_ntfy_test('request',self.config['gateways']['ntfy'])
        self.assertEqual(a,b);self.assertEqual(self.box.summary()['ntfy']['pending'],1)
        self.assertEqual(self.box.history(),[])
        raw=self.box.db.execute('SELECT payload FROM jobs').fetchone()[0]
        self.assertIn('test notification',raw)

    async def test_permanent_rejections_are_retained_after_five_attempts(self):
        self.box.queue_ntfy_test('test',self.config['gateways']['ntfy'])
        with patch('piwatch.notifications.outbox.activated',return_value=True),patch('piwatch.notifications.outbox.send_ntfy',side_effect=ntfy.NtfyError('HTTP403')):
            task=asyncio.create_task(self.box.work('ntfy'))
            for count in range(1,6):
                self.box.db.execute('UPDATE jobs SET next_attempt=0');self.box.db.commit()
                await self.until(lambda:(lambda row:row[0]>=count and row[1]>time.time())(self.box.db.execute('SELECT attempts,next_attempt FROM jobs').fetchone()))
            await self.until(lambda:self.box.summary()['ntfy']['failed']==1)
            self.running=False;await task
        self.assertEqual(self.box.ntfy_deliveries()[0]['error'],'HTTP403')
        self.assertIsNone(self.box.ntfy_deliveries()[0]['accepted_at'])

    async def test_long_outage_remains_queued_and_recovers_after_more_than_five_failures(self):
        self.box.queue_ntfy_test('offline', self.config['gateways']['ntfy'])
        with patch('piwatch.notifications.outbox.activated', return_value=True), \
             patch('piwatch.notifications.outbox.send_ntfy', side_effect=ntfy.NtfyError('Timeout')):
            task = asyncio.create_task(self.box.work('ntfy'))
            for count in range(1, 7):
                self.box.db.execute('UPDATE jobs SET next_attempt=0'); self.box.db.commit()
                await self.until(lambda:(lambda row: row[0] >= count and row[1] > time.time())(
                    self.box.db.execute('SELECT attempts,next_attempt FROM jobs').fetchone()))
            self.running = False; await task
        self.assertEqual(self.box.summary()['ntfy']['pending'], 1)
        self.assertEqual(self.box.summary()['ntfy']['retrying'], 1)
        self.running = True; self.box.db.execute('UPDATE jobs SET next_attempt=0'); self.box.db.commit()
        with patch('piwatch.notifications.outbox.activated', return_value=True), \
             patch('piwatch.notifications.outbox.send_ntfy', return_value={'id':'accepted'}):
            task = asyncio.create_task(self.box.work('ntfy'))
            await self.until(lambda:self.box.summary()['ntfy']['done'] == 1)
            self.running = False; await task
        self.assertEqual(self.box.ntfy_deliveries()[0]['attempts'], 7)

    async def test_activation_gate_retains_staged_jobs_without_network(self):
        self.box.queue_ntfy_test('test',self.config['gateways']['ntfy'])
        with patch('piwatch.notifications.outbox.activated',return_value=False),patch('piwatch.notifications.outbox.send_ntfy') as send:
            task=asyncio.create_task(self.box.work('ntfy'));await asyncio.sleep(.05)
            self.running=False;await task;send.assert_not_called()
        self.assertEqual(self.box.summary()['ntfy']['pending'],1)
