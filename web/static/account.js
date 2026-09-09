'use strict';
let account, enrollment;
const accountForm = document.getElementById('account-form');
async function loadAccount() {
  account = await PW.request('/api/account');
  accountForm.elements.username.value = account.username;
  accountForm.elements.idle_minutes.value = account.idle_minutes;
  document.getElementById('totp-status').textContent = account.totp ? 'Enabled' : 'Not enabled';
  document.getElementById('email-status').textContent = !account.smtp_ready ? 'Unavailable — configure SMTP first' : account.email ? 'Enabled' : 'Ready to set up';
  const totp = document.getElementById('totp-toggle'); totp.disabled = false; totp.textContent = account.totp ? 'Disable' : 'Set up';
  const email = document.getElementById('email-toggle'); email.disabled = !account.smtp_ready && !account.email; email.textContent = account.email ? 'Disable' : 'Set up';
  document.getElementById('recovery-status').textContent = account.totp || account.email ? `${account.recovery_remaining} recovery codes remaining.` : 'Set up a second factor to generate recovery codes.';
}
accountForm.addEventListener('submit', async event => {
  event.preventDefault(); const button = accountForm.querySelector('button'); button.disabled = true;
  try {
    if (accountForm.elements.password.value !== accountForm.elements.confirm_password.value) throw new Error('New passwords do not match.');
    const credentials = await PW.reauth();
    await PW.request('/api/account', {username:accountForm.elements.username.value, password:accountForm.elements.password.value, idle_minutes:Number(accountForm.elements.idle_minutes.value),...credentials});
    accountForm.elements.password.value = ''; accountForm.elements.confirm_password.value = '';
    await loadAccount(); PW.notice('Account updated. Other sessions were signed out.', true);
  } catch (error) {PW.error(error);} finally {button.disabled = false;}
});
for (const method of ['totp','email']) document.getElementById(method + '-toggle').addEventListener('click', async event => {
  event.target.disabled = true;
  try {
    const credentials = await PW.reauth();
    if (account[method]) {
      await PW.request('/api/account/factor/disable', {...credentials,method_to_disable:method});
      PW.notice('Verification method disabled. Other sessions were signed out.', true);
    } else {
      const data = await PW.request(`/api/account/${method}/start`, credentials);
      enrollment = {method,challenge:data.challenge};
      document.getElementById('enroll').hidden = false;
      document.getElementById('enroll-title').textContent = method === 'totp' ? 'Scan and verify' : 'Verify your email';
      document.getElementById('totp-setup').hidden = method !== 'totp';
      if (method === 'totp') {document.getElementById('totp-qr').src = data.qr; document.getElementById('totp-secret').value = data.secret;}
      PW.notice(method === 'totp' ? 'Scan the code in your authenticator app, then enter its six-digit code.' : 'Enter the eight-digit code sent to your configured SMTP recipient.', true);
      document.getElementById('confirm-factor').elements.code.focus();
    }
  } catch (error) {PW.error(error);} finally {await loadAccount().catch(PW.error);}
});
function clearEnrollment() {
  enrollment = undefined; document.getElementById('enroll').hidden = true;
  document.getElementById('totp-qr').removeAttribute('src'); document.getElementById('totp-secret').value = '';
  document.getElementById('confirm-factor').reset();
}
document.getElementById('cancel-enroll').onclick = clearEnrollment;
document.getElementById('confirm-factor').addEventListener('submit', async event => {
  event.preventDefault(); const button = event.target.querySelector('button'); button.disabled = true;
  try {
    const data = await PW.request(`/api/account/${enrollment.method}/confirm`, {challenge:enrollment.challenge,code:event.target.elements.code.value.trim()});
    clearEnrollment(); document.getElementById('recovery').hidden = false;
    document.getElementById('recovery-codes').textContent = data.recovery_codes.join('\n');
    await loadAccount(); PW.notice('Verification enabled. Save the recovery codes below.', true);
  } catch (error) {PW.error(error);} finally {button.disabled = false;}
});
document.getElementById('recovery-done').onclick = () => {document.getElementById('recovery-codes').textContent = ''; document.getElementById('recovery').hidden = true;};
loadAccount().catch(PW.error);
