#!/usr/bin/env bash
# Update the installed watchdog package and web panel with guarded rollback.
set -euo pipefail
# A legacy bridge requires the coordinated first cutover, never a generic update.
if [[ -f /etc/systemd/system/piwatch-ntfy.service && ! -f /etc/piwatch-ntfy-native/active ]]; then
    echo 'Use deployment/install-native-ntfy.sh --maintenance-ack for the first native migration.' >&2
    exit 1
fi

if [[ ${EUID} -ne 0 ]]; then
    echo 'Run with sudo from an interactive terminal.' >&2
    exit 1
fi

PIWATCH_SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PIWATCH_BACKUP="/var/backups/piwatch/$(date +%Y%m%d-%H%M%S)-dashboard"
PIWATCH_STAGE=$(mktemp -d /usr/local/lib/piwatch-dashboard.XXXXXX)
SERVICES_STOPPED=0

cleanup() {
    status=$?
    set +e
    if [[ $status -ne 0 && $SERVICES_STOPPED -eq 1 ]]; then
        echo 'Update failed; restoring the installed watchdog and web panel.' >&2
        systemctl stop piwatch-ha-mqtt.service 2>/dev/null || true
        systemctl disable piwatch-ha-mqtt.service 2>/dev/null || true
        rm -rf /usr/local/lib/piwatch/piwatch /usr/local/lib/piwatch/web
        rm -f /usr/local/lib/piwatch/piwatchd.py /usr/local/lib/piwatch/piwatch-ha-mqtt.py
        if [[ -d "$PIWATCH_STAGE/installed-piwatch" ]]; then
            mv "$PIWATCH_STAGE/installed-piwatch" /usr/local/lib/piwatch/piwatch
        else
            cp -a "$PIWATCH_BACKUP/usr/local/lib/piwatch/piwatch" /usr/local/lib/piwatch/piwatch
        fi
        if [[ -d "$PIWATCH_STAGE/installed-web" ]]; then
            mv "$PIWATCH_STAGE/installed-web" /usr/local/lib/piwatch/web
        else
            cp -a "$PIWATCH_BACKUP/usr/local/lib/piwatch/web" /usr/local/lib/piwatch/web
        fi
        if [[ -f "$PIWATCH_STAGE/installed-piwatchd.py" ]]; then
            mv "$PIWATCH_STAGE/installed-piwatchd.py" /usr/local/lib/piwatch/piwatchd.py
        else
            cp -a "$PIWATCH_BACKUP/usr/local/lib/piwatch/piwatchd.py" /usr/local/lib/piwatch/piwatchd.py
        fi
        if [[ -f "$PIWATCH_STAGE/installed-piwatch-ha-mqtt.py" ]]; then
            mv "$PIWATCH_STAGE/installed-piwatch-ha-mqtt.py" /usr/local/lib/piwatch/piwatch-ha-mqtt.py
        elif [[ -f "$PIWATCH_BACKUP/usr/local/lib/piwatch/piwatch-ha-mqtt.py" ]]; then
            cp -a "$PIWATCH_BACKUP/usr/local/lib/piwatch/piwatch-ha-mqtt.py" /usr/local/lib/piwatch/piwatch-ha-mqtt.py
        fi
        rm -f /etc/systemd/system/piwatch.service.d/config.conf
        cp -a "$PIWATCH_BACKUP/etc/systemd/system/piwatch.service" /etc/systemd/system/piwatch.service
        if [[ -f "$PIWATCH_STAGE/installed-config.conf" ]]; then
            mv "$PIWATCH_STAGE/installed-config.conf" /etc/systemd/system/piwatch.service.d/config.conf
        elif [[ -f "$PIWATCH_BACKUP/etc/systemd/system/piwatch.service.d/config.conf" ]]; then
            cp -a "$PIWATCH_BACKUP/etc/systemd/system/piwatch.service.d/config.conf" /etc/systemd/system/piwatch.service.d/config.conf
        fi
        rm -f /etc/sudoers.d/piwatch-poweroff
        if [[ -f "$PIWATCH_STAGE/installed-poweroff-sudoers" ]]; then
            mv "$PIWATCH_STAGE/installed-poweroff-sudoers" /etc/sudoers.d/piwatch-poweroff
        elif [[ -f "$PIWATCH_BACKUP/etc/sudoers.d/piwatch-poweroff" ]]; then
            cp -a "$PIWATCH_BACKUP/etc/sudoers.d/piwatch-poweroff" /etc/sudoers.d/piwatch-poweroff
        fi
        rm -f /etc/systemd/system/piwatch-ha-mqtt.service
        if [[ -f "$PIWATCH_STAGE/installed-piwatch-ha-mqtt.service" ]]; then
            mv "$PIWATCH_STAGE/installed-piwatch-ha-mqtt.service" /etc/systemd/system/piwatch-ha-mqtt.service
        elif [[ -f "$PIWATCH_BACKUP/etc/systemd/system/piwatch-ha-mqtt.service" ]]; then
            cp -a "$PIWATCH_BACKUP/etc/systemd/system/piwatch-ha-mqtt.service" /etc/systemd/system/piwatch-ha-mqtt.service
        fi
        systemctl daemon-reload
        systemctl start piwatch
        systemctl start piwatch-web
        if [[ -f "$PIWATCH_STAGE/mqtt-was-enabled" ]]; then
            systemctl enable piwatch-ha-mqtt
        fi
        if [[ -f "$PIWATCH_STAGE/mqtt-was-active" ]]; then
            systemctl start piwatch-ha-mqtt
        fi
    fi
    rm -rf -- "$PIWATCH_STAGE"
    exit "$status"
}
trap cleanup EXIT

cd "$PIWATCH_SOURCE"
python3 - <<'PY'
import ast
from pathlib import Path

for folder in ('watchdog', 'web', 'oled'):
    for path in Path(folder).rglob('*.py'):
        ast.parse(path.read_text(), filename=str(path))
PY

id piwatch-mqtt >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/piwatch-ha-mqtt --shell /usr/sbin/nologin piwatch-mqtt
usermod -a -G piwatch-config piwatch-mqtt
if [[ ! -x /opt/piwatch-mqtt/bin/python ]]; then
    python3 -m venv /opt/piwatch-mqtt
fi
/opt/piwatch-mqtt/bin/python -m pip install -r mqtt/requirements.txt

if systemctl is-enabled --quiet piwatch-ha-mqtt.service 2>/dev/null; then
    touch "$PIWATCH_STAGE/mqtt-was-enabled"
fi
if systemctl is-active --quiet piwatch-ha-mqtt.service 2>/dev/null; then
    touch "$PIWATCH_STAGE/mqtt-was-active"
fi

install -d -m 0700 "$PIWATCH_BACKUP/usr/local/lib/piwatch"
install -d -m 0700 "$PIWATCH_BACKUP/etc/systemd/system/piwatch.service.d"
install -d -m 0700 "$PIWATCH_BACKUP/etc/sudoers.d"
cp -a /usr/local/lib/piwatch/piwatch "$PIWATCH_BACKUP/usr/local/lib/piwatch/piwatch"
cp -a /usr/local/lib/piwatch/web "$PIWATCH_BACKUP/usr/local/lib/piwatch/web"
cp -a /usr/local/lib/piwatch/piwatchd.py "$PIWATCH_BACKUP/usr/local/lib/piwatch/piwatchd.py"
if [[ -f /usr/local/lib/piwatch/piwatch-ha-mqtt.py ]]; then
    cp -a /usr/local/lib/piwatch/piwatch-ha-mqtt.py "$PIWATCH_BACKUP/usr/local/lib/piwatch/piwatch-ha-mqtt.py"
fi
cp -a /etc/systemd/system/piwatch.service "$PIWATCH_BACKUP/etc/systemd/system/piwatch.service"
if [[ -f /etc/systemd/system/piwatch-ha-mqtt.service ]]; then
    install -d -m 0700 "$PIWATCH_BACKUP/etc/systemd/system"
    cp -a /etc/systemd/system/piwatch-ha-mqtt.service "$PIWATCH_BACKUP/etc/systemd/system/piwatch-ha-mqtt.service"
fi
if [[ -f /etc/systemd/system/piwatch.service.d/config.conf ]]; then
    cp -a /etc/systemd/system/piwatch.service.d/config.conf "$PIWATCH_BACKUP/etc/systemd/system/piwatch.service.d/config.conf"
fi
if [[ -f /etc/sudoers.d/piwatch-poweroff ]]; then
    cp -a /etc/sudoers.d/piwatch-poweroff "$PIWATCH_BACKUP/etc/sudoers.d/piwatch-poweroff"
fi
python3 - "$PIWATCH_BACKUP" <<'PY'
from contextlib import closing
from pathlib import Path
import sqlite3
import sys

backup = Path(sys.argv[1])
for raw in ('/var/lib/piwatch/outbox.sqlite3',
            '/var/lib/piwatch-config/settings.sqlite3'):
    source = Path(raw)
    if source.exists():
        target = backup / source.relative_to('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src, \
             closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
PY

python3 - "$PIWATCH_STAGE" <<'PY'
from pathlib import Path
import shutil
import sys

stage = Path(sys.argv[1])
shutil.copytree('watchdog/piwatch', stage / 'new-piwatch',
                ignore=shutil.ignore_patterns('__pycache__'))
shutil.copytree('web', stage / 'new-web', ignore=shutil.ignore_patterns('__pycache__'))
shutil.copy2('watchdog/piwatchd.py', stage / 'new-piwatchd.py')
shutil.copy2('watchdog/piwatch-ha-mqtt.py', stage / 'new-piwatch-ha-mqtt.py')
shutil.copy2('deployment/piwatch.service', stage / 'new-piwatch.service')
shutil.copy2('deployment/piwatch-ha-mqtt.service', stage / 'new-piwatch-ha-mqtt.service')
shutil.copy2('deployment/piwatch-config.conf', stage / 'new-config.conf')
shutil.copy2('deployment/piwatch-poweroff.sudoers', stage / 'new-poweroff-sudoers')
PY

systemd-analyze verify "$PIWATCH_STAGE/new-piwatch.service" "$PIWATCH_STAGE/new-piwatch-ha-mqtt.service"

SERVICES_STOPPED=1
systemctl stop piwatch
systemctl stop piwatch-web
systemctl stop piwatch-ha-mqtt.service 2>/dev/null || true
mv /usr/local/lib/piwatch/piwatch "$PIWATCH_STAGE/installed-piwatch"
mv "$PIWATCH_STAGE/new-piwatch" /usr/local/lib/piwatch/piwatch
mv /usr/local/lib/piwatch/web "$PIWATCH_STAGE/installed-web"
mv "$PIWATCH_STAGE/new-web" /usr/local/lib/piwatch/web
mv /usr/local/lib/piwatch/piwatchd.py "$PIWATCH_STAGE/installed-piwatchd.py"
mv "$PIWATCH_STAGE/new-piwatchd.py" /usr/local/lib/piwatch/piwatchd.py
if [[ -f /usr/local/lib/piwatch/piwatch-ha-mqtt.py ]]; then
    mv /usr/local/lib/piwatch/piwatch-ha-mqtt.py "$PIWATCH_STAGE/installed-piwatch-ha-mqtt.py"
fi
mv "$PIWATCH_STAGE/new-piwatch-ha-mqtt.py" /usr/local/lib/piwatch/piwatch-ha-mqtt.py
install -m 0644 "$PIWATCH_STAGE/new-piwatch.service" /etc/systemd/system/piwatch.service
if [[ -f /etc/systemd/system/piwatch-ha-mqtt.service ]]; then
    mv /etc/systemd/system/piwatch-ha-mqtt.service "$PIWATCH_STAGE/installed-piwatch-ha-mqtt.service"
fi
install -m 0644 "$PIWATCH_STAGE/new-piwatch-ha-mqtt.service" /etc/systemd/system/piwatch-ha-mqtt.service
if [[ -f /etc/systemd/system/piwatch.service.d/config.conf ]]; then
    mv /etc/systemd/system/piwatch.service.d/config.conf "$PIWATCH_STAGE/installed-config.conf"
fi
mv "$PIWATCH_STAGE/new-config.conf" /etc/systemd/system/piwatch.service.d/config.conf
if [[ -f /etc/sudoers.d/piwatch-poweroff ]]; then
    mv /etc/sudoers.d/piwatch-poweroff "$PIWATCH_STAGE/installed-poweroff-sudoers"
fi
visudo -cf "$PIWATCH_STAGE/new-poweroff-sudoers"
install -o root -g root -m 0440 "$PIWATCH_STAGE/new-poweroff-sudoers" /etc/sudoers.d/piwatch-poweroff
chown piwatch-web:piwatch-config /var/lib/piwatch-config/settings.sqlite3
chmod 2770 /var/lib/piwatch-config
chmod 0660 /var/lib/piwatch-config/settings.sqlite3

python3 - <<'PY'
import ast
from pathlib import Path

for folder in ('/usr/local/lib/piwatch/piwatch', '/usr/local/lib/piwatch/web'):
    for path in Path(folder).rglob('*.py'):
        ast.parse(path.read_text(), filename=str(path))
PY
PYTHONPATH=/usr/local/lib/piwatch python3 -c 'from piwatch.health import SystemSampler'
systemd-analyze verify /etc/systemd/system/piwatch.service /etc/systemd/system/piwatch-ha-mqtt.service
systemctl daemon-reload

systemctl start piwatch
systemctl start piwatch-web
systemctl enable --now piwatch-ha-mqtt

WEB_READY=0
for _ in {1..30}; do
    if curl --fail --silent http://127.0.0.1:8080/login >/dev/null; then
        WEB_READY=1
        break
    fi
    sleep .5
done
if [[ $WEB_READY -ne 1 ]]; then
    echo 'The web panel did not accept connections within 15 seconds.' >&2
    exit 1
fi

python3 - <<'PY'
import json
from pathlib import Path
import time

path = Path('/run/piwatch/status.json')
deadline = time.monotonic() + 12
while time.monotonic() < deadline:
    try:
        status = json.loads(path.read_text())
        metrics = status.get('metrics', {})
        battery = status.get('targets', {}).get('battery')
        battery_ready = battery is None or isinstance(battery.get('voltage_v'), (int, float))
        mains = status.get('targets', {}).get('mains')
        mains_ready = (mains is None or
                       (mains.get('state') in ('UP', 'DOWN') and
                        isinstance(mains.get('gpio_level'), bool)))
        supervisor = status.get('ups_supervisor', {})
        supervisor_ready = (supervisor.get('state') == 'IDLE' and
                            supervisor.get('startup_disarmed') is True)
        if (time.time() - status['generated_at'] < 4 and 'cpu_percent' in metrics and
                battery_ready and mains_ready and supervisor_ready):
            gps = status.get('self_health', {}).get('gps', {})
            print('GPS: mode={mode}, seen={seen}, used={used}'.format(
                mode=gps.get('mode', '?'), seen=gps.get('seen', '?'), used=gps.get('used', '?')))
            if battery:
                print('Battery: {voltage_v:.3f} V (low {low_voltage_v:.2f} V, recover {recover_voltage_v:.2f} V)'.format(**battery))
            if mains:
                print('Mains: {state} (GPIO{gpio} {level}, {debounce_seconds:g} s debounce)'.format(
                    level='HIGH' if mains['gpio_level'] else 'LOW', **mains))
            print('Pico supervisor: DISARM sent; shutdown at <= {shutdown_voltage_v:.2f} V for '
                  '{qualification_seconds:.0f} s'.format(**supervisor))
            break
    except (OSError, ValueError, KeyError):
        pass
    time.sleep(.5)
else:
    raise SystemExit('Fresh metrics, battery, GPIO mains, and Pico supervisor state did not appear within 12 seconds')
PY

systemctl is-active --quiet piwatch
systemctl is-active --quiet piwatch-web
systemctl is-active --quiet piwatch-ha-mqtt
systemctl restart piwatch-oled
systemctl is-active --quiet piwatch-oled

bash "$PIWATCH_SOURCE/deployment/refresh-heartbeat-installation.sh"
SERVICES_STOPPED=0
printf 'Updated. Backup: %s\nPanel remains available on its configured bind, port 8080. OLED restarted.\n' "$PIWATCH_BACKUP"
