# Windows 原生轻量面板

`WorkbenchPanel.cs` 编译为 WinForms 小面板：它不是网页、浏览器壳或完整聊天客户端。它只编排已定义的 WSL 实例检查、安装准备、服务控制、手机入口和内置可选模块配置；默认先运行检查，退出窗口不会停止后台服务。

需要 Windows 与 WSL2。可用 Windows 自带的 .NET Framework `csc.exe` 构建，且必须把成品输出到仓库外的任务私有目录：

```powershell
.\desktop\build-panel.ps1 -OutputDirectory C:\Temp\codex-panel-output
C:\Temp\codex-panel-output\CodexWorkbenchPanel.exe --self-test
C:\Temp\codex-panel-output\CodexWorkbenchPanel.exe --self-test --render C:\Temp\codex-panel-output\panel-self-test.png
```

脚本不安装 SDK 或依赖、不修改 ExecutionPolicy、不使用 Bypass、不自动提权。未签名 exe 可能触发 SmartScreen；不要为了运行它而绕过系统策略。面板与本地实例 CLI 配套；安装、服务/socket、原生本人登录、真实模型及手机链路分别核验。当前工程验收状态以 INSTALL.md 和维护记录为准，不把无窗口自测等同实体手机可用。

`--self-test --render <Windows PNG路径>` 只构造合成初始/已实例状态并用 WinForms `DrawToBitmap` 生成离线诊断 PNG；不会显示窗口或调用 WSL。

`--source <Windows源码目录>` 仅供维护测试覆盖同包源码目录的自动识别；普通使用不需要填写。首次登录只在用户点击后打开可见的原生终端，设备码不被面板捕获。手机绑定的一次性口令只在专用弹窗显示，10 分钟后失效。

## 维护构建与脚本策略

只构建已核对的本项目源码。若系统策略拒绝运行未签名 `.ps1`（包括把 WSL UNC 路径视为远程来源），不要更改 ExecutionPolicy、使用 Bypass／Unblock 或关闭系统防护。维护者可直接使用既有 .NET Framework C# 编译器进行常规源码构建，不执行被拒绝的脚本：

```powershell
& "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /target:winexe /utf8output /out:C:\Temp\codex-panel-output\CodexWorkbenchPanel.exe /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll .\desktop\WorkbenchPanel.cs
```

输出目录须事先准备在源码之外的任务私有目录。若组织策略也禁止编译／运行该程序，停止并由所有者按组织规定处理，不尝试绕过。源码候选及最终同包文件仍须核验；离线自测的合成窗口不证明 WSL 实际安装／本人登录／手机可用。

## 从源码生成完整同包目录

普通使用者优先下载 Release 的完整 Windows／WSL 附件。GitHub 自动生成的源码 ZIP／tar.gz 不含 EXE 或 `package-source.json`，不是可直接双击安装的包。

自行构建需要已核对的固定 Git checkout，且工作区干净。先按上文在 Windows 构建面板，再在同一份源码对应的 WSL checkout 执行（输出目录不得位于源码中）：

```bash
python3 scripts/build-desktop-package.py \
  --panel-exe /mnt/c/Temp/codex-panel-output/CodexWorkbenchPanel.exe \
  --destination "$HOME/codex-mobile-workbench-package"
```

这一步从固定 Git 版本生成源码、面板和内容清单，不自动上传／发布。完整解压生成的 ZIP 后，再按 [快速开始](../docs/QUICKSTART.md) 检查与安装；不要手动拼接其他版本的面板或清单。
