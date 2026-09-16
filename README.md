# PiWatch

Local monitoring, durable outage notifications, an OLED display, and a small web
panel. Source and backups stay on the Pi; this repository is never published.

## Code map

| Area | Entry point | Supporting code |
|---|---|---|
| Monitoring | `watchdog/piwatchd.py` | `watchdog/piwatch/`: checks, battery sensor, state, configuration, health, status |
| HA metrics | `watchdog/piwatch-ha-mqtt.py` | `watchdog/piwatch/mqtt_bridge.py`: MQTT Discovery and retained sensor state |
| Notifications | Part of the monitoring service | `watchdog/piwatch/notifications/`: message formatting, outbox, HA, SMTP, LoRa client |
| Radio gateway | `lora/lora-notifyd.py` | `lora/mesh_gateway/`: radio/ACK handling, delivery cache, HTTP API |
| OLED | `oled/piwatch-oled.py` | Metrics, gpsd reader, watchdog page |
| Web panel | `web/piwatch-web.py` | `web/piwatch_web/`, templates, static CSS/JavaScript |

The existing executable names are retained to match systemd. Their roles are
now stated in the entry-point docstrings. Importing the OLED or radio modules
does not initialize devices or start threads.

## Panel

- Overview: configured services and probes, snapshot freshness, Pi health,
  gpsd/chrony/radio state, INA219 UPS voltage, live CPU/memory/disk/temperature/network metrics,
  delivery counts and outstanding jobs, and recent incidents.
- Settings: add/edit/remove/disable monitored services; ping, TCP, and HTTP checks;
  Internet endpoints; DNS names; UPS sensor and voltage thresholds; intervals/thresholds;
  Home Assistant MQTT metrics; LoRa, SMTP, and HA notification gateways.
- Account: username/password, session timeout, authenticator-app and email 2FA,
  single-use recovery codes. Email setup is disabled until SMTP is explicitly
  configured in the panel and requires verification of the recipient.

A saved settings revision is applied by the monitor at its next control cycle
(usually 1–4 seconds, allowing in-flight probes to finish). The panel distinguishes
saved settings from the revision the monitor has actually applied. Existing jobs
retain their gateway overrides, message text, and delivery checkpoints.

## Storage

- `/var/lib/piwatch-config/settings.sqlite3`: current validated settings and a
  bounded revision audit (no old setting values). Owned by `piwatch-web`, readable
  by the monitor and MQTT publisher through the `piwatch-config` group. Explicit
  gateway and MQTT credentials are stored here with restricted filesystem
  permissions and never returned by
  the settings API. Blank credential inputs preserve the current value; the
  explicit clear checkbox stores an empty credential.
- `/var/lib/piwatch-web/auth.sqlite3`: password hashes, revocable sessions,
  second-factor secrets, hashed recovery codes, expiring challenges, attempt limits.
  Private to the web service. `session.key` signs its cookies.
- `/var/lib/piwatch/outbox.sqlite3`: existing delivery queue and tracker states,
  plus transactional incident history. New explicit gateway overrides are frozen
  in private job payloads. Deployment environment secrets are never imported into
  either database by the migration.
- `/var/lib/lora-notifyd/delivery.sqlite3`: existing confirmed-delivery cache.
- `/run/piwatch/status.json`: replaceable public status snapshot with no gateway
  credentials or message bodies. Includes the newest 100 incidents and 50
  outstanding jobs so the panel needs no outbox access.
- `/run/piwatch-ha-mqtt/status.json`: nonsecret MQTT publisher state, connection
  status, last publish time, and source snapshot age.

History is capped at 10,000 events; records older than 90 days are pruned when new
incidents are recorded. Completed/failed delivery records are retained separately.
Historical outages cannot be reconstructed automatically from the old job table.

The monitor can still start from `/etc/piwatch/config.json` before the settings DB
is initialized. Once initialized, SQLite is authoritative; editing the old JSON
has no effect. Secret fields left unset continue to use existing service environment
values. The panel never reads either deployment secrets file. SMTP for email 2FA
must be explicitly configured because the web service cannot access those files.

The UPS monitor reads the INA219 bus-voltage register directly through `/dev/i2c-3`;
it does not invoke `sudo` or depend on a Python sensor package.
The default 1S thresholds are 3.30 V low and 3.50 V recovered. The normal transition
counters debounce the alarm, and the separate recovery threshold prevents chatter
from voltage sag. Mains presence comes only from the active-high GPIO17 input on
physical pin 11. A change must remain stable for three seconds before it is accepted;
the GPIO is sampled once per control cycle, independently of the configurable service
probe interval. INA219 voltage changes never alter the mains state.

The separate `piwatch-ha-mqtt` service reads only `/run/piwatch/status.json` and
publishes selected measurements through Home Assistant MQTT Discovery. It does not
open I²C or GPIO. Discovery, metric state, and freshness state are retained at QoS
1. Metrics publish once per second, only while the watchdog snapshot is fresh. Home Assistant uses
the configured expiry, 30 seconds by default, so a short service restart preserves
the last value without a brief unavailable state. `Data fresh` continues to report
snapshot freshness while the publisher itself is connected. Broker configuration
and selected entities reload from the settings database without restarting either
the monitor or web panel. Network receive and transmit rates are converted from the
system byte counters and published in `Mbit/s` (Mbps). The `Estimated battery` entity converts the measured 1S
voltage through a nonlinear 3.05–4.20 V curve and clamps the result to 0–100%; it
is a voltage-based estimate rather than a coulomb-counted state of charge.
The same estimate appears on the dashboard's UPS battery card, and dashboard network
rates use Mbps.

Battery events use the normal notification policy. Mains outage and restoration
events create independent durable Home Assistant, SMTP, and LoRa jobs for every
enabled gateway, so failure of one path does not suppress the others. The
`piwatch` service receives I²C and GPIO access through its systemd supplementary groups.

The overview uses the locally vendored Vue 3.5.42 production runtime and render
functions. It polls the authenticated status endpoint every second and keeps
a rolling 60-sample trend in memory while the page is open. Vue is served by the
Pi, so the panel remains available during an Internet or DNS outage. The theme
control cycles through system, dark, and light modes and stores only that local
preference in the browser. Vue's license is in `web/static/vendor/VUE-LICENSE.txt`.

## Checks and freshness

Internet uses TCP connections to literal IP endpoints, independently of name
resolution. The default endpoints are `1.1.1.1:443` and `223.5.5.5:53`; change them
for the local network if needed. DNS uses `getent ahostsv4`, which exercises the
system's NSS resolver, including hosts/cache configuration. Any successful
endpoint/name makes its respective check healthy; individual failures stay visible
in the status data. These checks establish reachability/resolution, not unrestricted
Internet access or authoritative DNS correctness.

External checks default to 15 seconds; ordinary service checks default to the
existing configured interval. Only new samples count toward state transitions.
Changing a host or probe resets its evidence, while label/threshold changes keep
its tracked state. IDs are stable and not editable in the panel; deleting a target
retains its history and queued notifications.

The watchdog writes a fresh snapshot every control cycle, even between slower
probes. Readers use the boot ID and monotonic time to detect stale snapshots. Health
sampling has its own freshness deadline. The web panel also marks cached values
stale if its connection stops. This adds visibility into a stalled monitor; it does
not add a hardware watchdog or a systemd heartbeat.

## Installation and validation

See [local panel deployment](deployment/WEB_PANEL.md) before installing. Live
services are not restarted by tests. The [reliability procedure](deployment/README.md)
still applies to delivery and rollback. Run the commands in [tests/README.md](tests/README.md).
After the initial installation, `sudo bash deployment/update-dashboard.sh` updates
only the watchdog and web application, makes a timestamped backup, and leaves the
OLED and LoRa services running.

Design references: [SQLite transactions/isolation](https://www.sqlite.org/isolation.html),
[Flask web security](https://flask.palletsprojects.com/en/stable/web-security/),
[OWASP MFA guidance](https://cheatsheetseries.owasp.org/cheatsheets/Multifactor_Authentication_Cheat_Sheet.html),
[PyOTP replay protection](https://github.com/pyauth/pyotp), and
[Waitress deployment](https://docs.pylonsproject.org/projects/waitress/en/latest/usage.html).
Hardware references: [TI INA219 data sheet](https://www.ti.com/lit/ds/symlink/ina219.pdf)
and [Panasonic NCR18650GA specifications](https://energy.panasonic.com/eu/business/products/lithium-ion/models/NCR18650GA).
