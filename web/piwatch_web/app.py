"""Authenticated local control panel. Never starts monitoring or radio hardware."""
import hmac
import io
import os
from pathlib import Path
import secrets
import sqlite3
import time

from flask import Flask, abort, g, jsonify, redirect, render_template, request, session
import pyotp
import qrcode
import qrcode.image.svg

from piwatch.config_store import SettingsStore, ConflictError, SETTINGS_PATH
from piwatch.notifications.email import send_email_sync
from .auth import AuthStore, digest
from .status import read_status

ROOT = Path(__file__).resolve().parents[1]


def smtp_ready(config):
    smtp = config['gateways']['smtp']
    return bool(smtp.get('enabled') and all(smtp.get(k) for k in ('host', 'port', 'security', 'sender', 'recipient'))
                and '@' in smtp['recipient'] and ',' not in smtp['recipient']
                and (not smtp.get('username') or smtp.get('password')))


def create_app(state_dir=None, settings_path=None, status_path=None, secure_cookie=None,
               mqtt_status_path=None):
    state_dir = Path(state_dir or os.environ.get('PIWATCH_WEB_STATE', '/var/lib/piwatch-web'))
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_path = state_dir / 'session.key'
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(secrets.token_bytes(32))
    app = Flask(__name__, template_folder=str(ROOT / 'templates'), static_folder=str(ROOT / 'static'))
    app.config.update(SECRET_KEY=key_path.read_bytes(), MAX_CONTENT_LENGTH=65536,
                      SESSION_COOKIE_NAME='piwatch_session', SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE='Strict',
                      SESSION_COOKIE_SECURE=(os.environ.get('PIWATCH_WEB_SECURE_COOKIE', '0') == '1') if secure_cookie is None else secure_cookie)
    auth = AuthStore(state_dir / 'auth.sqlite3')
    settings = SettingsStore(settings_path or SETTINGS_PATH)
    status_path = status_path or os.environ.get('PIWATCH_STATUS_PATH', '/run/piwatch/status.json')
    mqtt_status_path = mqtt_status_path or os.environ.get(
        'PIWATCH_MQTT_STATUS_PATH', '/run/piwatch-ha-mqtt/status.json')
    app.extensions.update(auth_store=auth, settings_store=settings)

    def establish_session():
        session.clear()
        session['sid'] = auth.new_session()
        session['csrf'] = secrets.token_urlsafe(32)

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            abort(400, description='Expected a JSON object')
        return value

    def pending():
        value = session.get('pending', {})
        owner = auth.owner()
        if not owner or value.get('expires', 0) < time.time() or value.get('version') != owner['version']:
            abort(401, description='Sign in again')
        return value

    def require_reauth(data):
        if not auth.rate_limit('reauth:' + (request.remote_addr or 'local')):
            abort(429, description='Too many attempts. Try again in five minutes.')
        if not auth.password_ok(g.owner['username'], data.get('current_password', '')):
            abort(403, description='Current password or verification code is incorrect')
        if g.owner['totp'] or g.owner['email']:
            if not auth.verify_factor(data.get('code', ''), data.get('method', 'totp'), data.get('challenge'),
                                      'reauth', digest(session['sid'])):
                abort(403, description='Current password or verification code is incorrect')

    def send_code(purpose, binding, recipient=None):
        if not auth.rate_limit('email:' + (request.remote_addr or 'local'), limit=1, window=60):
            abort(429, description='Wait one minute before requesting another email')
        _, config = settings.read()
        if not smtp_ready(config):
            abort(400, description='Configure SMTP before using email verification')
        smtp = dict(config['gateways']['smtp'])
        recipient = recipient or smtp['recipient']
        smtp['recipient'] = recipient
        code = str(secrets.randbelow(100000000)).zfill(8)
        token = auth.challenge(purpose, digest(code), binding, recipient)
        try:
            send_email_sync('PiWatch verification code', f'Your PiWatch code is {code}. It expires in five minutes.', settings=smtp)
        except Exception:
            with auth.db() as db:
                db.execute('DELETE FROM challenges WHERE id=?', (token,))
            abort(503, description='Email could not be sent. Check SMTP settings or use another method.')
        return token

    @app.before_request
    def authenticate():
        session.setdefault('csrf', secrets.token_urlsafe(32))
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            supplied = request.headers.get('X-CSRF-Token', '')
            if not supplied or not hmac.compare_digest(supplied, session['csrf']):
                abort(403, description='Session expired. Reload the page.')
        public = {'static', 'login_page', 'login', 'verify_login', 'login_email', 'session_info'}
        g.owner = auth.session_owner(session.get('sid'))
        if request.endpoint not in public and not g.owner:
            if request.path.startswith('/api/'):
                abort(401, description='Sign in to continue')
            return redirect('/login')

    @app.after_request
    def headers(response):
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                                 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
                                 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        return response

    @app.errorhandler(400)
    @app.errorhandler(401)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(405)
    @app.errorhandler(413)
    @app.errorhandler(429)
    @app.errorhandler(503)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.errorhandler(ValueError)
    @app.errorhandler(KeyError)
    @app.errorhandler(TypeError)
    def invalid_input(error):
        # Validation messages contain field names, never submitted values.
        return jsonify(error='Invalid settings. Check field types and required values.' if not isinstance(error, ValueError) else str(error)), 400

    @app.errorhandler(sqlite3.Error)
    def database_error(error):
        return jsonify(error='Local settings are unavailable. Try again shortly.'), 503

    @app.get('/api/session')
    def session_info():
        return jsonify(csrf=session['csrf'], authenticated=bool(g.owner),
                       initialized=bool(auth.owner()), username=g.owner['username'] if g.owner else None)

    @app.get('/login')
    def login_page():
        return render_template('login.html')

    @app.post('/api/login')
    def login():
        data = body()
        if not auth.rate_limit('login:' + (request.remote_addr or 'local')) or not auth.rate_limit('login-account', limit=30):
            abort(429, description='Too many attempts. Try again in five minutes.')
        if not auth.owner():
            abort(503, description='Create the owner account on the Pi before signing in')
        if not auth.password_ok(data.get('username', ''), data.get('password', '')):
            abort(401, description='Username or password is incorrect')
        owner = auth.owner()
        session.clear()
        session['csrf'] = secrets.token_urlsafe(32)
        if owner['totp'] or owner['email']:
            session['pending'] = {'binding': secrets.token_urlsafe(32), 'expires': time.time()+300, 'version': owner['version']}
            return jsonify(mfa=True, totp=bool(owner['totp']), email=bool(owner['email']) and smtp_ready(settings.read()[1]), csrf=session['csrf'])
        establish_session()
        return jsonify(mfa=False, csrf=session['csrf'])

    @app.post('/api/login/email')
    def login_email():
        value = pending()
        owner = auth.owner()
        if not owner['email']:
            abort(400, description='Email verification is not enabled')
        return jsonify(challenge=send_code('login', value['binding'], owner['email']))

    @app.post('/api/login/verify')
    def verify_login():
        value = pending()
        if not auth.rate_limit('factor:' + value['binding'], limit=5):
            abort(429, description='Too many codes. Sign in again.')
        data = body()
        if not auth.verify_factor(data.get('code', ''), data.get('method', 'totp'), data.get('challenge'), 'login', value['binding']):
            abort(401, description='Code is incorrect, expired, or already used')
        establish_session()
        return jsonify(ok=True, csrf=session['csrf'])

    @app.post('/api/logout')
    def logout():
        auth.logout(session.get('sid'))
        session.clear()
        return jsonify(ok=True)

    @app.get('/')
    def dashboard():
        return render_template('dashboard.html', username=g.owner['username'])

    @app.get('/settings')
    def settings_page():
        return render_template('settings.html', username=g.owner['username'])

    @app.get('/account')
    def account_page():
        return render_template('account.html', username=g.owner['username'])

    @app.get('/api/status')
    def status():
        return jsonify(read_status(status_path, mqtt_status_path))

    @app.get('/api/settings')
    def get_settings():
        return jsonify(settings.public())

    @app.put('/api/settings')
    def save_settings():
        data = body()
        require_reauth(data)
        try:
            revision = settings.save(data['config'], data['revision'], g.owner['username'])
        except ConflictError as exc:
            return jsonify(error=str(exc)), 409
        return jsonify(ok=True, revision=revision)

    @app.post('/api/maintenance')
    def request_maintenance():
        data = body()
        require_reauth(data)
        request_id = settings.request_maintenance(data.get('action'), g.owner['username'])
        return jsonify(ok=True, request_id=request_id), 202

    @app.get('/api/maintenance/<request_id>')
    def maintenance_status(request_id):
        value = settings.maintenance_status(request_id)
        if value is None:
            abort(404, description='Maintenance request not found')
        return jsonify(value)

    @app.get('/api/account')
    def account():
        _, config = settings.read()
        owner = g.owner
        return jsonify(username=owner['username'], idle_minutes=owner['idle_minutes'],
                       totp=bool(owner['totp']), email=bool(owner['email']), smtp_ready=smtp_ready(config),
                       recovery_remaining=len(__import__('json').loads(owner['recovery'])))

    @app.post('/api/account')
    def change_account():
        data = body()
        require_reauth(data)
        auth.update_account(data['username'], data.get('password', ''), data['idle_minutes'])
        establish_session()
        return jsonify(ok=True, csrf=session['csrf'])

    @app.post('/api/account/email-code')
    def reauth_email():
        if not g.owner['email']:
            abort(400, description='Email verification is not enabled')
        return jsonify(challenge=send_code('reauth', digest(session['sid']), g.owner['email']))

    @app.post('/api/account/totp/start')
    def start_totp():
        require_reauth(body())
        secret = pyotp.random_base32()
        token = auth.challenge('totp_setup', secret, digest(session['sid']), ttl=600)
        uri = pyotp.TOTP(secret).provisioning_uri(g.owner['username'], issuer_name='PiWatch')
        image = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
        output = io.BytesIO()
        image.save(output)
        import base64
        return jsonify(challenge=token, secret=secret, qr='data:image/svg+xml;base64,' + base64.b64encode(output.getvalue()).decode())

    @app.post('/api/account/totp/confirm')
    def confirm_totp():
        data = body()
        codes = auth.enable_totp(data['challenge'], data['code'], digest(session['sid']))
        if codes is None:
            abort(400, description='Code is incorrect or setup has expired')
        establish_session()
        return jsonify(ok=True, recovery_codes=codes, csrf=session['csrf'])

    @app.post('/api/account/email/start')
    def start_email():
        require_reauth(body())
        return jsonify(challenge=send_code('email_setup', digest(session['sid'])))

    @app.post('/api/account/email/confirm')
    def confirm_email():
        data = body()
        email = auth.consume_email(data['challenge'], 'email_setup', data['code'], digest(session['sid']))
        if not email:
            abort(400, description='Code is incorrect or setup has expired')
        codes = auth.enable_email(email)
        establish_session()
        return jsonify(ok=True, recovery_codes=codes, csrf=session['csrf'])

    @app.post('/api/account/factor/disable')
    def disable_factor():
        data = body()
        require_reauth(data)
        if data.get('method_to_disable') not in ('totp', 'email'):
            abort(400)
        auth.disable_factor(data['method_to_disable'])
        establish_session()
        return jsonify(ok=True, csrf=session['csrf'])

    return app
