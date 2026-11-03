#!/usr/bin/env python3
"""Privileged, quiet-window native ntfy migration with bounded rollback.

No credentials or complete settings rows are printed or written to evidence.
The original bridge program, credential, state and unit are retained.
"""
import argparse
import ast
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

INSTALL = Path('/usr/local/lib/piwatch')
PRIVATE = Path('/etc/piwatch-ntfy-native')
EVIDENCE = Path('/var/lib/piwatch-ntfy-native/evidence.json')
SETTINGS = Path('/var/lib/piwatch-config/settings.sqlite3')
OUTBOX = Path('/var/lib/piwatch/outbox.sqlite3')
BRIDGE_STATE = Path('/var/lib/piwatch-ntfy/state.json')
BRIDGE_PROGRAM = Path('/usr/local/lib/piwatch-ntfy.py')
BRIDGE_CREDENTIAL = Path('/etc/piwatch-ntfy/credentials.json')
BRIDGE_CERT = Path('/etc/piwatch-ntfy/server.crt')
BRIDGE_UNIT = Path('/etc/systemd/system/piwatch-ntfy.service')
SNAPSHOT = Path('/run/piwatch/status.json')
HEARTBEAT = Path('/run/piwatch-heartbeat/status.json')


def command(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()


def systemctl(*args):
    return command('systemctl', *args)


def active(unit):
    return subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0


def dump(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, allow_nan=False))
    temp.chmod(mode)
    os.replace(temp, path)


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def config_row():
    with closing(sqlite3.connect(SETTINGS.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        revision, raw = db.execute('SELECT revision,value FROM settings WHERE id=1').fetchone()
    return revision, json.loads(raw)


def healthy_snapshot():
    value = json.loads(SNAPSHOT.read_text())
    age = time.monotonic() - value.get('generated_monotonic', -1e12)
    if value.get('boot_id') != Path('/proc/sys/kernel/random/boot_id').read_text().strip() or not 0 <= age < 3:
        raise RuntimeError('A fresh current-boot watchdog snapshot is required')
    supervisor = value.get('ups_supervisor', {})
    if supervisor.get('state') != 'IDLE' or supervisor.get('startup_disarmed') is not True:
        raise RuntimeError('Cutover requires an idle, successfully disarmed UPS supervisor')
    if not value.get('targets') or any(item.get('state') != 'UP' for item in value['targets'].values()):
        raise RuntimeError('Cutover requires a quiet window with every monitored target UP')
    return value


def bridge_is_quiet():
    value = json.loads(BRIDGE_STATE.read_text())
    return not (value.get('pending') or value.get('stale'))


def bridge_quiet():
    if not bridge_is_quiet():
        raise RuntimeError('Wait for the bridge queue to drain and monitoring to recover')


def legacy_contract():
    tree = ast.parse(BRIDGE_PROGRAM.read_text())
    urls = {node.value.rstrip('/') for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith('https://')}
    topics = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == 'topic' and isinstance(value, ast.Constant) and isinstance(value.value, str):
                    topics.add(value.value)
    heartbeat = {item for item in topics if 'heartbeat' in item}
    alerts = topics - heartbeat
    if len(urls) != 1 or len(heartbeat) != 1 or len(alerts) != 1:
        raise RuntimeError('Bridge endpoint/topic contract changed; inspect before migration')
    # Privileged migration reads only the existing Pi-only credential.
    credentials = json.loads(BRIDGE_CREDENTIAL.read_text())
    return {'enabled': True, 'heartbeat_enabled': True, 'url': urls.pop(),
            'topic': alerts.pop(), 'heartbeat_topic': heartbeat.pop(),
            'username': credentials['username'], 'password': credentials['password'],
            'tls_trust': 'private_ca'}


def wait_for(predicate, seconds, description):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(.2)
    raise RuntimeError(description)


def restore_configuration(previous):
    # Restore only fields owned by this migration; preserve unrelated concurrent edits.
    with closing(sqlite3.connect(SETTINGS)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        revision, raw = db.execute('SELECT revision,value FROM settings WHERE id=1').fetchone()
        value = json.loads(raw)
        if previous['ntfy'] is None:
            value.get('gateways', {}).pop('ntfy', None)
        else:
            value['gateways']['ntfy'] = previous['ntfy']
        if previous['network_policy'] is None:
            value.pop('network_policy', None)
        else:
            value['network_policy'] = previous['network_policy']
        db.execute('UPDATE settings SET revision=?,value=?,updated_at=? WHERE id=1',
                   (revision+1, json.dumps(value), time.time()))


def rollback(backup):
    metadata = json.loads((backup/'migration.json').read_text())
    (PRIVATE/'active').unlink(missing_ok=True)
    subprocess.run(['systemctl', 'stop', 'piwatch-heartbeat'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['systemctl', 'disable', 'piwatch-heartbeat'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Resume the old heartbeat immediately; no native publisher remains active.
    systemctl('enable' if metadata.get('bridge_enabled', True) else 'disable', 'piwatch-ntfy')
    systemctl('start', 'piwatch-ntfy')
    systemctl('stop', 'piwatch-web', 'piwatch')
    with closing(sqlite3.connect(OUTBOX)) as db, db:
        rows = db.execute("SELECT id,payload,status FROM jobs WHERE channel='ntfy'").fetchall()
        # Preserve native-only work privately for operator review; old code cannot read this channel.
        dump(backup/'native-jobs-on-rollback.json', rows)
        db.execute("DELETE FROM jobs WHERE channel='ntfy'")
    restore_configuration(metadata['previous'])
    for name in ('piwatch', 'web'):
        current = INSTALL/name
        if current.exists():
            shutil.rmtree(current)
        shutil.copytree(backup/name, current)
    shutil.copy2(backup/'piwatchd.py', INSTALL/'piwatchd.py')
    systemctl('start', 'piwatch', 'piwatch-web')
    print('Rollback restored the previous PiWatch code/configuration and legacy bridge. Native-only jobs retained privately.')


def handover():
    """A short writer barrier closes the incident/publisher handoff race."""
    # Barrier prevents an incident being observed by the old bridge and queued natively
    # in the gap between the final quiet check and stopping the old publisher.
    with closing(sqlite3.connect(OUTBOX, timeout=3)) as db:
        db.execute('BEGIN IMMEDIATE')
        healthy_snapshot(); bridge_quiet()
        if db.execute("SELECT COUNT(*) FROM jobs WHERE channel='ntfy'").fetchone()[0]:
            raise RuntimeError('An incident occurred during staging; rollback and retry a quiet window')
        systemctl('stop', 'piwatch-ntfy')
        systemctl('disable', 'piwatch-ntfy')
        bridge_quiet()
        stopped_at = time.time()
        (PRIVATE/'active').write_text('Native ownership enabled after legacy bridge stop.\n')
        (PRIVATE/'active').chmod(0o644)
        db.commit()
    return stopped_at


def install(source, maintenance_ack):
    if not maintenance_ack:
        raise RuntimeError('Use --maintenance-ack: a prolonged maintenance gap can intentionally trigger the VPS warning')
    if (PRIVATE/'active').exists() or active('piwatch-heartbeat'):
        raise RuntimeError('Native publishing already exists; do not rerun a first cutover')
    if not all(active(unit) for unit in ('piwatch', 'piwatch-web', 'piwatch-ntfy')):
        raise RuntimeError('The current watchdog, panel and bridge must all be active')
    healthy_snapshot(); bridge_quiet()
    for name in ('ups_supervisor.py', 'mains.py', 'battery.py'):
        if fingerprint(source/'watchdog/piwatch'/name) != fingerprint(INSTALL/'piwatch'/name):
            raise RuntimeError('Power safety source differs from production; inspect before deployment')
    with closing(sqlite3.connect(OUTBOX.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        if db.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0]:
            raise RuntimeError('Wait for existing delivery work to complete before cutover')
    contract = legacy_contract()
    backup = Path('/var/backups/piwatch')/(time.strftime('%Y%m%d-%H%M%S')+'-ntfy-native-'+str(os.getpid()))
    backup.mkdir(parents=True, mode=0o700)
    os.chmod(backup, 0o700)
    _, previous = config_row()
    import importlib.util
    spec = importlib.util.spec_from_file_location('cutover_config', source/'watchdog/piwatch/config.py')
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    baseline_battery = validator.validate_config(previous)['battery']
    metadata = {'previous': {'ntfy': previous.get('gateways', {}).get('ntfy'),
                             'network_policy': previous.get('network_policy')},
                'battery_hash': hashlib.sha256(json.dumps(baseline_battery, sort_keys=True).encode()).hexdigest(),
                'bridge_program_sha256': fingerprint(BRIDGE_PROGRAM),
                'bridge_unit_sha256': fingerprint(BRIDGE_UNIT),
                'server_certificate_sha256': fingerprint(BRIDGE_CERT),
                'old_monitor_sha256': fingerprint(INSTALL/'piwatch/daemon.py'),
                'bridge_enabled': systemctl('is-enabled', 'piwatch-ntfy') == 'enabled'}
    dump(backup/'migration.json', metadata)
    for name in ('piwatch', 'web'):
        shutil.copytree(INSTALL/name, backup/name, ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(INSTALL/'piwatchd.py', backup/'piwatchd.py')
    # Public certificate only. The old bridge retains its original credential/trust files.
    PRIVATE.mkdir(mode=0o755, exist_ok=True); PRIVATE.chmod(0o755)
    shutil.copy2(BRIDGE_CERT, PRIVATE/'server.crt')
    (PRIVATE/'server.crt').chmod(0o644)
    if fingerprint(PRIVATE/'server.crt') != fingerprint(BRIDGE_CERT):
        raise RuntimeError('TLS certificate copy differs')
    if subprocess.run(['id', 'piwatch-heartbeat'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        command('useradd', '--system', '--user-group', '--no-create-home', '--shell', '/usr/sbin/nologin', 'piwatch-heartbeat')
    command('usermod', '-a', '-G', 'piwatch-config', 'piwatch-heartbeat')
    print('Preflight passed; private source/configuration rollback prepared.', flush=True)
    changed = False
    try:
        # Keep the legacy heartbeat running while the core is replaced.
        print('Loading native code: watchdog and panel restart; legacy bridge remains active.', flush=True)
        changed = True
        systemctl('stop', 'piwatch-web', 'piwatch')
        for name, origin in (('piwatch', source/'watchdog/piwatch'), ('web', source/'web')):
            shutil.rmtree(INSTALL/name)
            shutil.copytree(origin, INSTALL/name, ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(source/'watchdog/piwatchd.py', INSTALL/'piwatchd.py')
        shutil.copy2(source/'watchdog/piwatch-heartbeat.py', INSTALL/'piwatch-heartbeat.py')
        shutil.copy2(source/'deployment/piwatch-heartbeat.service', '/etc/systemd/system/piwatch-heartbeat.service')
        sys.path.insert(0, str(INSTALL))
        from piwatch.config_store import SettingsStore
        from piwatch.notifications.ntfy import NtfyClient
        store = SettingsStore(SETTINGS)
        revision, value = store.read()
        value['gateways']['ntfy'] = contract
        revision = store.save(value, revision, 'ntfy-cutover')
        NtfyClient(contract)  # Trust parsing only; no request before the handover.
        for user in ('piwatch', 'piwatch-heartbeat'):
            command('runuser', '-u', user, '--', '/usr/bin/python3', str(INSTALL/'piwatch-heartbeat.py'), '--check-config')
        systemctl('daemon-reload')
        systemctl('start', 'piwatch', 'piwatch-web')
        systemctl('enable', '--now', 'piwatch-heartbeat')
        wait_for(lambda: healthy_snapshot().get('monitor', {}).get('settings_revision') == revision,
                 15, 'New watchdog did not produce fresh healthy status')
        wait_for(lambda: json.loads(HEARTBEAT.read_text()).get('state') == 'STAGED',
                 5, 'Native heartbeat did not reach the non-publishing staged state')
        print('Native workers staged. Waiting for the legacy bridge to finish any restart-related deliveries.', flush=True)
        wait_for(bridge_is_quiet, 45, 'Legacy bridge did not clear its pending alerts or stale state within 45 seconds')
        print('Performing the publisher handover.', flush=True)
        stopped_at = handover()
        print('Legacy publisher stopped. Observing 80 consecutive native heartbeat acceptances.', flush=True)
        samples = []
        last_id = None
        last_count = None
        deadline = time.monotonic() + 100
        while len(samples) < 80 and time.monotonic() < deadline:
            if active('piwatch-ntfy'):
                raise RuntimeError('Duplicate legacy publisher detected')
            value = json.loads(HEARTBEAT.read_text())
            if value.get('state') == 'STAGED' and time.time()-stopped_at < 2:
                time.sleep(.1); continue
            if (value.get('state') != 'UP' or value.get('error') or value.get('failed_samples', 0) or not value.get('piwatch_fresh') or
                    time.time()-value.get('last_accepted_at', 0) > 3):
                raise RuntimeError('Native heartbeat failed/staled during observation')
            if value['last_message_id'] != last_id:
                if last_count is not None and value['accepted_samples'] != last_count + 1:
                    raise RuntimeError('Heartbeat observation skipped a sample or the worker restarted')
                last_count = value['accepted_samples']
                last_id = value['last_message_id']
                samples.append({'id': last_id, 'accepted_at': value['last_accepted_at']})
                if len(samples) % 20 == 0:
                    print(str(len(samples))+'/80 accepted; monitoring evidence fresh; no heartbeat errors.', flush=True)
            time.sleep(.15)
        if len(samples) < 80:
            raise RuntimeError('Fewer than eighty distinct accepted heartbeat samples')
        # One labelled test through the actual native persistent outbox.
        request_id = store.request_maintenance('test_ntfy', 'ntfy-cutover')
        job_id = request_id + ':ntfy'
        def test_accepted():
            with closing(sqlite3.connect(OUTBOX.resolve().as_uri()+'?mode=ro', uri=True)) as db:
                row = db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
            return row and row[0] == 'done'
        wait_for(test_accepted, 20, 'Native test notification was not accepted; rollback required')
        _, after = store.read()
        if hashlib.sha256(json.dumps(after['battery'], sort_keys=True).encode()).hexdigest() != metadata['battery_hash']:
            raise RuntimeError('UPS/battery settings changed during cutover')
        EVIDENCE.parent.mkdir(mode=0o755, exist_ok=True); EVIDENCE.parent.chmod(0o755)
        proof = {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                 'completed_at': time.time(), 'stopped_bridge_at': stopped_at,
                 'heartbeat_topic': contract['heartbeat_topic'], 'samples': samples,
                 'test_job_id': job_id, 'phone_receipt_confirmed': False,
                 'bridge_program_sha256': metadata['bridge_program_sha256'],
                 'native_daemon_sha256': fingerprint(INSTALL/'piwatch/daemon.py'),
                 'native_heartbeat_sha256': fingerprint(INSTALL/'piwatch/heartbeat.py')}
        dump(EVIDENCE, proof, 0o644)
        print('Native ntfy cutover passed: 80 distinct one-second acceptances and one labelled test accepted.')
        print('Please confirm physical phone receipt; server acceptance alone is not delivery confirmation.')
        print('Legacy bridge disabled; original program, unit, state and credentials retained.')
        print('Private rollback backup: '+str(backup))
    except Exception:
        if HEARTBEAT.exists():
            try:
                dump(backup/'failure-heartbeat.json', json.loads(HEARTBEAT.read_text()))
            except (OSError, ValueError):
                pass
        if changed:
            rollback(backup)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--maintenance-ack', action='store_true')
    parser.add_argument('--rollback', type=Path)
    parser.add_argument('--confirm-phone', action='store_true', help='Record the operator-confirmed test receipt; never inferred from HTTP')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with sudo from an interactive terminal')
    try:
        if args.confirm_phone:
            proof = json.loads(EVIDENCE.read_text())
            proof['phone_receipt_confirmed'] = True
            proof['phone_confirmation_recorded_at'] = time.time()
            dump(EVIDENCE, proof, 0o644)
            print('Recorded the operator-confirmed phone receipt.')
        elif args.rollback:
            rollback(args.rollback.resolve())
        else:
            install(args.source.resolve(), args.maintenance_ack)
    except Exception as exc:
        # Do not print exception values from credentials, TLS, JSON, or settings.
        if isinstance(exc, RuntimeError):
            print('Cutover stopped: '+str(exc), file=sys.stderr)
        else:
            print('Cutover stopped: '+type(exc).__name__+' (details withheld)', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
