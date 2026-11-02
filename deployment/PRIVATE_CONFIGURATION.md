# Private installation configuration

Keep real values outside Git. `.private/`, `.env*`, `*.env`, databases, keys,
backups, and agent instructions are ignored. Ignoring a file does not remove
previously committed content.

The local `.private/site.env` preserves the deployment values removed from source.
It contains installation details, not copies of live password/token files. Keep it
mode 0600. Do not upload it or copy it into documentation.

## Runtime services

- Watchdog credentials and notification URLs: `/etc/piwatch/secrets.env`, loaded
  by the watchdog unit. Set `HA_BASE_URL` and `LORA_API_URL` explicitly.
- Radio credentials, destination, and device: `/etc/lora-notifyd.env`. Set
  `LORA_TOKEN`, `LORA_DEST`, and, when selecting a particular USB device,
  `LORA_DEVICE` to its exact path. `LORA_DEST` is required; no personal destination
  or USB serial is built into source.
- MQTT: configure the broker in the panel's private settings database. The
  fallback is localhost; `PIWATCH_MQTT_HOST` changes the initial default when
  supplied to each process that validates settings. Saved broker settings win.
- The radio service file is a generic template using `piwatch-radio`,
  `/opt/piwatch-radio`, and `/usr/local/lib/piwatch/lora`. Provision those first or
  adapt the installed unit. The previous installation-specific unit is retained
  locally in `.private/`; the live unit has not been changed.

Keep settings, authentication, delivery databases, session keys, and database
backups under their existing private `/var/lib` and `/var/backups` directories.
They may contain credentials even though the settings API redacts them.

## Local HTTPS helpers

The production Pi now has a root-owned mode-0600 `/etc/piwatch/site.env` for the
HTTPS and AdGuard helpers. For a new installation, create that file with these
installation variables:

```sh
PIWATCH_HTTPS_HOST=piwatch.example.test
PIWATCH_HTTPS_ADDRESS=192.0.2.99
ADGUARD_HOST=192.0.2.99
```

These are documentation addresses; supply your actual values. The helpers source
this file as shell code. `PIWATCH_SITE_ENV` may point to another trusted file.
The Caddy template is rendered during installation; do not install it directly.
Do not source the entire site file into every service: give each service only its
needed variables and credentials. Nothing here automatically changes a deployed
service or imports secrets into the checkout.

## Restart and deployment boundaries

The ignored site file is a record of removed source defaults, not an automatic
service environment loader. Existing watchdog and radio EnvironmentFiles remain
authoritative; do not replace their values with the recorded defaults. Saved
MQTT settings remain authoritative for the broker.

The production OLED and radio run directly from this checkout. Source edits can
therefore affect their next restart. The installed watchdog, MQTT publisher, and
web panel use the separate installed source tree until an explicit deployment.
The install/update scripts preserve the installed radio unit rather than installing
the generic radio template. No production services were restarted for this cleanup.
