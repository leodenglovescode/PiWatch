"""Authenticated ntfy dashboard workflow; synthetic settings, no publishing."""
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch
import test_web as existing
from piwatch.notifications.outbox import Outbox
from piwatch.daemon import process_maintenance
from piwatch.health import boot_id
from piwatch_web.status import read_heartbeat_status


class NtfyWebTests(unittest.TestCase):
    setUp = existing.WebTests.setUp
    tearDown = existing.WebTests.tearDown
    post = existing.WebTests.post
    login = existing.WebTests.login

    def configured(self):
        revision, value = self.store.read()
        value['gateways']['ntfy'].update(enabled=True, heartbeat_enabled=True,
            url='https://192.0.2.10:9443', topic='alerts', heartbeat_topic='heartbeat',
            username='synthetic-private-user', password='synthetic-private-password')
        self.store.save(value, revision, 'test')

    def test_public_api_preserves_both_omitted_credentials_on_update(self):
        self.configured();self.login()
        public = self.client.get('/api/settings').json
        self.assertNotIn('synthetic-private', json.dumps(public))
        self.assertTrue(public['configured_secrets']['ntfy']['username'])
        public['config']['interval_seconds'] = 2
        self.assertEqual(self.post('/api/settings', public, 'PUT').status_code, 200)
        private = self.store.read()[1]['gateways']['ntfy']
        self.assertEqual(private['username'], 'synthetic-private-user')
        self.assertEqual(private['password'], 'synthetic-private-password')

    def test_disabling_heartbeat_requires_explicit_maintenance_acknowledgement(self):
        self.configured();self.login()
        public = self.client.get('/api/settings').json
        public['config']['gateways']['ntfy']['heartbeat_enabled'] = False
        self.assertEqual(self.post('/api/settings', public, 'PUT').status_code, 400)
        self.assertTrue(self.store.read()[1]['gateways']['ntfy']['heartbeat_enabled'])
        public['acknowledge_heartbeat_disable'] = True
        self.assertEqual(self.post('/api/settings', public, 'PUT').status_code, 200)

    def test_test_action_is_queued_durably_not_claimed_as_phone_delivery(self):
        self.configured();self.login()
        with patch('piwatch.notifications.ntfy.activated', return_value=True):
            response = self.post('/api/ntfy/test', {})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json['state'], 'queued')
        self.assertIn('does not confirm phone', response.json['message'])
        request_id = response.json['request_id']
        box = Outbox(':memory:')
        try:
            process_maintenance(self.store, box)
            self.assertEqual(box.summary()['ntfy']['pending'], 1)
            self.assertEqual(self.store.maintenance_status(request_id)['result']['job_id'], request_id+':ntfy')
            self.assertNotIn('synthetic-private', json.dumps(box.ntfy_deliveries()))
        finally:
            box.db.close()

    def test_staged_test_action_cannot_publish(self):
        self.configured();self.login()
        with patch('piwatch.notifications.ntfy.activated', return_value=False):
            self.assertEqual(self.post('/api/ntfy/test', {}).status_code, 409)
        self.assertEqual(self.store.pending_maintenance(), [])

    def test_generic_maintenance_cannot_bypass_labelled_test_gate(self):
        self.configured(); self.login()
        self.assertEqual(self.post('/api/maintenance', {'action':'test_ntfy'}).status_code, 400)

    def test_test_action_requires_login_and_csrf(self):
        self.assertEqual(self.post('/api/ntfy/test', {}).status_code, 401)
        self.login()
        self.assertEqual(self.client.post('/api/ntfy/test',json={}).status_code, 403)

    def test_heartbeat_reader_redacts_unknown_fields_and_rejects_old_boot(self):
        path = self.root/'heartbeat.json'
        value = {'boot_id': boot_id(), 'generated_at':time.time(), 'last_accepted_at':time.time(),
                 'state':'UP','piwatch_fresh':True,'password':'synthetic-private-password','username':'synthetic-private-user'}
        path.write_text(json.dumps(value))
        self.assertNotIn('synthetic-private',json.dumps(read_heartbeat_status(path)))
        value['boot_id']='old-boot';path.write_text(json.dumps(value))
        self.assertEqual(read_heartbeat_status(path)['state'],'STALE')
