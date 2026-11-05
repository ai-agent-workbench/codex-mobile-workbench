# How to obtain a phone-access URL

[简体中文](PHONE_ACCESS.md) · [English](PHONE_ACCESS.en.md)

For a first connection, start with a **private network between your own devices**. Consider a hosted public entry if you prefer not to install a connection client on your phone. Both need stable, trusted HTTPS and workbench authentication. A connected tunnel alone does not prove the workbench works.

| Route | Phone requirements | Trade-off |
| --- | --- | --- |
| Tailscale Serve | Tailscale client and your own account | No separate domain purchase; access stays within the authorized private network; both devices must remain connected |
| Cloudflare Tunnel | Browser | Your own account and manageable domain; published entry is publicly reachable, requiring review of privacy, costs and access rules |

These are configuration and checking instructions, not automatic setup. Neither third-party route has completed this project's second-computer/physical-phone end-to-end acceptance. Local gateway checks are not that acceptance. See the [support matrix (Chinese)](SUPPORT_MATRIX.md).

## Install first; obtain the URL afterwards

1. Complete [Quick Start](QUICKSTART.en.md) steps 1–5: install the software and sign in to your own Codex. **No phone URL or workbench startup is required yet.**
2. Click “运行检查” (Run checks), and note “本机地址” (Local address), such as `http://127.0.0.1:PORT`. This address is loopback **inside the selected Ubuntu WSL distribution**, not your phone URL.
3. Run the gateway and connection program below **inside that same Ubuntu**. Windows loopback is not interchangeable with WSL loopback. Do not change WSL networking, open firewall ports, or bind to `0.0.0.0` to copy this example. An existing Windows-side connector needs its own verified path.

## 1. Prepare a filtering gateway in that WSL distribution

Both routes use: `phone HTTPS → connection program → filtering gateway at 127.0.0.1:18081 → panel's local address`. Do not point the tunnel directly at the application port: `/internal` and its subpaths must be blocked first.

You need a trusted Nginx installation in that Ubuntu. Prepare it under your system/organization's rules if absent; do not replace system Nginx configuration or take over an existing site. This example runs a separate loopback process, not a site in `/etc/nginx`.

In Ubuntu:

```bash
# If this directory exists, stop and inspect its purpose; do not overwrite it.
mkdir -m 700 "$HOME/codex-phone-gateway" || exit 1
mkdir -m 700 "$HOME/codex-phone-gateway/logs" || exit 1
ss -ltn '( sport = :18081 )'
```

If port 18081 is occupied, stop and choose another unused high port, replacing every gateway address below consistently. Open the [gateway configuration sample](examples/phone-gateway.nginx.conf) and save its contents as `$HOME/codex-phone-gateway/nginx.conf`. Replace `WORKBENCH_PORT` with the actual port from the panel, **not** an App Server port. The sample's 50 MiB request-body limit does not promise application support for every upload size.

```bash
nginx -p "$HOME/codex-phone-gateway/" -c nginx.conf -t || exit 1
# Start only after the configuration check succeeds.
nginx -p "$HOME/codex-phone-gateway/" -c nginx.conf
curl --noproxy '*' -i http://127.0.0.1:18081/internal
curl --noproxy '*' -i http://127.0.0.1:18081/internal/check
```

Both requests must return **404**. Ordinary `/login` may return 502 until the workbench starts; obtain the HTTPS URL next instead of reinstalling or repeatedly signing in to Codex. The gateway preserves WebSocket and Host, and sets the forwarded scheme to HTTPS for traffic arriving through the HTTPS connector. It listens only on loopback, not as a LAN HTTP entry.

## 2A. Private networking: Tailscale Serve (suggested first route)

1. Follow the [official Serve guide](https://tailscale.com/kb/1312/serve) to prepare and sign in to Tailscale **inside that Ubuntu**. Install the phone client and connect to the same private network using your own account. Review existing Windows clients/network policies before adding anything; do not migrate or overwrite them. Check access rules rather than assuming the same account alone guarantees connectivity.
2. Run `tailscale serve status` in Ubuntu. If Serve already has configuration, do not overwrite it; arrange a separate entry for this instance first. For a new entry:

   ```bash
   tailscale serve --bg http://127.0.0.1:18081
   ```

3. If asked to enable HTTPS, follow the provider's consent process and read the [certificate/device-name privacy notes](https://tailscale.com/kb/1153/enabling-https). Resolve permission errors through the provider's normal authorization flow; do not remove authentication or switch to public Funnel. This example uses **Serve, not Funnel**.
4. Copy the actual stable HTTPS URL shown by the command. The phone must remain connected to an authorized Tailscale network. Do not substitute an example, the author's URL, or WSL loopback.

## 2B. Hosted entry: Cloudflare Tunnel

For owners of a manageable domain who want phone-browser access, follow the [official remotely managed tunnel guide](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/):

1. Create a tunnel under your own account. Install, authorize and run its `cloudflared` connector **inside the same Ubuntu**. Token-bearing installation commands belong only in your trusted terminal, never Issues, screenshots or sample configurations.
2. Add a published application route for a dedicated hostname, not just a different path or port. Choose Service type **HTTP**, URL `127.0.0.1:18081`. Do not use the application port or override HTTP Host Header to localhost.
3. Preserve WebSocket and disable custom rules that cache private pages/APIs. Confirm the connector is online and copy the HTTPS URL. Publishing this route makes the entry publicly reachable; review third-party access policies without replacing workbench authentication.

Do not use a random Quick Tunnel URL for persistent pairing or disable certificate validation. Neither connector nor gateway may expose the native App Server or Unix socket.

## 3. Save the URL, start, and check from the phone

1. Enter the real URL in “手机 HTTPS 入口” (Phone HTTPS entry), save, then click “启动” (Start). Saving changes this instance's settings; it does not establish networking.
2. On the phone's actual network, open `https://YOUR-ACTUAL-HOST/login`. Expect the workbench sign-in page with no certificate warning. Check `/internal` and `/internal/check` at that hostname: both must return 404. If another site, an internal endpoint or a certificate warning appears, stop and fix the connection before pairing.
3. Complete [Quick Start](QUICKSTART.en.md) steps 8–10: bind your own phone, explicitly choose new-conversation permissions, and generate/download the first file. A working page alone does not prove WebSocket or model connectivity; check live task progress and the correct file.

Pair only after the URL is stable. A later URL change needs the [recovery procedure (Chinese)](OPERATIONS.md). You provide the domain, services and model costs; do not copy the author's connection details or credentials.

## Stop, restart and common blockers

This Nginx example is manually started, not registered as an autostart service. After WSL restarts, verify the connector, restart this gateway and the workbench, then check again from the phone. Tailscale `--bg` does not start Nginx or the workbench. For unattended operation, arrange service management suited to your own environment rather than copying the author's production units.

Stop **only this example's gateway** (not other Nginx instances):

```bash
nginx -p "$HOME/codex-phone-gateway/" -c nginx.conf -s quit
```

- **502:** check workbench startup and the actual upstream application port before reinstalling.
- **Cannot open:** check WSL, gateway and connector; private networking also needs the phone connection and access rules.
- **Certificate warning/wrong site:** fix the URL, domain and routing before pairing.
- **Can sign in but progress stalls:** check WebSocket forwarding; do not cache APIs.
- **Page works but task fails:** separately check permissions, native Codex sign-in and model networking; do not resend a task with unknown status.

Third-party official pages checked on 2026-11-05. Follow current official instructions if provider UI or commands change.
