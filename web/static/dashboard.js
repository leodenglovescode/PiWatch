'use strict';

const { createApp, h, ref, computed, onMounted, onUnmounted } = Vue;

function element(tag, className, children, attrs = {}) {
  return h(tag, { class: className, ...attrs }, children);
}

function statusPill(state) {
  const value = String(state || 'UNKNOWN').toUpperCase();
  return element('span', `status-pill state-${value.toLowerCase()}`, [
    element('span', 'status-dot', []), value
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
  const percent = Number.isFinite(options.percent) ? Math.max(0, Math.min(100, options.percent)) : null;
  return element('article', `metric-card ${options.tone || ''}`, [
    element('div', 'metric-label', title),
    element('div', 'metric-reading', [element('strong', '', value), unit ? element('span', '', unit) : null]),
    element('div', 'metric-note', note),
    percent === null ? null : element('div', 'meter', element('span', '', [], { style: `width:${percent}%` })),
    sparkline(values, options.min ?? null, options.max ?? null)
  ]);
}

function table(headers, rows, empty) {
  const body = rows.length ? rows.map(row => h('tr', {}, row.map(cell => h('td', {},
    cell && typeof cell === 'object' && '__v_isVNode' in cell ? [cell] : String(cell ?? '—')
  )))) : [h('tr', {}, [h('td', { colspan: headers.length, class: 'empty' }, empty)])];
  return element('div', 'table-wrap', h('table', {}, [
    h('thead', {}, h('tr', {}, headers.map(label => h('th', {}, label)))),
    h('tbody', {}, body)
  ]));
}

function healthCard(title, state, rows, accent = '') {
  return element('article', `health-card ${accent}`, [
    element('div', 'health-card-head', [element('h3', '', title), statusPill(state)]),
    ...rows.map(([label, value]) => element('div', 'detail-row', [
      element('span', '', label), element('strong', '', value ?? '—')
    ]))
  ]);
}

function formatBytesPerSecond(value) {
  if (!Number.isFinite(value)) return '—';
  if (value < 1024) return `${Math.round(value)} B/s`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB/s`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB/s`;
  return `${(value / 1024 ** 3).toFixed(1)} GB/s`;
}

function deriveOverallState(isFresh, targets, notifications, monitorState) {
  if (!isFresh) return 'STALE';
  if (targets.down) return 'DOWN';
  if (targets.suspect || targets.unknown || notifications.failed || monitorState !== 'UP') return 'DEGRADED';
  return 'UP';
}

const Dashboard = {
  setup() {
    const snapshot = ref(null);
    const receivedAt = ref(0);
    const now = ref(Date.now());
    const connectionFailed = ref(false);
    const loading = ref(false);
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
      notificationCounts.value, snapshot.value?.monitor?.state));

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
        PW.error(error);
      } finally {
        loading.value = false;
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
      if (Number.isFinite(value.shunt_voltage_mv)) probes.push(element('span', 'probe', `${value.shunt_voltage_mv >= 0 ? '+' : ''}${value.shunt_voltage_mv.toFixed(3)} mV`));
      if (value.inferred && value.detection_method) probes.push(element('span', 'probe probe-down', value.detection_method === 'shunt-direction' ? 'Discharging' : `Drop ${value.voltage_drop_v.toFixed(3)} V`));
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
      return element('div', `freshness-bar freshness-${state}`, [
        element('span', 'freshness-icon', ''), element('strong', '', message),
        element('span', 'freshness-time', snapshot.value?.available ? `Sample ${PW.age(snapshot.value.age_seconds + elapsed.value)} ago` : '')
      ], { role: 'status' });
    }

    function summaryCards() {
      const counts = targetCounts.value;
      const notifications = notificationCounts.value;
      let overallNote = `${counts.up} of ${counts.total} checks healthy`;
      if (!fresh.value) overallNote = 'Current state unavailable';
      else if (counts.down) overallNote = `${counts.down} ${counts.down === 1 ? 'check is' : 'checks are'} down`;
      else if (counts.suspect || counts.unknown) {
        const attention = counts.suspect + counts.unknown;
        overallNote = `${attention} ${attention === 1 ? 'check needs' : 'checks need'} attention`;
      } else if (notifications.failed) {
        overallNote = `Targets healthy · ${notifications.failed} delivery ${notifications.failed === 1 ? 'failure' : 'failures'} retained`;
      } else if (snapshot.value?.monitor?.state !== 'UP') overallNote = 'Targets healthy · monitor needs attention';
      return element('div', 'summary-grid', [
        element('article', 'summary-card summary-primary', [element('span', 'summary-label', 'Overall status'),
          element('div', 'summary-state', statusPill(overallState.value)),
          element('p', '', overallNote)]),
        element('article', 'summary-card', [element('span', 'summary-label', 'Monitored services'),
          element('strong', 'summary-value', `${counts.up}/${counts.total}`),
          element('p', '', counts.down ? `${counts.down} down` : (counts.suspect || counts.unknown) ? `${counts.suspect + counts.unknown} need attention` : 'No active outages')]),
        element('article', 'summary-card', [element('span', 'summary-label', 'Notification queue'),
          element('strong', 'summary-value', `${notifications.pending + notifications.failed}`),
          element('p', '', `${notifications.pending} pending · ${notifications.failed} failed`)]),
        element('article', 'summary-card', [element('span', 'summary-label', 'Monitor uptime'),
          element('strong', 'summary-value summary-duration', PW.age(snapshot.value?.monitor?.uptime_seconds)),
          element('p', '', `Settings revision ${snapshot.value?.monitor?.settings_revision ?? '—'}`)])
      ]);
    }

    function metricsPanel() {
      const metrics = snapshot.value?.metrics || snapshot.value?.self_health?.system || {};
      const cpu = metrics.cpu_percent;
      const memory = metrics.memory_percent;
      const disk = metrics.disk_percent;
      const temperature = metrics.temperature_c;
      return panel('Live system metrics', 'Rolling samples collected while this page is open', [
        element('div', 'metric-grid', [
          metricCard('CPU', Number.isFinite(cpu) ? cpu.toFixed(1) : '—', '%', `Load ${metrics.load_1m?.toFixed(2) ?? '—'} / ${metrics.load_5m?.toFixed(2) ?? '—'} / ${metrics.load_15m?.toFixed(2) ?? '—'}`, history.value.cpu, { percent: cpu, min: 0, max: 100 }),
          metricCard('Memory', Number.isFinite(memory) ? memory.toFixed(1) : '—', '%', 'Available system memory', history.value.memory, { percent: memory, min: 0, max: 100 }),
          metricCard('Disk', Number.isFinite(disk) ? disk.toFixed(1) : '—', '%', 'Root filesystem used', history.value.disk, { percent: disk, min: 0, max: 100 }),
          metricCard('Temperature', Number.isFinite(temperature) ? temperature.toFixed(1) : '—', '°C', temperature >= 80 ? 'Thermal attention required' : 'CPU thermal sensor', history.value.temperature, { percent: temperature / 85 * 100, min: 20, max: 85, tone: temperature >= 80 ? 'metric-alert' : '' })
        ]),
        element('div', 'network-strip', [
          element('div', 'network-reading', [element('span', 'network-direction', '↓'), element('div', '', [element('span', '', 'Receive'), element('strong', '', formatBytesPerSecond(metrics.network_rx_bps))])]),
          element('div', 'network-chart', sparkline(history.value.rx, 0, null)),
          element('div', 'network-reading', [element('span', 'network-direction', '↑'), element('div', '', [element('span', '', 'Transmit'), element('strong', '', formatBytesPerSecond(metrics.network_tx_bps))])]),
          element('div', 'network-chart', sparkline(history.value.tx, 0, null))
        ])
      ], 'metrics-panel');
    }

    function targetsPanel() {
      const rows = targets.value.map(([id, value]) => [
        element('div', 'target-name', [element('strong', '', value.name || id), element('span', '', id)]),
        statusPill(targetState(id, value)), element('div', 'probe-list', probeSummary(value)),
        `${PW.age(Date.now() / 1000 - value.checked_at)} ago`,
        value.down_since ? PW.age(Date.now() / 1000 - value.down_since) : '—'
      ]);
      return panel('Monitored services', `${targetCounts.value.total} active targets`, [
        table(['Service', 'State', 'Probes', 'Last check', 'Outage'], rows, 'No monitored services are enabled.'),
        element('div', 'panel-footer', h('a', { href: '/settings' }, 'Configure targets and probes'))
      ], 'targets-panel');
    }

    function operationsPanel() {
      const health = snapshot.value?.self_health || {};
      const healthState = value => healthFresh.value ? value?.state : 'STALE';
      const gps = health.gps || {};
      const battery = snapshot.value?.targets?.battery;
      const mains = snapshot.value?.targets?.mains;
      const gpsFix = gps.mode === 3 ? '3D fix' : gps.mode === 2 ? '2D fix' : 'No fix';
      const satellites = Number.isFinite(gps.seen) && Number.isFinite(gps.used)
        ? `${gps.used} used / ${gps.seen} seen` : 'Awaiting complete SKY report';
      const serviceValues = Object.entries(health.services?.units || {});
      const cards = [
        healthCard('GPS receiver', healthState(gps), [['Fix', gpsFix], ['Satellites', satellites], ['Sample', gps.report_at ? `${PW.age(Date.now() / 1000 - gps.report_at)} ago` : '—']], 'health-gps'),
        healthCard('Time synchronization', healthState(health.time), [['Source', health.time?.source], ['Stratum', health.time?.stratum], ['Leap status', health.time?.leap_status]], 'health-time'),
        healthCard('LoRa gateway', healthState(health.radio), [['USB radio', health.radio?.radio_connected ? 'Connected' : 'Disconnected'], ['Delivery proof', 'Destination ACK'], ['Health sample', health.radio?.checked_at ? `${PW.age(Date.now() / 1000 - health.radio.checked_at)} ago` : '—']], 'health-radio'),
        healthCard('Application services', healthState(health.services), serviceValues.length ? serviceValues.map(([name, state]) => [name, state]) : [['Status', 'No sample']], 'health-services')
      ];
      if (battery) cards.unshift(healthCard('UPS battery', targetState('battery', battery), [
        ['Voltage', Number.isFinite(battery.voltage_v) ? `${battery.voltage_v.toFixed(3)} V` : 'Unavailable'],
        ['Low alarm', `${battery.low_voltage_v.toFixed(2)} V`],
        ['Recovered', `${battery.recover_voltage_v.toFixed(2)} V`],
        ['Sensor', `I²C ${battery.bus} · ${battery.address}`]
      ], 'health-battery'));
      if (mains) cards.unshift(healthCard('Mains power', targetState('mains', mains), [
        ['State', mains.outage ? 'Outage inferred' : 'Present'],
        ['Shunt', Number.isFinite(mains.shunt_voltage_mv) ? `${mains.shunt_voltage_mv >= 0 ? '+' : ''}${mains.shunt_voltage_mv.toFixed(3)} mV` : 'Unavailable'],
        ['Direction', mains.discharge_polarity === 'unlearned' ? 'Awaiting first failover' : `${mains.discharge_polarity} = discharge`],
        ['Detection', mains.detection_method ? mains.detection_method.replace('-', ' ') : 'Watching direction and voltage']
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
        jobs.length ? table(['Channel', 'State', 'Stage', 'Attempts', 'Last error'], jobRows, '') : element('p', 'empty compact-empty', 'No pending or failed delivery jobs.')
      ]);
    }

    function incidentsPanel() {
      const rows = (snapshot.value?.incidents || []).map(incident => [
        new Date(incident.timestamp * 1000).toLocaleString(),
        incident.name || snapshot.value?.targets?.[incident.target]?.name || incident.target,
        statusPill(incident.state === 'UP' ? (incident.target === 'mains' ? 'RESTORED' : 'RECOVERED') : incident.target === 'battery' ? 'LOW' : incident.target === 'mains' ? 'OUTAGE' : incident.state),
        Number.isFinite(incident.probes?.voltage_v) ? `${incident.probes.voltage_v.toFixed(3)} V` : '—',
        incident.outage_seconds === null ? '—' : PW.age(incident.outage_seconds)
      ]);
      return panel('Incident history', 'Latest 100 events · 90-day retention', table(
        ['Time', 'Service', 'Event', 'Reading', 'Duration'], rows, 'No incidents recorded yet.'
      ), 'incidents-panel');
    }

    function render() {
      now.value;
      if (!snapshot.value) return element('div', 'dashboard-loading', [element('span', 'live-dot', []), 'Connecting to PiWatch…']);
      return element('div', 'dashboard-layout', [
        element('div', 'dashboard-heading', [
          element('div', '', [element('p', 'eyebrow', 'WATCHDOG-PI'), element('h1', '', 'System overview')]),
          element('div', 'live-controls', [
            element('span', `live-state ${fresh.value ? 'live' : 'offline'}`, [element('span', 'live-dot', []), fresh.value ? 'Live' : 'Stale']),
            element('span', 'last-updated', snapshot.value.available ? `Updated ${PW.age(snapshot.value.age_seconds + elapsed.value)} ago` : 'No sample'),
            h('button', { class: 'secondary refresh-button', type: 'button', disabled: loading.value, onClick: refresh }, loading.value ? 'Refreshing…' : 'Refresh')
          ])
        ]), freshnessNotice(), summaryCards(), metricsPanel(), targetsPanel(), operationsPanel(), deliveryPanel(), incidentsPanel()
      ]);
    }

    onMounted(() => {
      refresh();
      pollTimer = window.setInterval(() => { if (!document.hidden) refresh(); }, 2000);
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
