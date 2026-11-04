# CI

GitHub Actions 工作流位于 [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)。它在
普通 `push` 和 `pull_request` 上运行；不使用 `pull_request_target`、Secrets、模型调用或
自动发布。工作流权限固定为最小的 `contents: read`，所有第三方 Action 均固定到完整
commit SHA。

## 五项检查

1. **Python 3.12 full test suite**：在 `ubuntu-24.04` 以
   受信 `/usr/bin/python3.12` 创建独立 venv，以 `requirements-core.lock` 的哈希锁安装依赖，并运行
   `bash scripts/run_tests.sh`。
2. **Offline UI core and settings checks**：独立安装锁定的 Playwright
   1.62.1（见 [`ci/package-lock.json`](../ci/package-lock.json)）、Chromium、nginx 和
   OpenSSL，随后运行 `ui core settings`。不设置 `CODEX_SETTINGS_GATEWAY=direct`；缺少
   nginx 或 OpenSSL 会由既有 UI 入口失败，而不会降级为直连。
3. **Windows desktop panel self-test**：在 `windows-2022` 将 EXE 构建到
   `RUNNER_TEMP`，执行 `CodexWorkbenchPanel.exe --self-test` 并显式检查其退出码。只有
   通过自测的 EXE 才会作为同一 workflow run 的 artifact 上传。
4. **Build and verify desktop package**：全新的 Ubuntu checkout 下载同一次运行中的 EXE
   artifact，构建 ZIP 与 SHA-256 文件，核验 ZIP 顶层前缀、清单、每个受清单保护的文件和
   面板摘要，并确认 `package-source.json` 的来源 commit 等于该 checkout 的
   `GITHUB_SHA`。通过后只上传 artifact；不会创建 GitHub Release 或发布任何内容。
5. **Locked dependency audit**：固定 `pip-audit==2.10.0`，依次检查 core、native
   interactions、passkey、webpush build 和 webpush 五个既有锁文件。没有漏洞忽略列表；
   漏洞、无法解析依赖或审计错误都会使 job 失败。

## 本地相关入口

```bash
python3 -m unittest tests.test_ci_package -v
python3 scripts/check-ci-package.py --help
bash scripts/run_tests.sh ui --dry-run core settings
```

`scripts/check-ci-package.py` 只校验已经生成的 ZIP；不构建、上传或发布。完整打包仍使用
`scripts/build-desktop-package.py`，它要求源码 checkout 完全干净且输出目录在源码之外。

依赖内容锁的支持平台、校验边界与更新流程见 [Dependency content locks](DEPENDENCIES.md)。
