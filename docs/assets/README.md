# README 视觉素材来源

`workflow-demo.png` 保留为 [`docs/demo.html`](../demo.html) 的旧版可交互合成流程截图，供历史引用继续使用。它不是本次 README 的主视觉。

`workbench-overview.png` 和 `workbench-task-result.png` 是公开 **v0.1.0-preview.4** 的真实 Jinja 模板、项目静态 CSS/JavaScript 在本地夹具中的浏览器渲染。两张图仅使用合成项目、对话和文件：不是实际任务记录、实机录屏、账号、网络入口或私人资料。图中模型目录来自 `tests/render_conversation_ui_fixtures.py` 的完整公开 `MODELS` 夹具，只作界面示例；可用模型、推理档位和服务档位随账户和原生运行时变化。

## 复现

在仓库根目录运行（Python 路径按自己的虚拟环境调整）：

```bash
NODE_PATH=/path/to/node_modules \
PLAYWRIGHT_BROWSERS_PATH=/path/to/playwright-browsers \
CODEX_WEB_PYTHON=/path/to/python \
node docs/assets/render-workbench.js
```

脚本会从以下公开夹具渲染真实模板：

- `tests/render_conversation_ui_fixtures.py`：对话模板及完整公开 `MODEL_CATALOG`；
- 实际 `routes.projects.project_view`：以完整公开模型目录、合成项目名和合成任务输入渲染项目路由；
- `tests/render_workspace_mobile_ui_fixtures.py`：设置页面模板；设置 API 的合成配置在脚本进程内提供；
- 项目中的 `templates/`、`static/css/` 与 `static/*.js`。

运行时启动临时 HTTP 服务且只监听 `127.0.0.1`；浏览器上下文禁用 Service Worker，并以路由拦截拒绝一切非回环请求。脚本会断言项目模型数大于 2、所选模型推理档位至少 5 个、设置已无错误完成加载、页面无横向溢出、JavaScript 无控制台/页面错误以及 `externalRequests: 0`。不连接生产数据库、账号、真实项目目录、原生模型服务或外网；合成对话快照、设置配置和下载内容只在进程内提供。

不同 Chromium 版本和系统字体可能产生少量像素差异。当前素材使用 Chromium 的 Playwright 受管浏览器生成。
