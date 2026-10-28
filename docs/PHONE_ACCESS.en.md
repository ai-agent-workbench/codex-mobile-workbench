# How to obtain a phone-access URL

[返回中文版 / Chinese version](PHONE_ACCESS.md)

Your phone accesses the workbench through an HTTPS URL. If you do not already have one, prepare it by one of the two methods below.

First complete software installation on the computer using the [Quick Start](QUICKSTART.en.md), then click “运行检查” (Run checks) in the panel and note the displayed local address. **Give the local address to the connection tool; enter the HTTPS URL provided by the connection tool back into the workbench panel.**

## Method 1: install a connection tool on both computer and phone

Using [Tailscale Serve](https://tailscale.com/kb/1312/serve) as an example:

1. Install Tailscale on the computer and phone, sign in to the same account on both devices, and confirm that both are online.
2. On the computer, follow its official instructions to enable HTTPS access and connect it to the workbench’s local port according to the requirements below.
3. Copy the HTTPS URL shown by Tailscale into “手机 HTTPS 入口” (Phone HTTPS entry) in the workbench panel.

This suits access between your own devices and does not require buying another domain for it; the phone needs the connection client installed and kept connected.

## Method 2: connect the computer to a hosted service; use only a browser on the phone

Using [Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/) as an example:

1. Prepare your own service account and a domain you can manage.
2. Install its connection program on the computer, create a tunnel, configure a stable HTTPS URL for the workbench, and connect it to the local service according to the requirements below.
3. Enter this URL in the workbench panel; then the phone can open it with a browser.

This requires one fewer app on the phone, but you must prepare a domain and check the service’s cost, privacy, and access settings. A public entry point must retain workbench authentication for yourself.

These are examples of common methods, not a requirement to use a particular provider. For what this project has and has not verified, see the [support notes (Chinese)](SUPPORT_MATRIX.md). They do not mean the project has verified these methods in a real environment.

## After the URL is ready

Complete your own Codex sign-in, save the URL in the panel, and click “启动” (Start). Then scan the QR code or open the URL on your phone. Once the correct sign-in page appears, follow the [Quick Start](QUICKSTART.en.md) to bind your phone and complete the first task.

The URL should remain stable so you can continue signing in later. Copy the actual address given by the service; do not copy an example in documentation or the author’s address.

## Entry-point configuration requirements

- Forward only the workbench’s local port. Do not expose the native App Server or socket.
- Block `/internal` and all its subpaths; retain WebSocket and correct Host/HTTPS information.
- Use a dedicated HTTPS hostname; changing only a path or port does not isolate cookies. Retain your own authentication: a private network does not replace workbench sign-in.
- When using a private network, do not switch it to public mode or overwrite existing services without authorization. If the URL changes after binding, follow the recovery procedure.

For Tailscale HTTPS setup, also see the [official instructions](https://tailscale.com/kb/1153/enabling-https). Before enabling it, read the service’s privacy notice; device names must not include sensitive information.

## What to check first when it will not open

- The page will not open: check that the workbench has started and the connection program is online; when using a private network, the phone must also be connected.
- There is a certificate warning or it opens another site: correct the URL and configuration before binding the phone.
- The page opens but the task fails: check phone binding, Codex sign-in, and model connection separately; do not repeatedly send the same task with an unknown status.
