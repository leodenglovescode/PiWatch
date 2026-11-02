#!/usr/bin/env bash
# Put Caddy in front of the loopback-only PiWatch panel using Caddy's local CA.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo 'Run with sudo from an interactive terminal.' >&2
    exit 1
fi

PIWATCH_SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# Source only a trusted root-owned installation environment file.
PIWATCH_SITE_ENV=${PIWATCH_SITE_ENV:-/etc/piwatch/site.env}
set -a
source "$PIWATCH_SITE_ENV"
set +a
: "${PIWATCH_HTTPS_HOST:?Set PIWATCH_HTTPS_HOST in the site environment}"
: "${PIWATCH_HTTPS_ADDRESS:?Set PIWATCH_HTTPS_ADDRESS in the site environment}"
python3 - <<'CHECK'
import ipaddress, os, re
if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', os.environ['PIWATCH_HTTPS_HOST']):
    raise SystemExit('Invalid HTTPS hostname')
ipaddress.IPv4Address(os.environ['PIWATCH_HTTPS_ADDRESS'])
CHECK

PIWATCH_BACKUP="/var/backups/piwatch/$(date +%Y%m%d-%H%M%S)-caddy"
install -d -m 0700 "$PIWATCH_BACKUP/etc/caddy"
install -d -m 0700 "$PIWATCH_BACKUP/etc/systemd/system/piwatch-web.service.d"

if [[ -f /etc/caddy/Caddyfile ]]; then
    cp -a /etc/caddy/Caddyfile "$PIWATCH_BACKUP/etc/caddy/Caddyfile"
fi
if [[ -f /etc/systemd/system/piwatch-web.service.d/20-https.conf ]]; then
    cp -a /etc/systemd/system/piwatch-web.service.d/20-https.conf \
        "$PIWATCH_BACKUP/etc/systemd/system/piwatch-web.service.d/20-https.conf"
fi

if ! command -v caddy >/dev/null 2>&1; then
    apt-get update
    apt-get install -y caddy
fi

install -d -o root -g root -m 0755 /etc/caddy
install -o root -g root -m 0644 "$PIWATCH_SOURCE/deployment/Caddyfile" /etc/caddy/Caddyfile
python3 - <<'RENDER'
from pathlib import Path
import os
path = Path('/etc/caddy/Caddyfile')
text = path.read_text()
for key in ('PIWATCH_HTTPS_HOST', 'PIWATCH_HTTPS_ADDRESS'):
    text = text.replace('__' + key + '__', os.environ[key])
path.write_text(text)
RENDER

install -d -o root -g root -m 0755 /etc/systemd/system/piwatch-web.service.d
install -o root -g root -m 0644 "$PIWATCH_SOURCE/deployment/piwatch-web-https.conf" \
    /etc/systemd/system/piwatch-web.service.d/20-https.conf

caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl daemon-reload
systemctl restart piwatch-web
systemctl enable caddy
systemctl restart caddy

for _ in {1..30}; do
    if curl --fail --silent --insecure --noproxy '*' --connect-timeout 3 --max-time 5 \
        --resolve "${PIWATCH_HTTPS_HOST}:443:127.0.0.1" \
        "https://${PIWATCH_HTTPS_HOST}/login" >/dev/null; then
        break
    fi
    sleep .5
done
curl --fail --silent --insecure --noproxy '*' --connect-timeout 3 --max-time 5 \
    --resolve "${PIWATCH_HTTPS_HOST}:443:127.0.0.1" \
    "https://${PIWATCH_HTTPS_HOST}/login" >/dev/null

CADDY_ROOT=/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt
if [[ -f "$CADDY_ROOT" ]]; then
    install -o root -g root -m 0644 "$CADDY_ROOT" \
        /usr/local/share/ca-certificates/piwatch-caddy.crt
    update-ca-certificates
fi

systemctl is-active --quiet caddy
systemctl is-active --quiet piwatch-web
printf 'HTTPS enabled: https://%s or https://%s\nBackup: %s\n' \
    "$PIWATCH_HTTPS_HOST" "$PIWATCH_HTTPS_ADDRESS" "$PIWATCH_BACKUP"
