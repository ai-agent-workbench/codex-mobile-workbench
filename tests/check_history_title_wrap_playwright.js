const { chromium } = require('playwright');
const childProcess = require('child_process');
const fs = require('fs');
const http = require('http');
const path = require('path');

const root = path.resolve(__dirname, '..');
const python = (process.env.CODEX_WEB_PYTHON || path.join(root, '.venv/bin/python'));
const titles = {
  short: '执行中的简短标题',
  complete: '已完成的对话',
  unread: '完成后尚未阅读的对话',
  pinned: '置顶的很长历史对话标题必须保持一行并且不会增加卡片高度',
  chinese: '这是一个用于验证历史对话标题在手机窄屏上统一为单行并在超出可用空间时显示省略号的中文标题内容',
  english: 'UnbrokenEnglishConversationTitleMustTruncateWithoutHorizontalOverflow'.repeat(5),
  failed: '执行失败后的超长标题仍保持紧凑固定高度'.repeat(4),
  stopped: '已停止后的超长标题仍保持紧凑固定高度'.repeat(4),
};
const cardHtml = JSON.parse(childProcess.execFileSync(python, ['-c', `
import json
from shared.conversation_views import conversation_cards
items = json.loads(${JSON.stringify(JSON.stringify(Object.entries(titles)))})
values = [{"id": key, "title": title, "state": "idle" if key in ("failed", "stopped", "complete", "unread") else "active", "project_id": "project-a",
           "pinned": key == "pinned", "last_terminal_status": "failed" if key == "failed" else "interrupted" if key == "stopped" else "completed" if key in ("complete", "unread") else None,
           "unread": key == "unread",
           "activity_at": "2026-09-19T12:00:00+08:00"}
          for key, title in items]
print(json.dumps(conversation_cards(values, "history-conversation-manage", show_project=True,
                                   project_names={"project-a": "项目名称很长时也不能遮挡状态信息"}, group_dates=False, visual_status=True)))
`], { cwd: root, encoding: 'utf8' }));

function assert(value, message) { if (!value) throw new Error(message); }
function pageHtml(managing) {
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/css/app.css"></head><body${managing ? ' class="managing"' : ''}><main><section class="history-page"><div class="history-list conversation-history"><section class="conversation-date-group"><h2>今天</h2><div class="conversation-date-cards">${cardHtml}</div></section></div></section></main></body></html>`;
}
const server = http.createServer((request, response) => {
  const address = new URL(request.url, 'http://fixture.test');
  if (address.pathname === '/history-title-wrap') {
    response.setHeader('Content-Type', 'text/html; charset=utf-8'); return response.end(pageHtml(address.searchParams.get('managing') === '1'));
  }
  if (address.pathname === '/static/css/app.css') {
    response.setHeader('Content-Type', 'text/css; charset=utf-8'); return fs.createReadStream(path.join(root, 'static/css/app.css')).pipe(response);
  }
  response.statusCode = 404; return response.end();
});

async function inspect(page) {
  return page.evaluate(() => Object.fromEntries([...document.querySelectorAll('.manage-card')].map(card => {
    const title = card.querySelector('.conversation-card-copy b');
    const meta = card.querySelector('.conversation-card-copy small');
    const status = card.querySelector('.conversation-card-status');
    const checkbox = card.querySelector('.manage-check');
    const titleRect = title.getBoundingClientRect();
    const metaRect = meta.getBoundingClientRect();
    const statusRect = status.getBoundingClientRect();
    const checkboxRect = checkbox.getBoundingClientRect();
    const style = getComputedStyle(title);
    const intersects = (a, b) => a.left < b.right - .5 && b.left < a.right - .5 && a.top < b.bottom - .5 && b.top < a.bottom - .5;
    const textRight = titleRect.right - parseFloat(style.paddingRight || '0');
    return [card.dataset.id, {
      card: {height: card.getBoundingClientRect().height},
      title: {height: titleRect.height, width: titleRect.width, clientWidth: title.clientWidth, scrollWidth: title.scrollWidth,
        whiteSpace: style.whiteSpace, textOverflow: style.textOverflow, lineClamp: style.webkitLineClamp, textRight, top: titleRect.top, bottom: titleRect.bottom},
      status: {top: statusRect.top, bottom: statusRect.bottom, left: statusRect.left, right: statusRect.right},
      meta: {top: metaRect.top, bottom: metaRect.bottom, left: metaRect.left, right: metaRect.right},
      checkbox: {displayed: getComputedStyle(checkbox).display !== 'none', left: checkboxRect.left, right: checkboxRect.right, top: checkboxRect.top, bottom: checkboxRect.bottom},
      titleStatusIntersect: intersects(titleRect, statusRect),
      metaStatusIntersect: intersects(metaRect, statusRect),
      titleCheckboxTextIntersect: checkboxRect.left < textRight - .5 && checkboxRect.right > titleRect.left + .5 && intersects(titleRect, checkboxRect),
      statusCheckboxIntersect: intersects(statusRect, checkboxRect),
    }];
  })));
}
function verify(metrics, width, managing, fontSize) {
  const prefix = `${width}px/${managing ? '管理模式' : '普通模式'}/${fontSize}px`;
  const heights = Object.values(metrics).map(value => value.card.height);
  assert(heights.every(height => Math.abs(height - 68) <= 1), `${prefix}: 卡片未保持68px紧凑高度 (${heights.join(',')})`);
  assert(Math.max(...heights) - Math.min(...heights) <= 1, `${prefix}: 置顶或状态使卡片高度不一致`);
  for (const [id, value] of Object.entries(metrics)) {
    assert(value.title.whiteSpace === 'nowrap' && value.title.textOverflow === 'ellipsis', `${prefix}/${id}: 标题未使用单行省略`);
    assert(value.title.lineClamp === 'none', `${prefix}/${id}: 标题仍使用多行截断`);
    assert(!value.titleStatusIntersect, `${prefix}/${id}: 标题与状态相交`);
    assert(!value.metaStatusIntersect, `${prefix}/${id}: 项目时间与状态相交`);
    if (managing) {
      assert(value.checkbox.displayed, `${prefix}/${id}: 未显示管理复选框`);
      assert(!value.titleCheckboxTextIntersect, `${prefix}/${id}: 复选框遮挡可见标题文字`);
      assert(!value.statusCheckboxIntersect, `${prefix}/${id}: 复选框遮挡状态`);
    }
  }
  for (const id of ['pinned', 'chinese', 'english', 'failed', 'stopped']) {
    assert(metrics[id].title.scrollWidth > metrics[id].title.clientWidth, `${prefix}/${id}: 超长标题未产生省略`);
  }
}

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true});
  try {
    for (const width of [320, 360, 393, 430]) for (const managing of [false, true]) for (const theme of ["dark", "light"]) {
      const context = await browser.newContext({viewport: {width, height: 852}});
      const page = await context.newPage();
      await page.goto(`${base}/history-title-wrap${managing ? '?managing=1' : ''}`, {waitUntil: 'networkidle'});
      await page.evaluate(value => document.documentElement.dataset.theme = value, theme);
      verify(await inspect(page), width, managing, 16);
      const colors = await page.evaluate(() => {
        function luminance(color) {
          const rgb = color.match(/[\d.]+/g).slice(0,3).map(v => {
            const s = Number(v)/255; return s <= .04045 ? s/12.92 : ((s+.055)/1.055)**2.4;
          });
          return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722;
        }
        return Object.fromEntries(['short','complete','unread'].map(id => {
          const card = document.querySelector('[data-id="'+id+'"]');
          const chip = card.querySelector('.conversation-card-status');
          const css = getComputedStyle(chip);
          const a = luminance(css.color), b = luminance(css.backgroundColor);
          return [id, {text:chip.textContent.trim(),bg:css.backgroundColor,cardBg:getComputedStyle(card).backgroundColor,
            contrast:(Math.max(a,b)+.05)/(Math.min(a,b)+.05),
            rail:getComputedStyle(card.querySelector('.conversation-state-rail')).backgroundColor}];
        }));
      });
      assert(colors.short.text === '执行中' && colors.complete.text === '已完成' && colors.unread.text === '完成未读', 'state labels are ambiguous');
      assert(colors.short.cardBg !== colors.complete.cardBg, 'running card fill is overridden by neutral base');
      assert(new Set(Object.values(colors).map(c => c.bg)).size === 3, 'running/completed/unread chip colors collide');
      assert(new Set(Object.values(colors).map(c => c.rail)).size === 3, 'running/completed/unread rails collide');
      assert(Object.values(colors).every(c => c.contrast >= 4.5), 'state text contrast below 4.5: '+JSON.stringify(colors));
      if (process.env.COMPACT_SCREENSHOTS && width === 393 && !managing) {
        fs.mkdirSync(process.env.COMPACT_SCREENSHOTS, {recursive:true});
        await page.screenshot({path:path.join(process.env.COMPACT_SCREENSHOTS, 'compact-'+theme+'.png'),fullPage:true});
      }
      await page.addStyleTag({content: '.conversation-history .conversation-card-copy b{font-size:22px}'});
      verify(await inspect(page), width, managing, 22);
      assert(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)), `${width}px: 页面出现横向滚动`);
      await context.close();
    }
    console.log('history title single-line Playwright passed');
  } finally { await browser.close(); server.close(); }
})().catch(error => { server.close(); console.error(error.stack || error); process.exit(1); });
