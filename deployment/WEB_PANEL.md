# Local panel deployment

This release changes the watchdog, radio and OLED source layout, adds live
configuration and incident history, and introduces a separate authenticated web
service. Nothing is uploaded. Do not install by copying only `piwatchd.py`: its
`piwatch/` package must accompany it.

## Before installing

1. Run syntax and mocked tests in `tests/README.md`. No radio or email tests are
   sent by that suite.
2. Inspect current unit definitions and the nonsecret monitoring configuration.
   Never read or copy `/etc/piwatch/secrets.env` or `/etc/lora-notifyd.env`.
3. Choose a short installation window: watchdog, radio and OLED processes restart.
   gpsd and chrony are not restarted. Existing incidents remain durable; a watchdog
   restart resets consecutive probe counters as before.
4. Keep both existing delivery databases. The installer takes consistent SQLite
   backups and copies installed source/units into a private timestamped directory
   under `/var/backups/piwatch`. The original checkout scripts from before this
   implementation are also retained locally in `backups/pre-panel-20260909/`.

## Install from an interactive terminal on the Pi

```bash
cd /path/to/piwatch
sudo bash deployment/install-local.sh
```

The installer:

- Parses Python before changing production files.
- Creates restricted `piwatch-web` and `piwatch-mqtt` service accounts and the
  shared `piwatch-config` group.
- Installs an independent web virtual environment at `/opt/piwatch-web`; existing
  OLED and Meshtastic environments are not modified.
- Installs the MQTT publisher dependency in its own `/opt/piwatch-mqtt` virtual
  environment; it does not alter the system Python environment.
- Initializes the settings DB from the existing nonsecret JSON exactly once.
  No deployment credentials are read or copied. Normal notification delivery
  continues to use inherited service settings until overridden in the panel.
- Prompts locally for the initial username/password, with password echo disabled.
  There is no default login and no unauthenticated registration route.
- Installs the watchdog package and web code under `/usr/local/lib/piwatch`.
  OLED and radio still run from this checkout using their existing executable paths.
- Adds a watchdog group-access drop-in plus the web and MQTT units, reloads systemd, and
  restarts only the affected application services in notification-safe order.
- Grants the watchdog service access to the I²C group so it can read the INA219 on
  bus 3 without `sudo`; the device bus, address, and thresholds remain live settings.

The listener defaults to loopback. From your computer:

```bash
ssh -N -L 8080:127.0.0.1:8080 <user>@<Pi-LAN-address>
```

Open `http://127.0.0.1:8080` in your computer's browser. The SSH tunnel protects the
transport and avoids adding a public listener. Use your newly created credentials.

For a deliberate direct LAN/VPN listener, set `PIWATCH_WEB_BIND` to that interface's
address in a `piwatch-web.service` drop-in and restart only the web service. An
example drop-in is:

```ini
[Service]
Environment=PIWATCH_WEB_BIND=192.0.2.99
Environment=PIWATCH_WEB_PORT=8080
```

Use the actual address of this Pi. Plain HTTP does not encrypt login credentials;
use the SSH tunnel, a VPN transport, or a local HTTPS reverse proxy. For HTTPS,
set `PIWATCH_WEB_SECURE_COOKIE=1`. Keep the backend on loopback when proxying. The
application ignores untrusted forwarded headers and this release is scoped to
LAN/VPN use, not direct Internet exposure.

### Local HTTPS with Caddy

The Caddy template serves the hostname and address configured in
`/etc/piwatch/site.env` with Caddy's internal CA, keeps the Python listener on
`127.0.0.1:8080`, and enables Secure cookies. Install or update it locally with:

```bash
# Configure /etc/piwatch/site.env first; see PRIVATE_CONFIGURATION.md.
sudo bash deployment/enable-caddy-https.sh
```

The script backs up the previous Caddy and PiWatch web drop-in configuration under
`/var/backups/piwatch`, installs Caddy from the OS package repository when needed,
validates the configuration, restarts only `piwatch-web` and Caddy, and verifies the
HTTPS login endpoint. Caddy's local CA is trusted on the Pi. Other computers and
phones must import Caddy's public root certificate from
`/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt` to remove the
browser warning. Never copy the adjacent private key. This configuration is for
the LAN/VPN only and does not expose the panel through an Internet router.

On this Pi, Caddy owns ports 80 and 443: HTTP redirects to the PiWatch HTTPS
endpoint. AdGuard Home keeps DNS on TCP/UDP 53 and serves only its admin UI at
`http://192.0.2.99:3000`. To repeat that guarded web-port migration after an
AdGuard reinstall, run:

```bash
sudo bash deployment/move-adguard-web.sh 3000
```

The migration backs up the private AdGuard configuration, changes only
`http.address`, validates the staged YAML, and restores the backup automatically if
the admin UI or DNS listener does not return after restart.

## Live configuration

Use Settings to configure service names/addresses, ping/TCP/HTTP probes, enable or
remove targets, edit intervals/thresholds, Internet endpoints, DNS names, INA219
battery monitoring, Home Assistant MQTT metrics, and gateway settings. Saving
requires an authenticated session and the normal CSRF token; it does not ask for
the password again. Saves are validated and revision-checked, so a stale browser
cannot overwrite newer edits.
The panel shows when the watchdog reports the saved revision as active.

New settings do not restart the watchdog. Host/probe edits reset that target's
failure evidence; other targets retain their state. Gateway edits affect new
incidents. Frozen queued jobs keep their previous explicit gateway overrides, so
editing a gateway cannot redirect an existing alert midway through delivery. An
unset field uses the watchdog's inherited environment; explicitly clearing a
credential sets it to empty. Disable a gateway with its Enabled checkbox.

The Home Assistant metrics section controls an independent outbound MQTT publisher.
Its default broker is `localhost:1883`; configure a dedicated broker username
and password before enabling it when anonymous access is disabled. The password is
write-only in the panel. TLS uses the Pi's system trust store. MQTT topic prefixes
cannot contain wildcards. The expiry must be at least three publish intervals.
The default publish interval is one second, matching the watchdog's metric sampling.

The publisher creates one Home Assistant device with individually selectable CPU,
memory, disk, network, uptime, battery, mains, UPS, monitor, freshness, and
notification entities. It reads the atomic public PiWatch snapshot and never opens
the I²C bus or GPIO. Discovery and state messages use QoS 1 and retain. Metric
updates stop when the source snapshot is stale, and Home Assistant changes them to
unavailable only after the configured expiry. This preserves values through short
restarts without hiding a sustained failure. Settings revisions reload live in the
publisher; no service restart is needed after the first installation.
Network receive and transmit rates are published in `Mbit/s` (Mbps).
`Estimated battery` is a clamped 0–100% estimate from the 1S INA219 voltage; charging
and load sag can move it independently of the pack's true remaining capacity.

The UPS section controls INA219 battery-voltage monitoring and GPIO mains monitoring. Mains
outage/restoration differs from ordinary service alerts: each event is queued
independently for every enabled Home Assistant, SMTP, and LoRa gateway. GPIO17 on
physical pin 11 is authoritative: HIGH means the USB-C / 5 V input is
present and LOW means it is absent. A configurable two-to-three-second debounce
filters short changes. Alerts include the latest INA219 battery voltage when it is
available; INA219 readings do not determine mains state.

The Operational checks panel also shows the Pico UPS supervisor. Automatic
shutdown has a fixed safety rule: raw GPIO17 must remain LOW while a fresh INA219
reading remains at or below 3.05 V for 30 continuous seconds, sampled about once
per second. GPIO17 HIGH, an invalid INA219 sample, or any reading above 3.05 V
immediately resets the entire timer. After qualification, PiWatch latches the
action, queues a final alert independently for every enabled gateway, pulses GPIO2
LOW for one second to arm the Pico, releases GPIO2 to input, and requests a clean
poweroff. At service startup it sends one 150 ms GPIO2 LOW pulse to disarm the
Pico. PiWatch never requests GPIO3, which remains the Pico-controlled wake input.

The original `config.json` is only the initial import/fallback. Once the SQLite
settings DB exists, changes to that JSON do not alter live monitoring. Radio USB
selection, Meshtastic destination/ACK timeout, web listener address, and process
sandbox settings remain service-level configuration requiring the affected service
to restart. Secrets files must remain outside this repository.

## Clearing test records

The Stored records section in Settings can clear completed/failed delivery records
or incident history. Overview stays focused on current status and does not show
these histories. Each action requires an authenticated session but no step-up
password or second-factor prompt. The web service
places a validated request in the shared settings database; the watchdog performs
the deletion because the panel cannot access its private outbox database. Only the
`piwatch-web` and `piwatch` service accounts share write access to this control data
through the `piwatch-config` group.

Clearing delivery records removes completed and failed jobs, which resets retained
delivery errors on the dashboard. Pending jobs are preserved. Clearing incident
history does not reset live target states, outage timers, learned UPS direction, or
pending notifications. Both actions are intended for removing test data without
weakening current monitoring.

## Account and recovery

Account allows changing username/password and request-idle session timeout
(5–120 minutes, default 30; absolute session lifetime is 12 hours). Account or MFA
changes invalidate other sessions. The panel polls while open, so its requests
count as activity; closing it lets the idle timeout elapse. Logout revokes the
server-side session even if a cookie was copied.

Authenticator setup displays a locally generated QR and manual key, then requires
a valid code. Email setup stays unavailable until complete SMTP settings are stored
in the panel; the web process cannot inspect legacy SMTP environment secrets. Email
setup verifies the configured recipient before enabling login codes. Both methods
can coexist. Email login uses the verified recipient, even if the notification
recipient is later changed. Disable/re-enable email verification to change that
verified address. Switching on a factor produces a fresh set of eight one-use
recovery codes; save them outside the Pi. They replace the previous set.

TOTP codes cannot be reused. Email codes expire after five minutes, allow five
attempts, and can be requested at most once per minute per source address. Login
attempts are rate-limited in SQLite across web restarts. A signed-in owner has full
panel privileges; settings, maintenance, account, and MFA changes do not ask for
the password again. An SMTP outage does not bypass MFA at login: use the
authenticator or a recovery code.

If all second factors are lost, recover through an interactive terminal on the Pi:

```bash
sudo -u piwatch-web /opt/piwatch-web/bin/python /usr/local/lib/piwatch/web/piwatch-web.py reset-account
```

This requires operating-system access as the service user/root. It changes the
account password and revokes all sessions, challenges, and second factors. Set up
MFA again after signing in. Passwords are hashed with scrypt; TOTP secrets and new
gateway or MQTT credentials necessarily remain usable by their services and are protected
by private filesystem permissions. Backups containing those databases must stay
private as well.

## Verify after installation

```bash
systemctl is-active piwatch piwatch-ha-mqtt lora-notifyd piwatch-oled piwatch-web gpsd chrony
```

Sign in and check snapshot age, configured targets, the active settings revision,
Pi health and notification counts. Save a harmless label change and confirm the
revision becomes active without changing process IDs. Check chrony with
`chronyc tracking` and `chronyc sources -n`. Check LoRa `/health`, including its
`radio_connected` field. None of these proves destination delivery; ACK success,
timeout and disconnect tests still require an intended test destination/window.
The dashboard's Home Assistant MQTT card should report `Disabled` before setup or
`UP` with a connected broker after MQTT is enabled.

Freshness reports detect stale monitor and health snapshots. They do not add a
systemd watchdog heartbeat, hardware reboot logic, or automatic remediation.

## Rollback

Stop `piwatch-web` and `piwatch-ha-mqtt`, then stop `piwatch` so notification work checkpoints before
changing the radio service. Restore installed watchdog source/units from the
chosen private backup and remove the newly added `piwatch.service.d/config.conf`
if it was absent before this installation. For the original single-file radio/OLED
versions, restore the files in `checkout-before-panel` to their original checkout
paths. Reload systemd if unit/drop-in files changed, then restart radio, watchdog,
and OLED in that order. Keep `piwatch-web` disabled if restoring the old watchdog.

Preserve the current outbox and radio delivery databases during rollback. Their
original tables remain compatible with the previous reliability implementation;
the added incident table is additive. Do not restore an old delivery database just
to roll back code: losing newer checkpoints can cause duplicates. Leave the new
settings/auth databases privately stored for a later retry. The previous watchdog
uses `/etc/piwatch/config.json`, so panel changes will no longer govern it.
The previous watchdog also ignores the new frozen gateway overrides in jobs.
Before rolling back with pending new jobs, verify that its legacy gateways match
their intended destinations, or keep the watchdog stopped until those jobs have
been reviewed. Keeping the database alone does not preserve the old code's routing
behavior for newly configured gateways.

## Dashboard CSS and JavaScript updates

For overview changes confined to `web/static/style.css` and
`web/static/dashboard.js`, run:

```bash
sudo bash deployment/update-dashboard-assets.sh
```

This backs up the installed assets under `/var/backups/piwatch`, stages and
replaces the two files, then checks that the running panel serves the exact new
contents. Verification failure restores both originals. No Python code, settings,
units, or service processes change. Reload the dashboard after deployment.
Template or Python changes still require the normal deployment procedure.

## Full web interface updates

For template changes or shared browser scripts, use:

```bash
sudo bash deployment/update-web-interface.sh
```

This backs up the installed templates and browser assets, compiles the staged
Jinja templates, and briefly restarts only `piwatch-web`. It checks the login page
and verifies the served assets against the source. A deployment failure restores
the previous files and starts the panel again. Watchdog, OLED, radio, MQTT, settings,
and authentication databases are left in place. The asset-only updater above is
not sufficient for template changes.
