# v0.1.0-preview.1 · 电脑执行，手机掌控

我不会写代码，但我和 AI 一起，把自己的需求做成了一个真实能用的工作台。现在，把这条路径整理成新一代开源项目：**Codex Mobile Workbench**。

有想法，用手机派任务；需要调整，继续追问；做出文件，直接查看和下载。电脑负责执行，手机负责指挥。

## 这次带来了什么

- 原生 Codex App Server 的项目／对话／任务交互链，不再沿用旧 CLI／Kimi 架构。
- 手机上传资料、跟进任务、处理原生交互，以及受本人认证保护的文件成果入口。
- Windows 原生薄面板和 WSL 独立实例：检查、安装、本人登录、手机接线、启停、备份及新目录恢复。
- 固定 0.160.0 完整原生运行包与成员校验，保留官方布局、资源和法律文本；不只检查单个 CLI 版本。
- 明确的文件交付成功／失败回执，以及独立于成果交付的标题保护提示。

## 下载时选哪个

选择 Release 附件中的 `Codex-Mobile-Workbench_Windows-WSL_v0.1.0-preview.1.zip`，并用同页 `SHA256SUMS.txt` 核对。

完整包包含源码、未签名 Windows 面板和 `package-source.json`，不包含原生 Codex 程序、Python wheels、模型、账号或用户资料；安装时联网取得固定上游依赖。GitHub 自动附带的 Source code ZIP／tar.gz 只有源码，不能替代完整包。

## 开始使用

目标环境是 Windows＋Ubuntu 24.04 WSL2、x86-64、Python 3.12。使用自己的电脑、Codex 账号和手机可达的可信 HTTPS；电脑需要保持运行。按仓库中的 `docs/QUICKSTART.md` 新建独立实例，不覆盖旧工作台。

## 已经验证到哪里

维护者的独立实例已完成原生登录、可信 HTTPS、实体手机绑定、真实模型任务和成果下载内容确认，以及本次维护重启后继续原对话。另一台电脑、原生 Linux、完整切后台／断网场景、旧公开 CLI／Kimi 版导入未在本次实机验收中覆盖；具体以 `docs/SUPPORT_MATRIX.md` 为准。

这是**技术预览，不是稳定版或从零一键部署器**。当前新对话默认 Full Access；使用前阅读 `SECURITY.md`，保护自己的账号、设备和资料。Windows 面板未签名，不要求绕过系统保护。

## 旧项目与反馈

旧 AI Agent Workbench 仓库保留历史，不删除或强推改写。本项目独立发布，不承诺旧配置、认证数据或服务可以原地迁移。

普通问题可提交脱敏 Issue；漏洞利用细节请通过本仓库 Security 页的 **Report a vulnerability** 私密报告。自有部分使用 MIT，原公开署名和上游 LICENSE／NOTICE 独立保留。个人项目，不承诺持续维护或响应时限。
