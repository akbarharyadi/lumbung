"""Commands added after the core CLI: goal planning, sell review, daily digest.

Kept in a separate module so `cli.py` stays readable; registered onto the same
Typer app at import time.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import typer
from rich.console import Console
from rich.table import Table

from .config import PROJECT_ROOT, get_secrets, load_config
from .goal import (
    ASSUMED_RETURNS,
    DEFAULT_BLENDED,
    blended_yield,
    plan_income_goal,
    sell_signals,
)
from .journal import Journal
from .notify.app import build_notifier

console = Console()


def _net_worth_total() -> float:
    """Everything owned, for measuring concentration against.

    Concentration is only meaningful against the whole balance sheet. Measured
    inside the stock sleeve, a single holding is 100% of it by definition and the
    rule fires forever.

    Falls back to 0.0 -- which tells `sell_signals` to use the sleeve -- rather
    than raising: a sell review must not die because holdings.yaml is mid-edit.
    """
    try:
        from .holdings import analyse, load_holdings
        from .networth import live_crypto_value, load_networth

        holds, _cash, alerts = load_holdings()
        sv = sum(r.market_value for r in analyse(holds, load_config().stocks, alerts))
        return load_networth(stock_value=sv, crypto_value=live_crypto_value()).total
    except Exception:  # noqa: BLE001
        return 0.0


def _passive_income(reports) -> float:
    """Everything actually received each month: dividends AND interest.

    Two mistakes were live here at once and cancelled into a plausible number:

      * the screener's projected income from stocks NOT owned was added in, so
        the figure counted money that would only exist after a purchase;
      * savings and bond interest were left out, so real income was missing.

    Wrong in both directions still looks reasonable, which is precisely why it
    survived. Income means received, never projected.
    """
    try:
        from .networth import load_networth

        nw = load_networth(stock_value=sum(r.market_value for r in reports))
        return sum(r.monthly_income for r in reports) + nw.savings_income_monthly
    except Exception:  # noqa: BLE001
        return sum(r.monthly_income for r in reports)


# Fallbacks only. `providers:` in holdings.yaml wins, so someone who banks
# somewhere else sees their own instructions rather than an app they do not use.
DEFAULT_PROVIDERS = {
    "bonds": "SBN Ritel via your bond platform -- only during an offering window",
    "savings": "your savings account -- liquid and earning",
    "stocks": "your broker. Run `lumbung recommend` for candidates.",
    "gold": "your gold platform",
    "crypto": "transfer to Indodax, then raise capital.sleeve_idr to match",
    "cash": "plain bank account, earns nothing -- keep only a working balance",
    "other": "",
}


def _providers(nw) -> dict:
    """Per-bucket instructions: the person's own, falling back to generic."""
    out = dict(DEFAULT_PROVIDERS)
    out.update(getattr(nw, "providers", {}) or {})
    return out


def _rupiah(v: float) -> str:
    return f"Rp {v:,.0f}"


def register(app: typer.Typer) -> None:
    app.command()(payday)
    app.command()(deposit)
    app.command("check-ip")(check_ip)
    app.command()(dashboard)
    app.command()(doctor)
    app.command()(goal)
    app.command()(plan)
    app.command("sell-check")(sell_check)
    app.command()(daily)
    app.command()(todo)
    app.command()(research)


# ------------------------------------------------------------------ goal
def goal(
    monthly: float = typer.Option(3_000_000, help="Target income per month, in IDR"),
    contribute: float = typer.Option(0.0, help="How much you can add each month"),
    ret: float = typer.Option(
        0.0, help="Assumed blended annual return; 0 derives it from your allocation"
    ),
    years: int = typer.Option(20, help="How far to project"),
) -> None:
    """Work out what a monthly income target actually requires.

    Income here means yield you can take without selling the capital -- dividends
    and bond coupons. Living off price appreciation is a different, riskier plan.
    """
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    cfg = load_config()
    from .holdings import analyse, load_holdings

    holds, cash, alerts = load_holdings()
    capital = cash
    try:
        reports = analyse(holds, cfg.stocks, alerts) if holds else []
        capital += sum(r.market_value for r in reports)
    except Exception:  # noqa: BLE001
        reports = []

    # The blend comes from the target allocation, never from a flat guess. A
    # fifth of that allocation -- gold, cash, crypto -- pays nothing, so 7%
    # treats every rupiah as if it earned.
    if not ret:
        from .networth import load_networth as _load_nw

        _nw = _load_nw(stock_value=sum(r.market_value for r in reports))
        ret = blended_yield(
            {n: b.target_pct for n, b in _nw.buckets.items()}
        ) or DEFAULT_BLENDED

    plan = plan_income_goal(
        monthly_target=monthly, current_capital=capital,
        monthly_contribution=contribute, blended_return=ret,
    )

    t = Table("what", "value")
    t.add_row("target income", f"{_rupiah(monthly)}/month  ({_rupiah(plan.annual_target)}/year)")
    t.add_row("assumed return", f"{ret * 100:.2f}%/year blended")
    t.add_row("[bold]capital required[/]", f"[bold]{_rupiah(plan.capital_required)}[/]")
    t.add_row("your capital now", _rupiah(capital))
    t.add_row("[red]gap[/]", f"[red]{_rupiah(plan.gap)}[/]")
    t.add_row("multiple needed", f"{plan.capital_multiple:.1f}x your current capital")
    t.add_row("income now", f"{_rupiah(plan.income_now)}/month at that blended rate")
    console.print(t)

    console.print()
    console.print("[bold]Reference rates (2026-08-23)[/]")
    for k, v in ASSUMED_RETURNS.items():
        console.print(f"  {k:22s} {v * 100:.2f}%/year")

    console.print()
    if contribute <= 0:
        console.print(
            "[yellow]With no monthly contribution, only compounding closes the gap.[/]"
        )
        for c in (2_000_000, 5_000_000, 10_000_000):
            p = plan_income_goal(
                monthly_target=monthly, current_capital=capital,
                monthly_contribution=c, blended_return=ret,
            )
            yrs = p.years_to_target()
            console.print(
                f"  adding {_rupiah(c)}/month  →  "
                + (f"[green]{yrs} years[/]" if yrs else "[red]not within 60 years[/]")
            )
        p0 = plan_income_goal(
            monthly_target=monthly, current_capital=capital,
            monthly_contribution=0, blended_return=ret,
        )
        y0 = p0.years_to_target()
        console.print(
            "  adding nothing        →  "
            + (f"{y0} years" if y0 else "[red]not within 60 years[/]")
        )
    else:
        yrs = plan.years_to_target()
        console.print(
            f"[bold]Adding {_rupiah(contribute)}/month → "
            + (f"[green]{yrs} years[/] to target" if yrs else "[red]not within 60 years[/]")
            + "[/]"
        )
        tr = Table("year", "capital", "monthly income")
        for y, bal, inc in plan.trajectory(years=years, step=max(1, years // 10)):
            hit = "[green]" if inc >= monthly else ""
            tr.add_row(str(y), _rupiah(bal), f"{hit}{_rupiah(inc)}{'[/]' if hit else ''}")
        console.print(tr)

    console.print(
        "\n[dim]Yield assumptions are not guarantees. Dividends get cut, bond coupons "
        "reset, and inflation raises the target every year.[/]"
    )


def _firewall_hint(port: int) -> list[str]:
    """On Windows, check whether an inbound rule for `port` exists.

    Connections from this PC bypass the firewall entirely, so the dashboard can
    look perfectly healthy here while every phone on the LAN is silently blocked.
    That is the single most likely reason "it works on my PC but not my phone".
    """
    import platform
    import subprocess

    if platform.system() != "Windows":
        return []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-NetFirewallPortFilter | Where-Object { $_.LocalPort -eq "
             f"{port} }} | Measure-Object).Count"],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        if out.isdigit() and int(out) > 0:
            return []
    except Exception:  # noqa: BLE001
        return []
    return [
        f"No Windows Firewall rule for port {port} — other devices will be blocked.",
        "Run this ONCE in an Administrator PowerShell:",
        "",
        '  New-NetFirewallRule -DisplayName "Lumbung Dashboard" `',
        "    -Direction Inbound -Protocol TCP -LocalPort "
        f"{port} -Action Allow -RemoteAddress LocalSubnet",
        "",
        "RemoteAddress LocalSubnet limits it to your home network, so the port is",
        "not reachable from the internet even if your PC joins a public WiFi.",
    ]


# ------------------------------------------------------------------ payday
def payday(
    amount: float = typer.Option(0.0, help="Amount to deploy (default: your monthly surplus)"),
    notify: bool = typer.Option(False, help="Send the plan to the app chat"),
) -> None:
    """The plan for this month's money: how much goes where, and how to move it.

    Split proportionally across whatever is furthest below target, so new money
    does the rebalancing and nothing has to be sold.
    """
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    cfg = load_config()
    from .holdings import analyse, load_holdings
    from .networth import live_crypto_value, load_networth

    holds, cash, alerts = load_holdings()
    reports = analyse(holds, cfg.stocks, alerts) if holds else []
    nw = load_networth(stock_value=sum(r.market_value for r in reports),
                       crypto_value=live_crypto_value())

    # Deploy from the CONFIGURED baseline, not from the raw trailing average.
    # The average is a total: one car repair, one financed purchase or one
    # month of paying a freelance team lands in it whole and can swamp it for
    # months afterwards. That is real money, but it is not a recurring claim on
    # next month's salary, and treating it as one made this command refuse to
    # produce a plan at all on the one morning of the month it is needed.
    #
    # The average is still computed and still shown, and a gap between the two
    # is called out below -- so a baseline drifting away from reality is
    # visible, rather than silently overriding the plan.
    from .spending import connect as spend_connect
    from .spending import spending_profile

    prof = spending_profile(spend_connect(cfg.db_path), nw.cashflow.spending_monthly)
    baseline_surplus = max(0.0, nw.cashflow.surplus)
    measured = prof.surplus(nw.cashflow.income_monthly)
    amt = amount or baseline_surplus
    if amt <= 0:
        console.print(
            "[yellow]No surplus.[/] Your baseline spending "
            f"({_rupiah(nw.cashflow.spending_monthly)}) is at or above your income "
            f"({_rupiah(nw.cashflow.income_monthly)}). Fix cashflow in "
            "config/holdings.yaml, or deploy a figure you choose with "
            "`lumbung payday --amount <rupiah>`."
        )
        raise typer.Exit(1)

    if prof.tracked:
        colour = {"over budget": "yellow", "under budget": "green"}.get(prof.verdict, "")
        st = Table("spending", "value")
        st.add_row("budgeted", _rupiah(prof.budgeted))
        st.add_row(
            f"actual ({prof.basis_label})",
            f"[{colour}]{_rupiah(prof.average)}[/]" if colour else _rupiah(prof.average),
        )
        st.add_row(
            "range", f"{_rupiah(prof.lowest)} – {_rupiah(prof.highest)}"
            f"  [dim](swings {prof.swing_pct:.0f}%)[/]"
        )
        if prof.current:
            st.add_row(
                f"{prof.current[0]} so far", _rupiah(prof.current[1]) + "  [dim](in progress)[/]"
            )
        st.add_row(
            "[bold]deployable[/]",
            f"[bold]{_rupiah(amt)}[/]  [dim](income - baseline spending)[/]",
        )
        console.print(st)
        gap = prof.average - prof.budgeted
        if gap > prof.budgeted * 0.25:
            # Say which number this plan came from and which it did not. The
            # difference is usually one-offs; if it is not, the baseline is
            # stale and deploying against it would over-commit next month.
            console.print(
                f"[yellow]Recent months averaged {_rupiah(prof.average)}, "
                f"{_rupiah(gap)} above your {_rupiah(prof.budgeted)} baseline.[/]\n"
                "[dim]This plan uses the baseline. If that gap is one-offs "
                "(a repair, a financed purchase, freelance costs) it is fine. If it "
                "is your normal spending, raise the baseline before deploying -- on "
                f"the average there would be {_rupiah(measured)} to deploy, not "
                f"{_rupiah(amt)}.[/]"
            )
    else:
        console.print(
            "[dim]No spending recorded yet, so this uses your budgeted figure. "
            "Log purchases with `lumbung spend` and it will use reality instead.[/]"
        )

    plan = [(b, a) for b, a in nw.allocate_surplus(amt) if a >= 50_000]
    how = _providers(nw)

    console.print(f"\n[bold]Deploying {_rupiah(amt)}[/]")
    t = Table("where", "amount", "how")
    for bucket, a in sorted(plan, key=lambda x: -x[1]):
        t.add_row(bucket, _rupiah(a), how.get(bucket, ""))
    console.print(t)

    lines = [f"💰 Payday — deploy {_rupiah(amt)}"]
    for bucket, a in sorted(plan, key=lambda x: -x[1]):
        lines.append(f"{bucket}: {_rupiah(a)}")
        lines.append(f"  {how.get(bucket, '')}")

    console.print(
        "\n[dim]After you move the money, record it so the plan stays honest:[/]\n"
        "  lumbung deposit <amount> --to <bucket>"
    )
    if notify:
        lines.append("")
        lines.append("Record it after: lumbung deposit <amount> --to <bucket>")
        build_notifier(cfg.data_dir).send("\n".join(lines))
        console.print("[green]Sent to the app chat.[/]")


def deposit(
    amount: float = typer.Argument(..., help="Amount in IDR"),
    to: str = typer.Option(..., "--to", help="cash | gold | bonds | stocks | crypto"),
) -> None:
    """Record money you actually moved, so net worth and the goal stay accurate.

    Updates config/holdings.yaml. Stocks are deliberately excluded -- a share
    position needs a ticker, lots and an average price, so add those by hand.
    """
    bucket = to.lower()
    if bucket == "stocks":
        console.print(
            "[yellow]Add stock buys under `stocks:` in config/holdings.yaml[/] "
            "so lots and average price are recorded."
        )
        raise typer.Exit(1)
    if bucket not in ("cash", "gold", "bonds", "crypto"):
        console.print("[red]--to must be cash, gold, bonds or crypto[/]")
        raise typer.Exit(1)

    from .holdings_io import adjust, bucket_balance, edit

    path = PROJECT_ROOT / "config" / "holdings.yaml"
    result: list[float] = []

    def _move(doc):
        if bucket == "cash":
            result.append(adjust(doc, "cash", amount))
            return
        result.append(adjust(doc, bucket, amount))
        # Money put to work is money that left the cash pile. Never below zero:
        # the deposit may have come from salary that was never in cash_idr.
        adjust(doc, "cash", -min(amount, bucket_balance(doc, "cash")))

    try:
        edit(path, _move)
    except ValueError as exc:
        console.print(f"[red]Nothing changed:[/] {exc}")
        raise typer.Exit(1) from None
    new_total = result[0]
    console.print(f"[green]Recorded {_rupiah(amount)} into {bucket}.[/]")
    console.print(f"  {bucket} is now {_rupiah(new_total)}")
    console.print(f"  cash is now {_rupiah(float(raw.get('cash_idr', 0)))}")
    if bucket == "crypto":
        console.print(
            "\n[yellow]Also raise capital.sleeve_idr in config/config.yaml[/] to match what "
            "is actually sitting in Indodax — position sizing is computed from the sleeve."
        )
    console.print("\n[dim]Note: comments in holdings.yaml are not preserved by this rewrite.[/]")


# ------------------------------------------------------------------- ip
def check_ip() -> None:
    """Compare your public IP against the one whitelisted on the Indodax key.

    Indodax V2 binds a key to one IP. Home broadband is dynamic, so this is the
    thing most likely to break a working bot without any code changing.
    """
    from .netcheck import check

    cfg = load_config()
    sec = get_secrets()
    wl = getattr(sec, "indodax_whitelist_ip", "")
    wl = wl.get_secret_value() if hasattr(wl, "get_secret_value") else str(wl)

    st = check(cfg.db_path.parent / "last_ip.json", wl)

    t = Table("", "value")
    t.add_row("public IPv4 now", st.current or "[red]could not determine[/]")
    t.add_row("whitelisted on key", st.whitelisted or "[yellow]not recorded[/]")
    console.print(t)

    if st.unknown:
        console.print("[red]No internet, or every IP service was unreachable.[/]")
        raise typer.Exit(1)

    if st.not_configured:
        console.print()
        console.print(
            "[yellow]Set INDODAX_WHITELIST_IP in .env[/] to whatever you entered in the "
            "Indodax 'IP Permission' box. Then this command can tell you when it drifts."
        )
        console.print(f"  Right now that value should be: [bold]{st.current}[/]")
        return

    console.print()
    if st.ok:
        console.print("[green]Match — the API key will authorise.[/]")
    else:
        console.print("[red]MISMATCH — signed Indodax calls will be rejected.[/]")
        console.print()
        console.print("  Your ISP gave you a new IP. Two ways forward:")
        console.print(
            f"  1. Update the key's IP Permission at https://indodax.com/trade_api "
            f"to [bold]{st.current}[/], then set INDODAX_WHITELIST_IP in .env to match."
        )
        console.print(
            "  2. Move the bot to a VPS with a static IP — then this never happens "
            "again, and your stop-losses stop depending on this PC being awake."
        )
        raise typer.Exit(1)


# --------------------------------------------------------------- dashboard
def dashboard(
    host: str = typer.Option("0.0.0.0", help="Bind address (0.0.0.0 = reachable over Tailscale)"),
    port: int = typer.Option(8787, help="Port"),
    token: str = typer.Option("", help="Override the dashboard token"),
    readonly: bool = typer.Option(
        False, "--readonly", help="Disable all controls (use when exposed publicly)"
    ),
    use_access: bool = typer.Option(
        False, "--use-access",
        help="Trust Cloudflare Access identity instead of asking for a token",
    ),
) -> None:
    """Serve the phone dashboard (installable PWA).

    Reach it over Tailscale from anywhere, or over your home WiFi. The bearer
    token is required even on a private network -- "it's only on my LAN" is the
    assumption that quietly stops being true.
    """
    import socket

    import uvicorn

    from .web.server import create_app, resolve_token

    tok = token or resolve_token()

    access = None
    if use_access:
        from .web.access import AccessAuth

        sec_a = get_secrets()
        missing = [
            n for n, v in (
                ("ACCESS_TEAM_DOMAIN", sec_a.access_team_domain),
                ("ACCESS_AUD", sec_a.access_aud),
            ) if not v.strip()
        ]
        if missing:
            console.print(f"[red]--use-access needs {' and '.join(missing)} in .env[/]")
            raise typer.Exit(1)
        access = AccessAuth(
            sec_a.access_team_domain, sec_a.access_aud,
            sec_a.access_emails.split(","),
        )
        # Check the keys are reachable now rather than discovering a typo at the
        # moment someone is trying to log in.
        ok, detail = access.preflight()
        if not ok:
            console.print(f"[red]Cloudflare Access check failed:[/] {detail}")
            raise typer.Exit(1)
        console.print(f"[green]Cloudflare Access[/] {detail}")
        if not access.emails:
            console.print(
                "[yellow]ACCESS_EMAILS is empty[/] — anyone your Access policy "
                "admits will be let in. Set it to keep a second, independent list."
            )

    app = create_app(token=tok, readonly=readonly, access=access)

    # Best-effort local IP, so the URL below is one you can actually type.
    ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip, _ = s.getsockname()
        s.close()
    except Exception:  # noqa: BLE001
        pass

    console.print()
    console.print(
        "[bold green]Dashboard running[/]"
        + ("  [yellow](read-only — controls disabled)[/]" if readonly else "")
        + ("  [green](Cloudflare Access identity)[/]" if use_access else "")
    )
    console.print(f"  this PC     http://127.0.0.1:{port}/?t={tok}")
    console.print(f"  same WiFi   http://{ip}:{port}/?t={tok}")
    console.print(f"  Tailscale   http://<your-tailscale-name>:{port}/?t={tok}")
    console.print()
    console.print("[bold]On your phone:[/] open the link, then Chrome menu →")
    console.print("  \"Add to Home screen\" / \"Install app\".")
    console.print("[dim]The ?t=… token is saved on the device and stripped from the URL.[/]")
    if not token and not get_secrets().dashboard_token.get_secret_value():
        console.print(
            "[yellow]This token is regenerated every restart.[/] "
            "Set DASHBOARD_TOKEN in .env to keep it stable."
        )
    hint = _firewall_hint(port)
    if hint:
        console.print()
        console.print("[yellow]" + hint[0] + "[/]")
        for line in hint[1:]:
            console.print("  " + line if line else "")
    console.print()

    if not readonly:
        console.print(
            "[dim]Controls (pause/flat/kill) are ENABLED. If you expose this beyond "
            "your own network, restart with --readonly.[/]"
        )
    uvicorn.run(app, host=host, port=port, log_level="warning")


# ------------------------------------------------------------------ doctor
def doctor(
    live: bool = typer.Option(False, "--live", help="Check readiness for LIVE trading"),
) -> None:
    """Pre-flight check: what is still missing before this can run."""
    from .doctor import FAIL, OK, WARN, next_steps, run_checks

    checks = run_checks(want_live=live)
    colour = {OK: "green", WARN: "yellow", FAIL: "red"}
    icon = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL"}

    t = Table("", "check", "detail")
    for c in checks:
        t.add_row(f"[{colour[c.status]}]{icon[c.status]}[/]", c.name, c.detail)
    console.print(t)

    fixes = [c for c in checks if c.fix]
    if fixes:
        console.print()
        console.print("[bold]How to fix[/]")
        for c in fixes:
            console.print(f"  [{colour[c.status]}]{c.name}[/]")
            for line in c.fix.split(chr(10)):
                console.print(f"     {line}")

    blocking = [c for c in checks if c.blocking]
    console.print()
    if blocking:
        console.print(
            f"[red]{len(blocking)} blocking issue(s)[/] — "
            + ("live trading cannot start." if live else "paper trading cannot start.")
        )
    else:
        console.print("[green]No blocking issues.[/]")

    console.print()
    console.print("[bold]Next steps, in order[/]")
    for i, s in enumerate(next_steps(checks, want_live=live), 1):
        console.print(f"  {i}. {s}")


# --------------------------------------------------------------------- plan
def plan(
    monthly: float = typer.Option(3_000_000, help="Target passive income per month"),
    ret: float = typer.Option(
        0.0, help="Assumed blended annual return; 0 derives it from your allocation"
    ),
) -> None:
    """Whole balance sheet: allocation, emergency fund, and where the surplus goes.

    This is the command that matters most. With a large monthly surplus, the
    decisions that move the outcome are the buffer, the concentration, and where
    next month's money goes -- not which stock to pick.
    """
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    cfg = load_config()
    from .holdings import analyse, load_holdings
    from .networth import live_crypto_value, load_networth

    holds, cash, alerts = load_holdings()
    reports = analyse(holds, cfg.stocks, alerts) if holds else []
    stock_value = sum(r.market_value for r in reports)
    div_income = sum(r.monthly_income for r in reports)

    # Crypto comes from the exchange, not the yaml: top-ups need no bookkeeping.
    nw = load_networth(stock_value=stock_value, crypto_value=live_crypto_value())
    for r in reports:
        nw.positions.append((r.holding.ticker.replace(".JK", ""), r.market_value))
    total = nw.total

    # As in `goal`: derived, not assumed. A flat 7% understated the capital
    # needed for Rp 3jt/month by about Rp 300 million and the wait by 2.1 years.
    if not ret:
        ret = blended_yield(
            {n: b.target_pct for n, b in nw.buckets.items()}
        ) or DEFAULT_BLENDED

    # -- allocation -----------------------------------------------------
    t = Table("bucket", "value", "now", "target", "drift", "to reach target")
    from .networth import BUCKETS

    for name in BUCKETS:
        b = nw.buckets[name]
        if b.value == 0 and b.target_pct == 0:
            continue
        drift = b.drift(total)
        colour = "red" if abs(drift) > 15 else ("yellow" if abs(drift) > 7 else "green")
        gap = b.gap_idr(total)
        t.add_row(
            name, _rupiah(b.value), f"{b.weight(total) * 100:.1f}%",
            f"{b.target_pct * 100:.0f}%", f"[{colour}]{drift:+.1f}pp[/]",
            ("[green]+" if gap > 0 else "[red]") + _rupiah(abs(gap)) + "[/]",
        )
    t.add_row("[bold]net worth[/]", f"[bold]{_rupiah(total)}[/]", "100%", "100%", "", "")
    console.print(t)

    if nw.possessions:
        console.print()
        console.print("[bold]Also owned[/]")
        console.print(
            "  [dim]Not investments, and deliberately outside the table above. "
            "Counting them here would make BBCA read 12% instead of 48% without "
            "changing a single risk you carry.[/]"
        )
        pt = Table("what", "value", "")
        for p_ in sorted(nw.possessions, key=lambda x: -x.value_idr):
            flag = "" if not p_.depreciating else "[dim]falls in value[/]"
            pt.add_row(
                p_.name,
                _rupiah(p_.value_idr) if p_.value_idr else "[yellow]not set[/]",
                flag,
            )
        pt.add_row(
            "[bold]everything owned[/]",
            f"[bold]{_rupiah(nw.total_with_possessions)}[/]",
            "[dim]investable + these[/]",
        )
        console.print(pt)
        unset = [p_.name for p_ in nw.possessions if not p_.value_idr]
        if unset:
            console.print(
                f"  [yellow]No value set for: {', '.join(unset)}. "
                "Until you set them the total below is understated.[/]"
            )

    label, val, wt = nw.largest_position()
    if wt > 0.4:
        console.print(
            f"[red]Concentration:[/] {label} alone is {wt * 100:.0f}% of net worth "
            f"({_rupiah(val)})."
        )

    # -- cash flow ------------------------------------------------------
    from .spending import connect as _spend_connect
    from .spending import spending_profile as _spending_profile

    cf = nw.cashflow
    console.print()
    c = Table("cash flow", "value")
    c.add_row("income", f"{_rupiah(cf.income_monthly)}/month")
    # This is the CONFIGURED baseline, not a measurement. It used to be labelled
    # "measured", which is how a stale figure survived for months: nothing on the
    # screen contradicted it. The measured average is shown underneath, from the
    # expenses table, so a drift between the two is visible rather than implied.
    c.add_row("spending (baseline)", f"{_rupiah(cf.spending_monthly)}/month")
    _prof = _spending_profile(_spend_connect(cfg.db_path), cf.spending_monthly)
    if _prof.tracked:
        _gap = _prof.average - cf.spending_monthly
        _colour = "yellow" if abs(_gap) > cf.spending_monthly * 0.25 else "dim"
        c.add_row(
            f"[dim]actual ({_prof.basis_label})[/]",
            f"[{_colour}]{_rupiah(_prof.average)}/month"
            f"{f'  ({_rupiah(_gap)} above baseline)' if _gap > 0 else ''}[/]",
        )
    if cf.has_limit:
        # Shown next to the measured figure on purpose. A limit that quietly
        # replaced it would shrink the emergency fund by six times the gap.
        c.add_row(
            "[bold]spending limit[/]",
            f"[bold]{_rupiah(cf.spending_limit)}/month[/]  "
            f"[dim]asks for {_rupiah(cf.limit_gap)} less[/]",
        )
    c.add_row("[bold]surplus[/]", f"[bold green]{_rupiah(cf.surplus)}/month[/]")
    c.add_row("savings rate", f"[green]{cf.savings_rate * 100:.0f}%[/]")
    interest = nw.savings_income_monthly
    c.add_row("stock dividends", f"{_rupiah(div_income)}/month")
    c.add_row("interest + coupons", f"{_rupiah(interest)}/month")
    c.add_row("[bold]passive income[/]", f"[bold green]{_rupiah(div_income + interest)}/month[/]")
    console.print(c)

    if cf.has_limit:
        from datetime import date as _date

        from .spending import connect, monthly_totals

        rows = monthly_totals(connect(cfg.db_path), months=1)
        if rows:
            month, spent = rows[-1]
            if month == _date.today().strftime("%Y-%m"):
                left = cf.spending_limit - spent
                pct = spent / cf.spending_limit * 100 if cf.spending_limit else 0
                if left >= 0:
                    console.print(
                        f"  [green]{month}: {_rupiah(spent)} of {_rupiah(cf.spending_limit)} "
                        f"({pct:.0f}%) — {_rupiah(left)} left[/]"
                    )
                else:
                    console.print(
                        f"  [red]{month}: {_rupiah(spent)} of {_rupiah(cf.spending_limit)} "
                        f"({pct:.0f}%) — {_rupiah(-left)} over[/]"
                    )

    console.print(
        f"  [dim]Your surplus is {cf.surplus / max(div_income, 1):.0f}x your dividend "
        f"income and {cf.surplus * 12 / max(total, 1) * 100:.0f}% of net worth per "
        "year. Saving is the dominant lever, not returns.[/]"
    )

    # -- emergency fund --------------------------------------------------
    console.print()
    console.print("[bold]Emergency fund[/]")
    console.print(
        f"  target {nw.emergency_months_target} months = {_rupiah(nw.emergency_target)}"
    )
    console.print(
        f"  cash only     {_rupiah(nw.buckets['cash'].value)} "
        f"= [{'green' if nw.months_covered_cash >= 3 else 'red'}]"
        f"{nw.months_covered_cash:.1f} months[/]"
    )
    console.print(
        f"  + savings+gold {_rupiah(nw.liquid_now)} "
        f"= [{'green' if nw.months_covered_liquid >= nw.emergency_months_target else 'yellow'}]"
        f"{nw.months_covered_liquid:.1f} months[/]  [dim](gold sells same-day, ~3-5% spread)[/]"
    )
    # Promised money is not buffer. Showing the gross figure next to a target it
    # only clears because a bill has not been paid yet is how you get a pleasant
    # number in August and a problem in September.
    committed = nw.committed_total()
    if committed > 0:
        free = nw.free_liquid()
        console.print(
            f"  [red]- committed    {_rupiah(committed)}[/] "
            f"[dim](already promised, see below)[/]"
        )
        console.print(
            f"  [bold]= really free  {_rupiah(free)}[/] "
            f"= [{'green' if nw.months_covered_free() >= nw.emergency_months_target else 'red'}]"
            f"{nw.months_covered_free():.1f} months[/]"
        )
    if nw.emergency_shortfall > 0:
        console.print(
            f"  [yellow]shortfall {_rupiah(nw.emergency_shortfall)} — "
            f"{nw.months_to_fund_emergency:.1f} months of surplus to close[/]"
        )
    else:
        console.print("  [green]covered[/]")

    # -- commitments ------------------------------------------------------
    if nw.commitments:
        console.print()
        console.print("[bold]Committed[/]")
        console.print(
            "  [dim]Promised money. It is held liquid and kept out of bonds and "
            "the trading sleeve until it is paid.[/]"
        )
        c_tbl = Table("what", "amount", "due", "in")
        for c in sorted(nw.commitments, key=lambda x: (x.due is None, x.due)):
            d = c.days_away()
            when = c.due.isoformat() if c.due else "no date"
            if d is None:
                gap = "[yellow]undated[/]"
            elif d < 0:
                gap = f"[red]{-d} days ago[/]"
            else:
                gap = f"[{'red' if d <= 45 else 'yellow'}]{d} days[/]"
            c_tbl.add_row(c.name, _rupiah(c.amount_idr), when, gap)
        console.print(c_tbl)
        console.print("  [dim]Delete it from holdings.yaml once it is paid.[/]")

    wishes = nw.wishes()
    if wishes:
        console.print()
        console.print("[bold]Considering[/]")
        console.print(
            "  [dim]Not promised, so none of this touches your safety net or "
            "holds money back. Priced so you can see what it would cost.[/]"
        )
        w_tbl = Table("what", "price", "months of saving", "safe to buy", "buffer then")
        surplus = max(0.0, cf.surplus)
        # Simulate first, then order by the result: what you can have soonest
        # is the useful thing to read at the top.
        plans = [(w, nw.purchase_plan(w.amount_idr)) for w in wishes]

        def _soonest(item):
            wish, s = item
            if s["safe_now"]:
                return (0, 0, -wish.amount_idr)
            if s["when"] is not None:
                return (1, s["when"].toordinal(), -wish.amount_idr)
            return (2, 0, -wish.amount_idr)   # out of reach, last

        for w, sim in sorted(plans, key=_soonest):
            months = w.amount_idr / surplus if surplus else float("inf")
            if sim["safe_now"]:
                when = "[green]yes, now[/]"
            elif sim["when"] is not None:
                when = f"[yellow]wait — {sim['when'].strftime('%d %b %Y')}[/]"
            else:
                when = f"[red]not within {sim['horizon'] // 12} years[/]"
            w_tbl.add_row(
                w.name, _rupiah(w.amount_idr),
                "-" if months == float("inf") else f"{months:.1f}",
                when, f"{sim['months_after']:.1f} mo",
            )
        console.print(w_tbl)
        console.print(
            f"  [dim]\"Safe\" means: you can pay for it, and what is left still "
            f"covers {nw.emergency_months_target} months of spending "
            f"(Rp {nw.emergency_target:,.0f}) after everything you already owe. "
            "The walk uses your real payday plan, so money it sends to bonds "
            "stops counting as buffer.[/]"
        )

    # -- surplus deployment ----------------------------------------------
    console.print()
    console.print(f"[bold]Where next month's {_rupiah(cf.surplus)} should go[/]")
    console.print(
        "  [dim]Directing new money at underweight buckets rebalances without "
        "selling — no realised loss, no 0.1% PPh on proceeds.[/]"
    )
    d = Table("bucket", "amount", "why")
    why = {
        **_providers(nw),
    }
    for name, amt in sorted(nw.allocate_surplus(), key=lambda x: -x[1]):
        if amt < 50_000:
            continue
        d.add_row(name, _rupiah(amt), why.get(name, ""))
    console.print(d)

    # -- the goal --------------------------------------------------------
    plan_ = plan_income_goal(
        monthly_target=monthly, current_capital=total,
        monthly_contribution=cf.surplus, blended_return=ret,
    )
    yrs = plan_.years_to_target()
    console.print()
    g = Table("goal", "value")
    g.add_row("target", f"{_rupiah(monthly)}/month passive")
    # Shown, not hidden: the rate is the one assumption the whole card rests on,
    # and it is the one that was wrong.
    g.add_row("assumed return", f"{ret * 100:.2f}%/year blended")
    g.add_row("capital required", _rupiah(plan_.capital_required))
    g.add_row("net worth now", _rupiah(total))
    g.add_row("saving", f"{_rupiah(cf.surplus)}/month")
    g.add_row(
        "[bold]time to target[/]",
        f"[bold green]{yrs} years[/]" if yrs else "[red]not within 60 years[/]",
    )
    console.print(g)

    tr = Table("year", "net worth", "passive income/mo")
    for y, bal, inc in plan_.trajectory(years=10):
        hit = inc >= monthly
        tr.add_row(
            str(y), _rupiah(bal),
            f"[green]{_rupiah(inc)}[/]" if hit else _rupiah(inc),
        )
    console.print(tr)
    console.print(
        "\n[dim]Assumes the surplus keeps arriving and the blended return holds. "
        "Neither is guaranteed; inflation also raises the target each year.[/]"
    )


# ------------------------------------------------------------ sell review
def sell_check(notify: bool = typer.Option(False, help="Send to the app chat")) -> None:
    """Rule-based trim/exit review of the stocks you hold. Reports; never instructs."""
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    cfg = load_config()
    from .holdings import analyse, load_holdings

    holds, cash, alerts = load_holdings()
    if not holds:
        console.print("[yellow]No holdings in config/holdings.yaml[/]")
        raise typer.Exit(1)

    reports = analyse(holds, cfg.stocks, alerts)
    portfolio_value = sum(r.market_value for r in reports) + cash
    lines: list[str] = []

    for r in reports:
        prior = None
        # Prior calendar year's total dividend, for cut detection.
        try:
            import pandas as pd
            import yfinance as yf

            div = yf.Ticker(r.holding.ticker).dividends
            if len(div):
                div.index = div.index.tz_localize(None)
                last_year = pd.Timestamp.now().year - 1
                prior = float(div[div.index.year == last_year].sum()) or None
        except Exception:  # noqa: BLE001
            prior = None

        rev = sell_signals(
            r, portfolio_value=portfolio_value, net_worth=_net_worth_total(),
            prior_year_div=prior,
        )
        short = r.holding.ticker.replace(".JK", "")
        weight = r.market_value / portfolio_value * 100 if portfolio_value else 0

        t = Table(f"{short} — sell review", "")
        t.add_row("weight in portfolio", f"{weight:.1f}%")
        t.add_row("unrealised", f"{_rupiah(r.unrealised)} ({r.unrealised_pct:+.2f}%)")
        t.add_row("verdict", _verdict(rev))
        console.print(t)
        for s in rev.signals:
            colour = {"act": "red", "watch": "yellow", "info": "green"}[s.severity]
            console.print(f"  [{colour}]• {s.rule}[/]: {s.detail}")
        if rev.suggested_trim_lots:
            console.print(f"  [bold]→ {rev.trim_reason}[/]")
            _print_sell_quote(r, rev.suggested_trim_lots, cfg)

        # Always price the full exit too. The trim is the tool's suggestion; the
        # full sale is the option people actually reach for at a loss, and it is
        # the one whose cost is least visible in a broker app.
        _print_sell_quote(r, r.holding.lots, cfg, label="sell everything")
        console.print(
            f"  [dim]business intact: {'yes' if rev.business_is_intact else 'NO'} — "
            "a broken price with an intact business is a different problem from a "
            "broken business[/]"
        )
        console.print()

        lines.append(f"{short} {weight:.0f}% · {r.unrealised_pct:+.1f}% · {_verdict(rev, plain=True)}")
        for s in rev.signals:
            if s.severity in ("act", "watch"):
                lines.append(f"  • {s.rule}: {s.detail}")

    console.print(
        "[dim]These are rules, not advice. Concentration, tax, your income and what "
        "else you need the money for are all outside what this can see.[/]"
    )
    if notify:
        build_notifier(cfg.data_dir).send("🔍 Sell review\n" + "\n".join(lines))
        console.print("[green]Sent to the app chat.[/]")


def _verdict(rev, *, plain: bool = False) -> str:
    if rev.worst == "act":
        txt = "ACTION SUGGESTED" if not rev.business_is_intact else "TRIM WORTH CONSIDERING"
        return txt if plain else f"[red]{txt}[/]"
    if rev.worst == "watch":
        return "WATCH" if plain else "[yellow]WATCH[/]"
    return "HOLD" if plain else "[green]HOLD[/]"


def _print_sell_quote(report, lots: int, cfg, *, label: str = "") -> None:
    """What a sale of `lots` actually nets, after tax, fees and lost income."""
    from .trade_math import quote_sell

    q = quote_sell(
        lots=lots,
        price=report.price,
        avg_price=report.holding.avg_price,
        annual_dividend_per_share=report.ttm_dividend_per_share,
        fee_sell=cfg.stocks.fee_sell_pct,
    )
    if q.lots <= 0:
        return

    head = label or f"sell {q.lots} lots"
    t = Table(f"  {head}", "")
    t.add_row("proceeds", _rupiah(q.proceeds))
    t.add_row("cost basis", _rupiah(q.cost_basis))
    colour = "red" if q.gross_pnl < 0 else "green"
    t.add_row(
        "realised before costs",
        f"[{colour}]{_rupiah(q.gross_pnl)} ({q.gross_pnl_pct:+.2f}%)[/]",
    )
    t.add_row("PPh 0.1% (on proceeds)", f"[red]-{_rupiah(q.pph)}[/]")
    t.add_row("broker fee", f"[red]-{_rupiah(q.broker_fee)}[/]")
    colour = "red" if q.net_pnl < 0 else "green"
    t.add_row(
        "[bold]realised after costs[/]",
        f"[bold {colour}]{_rupiah(q.net_pnl)} ({q.net_pnl_pct:+.2f}%)[/]",
    )
    t.add_row("[bold]cash you receive[/]", f"[bold]{_rupiah(q.net_proceeds)}[/]")
    if q.dividend_lost_monthly > 0:
        t.add_row("income given up", f"{_rupiah(q.dividend_lost_monthly)}/month")
    t.add_row("break-even price", f"{_rupiah(q.breakeven_price)}")
    console.print(t)

    if q.is_loss:
        console.print(
            "  [dim]The 0.1% is final and charged on proceeds, so a losing sale "
            "pays tax and leaves nothing to offset. Indonesia has no capital-loss "
            "relief for retail equity.[/]"
        )


# --------------------------------------------------------------- research
def research(
    show: bool = typer.Option(True, help="Print the questions as well as queueing"),
    queue: bool = typer.Option(True, help="Append them to data/research_queue.jsonl"),
    pending: bool = typer.Option(
        False, "--pending", help="Show queued questions still waiting for an answer"
    ),
) -> None:
    """Queue the things only a person or an agent can find out.

    The rules answer everything computable from what is already known. This
    covers what changes in the world: a new SBN series, an ex-dividend date,
    news that alters a thesis. Answers are written back by whoever picks the
    queue up -- nothing here decides anything.
    """
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    cfg = load_config()
    from .bonds import load_bonds
    from .holdings import analyse, load_holdings
    from .research import (
        build_questions,
        file_age_days,
        pending_questions,
        queue_questions,
    )

    d = cfg.db_path.parent
    if pending:
        # The monitor notifies about new lines only, so anything queued while no
        # session was open is invisible to it. This is how you find that.
        open_q = pending_questions(
            d / "research_queue.jsonl", d / "research_answers.jsonl"
        )
        if not open_q:
            console.print("[green]Nothing pending -- every queued question "
                          "has an answer.[/]")
            return
        console.print(f"[yellow]{len(open_q)} question(s) still unanswered[/]")
        console.print()
        for q in open_q:
            when = datetime.fromtimestamp(q.get("ts", 0)).strftime("%d %b %H:%M")
            console.print(f"[dim]{when}[/]  [bold]{q.get('topic')}[/]  {q.get('text')}")
            console.print(f"        [dim]why: {q.get('why', '')}[/]")
            console.print()
        return

    offerings, _alts = load_bonds()
    holds, _cash, alerts = load_holdings()
    try:
        reports = analyse(holds, cfg.stocks, alerts) if holds else []
    except Exception:  # noqa: BLE001 -- a network blip must not block the queue
        reports = []

    questions = build_questions(
        offerings=offerings,
        reports=reports,
        bonds_file_age_days=file_age_days(PROJECT_ROOT / "config" / "bonds.yaml"),
    )

    if show:
        for q in questions:
            tag = "[red]HIGH[/]" if q.urgency == "high" else "[dim]    [/]"
            console.print(f"{tag} [bold]{q.topic}[/]  {q.text}")
            console.print(f"       [dim]why: {q.why}[/]")
            console.print()

    if queue:
        path = d / "research_queue.jsonl"
        n = queue_questions(path, questions)
        console.print(f"[green]Queued {n} question(s)[/] -> {path}")
        console.print(
            "[dim]A Claude Code session watching this file will answer them. "
            "Nothing is answered while no session is open -- the rules keep "
            "running regardless.[/]"
        )


# ------------------------------------------------------------------- todo
def todo(
    notify: bool = typer.Option(False, help="Send the list to the app chat"),
) -> None:
    """Everything the tool currently thinks you should do, worst first.

    The same list the dashboard shows. Ranked by what actually hurts: the safety
    net above allocation drift, drift above a bond offering you can skip.
    """
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    cfg = load_config()
    from .actions import build_actions
    from .bonds import load_bonds
    from .holdings import analyse, load_holdings
    from .networth import live_crypto_value, load_networth

    holds, _cash, alerts = load_holdings()
    reports = analyse(holds, cfg.stocks, alerts) if holds else []
    stock_value = sum(r.market_value for r in reports)
    nw = load_networth(stock_value=stock_value, crypto_value=live_crypto_value())
    reviews = [
        sell_signals(
            r, portfolio_value=stock_value or 1.0, net_worth=nw.total,
            max_position_pct=cfg.stocks.max_position_pct,
        )
        for r in reports
    ]
    offerings, _alts = load_bonds()

    j = Journal(cfg.db_path)
    bot = {"halted": bool(j.get_state("halted", False))}

    acts = build_actions(nw, offerings=offerings, reviews=reviews, bot=bot,
                         weight_ceiling=cfg.stocks.max_position_pct)

    shown = acts
    if not shown:
        console.print("[green]Nothing to do.[/] Everything is within target.")
        return

    colour = {"urgent": "red", "soon": "yellow", "idea": "dim"}
    for a in shown:
        console.print(
            f"[{colour.get(a.severity, '')}]{a.severity.upper():6s}[/] "
            f"[bold]{a.title}[/]"
        )
        console.print(f"      {a.detail}")
        if a.stale_hint:
            console.print(
                f"      [dim]once done, update {a.stale_hint} so the numbers stay true[/]"
            )
        console.print()

    console.print(f"[bold]{len(acts)} open[/]")
    console.print("[dim]Every item is computed from live numbers, so acting on "
                  "one makes it disappear on its own.[/]")

    if notify:
        lines = ["What to do next:"]
        for a in shown[:6]:
            lines.append(("! " if a.severity == "urgent" else "- ") + a.title)
        build_notifier(cfg.data_dir).send("\n".join(lines))
        console.print("\n[green]Sent to the app chat.[/]")


# --------------------------------------------------------------- daily digest
def daily(
    notify: bool = typer.Option(True, help="Send the digest to the app chat"),
    budget: float = typer.Option(
        0.0, help="Money to screen for (0 = your monthly surplus, not your cash)"
    ),
) -> None:
    """One digest: crypto engine state, holdings, sell review, and new IDX ideas.

    Intended for a scheduled run once per trading day after the IDX close
    (~16:15 WIB). See README for the Windows Task Scheduler entry.
    """
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    cfg = load_config()
    from .holdings import SUBSCRIPTION_IDR, analyse, load_holdings
    from .screener import (
        BANK_SECTORS,
        affordable,
        build_basket,
        fetch_candidates,
        load_universe,
        score,
    )

    holds, cash, alerts = load_holdings()

    # The screener budget is the money you are about to ADD, not the money you
    # are holding. It used to default to `cash` -- the BCA balance -- so the
    # digest cheerfully proposed spending the emergency fund on equities every
    # single day. Monthly surplus is the honest source: it is fresh, it arrives
    # on a schedule, and spending it breaks nothing.
    if not budget:
        try:
            from .networth import load_networth

            budget = max(0.0, load_networth(stock_value=0.0).cashflow.surplus)
        except Exception:  # noqa: BLE001
            budget = 0.0
    parts: list[str] = [f"📅 Daily digest — {time.strftime('%Y-%m-%d')}"]

    # -- crypto ---------------------------------------------------------
    try:
        j = Journal(cfg.db_path)
        curve = j.equity_curve(1)
        eq = float(curve[-1]["equity"]) if curve else 0.0
        halted = bool(j.get_state("halted", False))
        pnl30 = j.realized_pnl_since(int(time.time()) - 30 * 86400)
        stale = j.seconds_since_heartbeat()
        parts.append(
            f"\n🤖 Crypto ({j.get_state('mode', 'paper')})"
            f"\nequity {_rupiah(eq)} · 30d P&L {_rupiah(pnl30)}"
            + ("\n🛑 HALTED" if halted else "")
            + (
                f"\n⚠️ engine silent {stale / 60:.0f} min — stop-losses are NOT running"
                if stale > cfg.execution.heartbeat_stale_sec
                else ""
            )
        )
    except Exception as exc:  # noqa: BLE001
        parts.append(f"\n🤖 Crypto: unavailable ({exc})")

    # -- holdings + sell review -----------------------------------------
    income = 0.0
    reports = []
    if holds:
        reports = analyse(holds, cfg.stocks, alerts)
        pv = sum(r.market_value for r in reports) + cash
        income = sum(r.monthly_income for r in reports)
        parts.append("\n📋 Holdings")
        for r in reports:
            short = r.holding.ticker.replace(".JK", "")
            rev = sell_signals(r, portfolio_value=pv, net_worth=_net_worth_total())
            parts.append(
                f"{short} {_rupiah(r.price)} ({r.unrealised_pct:+.2f}%) "
                f"· {r.yield_on_market_pct:.1f}% yield · {_verdict(rev, plain=True)}"
            )
            for a in r.alerts:
                parts.append(f"  ⚠️ {a}")

    # -- new ideas -------------------------------------------------------
    if budget > 0:
        try:
            owned = {h.ticker for h in holds}
            cands = fetch_candidates([t for t in load_universe() if t not in owned])
            ranked = affordable(score(cands, budget=budget, avoid_sectors=BANK_SECTORS), budget)
            picks = build_basket(ranked, budget, n=3)
            if picks:
                add = sum(lots * 100 * c.ttm_div for c, lots in picks) / 12
                parts.append(f"\n💡 Ideas for {_rupiah(budget)} of new money")
                for c, lots in picks:
                    parts.append(
                        f"{c.short} {lots} lot @ {c.price:,.0f} "
                        f"= {_rupiah(lots * c.lot_cost)} · {c.yield_pct:.1f}%"
                    )
                parts.append(f"would add {_rupiah(add)}/month")
                income += add
        except Exception as exc:  # noqa: BLE001
            parts.append(f"\n💡 Screen failed: {exc}")

    received = _passive_income(reports)
    parts.append(
        f"\n💰 Income {_rupiah(received)}/month "
        f"= {received / SUBSCRIPTION_IDR * 100:.0f}% of your subscription"
    )

    # Payday: put the decision in front of you on the day the money lands,
    # rather than relying on remembering a week later.
    from .networth import load_networth

    # Bound before the try so the checklist below can test it. Assigning it in
    # the except instead would wipe a perfectly good value whenever the failure
    # happened later in the block, after load_networth had already succeeded.
    # Keep spending_monthly honest before anything is computed from it. A typed
    # budget drifts, and surplus, emergency target and years-to-goal all drift
    # with it. Guarded inside: complete months only, and a cap on how far one
    # sync may move the figure.
    try:
        from .spending import connect as spend_connect
        from .web.settings import sync_spending_from_actuals

        synced = sync_spending_from_actuals(spend_connect(cfg.db_path))
        if synced.get("changed"):
            parts.append(
                "\n📐 Spending updated from actuals: "
                f"{_rupiah(synced['was'])} -> {_rupiah(synced['spending_monthly'])}"
                f" ({synced['months_used']} months)"
            )
        elif synced.get("reason"):
            # A guard that fires into silence is indistinguishable from a guard
            # that never needed to fire. The sync refuses when actuals diverge
            # too far to apply unattended -- and that refusal is exactly the
            # moment the stored figure is most wrong, so it has to be said.
            actual = synced.get("actual_average")
            parts.append(
                "\n! Spending NOT updated from actuals: " + synced["reason"]
                + (
                    f"\n   baseline {_rupiah(synced['spending_monthly'])}"
                    f" vs actual {_rupiah(actual)}"
                    if actual else ""
                )
            )
    except Exception:  # noqa: BLE001 -- the digest matters more than the tidy-up
        pass

    nw = None
    try:
        nw = load_networth(stock_value=sum(r.market_value for r in reports) if reports else 0.0)
        cf = nw.cashflow
        if cf.is_payday():
            parts.append(f"\n🌾 PAYDAY - deploy {_rupiah(cf.surplus)}")
            for bucket, amt in sorted(nw.allocate_surplus(), key=lambda x: -x[1]):
                if amt >= 50_000:
                    parts.append(f"  {bucket}: {_rupiah(amt)}")
            parts.append("  details: lumbung payday")
        elif 0 < cf.days_until_payday() <= 3:
            parts.append(f"\n🌾 Payday in {cf.days_until_payday()} days.")
    except Exception as exc:  # noqa: BLE001
        parts.append(f"\n(payday check failed: {exc})")

    # --- what to move -------------------------------------------------------
    # The digest already answers what to sell (holdings review) and what to buy
    # (ideas). Allocation drift is the third question and the one most easily
    # forgotten, because nothing ever prompts it: money sits in the wrong bucket
    # quietly for months. Only still-open items appear, so ticking one on the
    # dashboard takes it out of tomorrow's message.
    if nw is not None:
        try:
            from .actions import build_actions
            from .bonds import load_bonds

            offerings, _alts = load_bonds()
            acts = build_actions(nw, offerings=offerings, reviews=[], bot=None)

            # deploy_surplus is skipped: the payday block above already says it,
            # and a digest that says the same thing twice trains you to skim.
            todo = [a for a in acts if a.kind != "deploy_surplus"]
            if todo:
                parts.append("\n✅ To do")
                for a in todo[:4]:
                    mark = "!" if a.severity == "urgent" else "-"
                    parts.append(f"  {mark} {a.title}")
                if len(todo) > 4:
                    parts.append(f"  ...and {len(todo) - 4} more on the dashboard")
        except Exception as exc:  # noqa: BLE001
            parts.append(f"\n(checklist failed: {exc})")

    msg = "\n".join(parts)
    console.print(msg)
    if notify:
        build_notifier(cfg.data_dir).send(msg)
        console.print("\n[green]Sent to the app chat.[/]")
