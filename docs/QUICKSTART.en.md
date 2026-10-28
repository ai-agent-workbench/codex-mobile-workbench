# First installation and use

[返回中文版 / Chinese version](QUICKSTART.md)

Once installed, you can send tasks from your phone, have your own computer perform them, and retrieve the results on your phone.

## Before you begin

- Your computer needs Windows + Ubuntu 24.04 WSL2 (x86-64, Python 3.12). See the other required components in the [installation environment](../INSTALL.en.md#installation-environment). If WSL is not installed yet, first prepare Ubuntu 24.04 following [Microsoft’s installation instructions](https://learn.microsoft.com/windows/wsl/install), then return to this page.
- Prepare your own Codex account and a network that can access software downloads and the model service. A proxy is not always required.
- Prepare your own HTTPS URL for phone access. If you do not have one yet, see [how to prepare a phone URL](PHONE_ACCESS.en.md).

## 1. Download and install

1. Download the complete Windows/WSL installation package from [Releases](https://github.com/ai-agent-workbench/codex-mobile-workbench/releases). Do not choose the automatically generated `Source code` archive: it does not contain the panel EXE or installation manifest.
2. Fully extract it, double-click `CodexWorkbenchPanel.exe`, select your Ubuntu, and click “运行检查” (Run checks). If the system blocks the program, first verify the download source and system policy; do not disable protections to force it to run.
3. Choose a new directory under your own WSL home directory and click “安装准备” (Prepare installation). Do not overwrite an existing workbench or data.
4. Choose a network method: use the existing WSL configuration, explicit direct connection, or specified proxy. After saving, click “安装” (Install) and wait for completion. **Installation complete does not mean it has started.**

## 2. Sign in and start

5. Click “原生账号登录” (Native account sign-in), complete your own Codex sign-in in the terminal and browser that open, then click “运行检查” (Run checks). Do not send device codes or sign-in credentials to anyone.
6. Enter your own stable URL in “手机 HTTPS 入口” (Phone HTTPS entry), save it, and click “启动” (Start). If you do not already have a URL, choose a method in [how to obtain a URL](PHONE_ACCESS.en.md) first.

## 3. Open it from your phone

7. Scan the standard access QR code with your phone, or open the saved URL directly. You should see your workbench’s sign-in page with no certificate warning. If it will not open, first check the computer service and connection method; do not repeatedly sign in to Codex again.
8. In the computer panel click “绑定手机” (Bind phone), then complete verification on your own phone as prompted. The one-time passcode expires after 10 minutes; do not share it.

## 4. Run your first task

9. In the phone workbench, create an empty test project, then create a conversation and check the selected model and permissions. Send the following text; you do not need to upload material first:

```text
Create only one file named “first-use.txt” with the content “This is my first result.” Deliver the file in this conversation and provide a clickable download link; do not only reply with the file path. Do not modify any other files.
```

10. Open the file on your phone and check its name and contents. **First use is complete when you can retrieve that correct file.**

The computer and WSL must remain running. Closing the panel does not stop background tasks; see the [operations guide (Chinese)](OPERATIONS.md) for stopping, backup, and restore.

If you encounter a problem, note the step and message where it stops, then see the [troubleshooting guide (Chinese)](TROUBLESHOOTING.md). Do not repeatedly send the same task with an unknown status, or upload passwords, binding passcodes, or complete private logs.

Scheduled tasks and completion notifications are off by default; enable them when needed. You do not need to test screen locking, network switching, recovery after an outage, and similar cases during first use.

If you used an older public edition, first see the [compatibility notes (Chinese)](LEGACY_COMPATIBILITY.md); do not overwrite the old directory.
