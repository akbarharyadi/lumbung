"""`lumbung bonds` -- compare SBN Ritel against savings, after tax."""

from __future__ import annotations

import logging
from datetime import date

import typer
from rich.console import Console
from rich.table import Table

from .config import load_config

console = Console()


def register(app: typer.Typer) -> None:
    app.command()(bonds)


def _rupiah(v: float) -> str:
    return f"Rp {v:,.0f}"


def bonds(
    amount: float = typer.Option(0.0, "--amount", "-a", help="How much to invest (IDR)"),
) -> None:
    """Which bond, which tenor, and is it better than leaving it in savings?

    Everything is compared NET of tax: SBN coupons are taxed 10%, savings and
    deposit interest 20%. Comparing the headline rates gets this backwards.
    """
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    cfg = load_config()
    from .bonds import DEPOSIT_TAX, SBN_TAX, load_bonds, recommend_tenor

    offerings, alts = load_bonds()
    today = date.today()

    if not amount:
        try:
            from .holdings import analyse, load_holdings
            from .networth import live_crypto_value, load_networth

            holds, _, alerts = load_holdings()
            reports = analyse(holds, cfg.stocks, alerts) if holds else []
            nw = load_networth(
                stock_value=sum(r.market_value for r in reports),
                crypto_value=live_crypto_value(),
            )
            amount = max(0.0, nw.buckets["bonds"].gap_idr(nw.total))
            months_buffer = nw.months_covered_liquid
            target = nw.emergency_months_target
        except Exception:  # noqa: BLE001
            amount, months_buffer, target = 5_000_000.0, 0.0, 6
    else:
        try:
            from .holdings import analyse, load_holdings
            from .networth import load_networth

            holds, _, alerts = load_holdings()
            reports = analyse(holds, cfg.stocks, alerts) if holds else []
            nw = load_networth(stock_value=sum(r.market_value for r in reports))
            months_buffer, target = nw.months_covered_liquid, nw.emergency_months_target
        except Exception:  # noqa: BLE001
            months_buffer, target = 0.0, 6

    console.print(f"\n[bold]Comparing {_rupiah(amount)}[/]  [dim]all figures net of tax[/]")

    open_now = [o for o in offerings if o.is_open(today)]
    if not open_now:
        console.print("\n[yellow]No SBN offering is open right now.[/]")
        upcoming = [o for o in offerings if o.opens > today]
        if upcoming:
            nxt = min(upcoming, key=lambda o: o.opens)
            console.print(f"  Next known: {nxt.series} opens {nxt.opens}")
        else:
            console.print(
                "  The calendar in config/bonds.yaml may be out of date — "
                "check kemenkeu.go.id or your Bibit/Bareksa app."
            )

    t = Table("option", "gross", "tax", "net", "per month", "access")
    for o in open_now:
        t.add_row(
            f"[bold]{o.series}[/] ({o.tenor_years}y)",
            f"{o.coupon * 100:.2f}%", f"{SBN_TAX:.0%}",
            f"[green]{o.net_coupon * 100:.2f}%[/]",
            _rupiah(o.monthly_income(amount)),
            "tradeable" if o.tradeable else "locked",
        )
    for a in alts:
        net = a.net(amount)
        tax = f"{DEPOSIT_TAX:.0%}" if a.taxed == "deposit" else "—"
        t.add_row(
            a.name, f"{a.rate * 100:.2f}%", tax, f"{net * 100:.2f}%",
            _rupiah(amount * net / 12),
            "instant" if a.liquid else "locked",
        )
    console.print(t)

    if open_now:
        best_bond = max(open_now, key=lambda o: o.net_coupon)
        best_alt = max(alts, key=lambda a: a.net(amount)) if alts else None
        if best_alt:
            gap = (best_bond.net_coupon - best_alt.net(amount)) * amount
            if gap > 0:
                console.print(
                    f"\n[green]{best_bond.series} pays {_rupiah(gap)}/year more[/] than "
                    f"{best_alt.name}, after tax — even though the headline rates look close."
                )
            else:
                console.print(
                    f"\n[yellow]{best_alt.name} actually wins here[/] once tax is applied."
                )

        pick, why = recommend_tenor(
            offerings, months_of_buffer=months_buffer, emergency_target=target
        )
        if pick:
            console.print(f"\n[bold]Suggested: {pick.series}[/] ({pick.tenor_years} years)")
            console.print(f"  {why}")
            console.print(
                f"  {_rupiah(amount)} pays about {_rupiah(pick.monthly_income(amount))}/month "
                f"until {pick.matures}."
            )
            console.print(f"  Liquidity: {pick.liquidity}")
            console.print(f"  Closes in [bold]{pick.days_left(today)} days[/] ({pick.closes}).")
            console.print(f"  Minimum {_rupiah(pick.min_idr)}. Buy via Bibit, Bareksa, or your bank.")

    console.print(
        "\n[dim]SBN is government-issued, so credit risk is minimal, but your money "
        "is committed for the tenor. Rates and the calendar live in "
        "config/bonds.yaml — update it when a new series opens.[/]"
    )
