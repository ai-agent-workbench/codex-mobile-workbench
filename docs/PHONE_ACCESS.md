# 怎样取得手机访问网址

[简体中文](PHONE_ACCESS.md) · [English](PHONE_ACCESS.en.md)

第一次接入建议先选**自己的设备私有组网**；如果不想在手机安装连接工具，再考虑托管公网入口。两种都需要稳定、受信的 HTTPS 和工作台本人登录，不能把“通道已连接”当作“工作台已经能用”。

| 路线 | 手机需要什么 | 主要取舍 |
| --- | --- | --- |
| Tailscale Serve 私有组网 | Tailscale 客户端及自己的账号 | 不必买域名，访问限于获准的私有网络；电脑、手机都要保持连接 |
| Cloudflare Tunnel 托管入口 | 浏览器 | 需账号和可管理域名；入口可被公网访问，要核对隐私、费用与访问策略，保留工作台认证 |

本页提供接线配置与检查步骤，不代装组件、不改已有网络。第三方路线尚未完成本项目的另一台电脑／实体手机端到端验收；本地网关检查不等于这些路线已实测成功。支持范围见[支持矩阵](SUPPORT_MATRIX.md)。

## 先安装，不要被网址卡住

1. 完成[快速开始](QUICKSTART.md)第 1–5 步：安装软件、完成自己的 Codex 登录。**这时不必启动工作台，也不需要先取得网址。**
2. 在 Windows 面板点“运行检查”，记下“本机地址”，例如 `http://127.0.0.1:端口`。这是**所选 Ubuntu WSL 内部**的回环地址，不是手机网址。
3. 下文中的 Nginx 与连接程序都运行在**该 Ubuntu 内**。不要把 Windows 连接程序里的 `127.0.0.1` 当成同一个地址，也不要为照抄步骤改变 WSL 网络模式、开放防火墙或绑定 `0.0.0.0`。已有 Windows 接线应按其实际路径核对，不能套用本例。

## 1. 在同一个 WSL 内准备过滤网关

两种路线共用：`手机 HTTPS → 连接程序 → 127.0.0.1:18081 的过滤网关 → 面板所示的本机地址`。不能把通道直接指向应用端口，因为 `/internal` 及其子路径必须先被挡住。

需要此 Ubuntu 已有可信来源的 Nginx。没有时先按系统／组织要求准备，不覆盖系统 Nginx 配置、不接管已有网站。以下只是本人回环端口上的独立进程，无需把新站点放进 `/etc/nginx`。

在 Ubuntu 终端：

```bash
# 目录已存在就停下，检查已有用途；不要覆盖。
mkdir -m 700 "$HOME/codex-phone-gateway" || exit 1
mkdir -m 700 "$HOME/codex-phone-gateway/logs" || exit 1
ss -ltn '( sport = :18081 )'
```

如果 18081 已被占用，停止并另选空闲高位端口，后面所有网关地址一并替换。打开[网关配置样例](examples/phone-gateway.nginx.conf)，将内容保存到 `$HOME/codex-phone-gateway/nginx.conf`；将 `WORKBENCH_PORT` 换成面板“本机地址”的实际端口，不能填原生 App Server 端口。样例的 50 MiB 为网关请求体上限，不代表应用支持所有大小的上传。

```bash
nginx -p "$HOME/codex-phone-gateway/" -c nginx.conf -t || exit 1
# 只有上一步成功才启动。
nginx -p "$HOME/codex-phone-gateway/" -c nginx.conf
curl --noproxy '*' -i http://127.0.0.1:18081/internal
curl --noproxy '*' -i http://127.0.0.1:18081/internal/check
```

两次必须均为 **404**。此时普通 `/login` 可能返回 502，因为工作台尚未启动；先完成下面的 HTTPS 网址配置，不要重装或重复登录 Codex。网关保留 WebSocket、原始 Host，并把来自 HTTPS 连接入口的转发协议固定为 HTTPS；它只在回环监听，不是供局域网直连的 HTTP 入口。

## 2A. 私有组网：Tailscale Serve（首次接入建议）

1. 按 [Tailscale 官方说明](https://tailscale.com/kb/1312/serve)在**该 Ubuntu**准备并登录 Tailscale，在手机安装客户端，用自己的账号连接同一私有网络。已有 Windows 客户端／其他网络策略时先核对，不迁移或覆盖现有配置。检查设备访问规则，不能仅凭“同一账号”推定互通。
2. 在该 Ubuntu 运行 `tailscale serve status`，若已有 Serve 配置，不覆盖，先为本实例选择独立接线。新入口运行：

   ```bash
   tailscale serve --bg http://127.0.0.1:18081
   ```

3. 如提示启用 HTTPS，按官方同意流程操作，先读[证书与设备名隐私提示](https://tailscale.com/kb/1153/enabling-https)。遇到权限错误按服务自己的授权流程处理，不关闭认证或改用公网 Funnel。本例使用 **Serve，不使用 Funnel**。
4. 复制命令显示的实际稳定 HTTPS 网址。手机必须连接获准的 Tailscale 网络；不能用例子、作者网址或 WSL 回环地址替代。

## 2B. 托管入口：Cloudflare Tunnel

适合已有可管理域名、希望手机只开浏览器的人。按[官方远程管理通道步骤](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/)：

1. 用自己的账号创建通道；在**同一个 Ubuntu**安装、授权并运行 `cloudflared` connector。包含通道 token 的安装命令只能在本人受信终端使用，不贴到 Issue、截图或配置样例。
2. 添加该工作台专用的发布应用路由：选择自己的独立 hostname（不能只换路径或端口），Service 类型为 **HTTP**，URL 为 `127.0.0.1:18081`。不要填工作台应用端口，不要把 HTTP Host Header 改成 localhost。
3. 保留 WebSocket，关闭可能缓存私有页面／API 的自定义规则。确认 connector 在线，复制实际 HTTPS 网址。新增发布路由会使入口可被公网访问；先核对第三方访问策略，但不能用它代替工作台本人登录。

不使用随机 Quick Tunnel 网址作长期绑定，不要求关闭证书验证；连接程序与网关都不能公开原生 App Server 或 Unix socket。

## 3. 回到面板启动，再逐项验收

1. 把实际 HTTPS 网址填入“手机 HTTPS 入口”，保存，点“启动”。保存网址只改本实例配置，不会开通网络。
2. 手机在实际使用的网络打开 `https://你的实际主机名/login`，应出现工作台登录页且无证书警告；再检查该 hostname 下的 `/internal` 与 `/internal/check` 都为 404。看见别的网站、内部接口或证书警告，先停止接入排查，不绑定手机。
3. 按[快速开始](QUICKSTART.md)第 8–10 步绑定本人手机、主动选择新对话权限，并完成文件生成与下载。页面能打开不代表 WebSocket 或模型连接已成功；以任务进展和正确文件为准。

网址稳定后再绑定；绑定后换网址须按[恢复说明](OPERATIONS.md)处理。自己配置的域名、服务和模型费用自理，不复制作者的接入信息或凭据。

## 停止、重启与常见卡点

本例 Nginx 手动启动，不自动注册开机服务。WSL 重启后，须确认连接程序恢复、重新启动此网关与工作台，再做一次手机检查；Tailscale `--bg` 不等于 Nginx 或工作台也会自动运行。需要长期无人值守时，应由本人为这些组件安排符合实际环境的服务管理，不复制作者的生产服务。

停止**本例专用网关**（不会停止其他 Nginx）：

```bash
nginx -p "$HOME/codex-phone-gateway/" -c nginx.conf -s quit
```

- **502**：检查工作台是否已启动、网关中的实际应用端口是否正确；不要先重装。
- **打不开**：确认 WSL、网关、connector 都在运行；私有组网还要检查手机连接与访问规则。
- **有证书警告／其他网站**：先修网址、域名和接线，不继续绑定。
- **能登录但不更新**：检查 WebSocket 升级／转发，不缓存接口。
- **页面正常、任务失败**：分开核对权限、原生 Codex 登录和模型网络；不要重复发送状态不明的任务。

第三方官方页面核对日期：2026-11-05；界面或命令改变时以对应官方说明为准。
