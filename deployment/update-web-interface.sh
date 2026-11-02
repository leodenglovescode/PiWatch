#!/usr/bin/env bash
# Deploy templates and browser assets; restart only the web panel.
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
    echo 'Run with sudo from an interactive terminal.' >&2
    exit 1
fi
PIWATCH_SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PIWATCH_WEB=/usr/local/lib/piwatch/web
PIWATCH_BACKUP="/var/backups/piwatch/$(date +%Y%m%d-%H%M%S)-web-interface-$$"
assets=(static/style.css static/common.js static/theme.js static/dashboard.js static/settings.js static/account.js static/login.js)
templates=(templates/base.html templates/dashboard.html templates/settings.html templates/account.html templates/login.html)
files=("${assets[@]}" "${templates[@]}")
for file in "${files[@]}"; do
    [[ -f "$PIWATCH_SOURCE/web/$file" && -f "$PIWATCH_WEB/$file" ]] || {
        echo "Missing interface file: $file" >&2
        exit 1
    }
done
systemctl is-active --quiet piwatch-web
install -d -o root -g root -m 0700 "$PIWATCH_BACKUP/static" "$PIWATCH_BACKUP/templates"
for file in "${files[@]}"; do cp -a -- "$PIWATCH_WEB/$file" "$PIWATCH_BACKUP/$file"; done
PIWATCH_STAGE=$(mktemp -d "$PIWATCH_WEB/.interface-stage.XXXXXX")
APPLIED=0
cleanup() {
    result=$?
    set +e
    if (( result != 0 && APPLIED == 1 )); then
        echo 'Interface verification failed; restoring the previous files.' >&2
        systemctl stop piwatch-web
        for file in "${files[@]}"; do
            cp -a -- "$PIWATCH_BACKUP/$file" "$PIWATCH_STAGE/$file"
            mv -f -- "$PIWATCH_STAGE/$file" "$PIWATCH_WEB/$file"
        done
        systemctl start piwatch-web
    fi
    rm -rf -- "$PIWATCH_STAGE"
    exit "$result"
}
trap cleanup EXIT
install -d -m 0700 "$PIWATCH_STAGE/static" "$PIWATCH_STAGE/templates"
for file in "${files[@]}"; do
    install -o root -g root -m 0644 "$PIWATCH_SOURCE/web/$file" "$PIWATCH_STAGE/$file"
done
/opt/piwatch-web/bin/python - "$PIWATCH_STAGE/templates" <<'PY'
import sys
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader(sys.argv[1]))
for path in Path(sys.argv[1]).glob('*.html'):
    env.get_template(path.name)
print('Staged templates compile.')
PY
APPLIED=1
systemctl stop piwatch-web
for file in "${files[@]}"; do mv -f -- "$PIWATCH_STAGE/$file" "$PIWATCH_WEB/$file"; done
systemctl start piwatch-web
ready=0
for _ in {1..20}; do
    if curl --fail --silent --noproxy '*' --connect-timeout 1 --max-time 2 \
        http://127.0.0.1:8080/login -o "$PIWATCH_STAGE/login.html"; then ready=1; break; fi
    sleep .5
done
[[ $ready -eq 1 ]] || { echo 'Panel did not become ready.' >&2; exit 1; }
for file in "${assets[@]}"; do
    curl --fail --silent --show-error --noproxy '*' --connect-timeout 2 --max-time 5 \
        "http://127.0.0.1:8080/$file" -o "$PIWATCH_STAGE/served-$(basename "$file")"
    cmp -- "$PIWATCH_SOURCE/web/$file" "$PIWATCH_STAGE/served-$(basename "$file")"
done
for file in "${templates[@]}"; do cmp -- "$PIWATCH_SOURCE/web/$file" "$PIWATCH_WEB/$file"; done
systemctl is-active --quiet piwatch-web
APPLIED=0
printf 'Web interface updated. Only piwatch-web was restarted.\nBackup: %s\n' "$PIWATCH_BACKUP"
