#!/usr/bin/env node
/* Isolated real-renderer benchmark. Every case keeps ALL 20/200/500 turns. */
const {chromium} = require('playwright');
const cp = require('child_process'), http = require('http'), fs = require('fs');
const path = require('path'), crypto = require('crypto');
const option = (name, fallback) => process.argv[process.argv.indexOf(name) + 1] || fallback;
const sourceDir = path.resolve(process.argv.includes('--source-dir') ? option('--source-dir') : process.cwd());
const runs = Number(process.argv.includes('--runs') ? option('--runs') : 8);
if (!Number.isInteger(runs) || runs < 2) throw Error('at least one warmup and one measured run required');
const counts = [20, 200, 500];
const rendered = JSON.parse(cp.execFileSync(path.join(sourceDir, '.venv/bin/python'),
  [path.join(sourceDir, 'tests/render_conversation_ui_fixtures.py')], {cwd: sourceDir, encoding: 'utf8'}));
const baseText = ('合成性能文本：仅用于隔离 DOM/Markdown 基线，不含生产会话内容。每一轮都保持固定长度，以验证尾部更新。').repeat(32).slice(0, 1024);
function makeSnapshot(count, revision, suffix = '') {
  const turns = Array.from({length: count}, (_, i) => {
    const n = i + 1, text = baseText + (n === count ? suffix : '');
    const turn = {id: 'turn-' + n, ordinal: n, status: 'completed', started_at: '2026-10-01T00:00:00Z',
      completed_at: '2026-10-01T00:01:00Z', final_text: text, error: null,
      commands: [{id: 'command-' + n, kind: n === 1 ? 'start' : 'messages', state: 'completed', text: '合成提问' + n, created_at: '2026-10-01T00:00:00Z'}],
      items: [{native_item_id: 'item-' + n, kind: 'agent_message', status: 'completed', phase: 'final_answer', text,
        created_at: '2026-10-01T00:01:00Z', updated_at: '2026-10-01T00:01:00Z'}], artifacts: []};
    // Match the additive public-projection content revision. The same fixture
    // (including this field) also runs against old source for a fair baseline.
    turn.presentation_revision = crypto.createHash('sha256').update(JSON.stringify(turn)).digest('hex');
    return turn;
  });
  return {schema_version: 4, conversation: {id: 'conversation-ui', thread_id: 'thread-1', title: '合成基线', state: 'idle', active_turn_id: null,
    thread_status: {type: 'idle'}, settings: {model: 'gpt-5.6-sol', effort: 'medium', permission: 'danger-full-access', service_tier: null},
    recovery_state: null, connection_state: 'connected', completion_notification: true, pinned: false, client_origin: 'mobile_web', revision,
    terminal_generation: 1, last_terminal_turn_id: 'turn-1', last_terminal_at: '2026-10-01T00:01:00Z', created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:01:00Z'},
    turns, messages: [], queue: [], artifacts: []};
}
const docs = Object.fromEntries(counts.map(n => [n, rendered.page_shell.replace('<!--fixture-content-->',
  rendered.conversation.replace(/(<script id="conversation-initial-snapshot" type="application\/json">)[\s\S]*?(<\/script>)/,
    '$1' + JSON.stringify(makeSnapshot(n, 1)) + '$2'))]));
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://fixture'), m = /^\/case\/(20|200|500)$/.exec(u.pathname);
  if (m) {res.writeHead(200, {'Content-Type': 'text/html;charset=utf-8', 'Cache-Control': 'no-store', 'Set-Cookie': `benchCount=${m[1]}; Path=/; SameSite=Lax`}); return res.end(docs[Number(m[1])]);}
  if (u.pathname.startsWith('/static/')) {
    const f = path.normalize(path.join(sourceDir, u.pathname));
    if (!f.startsWith(path.join(sourceDir, 'static') + path.sep) || !fs.existsSync(f)) {res.writeHead(404); return res.end();}
    res.writeHead(200, {'Content-Type': f.endsWith('.css') ? 'text/css' : f.endsWith('.js') ? 'application/javascript' : 'application/octet-stream'});
    return fs.createReadStream(f).pipe(res);
  }
  if (u.pathname.includes('/snapshot')) {
    const match = /(?:^|;\s*)benchCount=(20|200|500)(?:;|$)/.exec(String(req.headers.cookie || ''));
    if (!match) {res.writeHead(400); return res.end('missing exact benchCount');}
    res.writeHead(200, {'Content-Type': 'application/json'}); return res.end(JSON.stringify(makeSnapshot(Number(match[1]), 1)));
  }
  res.writeHead(204); res.end();
});
function stat(a) {const s = [...a].sort((a,b) => a-b), at = p => s[Math.min(s.length-1, Math.ceil(s.length*p)-1)]; return {median: +at(.5).toFixed(2), p95: +at(.95).toFixed(2), samples: a.map(n => +n.toFixed(2))};}
(async () => {
  let browser;
  try {
    await new Promise(r => server.listen(0, '127.0.0.1', r)); browser = await chromium.launch({headless: true});
    const results = [];
    for (const count of counts) {
      const metrics = {firstReadableDom: [], newestMarkdownReady: [], allMarkdownComplete: [], maxLongTask: [], tailSynchronousWork: [], tailUpdateTwoFrames: []};
      let preserved = true;
      for (let run = 0; run < runs; run++) {
        const page = await browser.newPage({viewport: {width: 390, height: 844}});
        await page.addInitScript(() => {
          window.__bench = {start: performance.now(), first: null, newest: null, syncTurns: null, long: []};
          new MutationObserver(() => {
            const bodies = document.querySelectorAll('.native-message.agent .native-message-body');
            if (bodies.length && window.__bench.first === null) window.__bench.first = performance.now();
            if (bodies.length && bodies[bodies.length-1].dataset.nativeEnhanced === '1' && window.__bench.newest === null) window.__bench.newest = performance.now();
          }).observe(document, {childList: true, subtree: true});
          new PerformanceObserver(list => {window.__bench.long.push(...list.getEntries().map(e => e.duration));}).observe({type: 'longtask', buffered: true});
          const native = window.fetch;
          window.fetch = function (...args) {return native.apply(this, args).then(response => {if (String(args[0]).includes('/snapshot')) response.clone().json().then(value => {window.__bench.syncTurns = value.turns?.length;}); return response;});};
          window.WebSocket = class {static OPEN=1; static CONNECTING=0; static CLOSING=2; constructor() {this.readyState=1;setTimeout(() => this.onopen?.(),0);} send(v) {if(v==='ping')setTimeout(() => this.onmessage?.({data:'{"type":"pong"}'}),0);} close() {this.readyState=3;}};
        });
        await page.goto(`http://127.0.0.1:${server.address().port}/case/${count}`, {waitUntil: 'domcontentloaded'});
        const first = await page.evaluate(async expected => {
          const deadline = performance.now() + 15000;
          while ((window.__bench.syncTurns !== expected || document.querySelectorAll('.native-message.agent .native-message-body[data-native-enhanced="1"]').length !== expected) && performance.now() < deadline) await new Promise(r => setTimeout(r, 5));
          const b = window.__bench, total = document.querySelectorAll('.native-message.agent .native-message-body[data-native-enhanced="1"]').length;
          await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
          return {total, syncTurns: b.syncTurns, first: b.first-b.start, newest: b.newest-b.start, complete: performance.now()-b.start, long: Math.max(0,...b.long)};
        }, count);
        if (first.total !== count || first.syncTurns !== count || !Number.isFinite(first.first)) throw Error('incomplete history: ' + JSON.stringify(first));
        const tail = await page.evaluate(async payload => {
          const first = document.querySelector('[data-turn-id="turn-1"]');
          const start = performance.now(); window.dispatchEvent(new CustomEvent('codex:conversation-action-result', {detail: payload}));
          const sync = performance.now() - start;
          await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
          const body = document.querySelector('[data-turn-id="turn-'+payload.turns.length+'"] .native-message-body');
          return {sync, total: performance.now()-start, count: document.querySelectorAll('.native-message.agent .native-message-body').length,
            suffix: body?.textContent.includes('[tail-update-suffix]'), same: first === document.querySelector('[data-turn-id="turn-1"]')};
        }, makeSnapshot(count, 2, ' [tail-update-suffix]'));
        if (tail.count !== count || !tail.suffix) throw Error('invalid tail: ' + JSON.stringify(tail));
        preserved &&= tail.same;
        if (run) {metrics.firstReadableDom.push(first.first); metrics.newestMarkdownReady.push(first.newest); metrics.allMarkdownComplete.push(first.complete); metrics.maxLongTask.push(first.long); metrics.tailSynchronousWork.push(tail.sync); metrics.tailUpdateTwoFrames.push(tail.total);}
        await page.close();
      }
      results.push({turns: count, ...Object.fromEntries(Object.entries(metrics).map(([k,v]) => [k,stat(v)])), initialAgentBodies: count, tailAgentBodies: count, firstHistoryNodePreserved: preserved});
    }
    console.log(JSON.stringify({sourceDir, runs, warmup: 1, measuredRuns: runs-1, viewport: '390x844', syntheticTextBytes: Buffer.byteLength(baseText), results}, null, 2));
  } finally {if(browser)await browser.close();server.closeAllConnections();await new Promise(r => server.close(r));}
})().catch(e => {console.error(e.stack||e);process.exitCode=1;});
