# Installation and Maintenance Guide

[返回中文版 / Chinese version](INSTALL.md)

For a first-time installation, follow the [Quick Start](docs/QUICKSTART.en.md). This page keeps the installation environment, network, and maintenance details; you do not need to read it all before first use.

## Installation environment

- Windows + Ubuntu 24.04 WSL2, x86-64.
- Python 3.12, including venv/ensurepip; Git, curl, and a working systemd user service are also required.
- A network that can download dependencies and reach the model service. A proxy is not always required; see the rules below.

The panel checks these requirements, but it does not install WSL for you or automatically elevate privileges to install system components. Use a new directory of your own; do not take over an existing workbench. Older public CLI/Kimi editions cannot be upgraded in place; see the [compatibility notes (Chinese)](docs/LEGACY_COMPATIBILITY.md).

## Normal use: begin from the local window

After downloading the complete Windows/WSL package, use the [Quick Start](docs/QUICKSTART.en.md) to install, sign in, start, bind your phone, and run your first task. The GitHub-generated Source code ZIP does not include the panel EXE or installation manifest.

You can obtain a phone URL through a private network or a hosted entry point; see [Phone access URL](docs/PHONE_ACCESS.en.md). After obtaining a stable HTTPS URL, enter and save it in the panel, click “启动” (Start), then open it on your phone to confirm.

## Program installation and network details

Installation uses the fixed official Codex 0.160.0 Linux x86-64 musl complete runtime package, retaining `bin/codex`, the tool host, accompanying resources, and their NOTICE/licenses. It verifies each item’s type, size, SHA-256, and permissions, not only the CLI version. Keeping resources from the official package does not enable additional features; Python dependencies are installed from PyPI into a new venv according to `requirements-core.lock`. If the network is unavailable, the panel reports the download stage and selected method; it does not automatically change routes or copy a production venv.

### Network methods

- **Use the existing WSL configuration (default):** use `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`, and their lowercase equivalents from the WSL process invoked by the panel. If cases conflict, lowercase takes precedence. You may continue with no proxy variables; the panel does not automatically read the Windows system proxy or terminal startup scripts.
- **Explicit direct connection:** downloads, native sign-in, and service for this instance do not use these proxy variables. This does not turn off system VPN/TUN or change routing.
- **Specified proxy:** enter an HTTP(S) proxy address reachable from WSL. An address reachable only from Windows is not sufficient. The proxy may include credentials; they are saved only in your private instance configuration and necessary service environment files, never in process command lines, runtime checks, logs, or distribution packages. On a later save, leaving the field empty retains the existing specified proxy; entering a value replaces it. Switching to either other mode clears this dedicated address. HTTP(S) proxy is the supported explicit configuration; SOCKS does not imply that extra dependencies will be installed automatically.

All three modes retain existing `NO_PROXY` exceptions and add localhost/127.0.0.1/::1. These explicit bypass rules are not a direct-connection fallback after a failure. Downloads and the next native sign-in use the saved setting; an already-running background service must be explicitly restarted. Installation checks do not call a model, and finding proxy variables does not display “model connected.” If your proxy fails, repair it or explicitly change the selection before retrying; the panel will not silently switch to direct connection.

The pip used for installation separately disables external `pip.conf`/`PIP_CONFIG_FILE`, so they cannot override this instance’s selection. It does not modify those external files or change how you run pip yourself in other terminals.

An unsigned Windows EXE may trigger system prompts; do not bypass SmartScreen, ExecutionPolicy, or UAC. Normal use does not require installing Electron, the .NET SDK, or compiling the panel yourself.

## A few optional features and everyday maintenance

- Scheduled tasks and external completion notifications are off by default. Enable and save them from the computer panel when needed; they take effect after a safe restart. Once enabled, scheduled tasks are still managed in the phone conversation in natural language; no separate phone-module switch is added.
- “安全停止／重启” (Safe stop/restart) first stops accepting new work and verifies that both Web and native in-progress tasks are empty; it does not force a stop when busy or unknown. It operates only on this instance and does not affect existing workbenches. Closing the panel does not stop a running task.
- Notifications must use your own task-channel sender and private topic **file path**. Do not enter topic contents in the panel or substitute a security-alert channel; see the deployment instructions below for the protocol. No test notification is sent when it is not configured.
- Before “备份” (Backup), first safely stop the instance. A backup includes this instance’s state, `workspace/`, and the healthy program/fallback version; it **does not include native sign-in cache, external project directories, or the entire computer**. A backup contains private data and the authentication database: keep it private, never commit it to Git or casually share it. Links or unsupported files cause a refusal rather than being silently omitted.
- “恢复到新目录” (Restore to a new directory) can be used directly on a new computer after its checks; an empty instance need not be installed first. It only creates a new offline copy: it does not overwrite the original instance, start it, or replay work. On success the panel switches to the new directory. You must sign in natively again; sessions on the old device are not migrated, and modules are off by default. Native history, external cwd, and delivery states awaiting verification still need checking; automatic restoration or reruns are not promised.
- Restore validates materials separately. In the same environment it can restore the fixed healthy program and prior fallback version; if a trusted system Python 3.12 path is unchanged but the base bytes have changed (for example, after a system patch), it restores data only and **does not copy or run the old program**. The panel prompts you to click “安装” (Install) to rebuild current software. A program backup is not a whole-machine image and does not support a different operating system/architecture. An untrusted interpreter path or damaged material is still refused. The restored copy’s fallback chain includes only the healthy versions actually brought back; it does not point to older versions not included. The original instance and original backup are unchanged.
- To upgrade this project’s instance: safe stop and verify idle → consistent backup → install the new software from the same package → start. If startup verification fails, reuse the existing fixed program package to fall back. Fallback switches only the program, not the conversation database; an older version that does not recognize module options requires you to check its actual behavior again.
- To upgrade **only from this project’s earlier unpublished single-CLI build**: the new check identifies the old incomplete native kit, but still allows “安全停止” (Safe stop) to stop writes/verify in-progress work; it does not allow the old kit to start or receive ordinary backups just for compatibility. Installation first captures an old data restore point explicitly marked “恢复须重建” (restore requires rebuild), then installs the same-version complete runtime package. The native sign-in directory and phone binding for the same instance are neither moved nor reset. If an old backup is restored to a new directory, software must still be rebuilt as required and you must sign in/bind again. This compatibility path **does not apply to old public CLI/Kimi repositories**.
- Native-directory switching has a persistent transaction record. If installation is interrupted, explicitly installing again first checks the prior installation record and fixed file identity, then restores or confirms this switch; if it finds an inconsistency, it preserves evidence and does not overwrite concurrent changes. The entire old `native` tree is retained unchanged as `.native-previous-*`, because it may contain extra personal files; it is **not recursively cleaned automatically**. Unverified extra content is not represented as included in a program backup.
- “卸载入口（保留资料）” (Remove entry point, keep data) only unregisters this instance’s service and run entry. Projects, state, your native sign-in, and backups are retained; old software is moved intact to `retired-software/`, with no automatic disk wipe. Deleting those materials requires separately specifying the target and recovery impact.

This instance’s `instance.json`/service environment is readable and writable only by you. State, workspace, native HOME, backups, and fixed software are stored separately, not in source code or the Windows installation package. You do not need to study these files for daily use.

## Maintainers: build and check

For the Windows build, see [`desktop/README.md` (Chinese)](desktop/README.md). The finished package and source for the same package must stay together. Maintenance builds can use:

```bash
python3 scripts/build-desktop-package.py --panel-exe /absolute/task-output/CodexWorkbenchPanel.exe --destination /absolute/task-output/CodexWorkbench
```

Export files only from a clean, fixed Git version; do not copy private Git history, native HOME, or state. The installation package includes a content-verification manifest and can be rebuilt independently without `.git`. Verification can detect damage; it **does not replace publisher identity/signing or public release verification**. This command does not upload or publish.

`manage-instance.py --config <instance>/instance.json inspect` does not change deployment settings or services and does not initiate a model task; querying native sign-in status may create a temporary directory in that instance’s native HOME. Other actions are invoked by the panel, not a remote-management API. Do not let a new panel take over any existing production instance; this build does not include the author’s private production deployment/gateway tools.

## Optional features: configure only during installation/deployment

No phone-module master switch is added; the thin local computer panel reuses these deployment settings and does not create an additional web control panel. Once enabled, scheduled tasks continue to be created, paused, and deleted through conversation; the existing task-management page remains for viewing and management.

For a new installation, use [`core.env.example`](core.env.example) as the basis and write the two items below into the **service environment of the actual running Web/runtime controller**. By default, only the core is enabled:

| Deployment setting | `0` | `1` |
|---|---|---|
| `CODEX_WEB_AUTOMATIONS_ENABLED` | Do not start the scheduling loop; do not allow creating, changing, or resuming tasks; retain viewing, pausing, and deleting | Enable existing scheduled-task capability |
| `CODEX_WEB_COMPLETION_NOTIFICATIONS_ENABLED` | Do not call the external sender for conversation-completion notifications | Send task-channel notifications according to each conversation’s existing bell setting |

For compatibility with existing deployments, **these two settings remain enabled when unset**; existing personal-use state is not silently changed. A new instance created by the panel explicitly writes the core defaults, so ordinary users need not edit environment files. Only `1/true/yes/on` enables; all other values disable it. Prefer only `0` or `1`.

Configuration is read at process startup; editing the file does not hot-switch it. A systemd deployment can use a protected instance configuration file (readable and writable only by its owner, for example `0600`) and `EnvironmentFile=`; place it in a stable instance-configuration location, **not in source code, Git, or an immutable runtime package**. Merely `source`-ing the file in a terminal does not change an already-running service. An existing controller passes its service environment to the Web child process; do not mistakenly modify an old unit that no longer starts it. Project changes/restarts still follow the existing controlled maintenance entry and its write-stop, backup, and verification process; do not bypass those checks by killing processes directly or restarting the native App Server.

After disabling, no new scheduling starts; rounds already in the persistent command ledger may complete under their original authorization and are not forcibly interrupted. After enabling again, periods missed while disabled are recorded as skipped and are not made up; ordinary maintenance restarts that remain enabled continue the existing policy of not making up delays over 60 seconds. Task and run records, plus conversation bell preferences, are retained. These deployment options require support from this version of the code; when falling back to an older version that does not recognize them, you must check its actual enabled state again rather than assume the old code also honors disabled settings. Re-enabling completion notifications applies only to subsequent new rounds: it does not send past-completion or pre-restart reminders retroactively. A notification that began sending but has an unknown result remains isolated as unknown and is not resent.

### External task-completion notifications (optional)

If notifications are not needed, no Tools directory, topic file, or dedicated notification dependency is needed. When they are needed, the instance owner configures their own absolute paths: `CODEX_WEB_TASK_NOTIFICATION_COMMAND` is a readable Bash task-channel sender script; `CODEX_WEB_TASK_NTFY_TOPIC_FILE` is the path to its private topic file. The script receives three positional arguments: title, body, and priority `default`; through its environment it receives the `NTFY_TOPIC_FILE` path, `NTFY_MARKDOWN=1`, and `NTFY_TAGS`. Only exit `0` records success; other exit codes or a timeout record failure without saving output. The owner configures the specific delivery service and network. If a file is missing, no sending process starts; `task_sender_not_configured` is recorded and basic conversation remains usable.

Do not read or expose topic content to the browser, substitute a security-alert channel, or send configuration test notifications. This setting only controls task-channel completion notifications for conversations; it does not disable required security alerts, foreground sounds, or authentication, restore discontinued Web Push, or change independently configured dot integration. When this build omits paths, it uses an unconfigured placeholder and does not call the author’s sender; a new instance must configure its own absolute paths and must not copy someone else’s credentials.

### File delivery and title receipt

File generation and delivery are two steps: rely on `success: true` in the publishing tool’s model-visible JSON and the controlled download entry in this conversation. A successful receipt provides the result ID, size, SHA-256, and a download route requiring your own sign-in; it does not expose a backend storage path. Failure still explicitly returns `success: false`. Saved receipts from older versions are replayed as-is; historical success is not fabricated.

The automatic title tool is only for the first turn of a new conversation. Manual titles/already-finished first turns are protected; its `changed: false` does not affect the real task or file delivery, so do not generate files repeatedly because of it.
