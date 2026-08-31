"""Command line interface. `lumbung --help` lists everything."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .config import PROJECT_ROOT, get_secrets, load_config, load_watchlist
from .data import candles as candles_mod
from .exchanges.indodax_private import IndodaxError
from .exchanges.indodax_public import IndodaxPublicClient
from .exchanges.indodax_v2 import DryRunV2Client, IndodaxV2Client
from .execution.broker import LiveBroker, PaperBroker
from .journal import Journal
from .notify.app import ConsoleNotifier, build_notifier

# Under pythonw.exe (no console -- how the autostart launcher runs us) both
# sys.stdout and sys.stderr are None. Any print, any rich render, and uvicorn's
# own log handlers then raise, and the process dies silently with no window and
# no log to explain it. Give them a real destination before anything writes.
if sys.stdout is None or sys.stderr is None:
    _logdir = Path(__file__).resolve().parents[2] / "logs"
    _logdir.mkdir(parents=True, exist_ok=True)
    _sink = open(_logdir / "headless.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = _sink
    if sys.stderr is None:
        sys.stderr = _sink

# Windows consoles default to cp1252, which cannot encode the block characters
# and arrows used below -- rich then dies with UnicodeEncodeError mid-render.
# Force UTF-8 on the streams before rich touches them.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, OSError, ValueError):  # not a real TTY, or already a file
        pass

app = typer.Typer(
    add_completion=False,
    help="Lumbung - the granary. Automated Indodax crypto trading, IDX stock "
         "signals, and whole-portfolio planning.",
)
console = Console()


def _setup_logging(verbose: bool = False) -> None:
    cfg = load_config()
    cfg.log_path.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(cfg.log_path / "lumbung.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _rupiah(v: float) -> str:
    return f"Rp {v:,.0f}"


def _amount(raw: str) -> float:
    """Read '5jt', '500rb', '1.500.000' or '1500000' as rupiah.

    People type the short forms, so the short forms have to work. Shared by
    every command that takes money rather than reimplemented three times with
    three slightly different sets of accepted spellings.
    """
    t = str(raw).strip().lower().replace(",", "").replace(".", "").replace(" ", "")
    mult = 1_000_000 if "jt" in t else (1_000 if "rb" in t else 1)
    t = t.replace("jt", "").replace("rb", "")
    if not t:
        raise ValueError(f"could not read {raw!r} as an amount")
    return float(t) * mult


# ---------------------------------------------------------------- data & test
@app.command()
def sync(months: float = 48.0, timeframe: str = "") -> None:
    """Download and cache OHLCV candles for the configured universe."""
    _setup_logging()
    cfg = load_config()
    tf = timeframe or cfg.universe.timeframe
    pub = IndodaxPublicClient()
    conn = candles_mod.connect(cfg.db_path)
    for pair in cfg.universe.pairs:
        n = candles_mod.sync(pub, conn, pair, tf, months=months)
        lo, hi = candles_mod.coverage(conn, pair, tf)
        console.print(
            f"  {pair:11s} +{n:5d} rows   "
            f"{time.strftime('%Y-%m-%d', time.gmtime(lo or 0))} → "
            f"{time.strftime('%Y-%m-%d', time.gmtime(hi or 0))}"
        )


@app.command("check-keys")
def check_keys() -> None:
    """Verify Indodax credentials with a read-only getInfo call. Places no orders."""
    _setup_logging()
    sec = get_secrets()
    if not sec.has_indodax:
        console.print("[red]No Indodax credentials.[/] Copy .env.example to .env and fill it in.")
        raise typer.Exit(1)
    client = IndodaxV2Client(
        sec.indodax_key.get_secret_value(), sec.indodax_secret.get_secret_value()
    )
    client.sync_time()
    try:
        info = client.get_info()
    except IndodaxError as exc:
        console.print(f"[red]Indodax rejected the key:[/] {exc.message}")
        m = str(exc.message).lower()
        if "api key version" in m:
            console.print("  That looks like a v1 key. Generate a Trade API V2 key instead.")
        elif "invalid credentials" in m or "not found" in m:
            console.print("  Check the key and secret are pasted whole, with no stray spaces.")
        elif "ip" in m:
            console.print("  Your IP may not be whitelisted. Run: lumbung check-ip")
        raise typer.Exit(1) from None

    console.print("[green]Credentials OK[/] [dim](Trade API v2)[/]")
    console.print(f"  user id     : {info.get('user_id')}")
    console.print(f"  account     : {info.get('account_type')}")
    console.print(f"  can trade   : {'[green]yes[/]' if info.get('can_trade') else '[red]NO[/]'}")
    console.print(
        "  can withdraw: "
        + ("[red]YES[/]" if info.get("withdraw_status") == 1 else "[green]no (good)[/]")
    )
    if info.get("withdraw_status") == 1:
        console.print(
            "\n[yellow]This key can withdraw funds.[/] A trading bot never needs that. "
            "Recreate it with view + trade only."
        )
    if not info.get("can_trade"):
        console.print("\n[yellow]Trading is disabled on this key — it can only read.[/]")
    balances = {k: float(v) for k, v in info.get("balance", {}).items() if float(v) > 0}
    if balances:
        t = Table("asset", "balance")
        for k, v in sorted(balances.items()):
            t.add_row(k.upper(), f"{v:,.8f}".rstrip("0").rstrip("."))
        console.print(t)
    else:
        console.print("  [dim]all balances zero[/]")


# -------------------------------------------------------------------- backtest
@app.command()
def backtest(
    months: float = 48.0,
    timeframe: str = "",
    monthly: bool = typer.Option(True, help="Print the month-by-month table"),
) -> None:
    """Backtest the configured strategy. This is the go/no-go gate before live."""
    _setup_logging()
    from .backtest import Backtester

    cfg = load_config()
    tf = timeframe or cfg.universe.timeframe
    conn = candles_mod.connect(cfg.db_path)
    pub = IndodaxPublicClient()
    cutoff = time.time() - months * 30.44 * 86400
    data = {p: candles_mod.load(conn, p, tf, start=int(cutoff)) for p in cfg.universe.pairs}
    data = {p: d for p, d in data.items() if not d.empty}
    if not data:
        console.print("[red]No candles.[/] Run `lumbung sync` first.")
        raise typer.Exit(1)
    ticks = {p: pub.price_increment(p) for p in data}

    res = Backtester(cfg).run(data, ticks=ticks)
    s = res.summary()

    t = Table("metric", "value")
    t.add_row("period", f"{s['months']} months, {len(data)} pairs, {tf} bars")
    t.add_row("start equity", _rupiah(s["start_equity"]))
    t.add_row("end equity", _rupiah(s["end_equity"]))
    t.add_row("total return", f"{s['total_return_pct']:+.2f}%")
    t.add_row("avg monthly", f"{s['monthly_return_pct']:+.2f}%")
    t.add_row("max drawdown", f"{s['max_drawdown_pct']:.2f}%")
    t.add_row("trades", str(s["trades"]))
    t.add_row("win rate", f"{s['win_rate_pct']:.1f}%")
    t.add_row("profit factor", f"{s['profit_factor']:.2f}")
    t.add_row("avg R", f"{s['avg_r']:+.3f}")
    if s["halted_at"]:
        t.add_row("[red]HALTED[/]", s["halt_reason"])
    console.print(t)

    if monthly:
        col = res.monthly_table().set_index("month")["return_pct"]
        console.print("\n[bold]monthly returns[/]")
        for m, v in col.items():
            bar = ("[green]" + "█" * int(v) if v > 0 else "[red]" + "█" * int(-v)) + "[/]"
            console.print(f"  {m}  {v:+7.2f}%  {bar}")
        console.print(
            f"\n  {len(col)} months · [green]{100 * (col > 0).mean():.0f}% positive[/] · "
            f"median {col.median():+.2f}% · best {col.max():+.2f}% · worst {col.min():+.2f}%"
        )
        top3 = col.nlargest(3).sum()
        console.print(
            f"  [yellow]top 3 months = {top3:+.1f}% of the total[/] — "
            "trend following earns almost everything in a few months."
        )

    # The go/no-go gate, evaluated rather than eyeballed.
    ok = s["total_return_pct"] > 0 and s["trades"] >= 40 and s["profit_factor"] > 1.2
    console.print(
        f"\n[{'green' if ok else 'red'}]GATE: {'PASS' if ok else 'FAIL'}[/] "
        "(needs positive return, >=40 trades, profit factor > 1.2)"
    )


# ------------------------------------------------------------------- trading
def _build(mode: str, dry_run: bool):
    cfg = load_config()
    sec = get_secrets()
    journal = Journal(cfg.db_path)
    pub = IndodaxPublicClient()
    # Alerts land in the in-app chat. The engine never talks to the web server
    # directly -- it appends to the transcript file, so an alert raised while
    # nothing is serving is still there when the app is next opened.
    notifier = build_notifier(cfg.data_dir)

    if mode == "live":
        if not sec.has_indodax:
            console.print("[red]Live mode needs Indodax credentials in .env[/]")
            raise typer.Exit(1)
        cls = DryRunV2Client if dry_run else IndodaxV2Client
        client = cls(sec.indodax_key.get_secret_value(), sec.indodax_secret.get_secret_value())
        client.sync_time()
        broker = LiveBroker(client, pub)
    else:
        state = journal.get_state("paper_broker")
        broker = PaperBroker(
            pub, cfg.costs, starting_idr=cfg.capital.sleeve_idr, state=state
        )
    return cfg, journal, pub, broker, notifier


@app.command()
def run(
    mode: str = typer.Option(
        "", help="paper | live. Defaults to TA_MODE in .env, else paper."
    ),
    dry_run: bool = typer.Option(False, help="live mode: log orders instead of sending them"),
    once: bool = typer.Option(False, help="Run a single loop iteration and exit"),
    verbose: bool = False,
) -> None:
    """Run the trading engine (paper or live)."""
    _setup_logging(verbose)
    # An explicit --mode always wins; TA_MODE only supplies the default. Live
    # still requires the confirmation prompt below, so an env var alone can
    # never quietly start spending real money.
    from .config import env_mode

    mode = (mode or env_mode() or "paper").lower()
    if mode not in ("paper", "live"):
        console.print(f"[red]mode must be 'paper' or 'live', got {mode!r}[/]")
        raise typer.Exit(1)

    from .engine import Engine

    cfg, journal, pub, broker, notifier = _build(mode, dry_run)

    if mode == "live" and not dry_run:
        console.print(
            f"\n[bold red]LIVE MODE[/] — real orders with real money.\n"
            f"  sleeve      {_rupiah(cfg.capital.sleeve_idr)}\n"
            f"  risk/trade  {cfg.risk.risk_per_trade_pct:.1%}\n"
            f"  DD halt     {cfg.risk.max_drawdown_pct:.0%}\n"
        )
        # No stdin under pythonw (the logon autostart), so an interactive prompt
        # is not a safety check there -- it is a crash. The deliberate act in
        # that path is having set TA_MODE=live; announce it loudly instead.
        interactive = sys.stdin is not None and sys.stdin.isatty()
        if interactive:
            if not typer.confirm("Start live trading?", default=False):
                raise typer.Exit(0)
        else:
            console.print(
                "[yellow]Starting live without a prompt — no terminal attached "
                "(TA_MODE=live). Set TA_MODE=paper in .env to stop this.[/]"
            )
            log = logging.getLogger(__name__)
            log.warning("LIVE trading started headless via TA_MODE=live")

    # One engine per machine. Two would share a journal and an exchange account,
    # size positions from the same balance, and both place the orders.
    from .singleton import AlreadyRunning, InstanceLock

    lock = InstanceLock(cfg.db_path.parent / "engine.pid")
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        console.print(f"[red]An engine is already running (pid {exc.pid}).[/]")
        console.print(
            "  It probably started at logon. Stop that one first, or just use the "
            "running instance — two engines would double every order."
        )
        console.print(f"  [dim]lock file: {exc.path}[/]")
        raise typer.Exit(1) from None

    engine = Engine(cfg, journal, broker, pub, notifier, mode=mode)

    try:
        if once:
            engine.reconcile()
            st = engine.run_once()
            console.print(st)
        else:
            engine.run_forever()
    finally:
        if isinstance(broker, PaperBroker):
            journal.set_state("paper_broker", broker.export_state())
        lock.release()


@app.command()
def status() -> None:
    """Print engine state from the journal without starting the engine."""
    _setup_logging()
    cfg = load_config()
    j = Journal(cfg.db_path)
    pos = j.positions()
    hb = j.seconds_since_heartbeat()
    t = Table("field", "value")
    t.add_row("mode", str(j.get_state("mode", "-")))
    t.add_row("halted", str(j.get_state("halted", False)))
    t.add_row("halt reason", str(j.get_state("halt_reason", "") or "-"))
    t.add_row("peak equity", _rupiah(float(j.get_state("peak_equity", 0) or 0)))
    t.add_row(
        "heartbeat",
        "never" if hb == float("inf") else f"{hb:.0f}s ago"
        + ("  [red](STALE)[/]" if hb > cfg.execution.heartbeat_stale_sec else ""),
    )
    t.add_row("open positions", str(len(pos)))
    console.print(t)
    if pos:
        pt = Table("pair", "qty", "entry", "stop")
        for p in pos.values():
            pt.add_row(p.pair, f"{p.qty:.8f}", f"{p.entry_price:,.0f}", f"{p.stop:,.0f}")
        console.print(pt)
    ev = j.recent_events(15)
    if ev:
        et = Table("time", "kind", "message")
        for e in reversed(ev):
            et.add_row(time.strftime("%m-%d %H:%M", time.localtime(e["ts"])), e["kind"], e["msg"][:70])
        console.print(et)


@app.command()
def flat(mode: str = "paper", yes: bool = typer.Option(False, "--yes")) -> None:
    """Close every open position at market, right now."""
    _setup_logging()
    if not yes and not typer.confirm("Close ALL positions at market?", default=False):
        raise typer.Exit(0)
    from .engine import Engine

    cfg, journal, pub, broker, notifier = _build(mode, False)
    engine = Engine(cfg, journal, broker, pub, notifier, mode=mode)
    engine.reconcile()
    engine.flatten_all(reason="cli_flat")
    console.print("[green]Flatten requested.[/] Check `lumbung status`.")


# --------------------------------------------------------------------- stocks
@app.command()
def stocks(budget: float = 0.0, notify: bool = typer.Option(False, help="Send to the app chat")) -> None:
    """Scan the IDX watchlist and print Auto Order instructions. Places no orders."""
    _setup_logging()
    cfg = load_config()
    tickers = load_watchlist()
    console.print(f"Scanning {len(tickers)} IDX tickers…")
    from .data.idx import fetch_daily, scan

    data = fetch_daily(tickers)
    sigs = scan(data, cfg.stocks, budget_idr=budget or cfg.stocks.budget_idr)
    if not sigs:
        console.print("[dim]No stock signals today.[/]")
        return
    j = Journal(cfg.db_path)
    notifier = build_notifier(cfg.data_dir) if notify else ConsoleNotifier()
    for s in sigs:
        msg = s.as_message(cfg.stocks.fee_buy_pct, cfg.stocks.fee_sell_pct)
        console.print(msg + "\n")
        j.record_stock_signal(
            ticker=s.ticker, action=s.action, entry=s.entry, stop=s.stop,
            target=s.target, lots=s.lots, reason=s.reason,
        )
        if notify:
            notifier.send(msg)


@app.command()
def portfolio(notify: bool = typer.Option(False, help="Send the summary to the app chat")) -> None:
    """Full picture: stock holdings, dividends, crypto sleeve, and alerts.

    Monitors only. The bot never places a stock order.
    """
    _setup_logging()
    logging.getLogger("yfinance").setLevel(logging.ERROR)
    cfg = load_config()
    from .holdings import SUBSCRIPTION_IDR, PortfolioSummary, analyse, load_holdings

    holds, cash, alerts = load_holdings()
    if not holds:
        console.print("[yellow]No holdings configured.[/] Add them to config/holdings.yaml")
        raise typer.Exit(1)

    console.print(f"Fetching {len(holds)} holding(s)…")
    reports = analyse(holds, cfg.stocks, alerts)

    crypto, crypto_real = 0.0, False
    try:
        j = Journal(cfg.db_path)
        curve = j.equity_curve(1)
        crypto = float(curve[-1]["equity"]) if curve else 0.0
        crypto_real = str(j.get_state("mode", "paper")) == "live"
    except Exception:  # noqa: BLE001
        pass

    summary = PortfolioSummary(
        reports=reports, cash_idr=cash, crypto_equity=crypto, crypto_is_real=crypto_real
    )

    for r in reports:
        h = r.holding
        t = Table(h.ticker.replace(".JK", "") + (f"  ({h.note})" if h.note else ""), "value")
        t.add_row("shares", f"{h.shares:,} ({h.lots} lots)")
        t.add_row("avg price", _rupiah(h.avg_price))
        t.add_row("last price", _rupiah(r.price))
        t.add_row("cost basis", _rupiah(h.cost_basis))
        t.add_row("market value", _rupiah(r.market_value))
        colour = "red" if r.unrealised < 0 else "green"
        t.add_row("unrealised", f"[{colour}]{_rupiah(r.unrealised)} ({r.unrealised_pct:+.2f}%)[/]")
        t.add_row("to break even", f"{r.breakeven_move_pct:+.1f}%")
        t.add_row("", "")
        t.add_row("TTM dividend", f"{_rupiah(r.ttm_dividend_per_share)}/share (last {r.last_dividend_date})")
        t.add_row("annual income (net)", _rupiah(r.annual_income))
        t.add_row(
            "[bold]monthly income (net)[/]",
            f"[bold green]{_rupiah(r.monthly_income)}[/]",
        )
        # Labelled, because the income rows below are net and these are not.
        t.add_row("yield on cost (gross)", f"{r.yield_on_cost_pct:.2f}%")
        t.add_row("yield on market (gross)", f"{r.yield_on_market_pct:.2f}%")
        t.add_row("", "")
        t.add_row("EMA50 / EMA200", f"{r.ema50:,.0f} / {r.ema200:,.0f}")
        t.add_row("ADX(14)", f"{r.adx:.1f}")
        t.add_row("52w high / low", f"{r.high_52w:,.0f} / {r.low_52w:,.0f}")
        t.add_row("from 52w high", f"{r.from_52w_high_pct:+.1f}%")
        sig_colour = "green" if r.signal == "BUY" else "yellow"
        t.add_row("strategy signal", f"[{sig_colour}]{r.signal}[/]")
        console.print(t)
        for a in r.alerts:
            console.print(f"  [yellow]⚠️  {a}[/]")
        console.print()

    tot = Table("portfolio", "value")
    tot.add_row("stocks (market)", _rupiah(summary.stock_value))
    tot.add_row("stocks (cost)", _rupiah(summary.stock_cost))
    colour = "red" if summary.stock_unrealised < 0 else "green"
    tot.add_row("stocks unrealised", f"[{colour}]{_rupiah(summary.stock_unrealised)}[/]")
    tot.add_row("cash", _rupiah(summary.cash_idr))
    if crypto:
        tot.add_row(
            "crypto sleeve",
            _rupiah(crypto) + ("" if crypto_real else "  [dim](paper — not counted)[/]"),
        )
    tot.add_row("[bold]total[/]", f"[bold]{_rupiah(summary.total_value)}[/]")
    console.print(tot)

    console.print(
        f"\n[bold]Dividend income[/]: {_rupiah(summary.monthly_income)}/month "
        f"({_rupiah(summary.annual_income)}/year)"
    )
    cov = summary.subscription_coverage_pct
    console.print(
        f"Your subscription is ≈{_rupiah(SUBSCRIPTION_IDR)}/month → dividends already cover "
        f"[bold green]{cov:.0f}%[/] of it, with no trading."
    )
    bot_monthly = cfg.capital.sleeve_idr * 0.0123
    console.print(
        f"For comparison, the crypto bot on its {_rupiah(cfg.capital.sleeve_idr)} sleeve "
        f"models to ≈{_rupiah(bot_monthly)}/month — and can draw down 17%."
    )

    if notify:
        build_notifier(cfg.data_dir).send(summary.as_message())
        console.print("[green]Sent to the app chat.[/]")


@app.command()
def recommend(
    budget: float = typer.Option(0.0, help="Cash to deploy (default: cash_idr in holdings.yaml)"),
    top: int = typer.Option(12, help="How many ranked rows to show"),
    basket: int = typer.Option(3, help="How many names to split the budget across"),
    notify: bool = typer.Option(False, help="Send the basket to the app chat"),
) -> None:
    """Screen IDX for somewhere to put idle cash, weighted to durable dividend income.

    Ranks on yield, dividend consistency, payout sustainability, trend and
    diversification against what you already own. Shows its reasoning. Not advice --
    you place every stock order yourself.
    """
    _setup_logging()
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    cfg = load_config()
    from .holdings import SUBSCRIPTION_IDR, analyse, load_holdings
    from .screener import (
        BANK_SECTORS,
        LOT_SIZE,
        affordable,
        build_basket,
        fetch_candidates,
        load_universe,
        score,
    )

    holds, cash, alerts = load_holdings()
    budget = budget or cash
    if budget <= 0:
        console.print("[red]No budget.[/] Set cash_idr in config/holdings.yaml or pass --budget")
        raise typer.Exit(1)

    existing_income = 0.0
    owned = {h.ticker for h in holds}
    if holds:
        try:
            existing_income = sum(r.monthly_income for r in analyse(holds, cfg.stocks, alerts))
        except Exception:  # noqa: BLE001
            pass

    tickers = [t for t in load_universe() if t not in owned]
    console.print(
        f"Screening {len(tickers)} IDX names for {_rupiah(budget)}"
        f"{' (excluding what you already own)' if owned else ''}…"
    )
    cands = fetch_candidates(tickers)
    ranked = score(cands, budget=budget, avoid_sectors=BANK_SECTORS)
    ranked = affordable(ranked, budget)
    if not ranked:
        console.print("[yellow]Nothing in the universe fits that budget.[/]")
        raise typer.Exit(1)

    t = Table("#", "ticker", "sector", "price", "1 lot", "yield", "paid", "payout", "vs 200d", "score")
    for i, c in enumerate(ranked[:top], 1):
        payout = f"{c.payout_ratio * 100:.0f}%" if c.payout_ratio else "-"
        trend_colour = "green" if c.pct_from_ema200 >= 0 else "red"
        t.add_row(
            str(i), c.short, (c.sector or "?")[:18], f"{c.price:,.0f}",
            _rupiah(c.lot_cost), f"{c.yield_pct:.2f}%", f"{c.years_paid}/5", payout,
            f"[{trend_colour}]{c.pct_from_ema200:+.1f}%[/]", f"{c.score:.1f}",
        )
    console.print(t)

    console.print("\n[bold]Why the top names scored where they did[/]")
    for c in ranked[:5]:
        parts = " ".join(f"{k}={v}" for k, v in c.score_parts.items())
        console.print(f"  [bold]{c.short}[/] {c.score:.1f}  [dim]{parts}[/]")
        for f in c.flags:
            console.print(f"    [yellow]! {f}[/]")

    picks = build_basket(ranked, budget, n=basket)
    if not picks:
        console.print("\n[yellow]Budget too small to split; consider a single name.[/]")
        raise typer.Exit(0)

    console.print(f"\n[bold]A {len(picks)}-name split of {_rupiah(budget)}[/] (one per sector)")
    bt = Table("ticker", "lots", "shares", "cost", "annual div", "monthly")
    deployed = income = 0.0
    for c, lots in picks:
        cost = lots * c.lot_cost
        inc = lots * LOT_SIZE * c.ttm_div
        deployed += cost
        income += inc
        bt.add_row(
            c.short, str(lots), f"{lots * LOT_SIZE:,}", _rupiah(cost),
            _rupiah(inc), _rupiah(inc / 12),
        )
    bt.add_row("[bold]total[/]", "", "", f"[bold]{_rupiah(deployed)}[/]",
               f"[bold]{_rupiah(income)}[/]", f"[bold]{_rupiah(income / 12)}[/]")
    console.print(bt)
    console.print(f"  leftover cash: {_rupiah(budget - deployed)}  [dim](whole lots only)[/]")

    total_monthly = existing_income + income / 12
    console.print()
    console.print("[bold]Income picture[/]")
    console.print(f"  current holdings : {_rupiah(existing_income)}/month")
    console.print(f"  this basket adds : {_rupiah(income / 12)}/month")
    console.print(f"  combined         : [bold]{_rupiah(total_monthly)}/month[/]")
    cov = total_monthly / SUBSCRIPTION_IDR * 100
    verdict = "green" if cov >= 100 else "yellow"
    console.print(
        f"  Subscription (~{_rupiah(SUBSCRIPTION_IDR)}/mo): "
        f"[{verdict}]{cov:.0f}% covered[/]"
    )
    console.print(
        "\n[dim]Dividends are trailing twelve months of declared payments, not a "
        "forecast — any of these can be cut. You place every order yourself.[/]"
    )

    if notify:
        lines = [f"💡 IDX screen for {_rupiah(budget)}"]
        for c, lots in picks:
            lines.append(
                f"{c.short}: {lots} lot @ {c.price:,.0f} = {_rupiah(lots * c.lot_cost)}"
                f" · {c.yield_pct:.1f}% yield"
            )
        lines.append("")
        lines.append(f"adds {_rupiah(income / 12)}/mo -> {_rupiah(total_monthly)}/mo total")
        lines.append(f"Subscription {cov:.0f}% covered")
        build_notifier(cfg.data_dir).send("\n".join(lines))
        console.print("[green]Sent to the app chat.[/]")


@app.command("verify-costs")
def verify_costs(pair: str = "btc_idr", count: int = 50) -> None:
    """Compare the fees Indodax actually charged against the configured cost model.

    The help centre and the pairs API disagree on the fee schedule, so this
    reconciles both against real fills once you have some.
    """
    _setup_logging()
    cfg = load_config()
    sec = get_secrets()
    if not sec.has_indodax:
        console.print("[red]Needs Indodax credentials.[/]")
        raise typer.Exit(1)
    client = IndodaxV2Client(
        sec.indodax_key.get_secret_value(), sec.indodax_secret.get_secret_value()
    )
    pub = IndodaxPublicClient()
    maker, taker = pub.fees(pair)
    console.print(f"pairs API says: maker {maker:.3%}  taker {taker:.3%}")
    console.print(
        f"config says   : maker {cfg.costs.maker_fee_pct:.3%}  taker {cfg.costs.taker_fee_pct:.3%}"
        f"  sell tax {cfg.costs.sell_tax_pct:.3%}"
    )
    trades = client.trade_history(pair, count=count)
    if not trades:
        console.print(f"[dim]No fills on {pair} yet — nothing to reconcile.[/]")
        return
    t = Table("time", "side", "price", "qty", "fee", "fee %")
    for tr in trades[:count]:
        price, qty = float(tr.get("price", 0)), float(tr.get(pair.split("_")[0], 0))
        fee = float(tr.get("fee", 0) or 0)
        notional = price * qty
        t.add_row(
            time.strftime("%m-%d %H:%M", time.localtime(int(tr.get("trade_time", 0)))),
            tr.get("type", "?"), f"{price:,.0f}", f"{qty:.8f}", f"{fee:,.2f}",
            f"{fee / notional:.4%}" if notional else "-",
        )
    console.print(t)


@app.command()
def report(days: int = 30) -> None:
    """Performance report from the live/paper journal."""
    _setup_logging()
    cfg = load_config()
    j = Journal(cfg.db_path)
    rows = j.closed_trades(500)
    cutoff = time.time() - days * 86400
    rows = [r for r in rows if (r["exit_ts"] or 0) >= cutoff]
    if not rows:
        console.print(f"[dim]No closed trades in the last {days} days.[/]")
        return
    pnl = sum(r["realized_pnl"] or 0 for r in rows)
    wins = [r for r in rows if (r["realized_pnl"] or 0) > 0]
    gross_w = sum(r["realized_pnl"] for r in wins)
    gross_l = -sum(r["realized_pnl"] for r in rows if (r["realized_pnl"] or 0) < 0)
    t = Table("metric", "value")
    t.add_row("period", f"last {days} days")
    t.add_row("closed trades", str(len(rows)))
    t.add_row("realized P&L", _rupiah(pnl))
    t.add_row("vs sleeve", f"{pnl / cfg.capital.sleeve_idr * 100:+.2f}%")
    t.add_row("win rate", f"{100 * len(wins) / len(rows):.1f}%")
    t.add_row("profit factor", f"{gross_w / gross_l:.2f}" if gross_l else "∞")
    console.print(t)
    # From config, not hardcoded: a second profile has its own subscription, or
    # none at all, and should not be measured against someone else's bill.
    from .networth import load_networth

    try:
        subscription = load_networth(stock_value=0.0).goals.subscription_idr
    except Exception:  # noqa: BLE001 -- a report must not die over a config read
        subscription = 330_000.0
    if subscription > 0:
        console.print(
            f"\nYour subscription is ≈{_rupiah(subscription)}/month. "
            f"This period covered [bold]{max(0, pnl) / subscription * 100:.0f}%[/] "
            "of one month."
        )
    tr = Table("exit", "pair", "P&L", "reason")
    for r in rows[:20]:
        p = r["realized_pnl"] or 0
        tr.add_row(
            time.strftime("%m-%d %H:%M", time.localtime(r["exit_ts"] or 0)),
            r["pair"], f"[{'green' if p > 0 else 'red'}]{_rupiah(p)}[/]", r["exit_reason"] or "",
        )
    console.print(tr)


@app.command()
def halt(remove: bool = typer.Option(False, "--remove", help="Delete the HALT file instead")) -> None:
    """Create (or remove) the HALT file. The engine stops trading while it exists."""
    cfg = load_config()
    p: Path = cfg.halt_path
    if remove:
        p.unlink(missing_ok=True)
        console.print(f"[green]Removed[/] {p}. Send /resume to clear the halt state too.")
    else:
        p.write_text(f"halted at {time.ctime()}\n", encoding="utf-8")
        console.print(f"[red]Created[/] {p}. The engine will flatten and stop trading.")


@app.command()
def agent(
    once: bool = typer.Option(False, "--once", help="Drain both queues once, then exit"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the always-on chat & research answerer (OpenCode 2 / GLM).

    Watches the same queues the Claude-session monitors watched, but without
    needing a session: app questions are answered into the chat, morning
    research questions into the ledger. The model never reads files and never
    decides anything -- it explains; the code computes and decides.
    """
    _setup_logging(verbose)
    from .agent_worker import run_worker
    run_worker(once=once)


@app.command()
def paths() -> None:
    """Show where everything lives."""
    from .config import CODE_ROOT

    cfg = load_config()
    # Which profile is loaded matters more than any other line here: with a
    # second instance around, running the wrong one means trading the wrong
    # person's money. Say it plainly rather than making it inferable.
    whose = "default" if PROJECT_ROOT == CODE_ROOT else f"LUMBUNG_HOME={PROJECT_ROOT}"
    console.print(f"profile : [bold]{whose}[/]")
    console.print(f"code    : {CODE_ROOT}")
    console.print(f"project : {PROJECT_ROOT}")
    console.print(f"config  : {PROJECT_ROOT / 'config' / 'config.yaml'}")
    console.print(f"db      : {cfg.db_path}")
    console.print(f"logs    : {cfg.log_path}")
    console.print(f"halt    : {cfg.halt_path} ({'PRESENT' if cfg.halt_path.exists() else 'absent'})")


from . import (  # noqa: E402  (extra command groups)
    cli_bonds,
    cli_extra,
    cli_news,
    cli_scan,
    cli_spend,
)

cli_extra.register(app)
cli_spend.register(app)
cli_news.register(app)
cli_scan.register(app)
cli_bonds.register(app)


if __name__ == "__main__":
    app()
