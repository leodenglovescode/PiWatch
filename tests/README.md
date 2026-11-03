# Reliability and panel validation

Syntax (does not start services):

```bash
python3 -m compileall -q oled lora watchdog web tests
bash -n deployment/install-local.sh
```

Monitoring/configuration/history/MQTT mapping tests use only the standard library
and a fake MQTT client:

```bash
python3 -m unittest discover -s tests -p test_monitoring.py -v
```

For web tests, install dependencies in a new temporary environment, not in either
existing hardware environment:

```bash
python3 -m venv /tmp/piwatch-web-tests
/tmp/piwatch-web-tests/bin/python -m pip install -r web/requirements.txt
/tmp/piwatch-web-tests/bin/python -m unittest discover -s tests -p test_web.py -v
```

`unittest discover -s tests -v` runs all available tests. Panel tests explicitly skip
when Flask/PyOTP/QR dependencies are absent; do not count that as panel validation.
Bubblewrap can prevent asyncio cross-thread wakeups; run the mocked suites on the
host if they stall there.

Tests use temporary databases, synthetic passwords/tokens, mocked radio/network
functions and mocked email sends. They cover history transactions, revision conflicts, secret redaction,
config reload without restart, target reconciliation, external probe sampling,
password/CSRF/session protection, TOTP replay rejection, verified email challenges,
recovery-code single use and stale status detection. They do not open hardware,
read deployment secret files, or send notifications.

The production MQTT service uses the pinned Paho version in `mqtt/requirements.txt`.
The deployment scripts install it in `/opt/piwatch-mqtt`; tests do not modify that
environment or connect to a broker.

The power-supervisor regressions use fake GPIO requests, a synthetic monotonic
clock, and a mocked `systemctl` subprocess. They never pulse a real GPIO or request
a real shutdown. They also cover the temporary-threshold dry run, ARM/DISARM test,
one-shot daemon request files, the guarded end-to-end request path, cross-process
input coordination, and OLED power-event/countdown presentation.

After an authorized deployment, hardware validation still needs an intended test
window/destination. Follow `deployment/WEB_PANEL.md` and `deployment/README.md`.

Native ntfy tests use synthetic credentials and mocked transport. Run them on the
host if sandbox asyncio/thread wakeups stall. `test_ntfy.py` covers total transport
deadlines, TLS trust, redirect rejection, tracker policy, long-outage persistence,
and independent heartbeat cadence; `test_ntfy_web.py` covers secret preservation,
maintenance acknowledgement and labelled test queuing. Cutover and verifier tests
use temporary databases/process fixtures and never contact the VPS or hardware.
