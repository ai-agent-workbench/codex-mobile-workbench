[English](#english) · [简体中文](#简体中文)

<a id="english"></a>

# I don't write code, but I built my own AI workbench.

### Codex Mobile Workbench

**Your computer does the work. Your phone directs it.**

Have an idea? Send a task from your phone. Follow up while it runs, add requirements, and change direction. When it's done, view and download the results right on your phone.

I didn't want to sit down at my computer every time I needed AI to do something. So, together with AI, I turned my own Codex into a workbench I can use wherever I am.

**Not another chat box: your own computer becomes the execution engine behind your phone.**

[Download the Windows/WSL preview](https://github.com/ai-agent-workbench/codex-mobile-workbench/releases) · [Get started](docs/QUICKSTART.en.md) · [See the workflow (中文)](docs/DEMO.md) · [MIT](LICENSE)

## Keep things moving away from your desk

### Have an idea? Put it to work.

A code change, some documents to organize, or a small project to finish. Open your phone and hand the task to Codex on your own computer, rather than waiting until you're back at your desk.

### Stay involved while it runs

Check progress, follow up within the same project, add files, refine requirements, and handle native interactions that need your decision. You don't have to explain the whole context again each time you open your phone.

### Take the results, not just an answer

Generated files have their own preview and download links. Take the finished documents, code, or other files with you, instead of just a screenshot of a chat.

<p align="center">
  <img src="docs/assets/workflow-demo.png" width="340" alt="Synthetic workflow: attach reading notes, start a task, preview and download an action list">
  <br><sub>A screenshot of the interactive synthetic demo, not a recording of a live installation. It uses public samples only, with no model connection or private data.</sub>
</p>

Want to try the interaction? Download the full source or installation package, then open [`docs/demo.html`](docs/demo.html) in a browser: add a sample → simulate a task → download a CSV → simulate returning to the original task. [Demo guide (中文)](docs/DEMO.md)

## Where it came from

This project started with “Can I get AI to actually do things for me?”, not “I want to build an AI product.”

I don't write code. I set the goals, make choices, and check the results; AI turns the requirements into an implementation. The project grew out of my everyday workbench on my own computer. Today, it brings native Codex tasks, projects, and file artifacts to my phone.

The path shared here has been checked with an independent instance installation, phone pairing, a real task, and a file download. The next goal is to help other people with the same need run it on their own computers.

**The code isn't the destination. Turning your own ideas into tools you can actually use is.**

## It connects to your own Codex

```text
Your phone / PWA
  ↓ Your own HTTPS entry point
An independent workbench instance on your computer
  ↓ Conversation Service / Runtime
Owner-only Unix socket
  ↓
Native Codex App Server → projects, tasks, and file artifacts
```

- **Your computer, account, and projects.** Model use goes through your own Codex account and configuration. No shared account or public model quota is provided.
- **Native execution, not another permission gate.** Phone authentication, native permissions/approvals, and operating-system permissions each retain their role. The old workbench's Windows PIN is not an additional execution requirement.
- **Independent installation and maintenance.** A lightweight Windows panel checks, installs, starts, stops, backs up, and restores a WSL instance to a new directory. It doesn't take over the author's production environment.
- **Optional features when you need them.** Persistent scheduled tasks and owner-configured completion notifications are disabled by default in new installations.

## Start here

1. Prepare **Windows + Ubuntu 24.04 WSL2, x86-64, and Python 3.12**.
2. Download the **complete Windows/WSL attachment package** from [Releases](https://github.com/ai-agent-workbench/codex-mobile-workbench/releases) and check its SHA-256. GitHub's automatically generated Source code ZIP does not include the panel EXE or installation manifest.
3. Follow the [quick start](docs/QUICKSTART.en.md) to create an independent instance, sign in, prepare your phone URL, pair the phone, and complete your first task.

Need a phone URL? The [phone access guide](docs/PHONE_ACCESS.en.md) explains private networking and hosted tunnel methods, including Tailscale Serve and Cloudflare Tunnel. Keep the computer running and the phone connected to your instance. Installation downloads pinned upstream dependencies; prepare WSL before using the panel.

The Windows panel currently uses Chinese labels. The English guides include those labels alongside English explanations so you can find the right controls.

## Current status

This is a **Windows/WSL technical preview**, currently **v0.1.0-preview.2**, using the complete pinned Codex 0.160.0 runtime kit.

The released setup is **a Windows panel with a WSL backend**, not a Windows-native backend. WSL was originally chosen to reduce rework caused by Windows command quoting, escaping, and path compatibility—not because Codex cannot run on Windows. Further work on a Windows-native release is paused; no installable Windows-native backend is offered. [Support matrix (中文)](docs/SUPPORT_MATRIX.md)

The installer offers three network choices: inherit the existing WSL configuration, connect directly, or specify a proxy. A failed configured proxy does not silently fall back to a direct connection. Earlier release attachments are unchanged; use the files attached to the version you choose. [Network choices and first use](docs/QUICKSTART.en.md) · [Phone access methods](docs/PHONE_ACCESS.en.md)

The maintainer has checked native sign-in, trusted HTTPS, physical-phone Passkey pairing, a real model task, and a file download on an independent instance. After upgrading to this network-fix preview, sign-in and pairing were retained, and the phone completed another task, obtained the correct file, and returned to the original conversation. Another computer, native Linux, full background/disconnection scenarios, and old public-version data import have not completed this release's real-device checks. See the [support matrix (中文)](docs/SUPPORT_MATRIX.md).

This is a high-privilege personal tool. New conversations currently default to **Full Access**; native tasks can use the runtime account's system permissions. Connect only your own account and devices, not a public multi-user AI service. [Security and private vulnerability reporting (中文)](SECURITY.md) · [Data and costs (中文)](docs/PRIVACY_AND_COSTS.md)

## Relationship to the older project

The earlier [AI Agent Workbench](https://github.com/ai-agent-workbench/ai-agent-workbench) retains its historical code and documentation. This is a new, independent project built around native Codex execution, not an in-place upgrade for the old CLI/Kimi version. Create a new instance for first use. [Compatibility details (中文)](docs/LEGACY_COMPATIBILITY.md)

## Learn more and contribute

- [Release notes (中文)](docs/RELEASE_NOTES.md) · [Maintenance and recovery (中文)](docs/OPERATIONS.md) · [Troubleshooting (中文)](docs/TROUBLESHOOTING.md)
- [Windows panel build (中文)](desktop/README.md) · [Tests (中文)](tests/README.md)
- [Feedback and contributions (中文)](CONTRIBUTING.md) · [Release checks (中文)](docs/RELEASE_CHECKLIST.md)
- Original project code is [MIT-licensed](LICENSE), with existing public attribution retained. Upstream Codex, protocol materials, and dependencies keep their own licenses; see [third-party notices (中文)](THIRD_PARTY_NOTICES.md).

Specific use cases and sanitized reproductions are welcome. No ongoing maintenance or response time is promised; keep your own backups. This is not an official OpenAI product and does not provide models, accounts, servers, or usage quota.

---

<a id="简体中文"></a>

# 我不会写代码，但我把自己的 AI 工作台做出来了。

### Codex Mobile Workbench

**电脑负责执行，手机负责指挥。**

有想法，掏出手机派任务。做到一半，随时补充要求、调整方向。做出成果，直接在手机上查看、下载。

我不想每次让 AI 干点事，都得先坐回电脑前。于是，我和 AI 一起，把自己的 Codex 变成了一个能随身操作的工作台。

**不是再造一个聊天框，而是把自己的电脑，变成手机背后的执行端。**

[下载 Windows／WSL 技术预览](https://github.com/ai-agent-workbench/codex-mobile-workbench/releases) · [开始使用](docs/QUICKSTART.md) · [看看工作流](docs/DEMO.md) · [MIT](LICENSE)

## 离开桌面，事情照样可以往前推进

### 想到了，就让它开工

一个代码改动、一份需要整理的资料、一个待完成的小项目。打开手机，把任务交给自己电脑上的 Codex，不必等到回到桌前才记起这件事。

### 做到一半，继续指挥

查看任务进展，围绕同一个项目追问、补充资料、调整要求，处理需要你拍板的原生交互。不是每次打开手机，都重新解释一遍上下文。

### 不只得到回答，还能拿到成果

任务生成的文件有自己的查看和下载入口。把处理好的文档、代码或其他文件带走，而不只是截下一段聊天记录。

<p align="center">
  <img src="docs/assets/workflow-demo.png" width="340" alt="合成工作流：关联读书笔记，发起任务，预览并下载行动清单">
  <br><sub>可交互合成演示的截图，非实机录屏；只使用公开样例，不连接模型或私人资料。</sub>
</p>

想亲手点一遍？完整下载源码或安装包后，在浏览器打开 [`docs/demo.html`](docs/demo.html)：添加样例 → 模拟任务 → 下载 CSV → 模拟返回原任务。[演示说明](docs/DEMO.md)

## 它是怎么来的

这个项目的起点，不是“我要做一款 AI 产品”，而是“我能不能让 AI 真正替我做事”。

我不写代码，负责提出目标、做选择、验收结果；AI 负责把需求变成实现。项目从自己电脑上的日常工作台一路演进，今天把原生 Codex 的任务、项目和文件成果接到了手机上。

这次开源的，是一条已经完成独立实例安装、手机绑定、真实任务和文件下载核心验证的路径。接下来，希望让更多有相同需求的人，能在自己的电脑上把它跑起来。

**代码不是终点。让自己的想法，变成真正能用的工具，才是。**

## 它连接的是你自己的 Codex

```text
你的手机／PWA
  ↓ 自己的 HTTPS 入口
你电脑上的独立工作台实例
  ↓ Conversation Service / Runtime
属主专用 Unix socket
  ↓
原生 Codex App Server → 项目、任务与文件成果
```

- **自己的电脑、账号和项目**：模型调用由自己的 Codex 账号与配置承担，不提供共享账号或公共模型额度。
- **原生执行，不另造一套裁判**：手机认证、原生权限／审批与操作系统权限各自负责，不沿用旧工作台的 Windows PIN 作为第二层执行门槛。
- **独立安装与维护**：Windows 薄面板编排 WSL 实例的检查、安装、启停、备份和新目录恢复，不接管作者的生产环境。
- **可选能力按需开启**：持久化定时任务与自己配置的完成通知，新安装默认关闭。

## 从这里开始

1. 准备 **Windows＋Ubuntu 24.04 WSL2、x86-64、Python 3.12**。
2. 到 [Releases](https://github.com/ai-agent-workbench/codex-mobile-workbench/releases) 下载**完整 Windows／WSL 附件包**，核对 SHA-256。GitHub 自动生成的 Source code ZIP 不包含面板 EXE 和安装清单。
3. 按 [快速开始](docs/QUICKSTART.md) 创建独立实例、本人登录、准备手机网址、绑定手机，再完成自己的第一个任务。

还没有手机网址？[手机网址说明](docs/PHONE_ACCESS.md)介绍了私有组网和托管入口两种办法，包括 Tailscale Serve、Cloudflare Tunnel。电脑需要保持运行，手机需要能连接该实例。安装会联网取得固定上游依赖；使用面板前，先准备好 WSL。

## 现在到哪一步了

这是 **Windows／WSL 技术预览**，当前版本为 **v0.1.0-preview.2**，固定 Codex 0.160.0 完整原生运行包。

已发布的是 **Windows 面板＋WSL 后端**，不是 Windows 原生后端。当初选择 WSL，是为了减少 Windows 命令引号、转义及路径兼容问题造成的返工；这是当时的工程取舍，不是 Windows 不能运行 Codex。Windows 原生版后续开发暂缓，尚未提供可安装的原生后端，见 [支持矩阵](docs/SUPPORT_MATRIX.md)。

本版安装器提供“沿用现有 WSL 配置／明确直连／指定代理”，不再一律要求代理；指定代理失败不会偷偷切换直连。首版附件保持不变，各版本行为以其固定附件为准。[网络选择与首次使用](docs/QUICKSTART.md) · [手机接入路线](docs/PHONE_ACCESS.md)。

维护者已经验证独立实例的原生登录、可信 HTTPS、实体手机 Passkey 绑定、真实模型任务和文件下载；本次联网修复版升级后保留登录与绑定，实体手机再次完成任务、取得正确文件并返回原对话。另一台电脑、原生 Linux、完整切后台／断网场景和旧公开版数据导入，尚未完成本次实机验收。详见 [支持矩阵](docs/SUPPORT_MATRIX.md)。

这是个人高权限工具：当前新对话默认 **Full Access**，原生任务可使用运行账户的系统权限。只连接自己的账号和设备；不要把它当作多人公共 AI 服务。[安全边界与私密漏洞报告](SECURITY.md) · [数据与费用](docs/PRIVACY_AND_COSTS.md)

## 和旧项目是什么关系

早期的 [AI Agent Workbench](https://github.com/ai-agent-workbench/ai-agent-workbench) 保留历史代码与文档。本项目是原生 Codex 执行链上的新一代独立项目，不是旧 CLI／Kimi 版本的覆盖升级包；首次使用应新建实例。[差异与兼容边界](docs/LEGACY_COMPATIBILITY.md)

## 深入了解与参与

- [版本说明](docs/RELEASE_NOTES.md) · [日常维护与恢复](docs/OPERATIONS.md) · [排障](docs/TROUBLESHOOTING.md)
- [Windows 面板构建](desktop/README.md) · [测试](tests/README.md)
- [反馈与贡献](CONTRIBUTING.md) · [发行核对](docs/RELEASE_CHECKLIST.md)
- 自有部分采用 [MIT](LICENSE)，保留原公开署名。上游 Codex、协议材料与依赖保持各自许可，见 [第三方声明](THIRD_PARTY_NOTICES.md)。

欢迎带着具体场景和脱敏复现来交流。项目不承诺持续维护或响应时限；请保留自己的资料备份。本项目不是 OpenAI 官方产品，不提供模型、账号、服务器或使用额度。
