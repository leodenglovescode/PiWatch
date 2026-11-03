'use strict';
const form = document.getElementById('settings-form');
let config, revision, dirty = false;
function markDirty() {
  dirty = true;
  document.getElementById('save-status').textContent = 'Unsaved changes';
}
form.addEventListener('input', markDirty);
form.addEventListener('change', markDirty);
window.addEventListener('beforeunload', event => {
  if (dirty) {event.preventDefault(); event.returnValue = '';}
});
const numeric = ['interval_seconds','external_interval_seconds','health_interval_seconds','suspect_after_failures','down_after_failures','recover_after_successes'];
const targetEditors = document.getElementById('target-editors');
const mqttSensorDefinitions = {
  cpu_temperature:'CPU temperature', cpu_usage:'CPU usage', memory_usage:'Memory usage',
  disk_usage:'Disk usage', load_1m:'Load average', network_rx:'Network receive rate',
  network_tx:'Network transmit rate', uptime:'System uptime', battery_voltage:'UPS battery voltage',
  battery_percentage:'Estimated battery',
  mains_power:'Mains power', ups_state:'UPS supervisor state', monitor_state:'Monitor state',
  data_fresh:'Data freshness', snapshot_age:'Snapshot age',
  notifications_pending:'Pending notifications', notifications_failed:'Failed notifications'
};
function syncBatteryFields() {
  const batteryEnabled = form.elements.battery_enabled.checked;
  const mainsEnabled = form.elements.mains_detection_enabled.checked;
  for (const input of ['battery_bus','battery_address'])
    form.elements[input].disabled = !(batteryEnabled || mainsEnabled);
  for (const input of ['battery_low_voltage','battery_recover_voltage'])
    form.elements[input].disabled = !batteryEnabled;
  form.elements.mains_debounce_seconds.disabled = !mainsEnabled;
}
form.elements.battery_enabled.addEventListener('change', syncBatteryFields);
form.elements.mains_detection_enabled.addEventListener('change', syncBatteryFields);
function targetEditor(target) {
  const editor = PW.node('details', undefined, 'target-editor'); editor.dataset.id = target.id;
  editor.innerHTML = `<summary><span class="target-summary-name"></span><span class="target-summary-host"></span><span class="target-summary-state"></span></summary><div class="target-body"><div class="target-heading"><label class="check"><input data-field="enabled" type="checkbox">Enable monitoring</label><button type="button" class="danger remove-target">Remove service</button></div><div class="form-grid two"><label>Service name<input data-field="name" required maxlength="40"></label><label>IP address / hostname<input data-field="host" required maxlength="253"></label></div><div class="target-checks"><fieldset class="probe-editor"><legend><label class="check"><input type="checkbox" data-field="ping">Ping</label></legend><p class="field-hint">Check whether the host responds.</p></fieldset><fieldset class="probe-editor"><legend><label class="check"><input type="checkbox" data-field="tcp">TCP</label></legend><label>TCP port<input type="number" min="1" max="65535" data-field="tcp_port"></label></fieldset><fieldset class="probe-editor"><legend><label class="check"><input type="checkbox" data-field="http">HTTP</label></legend><label>HTTP port<input type="number" min="1" max="65535" data-field="http_port"></label><label>HTTP path<input data-field="http_path" maxlength="1024"></label></fieldset></div></div>`;

  const input = key => editor.querySelector(`[data-field="${key}"]`);
  input('enabled').checked = target.enabled; input('name').value = target.name; input('host').value = target.host;
  for (const kind of ['ping','tcp','http']) {
    const check = target.checks.find(c => c.type === kind); input(kind).checked = Boolean(check);
    if (kind !== 'ping') input(kind + '_port').value = check?.port ?? (kind === 'http' ? 80 : 22);
    if (kind === 'http') input('http_path').value = check?.path ?? '/';
    const enableFields = () => {
      if (kind === 'ping') return;
      for (const key of kind === 'http' ? ['http_port','http_path'] : ['tcp_port']) {
        input(key).disabled = !input(kind).checked; input(key).required = input(kind).checked;
      }
    };
    input(kind).addEventListener('change', enableFields); enableFields();
  }
  const summary = () => {
    editor.querySelector('.target-summary-name').textContent = input('name').value || 'New service';
    editor.querySelector('.target-summary-host').textContent = input('host').value || 'Host not set';
    editor.querySelector('.target-summary-state').textContent = input('enabled').checked ? 'Enabled' : 'Disabled';
  };
  editor.addEventListener('input', summary); editor.addEventListener('change', summary); summary();
  editor.querySelector('.remove-target').onclick = () => {
    if (confirm(`Remove ${input('name').value || 'this service'} from monitoring? Its history will be retained.`)) {
      editor.remove(); markDirty(); document.getElementById('add-target').focus();
    }
  };
  targetEditors.append(editor); PW.enhanceForms(editor);
}
const gatewayDefinitions = {
  lora: {title:'LoRa gateway', fields:[['url','API base URL','url'],['token','Bearer token','password']]},
  home_assistant: {title:'Home Assistant webhook', fields:[['url','Base URL','url'],['webhook_id','Webhook ID','password']]},
  smtp: {title:'Email (SMTP)', fields:[['host','SMTP host','text'],['port','Port','number'],['security','Connection security','select'],['username','SMTP username','text'],['password','SMTP password','password'],['sender','From address','email'],['recipient','Recipient address','email']]}
};
function gatewayEditors(secretsConfigured) {
  const container = document.getElementById('gateway-editors'); container.replaceChildren();
  for (const [name, definition] of Object.entries(gatewayDefinitions)) {
    const gateway = config.gateways[name]; const editor = PW.node('fieldset', undefined, 'gateway-editor'); editor.dataset.gateway = name;
    editor.append(PW.node('legend', definition.title));
    const enabledLabel = PW.node('label', undefined, 'check'); const enabled = PW.node('input'); enabled.type = 'checkbox'; enabled.dataset.field = 'enabled'; enabled.checked = gateway.enabled; enabledLabel.append(enabled, 'Enabled'); editor.append(enabledLabel);
    const fields = PW.node('div', undefined, 'form-grid two');
    for (const [key, labelText, type] of definition.fields) {
      const label = PW.node('label', labelText); const input = PW.node(type === 'select' ? 'select' : 'input');
      if (type === 'select') {
        for (const [value,text] of [['','Existing service setting'],['ssl','SSL / TLS'],['starttls','STARTTLS']]) {const option = PW.node('option', text); option.value = value; input.append(option);}
      } else {input.type = type; input.maxLength = 4096; if (type === 'number') {input.min = '1'; input.max = '65535';}}
      input.dataset.field = key;
      if (type === 'password') {
        input.autocomplete = 'new-password'; input.placeholder = secretsConfigured[name]?.[key] ? 'Stored — leave blank to keep' : 'Leave blank to keep existing service setting';
      } else input.value = gateway[key] ?? '';
      label.append(input);
      if (type === 'password') {
        const clearLabel = PW.node('label', undefined, 'check'); const clear = PW.node('input'); clear.type = 'checkbox'; clear.dataset.clear = key;
        clearLabel.append(clear, 'Clear this credential');
        const group = PW.node('div'); const hint = PW.node('p', 'Leave blank to keep the stored credential or service setting.', 'field-hint'); hint.id = `gateway-${name}-${key}-hint`; input.setAttribute('aria-describedby', hint.id); group.append(label, clearLabel, hint); fields.append(group);
      } else fields.append(label);
    }
    editor.append(fields); container.append(editor);
  }
}
function mqttSensorEditors(enabledSensors) {
  const container = document.getElementById('mqtt-sensors'); container.replaceChildren();
  for (const [id, title] of Object.entries(mqttSensorDefinitions)) {
    const label = PW.node('label', undefined, 'check');
    const input = PW.node('input'); input.type = 'checkbox'; input.dataset.mqttSensor = id;
    input.checked = enabledSensors.includes(id); label.append(input, title); container.append(label);
  }
}
async function load() {
  const data = await PW.request('/api/settings'); config = data.config; revision = data.revision;
  document.getElementById('revision').textContent = `Saved revision ${revision}`;
  targetEditors.replaceChildren(); config.targets.forEach(targetEditor);
  for (const name of numeric) form.elements[name].value = config[name];
  form.elements.internet_enabled.checked = config.internet.enabled;
  form.elements.dns_enabled.checked = config.dns.enabled;
  form.elements.battery_enabled.checked = config.battery.enabled;
  form.elements.battery_bus.value = config.battery.bus;
  form.elements.battery_address.value = `0x${config.battery.address.toString(16)}`;
  form.elements.battery_low_voltage.value = config.battery.low_voltage_v.toFixed(2);
  form.elements.battery_recover_voltage.value = config.battery.recover_voltage_v.toFixed(2);
  form.elements.mains_detection_enabled.checked = config.battery.mains_detection_enabled;
  form.elements.mains_debounce_seconds.value = config.battery.mains_debounce_seconds;
  syncBatteryFields();
  form.elements.internet_targets.value = config.internet.targets.map(t => `${t.host}:${t.port}`).join('\n');
  form.elements.dns_names.value = config.dns.names.join('\n');
  form.elements.mqtt_enabled.checked = config.mqtt.enabled;
  form.elements.mqtt_host.value = config.mqtt.host;
  form.elements.mqtt_port.value = config.mqtt.port;
  form.elements.mqtt_tls.checked = config.mqtt.tls;
  form.elements.mqtt_username.value = config.mqtt.username;
  form.elements.mqtt_password.value = '';
  form.elements.mqtt_password.placeholder = data.configured_secrets.mqtt?.password ? 'Stored — leave blank to keep' : 'Optional';
  form.elements.mqtt_password_clear.checked = false;
  form.elements.mqtt_topic_prefix.value = config.mqtt.topic_prefix;
  form.elements.mqtt_discovery_prefix.value = config.mqtt.discovery_prefix;
  form.elements.mqtt_publish_interval.value = config.mqtt.publish_interval_seconds;
  form.elements.mqtt_expire_after.value = config.mqtt.expire_after_seconds;
  mqttSensorEditors(config.mqtt.sensors);
  gatewayEditors(data.configured_secrets);
  form.elements.network_suspect.value = config.network_policy.suspect_after_failures;
  form.elements.network_recover.value = config.network_policy.recover_after_successes;
  const ntfy = config.gateways.ntfy;
  for (const field of ['enabled', 'heartbeat_enabled']) form.elements['ntfy_' + field].checked = ntfy[field];
  for (const field of ['url', 'topic', 'heartbeat_topic', 'tls_trust']) form.elements['ntfy_' + field].value = ntfy[field];
  for (const field of ['username', 'password']) {
    form.elements['ntfy_' + field].value = '';
    form.elements['ntfy_' + field + '_clear'].checked = false;
    form.elements['ntfy_' + field].placeholder = data.configured_secrets.ntfy?.[field] ? 'Stored — leave blank to keep' : 'Not configured';
  }
  form.elements.ntfy_disable_ack.checked = false;
  document.getElementById('ntfy-configured').textContent = 'Username ' + (data.configured_secrets.ntfy?.username ? 'stored' : 'not configured') + '; password ' + (data.configured_secrets.ntfy?.password ? 'stored' : 'not configured') + '.';

  dirty = false; form.setAttribute('aria-busy', 'false'); PW.enhanceForms(form);
  document.getElementById('save-settings').disabled = false;
  document.getElementById('save-status').textContent = 'Changes are validated before they are applied.';
}
document.getElementById('add-target').onclick = () => {
  if (targetEditors.children.length >= 32) return PW.notice('Configure at most 32 monitored services.');
  targetEditor({id:'target_' + Date.now().toString(36) + Math.random().toString(36).slice(2,6),name:'',host:'',enabled:true,checks:[{type:'ping'}]});
  targetEditors.lastElementChild.open = true; markDirty();
  PW.focusField(targetEditors.lastElementChild.querySelector('[data-field="name"]'));
};
function readConfig() {
  const value = structuredClone(config);
  for (const name of numeric) value[name] = Number(form.elements[name].value);
  value.targets = [...targetEditors.children].map(editor => {
    const input = key => editor.querySelector(`[data-field="${key}"]`);
    const checks = [];
    if (input('ping').checked) checks.push({type:'ping'});
    if (input('tcp').checked) checks.push({...config.targets.find(t => t.id === editor.dataset.id)?.checks.find(c => c.type === 'tcp'),type:'tcp',port:Number(input('tcp_port').value)});
    if (input('http').checked) checks.push({type:'http',port:Number(input('http_port').value),path:input('http_path').value});
    if (!checks.length) throw PW.fieldError(input('ping'), 'Select at least one check for this service.');
    return {id:editor.dataset.id,name:input('name').value.trim(),host:input('host').value.trim(),enabled:input('enabled').checked,checks};
  });
  value.network_policy = {suspect_after_failures: Number(form.elements.network_suspect.value),
    down_after_failures: 3, recover_after_successes: Number(form.elements.network_recover.value)};
  value.gateways.ntfy = {enabled: form.elements.ntfy_enabled.checked,
    heartbeat_enabled: form.elements.ntfy_heartbeat_enabled.checked,
    url: form.elements.ntfy_url.value.trim(), topic: form.elements.ntfy_topic.value,
    heartbeat_topic: form.elements.ntfy_heartbeat_topic.value, tls_trust: form.elements.ntfy_tls_trust.value};
  for (const field of ['username', 'password']) {
    if (form.elements['ntfy_' + field].value) value.gateways.ntfy[field] = form.elements['ntfy_' + field].value;
    if (form.elements['ntfy_' + field + '_clear'].checked) value.gateways.ntfy[field] = '';
  }
  value.internet.enabled = form.elements.internet_enabled.checked;
  value.internet.targets = form.elements.internet_targets.value.split('\n').map(s => s.trim()).filter(Boolean).map(line => {
    const separator = line.lastIndexOf(':'); if (separator < 1) throw PW.fieldError(form.elements.internet_targets, 'Enter each Internet endpoint as IP:port.');
    return {host:line.slice(0,separator).replace(/^\[|\]$/g,''),port:Number(line.slice(separator+1))};
  });
  value.dns.enabled = form.elements.dns_enabled.checked;
  value.dns.names = form.elements.dns_names.value.split('\n').map(s => s.trim()).filter(Boolean);
  value.battery = {enabled:form.elements.battery_enabled.checked,
    bus:Number(form.elements.battery_bus.value), address:Number(form.elements.battery_address.value),
    low_voltage_v:Number(form.elements.battery_low_voltage.value),
    recover_voltage_v:Number(form.elements.battery_recover_voltage.value),
    mains_detection_enabled:form.elements.mains_detection_enabled.checked,
    mains_debounce_seconds:Number(form.elements.mains_debounce_seconds.value)};
  value.mqtt = {enabled:form.elements.mqtt_enabled.checked,
    host:form.elements.mqtt_host.value.trim(), port:Number(form.elements.mqtt_port.value),
    tls:form.elements.mqtt_tls.checked, username:form.elements.mqtt_username.value,
    topic_prefix:form.elements.mqtt_topic_prefix.value.trim(),
    discovery_prefix:form.elements.mqtt_discovery_prefix.value.trim(),
    publish_interval_seconds:Number(form.elements.mqtt_publish_interval.value),
    expire_after_seconds:Number(form.elements.mqtt_expire_after.value),
    sensors:[...document.querySelectorAll('[data-mqtt-sensor]')].filter(input => input.checked).map(input => input.dataset.mqttSensor),
    sensor_schema_version:config.mqtt.sensor_schema_version};
  if (form.elements.mqtt_password.value) value.mqtt.password = form.elements.mqtt_password.value;
  if (form.elements.mqtt_password_clear.checked) value.mqtt.password = '';
  for (const editor of document.querySelectorAll('[data-gateway]')) {
    const gateway = value.gateways[editor.dataset.gateway];
    for (const input of editor.querySelectorAll('[data-field]')) {
      if (input.type === 'checkbox') gateway[input.dataset.field] = input.checked;
      else if (input.value !== '') gateway[input.dataset.field] = input.type === 'number' ? Number(input.value) : input.value;
    }
    for (const clear of editor.querySelectorAll('[data-clear]')) if (clear.checked) gateway[clear.dataset.clear] = '';
  }
  return value;
}
form.addEventListener('submit', async event => {
  event.preventDefault(); const button = document.getElementById('save-settings'); button.disabled = true;
  try {
    const value = readConfig();
    const saved = await PW.request('/api/settings', {config:value,revision,acknowledge_heartbeat_disable:form.elements.ntfy_disable_ack.checked}, 'PUT');
    await load();
    const status = document.getElementById('save-status'); status.textContent = `Saved revision ${saved.revision}. Waiting for the monitor…`;
    PW.notice('Settings saved.', true);
    let tries = 0;
    const checkApplied = async () => {
      try {
        const report = await PW.request('/api/status');
        if (report.fresh && report.monitor?.settings_revision === saved.revision) {status.textContent = `Revision ${saved.revision} is active.`; return;}
      } catch (_) { /* Saved state remains valid even when status is unavailable. */ }
      if (++tries < 10) setTimeout(checkApplied, 2000);
      else status.textContent = `Revision ${saved.revision} saved. The monitor has not confirmed it yet.`;
    }; checkApplied();
  } catch (error) {PW.error(error);} finally {button.disabled = false;}
});
async function clearRecords(action, button, description) {
  if (!confirm(`Clear ${description}? This cannot be undone.`)) return;
  button.disabled = true;
  try {
    const result = await PW.runMaintenance(action);
    if (result) {
      const count = action === 'clear_deliveries' ? result.deliveries : result.incidents;
      PW.notice(`${count} ${action === 'clear_deliveries' ? 'delivery records' : 'incidents'} cleared.`, true);
    } else PW.notice('Clear request accepted. The watchdog will apply it shortly.', true);
  } catch (error) {
    PW.error(error);
  } finally {
    button.disabled = false;
  }
}
document.getElementById('clear-deliveries').onclick = event =>
  clearRecords('clear_deliveries', event.currentTarget, 'all completed and failed delivery records');
document.getElementById('clear-incidents').onclick = event =>
  clearRecords('clear_incidents', event.currentTarget, 'the full incident history');
load().catch(PW.error);

const search = document.getElementById('settings-search');
search.addEventListener('input', () => {
  const query = search.value.trim().toLowerCase();
  const results = document.getElementById('settings-results');
  const list = results.querySelector('ul'); list.replaceChildren(); results.hidden = !query;
  if (!query) return;
  const fields = [...form.querySelectorAll('input, select, textarea')].filter(field =>
    !field.disabled && `${PW.fieldLabel(field)} ${field.closest('.settings-section')?.querySelector('h2')?.textContent || ''}`.toLowerCase().includes(query));
  document.getElementById('search-count').textContent = `${fields.length} matching settings`;
  for (const field of fields.slice(0, 12)) {
    const item = PW.node('li');
    const link = PW.node('a', PW.fieldLabel(field)); link.href = `#${field.id}`;
    link.onclick = event => {event.preventDefault(); PW.focusField(field);};
    item.append(link); list.append(item);
  }
});
for (const link of document.querySelectorAll('.settings-subnav a')) {
  link.addEventListener('click', event => {
    event.preventDefault();
    history.replaceState(null, '', link.hash);
    document.querySelectorAll('.settings-subnav a').forEach(item => item.removeAttribute('aria-current'));
    link.setAttribute('aria-current', 'location');
    const heading = document.querySelector(link.hash + ' h2');
    if (heading) {
      heading.focus({preventScroll: true});
      document.querySelector(link.hash).scrollIntoView({block: 'start'});
    }
  });
}

document.getElementById('test-ntfy').onclick = async event => {
  const button = event.currentTarget;
  const status = document.getElementById('ntfy-test-status'); button.disabled = true;
  try {
    const result = await PW.request('/api/ntfy/test', {});
    status.textContent = 'Test queued. Waiting for the watchdog to attempt delivery.';
    for (let attempt = 0; attempt < 20; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      const maintenance = await PW.request('/api/maintenance/' + result.request_id);
      if (maintenance.status === 'failed') {status.textContent = 'The watchdog could not queue the test. Check the saved ntfy settings.'; return;}
      const report = await PW.request('/api/status');
      const job = report.ntfy_deliveries?.find(item => item.id === result.request_id + ':ntfy');
      if (job?.status === 'done') {status.textContent = 'VPS accepted the test. Please confirm receipt on your phone.'; return;}
      if (job?.status === 'failed') {status.textContent = `Test delivery failed: ${job.error || 'unknown error'}. See delivery history.`; return;}
    }
    status.textContent = 'Test remains queued or retrying. Check delivery status on the overview.';
  } catch (error) {PW.error(error);} finally {button.disabled = false;}
};
