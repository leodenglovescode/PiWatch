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
  setThemeAttributes(preference);
  localStorage.setItem('piwatch-theme', preference);
  const button = document.getElementById('theme-toggle');
  if (button) {
    const label = preference === 'system' ? 'System theme' : preference === 'dark' ? 'Dark theme' : preference === 'glass' ? 'Liquid Glass theme' : 'Light theme';
    button.textContent = label;
    button.setAttribute('aria-label', `${label}. Activate to change theme.`);
  }
}
const themeMedia = window.matchMedia('(prefers-color-scheme: dark)');
themeMedia.addEventListener('change', () => {
  const preference = document.documentElement.dataset.themePreference;
  if (preference === 'system' || preference === 'glass') setThemeAttributes(preference);
});
document.getElementById('theme-toggle')?.addEventListener('click', () => {
  const current = document.documentElement.dataset.themePreference || 'light';
  const themes = ['system', 'dark', 'light', 'glass'];
  applyTheme(themes[(themes.indexOf(current) + 1) % themes.length]);
});
applyTheme(document.documentElement.dataset.themePreference || 'light');
document.getElementById('logout')?.addEventListener('click', async () => {
  try {await PW.request('/api/logout', {}); location.assign('/login');} catch (error) {PW.error(error);}
});

// Shared form behaviour: explicit labels, visible errors and keyboard recovery.
let fieldSequence = 0;
PW.enhanceForms = (root = document) => {
  for (const form of root.querySelectorAll('form')) form.noValidate = true;
  for (const field of root.querySelectorAll('input, select, textarea')) {
    if (!field.id) {
      const prefix = field.closest('form')?.id || 'control';
      field.id = `${prefix}-${field.name || field.dataset.field || 'field'}-${++fieldSequence}`;
    }
    const label = field.closest('label');
    if (label) label.htmlFor = field.id;
    if (!field.dataset.fieldLabel) field.dataset.fieldLabel = field.labels?.[0]?.textContent.trim() || field.name || 'This field';
  }
};
PW.fieldLabel = field => {
  const context = field.closest('.target-editor')?.querySelector('.target-summary-name')?.textContent
    || field.closest('[data-gateway]')?.querySelector('legend')?.textContent;
  const name = field.dataset.fieldLabel || field.name || 'This field';
  return context ? `${context}: ${name}` : name;
};
PW.focusField = field => {
  for (let parent = field.parentElement; parent; parent = parent.parentElement) {
    if (parent.tagName === 'DETAILS') parent.open = true;
  }
  field.focus({preventScroll: true});
  field.scrollIntoView({block: 'center'});
};
PW.clearErrors = () => {
  for (const field of document.querySelectorAll('[aria-invalid="true"]')) {
    field.removeAttribute('aria-invalid');
    const errorId = `${field.id}-error`;
    const description = (field.getAttribute('aria-describedby') || '').split(' ').filter(id => id && id !== errorId);
    if (description.length) field.setAttribute('aria-describedby', description.join(' '));
    else field.removeAttribute('aria-describedby');
  }
  document.querySelectorAll('.field-error').forEach(node => node.remove());
  const summary = document.getElementById('error-summary');
  if (summary) {summary.hidden = true; summary.querySelector('ul').replaceChildren();}
};
PW.showErrors = (errors, focus = true) => {
  PW.clearErrors();
  const summary = document.getElementById('error-summary');
  if (!summary) return PW.notice(errors[0].message);
  const list = summary.querySelector('ul');
  for (const {message, field} of errors) {
    const item = PW.node('li');
    if (field) {
      const note = PW.node('span', message, 'field-error'); note.id = `${field.id}-error`;
      (field.closest('label') || field).insertAdjacentElement('afterend', note);
      field.setAttribute('aria-invalid', 'true');
      field.setAttribute('aria-describedby', `${field.getAttribute('aria-describedby') || ''} ${note.id}`.trim());
      const link = PW.node('a', message); link.href = `#${field.id}`;
      link.onclick = event => {event.preventDefault(); PW.focusField(field);}; item.append(link);
    } else item.textContent = message;
    list.append(item);
  }
  summary.hidden = false; if (focus) summary.focus();
};
PW.fieldError = (field, message) => Object.assign(new Error(message), {field});
PW.error = (error, focus = true) => {
  if (error.message !== 'Cancelled') PW.showErrors([{message: error.message || 'Request failed', field: error.field}], focus);
};
const originalNotice = PW.notice;
PW.notice = (message, success = false) => {
  if (success) PW.clearErrors();
  originalNotice(message, success);
};
PW.enhanceForms();
document.addEventListener('submit', event => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  PW.enhanceForms(form);
  const errors = [...form.elements].filter(field => field.willValidate && !field.validity.valid)
    .map(field => ({field, message: `${PW.fieldLabel(field)}: ${field.validationMessage}`}));
  const password = form.elements.namedItem('password');
  const confirmation = form.elements.namedItem('confirm_password');
  if (password && confirmation && password.value !== confirmation.value) {
    errors.push({field: confirmation, message: 'Confirm new password: the passwords do not match.'});
  }
  if (errors.length) {event.preventDefault(); event.stopImmediatePropagation(); PW.showErrors(errors);}
  else PW.clearErrors();
}, true);
