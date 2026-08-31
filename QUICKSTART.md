# Lumbung — QUICKSTART

## Just start it

Double-click **`START-HERE.bat`**.

It runs a setup check, then opens two windows — a dashboard and a paper-trading
engine. Leave both open; close them to stop. No API keys, no real money.

That is genuinely all you need to begin. Everything below is optional and can be
done later, in any order.

---

## The four optional steps

### 1. Let your phone reach the dashboard (2 min, once)

In an **Administrator** PowerShell:

```powershell
New-NetFirewallRule -DisplayName "Lumbung Dashboard" `
  -Direction Inbound -Protocol TCP -LocalPort 8787 -Action Allow -RemoteAddress LocalSubnet
```

Without it your phone times out. Connections from the PC itself bypass the firewall, so
the dashboard looks fine here while every other device is blocked — that is nearly
always why "it works on my PC but not my phone".

`-RemoteAddress LocalSubnet` keeps it to your home network, so the port stays
unreachable from the internet even if this laptop later joins a café WiFi.

Then open the `same WiFi` link the dashboard window prints, and use Chrome menu →
**Add to Home screen**.

### 2. Alerts and commands (nothing to set up)

Alerts arrive in the app chat, the same place you type commands. The engine appends
them to `data/answers.jsonl`; the dashboard streams that file. There is no token, no
bot and no second app.

That also means the engine does not need the dashboard to be running. A halt raised at
02:00 with nothing serving is waiting in the chat when you next open it.

Tap the chat bubble on the dashboard and type:

| Command | What it does |
|---|---|
| `/status` | the whole picture — net worth, passive income, goal, then the bot |
| `/positions` | open positions with live P&L |
| `/pnl` | realised P&L over 24h / 7d / 30d |
| `/pause` · `/resume` | stop / restart opening new trades |
| `/flat` | sell everything at market |
| `/kill` | flatten and halt completely |

The last four need a dashboard started **without** `--readonly`. On a read-only
deployment they are not registered at all, so `/kill` there is an unknown command that
becomes a question — not a lever behind a greyed-out button.

Anything that is not a command is a question, and gets answered in words.


### 3. Daily stock reminders

Double-click **`install-schedule.bat`**. A digest arrives at 16:15 every day: holdings,
sell rules that fired, new IDX ideas, income versus the subscription.

### The Indodax API key and your dynamic IP

Indodax Trade API V2 binds each key to an **IP Permission** list. Two things about
this are easy to get wrong.

**1. It must be your IPv4, and the bot must actually use IPv4.**

On a dual-stack connection Python prefers IPv6, so Indodax would see your IPv6 address
while the whitelist holds your IPv4 — and every signed call fails with an authorisation
error that looks exactly like a bad key. Both clients now force IPv4 (`force_ipv4=True`),
so what Indodax sees matches what you whitelisted.

Find the right value with:

```
lumbung check-ip
```

**2. Home broadband IPs change.** When yours does, the key stops working — with no code
change and no obvious cause. So record what you whitelisted:

```
INDODAX_WHITELIST_IP=203.0.113.10
```

`lumbung check-ip` and `lumbung doctor` then compare it against your live IP and tell
you plainly when it has drifted, instead of leaving you debugging a key that is fine.

#### Is whitelisting my IP safe if someone else gets it later?

Yes. The IP is a **restriction, not a credential**. Every private Indodax call must
carry all three of:

| | |
|---|---|
| `Key` header | your API key |
| `Sign` header | HMAC-SHA512 of the request body, keyed by your **secret** |
| source IP | must be on the whitelist |

Someone who inherits your IP later has the third and neither of the first two. They
cannot produce a valid signature without the secret, so every request they send is
rejected. The whitelist only ever *narrows* who can use a key that has already leaked —
it grants nothing by itself.

So the whitelist makes a leak less dangerous, not more:

* without it — key + secret leak, and anyone in the world can trade your account
* with it — key + secret leak, and the thief must also be on your IP

**The setting that actually protects you is leaving `Enable IDR & Crypto Withdrawal`
unchecked.** Even in total compromise — secret stolen *and* the attacker on your IP —
they can only place trades. They cannot move a single coin off the exchange. The worst
case becomes trading losses, not theft. Indodax also blocks API withdrawals for 24 hours
after any key regeneration, as a deliberate anti-theft delay.

**A note on CGNAT.** Many Indonesian ISPs put customers behind carrier-grade NAT, so a
single public IPv4 is shared by many subscribers at once. A traceroute showing private
hops (`10.x`, `172.16–31.x`) just past your router is the giveaway. If you are behind
CGNAT, your whitelisted IP already covers more people than just you — which dilutes the
protection, but still does not expose you, because your neighbours do not have your
secret. It is also why the IP churns: those pools get reshuffled.

**Hygiene worth keeping:**

1. Withdrawal permission stays off. Non-negotiable.
2. `.env` is gitignored — never commit it, never paste the secret into a chat or a
   screenshot.
3. Regenerate the key if you ever suspect the secret leaked; that invalidates the old
   one immediately.

#### What actually fixes it

| Option | Cost | Verdict |
|---|---|---|
| Update the whitelist when it changes | free | fine while paper trading; `doctor` catches it |
| **Run the bot on a VPS** | ~Rp 50–80rb/mo | **the real fix** — static IP, and stop-losses stop depending on your PC |
| Ask your ISP for a static IP | varies | works if offered; ask before paying for a VPS |
| VPN with a dedicated IP | ~$5/mo | works, but you are paying VPS money for less |

A tunnel — Cloudflare Tunnel, ngrok, Tailscale Funnel — **does not help here**, and this
is worth being clear about because it looks like it should. Those solve *inbound*
traffic: letting your phone reach your PC. The whitelist problem is *outbound*: which
source IP Indodax sees when the bot calls it. Opposite directions, different fix.

The VPS is the honest recommendation because it also removes the biggest operational
risk in this whole system: Indodax has no server-side stop-loss, so your stop only
exists while the engine process is running. A VPS runs it 24/7. One purchase, two
problems gone.

**You do not need any of this yet.** Paper trading needs no API key at all. Set the key
up when you are ready to go live — by then the VPS decision will have made itself.

### 4. Real crypto money — only after paper looks right

Create a key at [indodax.com/trade_api](https://indodax.com/trade_api) with **`view` +
`trade` ticked and `withdraw` OFF**, paste it into `.env`, then:

```
lumbung check-keys                    # read-only, places nothing
lumbung backtest                      # must print GATE: PASS
lumbung run --mode live --dry-run     # logs orders, sends none
start-live.bat                             # real money
```

---

## Every script

| File | What it does |
|---|---|
| **`START-HERE.bat`** | **dashboard + paper engine — start here** |
| `start-dashboard.bat` | dashboard only |
| `start-paper.bat` | paper engine only |
| `start-live.bat` | real orders, real money |
| `daily-report.bat` | one stock digest now |
| `install-schedule.bat` | schedule that digest for 16:15 daily |

Run `lumbung doctor` any time to see what is still missing.

---

## How the two halves differ

Two different things run here, on purpose:

| | Indodax (crypto) | Stockbit / gold / bonds |
|---|---|---|
| **Mode** | fully automatic | reminder only |
| **Places orders** | yes, by itself | never — you do |
| **Runs** | continuously, 24/7 | once a day, 16:15 WIB |
| **You do** | nothing after starting it | read the digest in the app, decide, act |

---

## What you need

1. **Nothing at all** to paper trade. It works right now.
2. **An Indodax API key** to trade for real —
   [indodax.com/trade_api](https://indodax.com/trade_api).
   **Tick `view` and `trade`. Leave `withdraw` OFF.** A bot never needs to move money
   off the exchange; if the key ever leaks, that one unticked box is what saves your
   balance.
3. **A machine that stays on** if you hold crypto positions. See the warning below.

Run this any time to see what is still missing:

```
lumbung doctor          # for paper
lumbung doctor --live   # stricter, for real money
```

It prints every check, the exact fix, and your next steps in order.

---

## Setting up

```bat
copy .env.example .env
notepad .env
```

Fill in what you have. Blank Indodax keys just means paper mode only.

---

## Running it

Double-click, or run from a terminal:

| File | What it does |
|---|---|
| `start-paper.bat` | paper trading — **start here**, no keys, no risk |
| `start-live.bat` | real orders, real money (asks you to confirm) |
| `daily-report.bat` | one stock/gold/bond digest |
| `install-schedule.bat` | registers the daily digest for 16:15 every day |
| `start-dashboard.bat` | the phone dashboard (see below) |

### The order to do things in

```
lumbung doctor              # 1. what is missing
lumbung backtest            # 2. must print GATE: PASS
start-paper.bat                  # 3. leave running ~72 hours
install-schedule.bat             # 4. daily stock reminders start arriving
                                 # 5. create the Indodax key, put it in .env
lumbung check-keys          # 6. read-only test, places nothing
lumbung run --mode live --dry-run   # 7. logs orders, sends none
start-live.bat                   # 8. real money
```

Do not skip step 2 or step 7.

---

## Running unattended

Three pieces are registered on this machine:

| What | Where | When |
|---|---|---|
| Dashboard | `shell:startup` → `Lumbung-Dashboard.vbs` | every logon, hidden |
| Paper engine | `shell:startup` → `Lumbung-Engine.vbs` | every logon, hidden |
| Daily digest | Task Scheduler → `Lumbung-Daily` | 16:15 daily |

The two launchers are VBScript rather than shortcuts because `WScript.Shell.Run`
with a window style of `0` starts them genuinely invisibly — no console flash at
logon, nothing in the taskbar.

**The engine launcher passes `--mode paper` explicitly.** That is deliberate: it
means `TA_MODE=live` in `.env` can never silently turn the autostarted engine
into one spending real money. Going live stays a manual act.

### Only one engine may run

The engine takes a PID lock at `data/engine.pid`. Start a second one -- by
double-clicking a launcher while the autostarted one is up -- and it refuses:

```
An engine is already running (pid 48084).
  It probably started at logon. Stop that one first, or just use the
  running instance — two engines would double every order.
```

Two engines would share one journal and one exchange account, size positions
from the same balance, and both send the orders. A stale lock left by a crash is
detected and taken over, so a hard kill never wedges you out permanently.

### Turning it off

```powershell
# stop autostart
Remove-Item "$([Environment]::GetFolderPath('Startup'))\Lumbung-*.vbs"

# stop the daily digest
schtasks /delete /tn "Lumbung-Daily" /f

# stop what is running now
Get-Process pythonw | Stop-Process -Force
```

### A note on pythonw

`pythonw.exe` has no console, so `sys.stdout` and `sys.stderr` are `None`. Any
print, any rich render, or uvicorn's own log handlers then raise — and the
process dies silently with no window and no log to explain why. The CLI now
redirects both streams to `logs/headless.log` before anything writes, which is
where to look if an autostarted process misbehaves.


---

## ⚠️ The one thing that can actually hurt you

**Indodax has no server-side stop-loss.** The stop is enforced by this program checking
the price every ~20 seconds. **If the program is not running, you have no stop-loss** —
an open position has unlimited downside while your PC is off.

So, pick one:

- leave `start-live.bat` running (a laptop that sleeps is *not* running), or
- run it on a small VPS (~Rp 50rb/month), or
- close positions before shutting down: `lumbung flat --mode live`

`lumbung doctor` warns you when the heartbeat has gone stale, and the bot writes to
the app chat if its loop stalls for more than 5 minutes.

---

## The phone dashboard (PWA)

An installable app for your phone, without the Play Store, a Firebase project, or a
signing keystore. It is the whole interface: alerts, chat and kill switch, plus the
things a chat window is bad at — allocation bars, an equity curve, goal progress.

```bat
start-dashboard.bat
```

It prints three links. Open one on your phone, then **Chrome menu → "Add to Home
screen"**. You get an icon, a fullscreen app with no browser bars, and an offline shell.

```
this PC     http://127.0.0.1:8787/?t=<token>
same WiFi   http://192.168.1.9:8787/?t=<token>
Tailscale   http://<your-tailscale-name>:8787/?t=<token>
```

The `?t=…` token is saved on the device and then stripped from the URL, so it does not
sit in your history. Set `DASHBOARD_TOKEN` in `.env` to a fixed value — otherwise a new
one is generated on every restart and you have to re-pair the phone.

### Home WiFi now (Tailscale later)

You only need the local IP while you are at home. Nothing to install.

**1. Allow the port through Windows Firewall — once, in an Administrator PowerShell:**

```powershell
New-NetFirewallRule -DisplayName "Lumbung Dashboard" `
  -Direction Inbound -Protocol TCP -LocalPort 8787 -Action Allow -RemoteAddress LocalSubnet
```

Without this your phone just times out. Connections from the PC itself bypass the
firewall, so the dashboard looks perfectly healthy here while every other device is
blocked — that is almost always the reason "it works on my PC but not my phone".

`-RemoteAddress LocalSubnet` scopes it to your home network, so the port stays
unreachable from the internet even if the laptop later joins a café WiFi.
`start-dashboard.bat` checks for this rule and reminds you if it is missing.

**2. Start it:**

```bat
start-dashboard.bat
```

**3. On your phone**, open the `same WiFi` link it prints, then Chrome menu →
**Add to Home screen**.

### If the phone cannot open it

Read the exact Chrome error — the two failures have opposite causes:

| Chrome says | Cause | Fix |
|---|---|---|
| `ERR_CONNECTION_REFUSED` | nothing is listening | the dashboard window is closed — run `START-HERE.bat` |
| `ERR_CONNECTION_TIMED_OUT` | it is running, the firewall is dropping packets | add the firewall rule above |
| `ERR_ADDRESS_UNREACHABLE` | wrong IP, or phone is on mobile data | rejoin home WiFi; re-read the `same WiFi` line |

"Refused" means a machine answered and said no port is open there. "Timed out" means the
packet disappeared into a firewall. They are never the same problem.

Check from the PC first — if this fails, the phone was never going to work:

```
curl "http://127.0.0.1:8787/api/summary?t=<your token>"
```

**The dashboard window must stay open.** It is the server. Closing it is what produces
connection-refused.

#### If the address stops working

Your PC gets its IP from the router by DHCP, so it can change after a reboot or a power
cut. If the app suddenly cannot connect, run `start-dashboard.bat` again and read the
new `same WiFi` line. To stop it moving, set a DHCP reservation for this PC in your
router's admin page.

The phone only reaches the dashboard while it is on your home WiFi — and since the
dashboard is now the only interface, away from home you reach nothing. Fix that with
Tailscale using the section below, or with the Cloudflare tunnel described in
`tools/setup-tunnel.md`. Until then, alerts raised while you are out are waiting in the
chat when you get back rather than arriving as they happen.

### Reaching it from outside the house — Tailscale

1. Install Tailscale on the PC and on your phone, sign into both with the same account.
2. On the PC run `tailscale ip -4` (or read the machine name in the app).
3. Open `http://<that-name-or-ip>:8787/` on the phone.

Your PC is never exposed to the public internet — no port forwarding, no certificates,
no login page for strangers to find. The bearer token stays required anyway, because
"it's only on my private network" is the assumption that quietly stops being true.

### What the dashboard will and will not do

| | |
|---|---|
| Shows | net worth, allocation vs target, goal progress, cash flow, holdings with P&L and sell verdicts, bot equity curve, open positions, recent activity |
| Controls | Pause / Resume / Flat / Kill |
| Does **not** do | push notifications — Android kills background web pages, so an alert is waiting when you open the app rather than buzzing when it is raised |

Flat and Kill do not sell anything themselves. They raise a flag that the **engine**
consumes on its next loop, so only one process ever talks to the exchange. Two writers
racing on the same positions is how you end up selling twice.

The service worker caches the app shell but **never** an API response — a stale
portfolio number is worse than an honest "offline".

---

## Controlling it from your phone

In the app chat, on a dashboard started without `--readonly`:

| Command | What it does |
|---|---|
| `/status` | equity, positions, drawdown, day P&L |
| `/positions` | every open position with live P&L |
| `/pnl` | realised P&L over 24h / 7d / 30d |
| `/pause` | stop opening new trades, keep managing open ones |
| `/resume` | start again (rebases the drawdown baseline) |
| `/flat` | sell everything at market, now |
| `/kill` | flatten and halt completely |

---

## What arrives, and when

**Crypto — written as it happens:** every buy, every sell, every stop, every halt.
Usually a handful of messages a month; the strategy averages ~5 trades a month. They
are in the chat whether or not the app was open at the time.

**Stocks, gold, bonds — one digest at 16:15 WIB:** holdings with P&L, any sell rules
that fired, new screen ideas, and total income versus the subscription. Daily on purpose
— a daily-bar strategy has nothing new to say intraday, and over-alerting just teaches
you to ignore it.

You act on the stock ones yourself. A useful shortcut: Stockbit's own **Auto Order**
takes the entry, stop and target from the message, then executes without you watching.
So you approve each trade once and never sit at a screen.

---

## Reading the money side

```
lumbung plan            # net worth, allocation, emergency fund, goal
lumbung portfolio       # holdings P&L + dividend income
lumbung recommend       # where to put idle cash
lumbung sell-check      # trim/exit rules on what you hold
lumbung report          # how the crypto bot has actually done
```

`plan` is the one worth reading regularly. At a high savings rate, your monthly surplus
moves your net worth far more than any trading result does.
