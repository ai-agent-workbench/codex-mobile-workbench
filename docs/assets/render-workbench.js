#!/usr/bin/env node
/*
 * Generate public README artwork from real preview.4 Jinja fixtures and the
 * checked-in static CSS/JavaScript.  Every browser request is confined to the
 * loopback fixture server; no account, model, project directory or network
 * service is used.
 *
 * Run from the repository root (see docs/assets/README.md).
 */
'use strict';

const assert = require('node:assert/strict');
const childProcess = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..', '..');
const outputDir = __dirname;
const python = process.env.CODEX_WEB_PYTHON || 'python3';
const runPython = (file) => childProcess.execFileSync(python, [path.join(root, file)], {
  cwd: root, encoding: 'utf8', maxBuffer: 16 * 1024 * 1024,
});
const renderedConversation = JSON.parse(runPython('tests/render_conversation_ui_fixtures.py'));
const renderedWorkspace = JSON.parse(runPython('tests/render_workspace_mobile_ui_fixtures.py'));
const runPythonCode = (code) => childProcess.execFileSync(python, ['-c', code], {
  cwd: root, encoding: 'utf8', maxBuffer: 16 * 1024 * 1024,
});
const publicProjectFixture = JSON.parse(runPythonCode(String.raw`
import asyncio, json
from unittest import mock
import projects
from routes import projects as project_routes
from tests.render_conversation_ui_fixtures import MODEL_CATALOG
values = {'model':'gpt-6-sol','effort':'high','permission':'danger-full-access','service_tier':'priority','completion_notification':True,'skills':[],'collaboration_mode':'default','followup':'steer'}
store = mock.Mock(); store.resolve.return_value = {'values':values, 'sources':{'model':'account'}}
service = mock.Mock(); service.list_public_page.return_value = {'values':[], 'next_cursor':None, 'order_version':'fixture'}
service.catalog = mock.AsyncMock(return_value=MODEL_CATALOG)
with mock.patch.object(project_routes, 'require_auth', return_value=None), \
     mock.patch.object(project_routes, 'logged_in', return_value='browser'), \
     mock.patch.object(project_routes, 'conversation_service', return_value=service), \
     mock.patch.object(project_routes, 'settings_store', return_value=store), \
     mock.patch.object(projects, 'get', return_value={'id':'fixture','name':'公开合成项目','path':'/workspace/demo-project'}), \
     mock.patch.object(projects, 'upload_directory', return_value='/workspace/demo-project'):
    page = asyncio.run(project_routes.project_view(object(), 'fixture')).body.decode('utf-8')
print(json.dumps({'project':page, 'catalog':MODEL_CATALOG}))
`));

let syntheticSnapshot;
function withSyntheticResult(html) {
  const match = html.match(/(<script id="conversation-initial-snapshot"[^>]*>)([\s\S]*?)(<\/script>)/);
  assert(match, 'conversation fixture did not provide initial snapshot');
  const snapshot = JSON.parse(match[2]);
  const turn = {
    id: 'turn-public-demo', ordinal: 2, status: 'completed',
    started_at: '2026-11-01T09:20:00Z', completed_at: '2026-11-01T09:21:00Z',
    final_text: '已整理出可执行清单，并生成便于手机查看和下载的项目摘要。', error: null,
    commands: [{ id: 'command-public-demo', kind: 'start', state: 'completed',
      text: '整理本周项目进展，输出下一步行动清单和可下载摘要。', created_at: '2026-11-01T09:20:00Z' }],
    items: [{ native_item_id: 'message-public-demo', kind: 'agent_message', status: 'completed',
      phase: 'final_answer', text: '已整理出可执行清单，并生成便于手机查看和下载的项目摘要。',
      created_at: '2026-11-01T09:21:00Z', updated_at: '2026-11-01T09:21:00Z' }],
    artifacts: [{ id: 'artifact-public-demo', name: '项目行动清单.md', size: 1648,
      preview_kind: 'text', preview_text: '# 下一步\n\n- 核对目标与验收范围\n- 完成相关验证\n- 汇总可交付成果',
      download_url: '/fixture-download/project-action-list.md', content_url: '/fixture-content/project-action-list.md',
      created_at: '2026-11-01T09:21:00Z' }],
  };
  snapshot.turns = [turn]; snapshot.artifacts = []; syntheticSnapshot = snapshot;
  snapshot.conversation.title = '项目推进与成果交付';
  const updated = JSON.stringify(snapshot).replace(/</g, '\\u003c');
  return html.replace(match[0], match[1] + updated + match[3])
    .replaceAll('/home/workbench/fixture', '/workspace/demo-project');
}

const browserFixture = '<script>window.WebSocket=class extends EventTarget{constructor(){super();this.readyState=1;setTimeout(()=>this.dispatchEvent(new Event(\'open\')),0)}send(){}close(){this.readyState=3;this.dispatchEvent(new CloseEvent(\'close\'))}}</script>';
const withBrowserFixture = (html) => html.replace('<head>', '<head>' + browserFixture);
const publicModels = publicProjectFixture.catalog.models.map((row) => ({
  model: row.model, name: row.displayName, available: true,
  efforts: row.supportedReasoningEfforts.map((item) => item.reasoningEffort),
  default_effort: row.defaultReasoningEffort,
  tiers: (row.serviceTiers || []).map((tier) => ({id:tier.id, name:tier.name})),
}));
const settingsConfig = {
  account: {revision:0, defaults:{model:'gpt-6-sol', effort:'high', permission:'danger-full-access', service_tier:'priority', completion_notification:true, skills:[], collaboration_mode:'default', followup:'steer'}, personal:'', presets:[]},
  project: null,
  effective: {model:'gpt-6-sol', effort:'high', permission:'danger-full-access', service_tier:'priority', completion_notification:true, skills:[], collaboration_mode:'default', followup:'steer'},
  sources: {model:'account', effort:'account', permission:'account', service_tier:'account', completion_notification:'account', skills:'account', collaboration_mode:'account', followup:'account'},
  system: {model:'gpt-6-sol', effort:'medium', permission:'workspace-write', service_tier:null, completion_notification:true, skills:[], collaboration_mode:'default', followup:'steer'},
  models: publicModels, skills: [], catalog_error: false, projects: [{id:'fixture', name:'公开合成项目'}],
};
const publicProjectPage = publicProjectFixture.project.replace(
  /(<textarea name="prompt"[\s\S]*?>)(<\/textarea>)/,
  '$1整理本周项目进展，输出下一步行动清单和可下载摘要。$2',
);
const pages = {
  conversation: withBrowserFixture(renderedConversation.page_shell.replace('<!--fixture-content-->', withSyntheticResult(renderedConversation.conversation))),
  project: withBrowserFixture(publicProjectPage),
  settings: withBrowserFixture(renderedWorkspace.settings_admin),
};
const canvasCss = `
  :root{color-scheme:dark;font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
  *{box-sizing:border-box} body{margin:0;min-width:1200px;background:#0b111c;color:#edf4ff}
  .hero{display:flex;align-items:end;justify-content:space-between;gap:32px;padding:44px 58px 28px;background:radial-gradient(80% 160% at 100% 0,#1b4260 0%,#101c2c 52%,#0b111c 100%);border-bottom:1px solid #29394d}
  .eyebrow{margin:0 0 10px;color:#a8c9e5;font-size:14px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}.hero h1{margin:0;max-width:740px;font-size:46px;line-height:1.08;letter-spacing:-.045em}.hero p{max-width:620px;margin:13px 0 0;color:#b8c8d7;font-size:18px;line-height:1.55}.tags{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:9px;max-width:430px}.tag{padding:9px 12px;border:1px solid #42617d;border-radius:999px;background:#14283a;color:#d8edff;font-size:13px;white-space:nowrap}.screens{display:grid;grid-template-columns:repeat(3,1fr);gap:24px;padding:30px 58px 48px}.screen{overflow:hidden;border:1px solid #31445b;border-radius:22px;background:#0f1826;box-shadow:0 24px 52px rgba(0,0,0,.35)}.screen-head{display:flex;align-items:center;gap:8px;padding:13px 16px;border-bottom:1px solid #2a3d52;background:#142234;color:#cfdfef;font-size:14px;font-weight:650}.dot{width:8px;height:8px;border-radius:50%;background:#6cc6b7}.screen iframe{display:block;width:100%;height:700px;border:0;background:#111827}
  .result-layout{display:grid;grid-template-columns:minmax(440px,.95fr) minmax(520px,1.05fr);gap:30px;align-items:stretch;padding:32px 58px 50px}.result-copy{display:flex;flex-direction:column;justify-content:center;padding:26px 10px}.result-copy h1{margin:0;font-size:48px;line-height:1.08;letter-spacing:-.045em}.result-copy p{max-width:500px;color:#b8c8d7;font-size:18px;line-height:1.6}.result-points{display:grid;gap:12px;margin-top:18px}.result-points div{padding:13px 15px;border:1px solid #31445b;border-radius:15px;background:#101d2d;color:#d7e4ef;font-size:15px}.result-points b{color:#83d4c6}.result-screen{height:860px}.result-screen iframe{height:805px}
`;
function artPage(kind) {
  const overview = kind === 'overview';
  const title = overview ? '电脑执行，手机推进项目' : '把任务成果带在身边';
  const intro = overview
    ? '从项目任务、同一对话的跟进，到账户设置；图中均为合成示例，非真实任务记录。'
    : '在同一对话里追问、查看生成的项目文件并下载；合成示例，非真实任务记录。';
  const tags = overview
    ? ['项目优先', '连续对话', '文件成果', '合成示例 · 非真实任务记录']
    : ['真实模板', '合成任务', '文件预览／下载'];
  const screen = (label, route, extra = '') => `<section class="screen ${extra}"><div class="screen-head"><i class="dot"></i>${label}</div><iframe title="${label}" src="/${route}"></iframe></section>`;
  const body = overview
    ? `<div class="screens">${screen('项目：发起下一步', 'project')}${screen('对话：跟进进展与成果', 'conversation')}${screen('设置：管理自己的工作台', 'settings')}</div>`
    : `<div class="result-layout"><section class="result-copy"><p class="eyebrow">Codex Mobile Workbench · preview.4</p><h1>${title}</h1><p>${intro}</p><div class="result-points"><div><b>同一上下文</b>：继续补充任务，不必每次重讲背景。</div><div><b>可交付成果</b>：生成的文件有预览和下载入口。</div><div><b>自己的执行端</b>：手机是入口，电脑保持运行并执行。</div></div></section>${screen('对话与项目文件：公开合成示例', 'conversation', 'result-screen')}</div>`;
  const hero = overview ? `<header class="hero"><div><p class="eyebrow">Codex Mobile Workbench · preview.4</p><h1>${title}</h1><p>${intro}</p></div><div class="tags">${tags.map((tag) => `<span class="tag">${tag}</span>`).join('')}</div></header>` : '';
  return `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${title}</title><style>${canvasCss}</style><body>${hero}${body}</body></html>`;
}

function serveStatic(req, res) {
  const url = new URL(req.url, 'http://127.0.0.1');
  if (url.pathname === '/overview') return respond(res, 200, artPage('overview'), 'text/html; charset=utf-8');
  if (url.pathname === '/results') return respond(res, 200, artPage('results'), 'text/html; charset=utf-8');
  if (url.pathname === '/conversation' || url.pathname === '/project' || url.pathname === '/settings') return respond(res, 200, pages[url.pathname.slice(1)], 'text/html; charset=utf-8');
  if (url.pathname === '/conversation/conversation-ui/snapshot') return respond(res, 200, JSON.stringify(syntheticSnapshot), 'application/json; charset=utf-8');
  if (url.pathname === '/settings/api/config') return respond(res, 200, JSON.stringify(settingsConfig), 'application/json; charset=utf-8');
  if (url.pathname === '/sw.js') return respond(res, 200, '', 'application/javascript; charset=utf-8');
  if (url.pathname.startsWith('/fixture-')) return respond(res, 200, '公开合成示例', 'text/plain; charset=utf-8');
  if (url.pathname.startsWith('/static/')) {
    const requested = path.resolve(root, '.' + url.pathname);
    const staticRoot = path.join(root, 'static') + path.sep;
    if (!requested.startsWith(staticRoot)) return respond(res, 403, 'forbidden');
    try { return respond(res, 200, fs.readFileSync(requested), contentType(requested)); }
    catch { return respond(res, 404, 'not found'); }
  }
  return respond(res, 204, '');
}
function contentType(filename) {
  if (filename.endsWith('.css')) return 'text/css; charset=utf-8';
  if (filename.endsWith('.js')) return 'application/javascript; charset=utf-8';
  if (filename.endsWith('.png')) return 'image/png';
  if (filename.endsWith('.svg')) return 'image/svg+xml';
  return 'application/octet-stream';
}
function respond(res, status, body, type = 'text/plain; charset=utf-8') { res.writeHead(status, {'content-type': type, 'cache-control': 'no-store'}); res.end(body); }

(async () => {
  const requests = []; const blockedRequests = []; const consoleErrors = []; const pageErrors = [];
  const server = http.createServer(serveStatic);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  try {
    const context = await browser.newContext({ serviceWorkers: 'block', viewport: {width: 1600, height: 1080}, deviceScaleFactor: 1 });
    await context.route('**/*', (route) => {
      const requestUrl = new URL(route.request().url());
      if (requestUrl.protocol === 'http:' && requestUrl.hostname === '127.0.0.1' && requestUrl.port === String(server.address().port)) return route.continue();
      blockedRequests.push(requestUrl.href); return route.abort('blockedbyclient');
    });
    const page = await context.newPage();
    page.on('request', (request) => requests.push(request.url()));
    page.on('console', (message) => { if (message.type() === 'error') consoleErrors.push(message.text()); });
    page.on('pageerror', (error) => pageErrors.push(error.message));
    for (const [route, filename] of [['overview', 'workbench-overview.png'], ['results', 'workbench-task-result.png']]) {
      await page.goto(`${base}/${route}`, {waitUntil: 'networkidle'});
      if (route === 'overview') {
        await page.waitForFunction(() => {
          const frames = [...document.querySelectorAll('iframe')];
          const project = frames.find((frame) => frame.contentDocument?.location.pathname === '/project')?.contentDocument;
          const settings = frames.find((frame) => frame.contentDocument?.location.pathname === '/settings')?.contentDocument;
          return project?.querySelectorAll('#project-conversation-model option').length > 2
            && project?.querySelectorAll('#project-conversation-effort option').length >= 5
            && project?.querySelector('textarea[name="prompt"]')?.value.includes('行动清单')
            && settings?.querySelector('#sc-summary-models')?.textContent.includes('6-sol')
            && !settings?.querySelector('#sc-status')?.classList.contains('error')
            && !settings?.querySelector('#sc-status')?.textContent.trim();
        });
      }
      if (route === 'results') {
        const resultPosition = await page.evaluate(() => {
          const frame = document.querySelector('iframe[src="/conversation"]');
          const doc = frame?.contentDocument, win = frame?.contentWindow;
          const user = doc?.querySelector('.native-message.user');
          const artifact = doc?.querySelector('.native-artifacts');
          const head = doc?.querySelector('.native-conversation-head');
          if (!doc || !win || !user || !artifact || !head) return null;
          win.scrollTo(0, Math.max(0, user.getBoundingClientRect().top + win.scrollY - head.getBoundingClientRect().bottom - 8));
          return true;
        });
        assert.equal(resultPosition, true, 'result fixture did not render user message and artifact');
        await page.waitForTimeout(80);
        const resultVisible = await page.evaluate(() => {
          const frame = document.querySelector('iframe[src="/conversation"]');
          const doc = frame.contentDocument, win = frame.contentWindow;
          const user = doc.querySelector('.native-message.user').getBoundingClientRect();
          const artifact = doc.querySelector('.native-artifacts').getBoundingClientRect();
          const composer = doc.querySelector('.native-composer').getBoundingClientRect();
          const head = doc.querySelector('.native-conversation-head').getBoundingClientRect();
          return user.top >= head.bottom && user.bottom <= composer.top && artifact.top >= head.bottom && artifact.bottom <= composer.top;
        });
        assert.equal(resultVisible, true, 'result screenshot cuts off the user prompt, answer, or artifact');
      }
      const frameWidths = await page.evaluate(() => [...document.querySelectorAll('iframe')].map((frame) => {
        const doc = frame.contentDocument;
        const root = doc && doc.documentElement;
        return {path: doc && doc.location.pathname, scrollWidth: root && root.scrollWidth, clientWidth: root && root.clientWidth};
      }));
      assert(frameWidths.every(({scrollWidth, clientWidth}) => scrollWidth <= clientWidth + 2),
        `horizontal overflow: ${JSON.stringify(frameWidths)}`);
      await page.screenshot({path: path.join(outputDir, filename), fullPage: true});
    }
    assert.equal(consoleErrors.length, 0, `browser console errors: ${consoleErrors.join('; ')}`);
    assert.equal(pageErrors.length, 0, `browser page errors: ${pageErrors.join('; ')}`);
    assert.equal(blockedRequests.length, 0, `blocked non-loopback browser request: ${blockedRequests.join('; ')}`);
    assert(requests.every((url) => url.startsWith(base)), `non-loopback browser request: ${requests.find((url) => !url.startsWith(base))}`);
    await context.close();
    process.stdout.write(JSON.stringify({ok: true, files: ['workbench-overview.png', 'workbench-task-result.png'], requests: requests.length, externalRequests: 0, blockedRequests: 0, serviceWorkers: 'block'}) + '\n');
  } finally {
    await browser.close(); await new Promise((resolve) => server.close(resolve));
  }
})().catch((error) => { console.error(error.stack || error); process.exitCode = 1; });
