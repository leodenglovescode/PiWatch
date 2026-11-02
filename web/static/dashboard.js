'use strict';

document.body.classList.add('dashboard-page');

const { createApp, h, ref, computed, onMounted, onUnmounted } = Vue;

function element(tag, className, children, attrs = {}) {
  return h(tag, { class: className, ...attrs }, children);
}

function statusPill(state, label = null) {
  const value = String(state || 'UNKNOWN').toUpperCase();
  return element('span', `status-pill state-${value.toLowerCase()}`, [
    element('span', 'status-dot', []), label || value
  ]);
}

function panel(title, meta, content, className = '') {
  return element('section', `dashboard-panel ${className}`, [
    element('div', 'panel-heading', [
      element('div', '', [element('h2', '', title), meta ? element('p', 'panel-meta', meta) : null])
    ]),
    content
  ]);
}

function sparkline(values, min = null, max = null) {
  const samples = values.filter(Number.isFinite);
  if (samples.length < 2) return element('div', 'sparkline-empty', 'Collecting trend…');
  const low = min === null ? Math.min(...samples) : min;
  const high = max === null ? Math.max(...samples) : max;
  const spread = Math.max(1, high - low);
  const points = samples.map((value, index) => {
    const x = index * 100 / (samples.length - 1);
    const y = 38 - ((value - low) / spread) * 34;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(' ');
  return h('svg', { class: 'sparkline', viewBox: '0 0 100 42', preserveAspectRatio: 'none',
    role: 'img', 'aria-label': `Trend from ${samples[0]} to ${samples.at(-1)}` }, [
    h('line', { x1: 0, y1: 39, x2: 100, y2: 39, class: 'sparkline-base' }),
    h('polyline', { points, fill: 'none', class: 'sparkline-line', 'vector-effect': 'non-scaling-stroke' })
  ]);
}

function metricCard(title, value, unit, note, values, options = {}) {
  return element('article', `metric-card ${options.tone || ''}`, [
    element('div', 'metric-copy', [element('div', 'metric-label', title), element('div', 'metric-note', note)]),
    element('div', 'metric-trend', sparkline(values, options.min ?? null, options.max ?? null)),
    element('div', 'metric-reading', [element('strong', '', value), unit ? element('span', '', unit) : null])
  ]);
}

function table(headers, rows, empty) {
  const body = rows.length ? rows.map(row => h('tr', {}, row.map(cell => h('td', {},
    cell && typeof cell === 'object' && '__v_isVNode' in cell ? [cell] : String(cell ?? '—')
  )))) : [h('tr', {}, [h('td', { colspan: headers.length, class: 'empty' }, empty)])];
  return element('div', 'table-wrap', h('table', {}, [
    h('thead', {}, h('tr', {}, headers.map(label => h('th', { scope: 'col' }, label)))),
    h('tbody', {}, body)
  ]), { tabindex: 0, role: 'region', 'aria-label': headers.join(', ') });
}

function healthCard(title, state, rows, accent = '') {
  return element('article', `health-card ${accent}`, [
    element('div', 'health-card-head', [element('h3', '', title), statusPill(state)]),
    ...rows.map(([label, value]) => element('div', 'detail-row', [
      element('span', '', label), element('strong', '', value ?? '—')
    ]))
  ]);
}

function formatMegabitsPerSecond(value) {
  if (!Number.isFinite(value)) return '—';
  const mbps = value * 8 / 1_000_000;
  return `${mbps < 1 ? mbps.toFixed(3) : mbps.toFixed(2)} Mbps`;
}

function upsSupervisorState(supervisor) {
  if (!supervisor?.state) return 'UNKNOWN';
  if (supervisor.error || supervisor.startup_disarmed === false
      || supervisor.state === 'POWEROFF_FAILED') return 'DOWN';
  if (supervisor.state !== 'IDLE') return 'SUSPECT';
  return 'UP';
}

function deriveOverallState(isFresh, targets, notifications, monitorState, supervisorState) {
  if (!isFresh) return 'STALE';
  if (targets.down || supervisorState === 'DOWN') return 'DOWN';
  if (targets.suspect || targets.unknown || notifications.failed || monitorState !== 'UP'
      || supervisorState === 'SUSPECT') return 'DEGRADED';
  return 'UP';
}

const Dashboard = {
  setup() {
    const snapshot = ref(null);
    const receivedAt = ref(0);
    const now = ref(Date.now());
    const connectionFailed = ref(false);
    const loading = ref(false);
    const clearing = ref('');
    const history = ref({ cpu: [], memory: [], disk: [], temperature: [], rx: [], tx: [], load: [] });
    let pollTimer;
    let clockTimer;
    let lastMetricSample;

    const elapsed = computed(() => {
      now.value;
      return receivedAt.value ? (performance.now() - receivedAt.value) / 1000 : 0;
    });
    const fresh = computed(() => Boolean(snapshot.value?.fresh && !connectionFailed.value &&
      snapshot.value.age_seconds + elapsed.value <= snapshot.value.stale_after_seconds));
    const healthFresh = computed(() => fresh.value && snapshot.value?.health_fresh);
    const targets = computed(() => Object.entries(snapshot.value?.targets || {}));
    const targetCounts = computed(() => {
      const values = targets.value.map(([id, value]) => targetState(id, value));
      return { total: values.length, up: values.filter(value => value === 'UP').length,
        down: values.filter(value => value === 'DOWN').length,
        suspect: values.filter(value => value === 'SUSPECT').length,
        unknown: values.filter(value => ['UNKNOWN', 'STALE'].includes(value)).length };
    });
    const notificationCounts = computed(() => Object.values(snapshot.value?.notifications || {}).reduce(
      (counts, channel) => ({ pending: counts.pending + (channel.pending || 0),
        failed: counts.failed + (channel.failed || 0), done: counts.done + (channel.done || 0) }),
      { pending: 0, failed: 0, done: 0 }
    ));
    const overallState = computed(() => deriveOverallState(fresh.value, targetCounts.value,
      notificationCounts.value, snapshot.value?.monitor?.state,
      upsSupervisorState(snapshot.value?.ups_supervisor)));

    function addMetricSample(metrics) {
      if (!metrics?.checked_at || metrics.checked_at === lastMetricSample) return;
      lastMetricSample = metrics.checked_at;
      const mapping = { cpu: metrics.cpu_percent, memory: metrics.memory_percent,
        disk: metrics.disk_percent, temperature: metrics.temperature_c, rx: metrics.network_rx_bps,
        tx: metrics.network_tx_bps, load: metrics.load_1m };
      for (const [key, value] of Object.entries(mapping)) {
        if (Number.isFinite(value)) history.value[key] = [...history.value[key].slice(-59), value];
      }
    }

    async function refresh() {
      if (loading.value) return;
      loading.value = true;
      try {
        const data = await PW.request('/api/status');
        snapshot.value = data;
        receivedAt.value = performance.now();
        connectionFailed.value = false;
        addMetricSample(data.metrics);
      } catch (error) {
        connectionFailed.value = true;
        PW.error(error, false);
      } finally {
        loading.value = false;
      }
    }

    async function clearRecords(action) {
      const deliveries = action === 'clear_deliveries';
      const warning = deliveries
        ? 'Clear all completed and failed delivery records? Pending alerts will be preserved.'
        : 'Clear the full incident history? Current monitoring states will be preserved.';
      if (!confirm(warning)) return;
      clearing.value = action;
      try {
        const result = await PW.runMaintenance(action);
        if (result) {
          const count = deliveries ? result.deliveries : result.incidents;
          PW.notice(`${count} ${deliveries ? 'delivery records' : 'incidents'} cleared.`, true);
        } else PW.notice('Clear request accepted. The watchdog will apply it shortly.', true);
        await refresh();
      } catch (error) {
        PW.error(error);
      } finally {
        clearing.value = '';
      }
    }

    function targetState(id, value) {
      if (!fresh.value) return 'STALE';
      if (['battery', 'mains'].includes(id) && value.available === false && value.state !== 'DOWN') return 'UNKNOWN';
      const checkAge = Date.now() / 1000 - value.checked_at;
      if (['internet', 'dns'].includes(id) &&
          (checkAge < -5 || checkAge > snapshot.value.external_stale_after_seconds)) return 'STALE';
      return value.state;
    }

    function probeSummary(value) {
      const labels = { ping: 'Ping', ssh: 'SSH', tcp: 'TCP', http: 'HTTP', resolve: 'DNS' };
      const probes = Object.entries(labels).filter(([key]) => key in value).map(([key, label]) =>
        element('span', `probe ${value[key] ? 'probe-up' : 'probe-down'}`, `${label} ${value[key] ? 'OK' : 'FAIL'}`)
      );
      if (Number.isFinite(value.voltage_v)) probes.push(element('span', `probe ${value.low ? 'probe-down' : 'probe-up'}`, `${value.voltage_v.toFixed(3)} V`));
      if (typeof value.gpio_level === 'boolean') probes.push(element('span', `probe ${value.gpio_level ? 'probe-up' : 'probe-down'}`, `GPIO${value.gpio ?? 17} ${value.gpio_level ? 'HIGH' : 'LOW'}`));
      if (value.debouncing) probes.push(element('span', 'probe', 'Debouncing'));
      else if (value.available === false) probes.push(element('span', 'probe probe-down', 'Sensor unavailable'));
      return probes;
    }

    function freshnessNotice() {
      let message = 'All monitoring systems are reporting normally.';
      let state = 'good';
      if (connectionFailed.value) {
        message = 'Connection lost. Showing the last received sample.';
        state = 'bad';
      } else if (!snapshot.value?.available) {
        message = snapshot.value?.reason || 'Waiting for the first watchdog sample.';
        state = 'bad';
      } else if (!fresh.value) {
        message = 'Watchdog data is stale. Values below may no longer be current.';
        state = 'bad';
      } else if (snapshot.value.monitor?.config_error) {
        message = 'The monitor kept its last valid settings because a reload failed.';
        state = 'warn';
      } else if (upsSupervisorState(snapshot.value.ups_supervisor) === 'DOWN') {
        message = 'UPS supervisor needs attention. Check power and shutdown readiness.';
        state = 'bad';
      } else if (upsSupervisorState(snapshot.value.ups_supervisor) === 'SUSPECT') {
        message = 'UPS shutdown sequence is active. Check the power supply.';
        state = 'warn';
      } else if (targetCounts.value.down) {
        message = `${targetCounts.value.down} monitored ${targetCounts.value.down === 1 ? 'target is' : 'targets are'} down.`;
        state = 'bad';
      } else if (targetCounts.value.suspect || targetCounts.value.unknown) {
        const count = targetCounts.value.suspect + targetCounts.value.unknown;
        message = `${count} monitored ${count === 1 ? 'target needs' : 'targets need'} attention.`;
        state = 'warn';
      } else if (notificationCounts.value.failed) {
        const count = notificationCounts.value.failed;
        message = `${count} notification ${count === 1 ? 'delivery failure is' : 'delivery failures are'} retained for review.`;
        state = 'warn';
      } else if (snapshot.value.monitor?.state !== 'UP') {
        message = 'The monitor reports an internal health issue.';
        state = 'warn';
      }
      if (state === 'good') return null;
      return element('div', `freshness-bar freshness-${state}`, [
        element('span', 'freshness-icon', ''), element('strong', '', message),
        element('span', 'freshness-time', snapshot.value?.available ? `Sample ${PW.age(snapshot.value.age_seconds + elapsed.value)} ago` : '')
      ], { role: 'status' });
    }

    function summaryCards() {
      const counts = targetCounts.value;
      const notifications = notificationCounts.value;
      const item = (label, value, className = '') => element('div', `overview-stat ${className}`, [
        element('dt', '', label), element('dd', '', value)
      ]);
      const labels = { UP: 'Healthy', DOWN: 'Outage', DEGRADED: 'Needs attention', STALE: 'Stale' };
      return element('dl', 'overview-strip', [
        item('Service health', [statusPill(overallState.value, labels[overallState.value]),
          element('span', 'overview-context', `${counts.up} of ${counts.total} checks passing`)], 'overview-status'),
        item('Monitor uptime', PW.age(snapshot.value?.monitor?.uptime_seconds)),
        item('Queued alerts', notifications.pending),
        item('Delivery failures', notifications.failed, notifications.failed ? 'stat-warning' : '')
      ]);
    }

    function metricsPanel() {
      const metrics = snapshot.value?.metrics || snapshot.value?.self_health?.system || {};
      const cpu = metrics.cpu_percent;
      const memory = metrics.memory_percent;
      const disk = metrics.disk_percent;
      const temperature = metrics.temperature_c;
      return panel('System resources', 'Recent samples',
        element('div', 'metric-grid', [
          metricCard('CPU', Number.isFinite(cpu) ? cpu.toFixed(1) : '—', '%', `Load ${metrics.load_1m?.toFixed(2) ?? '—'} / ${metrics.load_5m?.toFixed(2) ?? '—'} / ${metrics.load_15m?.toFixed(2) ?? '—'}`, history.value.cpu, { min: 0, max: 100 }),
          metricCard('Memory', Number.isFinite(memory) ? memory.toFixed(1) : '—', '%', 'Used memory', history.value.memory, { min: 0, max: 100 }),
          metricCard('Disk', Number.isFinite(disk) ? disk.toFixed(1) : '—', '%', 'Root filesystem', history.value.disk, { min: 0, max: 100 }),
          metricCard('Temperature', Number.isFinite(temperature) ? temperature.toFixed(1) : '—', '°C', temperature >= 80 ? 'High temperature' : 'CPU temperature', history.value.temperature, { min: 20, max: 85, tone: temperature >= 80 ? 'metric-alert' : '' }),
          metricCard('Receive', formatMegabitsPerSecond(metrics.network_rx_bps).replace(' Mbps', ''), 'Mbps', 'Network in', history.value.rx, { min: 0 }),
          metricCard('Transmit', formatMegabitsPerSecond(metrics.network_tx_bps).replace(' Mbps', ''), 'Mbps', 'Network out', history.value.tx, { min: 0 })
        ]), 'metrics-panel');
    }

    function targetsPanel() {
      const priority = { DOWN: 0, SUSPECT: 1, UNKNOWN: 2, STALE: 3, UP: 4 };
      const ordered = [...targets.value].sort((a, b) =>
        (priority[targetState(...a)] ?? 2) - (priority[targetState(...b)] ?? 2));
      const rows = ordered.map(([id, value]) => [
        element('div', 'target-name', element('strong', '', value.name || id), { title: id }),
        statusPill(targetState(id, value)), element('div', 'probe-list', probeSummary(value)),
        `${PW.age(Date.now() / 1000 - value.checked_at)} ago`,
        value.down_since ? PW.age(Date.now() / 1000 - value.down_since) : '—'
      ]);
      return panel('Monitored services', `${targetCounts.value.total} configured`, [
        table(['Service', 'State', 'Probes', 'Last check', 'Outage'], rows, 'No monitored services are enabled.'),
        element('div', 'panel-footer', h('a', { href: '/settings' }, 'Manage checks →'))
      ], 'targets-panel');
    }

    function operationsPanel() {
      const health = snapshot.value?.self_health || {};
      const healthState = value => healthFresh.value ? value?.state : 'STALE';
      const gps = health.gps || {};
      const battery = snapshot.value?.targets?.battery;
      const mains = snapshot.value?.targets?.mains;
      const supervisor = snapshot.value?.ups_supervisor;
      const gpsFix = gps.mode === 3 ? '3D fix' : gps.mode === 2 ? '2D fix' : 'No fix';
      const satellites = Number.isFinite(gps.seen) && Number.isFinite(gps.used)
        ? `${gps.used} used / ${gps.seen} seen` : 'Awaiting complete SKY report';
      const serviceValues = Object.entries(health.services?.units || {});
      const mqtt = snapshot.value?.mqtt_bridge;
      const cards = [
        healthCard('GPS receiver', healthState(gps), [['Fix', gpsFix], ['Satellites', satellites], ['Sample', gps.report_at ? `${PW.age(Date.now() / 1000 - gps.report_at)} ago` : '—']], 'health-gps'),
        healthCard('Time synchronization', healthState(health.time), [['Source', health.time?.source], ['Stratum', health.time?.stratum], ['Leap status', health.time?.leap_status]], 'health-time'),
        healthCard('LoRa gateway', healthState(health.radio), [['USB radio', health.radio?.radio_connected ? 'Connected' : 'Disconnected'], ['Delivery proof', 'Destination ACK'], ['Health sample', health.radio?.checked_at ? `${PW.age(Date.now() / 1000 - health.radio.checked_at)} ago` : '—']], 'health-radio'),
        healthCard('Application services', healthState(health.services), serviceValues.length ? serviceValues.map(([name, state]) => [name, state]) : [['Status', 'No sample']], 'health-services')
      ];
      if (mqtt) {
        const mqttState = mqtt.state === 'UP' && mqtt.connected ? 'UP'
          : mqtt.state === 'DISABLED' ? 'UNKNOWN' : mqtt.state === 'CONNECTING' ? 'SUSPECT' : 'DOWN';
        cards.splice(3, 0, healthCard('Home Assistant MQTT', mqttState, [
          ['Publisher', mqtt.state === 'DISABLED' ? 'Disabled' : mqtt.state],
          ['Broker connection', mqtt.connected ? 'Connected' : 'Not connected'],
          ['Last publish', mqtt.last_publish_at ? `${PW.age(Date.now() / 1000 - mqtt.last_publish_at)} ago` : '—'],
          ['Source snapshot', Number.isFinite(mqtt.snapshot_age_seconds) ? `${PW.age(mqtt.snapshot_age_seconds)} old` : '—'],
          ['Error', mqtt.error || 'None']
        ], 'health-mqtt'));
      }
      if (supervisor?.state) {
        const supervisorHealth = upsSupervisorState(supervisor);
        const phase = {
          IDLE: 'Standing by', QUALIFYING: 'Confirming low battery', TRIGGERED: 'Shutdown triggered',
          TEST_TRIGGERED: 'Operator shutdown test',
          TEST_COUNTDOWN: 'Test shutdown countdown', DISPLAY_OFF: 'Turning display off',
          NOTIFYING: 'Sending final alert', ARMING: 'Arming Pico', ARMED: 'Pico armed',
          ARM_FAILED: 'Pico ARM failed', POWEROFF_REQUESTED: 'Poweroff requested',
          POWEROFF_FAILED: 'Poweroff failed',
          RECOVERY_DISARM_FAILED: 'Recovery DISARM failed'
        }[supervisor.state] || supervisor.state;
        cards.unshift(healthCard('UPS supervisor', supervisorHealth, [
          ['State', phase],
          ['Shutdown rule', `≤ ${supervisor.shutdown_voltage_v.toFixed(2)} V for ${supervisor.qualification_seconds.toFixed(0)} s`],
          ['Qualification', supervisor.state === 'QUALIFYING' ? `${supervisor.remaining_seconds.toFixed(1)} s remaining` : 'Not active'],
          ['Test countdown', supervisor.state === 'TEST_COUNTDOWN' ? `${supervisor.final_countdown_seconds.toFixed(1)} s remaining` : 'Not active'],
          ['Pico command', `GPIO${supervisor.command_gpio} · physical pin ${supervisor.command_physical_pin}`],
          ['Command health', supervisor.error || supervisor.startup_error || 'Ready']
        ], 'health-supervisor'));
      }
      if (battery) cards.unshift(healthCard('UPS battery', targetState('battery', battery), [
        ['Voltage', Number.isFinite(battery.voltage_v) ? `${battery.voltage_v.toFixed(3)} V` : 'Unavailable'],
        ['Estimated battery', Number.isFinite(battery.estimated_percentage) ? `${battery.estimated_percentage}%` : 'Unavailable'],
        ['Low alarm', `${battery.low_voltage_v.toFixed(2)} V`],
        ['Recovered', `${battery.recover_voltage_v.toFixed(2)} V`],
        ['Sensor', `I²C ${battery.bus} · ${battery.address}`]
      ], 'health-battery'));
      if (mains) cards.unshift(healthCard('Mains power', targetState('mains', mains), [
        ['State', mains.available === false ? 'Input unavailable' : mains.debouncing ? `Confirming ${mains.gpio_level ? 'restoration' : 'loss'}` : mains.outage ? 'Input absent' : 'Input present'],
        ['Input', typeof mains.gpio_level === 'boolean' ? `GPIO${mains.gpio} ${mains.gpio_level ? 'HIGH' : 'LOW'}` : 'Unavailable'],
        ['Battery', Number.isFinite(mains.voltage_v) ? `${mains.voltage_v.toFixed(3)} V` : 'Unavailable'],
        ['Detection', `Physical pin ${mains.physical_pin ?? 11} · ${mains.debounce_seconds ?? 3} s debounce`]
      ], 'health-mains'));
      return panel('Operational checks', 'Local hardware and service health', element('div', 'health-card-grid', cards), 'operations-panel');
    }

    function deliveryPanel() {
      const notifications = snapshot.value?.notifications || {};
      const jobs = snapshot.value?.notification_jobs || [];
      const channel = (key, title, note) => {
        const counts = notifications[key] || { pending: 0, failed: 0, done: 0 };
        return element('article', 'delivery-card', [
          element('div', 'delivery-card-head', [element('h3', '', title), statusPill(counts.failed ? 'FAILED' : counts.pending ? 'PENDING' : 'UP')]),
          element('div', 'delivery-counts', [
            element('div', '', [element('strong', '', counts.pending), element('span', '', 'Pending')]),
            element('div', '', [element('strong', '', counts.failed), element('span', '', 'Failed')]),
            element('div', '', [element('strong', '', counts.done), element('span', '', 'Complete')])
          ]), element('p', 'metric-note', note)
        ]);
      };
      const jobRows = jobs.map(job => [job.channel.toUpperCase(), statusPill(job.status),
        job.channel === 'lora' ? (job.stage === 0 ? 'Incident message' : 'Context message') :
          job.channel === 'ha' ? 'Home Assistant' : job.channel === 'smtp' ? 'Email' : 'IP fallback',
        `${job.attempts} / 5`, job.last_error || '—']);
      return panel('Notification delivery', `${jobs.length} jobs need attention`, [
        element('div', 'delivery-grid', [
          channel('ip', 'Standard IP alerts', 'Home Assistant first, SMTP fallback.'),
          channel('ha', 'Mains · Home Assistant', 'Independent delivery for mains events.'),
          channel('smtp', 'Mains · email', 'Independent delivery for mains events.'),
          channel('lora', 'LoRa', 'Complete after an explicit destination acknowledgment.')]),
        jobs.length ? table(['Channel', 'State', 'Stage', 'Attempts', 'Last error'], jobRows, '') : element('p', 'empty compact-empty', 'No pending or failed delivery jobs.'),
        element('div', 'panel-footer panel-actions', [
          element('span', '', 'Clears completed and failed records. Pending alerts are preserved.'),
          h('button', { class: 'danger', type: 'button',
            disabled: clearing.value || !(notificationCounts.value.done + notificationCounts.value.failed),
            onClick: () => clearRecords('clear_deliveries') },
          clearing.value === 'clear_deliveries' ? 'Clearing…' : 'Clear delivery records')
        ])
      ]);
    }

    function incidentsPanel() {
      const rows = (snapshot.value?.incidents || []).map(incident => [
        new Date(incident.timestamp * 1000).toLocaleString(),
        incident.name || snapshot.value?.targets?.[incident.target]?.name || incident.target,
        statusPill(incident.state === 'UP' ? (incident.target === 'mains' ? 'RESTORED' : 'RECOVERED') : incident.target === 'battery' ? 'LOW' : incident.target === 'mains' ? 'LOST' : incident.state),
        Number.isFinite(incident.probes?.voltage_v) ? `${incident.probes.voltage_v.toFixed(3)} V` : '—',
        incident.outage_seconds === null ? '—' : PW.age(incident.outage_seconds)
      ]);
      return panel('Incident history', 'Latest 100 events · 90-day retention', [
        table(['Time', 'Service', 'Event', 'Reading', 'Duration'], rows, 'No incidents recorded yet.'),
        element('div', 'panel-footer panel-actions', [
          element('span', '', 'Clears recorded events without changing current monitor state.'),
          h('button', { class: 'danger', type: 'button', disabled: clearing.value || !rows.length,
            onClick: () => clearRecords('clear_incidents') },
          clearing.value === 'clear_incidents' ? 'Clearing…' : 'Clear incident history')
        ])
      ], 'incidents-panel');
    }

    function render() {
      now.value;
      if (!snapshot.value) return element('div', 'dashboard-loading', [element('span', 'live-dot', []), 'Connecting to PiWatch…']);
      return element('div', 'dashboard-layout', [
        element('div', 'dashboard-heading', [
          element('div', '', [element('h1', '', 'Overview')]),
          element('div', 'live-controls', [
            element('span', `live-state ${fresh.value ? 'live' : 'offline'}`, [element('span', 'live-dot', []), fresh.value ? 'Live' : 'Stale']),
            element('span', 'last-updated', snapshot.value.available ? `Updated ${PW.age(snapshot.value.age_seconds + elapsed.value)} ago` : 'No sample'),
            h('button', { class: 'secondary refresh-button', type: 'button', disabled: loading.value, onClick: refresh }, loading.value ? 'Refreshing…' : 'Refresh')
          ])
        ]), freshnessNotice(), summaryCards(),
        element('div', 'dashboard-columns', [metricsPanel(), targetsPanel()])
      ]);
    }

    onMounted(() => {
      refresh();
      pollTimer = window.setInterval(() => { if (!document.hidden) refresh(); }, 1000);
      clockTimer = window.setInterval(() => { now.value = Date.now(); }, 1000);
      document.addEventListener('visibilitychange', refresh);
    });
    onUnmounted(() => {
      clearInterval(pollTimer);
      clearInterval(clockTimer);
      document.removeEventListener('visibilitychange', refresh);
    });
    return render;
  }
};

createApp(Dashboard).mount('#dashboard-app');
