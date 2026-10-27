const { chromium } = require('playwright');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const net = require('net');
const https = require('https');
const cp = require('child_process');

const root = path.resolve(__dirname, '..');
const temporary = fs.mkdtempSync('/tmp/codex-pwa-browser-');
let service;
let browser;

function freePort() {
  return new Promise(resolve => {
    const socket = net.createServer();
    socket.listen(0, '127.0.0.1', () => { const port = socket.address().port; socket.close(() => resolve(port)); });
  });
}
function get(url) {
  return new Promise((resolve, reject) => {
    const request = https.get(url, {rejectUnauthorized: false, family: 4}, response => {
      let body = ''; response.on('data', part => body += part); response.on('end', () => resolve(JSON.parse(body)));
    });
    request.on('error', reject); request.setTimeout(1000, () => request.destroy());
  });
}
async function authenticator(context, page, credentials = []) {
  const cdp = await context.newCDPSession(page);
  await cdp.send('WebAuthn.enable');
  const {authenticatorId} = await cdp.send('WebAuthn.addVirtualAuthenticator', {options: {
    protocol: 'ctap2', transport: 'internal', hasResidentKey: true,
    hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true,
  }});
  for (const credential of credentials) await cdp.send('WebAuthn.addCredential', {authenticatorId, credential});
  return {cdp, authenticatorId};
}

(async () => {
  const port = await freePort();
  const origin = `https://localhost:${port}`;
  cp.execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', path.join(temporary, 'key.pem'), '-out', path.join(temporary, 'cert.pem'), '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'], {stdio: 'ignore'});
  service = cp.spawn((process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python')), [path.join(__dirname, 'serve_admin_pwa_fixture.py'), temporary, String(port)], {cwd: root, stdio: 'ignore'});
  let fixture;
  for (let i = 0; i < 80; i++) {
    try { fixture = await get(origin + '/fixture/recovery-code'); break; } catch (_) { await new Promise(resolve => setTimeout(resolve, 100)); }
  }
  assert(fixture && fixture.code, 'isolated WebAuthn fixture must start');
  browser = await chromium.launch({headless: true, args: ['--ignore-certificate-errors']});
  let context = await browser.newContext({ignoreHTTPSErrors: true, viewport: {width: 390, height: 844}, isMobile: true, hasTouch: true});
  let page = await context.newPage();
  let auth = await authenticator(context, page);
  page.once('dialog', dialog => dialog.accept(fixture.code));
  await page.goto(origin + '/auth/passkey/recover');
  await page.locator('.fingerprint-action').click();
  await page.locator('#fixture-authenticated').waitFor();
  const {credentials} = await auth.cdp.send('WebAuthn.getCredentials', {authenticatorId: auth.authenticatorId});
  assert.strictEqual(credentials.length, 1, 'real resident Passkey was registered');
  const cookie = (await context.cookies()).find(value => value.name === '__Host-codex_admin_session');
  assert(cookie && cookie.secure && cookie.httpOnly && cookie.sameSite === 'Lax');
  assert(cookie.expires > Date.now() / 1000 + 89 * 86400, 'persistent cookie has the intended absolute lifetime');
  await page.locator('#fixture-draft').fill('切换应用后仍需保留的草稿');
  const storage = await context.storageState();
  await context.close();

  // Simulate cold launch by creating a new browser context with persisted state,
  // without an authenticator: a valid session must not request another ceremony.
  context = await browser.newContext({ignoreHTTPSErrors: true, storageState: storage, viewport: {width: 390, height: 844}, isMobile: true, hasTouch: true});
  page = await context.newPage();
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.goto(origin + '/login');
  await page.locator('#fixture-authenticated').waitFor();
  assert.strictEqual(await page.locator('#fixture-draft').inputValue(), '切换应用后仍需保留的草稿');
  const persistedCookie = (await context.cookies()).find(value => value.name === '__Host-codex_admin_session');
  assert.strictEqual(persistedCookie.value, cookie.value, 'cold launch reused rather than replaced the existing session');
  await page.goto(origin + '/conversation/cold-start?turn=fixture-turn');
  await page.locator('#fixture-authenticated').waitFor();
  await context.clearCookies();
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:auth-required')));
  await page.locator('#workbench-auth-recovery').waitFor({state: 'visible'});
  assert(page.url().includes('/conversation/cold-start'), 'auth recovery must not auto-navigate or discard the composer');
  assert.strictEqual(await page.locator('#fixture-draft').inputValue(), '切换应用后仍需保留的草稿');
  assert.strictEqual(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);

  // Storage failures must not silently lose text when the login link is clicked.
  await page.evaluate(() => {
    window.fixtureSetItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function () { throw new Error('fixture-storage-disabled'); };
  });
  page.once('dialog', dialog => dialog.dismiss());
  await page.locator('#workbench-auth-recovery a').click();
  assert(page.url().includes('/conversation/cold-start'));
  await page.evaluate(() => { Storage.prototype.setItem = window.fixtureSetItem; });

  auth = await authenticator(context, page, credentials);
  await page.locator('#workbench-auth-recovery a').click();
  await page.locator('.fingerprint-action').waitFor();
  await page.locator('.fingerprint-action').click();
  await page.locator('#fixture-authenticated').waitFor();
  assert(page.url().endsWith('/conversation/cold-start?turn=fixture-turn'), 're-authentication returned to the exact conversation and turn');
  assert.strictEqual(await page.locator('#fixture-draft').inputValue(), '切换应用后仍需保留的草稿');
  assert.deepStrictEqual(pageErrors, []);
  console.log('admin_pwa_playwright=passed real_webauthn=passed persistent_cold_start=passed safe_reauth=passed draft_retained=passed mobile_width=390');
  await context.close();
})().catch(error => { console.error(error.message); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  if (service && service.exitCode === null) { service.kill('SIGTERM'); await new Promise(resolve => service.once('exit', resolve)); }
  fs.rmSync(temporary, {recursive: true, force: true});
});
