"""Purchase advice and expense commands: `buy`, `spend`, `expenses`."""

from __future__ import annotations

import logging
import time

import typer
from rich.console import Console
from rich.table import Table

from .config import load_config

console = Console()


def _rupiah(v: float) -> str:
    return f"Rp {v:,.0f}"


def register(app: typer.Typer) -> None:
    app.command()(buy)
    app.command()(spend)
    app.command()(expenses)


def _networth_and_goal_delay(price: float = 0.0):
    """Current balance sheet, plus how many months `price` adds to the goal.

    The delay is the number that changes behaviour. A price is abstract; "five
    weeks further from Rp 3jt a month" is a cost you can actually feel.
    """
    cfg = load_config()
    from .goal import plan_income_goal
    from .holdings import analyse, load_holdings
    from .networth import live_crypto_value, load_networth

    holds, _, alerts = load_holdings()
    reports = analyse(holds, cfg.stocks, alerts) if holds else []
    nw = load_networth(
        stock_value=sum(r.market_value for r in reports), crypto_value=live_crypto_value()
    )
    cf = nw.cashflow
    base = plan_income_goal(
        monthly_target=3_000_000, current_capital=nw.total, monthly_contribution=cf.surplus
    ).years_to_target()
    after = plan_income_goal(
        monthly_target=3_000_000,
        current_capital=max(0.0, nw.total - price),
        monthly_contribution=cf.surplus,
    ).years_to_target()
    delay = ((after - base) * 12) if (base is not None and after is not None) else 0.0
    return nw, delay


def _zero_percent_cards() -> list[str]:
    import yaml

    from .config import PROJECT_ROOT

    try:
        raw = yaml.safe_load(
            (PROJECT_ROOT / "config" / "holdings.yaml").read_text(encoding="utf-8")
        ) or {}
    except OSError:
        return []
    return [c.get("name", "?") for c in (raw.get("cards") or []) if c.get("zero_percent")]


def buy(
    item: str = typer.Argument(..., help="What you want to buy, e.g. 'RTX 5070 Ti'"),
    price: float = typer.Option(..., "--price", "-p", help="Price in IDR"),
    tenor: int = typer.Option(12, help="Instalment months, if you use a card"),
    zero: bool = typer.Option(
        True, "--zero/--no-zero", help="Is a genuine 0% instalment available here?"
    ),
) -> None:
    """Should you buy it, and how should you pay?

    Answered with arithmetic: what it does to your safety net, how far it pushes
    your income goal back, and whether cash or a card is actually cheaper.
    """
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    from .spending import _human_delay, advise_payment, assess

    nw, delay = _networth_and_goal_delay(price)
    cf = nw.cashflow
    # Net of what is already promised, but only the part your future paydays do
    # not already cover -- otherwise a bill due after two salaries reads as if
    # it had to come out of today's balance.
    liquid = (
        nw.buckets["cash"].value + nw.buckets["savings"].value
        - nw.committed_net_of_income()
    )

    v = assess(
        item=item, price=price, liquid=liquid,
        spending_monthly=cf.spending_monthly, surplus_monthly=cf.surplus,
        emergency_months=nw.emergency_months_target, goal_delay_months=delay,
    )

    colour = {"YES": "green", "TIGHT": "yellow", "NO": "red"}[v.verdict]
    console.print()
    console.print(f"[bold]{item}[/] — {_rupiah(price)}")
    console.print(f"[{colour}][bold]{v.verdict}[/][/]  {v.headline}")
    console.print()

    t = Table("", "")
    t.add_row(
        "liquid now",
        f"{_rupiah(v.liquid_before)}  [dim](cash + savings"
        + (", less what is promised" if nw.committed_net_of_income() > 0 else "")
        + ")[/]",
    )
    t.add_row("liquid after", _rupiah(v.liquid_after))
    t.add_row(
        "safety net after",
        f"{v.months_covered_after:.1f} months  [dim](target {v.emergency_target_months})[/]",
    )
    t.add_row("costs you", f"{v.surplus_months:.1f} months of saving")
    if delay >= 0.1:
        t.add_row("goal delay", f"{_human_delay(delay)} further from Rp 3jt/month")
    console.print(t)

    for w in v.warnings:
        console.print(f"[yellow]  ! {w}[/]")

    cards = _zero_percent_cards()
    adv = advise_payment(
        price, can_pay_cash=v.liquid_after >= 0,
        zero_percent_available=zero and bool(cards), tenor_months=tenor,
    )
    label = {"cash": "Pay cash", "credit0": "Use the 0% instalment", "wait": "Wait"}
    console.print()
    console.print(f"[bold]{label[adv.method]}[/]")
    if adv.method == "credit0" and cards:
        console.print(f"  Cards with 0% programmes: {', '.join(cards)}")
    console.print(f"  {adv.detail}")

    method = "credit0" if adv.method == "credit0" else "cash"
    console.print()
    console.print(
        f"[dim]Record it after: lumbung spend {price:.0f} --on '{item}' --method {method}[/]"
    )


def spend(
    amount: float = typer.Argument(..., help="Amount in IDR"),
    on: str = typer.Option(..., "--on", help="What you bought"),
    category: str = typer.Option("other", "--category", "-c", help="tech, food, transport..."),
    method: str = typer.Option("cash", "--method", "-m", help="cash | credit | credit0"),
    frm: str = typer.Option("", "--from", help="Deduct from this bucket: cash | savings | gold | bonds | crypto"),
    note: str = typer.Option("", "--note"),
) -> None:
    """Record a purchase, so the spending picture stays real rather than assumed.

    `--from` also moves the money. Without it the ledger records the purchase and
    the balance sheet does not change -- which is correct if you paid by credit,
    and a slow lie if you paid from an account and never said so.
    """
    from .networth import load_networth
    from .spending import CATEGORIES, connect, monthly_totals, record

    cfg = load_config()
    if category.lower() not in CATEGORIES:
        console.print(f"[yellow]Unknown category.[/] Known: {', '.join(CATEGORIES)}")
    conn = connect(cfg.db_path)
    record(conn, amount=amount, item=on, category=category, method=method, note=note)
    console.print(f"[green]Recorded[/] {_rupiah(amount)} — {on} ({category}, {method})")

    if frm:
        from .config import PROJECT_ROOT
        from .holdings_io import adjust, edit

        path = PROJECT_ROOT / "config" / "holdings.yaml"
        new_balance: list[float] = []
        try:
            edit(path, lambda doc: new_balance.append(adjust(doc, frm.lower(), -amount)))
        except ValueError as exc:
            console.print(f"[red]Balance not changed:[/] {exc}")
            raise typer.Exit(1) from None
        console.print(f"  {frm.lower()} is now {_rupiah(new_balance[0])}")
    elif method not in ("credit", "credit0"):
        # Silence here is how the balance sheet drifts. Say it every time.
        console.print(
            "  [yellow]Balance not changed.[/] Pass [bold]--from cash[/] "
            "(or savings, gold, bonds, crypto) to deduct it."
        )

    totals = monthly_totals(conn, months=1)
    if totals:
        month, spent = totals[-1]
        cf = load_networth(stock_value=0.0).cashflow
        budget = cf.budget
        if budget:
            console.print(
                f"  {month}: {_rupiah(spent)} recorded so far — "
                f"{spent / budget * 100:.0f}% of your {_rupiah(budget)} monthly budget"
            )


def expenses(days: int = typer.Option(90, help="How far back to look")) -> None:
    """Where the money actually went."""
    from .spending import by_category, connect, monthly_totals, recent

    cfg = load_config()
    conn = connect(cfg.db_path)
    rows = recent(conn, days)
    if not rows:
        console.print(f"[dim]Nothing recorded in the last {days} days.[/]")
        console.print("  Record with: lumbung spend 250000 --on 'groceries' -c food")
        return

    total = sum(r["amount"] for r in rows)
    console.print(f"\n[bold]{_rupiah(total)}[/] over {days} days · {len(rows)} purchases")

    t = Table("category", "spent", "n", "share")
    for cat, amt, n in by_category(conn, days):
        t.add_row(cat, _rupiah(amt), str(n), f"{amt / total * 100:.0f}%")
    console.print(t)

    months = monthly_totals(conn, months=6)
    if len(months) > 1:
        console.print("\n[bold]by month[/]")
        for m, amt in months:
            console.print(f"  {m}  {_rupiah(amt)}")

    rt = Table("when", "item", "amount", "how")
    for r in rows[:12]:
        rt.add_row(
            time.strftime("%d %b", time.localtime(r["ts"])),
            r["item"][:28], _rupiah(r["amount"]), r["method"],
        )
    console.print(rt)
