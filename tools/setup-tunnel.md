# Cloudflare Tunnel + Access -- LIVE

The dashboard is published at **<https://lumbung.example.com>**, behind
Cloudflare Access. Everything below is already done; this is the record of what
was built and how to verify or undo it.

## What is running

| Piece | Value |
|---|---|
| Domain | `akbarharyadi.com`, Cloudflare Free, zone Active |
| Registrar | Biznet Gio NEO Domain -> NS `craig.ns.cloudflare.com`, `marjory.ns.cloudflare.com` |
| Tunnel | `lumbung`, UUID `<your-tunnel-uuid>` |
| Tunnel config | `%USERPROFILE%\.cloudflared\config.yml` -> `http://localhost:8788` |
| Access app | `lumbung` / destination `lumbung.example.com` |
| Access policy | `Akbar only` -- Allow, Include **Emails** = `you@example.com` |
| Session | 24 hours |
| Team domain | `your-team.cloudflareaccess.com` |
| Dashboard | `--readonly --host 127.0.0.1 --port 8788` |
| Autostart | `Lumbung-Engine.vbs`, `Lumbung-Dashboard.vbs`, `Lumbung-Tunnel.vbs` |

The old LAN dashboard on `0.0.0.0:8787` is **no longer started**. There is one
dashboard now and it is the published one.

## Why the published dashboard is 127.0.0.1 and read-only

Two separate reasons, and both still matter now that Access is verified:

* **`--host 127.0.0.1`** -- nothing on the WiFi can reach port 8788 directly.
  The only route in is the tunnel, so there is no way to arrive at the
  dashboard without passing Access first. Binding `0.0.0.0` would leave a
  second, unauthenticated door open on the LAN.
* **`--readonly`** -- pause / flat / kill are disabled even for a fully
  authenticated session, in the chat as well as on the buttons. Getting at them
  means starting a dashboard without this flag on the home network, which the
  tunnel does not reach. One compromised path should not be enough to sell your
  position.

To get the buttons back in the browser, drop `--readonly` from
`Lumbung-Dashboard.vbs` and `tools\start-tunnel.bat`. That is a deliberate
trade, not a default.

## Verifying it still works

    curl -s -o /dev/null -w "%{http_code}\n" https://lumbung.example.com/

**302** is correct -- it means Access is intercepting. **200** means Access is
NOT protecting the site and you should stop the tunnel and check the policy.

Access runs at Cloudflare's edge, *before* the request reaches this PC, so the
302 appears even with the tunnel stopped. That is the cleanest way to test the
policy without exposing anything.

Also confirmed: a request carrying the correct dashboard bearer token still
gets a 302. Access rejects before the token is ever evaluated.

Finally, **open it in a private browser window.** If it loads without asking
you to sign in, the policy is not attached. Testing while signed in proves
nothing.

## Logging in

First visit -> Cloudflare asks for your email -> sends a one-time PIN to
`you@example.com` -> 24-hour session.

To allow another address, add it to the `Akbar only` policy:
Zero Trust -> Access controls -> Applications -> `lumbung` -> Policies.

## Turning it off

* **Stop publishing, keep the setup:** delete `Lumbung-Tunnel.vbs` from the
  Startup folder and `taskkill /IM cloudflared.exe`.
* **Tear it down entirely:** `tools\cloudflared.exe tunnel delete lumbung`,
  then delete the Access application and the `lumbung` DNS record.

## Rebuilding from scratch

    tools\cloudflared.exe tunnel login
    tools\cloudflared.exe tunnel create lumbung
    REM copy tools\tunnel-config-template.yml -> %USERPROFILE%\.cloudflared\config.yml
    REM substituting the UUID and domain, and pointing service: at port 8788
    tools\cloudflared.exe tunnel route dns lumbung lumbung.example.com

Then recreate the Access application (Self-hosted, Public DNS) and its policy.

## Note on Zero Trust billing

Zero Trust Free is $0/seat/month for 50 seats, but Cloudflare still routes
activation through a billing checkout that requires a payment method on file.
Akbar completed that step himself.
