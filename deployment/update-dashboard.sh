#!/usr/bin/env bash
# Update the installed watchdog package and web panel without touching hardware services.
set -euo pipefail

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
        rm -rf /usr/local/lib/piwatch/piwatch /usr/local/lib/piwatch/web
        cp -a "$PIWATCH_BACKUP/usr/local/lib/piwatch/piwatch" /usr/local/lib/piwatch/piwatch
        cp -a "$PIWATCH_BACKUP/usr/local/lib/piwatch/web" /usr/local/lib/piwatch/web
        systemctl start piwatch
        systemctl start piwatch-web
    fi
    rm -rf -- "$PIWATCH_STAGE"
    exit "$status"
}
trap cleanup EXIT

cd "$PIWATCH_SOURCE"
python3 - <<'PY'
import ast
from pathlib import Path

for folder in ('watchdog', 'web'):
    for path in Path(folder).rglob('*.py'):
        ast.parse(path.read_text(), filename=str(path))
PY

install -d -m 0700 "$PIWATCH_BACKUP/usr/local/lib/piwatch"
cp -a /usr/local/lib/piwatch/piwatch "$PIWATCH_BACKUP/usr/local/lib/piwatch/piwatch"
cp -a /usr/local/lib/piwatch/web "$PIWATCH_BACKUP/usr/local/lib/piwatch/web"

python3 - "$PIWATCH_STAGE" <<'PY'
from pathlib import Path
import shutil
import sys

stage = Path(sys.argv[1])
shutil.copytree('watchdog/piwatch', stage / 'piwatch',
                ignore=shutil.ignore_patterns('__pycache__'))
shutil.copytree('web', stage / 'web', ignore=shutil.ignore_patterns('__pycache__'))
PY

SERVICES_STOPPED=1
systemctl stop piwatch
systemctl stop piwatch-web
rm -rf /usr/local/lib/piwatch/piwatch /usr/local/lib/piwatch/web
cp -a "$PIWATCH_STAGE/piwatch" /usr/local/lib/piwatch/piwatch
cp -a "$PIWATCH_STAGE/web" /usr/local/lib/piwatch/web

python3 - <<'PY'
import ast
from pathlib import Path

for folder in ('/usr/local/lib/piwatch/piwatch', '/usr/local/lib/piwatch/web'):
    for path in Path(folder).rglob('*.py'):
        ast.parse(path.read_text(), filename=str(path))
PY

systemctl start piwatch
systemctl start piwatch-web
systemctl is-active --quiet piwatch
systemctl is-active --quiet piwatch-web
curl --fail --silent --show-error http://127.0.0.1:8080/login >/dev/null

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
        if time.time() - status['generated_at'] < 4 and 'cpu_percent' in metrics:
            gps = status.get('self_health', {}).get('gps', {})
            print('GPS: mode={mode}, seen={seen}, used={used}'.format(
                mode=gps.get('mode', '?'), seen=gps.get('seen', '?'), used=gps.get('used', '?')))
            break
    except (OSError, ValueError, KeyError):
        pass
    time.sleep(.5)
else:
    raise SystemExit('Fresh status metrics did not appear within 12 seconds')
PY

SERVICES_STOPPED=0
printf 'Updated. Backup: %s\nPanel remains available on its configured bind, port 8080.\n' "$PIWATCH_BACKUP"
