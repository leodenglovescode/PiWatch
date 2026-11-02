'use strict';
const PW = {
  csrf: '',
  async request(path, data, method = 'POST') {
    await PW.ready;
    const response = await fetch(path, {
      method: data === undefined ? 'GET' : method,
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': PW.csrf},
      body: data === undefined ? undefined : JSON.stringify(data),
      signal: AbortSignal.timeout(20000), credentials: 'same-origin', cache: 'no-store'
    });
    const value = await response.json();
    if (value.csrf) PW.csrf = value.csrf;
    if (!response.ok) {
      if (response.status === 401 && !location.pathname.startsWith('/login') && !path.includes('/account')) location.assign('/login');
      throw new Error(value.error || `Request failed (${response.status})`);
    }
    return value;
  },
  notice(message, success = false) {
    const node = document.getElementById('notice');
    node.textContent = message;
    node.classList.toggle('success', success);
  },
  error(error) { if (error.message !== 'Cancelled') PW.notice(error.message || 'Request failed'); },
  node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  },
  badge(state) { return PW.node('span', state || 'UNKNOWN', `badge ${String(state || 'unknown').toLowerCase()}`); },
  age(seconds) {
    if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—';
    seconds = Math.max(0, Math.floor(seconds));
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
    return `${Math.floor(seconds / 86400)}d ${Math.floor(seconds % 86400 / 3600)}h`;
  },
  async runMaintenance(action) {
    const queued = await PW.request('/api/maintenance', {action});
    for (let attempt = 0; attempt < 20; attempt += 1) {
      await new Promise(resolve => setTimeout(resolve, 250));
      const status = await PW.request(`/api/maintenance/${queued.request_id}`);
      if (status.status === 'complete') return status.result;
      if (status.status === 'failed') throw new Error('The watchdog could not clear those records.');
    }
    return null;
  }
};
PW.ready = fetch('/api/session', {cache: 'no-store', credentials: 'same-origin'})
  .then(async response => {
    if (!response.ok) throw new Error('Could not connect to PiWatch');
    const value = await response.json(); PW.csrf = value.csrf; return value;
  });
PW.ready.catch(PW.error);
function applyTheme(preference) {
  const dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme = preference === 'system' ? (dark ? 'dark' : 'light') : preference;
  document.documentElement.dataset.themePreference = preference;
  localStorage.setItem('piwatch-theme', preference);
  const button = document.getElementById('theme-toggle');
  if (button) {
    const label = preference === 'system' ? 'System theme' : preference === 'dark' ? 'Dark theme' : 'Light theme';
    button.textContent = label;
    button.setAttribute('aria-label', `${label}. Activate to change theme.`);
  }
}
const themeMedia = window.matchMedia('(prefers-color-scheme: dark)');
themeMedia.addEventListener('change', () => {
  if (document.documentElement.dataset.themePreference === 'system') applyTheme('system');
});
document.getElementById('theme-toggle')?.addEventListener('click', () => {
  const current = document.documentElement.dataset.themePreference || 'system';
  applyTheme(current === 'system' ? 'dark' : current === 'dark' ? 'light' : 'system');
});
applyTheme(document.documentElement.dataset.themePreference || 'system');
document.getElementById('logout')?.addEventListener('click', async () => {
  try {await PW.request('/api/logout', {}); location.assign('/login');} catch (error) {PW.error(error);}
});
