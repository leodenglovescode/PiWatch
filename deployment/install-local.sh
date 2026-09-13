#!/usr/bin/env bash
# Install only on this Pi, from the local checkout. Never upload repository files.
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
    echo 'Run with sudo from an interactive terminal.' >&2
    exit 1
fi
PIWATCH_SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PIWATCH_BACKUP="/var/backups/piwatch/$(date +%Y%m%d-%H%M%S)"
cd "$PIWATCH_SOURCE"
# Parse source without importing services or creating production bytecode caches.
python3 - <<'PY'
import ast
from pathlib import Path
for folder in ('watchdog', 'lora', 'oled', 'web'):
    for path in Path(folder).rglob('*.py'):
        ast.parse(path.read_text(), filename=str(path))
PY
install -d -m 0700 "$PIWATCH_BACKUP"
# Back up installed source/units and consistent database snapshots; never copy env files.
python3 - "$PIWATCH_BACKUP" "$PIWATCH_SOURCE" <<'PY'
from pathlib import Path
import shutil
import sqlite3
import sys
from contextlib import closing
backup, source = map(Path, sys.argv[1:])
paths = ['/usr/local/lib/piwatch', '/etc/systemd/system/piwatch.service',
         '/etc/systemd/system/piwatch.service.d', '/etc/systemd/system/piwatch-web.service',
         '/etc/systemd/system/piwatch-web.service.d', '/etc/piwatch/config.json',
         '/etc/sudoers.d/piwatch-poweroff']
for raw in paths:
    path = Path(raw)
    if path.exists():
        target = backup / path.relative_to('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            shutil.copytree(path, target, ignore=shutil.ignore_patterns('__pycache__'))
        else:
            shutil.copy2(path, target)
for raw in ['/var/lib/piwatch/outbox.sqlite3', '/var/lib/lora-notifyd/delivery.sqlite3',
            '/var/lib/piwatch-config/settings.sqlite3', '/var/lib/piwatch-web/auth.sqlite3']:
    path = Path(raw)
    if path.exists():
        target = backup / path.relative_to('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
key = Path('/var/lib/piwatch-web/session.key')
if key.exists():
    target = backup / key.relative_to('/')
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(key, target)
originals = source / 'backups/pre-panel-20260909'
if originals.exists():
    shutil.copytree(originals, backup / 'checkout-before-panel')
PY
getent group piwatch-config >/dev/null || groupadd --system piwatch-config
id piwatch-web >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/piwatch-web --shell /usr/sbin/nologin piwatch-web
usermod -a -G piwatch-config piwatch
usermod -a -G piwatch-config piwatch-web
install -d -o piwatch-web -g piwatch-config -m 2770 /var/lib/piwatch-config
install -d -o piwatch-web -g piwatch-web -m 0700 /var/lib/piwatch-web
if [[ ! -x /opt/piwatch-web/bin/python ]]; then
    python3 -m venv /opt/piwatch-web
fi
/opt/piwatch-web/bin/python -m pip install -r web/requirements.txt
# Stage code completely, then replace the watchdog package while its process is stopped.
PIWATCH_STAGE=$(mktemp -d /usr/local/lib/piwatch-stage.XXXXXX)
trap 'rm -rf -- "$PIWATCH_STAGE"' EXIT
python3 - "$PIWATCH_STAGE" <<'PY'
from pathlib import Path
import shutil
import sys
stage = Path(sys.argv[1])
shutil.copytree('watchdog/piwatch', stage / 'piwatch', ignore=shutil.ignore_patterns('__pycache__'))
shutil.copy2('watchdog/piwatchd.py', stage / 'piwatchd.py')
shutil.copytree('web', stage / 'web', ignore=shutil.ignore_patterns('__pycache__'))
PY
# Initialize only the new settings DB from the existing nonsecret JSON.
PYTHONPATH="$PIWATCH_SOURCE/watchdog" python3 - <<'PY'
from piwatch.config import load_config
from piwatch.config_store import SettingsStore
store = SettingsStore()
if not store.path.exists():
    store.initialize(load_config())
PY
chown piwatch-web:piwatch-config /var/lib/piwatch-config/settings.sqlite3
chmod 0660 /var/lib/piwatch-config/settings.sqlite3
# Bootstrap from staging before disrupting monitoring; getpass never echoes passwords.
if ! runuser -u piwatch-web -- /opt/piwatch-web/bin/python -c 'from pathlib import Path; import sqlite3, sys; p=Path("/var/lib/piwatch-web/auth.sqlite3"); sys.exit(0 if p.exists() and sqlite3.connect(p).execute("SELECT COUNT(*) FROM owner").fetchone()[0] else 1)'; then
    chmod 0755 "$PIWATCH_STAGE"
    runuser -u piwatch-web -- /opt/piwatch-web/bin/python "$PIWATCH_STAGE/web/piwatch-web.py" init
fi
systemctl stop piwatch
systemctl stop piwatch-web.service 2>/dev/null || true
install -d -m 0755 /usr/local/lib/piwatch
cp -a "$PIWATCH_STAGE/piwatch" "$PIWATCH_STAGE/piwatchd.py" "$PIWATCH_STAGE/web" /usr/local/lib/piwatch/
install -m 0644 deployment/piwatch-web.service /etc/systemd/system/piwatch-web.service
install -d -m 0755 /etc/systemd/system/piwatch.service.d
install -m 0644 deployment/piwatch-config.conf /etc/systemd/system/piwatch.service.d/config.conf
visudo -cf deployment/piwatch-poweroff.sudoers
install -o root -g root -m 0440 deployment/piwatch-poweroff.sudoers /etc/sudoers.d/piwatch-poweroff
systemd-analyze verify /etc/systemd/system/piwatch-web.service /etc/systemd/system/piwatch.service
systemctl daemon-reload
systemctl restart lora-notifyd
systemctl start piwatch
systemctl restart piwatch-oled
systemctl enable --now piwatch-web
systemctl is-active piwatch lora-notifyd piwatch-oled piwatch-web
printf 'Installed. Backup: %s\nPanel: http://127.0.0.1:8080 (use an SSH tunnel or a deliberate LAN/VPN binding).\n' "$PIWATCH_BACKUP"
