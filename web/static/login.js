'use strict';
const loginForm = document.getElementById('login-form');
const factorForm = document.getElementById('factor-form');
let challenge;
PW.ready.then(value => {
  if (value.authenticated) location.replace('/');
  if (!value.initialized) {
    PW.notice('Create the owner account on the Pi before signing in. See the local deployment instructions.');
    loginForm.querySelector('button').disabled = true;
  }
}).catch(PW.error);
loginForm.addEventListener('submit', async event => {
  event.preventDefault(); const button = loginForm.querySelector('button'); button.disabled = true;
  try {
    const data = await PW.request('/api/login', {username: loginForm.elements.username.value, password: loginForm.elements.password.value});
    loginForm.elements.password.value = '';
    if (!data.mfa) return location.assign('/');
    loginForm.hidden = true; factorForm.hidden = false;
    for (const option of factorForm.elements.method.options) option.disabled = option.value === 'totp' ? !data.totp : option.value === 'email' ? !data.email : false;
    factorForm.elements.method.value = data.totp ? 'totp' : data.email ? 'email' : 'recovery';
    factorForm.elements.method.dispatchEvent(new Event('change'));
    factorForm.elements.code.focus(); PW.notice('');
  } catch (error) {PW.error(error);} finally {button.disabled = false;}
});
factorForm.elements.method.addEventListener('change', () => {
  document.getElementById('send-email').hidden = factorForm.elements.method.value !== 'email';
});
document.getElementById('send-email').addEventListener('click', async event => {
  event.target.disabled = true;
  try {challenge = (await PW.request('/api/login/email', {})).challenge; PW.notice('Code sent. Check your email.', true);}
  catch (error) {PW.error(error);} finally {event.target.disabled = false;}
});
factorForm.addEventListener('submit', async event => {
  event.preventDefault(); const button = factorForm.querySelector('button[type=submit]'); button.disabled = true;
  try {await PW.request('/api/login/verify', {method: factorForm.elements.method.value, code: factorForm.elements.code.value.trim(), challenge}); location.assign('/');}
  catch (error) {PW.error(error);} finally {button.disabled = false;}
});
