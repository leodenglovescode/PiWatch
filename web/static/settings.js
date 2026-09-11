'use strict';
const form = document.getElementById('settings-form');
let config, revision;
const numeric = ['interval_seconds','external_interval_seconds','health_interval_seconds','suspect_after_failures','down_after_failures','recover_after_successes'];
const targetEditors = document.getElementById('target-editors');
function syncBatteryFields() {
  const enabled = form.elements.battery_enabled.checked;
  for (const input of ['battery_bus','battery_address','battery_low_voltage','battery_recover_voltage',
    'mains_detection_enabled','discharge_polarity','discharge_shunt_mv','mains_drop_v',
    'mains_window_seconds','mains_recover_rise_v']) {
    form.elements[input].disabled = !enabled;
  }
  const mainsEnabled = enabled && form.elements.mains_detection_enabled.checked;
  for (const input of ['discharge_polarity','discharge_shunt_mv','mains_drop_v',
    'mains_window_seconds','mains_recover_rise_v']) form.elements[input].disabled = !mainsEnabled;
}
form.elements.battery_enabled.addEventListener('change', syncBatteryFields);
form.elements.mains_detection_enabled.addEventListener('change', syncBatteryFields);
function targetEditor(target) {
  const editor = PW.node('div', undefined, 'target-editor'); editor.dataset.id = target.id;
  editor.innerHTML = `<div class="target-heading"><label class="check"><input data-field="enabled" type="checkbox">Enabled</label><button type="button" class="danger remove-target">Remove</button></div><div class="form-grid two"><label>Service name<input data-field="name" required maxlength="40"></label><label>IP address / hostname<input data-field="host" required maxlength="253"></label></div><div class="target-checks"><div><label class="check"><input type="checkbox" data-field="ping">Ping</label></div><div><label class="check"><input type="checkbox" data-field="tcp">TCP connection</label><label>TCP port<input type="number" min="1" max="65535" data-field="tcp_port"></label></div><div><label class="check"><input type="checkbox" data-field="http">HTTP response</label><label>HTTP port<input type="number" min="1" max="65535" data-field="http_port"></label><label>HTTP path<input data-field="http_path" maxlength="1024"></label></div></div>`;
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
  editor.querySelector('.remove-target').onclick = () => {if (confirm(`Remove ${input('name').value || 'this service'} from monitoring? Its history will be retained.`)) editor.remove();};
  targetEditors.append(editor);
}
const gatewayDefinitions = {
  lora: {title:'LoRa gateway', fields:[['url','API base URL','url'],['token','Bearer token','password']]},
  home_assistant: {title:'Home Assistant webhook', fields:[['url','Base URL','url'],['webhook_id','Webhook ID','password']]},
  smtp: {title:'Email (SMTP)', fields:[['host','SMTP host','text'],['port','Port','number'],['security','Connection security','select'],['username','SMTP username','text'],['password','SMTP password','password'],['sender','From address','email'],['recipient','Recipient address','email']]}
};
function gatewayEditors(secretsConfigured) {
  const container = document.getElementById('gateway-editors'); container.replaceChildren();
  for (const [name, definition] of Object.entries(gatewayDefinitions)) {
    const gateway = config.gateways[name]; const editor = PW.node('div', undefined, 'gateway-editor'); editor.dataset.gateway = name;
    editor.append(PW.node('h3', definition.title));
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
        const group = PW.node('div'); group.append(label, clearLabel); fields.append(group);
      } else fields.append(label);
    }
    editor.append(fields); container.append(editor);
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
  form.elements.discharge_polarity.value = config.battery.discharge_polarity;
  form.elements.discharge_shunt_mv.value = config.battery.discharge_shunt_mv.toFixed(2);
  form.elements.mains_drop_v.value = config.battery.mains_drop_v.toFixed(2);
  form.elements.mains_window_seconds.value = config.battery.mains_window_seconds;
  form.elements.mains_recover_rise_v.value = config.battery.mains_recover_rise_v.toFixed(2);
  syncBatteryFields();
  form.elements.internet_targets.value = config.internet.targets.map(t => `${t.host}:${t.port}`).join('\n');
  form.elements.dns_names.value = config.dns.names.join('\n');
  gatewayEditors(data.configured_secrets);
  document.getElementById('save-settings').disabled = false;
  document.getElementById('save-status').textContent = 'Changes are validated before they are applied.';
}
document.getElementById('add-target').onclick = () => {
  if (targetEditors.children.length >= 32) return PW.notice('Configure at most 32 monitored services.');
  targetEditor({id:'target_' + Date.now().toString(36) + Math.random().toString(36).slice(2,6),name:'',host:'',enabled:true,checks:[{type:'ping'}]});
  targetEditors.lastElementChild.querySelector('[data-field="name"]').focus();
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
    if (!checks.length) throw new Error('Select at least one check for each service.');
    return {id:editor.dataset.id,name:input('name').value.trim(),host:input('host').value.trim(),enabled:input('enabled').checked,checks};
  });
  value.internet.enabled = form.elements.internet_enabled.checked;
  value.internet.targets = form.elements.internet_targets.value.split('\n').map(s => s.trim()).filter(Boolean).map(line => {
    const separator = line.lastIndexOf(':'); if (separator < 1) throw new Error('Enter each Internet endpoint as IP:port.');
    return {host:line.slice(0,separator).replace(/^\[|\]$/g,''),port:Number(line.slice(separator+1))};
  });
  value.dns.enabled = form.elements.dns_enabled.checked;
  value.dns.names = form.elements.dns_names.value.split('\n').map(s => s.trim()).filter(Boolean);
  value.battery = {enabled:form.elements.battery_enabled.checked,
    bus:Number(form.elements.battery_bus.value), address:Number(form.elements.battery_address.value),
    low_voltage_v:Number(form.elements.battery_low_voltage.value),
    recover_voltage_v:Number(form.elements.battery_recover_voltage.value),
    mains_detection_enabled:form.elements.mains_detection_enabled.checked,
    discharge_polarity:form.elements.discharge_polarity.value,
    discharge_shunt_mv:Number(form.elements.discharge_shunt_mv.value),
    mains_drop_v:Number(form.elements.mains_drop_v.value),
    mains_window_seconds:Number(form.elements.mains_window_seconds.value),
    mains_recover_rise_v:Number(form.elements.mains_recover_rise_v.value)};
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
    const value = readConfig(); const credentials = await PW.reauth();
    const saved = await PW.request('/api/settings', {config:value,revision,...credentials}, 'PUT');
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
