"""`lumbung news` -- screen headlines for the things you hold."""

from __future__ import annotations

import logging

import typer
from rich.console import Console
from rich.table import Table

from .config import get_secrets, load_config, load_watchlist

console = Console()
SEV_COLOUR = {"high": "red", "medium": "yellow", "low": "green"}


def register(app: typer.Typer) -> None:
    app.command()(news)


def news(
    days: int = typer.Option(7, help="How far back to look"),
    watchlist: bool = typer.Option(
        False, "--watchlist", help="Also screen the IDX watchlist, not just holdings"
    ),
    all_headlines: bool = typer.Option(False, "--all", help="Print every headline found"),
    notify: bool = typer.Option(False, help="Send material findings to the app chat"),
) -> None:
    """Screen news for your holdings and flag anything material.

    Works without an API key (keyword flagging). Set OPENROUTER_API_KEY in .env
    for judgement about what actually matters.

    Advisory only — this never places an order.
    """
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    from .holdings import load_holdings
    from .news import screen

    cfg = load_config()
    sec = get_secrets()
    key = sec.openrouter_api_key.get_secret_value().strip()
    model = cfg.news.model

    holds, _, _ = load_holdings()
    tickers = [h.ticker for h in holds]
    if watchlist:
        tickers += [t for t in load_watchlist() if t not in tickers]
    if not tickers:
        console.print("[yellow]Nothing to screen.[/] Add holdings to config/holdings.yaml")
        raise typer.Exit(1)

    console.print(
        f"Screening {len(tickers)} ticker(s) over {days} days "
        + ("[dim](LLM assessment)[/]" if key else "[dim](keyword flagging — no API key)[/]")
    )

    results = screen(tickers, days=days, api_key=key, model=model)
    material, lines = [], []

    for ticker, arts, verdict in results:
        code = ticker.replace(".JK", "")
        if not arts:
            console.print(f"\n[dim]{code}: no recent news[/]")
            continue

        if verdict and verdict.material:
            material.append((code, verdict))
            colour = SEV_COLOUR.get(verdict.severity, "yellow")
            console.print()
            console.print(
                f"[{colour}][bold]{code}[/]  {verdict.severity.upper()}[/]  "
                f"[dim]{verdict.category}[/]"
            )
            console.print(f"  {verdict.summary}")
            if verdict.why:
                console.print(f"  [dim]{verdict.why}[/]")
            lines.append(f"\n📰 {code} ({verdict.severity}) — {verdict.category}")
            lines.append(f"  {verdict.summary}")
            if verdict.why:
                lines.append(f"  {verdict.why}")
        else:
            console.print(f"\n[green]{code}[/]: nothing material in {len(arts)} headlines")

        if all_headlines or (verdict and verdict.material):
            t = Table("when", "source", "headline")
            for a in arts[:8]:
                t.add_row(
                    f"{a.age_days:.0f}d" if a.published else "?",
                    a.source[:16],
                    a.title[:74],
                )
            console.print(t)

    console.print()
    if material:
        console.print(f"[bold]{len(material)} ticker(s) with something worth reading.[/]")
    else:
        console.print("[green]Nothing material found.[/]")
    console.print(
        "[dim]Advisory only. Headlines are third-party text and are never used as a "
        "trading signal.[/]"
    )

    if notify and lines:
        from .notify.app import build_notifier

        build_notifier(cfg.data_dir).send("📰 News screen" + "".join(lines))
        console.print("[green]Sent to the app chat.[/]")
