#!/usr/bin/env bash
# Existing native installations only; called after the core update is healthy.
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then echo 'Run with sudo.' >&2; exit 1; fi
PIWATCH_SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ ! -f /etc/systemd/system/piwatch-heartbeat.service ]]; then exit 0; fi
PIWATCH_BACKUP="/var/backups/piwatch/$(date +%Y%m%d-%H%M%S)-heartbeat-update-$$"
install -d -m 0700 "$PIWATCH_BACKUP"
cp -a /etc/systemd/system/piwatch-heartbeat.service "$PIWATCH_BACKUP/piwatch-heartbeat.service"
cp -a /usr/local/lib/piwatch/piwatch-heartbeat.py "$PIWATCH_BACKUP/piwatch-heartbeat.py"
restore() {
    result=$?
    if (( result != 0 )); then
        cp -a "$PIWATCH_BACKUP/piwatch-heartbeat.service" /etc/systemd/system/piwatch-heartbeat.service
        cp -a "$PIWATCH_BACKUP/piwatch-heartbeat.py" /usr/local/lib/piwatch/piwatch-heartbeat.py
        systemctl daemon-reload
        systemctl try-restart piwatch-heartbeat || true
    fi
    exit "$result"
}
trap restore EXIT
install -o root -g root -m 0644 "$PIWATCH_SOURCE/watchdog/piwatch-heartbeat.py" /usr/local/lib/piwatch/piwatch-heartbeat.py
install -o root -g root -m 0644 "$PIWATCH_SOURCE/deployment/piwatch-heartbeat.service" /etc/systemd/system/piwatch-heartbeat.service
systemd-analyze verify /etc/systemd/system/piwatch-heartbeat.service
systemctl daemon-reload
systemctl try-restart piwatch-heartbeat
