'use strict';
let snapshot = null, received = 0, connectionFailed = false, loading = false;
function rows(id, values, empty, columns) {
  const table = document.getElementById(id); table.replaceChildren();
  for (const valuesRow of values) {
    const row = PW.node('tr');
    for (const value of valuesRow) {
      const cell = PW.node('td'); cell.append(value instanceof Node ? value : String(value ?? '—')); row.append(cell);
    }
    table.append(row);
  }
  if (!values.length) {const cell = PW.node('td', empty, 'empty'); cell.colSpan = columns; const row = PW.node('tr'); row.append(cell); table.append(row);}
}
function render() {
  const data = snapshot;
  const elapsed = (performance.now() - received) / 1000;
  const fresh = data?.fresh && data.age_seconds + elapsed <= data.stale_after_seconds && !connectionFailed;
  const healthFresh = fresh && data?.health_fresh;
  const banner = document.getElementById('freshness');
  banner.className = `banner ${fresh ? '' : 'bad'}`;
  banner.textContent = connectionFailed ? 'Connection lost. Values below are the last received sample.' : !data?.available ? data?.reason || 'Waiting for watchdog status.' : !fresh ? 'Watchdog status is stale. Values below may no longer be current.' : data.monitor.config_error ? 'Monitoring continues with the last valid settings. A settings reload failed.' : 'Watchdog is updating normally.';
  document.getElementById('updated').textContent = data?.available ? `Updated ${PW.age(data.age_seconds + elapsed)} ago` : 'No current sample';
  const targets = data?.targets || {};
  rows('targets', Object.entries(targets).map(([id, value]) => {
    const checkAge = Date.now() / 1000 - value.checked_at;
    const externalStale = ['internet', 'dns'].includes(id) && (checkAge < -5 || checkAge > data.external_stale_after_seconds);
    const probes = Object.entries(value).filter(([key]) => ['ping','ssh','tcp','http','resolve'].includes(key)).map(([key, ok]) => `${key.toUpperCase()} ${ok ? 'OK' : 'FAIL'}`).join(' · ');
    return [value.name || id, PW.badge(fresh && !externalStale ? value.state : 'STALE'), probes, `${PW.age(checkAge)} ago`, PW.age(value.outage_seconds)];
  }), 'No monitored services in this snapshot.', 5);
  const health = document.getElementById('health'); health.replaceChildren();
  const sampled = data?.self_health || {};
  function item(title, key, lines) {
    const value = sampled[key]; const node = PW.node('div', undefined, 'health-item');
    const heading = PW.node('h3', title); heading.append(PW.badge(!healthFresh ? 'STALE' : value?.state)); node.append(heading);
    for (const line of lines(value || {})) node.append(PW.node('p', line)); health.append(node);
  }
  item('System', 'system', v => [`Temperature ${v.temperature_c === undefined ? '—' : v.temperature_c.toFixed(1) + ' °C'}`, `Memory ${v.memory_percent ?? '—'}% · Disk ${v.disk_percent ?? '—'}%`, `Uptime ${PW.age(v.uptime_seconds)} · Load ${v.load_1m?.toFixed(2) ?? '—'}`]);
  item('Services', 'services', v => Object.entries(v.units || {}).map(([name, state]) => `${name}: ${state}`));
  item('Time synchronization', 'time', v => [`Source ${v.source ?? '—'}`, `Stratum ${v.stratum ?? '—'} · ${v.leap_status ?? 'Unknown'}`, v.offset ?? 'No sample']);
  item('GPS', 'gps', v => [`Fix ${v.mode === 3 ? '3D' : v.mode === 2 ? '2D' : 'None'}`, `Satellites ${v.used ?? '—'} used / ${v.seen ?? '—'} seen`]);
  item('LoRa radio', 'radio', v => [v.radio_connected === undefined ? 'No sample' : v.radio_connected ? 'USB radio connected' : 'Radio disconnected']);
  const monitor = PW.node('div', undefined, 'health-item'); const title = PW.node('h3', 'Monitor'); title.append(PW.badge(fresh ? data.monitor.state : 'STALE')); monitor.append(title, PW.node('p', `Settings revision ${data?.monitor?.settings_revision ?? '—'}`), PW.node('p', `Running ${PW.age(data?.monitor?.uptime_seconds)}`)); health.append(monitor);
  const delivery = document.getElementById('delivery'); delivery.replaceChildren();
  for (const [channel, label] of [['ip','Home Assistant / email'], ['lora','LoRa']]) {
    const counts = data?.notifications?.[channel]; const node = PW.node('div'); node.append(PW.node('h3', label));
    node.append(PW.node('p', counts ? `${counts.pending} pending · ${counts.failed} failed · ${counts.done} complete` : 'No sample')); delivery.append(node);
  }
  rows('jobs', (data?.notification_jobs || []).map(v => [v.channel.toUpperCase(), PW.badge(v.status.toUpperCase()), v.channel === 'lora' ? v.stage === 0 ? 'Incident message' : 'Context message' : 'IP delivery', `${v.attempts} / 5`, v.last_error || '—']), 'No pending or failed jobs in this snapshot.', 5);
  rows('incidents', (data?.incidents || []).map(v => [new Date(v.timestamp * 1000).toLocaleString(), v.name || targets[v.target]?.name || v.target, PW.badge(v.state === 'UP' ? 'RECOVERED' : v.state), PW.age(v.outage_seconds)]), 'No incidents recorded yet.', 4);
}
async function refresh() {
  if (loading) return; loading = true;
  try {snapshot = await PW.request('/api/status'); received = performance.now(); connectionFailed = false;}
  catch (error) {connectionFailed = true; PW.error(error);}
  finally {loading = false; render();}
}
document.getElementById('refresh').addEventListener('click', refresh);
refresh(); setInterval(refresh, 5000); setInterval(render, 1000);
