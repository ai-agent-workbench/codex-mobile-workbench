/* Real route HTML and assets; submissions go only to this local HTTP double. */
const assert = require('assert');
const childProcess = require('child_process');
const fs = require('fs');
const http = require('http');
const path = require('path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const fixtures = JSON.parse(childProcess.execFileSync((process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python')),
  [path.join(__dirname, 'render_file_confirmation_fixtures.py')], { cwd: root, encoding: 'utf8' }));
const expected = {
  copy: ['/files/transfer/execute', '确认覆盖并执行'],
  move: ['/move/execute', '确认覆盖并移动'],
  upload: ['/files/upload/confirm', '确认覆盖并上传'],
  edit: ['/edit/write', '确认覆盖写入'],
};
let submitted = [];
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://fixture.invalid');
  if (req.method === 'POST') {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      submitted.push([url.pathname, new URLSearchParams(body)]);
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      res.end('<h1>fixture-confirmed</h1>');
    });
    return;
  }
  if (url.pathname.startsWith('/static/')) {
    const file = path.resolve(root, '.' + url.pathname);
    if (file.startsWith(path.join(root, 'static') + path.sep) && fs.existsSync(file) && fs.statSync(file).isFile()) {
      res.writeHead(200, { 'content-type': file.endsWith('.css') ? 'text/css' : 'application/javascript' });
      res.end(fs.readFileSync(file)); return;
    }
  }
  const content = fixtures[url.pathname.slice(1)];
  res.writeHead(content ? 200 : 404, { 'content-type': 'text/html; charset=utf-8' });
  res.end(content || 'fixture-only');
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true });
  try {
    let checks = 0;
    for (const width of [320, 390, 412]) for (const theme of ['light', 'dark']) {
      const context = await browser.newContext({ viewport: { width, height: 844 } });
      const page = await context.newPage();
      for (const key of Object.keys(fixtures)) {
        await page.goto(`http://127.0.0.1:${server.address().port}/${key}`);
        await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `${key}/${width}/${theme}: overflow`);
        if (key === 'duplicate') {
          assert(await page.getByText('本批上传包含重名文件', { exact: false }).isVisible());
          assert.strictEqual(await page.locator('form').count(), 0);
        } else {
          const [action, label] = expected[key];
          const button = page.getByRole('button', { name: label, exact: true });
          await button.scrollIntoViewIfNeeded();
          assert(await button.isVisible());
          await button.click();
          await page.getByText('fixture-confirmed').waitFor();
          const [actual, body] = submitted.at(-1);
          assert.strictEqual(actual, action);
          assert(body.get('token'));
        }
        checks++;
      }
      await context.close();
    }
    process.stdout.write(`file_confirmation_mobile_checks=${checks} passed\n`);
  } finally { await browser.close(); server.close(); }
})().catch(error => { server.close(); process.stderr.write(`${error.stack}\n`); process.exitCode = 1; });
