# Reliability deployment

For the modular monitor and authenticated panel release, use [WEB_PANEL.md](WEB_PANEL.md)
and `install-local.sh`. The package must be installed with its entry point; the
single-file copy procedure below describes the earlier reliability deployment.

`watchdog/piwatchd.py` is the source for `/usr/local/lib/piwatch/piwatchd.py`.
The OLED and LoRa units run scripts directly from this checkout. Unit files here
reference existing environment files; do not copy or read their secret values.

Before deployment inspect current units/configuration and back up the daemon,
LoRa script, and affected units. Validate Python syntax and run the mocked tests
in `tests/README.md`. Install the watchdog and the two unit files, run
`systemctl daemon-reload`, stop `piwatch`, restart `lora-notifyd`, then start
`piwatch`. This ordering lets the watchdog checkpoint current work while LoRa is
still available. Do not restart OLED, gpsd, or chrony for this change.

Systemd creates private persistent directories:

- `/var/lib/piwatch/outbox.sqlite3`: tracker states and frozen notification jobs.
- `/var/lib/lora-notifyd/delivery.sqlite3`: request IDs, payload hashes, and cached
  successful results. Preserve this database when restarting or rolling back.

`deployment/piwatch-poweroff.sudoers` grants the unprivileged `piwatch` account
only the exact noninteractive `/usr/bin/systemctl --no-block poweroff` command.
The install/update scripts validate it with `visudo`, install it as root-owned
mode `0440`, and include the previous rule in deployment backup and rollback.

Each notification channel runs in incident order within its priority. Ordinary IP notifications use
Home Assistant with SMTP fallback. Mains outage/restoration notifications instead
use independent `ha`, `smtp`, and `lora` jobs so every enabled path is attempted.
The final low-battery shutdown event uses the same independent fan-out and is
placed ahead of ordinary queued work in each channel before the Pico ARM pulse.
LoRa primary/context messages
have separate durable checkpoints and stable request IDs. A channel/stage gets
five attempts with 5, 10, 20, and 40 seconds between failures. Failed jobs remain
stored; they do not prevent subsequent jobs from running. `/run/piwatch/status.json`
includes per-channel counts for pending, done, and failed jobs. Configuration
thresholds are reloaded on restart; states/outage timestamps persist, while
consecutive probe counters restart.

The API rejects concurrent sends immediately with HTTP 503 (`busy`), and rejects
unavailable radios without connecting inside the request. The background radio
maintainer reconnects. ACK waits use a monotonic deadline starting at send time;
the watchdog retains its 65-second HTTP timeout for the deployed 60-second ACK
budget. Keep the HTTP timeout above the configured ACK budget when changing it.
USB/SDK transmission itself is synchronous and cannot be forcibly cancelled;
client timeout does not prove that no transmission occurred.

Successful request IDs are cached across service restarts for seven days. Reusing
an ID with a different payload during that window returns 409. Retry of an
unconfirmed send can duplicate a
message when delivery succeeded but its ACK was lost, or after a crash between
delivery and cache commit. HA/SMTP have the same ambiguous-delivery limitation;
this is not exactly-once delivery.

## UPS supervisor CLI tests

These commands are available after deployment through the installed entry point.
They do not alter the fixed production threshold or leave a persistent test mode.

Exercise the real GPIO17 and INA219 inputs with a temporary dry-run threshold:

```bash
sudo /usr/bin/python3 /usr/local/lib/piwatch/piwatchd.py ups-test dry-run \
  --threshold 4.20 --seconds 30
```

The dry run prints one sample per second and reports when shutdown would trigger.
It never requests GPIO2 and never powers off. Choose a threshold at or just above
the current battery voltage, then unplug mains to exercise the full countdown. For
a battery reading around 4.18 V, use `--threshold 4.20`; a 4.00 V threshold remains
idle because the measured voltage is still above it. A shared lock serializes its
INA219 and GPIO17 reads with the running watchdog.

With mains present, test the Pico command pulse and buzzer without shutting down:

```bash
sudo /usr/bin/python3 /usr/local/lib/piwatch/piwatchd.py ups-test pico \
  --confirm PULSE-GPIO2
```

This sends ARM, waits two seconds, and sends DISARM in a `finally` block so an
interruption during the wait still releases and disarms the Pico.

For the real end-to-end test, unplug mains first and keep the running watchdog
service active. The daemon rejects the request unless GPIO17 is LOW and the
INA219 sample is valid:

```bash
sudo /usr/bin/python3 /usr/local/lib/piwatch/piwatchd.py ups-test shutdown \
  --confirm POWER-OFF
```

The running daemon records a clearly labeled test incident, starts every enabled
notification path, presents a ten-second OLED test countdown, puts the SSD1306 into its
low-power display-off mode, sends the Pico ARM pulse, and requests clean poweroff.
Restore mains after the Pi halts to test the Pico's GPIO3 wake pulse and retries.

The OLED also temporarily replaces its rotating pages with `MAINS POWER` and
`DISCONNECTED` or `RESTORED` for three seconds after each debounced GPIO17 event.
During automatic low-battery qualification, its final ten seconds replace the
rotating pages with the shutdown countdown; this is part of the existing 30-second
period and adds no delay. On service termination it sends the SSD1306 display-off
command before exiting.

Every three days, each service prunes and compacts its own delivery database. The
watchdog removes completed and failed jobs older than three days while preserving
pending jobs, tracker state, and the separately bounded 90-day incident history.
The LoRa gateway removes incomplete cache entries after three days and confirmed
request IDs after seven days. Both delivery tables also have hard row caps.

Inspect aggregate queue status with a read-only SQLite query, e.g. as the service
user: `SELECT channel, status, COUNT(*) FROM jobs GROUP BY channel, status;`.
For an operator-approved retry of a failed job, stop `piwatch`, back up its database,
then update only the intended job: set `status='pending'`, `attempts=0`, and
`next_attempt=0`, keeping its ID, payload, and stage unchanged. Start `piwatch` and
check status again. Failed jobs require operator follow-up during their three-day
retention window.

After deployment check unit state, status-file freshness/notification counts,
and `GET /health` (including `radio_connected`). Hardware ACK success, timeout,
and disconnect tests require an agreed destination/window. Do not simulate
production outages or send test notifications without that scope.

For rollback, stop the watchdog first, restore backed-up code and units, reload
systemd, restart LoRa, then start the watchdog. Preserve databases; older code
will not process the outbox or restore saved tracker states.
It also will not process newer direct `ha` or `smtp` mains-event jobs; review those
jobs before leaving an older watchdog version running.
