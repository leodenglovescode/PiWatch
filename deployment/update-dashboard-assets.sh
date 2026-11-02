#!/usr/bin/env bash
# Update the overview's static files without restarting the web service.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo 'Run with sudo from an interactive terminal.' >&2
    exit 1
fi

PIWATCH_SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PIWATCH_STATIC=/usr/local/lib/piwatch/web/static
PIWATCH_BACKUP="/var/backups/piwatch/$(date +%Y%m%d-%H%M%S)-dashboard-assets-$$"

for asset in style.css dashboard.js; do
    [[ -f "$PIWATCH_SOURCE/web/static/$asset" && -f "$PIWATCH_STATIC/$asset" ]] || {
        echo "Missing dashboard asset: $asset" >&2
        exit 1
    }
done
systemctl is-active --quiet piwatch-web
install -d -o root -g root -m 0700 "$PIWATCH_BACKUP"
for asset in style.css dashboard.js; do
    cp -a -- "$PIWATCH_STATIC/$asset" "$PIWATCH_BACKUP/$asset"
done

PIWATCH_STAGE=$(mktemp -d "$PIWATCH_STATIC/.dashboard-assets.XXXXXX")
APPLIED=0
cleanup() {
    result=$?
    set +e
    if (( result != 0 && APPLIED == 1 )); then
        echo 'Asset verification failed; restoring the previous dashboard.' >&2
        for asset in style.css dashboard.js; do
            cp -a -- "$PIWATCH_BACKUP/$asset" "$PIWATCH_STAGE/$asset"
            mv -f -- "$PIWATCH_STAGE/$asset" "$PIWATCH_STATIC/$asset"
        done
    fi
    rm -rf -- "$PIWATCH_STAGE"
    exit "$result"
}
trap cleanup EXIT

for asset in style.css dashboard.js; do
    install -o root -g root -m 0644 "$PIWATCH_SOURCE/web/static/$asset" "$PIWATCH_STAGE/$asset"
done
APPLIED=1
for asset in style.css dashboard.js; do
    mv -f -- "$PIWATCH_STAGE/$asset" "$PIWATCH_STATIC/$asset"
done
for asset in style.css dashboard.js; do
    curl --fail --silent --show-error --noproxy '*' --connect-timeout 3 --max-time 10 \
        "http://127.0.0.1:8080/static/$asset" -o "$PIWATCH_STAGE/served-$asset"
    cmp -- "$PIWATCH_SOURCE/web/static/$asset" "$PIWATCH_STAGE/served-$asset"
done
systemctl is-active --quiet piwatch-web
APPLIED=0
printf 'Dashboard assets updated. No service restart needed.\nBackup: %s\n' "$PIWATCH_BACKUP"
