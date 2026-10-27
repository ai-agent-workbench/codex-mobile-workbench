# 验证入口与职责

测试数量不是验收指标。优先保护用户行为、真实事故与重要边界；不用源码
函数名、CSS 拼写或内部文件布局代替可观察行为。真实缺陷处理，等效重构
不应因无关实现文字而误报。安全/架构静态合同确无更合适替代时仍保留。

## 按影响选检查，不叠加固定套餐

```bash
bash scripts/run_tests.sh
bash scripts/run_tests.sh tests.test_conversation_actions -v
bash scripts/run_tests.sh discover -s tests -q --durations 20
bash scripts/run_tests.sh ui --list
bash scripts/run_tests.sh ui --dry-run core native
bash scripts/run_tests.sh ui core native
bash scripts/run_tests.sh ui all
```

- 默认入口保持 Python `unittest`；旧参数透传仍有效，定向导入与 discover
  使用相同的共享夹具路径。数量只描述本次实际收集范围，不代表手机实机通过。
- `ui` 必须显式选择分组；交集去重、串行执行、失败返回非零。分组定义在
  `scripts/run_ui_checks.py`，不维护第二份脚本清单。
- core：工作台与对话、紧凑控件、轻关闭、诊断；history：列表、排序、搜索；
  files：文件/附件/上传；settings：设置、模型选择；native：问题与 Plan/Goal
  操作；security：隔离 TLS/来源/登录；dot：保留的 dot 能力；automation：隔离
  定时任务夹具。共享前端变化扩大相关分组，局部变化只跑对应组。
- UI 使用本机隔离服务、合成数据和拦截网络，不调用真实模型、不操作生产
  数据。需要 Node/Playwright；部分组需 nginx/openssl。入口继承已配置环境，
  按 Node／Playwright 正常规则查找；也可显式设置 `NODE_PATH`、`PLAYWRIGHT_BROWSERS_PATH` 与 `CODEX_WEB_PYTHON`。不依赖作者 Tools 目录；缺依赖明确失败。
- `check_*real*.py` 和 App Server 实协议探针**不在默认或 `ui all` 中**；只在
  任务确需真实集成证据时明确执行，先核对费用、副作用与自有资源清理。

## 行为夹具与冷启动边界

`tests.store_fixtures.seeded_store(path)` 仅用于普通业务夹具的首次空库。
当前实现真实初始化一个空 SQLite 库，关闭/checkpoint 后缓存其字节；每例
独占新文件，再由真实 `ConversationStore` 打开。没有共享连接、共享业务
数据、构造函数替换、内存库或 WAL/FULL 降级；目标已存在则拒绝覆盖。

首次建库、schema/迁移、损坏恢复、旧版本/回滚、路径权限等测试继续直接
构造 `ConversationStore`。所有已有数据的 close/reopen 也直接构造，不能重新
播种覆盖。`test_conversation_store`、搜索 schema、Web Push extension、automation
schema、旧 Store 回滚等覆盖保留真实冷库；`test_store_fixtures` 保护独立副本、
schema 等价、真实重开和同步设置。

## 与审查配合

Web 卡顿留证专项：

```bash
bash scripts/run_tests.sh tests.test_web_stall_monitor tests.test_web_stall_lifecycle tests.test_diagnostics_read
```

包含临时目录/隔离子进程中的真实 asyncio 同步阻塞、正常异步等待不误报、
限频/恢复/观察缺口、帧隐私与容量边界、安全轮转、启动失败与正常退出及统一
只读 CLI。不得为验收故意卡住生产 Web、发真实模型 Turn 或增加自动恢复动作；
线程/GIL/整机冻结覆盖边界见维护仓库的日志取证说明。

开发自检与相关测试先完成，固定候选再按风险/明确要求安排独立审查。
同一候选的全量与只读审查可以并行；发现成组修复后定向验证、对应复审，
最终发布仍按项目规则跑全量和运行核验。成功且仍适用的结果才可复用。
不把工具可用变成每次修改必须审查，也不把 `ui all` 变成新的日常负担。

## 工作台维护准入闸（隔离候选）

`bash scripts/run_tests.sh tests.test_maintenance_admission -v`
覆盖新任务明确拒绝、已接受队列/定时派发自然排空、在途补充与原生续轮通知、
身份独立的本机控制凭据，以及真实隔离 Uvicorn 的 SIGTERM 正常退出。准入闸
不暂停原生 Goal；仍活动或投递未知时必须继续等或拒绝维护。

准入闸使用私有 side file，无常驻定时器或业务 Schema 升级。首次从旧版加载
须显式选择受控发布的 `--maintenance-admission-owner`，短事务安装限定的
SQLite INSERT 兼容保护，再自然排空；旧版这一小段新工作请求会明确失败，
已有任务、问询答复、通知、更新与正常退出不持有长 SQLite 写锁。实际加载
新协议并核验独立本机凭据、模块及整条准入实现的加载时指纹、数据库路径和空闲后移除兼容保护，
发布和生命周期验收成功后，核验同一实际实现与维护归属并重新开放准入；
本次加载不保持关闸去等待以后扩容，扩容控制器届时另行关闭。重新开放本身
不停止任何工作，不要求已经完成排空的任务再做一次空闲证明。失败只在已验证的原健康版本真正恢复
后重新开放，否则保留保护待核查；候选已标失败但既有回退尚在切换时，最多观察
60秒等待其指针和事务标记完成，再证明旧版本实际运行，不另发回退或重启。
不自动回写会话或重放新任务。

以上代码/夹具通过不等于正式加载授权；资源维护另核本次窗口和影响，不能
在当前工作台仍有在途任务时强行停机，也不能用旧版 SQL 写锁横跨服务退出。

旧版排空另外覆盖固定关闸时间下界与真实终态投递日志，证据缺失则拒停，
不把过期、未认领或历史 healthy 当作完成。新协议统计原生通知整个入口和
接收队列，涵盖终态已落库但仍在归档图像、尚未登记后台任务的间隙。维护
期间的补充票据绑定注册时的确切轮次；服务端拒绝即终结，不入队换轮重送。

原生排空还通过 `thread/loaded/list` + metadata-only `thread/read` + `thread/goal/get`
统计未投影到本地会话的子线程与 Goal，并用既有 `thread/backgroundTerminals/list`
统计空闲线程仍持有的后台终端；不清理终端、不 resume/interrupt/暂停 Goal，不读完整
历史正文。已收到请求的 callback 一直计数到响应实际发出；同步 local sample
放在原生只读扫描后，RPC 等待期间新增的本地工作不能遗漏。原生协议不支持、
连接变化/源漂移/清单不稳定均拒绝空闲证明，首次兼容加载另核原 Web/native PID。

排空不把旧版遗留的审查投影永久算作当前工作：只有父轮次已终结、创建/更新/
终结时间均严格早于已核验的当前 Web 进程起点，且无待清理或生命周期标记的
`code_review/inProgress` 才不再阻挡；记录原样保留。日期损坏、父轮次不明、
当前进程中的审查及显式待清理记录仍阻挡。持久的审查线程归属不是待完成标记，
但真实原生线程、后台任务与进程内审查继续独立核验；超时只记录非零类别/数量。

`bash scripts/run_tests.sh fixtures.maintenance_native_protocol` 额外使用 SHA 匹配
的已安装 0.160.0 原生程序及唯一临时 HOME/CODEX_HOME 验证双连接 loaded 清单：
模型 provider 限定合成本地地址，只建立一个空合成线程，不发模型轮次或触碰
生产会话。普通全量 unittest 不以这一显式原生诊断代替。

### 首次加载的独立调用（仅确认维护窗口后执行）


## PWA 启动与故障报告

`ui core` 包含真实浏览器的启动减负／推送专用 SW 升级／诊断队列和独立手动报告：
慢诊断及阅读偏好资源不能阻塞主体或交互，旧 SW 更新后恢复原生网络错误／取消，
旧导航队列仍可回放；存储超时、暂存／紧急尾记录的容量缺口必须可见。
后台隐藏后没有 pagehide 仅记“结束未观测”，不当作已证明崩溃。

手动报告使用自己的 localhost FastAPI、合成账号及图片，覆盖无截图／有图、
无 JavaScript、手机本地时间到 UTC、未知回执后的同 ID 手动重试及单飞。
`tests.test_diagnostic_reports` 覆盖私有存储、认证／来源、大小／容量、幂等／
归属与落盘失败；`tests.test_diagnostics_read` 的 `--report-id` 只读查单份报告
关联信息及故障时间前后各5分钟日志，不读截图或输出账号信息。
这些检查不向生产提交报告、投递通知、调用模型或证明实体 Android 不再崩溃。
