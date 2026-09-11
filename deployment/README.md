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

Each notification channel runs in incident order. Ordinary IP notifications use
Home Assistant with SMTP fallback. Mains outage/restoration notifications instead
use independent `ha`, `smtp`, and `lora` jobs so every enabled path is attempted.
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
