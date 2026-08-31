# Lumbung

*Lumbung* is the rice granary — where a harvest is stored so it feeds you for
months afterwards. That is what this is: a patient store built from surplus, not a
machine for making money quickly. The strategy trades about five times a month, holds
for days or weeks, and earns almost everything in two or three months a year.

Automated crypto trading on **Indodax**, signal-only alerts for **IDX stocks**, and
whole-portfolio planning.

---

## Read this before anything else

Can this pay for a recurring subscription (~$20/mo ≈ **Rp 330rb**)
from Rp 10.000.000? Here is what four years of backtesting actually says.

**Measured over 48 months, 14 pairs, 4h bars, with real fees, the 0.21% sell tax and
every risk gate applied:**

| | |
|---|---|
| Average monthly return | **+1.23%** |
| Max drawdown | **-17.4%** |
| Win rate | 35% |
| Profit factor | 1.65 |
| Trades | 257 (~5/month) |
| Positive months | **47%** |
| Median month | **+0.00%** |
| Longest drawdown | **318 days** |
| Top 3 months | **+47.4% of the entire +80% total** |

Three consequences you should internalise before funding this:

1. **It does not earn Rp 330rb per month.** At +1.23%/month, Rp 10jt produces about
   **Rp 123rb/month** — roughly a third of that. Covering the full subscription
   at this rate needs about **Rp 27jt** of capital, not Rp 10jt.
2. **Most months earn nothing.** The median month is flat. Profit arrives in two or
   three explosive months a year and you have to still be running when they come.
3. **A year underwater is normal.** The longest historical drawdown was 318 days.
   That is the strategy working as designed, not a malfunction.

This is a real, positive edge — but it is a slow, lumpy one. If you need Rp 330rb
every month reliably, no honest backtest here supports that, and any bot that claims
to is overfitted.

---

## What it does and does not do

| | Crypto (Indodax) | Stocks (Stockbit) |
|---|---|---|
| Places orders | **Yes**, automatically | **No, never** |
| How | Documented Trade API (`/tapi`) | App alert; you type it into Auto Order |
| Why | Real API, view+trade keys | Stockbit has no official retail trading API |

Stockbit only offers in-app Auto Order. Driving its private API or the browser on a
live securities account is fragile and against its terms, so this bot does not do it.
`lumbung stocks` gives you exact entry / stop / target / lot numbers to enter
yourself.

### Holdings you already own

Record real positions in `config/holdings.yaml`, then:

```bash
lumbung portfolio            # P&L, dividend income, trend read, alerts
lumbung portfolio --notify   # same, into the app chat
```

This monitors — it never trades stocks. It exists because the numbers that matter most
are easy to lose track of inside a broker app: yield on cost vs yield on market, how far
a recovery actually has to travel, and how a single holding compares to everything else
you own.

**Worth knowing before you tune the bot:** a dividend-paying holding can quietly out-earn
the trading bot. A single large-cap bank holding's trailing dividend can pay ~Rp 300rb/month
— about 90% of the subscription — while the bot on a Rp 3jt sleeve models to ~Rp 37rb/month. Check
`lumbung portfolio` before deciding where effort is best spent.

---

## Stocks: what to buy, what to sell, and when you hear about it

Three commands, all read-only. **The bot never places a stock order** — you type every
one into Stockbit yourself.

```bash
lumbung recommend       # screen IDX for idle cash, income-weighted
lumbung sell-check      # rule-based trim/exit review of what you hold
lumbung goal            # what a monthly income target actually costs
lumbung daily           # all of the above as one digest in the app chat
```

### `recommend` — and the trap it exists to avoid

A high dividend yield is usually a *symptom*: yield rises when the price falls, so the
top row of a naive yield screen is often a company in trouble, or one that paid a
one-off special that will not repeat. So yield is capped at 10% of credit and is only
35 of 100 points. The rest is consistency (paid in each of the last 5 years), stability
(how much the annual payment swings), sustainability (payout ratio), trend, and
diversification against what you already own. Every row shows its score breakdown and
its flags.

Sector diversification is enforced in the basket builder: one name per sector, so a
screen full of banks cannot hand you three more banks when you already own one.

### `sell-check` — rules, not instructions

It never emits a bare "SELL". It reports which named rules fired:

| Rule | Severity | Meaning |
|---|---|---|
| concentration | act | one position exceeds 40% of the portfolio |
| dividend cut | act | TTM payment down >30% versus the prior year |
| payout unsustainable | act | paying out more than it earns |
| trend broken | watch | EMA50 below EMA200 |
| near 52w low | watch | within 2% of the 52-week low |
| paid to wait | info | yield on today's price |

It also reports **business intact: yes/no** — separating "the price fell" from "the
company broke", which are different problems with different answers. A falling price
with a growing dividend is a *watch*, not an emergency.

### Scheduling — how often you get told

`lumbung daily` is designed to run **once per trading day after the IDX close**
(~16:15 WIB). It writes one message into the app chat: crypto engine state, holdings
with sell verdicts, new screen ideas, and total income versus the subscription.

```powershell
schtasks /create /tn "Lumbung-Daily" /sc daily /st 16:15 /tr "C:\Users\akbar\AppWork\Lumbung\.venv\Scripts\python.exe -m lumbung.cli daily"
```

Crypto alerts are separate and immediate — the engine messages you on every fill, stop
and halt, as they happen. Stocks are a once-a-day digest on purpose: a daily-bar
strategy has nothing new to say intraday, and alerting more often just trains you to
ignore it.

---

## `lumbung plan` — the command that actually matters

Once a monthly surplus is in the picture, "which stock" stops being the important
question. `plan` shows the whole balance sheet:

- **allocation drift** — every bucket versus your target in `holdings.yaml`
- **emergency fund** — months of spending covered by cash, and by cash + gold
- **surplus routing** — where next month's money goes
- **the goal** — years to your passive-income target, with the trajectory

### Why new money, not selling, does the rebalancing

When fresh cash arrives every month, an overweight bucket can be fixed by simply not
buying more of it, while the underweight ones get the new money. That avoids realising
a loss, avoids the 0.1% PPh charged on IDX sale proceeds, and avoids a tax event
entirely. `allocate_surplus()` therefore splits the surplus across the underweight
buckets in proportion to their shortfall, and sends **nothing** to buckets already at
or above target.

Selling only becomes the right tool when a position is so concentrated that waiting for
contributions to dilute it would take years.

### Saving versus returns, at this size

The planner prints your surplus as a multiple of your dividend income and as a
percentage of net worth per year. For a saver putting aside more than half their
income those can read 34x and 113% — which
is the whole point. A year of saving adds more than the entire portfolio is worth. No
plausible improvement in strategy competes with that, and it is worth re-reading
whenever the temptation arises to take more risk for a better return.

---

## Setup

Already set up? Just double-click **START-HERE.bat**. From scratch:

```bash
uv venv --python 3.13
uv pip install -e ".[dev]"
cp .env.example .env       # then fill it in
```

### Indodax API key

Create at **https://indodax.com/trade_api**.

> **Enable `view` + `trade` only. Never enable `withdraw`.**
> A trading bot has no reason to move funds off the exchange. If the key leaks, this
> is the single setting that saves your balance. IP-whitelist it if you have a static IP.

Verify it without placing any order:

```bash
lumbung check-keys     # read-only getInfo; warns if the key can withdraw
```

### Alerts

Alerts go into the app chat. There is nothing to configure: the engine appends them
to `data/answers.jsonl`, the dashboard streams that file, and the dashboard is an
installable PWA — so a halt raised at 02:00 is on the phone in the morning whether
or not anything was serving at the time.

A 24/7 crypto bot you cannot see or stop from your phone is a liability, which is
why `/status`, `/positions` and `/pnl` are answerable from the chat, and
`/pause`, `/resume`, `/flat` and `/kill` are too on a dashboard not started
`--readonly`.

Commands: `/status` `/positions` `/pnl` `/pause` `/resume` `/flat` `/kill` `/help`

---

## Usage

```bash
lumbung sync                    # download 4 years of candles (~30s)
lumbung backtest                # the go/no-go gate
lumbung run --mode paper        # paper trade against the live book
lumbung run --mode live --dry-run   # live data, logs orders, sends none
lumbung run --mode live         # real money (asks for confirmation)

lumbung status                  # engine state + recent events
lumbung report --days 30        # realized P&L from the journal
lumbung stocks --notify         # IDX scan -> app chat
lumbung portfolio               # holdings P&L + dividend income + alerts
lumbung plan                    # net worth, allocation, surplus, goal
lumbung dashboard               # phone dashboard (installable PWA)
lumbung doctor                  # what is still missing before you run
lumbung verify-costs            # real fees vs the configured model
lumbung halt                    # emergency stop (creates the HALT file)
lumbung flat --mode live        # close everything at market, now
```

---

## Strategy

Donchian breakout with a trend filter, long-only (Indodax spot has no shorting).

- **Entry** — close breaks the prior 55-bar high **and** EMA50 > EMA200 **and** ADX > 25
- **Stop** — `entry − 4.0 × ATR(14)`, monitored locally, exits at market
- **Trail** — chandelier: `highest close since entry − 4.5 × ATR`, ratchets up only
- **Exit** — EMA50 crosses below EMA200
- **No partial take-profit.** Tested both ways: taking half off at +1.5R turned a
  +22.8% out-of-sample run into +16.6%. Trend following earns everything from a few
  huge winners, so capping them removes the profit.

Parameters come from a walk-forward sweep (`scripts/sweep.py`, `scripts/sweep_risk.py`)
and sit on a broad plateau — 88 of 108 configurations were profitable in both the
in-sample and out-of-sample windows. Neighbouring settings behave similarly, which is
what makes them worth trusting.

### Costs (verified against the live API on 2026-08-23)

| Component | Rate |
|---|---|
| Maker fee | 0.10% |
| Taker fee | 0.20% |
| PPh final on sells | 0.21% (PMK 50/2025; PPN was abolished) |
| **Post-only round trip** | **~0.51%** |
| **Stop-loss round trip** | **~0.86%** (taker + spread) |

Entries and planned exits are **post-only (`time_in_force: MOC`)** so they rest on the
book and pay the maker fee. Only stop-losses cross the spread.

> The Indodax help-centre article claims 0% maker / 0.3% taker, which contradicts the
> `/api/pairs` metadata used above. `lumbung verify-costs` reconciles both against
> your real fills once you have some.

---

## Risk controls

Every order path calls the same gates in `risk.py`. There is no override flag.

| Gate | Default | Action |
|---|---|---|
| Risk per trade | 1% of sleeve | position sized from the stop distance |
| Max position | 25% of sleeve | size down |
| Max concurrent positions | 6 | block new entries |
| Max total exposure | 75% of sleeve | block new entries |
| Daily loss limit | -3% | no new entries until 00:00 WIB |
| **Max drawdown** | **-20%** | **flatten everything + halt + alert** |
| Kill switch | `HALT` file or `/kill` | flatten + halt |

**Why 20% and not 10%.** The strategy's own worst historical drawdown is 17.4% and it
spends 53% of its life more than 5% below peak — drawdown is its normal operating
state. A 10% halt fired within 9–12 months in *every* backtest and killed the strategy
before its payoff months arrived. 20% clears the historical worst with margin while
still capping the damage. On a Rp 3jt sleeve that is Rp 600rb of maximum designed loss.

Resuming after a halt is deliberately manual (`/resume`) and **rebases the equity peak**
to current equity. Without that rebase, resuming re-trips the drawdown gate on the next
tick and the engine deadlocks permanently.

---

## The uptime requirement — read this

**Indodax has no server-side stop-loss order.** The stop is monitored by this process
against the live price every ~20 seconds and fires a market sell when breached.

**If the process is not running, you have no stop.** An open position has unbounded
downside while the bot is down. Either:

- keep it running on a small VPS (~Rp 50rb/month), or
- run it as a Windows service, or
- close your positions before shutting the machine down (`lumbung flat`).

A heartbeat is written to SQLite every loop; `lumbung status` flags it as STALE
past `heartbeat_stale_sec`.

### Run as a Windows scheduled task

```powershell
schtasks /create /tn "Lumbung" /sc onstart /rl highest ^
  /tr "C:\Users\akbar\AppWork\Lumbung\.venv\Scripts\python.exe -m lumbung.cli run --mode live"
```

---

## Rollout ladder — gates, not dates

| Phase | Capital | Gate to advance |
|---|---|---|
| 1 | Rp 0 | `lumbung backtest` prints GATE: PASS |
| 2 | Rp 0 (paper) | 72h paper run, no crashes, journal matches the log |
| 3 | **Rp 500rb**, dry-run first | real fills and fees match the model (`verify-costs`) |
| 4 | **Rp 3jt** | 2 weeks live, max drawdown < 10% |
| 5 | Rp 6jt → Rp 10jt | 4 weeks live, positive, drawdown behaving as modelled |

Set the sleeve in `config/config.yaml` → `capital.sleeve_idr`. It is the amount the bot
may trade, not your account balance.

---

## Layout

```
src/lumbung/
  config.py                  typed config + secrets
  exchanges/indodax_private.py   signed TAPI client (HMAC-SHA512)
  exchanges/indodax_public.py    book, ticker, pair metadata, OHLCV
  data/candles.py            SQLite candle cache
  data/idx.py                IDX stock signals (alerts only)
  holdings.py                monitoring for stocks you already own
  screener.py                IDX income screen + basket builder
  goal.py                    income-target maths + sell rules
  networth.py                allocation, emergency fund, surplus routing
  doctor.py                  pre-flight checks
  web/                       FastAPI dashboard + installable PWA
  strategy/                  indicators + donchian_trend
  risk.py                    sizing + every hard gate
  execution/broker.py        LiveBroker | PaperBroker, one interface
  backtest.py                event-driven simulator
  engine.py                  the live loop
  journal.py                 SQLite source of truth
  notify/app.py              alerts, written into the chat transcript
  chat.py                    every command, one dispatcher
  cli.py
scripts/                     sweep.py, sweep_risk.py, candidates.py, bt.py
tests/                       116 tests
```

Run the suite with `pytest`.

---

## Honest limitations

- Backtested on 2022–2026, which contains one full bull cycle. A different regime can
  behave differently, and 4 years is a small sample for a strategy that profits in ~3
  months per year.
- 14 crypto pairs are highly correlated; this is far less diversification than the
  count suggests.
- Paper mode simulates fills against the real book but cannot model queue position, so
  live fills will be slightly worse.
- The 0.21% PPh applies to every sell regardless of profit.
- Backtest performance does not carry forward. The drawdown halt protects capital; the
  backtest does not.
