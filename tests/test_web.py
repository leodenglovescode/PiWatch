"""Panel security and status tests: no real email, radio, or service calls."""
import copy
import json
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'watchdog'))
sys.path.insert(0, str(ROOT / 'web'))
try:
    import pyotp
    from piwatch_web.app import create_app
    from piwatch_web.status import read_status
except ImportError as exc:
    raise unittest.SkipTest('Install web/requirements.txt to run panel tests') from exc
from piwatch.config_store import SettingsStore
from piwatch.health import boot_id

PASSWORD = 'synthetic long test passphrase'


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SettingsStore(self.root / 'config.sqlite3')
        self.store.initialize({'targets':[]})
        self.app = create_app(self.root / 'web', self.store.path, self.root / 'status.json')
        self.app.config['TESTING'] = True
        self.auth = self.app.extensions['auth_store']
        self.auth.create_owner('owner', PASSWORD)
        self.client = self.app.test_client()
        self.csrf = self.client.get('/api/session').json['csrf']

    def tearDown(self):
        self.temp.cleanup()

    def post(self, path, data, method='POST'):
        response = self.client.open(path, method=method, json=data, headers={'X-CSRF-Token':self.csrf})
        if response.is_json and response.json.get('csrf'):
            self.csrf = response.json['csrf']
        return response

    def login(self):
        return self.post('/api/login', {'username':'owner','password':PASSWORD})

    def setup_totp(self):
        self.login()
        result = self.post('/api/account/totp/start', {'current_password':PASSWORD})
        self.assertEqual(result.status_code, 200)
        secret = result.json['secret']
        code = pyotp.TOTP(secret).now()
        response = self.post('/api/account/totp/confirm', {'challenge':result.json['challenge'],'code':code})
        self.assertEqual(response.status_code, 200)
        return secret, code, response.json['recovery_codes']

    def configure_smtp(self):
        revision, config = self.store.read()
        config['gateways']['smtp'] = {'enabled':True,'host':'mail.example.test','port':465,'security':'ssl','sender':'pi@example.test','recipient':'owner@example.test','username':'owner','password':'synthetic-email-password'}
        self.store.save(config, revision, 'test')

    def test_login_required_and_csrf_enforced(self):
        self.assertEqual(self.client.get('/api/settings').status_code, 401)
        self.assertEqual(self.client.get('/api/status').status_code, 401)
        self.assertEqual(self.client.post('/api/login', json={'username':'owner','password':PASSWORD}).status_code, 403)
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.client.get('/api/settings').status_code, 200)
        self.assertNotIn(PASSWORD, self.auth.path.read_bytes().decode(errors='ignore'))

    def test_templates_and_static_assets_render(self):
        self.login()
        for path in ('/','/settings','/account','/login','/static/style.css','/static/common.js','/static/dashboard.js','/static/settings.js','/static/account.js','/static/login.js'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
            response.close()

    def test_settings_require_password_and_do_not_return_secrets(self):
        self.login()
        value = self.client.get('/api/settings').json
        value['config']['gateways']['lora']['token'] = 'synthetic-sensitive-token'
        self.assertEqual(self.post('/api/settings', value, 'PUT').status_code, 403)
        value['current_password'] = PASSWORD
        self.assertEqual(self.post('/api/settings', value, 'PUT').status_code, 200)
        self.assertNotIn('synthetic-sensitive-token', self.client.get('/api/settings').text)
        self.assertEqual(self.post('/api/settings', value, 'PUT').status_code, 409)

    def test_login_rate_limit(self):
        for _ in range(8):
            self.assertEqual(self.post('/api/login', {'username':'owner','password':'wrong'}).status_code, 401)
        self.assertEqual(self.login().status_code, 429)

    def test_logout_revokes_copied_session(self):
        self.login()
        cookie = self.client.get_cookie('piwatch_session').value
        self.post('/api/logout', {})
        self.client.set_cookie('piwatch_session', cookie)
        self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_password_change_revokes_other_sessions(self):
        self.login()
        copied = self.client.get_cookie('piwatch_session').value
        response = self.post('/api/account', {'current_password':PASSWORD,'username':'newowner','password':'replacement long passphrase','idle_minutes':15})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/api/account').json['username'], 'newowner')
        self.client.set_cookie('piwatch_session', copied)
        self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_totp_enrollment_replay_and_single_use_recovery(self):
        secret, used, codes = self.setup_totp()
        self.assertFalse(self.auth.verify_factor(used))
        self.post('/api/logout', {})
        self.csrf = self.client.get('/api/session').json['csrf']
        response = self.login()
        self.assertTrue(response.json['mfa'])
        self.assertEqual(self.client.get('/api/status').status_code, 401)
        response = self.post('/api/login/verify', {'method':'recovery','code':codes[0]})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.auth.verify_factor(codes[0], 'recovery'))
        self.assertEqual(self.client.get('/api/account').json['recovery_remaining'], 7)

    def test_session_expiry(self):
        self.login()
        with patch('piwatch_web.auth.time.time', return_value=time.time()+1900):
            self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_email_unavailable_until_smtp_configured_and_verified(self):
        self.login()
        self.assertFalse(self.client.get('/api/account').json['smtp_ready'])
        self.assertEqual(self.post('/api/account/email/start', {'current_password':PASSWORD}).status_code, 400)
        self.configure_smtp()
        self.assertTrue(self.client.get('/api/account').json['smtp_ready'])
        # Reset the test rate bucket; failed setup also consumes a resend attempt.
        with self.auth.db() as db:
            db.execute('DELETE FROM attempts')
        with patch('piwatch_web.app.send_email_sync') as send:
            start = self.post('/api/account/email/start', {'current_password':PASSWORD})
        self.assertEqual(start.status_code, 200)
        self.assertFalse(self.client.get('/api/account').json['email'])
        code = re.search(r'\b\d{8}\b', send.call_args.args[1]).group()
        confirm = self.post('/api/account/email/confirm', {'challenge':start.json['challenge'],'code':code})
        self.assertEqual(confirm.status_code, 200)
        self.assertTrue(self.client.get('/api/account').json['email'])
        self.assertEqual(self.post('/api/account/email/confirm', {'challenge':start.json['challenge'],'code':code}).status_code, 400)

    def test_email_codes_expire_and_lock_after_five_wrong_attempts(self):
        from piwatch_web.auth import digest
        token = self.auth.challenge('login', digest('12345678'), 'binding', 'owner@example.test')
        for _ in range(5):
            self.assertIsNone(self.auth.consume_email(token, 'login', 'bad-code', 'binding'))
        self.assertIsNone(self.auth.consume_email(token, 'login', '12345678', 'binding'))
        token = self.auth.challenge('login', digest('12345678'), 'binding', 'owner@example.test', ttl=-1)
        self.assertIsNone(self.auth.consume_email(token, 'login', '12345678', 'binding'))

    def test_status_fails_closed_when_old_missing_or_previous_boot(self):
        path = self.root / 'status.json'
        self.assertFalse(read_status(path)['fresh'])
        data = {'schema_version':2,'boot_id':boot_id(),'generated_at':time.time(),'generated_monotonic':time.monotonic(),
                'stale_after_seconds':10,'health_stale_after_seconds':45,'self_health':{'checked_monotonic':time.monotonic()}}
        path.write_text(json.dumps(data))
        self.assertTrue(read_status(path)['fresh'])
        data['generated_monotonic'] -= 30
        path.write_text(json.dumps(data))
        self.assertFalse(read_status(path)['fresh'])
        data['boot_id'] = 'a-previous-boot'
        path.write_text(json.dumps(data))
        self.assertFalse(read_status(path)['fresh'])

    def test_email_login_is_bound_to_pending_password_session(self):
        self.configure_smtp()
        self.auth.enable_email('owner@example.test')
        self.assertTrue(self.login().json['mfa'])
        with patch('piwatch_web.app.send_email_sync') as send:
            response = self.post('/api/login/email', {})
        code = re.search(r'\b\d{8}\b', send.call_args.args[1]).group()
        self.assertEqual(self.client.get('/api/status').status_code, 401)
        verified = self.post('/api/login/verify', {'method':'email','challenge':response.json['challenge'],'code':code})
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(self.client.get('/api/status').status_code, 200)

    def test_mfa_is_required_to_change_account_or_gateways(self):
        secret, code, recovery = self.setup_totp()
        data = self.client.get('/api/settings').json
        data['current_password'] = PASSWORD
        self.assertEqual(self.post('/api/settings', data, 'PUT').status_code, 403)
        data.update(method='recovery',code=recovery[0])
        self.assertEqual(self.post('/api/settings', data, 'PUT').status_code, 200)
        self.assertEqual(self.post('/api/settings', data, 'PUT').status_code, 403)
