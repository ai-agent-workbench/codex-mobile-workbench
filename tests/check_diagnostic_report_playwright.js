/* Real localhost browser check for the isolated manual fault-report page. */
const assert = require('assert');
const child = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const state = fs.mkdtempSync('/tmp/codex-fault-report-');
const port = 39000 + Math.floor(Math.random() * 1000);
const origin = `http://127.0.0.1:${port}`;
const fixture = child.spawn((process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python')), [path.join(__dirname, 'serve_diagnostic_report_fixture.py'), state, port], {cwd: root, stdio: 'pipe'});
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
async function ready() { for (let i=0;i<80;i++) { try { if ((await fetch(origin+'/_fixture/reports')).ok) return; } catch (_) {} await wait(100); } throw new Error('fixture did not start'); }
async function count() { return (await (await fetch(origin+'/_fixture/reports')).json()); }
(async () => {
  let browser;
  try {
    await ready();
    browser = await chromium.launch({headless:true});
    const context = await browser.newContext({timezoneId:'Asia/Shanghai'});
    await context.addInitScript(()=>sessionStorage.setItem('codex-startup-v1',JSON.stringify({page_id:'fixture-clock-skew-page',path:'/home',phase:'hidden',ts:new Date(Date.now()+2*86400000).toISOString(),version:'startup-v1'})));
    const page = await context.newPage();
    await page.goto(origin + '/settings/diagnostics/report');
    const serverId = await page.locator('#report-id').inputValue();
    assert.match(serverId, /^[a-f0-9]{32}$/);
    // The browser displays local time; submit converts that exact wall time to UTC.
    const localTime = await page.locator('#fault-time').evaluate(input => {
      const d = new Date(Date.now() - 60 * 60 * 1000), z = n => String(n).padStart(2, '0');
      input.value = `${d.getFullYear()}-${z(d.getMonth()+1)}-${z(d.getDate())}T${z(d.getHours())}:${z(d.getMinutes())}`;
      return {wall: input.value, utc: new Date(input.value).toISOString().replace('.000Z', '.000Z')};
    });
    await page.locator('#fault-report').evaluate(form => form.requestSubmit());
    await page.locator('#submit-status').waitFor({state:'visible'});
    await page.waitForFunction(() => document.querySelector('#submit-status').textContent.includes('已保存'));
    let observed = await count();
    assert.equal(observed.count, 1); assert.equal(observed.reports[0].fault_time, localTime.utc);
    assert(new Date(observed.reports[0].client_time).getTime()>Date.now()+86400000,'optional skewed device clock was not retained independently of fault time');
    assert.equal(observed.reports[0].screenshot, false, 'untouched file input must be treated as no image');

    await page.goto(origin + '/settings/diagnostics/report');
    await page.locator('#screenshot').setInputFiles({name:'small.png', mimeType:'image/png', buffer:Buffer.from([137,80,78,71,13,10,26,10,1])});
    await page.locator('#fault-report').evaluate(form => form.requestSubmit());
    await page.waitForFunction(() => document.querySelector('#submit-status').textContent.includes('已保存'));
    observed = await count(); assert.equal(observed.count, 2); assert.ok(observed.reports.some(report => report.screenshot));

    const noJs = await browser.newContext({javaScriptEnabled:false, timezoneId:'Asia/Shanghai'});
    const plain = await noJs.newPage(); await plain.goto(origin + '/settings/diagnostics/report');
    const plainId = await plain.locator('#report-id').inputValue();
    await Promise.all([plain.waitForNavigation({waitUntil:'domcontentloaded'}), plain.getByRole('button',{name:'保存故障报告'}).click()]);
    assert.match(await plain.locator('#report-id').inputValue(), /^[a-f0-9]{32}$/);
    assert.match((await plain.locator('body').innerText()), /已保存/);
    observed = await count(); assert.equal(observed.count, 3); await noJs.close();

    // Let the actual request reach localhost, then replace its response with a
    // disposable gateway failure. The browser cannot confirm the save; a manual
    // retry must retain its id and must not create a second report.
    await page.goto(origin + '/settings/diagnostics/report');
    const retryId = await page.locator('#report-id').inputValue(); const before = (await count()).count;
    await page.route('**/settings/diagnostics/report', async route => { await route.fetch(); await route.fulfill({status: 502, contentType: 'text/plain', body: 'upstream response unavailable'}); });
    await page.locator('#fault-report').evaluate(form => form.requestSubmit());
    await page.waitForFunction(() => document.querySelector('#submit-status').textContent.includes('未确认'));
    await page.unroute('**/settings/diagnostics/report');
    assert.equal(await page.locator('#report-id').inputValue(), retryId);
    await page.locator('#fault-report').evaluate(form => form.requestSubmit());
    await page.waitForFunction(() => document.querySelector('#submit-status').textContent.includes('已保存'));
    assert.equal((await count()).count, before + 1);

    await page.goto(origin + '/settings/diagnostics/report'); const concurrentBefore = (await count()).count;
    await page.route('**/settings/diagnostics/report', async route => { await wait(250); await route.continue(); });
    await page.locator('#fault-report').evaluate(form => { form.requestSubmit(); form.requestSubmit(); });
    await page.waitForFunction(() => document.querySelector('#submit-status').textContent.includes('已保存'));
    await page.unroute('**/settings/diagnostics/report');
    assert.equal((await count()).count, concurrentBefore + 1, 'same tab must submit one in-flight report only');
    await context.close();
  } finally {
    if (browser) await browser.close();
    fixture.kill('SIGTERM'); fs.rmSync(state, {recursive:true, force:true});
  }
})().catch(error => { console.error(error); fixture.kill('SIGTERM'); fs.rmSync(state, {recursive:true, force:true}); process.exitCode=1; });
