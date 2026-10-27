using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace CodexWorkbenchPanel
{
    internal sealed class PanelOptions
    {
        internal string SourceOverride;
        internal string RenderPath;
        internal bool SelfTest;

        internal static PanelOptions Parse(string[] args)
        {
            PanelOptions options = new PanelOptions();
            for (int index = 0; index < args.Length; index++)
            {
                if (args[index] == "--self-test")
                {
                    options.SelfTest = true;
                }
                else if (args[index] == "--source" && index + 1 < args.Length)
                {
                    options.SourceOverride = args[++index];
                }
                else if (args[index] == "--render" && index + 1 < args.Length)
                {
                    options.RenderPath = args[++index];
                }
                else
                {
                    throw new ArgumentException("只支持 --self-test、--render <PNG路径> 和 --source <源码目录>。");
                }
            }
            if (!String.IsNullOrEmpty(options.RenderPath) && !options.SelfTest)
            {
                throw new ArgumentException("--render 只能与 --self-test 一起使用。");
            }
            return options;
        }
    }

    internal sealed class ProcessOutput
    {
        internal int ExitCode;
        internal string StandardOutput;
        internal string StandardError;
    }

    internal interface IProcessRunner
    {
        ProcessOutput Run(string fileName, IList<string> arguments, string standardInput, Encoding outputEncoding);
    }

    internal static class ProcessEncodings
    {
        // WSL/Python manager JSON always uses UTF-8 without a BOM. `wsl --list`
        // is a Windows command whose redirected list is UTF-16LE.
        internal static readonly Encoding Utf8NoBom = new UTF8Encoding(false);
        internal static readonly Encoding WslList = new UnicodeEncoding(false, false);
    }

    internal static class SystemExecutablePaths
    {
        internal static string WslExe
        {
            get
            {
                string systemDirectory = Environment.GetFolderPath(Environment.SpecialFolder.System);
                if (String.IsNullOrEmpty(systemDirectory))
                {
                    throw new InvalidOperationException("无法定位 Windows 系统目录中的 wsl.exe。");
                }
                return Path.GetFullPath(Path.Combine(systemDirectory, "wsl.exe"));
            }
        }
    }

    internal sealed class WindowsProcessRunner : IProcessRunner
    {
        public ProcessOutput Run(string fileName, IList<string> arguments, string standardInput, Encoding outputEncoding)
        {
            ProcessStartInfo startInfo = new ProcessStartInfo();
            startInfo.FileName = fileName;
            startInfo.Arguments = CommandLine.BuildArguments(arguments);
            startInfo.UseShellExecute = false;
            startInfo.CreateNoWindow = true;
            startInfo.RedirectStandardOutput = true;
            startInfo.RedirectStandardError = true;
            startInfo.RedirectStandardInput = standardInput != null;
            startInfo.StandardOutputEncoding = outputEncoding;
            startInfo.StandardErrorEncoding = outputEncoding;

            using (Process process = Process.Start(startInfo))
            {
                // Drain both pipes concurrently: a verbose stderr must not block a
                // manager that is still waiting for stdout to be read.
                Task<string> standardOutputTask = process.StandardOutput.ReadToEndAsync();
                Task<string> standardErrorTask = process.StandardError.ReadToEndAsync();
                if (standardInput != null)
                {
                    byte[] inputBytes = ProcessEncodings.Utf8NoBom.GetBytes(standardInput);
                    process.StandardInput.BaseStream.Write(inputBytes, 0, inputBytes.Length);
                    process.StandardInput.Close();
                }
                process.WaitForExit();
                Task.WaitAll(standardOutputTask, standardErrorTask);
                return new ProcessOutput
                {
                    ExitCode = process.ExitCode,
                    StandardOutput = standardOutputTask.Result,
                    StandardError = standardErrorTask.Result,
                };
            }
        }
    }

    internal static class CommandLine
    {
        internal static string BuildArguments(IList<string> arguments)
        {
            StringBuilder result = new StringBuilder();
            for (int index = 0; index < arguments.Count; index++)
            {
                if (index > 0)
                {
                    result.Append(' ');
                }
                result.Append(QuoteWindowsArgument(arguments[index]));
            }
            return result.ToString();
        }

        internal static string QuoteWindowsArgument(string value)
        {
            if (value == null || value.Length == 0)
            {
                return "\"\"";
            }
            bool needsQuotes = false;
            foreach (char character in value)
            {
                if (char.IsWhiteSpace(character) || character == '"' || character == '&' ||
                    character == '|' || character == '<' || character == '>' || character == '^')
                {
                    needsQuotes = true;
                    break;
                }
            }
            if (!needsQuotes)
            {
                return value;
            }

            StringBuilder quoted = new StringBuilder();
            quoted.Append('"');
            int backslashes = 0;
            foreach (char character in value)
            {
                if (character == '\\')
                {
                    backslashes++;
                    continue;
                }
                if (character == '"')
                {
                    quoted.Append('\\', backslashes * 2 + 1);
                    quoted.Append('"');
                }
                else
                {
                    quoted.Append('\\', backslashes);
                    quoted.Append(character);
                }
                backslashes = 0;
            }
            quoted.Append('\\', backslashes * 2);
            quoted.Append('"');
            return quoted.ToString();
        }
    }

    internal sealed class ManagerResponse
    {
        internal bool Ok;
        internal Dictionary<string, object> Data;
        internal string Error;

        internal static ManagerResponse Parse(ProcessOutput output)
        {
            try
            {
                JavaScriptSerializer serializer = new JavaScriptSerializer();
                Dictionary<string, object> data = serializer.Deserialize<Dictionary<string, object>>(output.StandardOutput);
                if (data == null || !data.ContainsKey("ok"))
                {
                    return new ManagerResponse { Ok = false, Error = "后端未返回可识别的状态。" };
                }
                bool ok = data["ok"] is bool && (bool)data["ok"];
                string error = GetString(data, "error");
                return new ManagerResponse
                {
                    Ok = ok && output.ExitCode == 0,
                    Data = data,
                    Error = String.IsNullOrEmpty(error) ? "操作未完成，请查看该实例的检查结果。" : error,
                };
            }
            catch (Exception)
            {
                return new ManagerResponse { Ok = false, Error = "后端返回不是预期的单个 JSON 状态。" };
            }
        }

        internal static string GetString(Dictionary<string, object> data, string key)
        {
            object value;
            return data != null && data.TryGetValue(key, out value) && value != null ? value.ToString() : "";
        }
    }

    internal static class WslDistributionList
    {
        internal static string[] Parse(string output)
        {
            List<string> distributions = new List<string>();
            foreach (string line in (output ?? "").Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries))
            {
                string name = line.Trim();
                if (!String.IsNullOrEmpty(name))
                {
                    distributions.Add(name);
                }
            }
            return distributions.ToArray();
        }
    }

    internal sealed class ManagerClient
    {
        private readonly IProcessRunner runner;
        private readonly string sourceOverride;
        private readonly Action<ProcessStartInfo> loginTerminalStarter;

        internal ManagerClient(IProcessRunner runner, string sourceOverride)
            : this(runner, sourceOverride, delegate(ProcessStartInfo startInfo) { Process.Start(startInfo); })
        {
        }

        internal ManagerClient(IProcessRunner runner, string sourceOverride, Action<ProcessStartInfo> loginTerminalStarter)
        {
            if (loginTerminalStarter == null)
            {
                throw new ArgumentNullException("loginTerminalStarter");
            }
            this.runner = runner;
            this.sourceOverride = sourceOverride;
            this.loginTerminalStarter = loginTerminalStarter;
        }

        internal async Task<string> GetLinuxSourceAsync(string distribution)
        {
            string windowsSource = sourceOverride;
            if (String.IsNullOrEmpty(windowsSource))
            {
                string exeDirectory = Path.GetDirectoryName(Application.ExecutablePath);
                if (File.Exists(Path.Combine(exeDirectory, "scripts", "manage-instance.py")))
                {
                    windowsSource = exeDirectory;
                }
                else
                {
                    windowsSource = Path.GetFullPath(Path.Combine(exeDirectory, ".."));
                }
            }
            ProcessOutput output = await Task.Run(delegate
            {
                return runner.Run(SystemExecutablePaths.WslExe, new[] { "-d", distribution, "--exec", "/usr/bin/wslpath", "-u", windowsSource }, null, ProcessEncodings.Utf8NoBom);
            });
            if (output.ExitCode != 0 || String.IsNullOrWhiteSpace(output.StandardOutput))
            {
                throw new InvalidOperationException("无法将面板同包源码目录转换为 WSL 路径。可由维护测试使用 --source 指定源码目录。");
            }
            return output.StandardOutput.Trim();
        }

        internal async Task<ManagerResponse> InvokeAsync(string distribution, string instanceRoot, string action, Dictionary<string, object> input)
        {
            string normalizedRoot = NormalizeInstanceRoot(instanceRoot);
            if (action == "initialize" && (input == null || NormalizeInstanceRoot(ManagerResponse.GetString(input, "root")) != normalizedRoot))
            {
                throw new InvalidOperationException("安装准备的 root 必须与实例配置所在目录相同。 ");
            }
            string source = await GetLinuxSourceAsync(distribution);
            List<string> arguments = BuildManagerArguments(distribution, source, normalizedRoot, action);
            string standardInput = input == null ? null : new JavaScriptSerializer().Serialize(input);
            ProcessOutput output = await Task.Run(delegate { return runner.Run(SystemExecutablePaths.WslExe, arguments, standardInput, ProcessEncodings.Utf8NoBom); });
            return ManagerResponse.Parse(output);
        }

        internal static List<string> BuildManagerArguments(string distribution, string source, string instanceRoot, string action)
        {
            return new List<string>
            {
                "-d", distribution, "--exec", "/usr/bin/python3", "-B",
                source.TrimEnd('/') + "/scripts/manage-instance.py", "--config",
                NormalizeInstanceRoot(instanceRoot) + "/instance.json", action,
            };
        }

        internal static string NormalizeInstanceRoot(string instanceRoot)
        {
            string normalized = (instanceRoot ?? "").Trim().TrimEnd('/');
            if (String.IsNullOrEmpty(normalized))
            {
                throw new InvalidOperationException("请填写实例根目录。 ");
            }
            return normalized;
        }

        internal async Task<string> GetHomeAsync(string distribution)
        {
            ProcessOutput output = await Task.Run(delegate
            {
                return runner.Run(SystemExecutablePaths.WslExe, new[] { "-d", distribution, "--exec", "/usr/bin/printenv", "HOME" }, null, ProcessEncodings.Utf8NoBom);
            });
            if (output.ExitCode != 0 || String.IsNullOrWhiteSpace(output.StandardOutput))
            {
                throw new InvalidOperationException("未能读取该 WSL 发行版的 HOME。请先运行检查确认实例。 ");
            }
            return output.StandardOutput.Trim();
        }

        internal async Task StartLoginTerminalAsync(string distribution, string instanceRoot)
        {
            string source = await GetLinuxSourceAsync(distribution);
            string normalizedRoot = NormalizeInstanceRoot(instanceRoot);
            List<string> wslArguments = BuildManagerArguments(distribution, source, normalizedRoot, "login");
            loginTerminalStarter(BuildInteractiveLoginStartInfo(wslArguments));
        }

        internal static ProcessStartInfo BuildInteractiveLoginStartInfo(IList<string> wslArguments)
        {
            ProcessStartInfo startInfo = new ProcessStartInfo();
            startInfo.FileName = SystemExecutablePaths.WslExe;
            startInfo.Arguments = CommandLine.BuildArguments(wslArguments);
            startInfo.UseShellExecute = true;
            startInfo.RedirectStandardInput = false;
            startInfo.RedirectStandardOutput = false;
            startInfo.RedirectStandardError = false;
            return startInfo;
        }
    }

    internal sealed class WorkbenchPanelForm : Form
    {
        private readonly PanelOptions options;
        private readonly IProcessRunner runner;
        private readonly ManagerClient manager;
        private readonly ComboBox distributionBox = new ComboBox();
        private readonly TextBox rootBox = new TextBox();
        private readonly TextBox accountBox = new TextBox();
        private readonly TextBox phoneOriginBox = new TextBox();
        private readonly CheckBox automationsBox = new CheckBox();
        private readonly CheckBox completionNoticeBox = new CheckBox();
        private readonly TextBox notificationCommandBox = new TextBox();
        private readonly TextBox notificationTopicBox = new TextBox();
        private readonly ListView checksView = new ListView();
        private readonly Label phoneEntryLabel = new Label();
        private readonly Label localAddressLabel = new Label();
        private readonly Label activityLabel = new Label();
        private readonly PictureBox qrPicture = new PictureBox();
        private readonly List<Button> guardedButtons = new List<Button>();
        private Button inspectButton;
        private Button initializeButton;
        private Button installButton;
        private Button startButton;
        private Button stopButton;
        private Button restartButton;
        private Button configureButton;
        private Button loginButton;
        private Button authorizeButton;
        private Button backupButton;
        private Button rollbackButton;
        private Button restoreButton;
        private Button uninstallButton;
        private Button httpsHelpButton;
        private bool inspected;
        private bool instanceFound;
        private bool softwareVerified;
        private bool servicesStopped;
        private bool busy;
        private string inspectedScope = "";
        private string revision = "";
        private int activityCount;

        internal WorkbenchPanelForm(PanelOptions options, IProcessRunner runner)
        {
            this.options = options;
            this.runner = runner;
            this.manager = new ManagerClient(runner, options.SourceOverride);
            InitializeComponent();
        }

        internal int ActivityCount { get { return activityCount; } }
        internal int CheckItemCount { get { return checksView.Items.Count; } }

        internal void ApplyInspectionFixture(Dictionary<string, object> data, string distribution, string root)
        {
            bool originalBusy = busy;
            busy = true;
            distributionBox.Items.Clear();
            distributionBox.Items.Add(distribution);
            distributionBox.SelectedIndex = 0;
            rootBox.Text = root;
            busy = originalBusy;
            ApplyInspectionForScope(data, CurrentScope());
        }

        internal bool IsActionEnabled(string action)
        {
            foreach (Button button in guardedButtons)
            {
                if (String.Equals(button.Tag as string, action, StringComparison.Ordinal))
                {
                    return button.Enabled;
                }
            }
            return false;
        }

        internal void RenderDiagnostic(string path)
        {
            using (Bitmap bitmap = new Bitmap(ClientSize.Width, ClientSize.Height))
            using (Graphics graphics = Graphics.FromImage(bitmap))
            {
                graphics.Clear(SystemColors.Control);
                Control content = Controls.Count == 0 ? null : Controls[0];
                if (content == null)
                {
                    throw new InvalidOperationException("未构造面板内容，无法渲染诊断。 ");
                }
                content.Size = ClientSize;
                CreateControlsForDiagnostic(content);
                content.PerformLayout();
                DrawControlTree(content, graphics, Point.Empty);
                bitmap.Save(path, ImageFormat.Png);
            }
        }

        private static void CreateControlsForDiagnostic(Control control)
        {
            control.Visible = true;
            control.CreateControl();
            foreach (Control child in control.Controls)
            {
                CreateControlsForDiagnostic(child);
            }
        }

        private static void DrawControlTree(Control control, Graphics graphics, Point origin)
        {
            if (control.Width <= 0 || control.Height <= 0)
            {
                return;
            }
            using (Bitmap controlImage = new Bitmap(control.Width, control.Height))
            {
                control.DrawToBitmap(controlImage, new Rectangle(Point.Empty, controlImage.Size));
                graphics.DrawImageUnscaled(controlImage, origin);
            }
            foreach (Control child in control.Controls)
            {
                DrawControlTree(child, graphics, new Point(origin.X + child.Left, origin.Y + child.Top));
            }
        }

        private void InitializeComponent()
        {
            Text = "Codex 工作台轻量面板";
            Font = SystemFonts.MessageBoxFont;
            ClientSize = new Size(900, 690);
            MinimumSize = new Size(760, 620);
            StartPosition = FormStartPosition.CenterScreen;

            TableLayoutPanel layout = new TableLayoutPanel();
            layout.Dock = DockStyle.Fill;
            layout.Padding = new Padding(10);
            layout.ColumnCount = 2;
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 55));
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 45));
            layout.RowCount = 3;
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 180));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            Controls.Add(layout);

            layout.Controls.Add(BuildInstanceGroup(), 0, 0);
            layout.SetColumnSpan(layout.GetControlFromPosition(0, 0), 2);
            layout.Controls.Add(BuildChecksGroup(), 0, 1);
            layout.Controls.Add(BuildConfigurationGroup(), 1, 1);
            activityLabel.AutoSize = true;
            activityLabel.Text = "状态：未检查。退出窗口不会停止后台服务。";
            layout.Controls.Add(activityLabel, 0, 2);
            layout.SetColumnSpan(activityLabel, 2);

            Shown += async delegate { await LoadDistributionsAsync(); };
        }

        private Control BuildInstanceGroup()
        {
            GroupBox group = new GroupBox();
            group.Text = "实例与服务";
            group.Dock = DockStyle.Fill;
            group.AutoSize = false;
            TableLayoutPanel table = NewTable(2, 5);
            group.Controls.Add(table);

            distributionBox.DropDownStyle = ComboBoxStyle.DropDownList;
            distributionBox.SelectedIndexChanged += async delegate { InvalidateInspection(); await LoadDefaultRootAsync(); };
            AddRow(table, 0, "WSL 发行版", distributionBox, null);
            rootBox.Text = "尚未读取 HOME";
            rootBox.TextChanged += delegate { InvalidateInspection(); };
            AddRow(table, 1, "实例根目录", rootBox, "高级：可修改");
            accountBox.Text = "owner";
            AddRow(table, 2, "管理员名", accountBox, null);

            FlowLayoutPanel buttons = new FlowLayoutPanel();
            buttons.AutoSize = true;
            inspectButton = ActionButton("运行检查", "inspect", async delegate { await InspectAsync(); }, true);
            initializeButton = ActionButton("安装准备", "initialize", async delegate { await InitializeAsync(); }, false);
            installButton = ActionButton("安装", "install", async delegate { await RunActionAsync("install", null, "安装请求已完成。"); }, false);
            startButton = ActionButton("启动", "start", async delegate { await RunActionAsync("start", null, "已请求启动该实例。"); }, false);
            stopButton = ActionButton("安全停止", "stop", async delegate { await StopOrRestartAsync("stop"); }, false);
            restartButton = ActionButton("重启", "restart", async delegate { await StopOrRestartAsync("restart"); }, false);
            foreach (Button button in new[] { inspectButton, initializeButton, installButton, startButton, stopButton, restartButton })
            {
                buttons.Controls.Add(button);
            }
            table.Controls.Add(buttons, 1, 3);
            FlowLayoutPanel recoveryButtons = new FlowLayoutPanel();
            recoveryButtons.AutoSize = true;
            backupButton = ActionButton("备份", "backup", async delegate { await RunActionAsync("backup", null, null); }, false);
            rollbackButton = ActionButton("回退程序", "rollback", async delegate { await RollbackAsync(); }, false);
            restoreButton = ActionButton("恢复到新目录", "restore", async delegate { await RestoreAsync(); }, false);
            recoveryButtons.Controls.Add(backupButton);
            recoveryButtons.Controls.Add(rollbackButton);
            recoveryButtons.Controls.Add(restoreButton);
            uninstallButton = ActionButton("卸载入口（保留资料）", "uninstall", async delegate {
                if (MessageBox.Show("仅卸载所选实例的服务与运行入口。项目、状态、原生登录和备份保留；旧程序移入保留目录，不清理磁盘。是否继续？", "确认卸载所选实例", MessageBoxButtons.YesNo, MessageBoxIcon.Warning) == DialogResult.Yes) {
                    await RunActionAsync("uninstall", null, null);
                }
            }, false);
            recoveryButtons.Controls.Add(uninstallButton);
            table.Controls.Add(recoveryButtons, 1, 4);
            return group;
        }

        private Control BuildChecksGroup()
        {
            GroupBox group = new GroupBox();
            group.Text = "运行检查";
            group.Dock = DockStyle.Fill;
            TableLayoutPanel table = NewTable(1, 3);
            table.RowStyles[0] = new RowStyle(SizeType.Percent, 100);
            group.Controls.Add(table);
            checksView.Dock = DockStyle.Fill;
            checksView.View = View.Details;
            checksView.FullRowSelect = true;
            checksView.ShowItemToolTips = true;
            checksView.Columns.Add("检查", 115);
            checksView.Columns.Add("状态", 80);
            checksView.Columns.Add("说明", 220);
            checksView.Resize += delegate { FitCheckColumns(); };
            table.Controls.Add(checksView, 0, 0);
            table.SetRowSpan(checksView, 1);
            phoneEntryLabel.AutoSize = true;
            phoneEntryLabel.Text = "手机 HTTPS 入口：未检查";
            table.Controls.Add(phoneEntryLabel, 0, 1);
            qrPicture.SizeMode = PictureBoxSizeMode.CenterImage;
            qrPicture.Size = new Size(200, 200);
            table.Controls.Add(qrPicture, 0, 2);
            return group;
        }

        private Control BuildConfigurationGroup()
        {
            GroupBox group = new GroupBox();
            group.Text = "手机入口与内置可选模块";
            group.Dock = DockStyle.Fill;
            TableLayoutPanel table = NewTable(2, 10);
            group.Controls.Add(table);
            AddRow(table, 0, "手机 HTTPS 入口", phoneOriginBox, "例如 https://example.invalid");
            httpsHelpButton = new Button();
            httpsHelpButton.Text = "HTTPS 准备说明";
            httpsHelpButton.AutoSize = true;
            httpsHelpButton.Click += delegate { ShowHttpsHelp(); };
            table.Controls.Add(httpsHelpButton, 1, 1);
            localAddressLabel.AutoSize = true;
            localAddressLabel.Text = "本机地址：未检查";
            table.Controls.Add(localAddressLabel, 1, 2);
            automationsBox.Text = "启用定时任务模块";
            completionNoticeBox.Text = "启用完成通知模块";
            table.Controls.Add(automationsBox, 1, 3);
            table.Controls.Add(completionNoticeBox, 1, 4);
            AddRow(table, 5, "通知发送器路径", notificationCommandBox, "可选；只填路径");
            AddRow(table, 6, "通知主题文件路径", notificationTopicBox, "可选；只填路径，不填秘密");
            configureButton = ActionButton("保存设置（重启后生效）", "configure", async delegate { await ConfigureAsync(); }, false);
            loginButton = ActionButton("原生账号登录（打开终端）", "login", async delegate { await StartLoginAsync(); }, false);
            authorizeButton = ActionButton("绑定手机", "authorize-phone", async delegate { await AuthorizePhoneAsync(); }, false);
            table.Controls.Add(configureButton, 1, 7);
            table.Controls.Add(loginButton, 1, 8);
            table.Controls.Add(authorizeButton, 1, 9);
            return group;
        }

        private static TableLayoutPanel NewTable(int columns, int rows)
        {
            TableLayoutPanel table = new TableLayoutPanel();
            table.Dock = DockStyle.Fill;
            table.AutoSize = true;
            table.Padding = new Padding(8);
            table.ColumnCount = columns;
            if (columns > 1)
            {
                table.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
                table.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            }
            for (int row = 0; row < rows; row++)
            {
                table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            }
            return table;
        }

        private static void AddRow(TableLayoutPanel table, int row, string label, Control control, string hint)
        {
            Label title = new Label();
            title.Text = label;
            title.AutoSize = true;
            title.Anchor = AnchorStyles.Left;
            table.Controls.Add(title, 0, row);
            if (!String.IsNullOrEmpty(hint))
            {
                control.AccessibleDescription = hint;
                new ToolTip().SetToolTip(control, hint);
            }
            control.Dock = DockStyle.Fill;
            table.Controls.Add(control, 1, row);
        }

        private Button ActionButton(string text, string action, EventHandler handler, bool enabled)
        {
            Button button = new Button();
            button.AutoSize = true;
            button.Text = text;
            button.Tag = action;
            button.Enabled = enabled;
            button.Click += handler;
            guardedButtons.Add(button);
            return button;
        }

        private async Task LoadDistributionsAsync()
        {
            bool readDefaultRoot = false;
            SetBusy(true);
            try
            {
                ProcessOutput output = await Task.Run(delegate { return managerListDistributions(); });
                if (output.ExitCode != 0)
                {
                    RecordActivity("无法列出 WSL 发行版；请确认 WSL2 可用。");
                }
                else
                {
                    foreach (string name in WslDistributionList.Parse(output.StandardOutput))
                    {
                        distributionBox.Items.Add(name);
                    }
                    if (distributionBox.Items.Count > 0)
                    {
                        distributionBox.SelectedIndex = 0;
                        readDefaultRoot = true;
                        RecordActivity("正在读取默认实例根目录。");
                    }
                    else
                    {
                        RecordActivity("未找到 WSL 发行版。面板不会尝试安装或更改 WSL。 ");
                    }
                }
            }
            catch (Exception)
            {
                RecordActivity("无法列出 WSL 发行版。 ");
            }
            finally
            {
                SetBusy(false);
            }
            if (readDefaultRoot)
            {
                await LoadDefaultRootAsync();
            }
        }

        private ProcessOutput managerListDistributions()
        {
            return runner.Run(SystemExecutablePaths.WslExe, new[] { "--list", "--quiet" }, null, ProcessEncodings.WslList);
        }

        private async Task LoadDefaultRootAsync()
        {
            if (String.IsNullOrEmpty(SelectedDistribution()) || busy)
            {
                return;
            }
            try
            {
                string home = await manager.GetHomeAsync(SelectedDistribution());
                rootBox.Text = home.TrimEnd('/') + "/CodexWeb";
            }
            catch (Exception)
            {
                rootBox.Text = "未能读取 HOME；可在确认后手动填写实例根目录";
            }
        }

        private string SelectedDistribution()
        {
            return distributionBox.SelectedItem == null ? "" : distributionBox.SelectedItem.ToString();
        }

        private string SelectedRoot()
        {
            return ManagerClient.NormalizeInstanceRoot(rootBox.Text);
        }

        private async Task InspectAsync()
        {
            await RunActionAsync("inspect", null, null);
        }

        private async Task InitializeAsync()
        {
            Dictionary<string, object> input = new Dictionary<string, object>();
            input["root"] = rootBox.Text.Trim();
            input["account"] = accountBox.Text.Trim();
            await RunActionAsync("initialize", input, "安装准备已完成，请再次运行检查确认状态。");
        }

        private async Task ConfigureAsync()
        {
            Dictionary<string, object> input = new Dictionary<string, object>();
            input["phone_origin"] = phoneOriginBox.Text.Trim();
            input["automations"] = automationsBox.Checked;
            input["completion_notifications"] = completionNoticeBox.Checked;
            input["notification_command"] = notificationCommandBox.Text.Trim();
            input["notification_topic_file"] = notificationTopicBox.Text.Trim();
            input["expected_revision"] = revision;
            await RunActionAsync("configure", input, "模块配置已保存，正在刷新检查。");
        }

        private async Task StopOrRestartAsync(string action)
        {
            string operation = action == "stop" ? "安全停止" : "重启";
            DialogResult confirmation = MessageBox.Show(
                operation + "只作用于当前实例。在途任务会等待收口，面板不能强制停止。是否继续？",
                "确认" + operation,
                MessageBoxButtons.YesNo,
                MessageBoxIcon.Warning);
            if (confirmation == DialogResult.Yes)
            {
                await RunActionAsync(action, null, "已请求" + operation + "该实例。");
            }
        }

        private async Task RunActionAsync(string action, Dictionary<string, object> input, string successMessage)
        {
            if (String.IsNullOrEmpty(SelectedDistribution()))
            {
                RecordActivity("请先选择 WSL 发行版。 ");
                return;
            }
            string scope;
            try
            {
                scope = CurrentScope();
            }
            catch (Exception exception)
            {
                RecordActivity("操作未完成：" + exception.Message);
                return;
            }
            SetBusy(true);
            try
            {
                ManagerResponse response = await manager.InvokeAsync(SelectedDistribution(), SelectedRoot(), action, input);
                if (!response.Ok)
                {
                    RecordActivity("操作失败：" + response.Error);
                    return;
                }
                if (action == "inspect")
                {
                    ApplyInspectionForScope(response.Data, scope);
                    return;
                }
                if (action == "configure")
                {
                    string savedRevision = ManagerResponse.GetString(response.Data, "revision");
                    if (!String.IsNullOrEmpty(savedRevision))
                    {
                        revision = savedRevision;
                    }
                }
                if (action == "restore")
                {
                    scope = SelectRestoredRoot(response.Data);
                }
                await RefreshAfterWriteAsync(action, scope, ResultMessage(response.Data, successMessage));
            }
            catch (Exception exception)
            {
                RecordActivity("操作未完成：" + exception.Message);
            }
            finally
            {
                SetBusy(false);
            }
        }

        internal string SelectRestoredRoot(Dictionary<string, object> data)
        {
            rootBox.Text = ManagerClient.NormalizeInstanceRoot(ManagerResponse.GetString(data, "root"));
            InvalidateInspection();
            return CurrentScope();
        }

        private async Task RefreshAfterWriteAsync(string action, string scope, string successMessage)
        {
            ManagerResponse inspection = await manager.InvokeAsync(SelectedDistribution(), SelectedRoot(), "inspect", null);
            if (!inspection.Ok)
            {
                InvalidateInspection();
                RecordActivity("操作完成但刷新检查失败：" + inspection.Error);
                return;
            }
            ApplyInspectionForScope(inspection.Data, scope);
            RecordActivity(successMessage ?? "操作完成，已刷新检查。 ");
        }

        private static string ResultMessage(Dictionary<string, object> data, string fallback)
        {
            List<string> parts = new List<string>();
            string note = ManagerResponse.GetString(data, "note");
            string root = ManagerResponse.GetString(data, "root");
            string backup = ManagerResponse.GetString(data, "backup");
            if (!String.IsNullOrEmpty(note)) { parts.Add(note); }
            if (!String.IsNullOrEmpty(root)) { parts.Add("实例：" + root); }
            if (!String.IsNullOrEmpty(backup)) { parts.Add("备份：" + backup); }
            return parts.Count == 0 ? (fallback ?? "操作完成，已刷新检查。 ") : String.Join("；", parts.ToArray());
        }

        private async Task RollbackAsync()
        {
            if (MessageBox.Show("回退只更换程序，不会回退数据。仅当前已停止实例可继续。是否继续？", "确认回退程序", MessageBoxButtons.YesNo, MessageBoxIcon.Warning) == DialogResult.Yes)
            {
                await RunActionAsync("rollback", null, null);
            }
        }

        private async Task RestoreAsync()
        {
            Dictionary<string, object> input = PromptRestore();
            if (input != null)
            {
                await RunActionAsync("restore", input, null);
            }
        }

        private Dictionary<string, object> PromptRestore()
        {
            using (Form dialog = new Form())
            {
                dialog.Text = "恢复到新目录";
                dialog.StartPosition = FormStartPosition.CenterParent;
                dialog.ClientSize = new Size(500, 170);
                Label message = new Label { AutoSize = false, Location = new Point(12, 10), Size = new Size(475, 35), Text = "已有目标会拒绝，不覆盖当前实例；成功后面板切到新目录。" };
                Label backupLabel = new Label { AutoSize = true, Location = new Point(12, 39), Text = "WSL 备份目录" };
                TextBox backup = new TextBox { Location = new Point(110, 36), Size = new Size(377, 24) };
                backup.AccessibleName = "WSL 备份目录";
                Label destinationLabel = new Label { AutoSize = true, Location = new Point(12, 73), Text = "新恢复目录" };
                TextBox destination = new TextBox { Location = new Point(110, 70), Size = new Size(377, 24), Text = SelectedRoot() + "-recovered" };
                Button confirm = new Button { Text = "恢复", Location = new Point(330, 125), DialogResult = DialogResult.OK };
                dialog.Controls.Add(message); dialog.Controls.Add(backupLabel); dialog.Controls.Add(backup); dialog.Controls.Add(destinationLabel); dialog.Controls.Add(destination); dialog.Controls.Add(confirm); dialog.AcceptButton = confirm;
                if (dialog.ShowDialog() != DialogResult.OK || String.IsNullOrWhiteSpace(backup.Text) || String.IsNullOrWhiteSpace(destination.Text))
                {
                    return null;
                }
                Dictionary<string, object> input = new Dictionary<string, object>();
                input["backup"] = backup.Text.Trim();
                input["destination"] = destination.Text.Trim();
                return input;
            }
        }

        private static void ShowHttpsHelp()
        {
            MessageBox.Show("填写 HTTPS 地址不会自动开通入口。请先准备受控反向代理或私有网络入口、有效 HTTPS 证书和到本机服务的受限转发；随后运行检查确认。面板不会修改端口、组网或防火墙。", "HTTPS 准备说明", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }

        private async Task AuthorizePhoneAsync()
        {
            if (String.IsNullOrEmpty(SelectedDistribution()))
            {
                return;
            }
            SetBusy(true);
            try
            {
                ManagerResponse response = await manager.InvokeAsync(SelectedDistribution(), SelectedRoot(), "authorize-phone", null);
                if (!response.Ok)
                {
                    RecordActivity("手机绑定请求失败：" + response.Error);
                    return;
                }
                string code = ManagerResponse.GetString(response.Data, "one_time_code");
                string loginUrl = ManagerResponse.GetString(response.Data, "login_url");
                if (String.IsNullOrEmpty(code))
                {
                    RecordActivity("手机绑定未返回一次性口令。 ");
                    return;
                }
                ShowAuthorizationDialog(code, loginUrl);
            }
            catch (Exception exception)
            {
                RecordActivity("手机绑定请求未完成：" + exception.Message);
            }
            finally
            {
                SetBusy(false);
            }
        }

        private async Task StartLoginAsync()
        {
            if (String.IsNullOrEmpty(SelectedDistribution()))
            {
                return;
            }
            SetBusy(true);
            try
            {
                await manager.StartLoginTerminalAsync(SelectedDistribution(), SelectedRoot());
                RecordActivity("已打开原生登录终端；完成后请手动运行检查。设备码只显示在该终端。 ");
            }
            catch (Exception exception)
            {
                RecordActivity("无法打开原生登录终端：" + exception.Message);
            }
            finally
            {
                SetBusy(false);
            }
        }

        private void SetBusy(bool value)
        {
            busy = value;
            distributionBox.Enabled = !value;
            rootBox.Enabled = !value;
            accountBox.Enabled = !value;
            phoneOriginBox.Enabled = !value;
            automationsBox.Enabled = !value;
            completionNoticeBox.Enabled = !value;
            notificationCommandBox.Enabled = !value;
            notificationTopicBox.Enabled = !value;
            UpdateActionButtons();
        }

        private void UpdateActionButtons()
        {
            bool validRevision = (!instanceFound && revision == "absent") || (instanceFound && IsSha256(revision));
            bool validInspection = inspected && validRevision && inspectedScope == CurrentScopeOrEmpty();
            foreach (Button button in guardedButtons)
            {
                string action = button.Tag as string;
                bool enabled = action == "inspect" ||
                    (action == "initialize" && validInspection && !instanceFound) ||
                    (action == "install" && validInspection && instanceFound) ||
                    (action == "configure" && validInspection && instanceFound) ||
                    (action == "restore" && validInspection) ||
                    ((action == "backup" || action == "rollback" || action == "uninstall") && validInspection && instanceFound && softwareVerified && servicesStopped) ||
                    ((action == "start" || action == "stop" || action == "restart" || action == "authorize-phone" || action == "login") && validInspection && instanceFound && softwareVerified);
                button.Enabled = !busy && enabled;
            }
        }

        private string CurrentScope()
        {
            return SelectedDistribution() + "\n" + SelectedRoot();
        }

        private string CurrentScopeOrEmpty()
        {
            try { return CurrentScope(); }
            catch (Exception) { return ""; }
        }

        private void InvalidateInspection()
        {
            inspected = false;
            instanceFound = false;
            softwareVerified = false;
            servicesStopped = false;
            inspectedScope = "";
            revision = "";
            UpdateActionButtons();
        }

        private static bool IsSha256(string value)
        {
            if (String.IsNullOrEmpty(value) || value.Length != 64)
            {
                return false;
            }
            foreach (char character in value)
            {
                if (!((character >= '0' && character <= '9') || (character >= 'a' && character <= 'f') || (character >= 'A' && character <= 'F')))
                {
                    return false;
                }
            }
            return true;
        }

        private static bool HasVerifiedSoftware(IList checks)
        {
            if (checks == null)
            {
                return false;
            }
            foreach (object item in checks)
            {
                Dictionary<string, object> check = item as Dictionary<string, object>;
                if (check != null && ManagerResponse.GetString(check, "name") == "software" && ManagerResponse.GetString(check, "status") == "ok")
                {
                    return true;
                }
            }
            return false;
        }

        private static bool ServicesAreStopped(Dictionary<string, object> services)
        {
            if (services == null || services.Count == 0)
            {
                return false;
            }
            foreach (object service in services.Values)
            {
                // The manager returns bare systemd-style strings.  Only explicit
                // non-running states permit maintenance, including a known failed
                // unit. Unknown, transitioning or running states fail closed.
                string status = service as string;
                if (status != "inactive" && status != "failed" && status != "not_installed")
                {
                    return false;
                }
            }
            return true;
        }

        private void RecordActivity(string message)
        {
            activityCount++;
            activityLabel.Text = "状态：" + message;
        }

        private void ApplyInspectionForScope(Dictionary<string, object> data, string scope)
        {
            string returnedRevision = ManagerResponse.GetString(data, "revision");
            bool found = GetBoolean(data, "instance_found");
            bool revisionValid = (!found && returnedRevision == "absent") || (found && IsSha256(returnedRevision));
            if (!revisionValid || scope != CurrentScopeOrEmpty())
            {
                InvalidateInspection();
                RecordActivity(!revisionValid ? "检查 revision 无效，未启用写操作。" : "实例作用域已变化，请重新检查。 ");
                return;
            }
            inspected = true;
            inspectedScope = scope;
            revision = returnedRevision;
            instanceFound = found;
            softwareVerified = HasVerifiedSoftware(data["checks"] as IList);
            servicesStopped = ServicesAreStopped(data.ContainsKey("services") ? data["services"] as Dictionary<string, object> : null);
            ApplyInspect(data);
            UpdateActionButtons();
            RecordActivity("检查完成；显示的是后端已验证状态。 ");
        }

        private void ApplyInspect(Dictionary<string, object> data)
        {
            string account = ManagerResponse.GetString(data, "account");
            if (!String.IsNullOrEmpty(account)) { accountBox.Text = account; }
            accountBox.ReadOnly = GetBoolean(data, "instance_found");
            checksView.Items.Clear();
            AddChecks(data["checks"] as IList);
            AddService("Web 服务", GetValue(data, "services", "web"));
            AddService("App Server", GetValue(data, "services", "app_server"));
            localAddressLabel.Text = "本机地址：" + (String.IsNullOrEmpty(ManagerResponse.GetString(data, "local_address")) ? "未检查" : ManagerResponse.GetString(data, "local_address"));
            Dictionary<string, object> settings = data.ContainsKey("settings") ? data["settings"] as Dictionary<string, object> : null;
            if (settings != null)
            {
                phoneOriginBox.Text = ManagerResponse.GetString(settings, "phone_origin");
                automationsBox.Checked = GetBoolean(settings, "automations");
                completionNoticeBox.Checked = GetBoolean(settings, "completion_notifications");
                notificationCommandBox.Text = ManagerResponse.GetString(settings, "notification_command");
                notificationTopicBox.Text = ManagerResponse.GetString(settings, "notification_topic_file");
            }
            Dictionary<string, object> phoneEntry = data.ContainsKey("phone_entry") ? data["phone_entry"] as Dictionary<string, object> : null;
            if (phoneEntry == null)
            {
                phoneEntryLabel.Text = "手机 HTTPS 入口：未检查或缺少";
                qrPicture.Image = null;
            }
            else
            {
                phoneEntryLabel.Text = "手机 HTTPS 入口：" + ManagerResponse.GetString(phoneEntry, "url");
                qrPicture.Image = DrawQr(phoneEntry.ContainsKey("qr_matrix") ? phoneEntry["qr_matrix"] as IList : null);
            }
        }

        private static object GetValue(Dictionary<string, object> data, string parent, string key)
        {
            Dictionary<string, object> parentData = data.ContainsKey(parent) ? data[parent] as Dictionary<string, object> : null;
            if (parentData == null || !parentData.ContainsKey(key))
            {
                return null;
            }
            return parentData[key];
        }

        private void AddChecks(IList checks)
        {
            if (checks == null)
            {
                return;
            }
            foreach (object checkObject in checks)
            {
                Dictionary<string, object> check = checkObject as Dictionary<string, object>;
                if (check == null)
                {
                    continue;
                }
                string title = ManagerResponse.GetString(check, "title");
                string name = ManagerResponse.GetString(check, "name");
                ListViewItem item = new ListViewItem(String.IsNullOrEmpty(title) ? name : title);
                string rawStatus = ManagerResponse.GetString(check, "status");
                item.SubItems.Add(DisplayStatus(rawStatus));
                item.SubItems.Add(ManagerResponse.GetString(check, "detail"));
                item.ToolTipText = rawStatus;
                checksView.Items.Add(item);
            }
        }

        private void AddService(string title, object service)
        {
            if (service == null)
            {
                checksView.Items.Add(new ListViewItem(new[] { title, "未检查", "未返回服务状态" }));
                return;
            }
            Dictionary<string, object> details = service as Dictionary<string, object>;
            if (details != null)
            {
                ListViewItem item = new ListViewItem(new[] { title, DisplayStatus(ManagerResponse.GetString(details, "status")), ManagerResponse.GetString(details, "detail") });
                item.ToolTipText = ManagerResponse.GetString(details, "status");
                checksView.Items.Add(item);
                return;
            }
            checksView.Items.Add(new ListViewItem(new[] { title, DisplayStatus(service.ToString()), "" }));
        }

        private void FitCheckColumns()
        {
            if (checksView.Columns.Count < 3)
            {
                return;
            }
            int detailWidth = Math.Max(140, checksView.ClientSize.Width - checksView.Columns[0].Width - checksView.Columns[1].Width - 8);
            checksView.Columns[2].Width = detailWidth;
        }

        private static string DisplayStatus(string status)
        {
            switch (status)
            {
                case "ok": return "正常";
                case "missing": return "缺少";
                case "error": return "错误";
                case "not_checked": return "未检查";
                case "not_installed": return "未安装";
                case "inactive": return "已停止";
                case "active": return "运行中";
                case "activating": return "启动中";
                case "deactivating": return "停止中";
                case "failed": return "启动失败";
                case "stopped": return "已停止";
                case "running": return "运行中";
                default: return String.IsNullOrEmpty(status) ? "未检查" : status;
            }
        }

        private static bool GetBoolean(Dictionary<string, object> data, string key)
        {
            object value;
            return data != null && data.TryGetValue(key, out value) && value is bool && (bool)value;
        }

        internal static Bitmap DrawQr(IList matrix)
        {
            if (matrix == null || matrix.Count == 0)
            {
                return null;
            }
            int width = 0;
            foreach (object row in matrix)
            {
                IList cells = row as IList;
                if (cells == null)
                {
                    return null;
                }
                width = Math.Max(width, cells.Count);
            }
            if (width == 0)
            {
                return null;
            }
            const int quietZone = 4;
            int modules = Math.Max(width, matrix.Count) + quietZone * 2;
            int cell = Math.Max(2, Math.Min(8, (int)Math.Ceiling(180.0 / modules)));
            Bitmap bitmap = new Bitmap(modules * cell, modules * cell);
            using (Graphics graphics = Graphics.FromImage(bitmap))
            {
                graphics.Clear(Color.White);
                for (int y = 0; y < matrix.Count; y++)
                {
                    IList row = matrix[y] as IList;
                    for (int x = 0; x < row.Count; x++)
                    {
                        if (row[x] is bool && (bool)row[x])
                        {
                            graphics.FillRectangle(Brushes.Black, (x + quietZone) * cell, (y + quietZone) * cell, cell, cell);
                        }
                    }
                }
            }
            return bitmap;
        }

        private static void ShowAuthorizationDialog(string code, string loginUrl)
        {
            using (Form dialog = new Form())
            {
                dialog.Text = "绑定手机（一次性口令）";
                dialog.StartPosition = FormStartPosition.CenterParent;
                dialog.ClientSize = new Size(430, 180);
                Label message = new Label();
                message.AutoSize = false;
                message.Location = new Point(14, 14);
                message.Size = new Size(400, 55);
                message.Text = "此一次性口令将在 10 分钟后失效。仅在手机绑定页面输入；关闭此窗口后请重新申请。";
                TextBox codeBox = new TextBox();
                codeBox.Location = new Point(14, 78);
                codeBox.Size = new Size(400, 25);
                codeBox.ReadOnly = true;
                codeBox.Text = code;
                Label url = new Label();
                url.AutoSize = false;
                url.Location = new Point(14, 110);
                url.Size = new Size(400, 30);
                url.Text = String.IsNullOrEmpty(loginUrl) ? "请在手机入口继续。" : loginUrl;
                Button close = new Button();
                close.Text = "关闭";
                close.DialogResult = DialogResult.OK;
                close.Location = new Point(339, 145);
                dialog.Controls.Add(message);
                dialog.Controls.Add(codeBox);
                dialog.Controls.Add(url);
                dialog.Controls.Add(close);
                dialog.AcceptButton = close;
                dialog.ShowDialog();
            }
        }
    }

    internal sealed class FakeRunner : IProcessRunner
    {
        private readonly int sourceDelayMilliseconds;

        internal FakeRunner(int sourceDelayMilliseconds = 0)
        {
            this.sourceDelayMilliseconds = sourceDelayMilliseconds;
        }

        internal sealed class Call
        {
            internal IList<string> Arguments;
            internal string StandardInput;
            internal Encoding OutputEncoding;
        }

        internal readonly List<Call> Calls = new List<Call>();

        public ProcessOutput Run(string fileName, IList<string> arguments, string standardInput, Encoding outputEncoding)
        {
            Calls.Add(new Call
            {
                Arguments = new List<string>(arguments),
                StandardInput = standardInput,
                OutputEncoding = outputEncoding,
            });
            if (arguments.Contains("/usr/bin/wslpath"))
            {
                if (sourceDelayMilliseconds > 0)
                {
                    Thread.Sleep(sourceDelayMilliseconds);
                }
                return new ProcessOutput { ExitCode = 0, StandardOutput = "/mnt/源码 A &\n", StandardError = "" };
            }
            if (arguments.Contains("/usr/bin/printenv"))
            {
                return new ProcessOutput { ExitCode = 0, StandardOutput = "/home/测试 用户\n", StandardError = "" };
            }
            string action = arguments[arguments.Count - 1];
            if (action == "inspect")
            {
                return new ProcessOutput
                {
                    ExitCode = 0,
                    StandardOutput = "{\"ok\":true,\"action\":\"inspect\",\"revision\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"instance_found\":true,\"local_address\":\"127.0.0.1:18080\",\"settings\":{\"phone_origin\":\"https://panel.invalid\",\"automations\":false,\"completion_notifications\":false,\"notification_command\":\"\",\"notification_topic_file\":\"\"},\"checks\":[{\"name\":\"software\",\"title\":\"运行软件\",\"status\":\"ok\",\"detail\":\"已验证\"},{\"name\":\"socket\",\"title\":\"本机连接\",\"status\":\"ok\",\"detail\":\"可用\"}],\"services\":{\"web\":\"inactive\",\"app_server\":\"inactive\"},\"phone_entry\":{\"url\":\"https://panel.invalid\",\"qr_matrix\":[[true,false],[false,true]]}}",
                    StandardError = "",
                };
            }
            if (action == "configure")
            {
                return new ProcessOutput { ExitCode = 0, StandardOutput = "{\"ok\":true,\"action\":\"configure\",\"revision\":\"revision-2\"}", StandardError = "" };
            }
            return new ProcessOutput { ExitCode = 0, StandardOutput = "{\"ok\":true,\"action\":\"" + action + "\"}", StandardError = "" };
        }

        internal void AssertInstanceConfig(string expectedRoot, params string[] actions)
        {
            foreach (string action in actions)
            {
                Call call = Calls.Find(delegate(Call item) { return item.Arguments[item.Arguments.Count - 1] == action; });
                if (call == null)
                {
                    throw new InvalidOperationException("Fake runner 未收到动作：" + action);
                }
                AssertConfigArguments(call.Arguments, expectedRoot, action);
                if (action == "initialize")
                {
                    Dictionary<string, object> input = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(call.StandardInput);
                    if (ManagerResponse.GetString(input, "root") != expectedRoot)
                    {
                        throw new InvalidOperationException("安装准备 root 与配置目录不一致。");
                    }
                }
                if (action == "configure")
                {
                    Dictionary<string, object> input = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(call.StandardInput);
                    if (ManagerResponse.GetString(input, "expected_revision") != "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
                    {
                        throw new InvalidOperationException("configure 未传入已检查 revision。");
                    }
                }
            }
        }

        internal void AssertManagerUtf8()
        {
            foreach (Call call in Calls)
            {
                if (!call.Arguments.Contains("/usr/bin/wslpath") && !Object.ReferenceEquals(call.OutputEncoding, ProcessEncodings.Utf8NoBom))
                {
                    throw new InvalidOperationException("Manager 调用未使用 UTF-8 输出。 ");
                }
            }
        }

        internal static void AssertConfigArguments(IList<string> arguments, string expectedRoot, string action)
        {
            int configIndex = arguments.IndexOf("--config");
            if (configIndex < 0 || arguments[configIndex + 1] != expectedRoot + "/instance.json")
            {
                throw new InvalidOperationException("动作未使用实例根配置：" + action);
            }
            if (arguments[configIndex + 1].StartsWith("/mnt/源码 A &/", StringComparison.Ordinal))
            {
                throw new InvalidOperationException("动作错误地向源码包写配置：" + action);
            }
        }
    }

    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            try
            {
                PanelOptions options = PanelOptions.Parse(args);
                if (options.SelfTest)
                {
                    return RunSelfTest(options.RenderPath);
                }
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.Run(new WorkbenchPanelForm(options, new WindowsProcessRunner()));
                return 0;
            }
            catch (Exception exception)
            {
                Console.Error.WriteLine(exception.Message);
                return 2;
            }
        }

        private static int RunSelfTest(string renderPath)
        {
            try
            {
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Assert(CommandLine.QuoteWindowsArgument("") == "\"\"", "空参数 quoting");
                Assert(CommandLine.QuoteWindowsArgument("C:\\中文 & 目录\\") == "\"C:\\中文 & 目录\\\\\"", "中文、& 和尾反斜杠 quoting");
                ProcessOutput errorOutput = new ProcessOutput { ExitCode = 2, StandardOutput = "{\"ok\":false,\"error\":\"缺少依赖\",\"code\":\"missing\"}" };
                Assert(!ManagerResponse.Parse(errorOutput).Ok, "JSON 错误状态解析");

                FakeRunner fakeRunner = new FakeRunner();
                ManagerClient client = new ManagerClient(fakeRunner, "C:\\中文 源码 A &\\");
                string distribution = "中文发行版 &";
                string instanceRoot = "/home/中文 用户/实例 B &";
                ManagerResponse inspection = client.InvokeAsync(distribution, instanceRoot, "inspect", null).GetAwaiter().GetResult();
                Assert(inspection.Ok && ManagerResponse.GetString(inspection.Data, "revision") == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "inspect revision");
                Dictionary<string, object> configureInput = new Dictionary<string, object>();
                configureInput["phone_origin"] = "https://panel.invalid";
                configureInput["automations"] = false;
                configureInput["completion_notifications"] = false;
                configureInput["notification_command"] = "";
                configureInput["notification_topic_file"] = "";
                configureInput["expected_revision"] = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
                Assert(client.InvokeAsync(distribution, instanceRoot, "configure", configureInput).GetAwaiter().GetResult().Ok, "configure revision");
                Dictionary<string, object> initializeInput = new Dictionary<string, object>();
                initializeInput["root"] = instanceRoot;
                initializeInput["account"] = "owner";
                Assert(client.InvokeAsync(distribution, instanceRoot, "initialize", initializeInput).GetAwaiter().GetResult().Ok, "安装准备调用");
                fakeRunner.AssertInstanceConfig(instanceRoot, "inspect", "configure", "initialize");
                fakeRunner.AssertManagerUtf8();
                Assert(ProcessEncodings.Utf8NoBom.GetPreamble().Length == 0, "JSON stdin 不带 UTF-8 BOM");
                Assert(WslDistributionList.Parse(ProcessEncodings.WslList.GetString(ProcessEncodings.WslList.GetBytes("中文发行版\r\n")))[0] == "中文发行版", "UTF-16 WSL 发行版列表解析");

                List<string> loginArguments = ManagerClient.BuildManagerArguments(distribution, "/mnt/源码 A &", instanceRoot, "login");
                FakeRunner.AssertConfigArguments(loginArguments, instanceRoot, "login");
                ProcessStartInfo loginStart = ManagerClient.BuildInteractiveLoginStartInfo(loginArguments);
                Assert(loginStart.FileName == SystemExecutablePaths.WslExe && Path.IsPathRooted(loginStart.FileName) && loginStart.UseShellExecute && !loginStart.Arguments.Contains("cmd.exe") && loginStart.Arguments.Contains("\"中文发行版 &\""), "直接交互 WSL login quoting");
                AssertAsyncLoginWithWindowsFormsMessagePump(distribution, instanceRoot);

                JavaScriptSerializer serializer = new JavaScriptSerializer();
                Dictionary<string, object> qrFixture = serializer.Deserialize<Dictionary<string, object>>("{\"qr_matrix\":[[true,false],[false,true]]}");
                Bitmap qr = WorkbenchPanelForm.DrawQr(qrFixture["qr_matrix"] as IList);
                Assert(qr != null && qr.Width > 0 && qr.Height > 0, "QR object[]/ArrayList 兼容");
                qr.Dispose();

                WorkbenchPanelForm form = new WorkbenchPanelForm(new PanelOptions { SourceOverride = "C:\\临时 源码" }, new FakeRunner());
                try
                {
                    form.CreateControl();
                    Assert(form.ActivityCount == 0, "初始通用状态日志为空");
                    Dictionary<string, object> initialFixture = serializer.Deserialize<Dictionary<string, object>>("{\"revision\":\"absent\",\"instance_found\":false,\"checks\":[{\"name\":\"software\",\"title\":\"运行软件\",\"status\":\"missing\",\"detail\":\"未安装\"}],\"services\":{},\"settings\":{},\"phone_entry\":null}");
                    form.ApplyInspectionFixture(initialFixture, distribution, instanceRoot);
                    Assert(form.IsActionEnabled("initialize") && form.IsActionEnabled("restore") && !form.IsActionEnabled("start") && !form.IsActionEnabled("configure"), "无实例 absent revision 可直接恢复，不能启动或配置");
                    form.ApplyInspectionFixture(inspection.Data, distribution, instanceRoot);
                    Assert(form.IsActionEnabled("start") && form.IsActionEnabled("configure") && form.IsActionEnabled("backup") && !form.IsActionEnabled("initialize"), "已实例按钮状态");
                    string recoveredRoot = instanceRoot + "-recovered";
                    string recoveredScope = form.SelectRestoredRoot(new Dictionary<string, object> { { "root", recoveredRoot } });
                    Assert(recoveredScope == distribution + "\n" + recoveredRoot, "恢复成功后选择新目录");
                    Assert(!form.IsActionEnabled("start") && !form.IsActionEnabled("configure"), "恢复切换后旧检查失效");
                    form.ApplyInspectionFixture(inspection.Data, distribution, instanceRoot);
                    AssertMaintenanceButtons(form, serializer, distribution, instanceRoot, "inactive", true);
                    AssertMaintenanceButtons(form, serializer, distribution, instanceRoot, "not_installed", true);
                    AssertMaintenanceButtons(form, serializer, distribution, instanceRoot, "failed", true);
                    foreach (string status in new[] { "active", "activating", "deactivating", "not_checked", "" })
                    {
                        AssertMaintenanceButtons(form, serializer, distribution, instanceRoot, status, false);
                    }
                    form.ApplyInspectionFixture(inspection.Data, distribution, instanceRoot);
                    Assert(form.CheckItemCount >= 4, "真实 JSON checks 未丢失");
                    if (!String.IsNullOrEmpty(renderPath))
                    {
                        form.RenderDiagnostic(renderPath);
                        Assert(File.Exists(renderPath), "PNG 渲染诊断");
                    }
                }
                finally
                {
                    form.Dispose();
                }
                Console.WriteLine("panel_self_test=passed");
                return 0;
            }
            catch (Exception exception)
            {
                Console.Error.WriteLine("面板自测失败：" + exception.Message);
                return 1;
            }
        }

        private static void AssertAsyncLoginWithWindowsFormsMessagePump(string distribution, string instanceRoot)
        {
            bool launcherCalled = false;
            ProcessStartInfo launchedStartInfo = null;
            bool completed = false;
            Exception failure = null;
            FakeRunner delayedRunner = new FakeRunner(50);
            ManagerClient client = new ManagerClient(delayedRunner, "C:\\中文 源码 A &\\", delegate(ProcessStartInfo startInfo)
            {
                launcherCalled = true;
                launchedStartInfo = startInfo;
            });
            ApplicationContext messageLoop = new ApplicationContext();
            System.Windows.Forms.Timer deadline = new System.Windows.Forms.Timer();
            deadline.Interval = 2000;
            deadline.Tick += delegate
            {
                failure = new TimeoutException("WinForms 登录异步消息泵超时。");
                messageLoop.ExitThread();
            };
            SynchronizationContext previousContext = SynchronizationContext.Current;
            try
            {
                // A real WinForms synchronization context plus Application.Run's
                // message pump exercises the continuation that previously deadlocked.
                using (Control contextControl = new Control())
                {
                    contextControl.CreateControl();
                    SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());
                    Task loginTask = client.StartLoginTerminalAsync(distribution, instanceRoot);
                    loginTask.ContinueWith(delegate(Task task)
                    {
                        completed = true;
                        if (task.IsFaulted)
                        {
                            failure = task.Exception;
                        }
                        messageLoop.ExitThread();
                    }, TaskScheduler.FromCurrentSynchronizationContext());
                    deadline.Start();
                    Application.Run(messageLoop);
                    deadline.Stop();
                }
            }
            finally
            {
                deadline.Dispose();
                messageLoop.Dispose();
                SynchronizationContext.SetSynchronizationContext(previousContext);
            }

            Assert(failure == null && completed, "WinForms 登录异步流程完成");
            Assert(launcherCalled && launchedStartInfo != null, "登录仅经可注入启动器启动");
            Assert(launchedStartInfo.FileName == SystemExecutablePaths.WslExe && Path.IsPathRooted(launchedStartInfo.FileName) && launchedStartInfo.UseShellExecute && !launchedStartInfo.Arguments.Contains("cmd.exe"), "异步登录直接启动系统 WSL");
            Assert(launchedStartInfo.Arguments.Contains("--config") && launchedStartInfo.Arguments.Contains(CommandLine.QuoteWindowsArgument(instanceRoot + "/instance.json")), "异步登录使用所选实例配置");
        }

        private static void AssertMaintenanceButtons(WorkbenchPanelForm form, JavaScriptSerializer serializer, string distribution, string instanceRoot, string serviceStatus, bool expectedEnabled)
        {
            string fixture = "{\"revision\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"instance_found\":true,\"checks\":[{\"name\":\"software\",\"title\":\"运行软件\",\"status\":\"ok\",\"detail\":\"已验证\"}],\"services\":{\"web\":\"" + serviceStatus + "\",\"app_server\":\"" + serviceStatus + "\"},\"settings\":{},\"phone_entry\":null}";
            Dictionary<string, object> data = serializer.Deserialize<Dictionary<string, object>>(fixture);
            form.ApplyInspectionFixture(data, distribution, instanceRoot);
            bool correct = form.IsActionEnabled("backup") == expectedEnabled && form.IsActionEnabled("rollback") == expectedEnabled && form.IsActionEnabled("uninstall") == expectedEnabled;
            Assert(correct, "服务状态 " + (String.IsNullOrEmpty(serviceStatus) ? "空" : serviceStatus) + " 的维护按钮状态");
        }

        private static bool HasActionButton(Control control, string action)
        {
            Button button = control as Button;
            if (button != null && String.Equals(button.Tag as string, action, StringComparison.Ordinal))
            {
                return true;
            }
            foreach (Control child in control.Controls)
            {
                if (HasActionButton(child, action))
                {
                    return true;
                }
            }
            return false;
        }

        private static void Assert(bool condition, string message)
        {
            if (!condition)
            {
                throw new InvalidOperationException(message);
            }
        }
    }
}
