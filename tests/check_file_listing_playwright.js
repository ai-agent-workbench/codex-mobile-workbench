/* Real route HTML; local synthetic pages, including no-JS navigation. */
const assert = require('assert');
const cp = require('child_process');
const fs = require('fs');
const http = require('http');
const path = require('path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const fixture = JSON.parse(cp.execFileSync((process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python')),
  [path.join(__dirname, 'render_file_listing_fixtures.py')], { cwd: root, encoding: 'utf8', maxBuffer: 8 * 1024 * 1024 }));
function key(address) { const url = new URL(address, 'http://fixture'); url.searchParams.sort(); return url.pathname + '?' + url.searchParams; }
const pages = new Map(Object.entries(fixture.pages).map(([address, body]) => [key(address), body]));
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname.startsWith('/static/')) {
    const file = path.resolve(root, '.' + url.pathname);
    if (file.startsWith(path.join(root, 'static') + path.sep) && fs.existsSync(file) && fs.statSync(file).isFile()) {
      res.writeHead(200, { 'content-type': file.endsWith('.css') ? 'text/css' : 'application/javascript' });
      return res.end(fs.readFileSync(file));
    }
  }
  const body = pages.get(key(req.url));
  res.writeHead(body ? 200 : 404, { 'content-type': 'text/html; charset=utf-8' }); res.end(body || 'fixture only');
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true });
  try {
    for (const width of [320, 360, 390, 430]) for (const javaScriptEnabled of [false, true]) {
      const context = await browser.newContext({ viewport: { width, height: 844 }, javaScriptEnabled });
      const page = await context.newPage();
      await page.goto(`http://127.0.0.1:${server.address().port}` + fixture.start);
      assert.equal(await page.locator('.file-entry').count(), 100);
      assert(await page.locator('.file-search').isVisible());
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'file page overflow at ' + width);
      const names = [];
      for (let index = 0; index < 6; index++) {
        names.push(...await page.locator('.file-entry .file-name').allTextContents());
        if (index < 5) await page.locator('a[rel=next]').click();
      }
      assert.equal(names.length, 505); assert.equal(new Set(names).size, 505);
      assert(names.at(-1).includes('文件-0504.txt'));
      assert.equal(await page.locator('a[rel=next]').count(), 0);
      await page.locator('a[rel=prev]').click();
      assert.equal(await page.locator('.file-entry').count(), 100);
      await page.locator('#file-search-query').fill('文件-0504');
      await page.locator('.file-search button').click();
      assert.equal(await page.locator('.file-entry').count(), 1);
      assert((await page.locator('.file-entry').textContent()).includes('文件-0504.txt'));
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
      if (width === 390 && javaScriptEnabled && process.env.FILE_LISTING_SCREENSHOT) {
        await page.screenshot({ path: process.env.FILE_LISTING_SCREENSHOT });
      }
      await context.close();
    }
    console.log('file listing real-route browser checks passed');
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
