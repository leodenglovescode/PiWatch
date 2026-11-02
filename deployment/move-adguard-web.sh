#!/usr/bin/env bash
# Move only AdGuard Home's admin listener; preserve DNS and all other settings.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo 'Run with sudo from an interactive terminal.' >&2
    exit 1
fi

# Source only a trusted root-owned installation environment file.
source "${PIWATCH_SITE_ENV:-/etc/piwatch/site.env}"
: "${ADGUARD_HOST:?Set ADGUARD_HOST in the site environment}"
ADGUARD_PORT=${1:-3000}
if [[ ! $ADGUARD_PORT =~ ^[0-9]+$ ]] || (( ADGUARD_PORT < 1024 || ADGUARD_PORT > 65535 )); then
    echo 'Choose an unprivileged TCP port from 1024 through 65535.' >&2
    exit 1
fi
case "$ADGUARD_PORT" in
    3000) ;;
    8080) echo 'Port 8080 is reserved for the loopback PiWatch backend.' >&2; exit 1 ;;
esac

ADGUARD_CONFIG=/opt/AdGuardHome/AdGuardHome.yaml
ADGUARD_BINARY=/opt/AdGuardHome/AdGuardHome
ADGUARD_BACKUP="/var/backups/adguardhome/$(date +%Y%m%d-%H%M%S)-web-port"
ADGUARD_STAGE=$(mktemp /opt/AdGuardHome/.AdGuardHome.yaml.web-port.XXXXXX)
APPLIED=0

cleanup() {
    status=$?
    set +e
    if [[ $status -ne 0 && $APPLIED -eq 1 ]]; then
        echo 'AdGuard Home verification failed; restoring its previous configuration.' >&2
        install -o root -g root -m 0600 "$ADGUARD_BACKUP/AdGuardHome.yaml" "$ADGUARD_CONFIG"
        systemctl restart AdGuardHome
    fi
    rm -f -- "$ADGUARD_STAGE"
    exit "$status"
}
trap cleanup EXIT

if [[ -n $(ss -H -ltn "sport = :$ADGUARD_PORT") ]]; then
    echo "TCP port $ADGUARD_PORT is already in use." >&2
    exit 1
fi

install -d -o root -g root -m 0700 "$ADGUARD_BACKUP"
install -o root -g root -m 0600 "$ADGUARD_CONFIG" "$ADGUARD_BACKUP/AdGuardHome.yaml"

python3 - "$ADGUARD_CONFIG" "$ADGUARD_STAGE" "$ADGUARD_PORT" <<'PY'
from pathlib import Path
import re
import sys

source, target = map(Path, sys.argv[1:3])
port = int(sys.argv[3])
lines = source.read_text().splitlines(keepends=True)
in_http = False
changed = False
for index, line in enumerate(lines):
    stripped = line.strip()
    if line and not line[0].isspace():
        in_http = stripped == 'http:'
        continue
    if in_http and re.fullmatch(r'  address:.*(?:\n)?', line):
        address = line.split(':', 1)[1].strip()
        host, separator, old_port = address.rpartition(':')
        if not separator or not host or not old_port.isdigit():
            raise SystemExit('Unsupported AdGuard Home HTTP address format')
        lines[index] = f'  address: {host}:{port}\n'
        changed = True
        break
if not changed:
    raise SystemExit('AdGuard Home HTTP address was not found')
target.write_text(''.join(lines))
PY
chown root:root "$ADGUARD_STAGE"
chmod 0600 "$ADGUARD_STAGE"

"$ADGUARD_BINARY" --check-config --config "$ADGUARD_STAGE"
systemctl stop AdGuardHome
if ! cmp -s "$ADGUARD_CONFIG" "$ADGUARD_BACKUP/AdGuardHome.yaml"; then
    echo 'AdGuard Home configuration changed during migration; refusing to overwrite it.' >&2
    systemctl start AdGuardHome
    exit 1
fi
mv "$ADGUARD_STAGE" "$ADGUARD_CONFIG"
APPLIED=1
systemctl start AdGuardHome

for _ in {1..30}; do
    if curl --fail --silent --output /dev/null --noproxy '*' \
        --connect-timeout 2 --max-time 3 "http://${ADGUARD_HOST}:$ADGUARD_PORT/" && \
            ss -H -ltn "sport = :53" | grep -Fq -- "${ADGUARD_HOST}:53"; then
        APPLIED=0
        printf 'AdGuard Home UI moved to http://%s:%s\nBackup: %s\n' \
            "$ADGUARD_HOST" "$ADGUARD_PORT" "$ADGUARD_BACKUP"
        exit 0
    fi
    sleep 1
done
echo 'AdGuard Home did not restore both its web and DNS listeners within 30 seconds.' >&2
exit 1
