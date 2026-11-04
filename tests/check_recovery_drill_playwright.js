// Real routes/runtime/auth and backup/restore, with a synthetic UDS native peer.
// No production state, external requests, real credentials or paid model calls.
const {chromium} = require('playwright');
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const net = require('net');
const cp = require('child_process');
const readline = require('readline');
const crypto = require('crypto');
// Match the production user units, independently of the CI shell's umask.
process.umask(0o077);
const root = path.resolve(__dirname, '..');
const python = process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python');
const temporary = fs.mkdtempSync(path.join(os.homedir(), '.recovery-drill-'));
const env = {PATH: '/usr/bin:/bin', HOME: temporary, PYTHONDONTWRITEBYTECODE: '1'};
const draft = '恢复后必须保留的合成草稿';
const digest = value => crypto.createHash('sha256').update(value).digest('hex');
const expected = digest('recovery-drill-artifact-v1\n');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let driver, native, web, browser, context, command;
const children = [];
function spawn(file, args) {
  const child = cp.spawn(python, [path.join(__dirname, 'fixtures', file), ...args],
    {cwd: root, stdio: ['pipe', 'pipe', 'pipe'], env});
  child.stderr.on('data', () => {}); // Never expose registration material in output.
  child.on('error', () => {});
  children.push(child);
  return child;
}
async function until(check, label, timeout = 15000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await check()) return;
    await sleep(50);
  }
  throw Error('timeout: ' + label);
}
async function stop(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  child.kill('SIGTERM');
  try { await until(() => child.exitCode !== null || child.signalCode !== null, 'child stopped', 5000); }
  catch (_) {
    child.kill('SIGKILL');
    await until(() => child.exitCode !== null || child.signalCode !== null, 'child killed', 5000);
  }
}
function controller(child) {
  const lines = readline.createInterface({input: child.stdout});
  let pending;
  const fail = error => { if (pending) { clearTimeout(pending.timer); pending.reject(error); pending = null; } };
  lines.on('line', line => {
    if (!pending) return;
    const current = pending; pending = null; clearTimeout(current.timer);
    try { const value = JSON.parse(line); value.ok ? current.resolve(value) : current.reject(Error([value.operation, value.error, value.code].filter(Boolean).join(': '))); }
    catch (error) { current.reject(error); }
  });
  child.on('exit', () => fail(Error('controller exited')));
  child.on('error', fail);
  return value => new Promise((resolve, reject) => {
    assert(!pending, 'controller operations are serial');
    pending = {resolve, reject, timer: setTimeout(() => fail(Error('controller timeout')), value.op === 'shutdown' ? 5000 : 30000)};
    child.stdin.write(JSON.stringify(value) + '\n', error => { if (error) fail(error); });
  });
}
function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer(); server.on('error', reject);
    server.listen(0, '127.0.0.1', () => { const port = server.address().port; server.close(() => resolve(port)); });
  });
}
async function webStart(instance, port, socket) {
  cp.execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes',
    '-keyout', path.join(instance, 'fixture-key.pem'), '-out', path.join(instance, 'fixture-cert.pem'),
    '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
    {stdio: 'ignore', timeout: 10000});
  web = spawn('recovery_drill_fixture.py', [instance, String(port), socket]);
  await until(async () => {
    assert(web.exitCode === null && web.signalCode === null, 'fixture web exited');
    try { return (await context.request.get(`https://localhost:${port}/login/status`, {timeout: 500})).ok(); }
    catch (_) { return false; }
  }, 'fixture readiness');
}
async function register(page, origin) {
  const cdp = await context.newCDPSession(page);
  await cdp.send('WebAuthn.enable');
  const {authenticatorId} = await cdp.send('WebAuthn.addVirtualAuthenticator', {options: {
    protocol: 'ctap2', transport: 'internal', hasResidentKey: true,
    hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true,
  }});
  const response = await context.request.get(origin + '/fixture/recovery-code');
  const {code} = await response.json();
  page.once('dialog', dialog => dialog.accept(code));
  await page.goto(origin + '/auth/passkey/recover');
  await page.locator('.fingerprint-action').click();
  await until(async () => (await (await context.request.get(origin + '/login/status')).json()).authenticated,
    'normal WebAuthn registration');
  const {credentials} = await cdp.send('WebAuthn.getCredentials', {authenticatorId});
  assert.strictEqual(credentials.length, 1);
  return cdp;
}
async function download(page) {
  const link = page.locator('a[href*="/artifact/"]').first();
  await link.waitFor();
  const href = await link.getAttribute('href');
  const pending = page.waitForEvent('download');
  await link.click();
  const result = await pending;
  assert.strictEqual(await result.failure(), null);
  assert.strictEqual(digest(fs.readFileSync(await result.path())), expected);
  return href;
}
const outside = [], pageErrors = [];
async function newContext(origin, storageState) {
  const value = await browser.newContext({ignoreHTTPSErrors: true, storageState,
    viewport: {width: 390, height: 844}, isMobile: true, hasTouch: true});
  value.setDefaultTimeout(15000);
  await value.route('**/*', route => {
    if (new URL(route.request().url()).origin === origin) return route.continue();
    outside.push(new URL(route.request().url()).origin); return route.abort();
  });
  await value.routeWebSocket('**/*', ws => {
    if (new URL(ws.url()).origin === origin.replace('https:', 'wss:')) ws.connectToServer();
    else { outside.push(new URL(ws.url()).origin); ws.close(); }
  });
  value.on('page', page => page.on('pageerror', error => pageErrors.push(error.message)));
  return value;
}
(async () => {
  driver = spawn('recovery_drill_recovery.py', []);
  command = controller(driver);
  const initial = await command({op: 'init'});
  assert(path.resolve(initial.root).startsWith(temporary + path.sep), 'driver root belongs to this run');
  const port = await freePort(), origin = `https://localhost:${port}`;
  const socket = path.join(path.dirname(initial.root), 'native.sock');
  const counts = path.join(path.dirname(initial.root), 'native-counts.json');
  native = spawn('recovery_drill_native.py', [socket, counts]);
  await until(() => fs.existsSync(socket), 'native UDS');
  browser = await chromium.launch({headless: true, args: ['--ignore-certificate-errors']});
  context = await newContext(origin);
  await webStart(initial.root, port, socket);
  let page = await context.newPage();
  const cdp = await register(page, origin);
  let frames = 0, closed = 0;
  page.on('websocket', ws => { ws.on('framereceived', () => frames++); ws.on('close', () => closed++); });
  await page.goto(origin + '/fixture/seed');
  assert(page.url().includes('/conversation/'), 'seed follows the real completed native turn');
  const conversation = page.url();
  await until(() => frames > 0, 'initial real WebSocket snapshot');
  const artifact = await download(page);
  const composer = page.locator('textarea[aria-label="输入消息"]');
  await composer.fill(draft);
  await until(async () => JSON.stringify(await context.storageState()).includes(draft), 'draft persisted');
  await context.setOffline(true);
  assert.strictEqual(await page.evaluate(() => navigator.onLine), false);
  await cdp.send('Page.setWebLifecycleState', {state: 'frozen'});
  await sleep(150);
  await cdp.send('Page.setWebLifecycleState', {state: 'active'});
  await context.setOffline(false);
  assert.strictEqual(await page.evaluate(() => navigator.onLine), true);
  assert.strictEqual(await composer.inputValue(), draft);
  const beforeClose = closed;
  await stop(web);
  await until(() => closed > beforeClose, 'socket observes real Web process exit');
  const beforeRestart = frames;
  await webStart(initial.root, port, socket);
  await until(() => frames > beforeRestart, 'same-page reconnect after same-root Web restart', 25000);
  assert.strictEqual(await composer.inputValue(), draft);
  await download(page);
  assert.strictEqual(JSON.parse(fs.readFileSync(counts))['turn/start'], 1);
  const storage = await context.storageState();
  await stop(web);
  const restored = await command({op: 'backup_restore'});
  assert.notStrictEqual(restored.root, initial.root);
  assert(path.resolve(restored.root).startsWith(temporary + path.sep));
  assert.deepStrictEqual(restored.delivery_unknown, ['queued-drill', 'sending-drill']);
  assert.strictEqual(restored.sessions, 0);
  assert.strictEqual(restored.source_unchanged, true);
  assert.strictEqual(restored.native_auth_excluded, true);
  assert(restored.credentials_revoked >= 1);
  assert.strictEqual(restored.commands_replayed, false);
  assert.strictEqual(restored.activated, false);
  await context.close();
  context = await newContext(origin, storage);
  await webStart(restored.root, port, socket);
  assert.strictEqual((await (await context.request.get(origin + '/login/status')).json()).authenticated, false,
    'source session must not authenticate the restored instance');
  for (const url of [conversation, new URL(artifact, origin).href]) {
    const response = await context.request.get(url, {maxRedirects: 0});
    assert([302, 303, 401, 403].includes(response.status()), 'old session denied protected content');
  }
  page = await context.newPage();
  await register(page, origin);
  await page.goto(conversation);
  assert.strictEqual(await page.locator('textarea[aria-label="输入消息"]').inputValue(), draft);
  await download(page);
  assert.strictEqual(JSON.parse(fs.readFileSync(counts))['turn/start'], 1);
  assert.deepStrictEqual(outside, []);
  assert.deepStrictEqual(pageErrors, []);
  console.log('recovery_drill=passed offline_lifecycle=true same_root_restart=true new_root_restore=true old_session_denied=true fresh_passkey=true artifact_sha256_before_after=true unknown_commands=2 no_replay=true');
  await context.close(); context = null;
  await stop(web); await stop(native);
  await command({op: 'shutdown'}); command = null;
})().catch(error => { console.error(error.stack); process.exitCode = 1; }).finally(async () => {
  if (context) await context.close();
  if (browser) await browser.close();
  await stop(web); await stop(native);
  if (command && driver.exitCode === null && driver.signalCode === null) {
    try { await command({op: 'shutdown'}); } catch (_) { /* bounded PID cleanup follows */ }
  }
  for (const child of children.reverse()) await stop(child);
  // Read-only controller directories are intentional. Only our own stopped
  // fixture tree is made removable; never follow links outside that tree.
  function makeRemovable(directory) {
    const info = fs.lstatSync(directory);
    if (!info.isDirectory() || info.isSymbolicLink()) return;
    fs.chmodSync(directory, 0o700);
    for (const name of fs.readdirSync(directory)) makeRemovable(path.join(directory, name));
  }
  makeRemovable(temporary);
  fs.rmSync(temporary, {recursive: true, force: true});
});
