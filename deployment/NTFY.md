# Native ntfy alerts and VPS heartbeat

PiWatch queues ntfy alerts with its incident IDs in the existing outbox. The
separate `piwatch-heartbeat` service reports fresh monitoring state every second.
It has no GPIO/I²C access and does not wait for probes or notification delivery.
The VPS keeps responsibility for detecting a missing Pi/home reporting path.
It does not probe home servers directly.

## State and delivery

Network/service targets become DOWN after three distinct failed reports. Recovery
requires one healthy report by default and is configurable in Settings. The
same tracker drives the dashboard and all incident channels. Battery thresholds,
mains debounce, Pico signalling and low-battery shutdown qualification remain
separate. Ordinary services and Internet/DNS have different polling intervals;
probe deadlines and scheduling add latency. Three reports do not guarantee a
three-second phone alert. Restarting the core preserves incident states and jobs
but resets consecutive probe counters, as before.

ntfy runs independently of the other alert paths. Transport failures, HTTP 429 and
server errors stay queued with exponential backoff capped at five minutes, so a
long home outage does not discard detailed incidents. Permanent rejections are
retained as failed after five attempts. Urgent final power events take priority.
Messages contain their incident ID; a lost response or crash after server receipt
can still produce a duplicate on retry. HTTP acceptance is not physical phone
confirmation. Other channels retain their existing retry policy.

The heartbeat has an 800 ms total transport deadline, verified HTTPS with a literal
IP address, and no ambient proxy or redirects. It sends exactly this compatible
message shape, with a boolean in the nested JSON string:

```json
{"topic":"piwatch-heartbeat","message":"{\"piwatch_fresh\":true}"}
```

Freshness uses the current boot ID and a monotonic snapshot age of at most five
seconds. Missing/stale monitoring sends `false`, not a claim that the worker is
healthy. Failed heartbeats are dropped; they are never queued or replayed.

## First cutover from the existing bridge

Use an interactive terminal on the Pi, from the checkout:

```bash
sudo bash deployment/install-native-ntfy.sh --maintenance-ack
```

The acknowledgement is deliberate: a prolonged maintenance gap can trigger the
VPS's existing missing-heartbeat warning. The installer never disables that VPS
policy or changes its rate limits. Choose a quiet window; all targets must be UP,
existing delivery queues drained, and current status fresh.

The installer:

1. Records installed source/unit fingerprints and backs up affected source and
   only the configuration fields it owns in a private directory.
2. Reads the existing Pi-only credential in a privileged migration, without
   displaying it. It derives the endpoint/topics from the installed bridge and
   copies the same public server certificate. Original bridge artifacts remain.
3. Stages native publishing behind a root-owned activation marker. It restarts
   the watchdog and web panel to load the new code, while the old bridge remains
   active. Other services and all UPS/power settings remain unchanged.
4. Checks settings/certificate readability under the actual service accounts.
   A short database writer barrier prevents an incident crossing the publisher
   handoff. The old bridge is stopped/disabled before native activation.
5. Requires 80 distinct consecutive accepted samples, no transport failures, and
   fresh monitoring evidence. It queues one clearly labelled notification through
   the actual native channel and checks VPS acceptance. Confirm its receipt on
   the Android phone separately.
6. On failure, pauses native publishing, restores the bridge and prior source,
   and selectively restores migration settings. Existing incident state and other
   channels' jobs are preserved. Native-only jobs are archived privately for review.

Rollback explicitly with the backup path printed by the installer:

```bash
sudo python3 deployment/ntfy_cutover.py --rollback /var/backups/piwatch/PRINTED-BACKUP
```

After personally confirming the labelled test on the phone, record that fact:

```bash
sudo python3 deployment/ntfy_cutover.py --confirm-phone
```

This flag is an operator statement; it is never inferred from an HTTP response.

## Verify from the Mac

Run `deployment/verify-ntfy-cutover.py` on the Mac, where the existing SSH aliases
are available. For example, stream the script from the Pi checkout and execute it
locally, replacing the checkout path for that installation:

```bash
ssh watchdog-pi 'cat /path/to/piwatch/deployment/verify-ntfy-cutover.py' | python3 -
```

Optional arguments are `--pi watchdog-pi --vps bj-vps`. Both SSH accounts need
read access for the checks. The VPS account must be able to read its ntfy process
metadata, selected cache/rate-limit configuration, heartbeat cache rows, and
monitor journal; the script never reads authentication databases or credentials.

The script matches all 80 native message IDs and nested freshness payloads on the
VPS, checks timestamp tolerance and a currently fresh heartbeat, checks both
services and the corrected rate limits, and looks for monitor failure diagnostics.
It records the deployed monitor binary hash. It does not alter VPS configuration,
restart its services, or inspect the SuperServer resource agent. If ntfy has only
an in-memory cache or access is insufficient, it fails explicitly instead of
claiming historical verification. Evidence belongs to the cutover boot; a later
reboot needs a new observation before treating that historical proof as current.

## Settings and maintenance

Settings → ntfy & VPS controls alerts, heartbeat, endpoint, topics, and system or
private-certificate trust. Username and password are write-only and omitted
values preserve credentials. The private certificate is installed at
`/etc/piwatch-ntfy-native/server.crt`; there is no insecure TLS option.

The overview's VPS reporting section shows actual acceptance age, freshness,
errors, queue/retry/failure counts and recent delivery records. The labelled test
button uses saved settings, is rate-limited, and queues a durable job. It reports
VPS acceptance separately from phone delivery.

Disabling heartbeats intentionally trips the VPS detector and requires explicit
acknowledgement in Settings. Coordinate maintenance with ServerWatch separately.
Disabling new incident notifications does not cancel older frozen outbox jobs.
Do not run the old bridge alongside native publishing or delete its artifacts
until native operation and rollback have been reviewed.

Normal installation/update scripts include the native lifecycle. They refuse to
bypass the first handover when a legacy bridge is present. Subsequent core updates
refresh the independent heartbeat installation after the watchdog is healthy.
No reboot is required for this integration.
