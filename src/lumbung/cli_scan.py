"""`lumbung scan` -- photograph a receipt, confirm, record."""

from __future__ import annotations

import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .config import get_secrets, load_config

console = Console()


def register(app: typer.Typer) -> None:
    app.command()(scan)


def _rupiah(v: float) -> str:
    return f"Rp {v:,.0f}"


def scan(
    image: Path = typer.Argument(None, help="Photo of the receipt or payment screenshot"),
    check: bool = typer.Option(False, "--check", help="Test the LLM endpoint and exit"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Record without confirming"),
    category: str = typer.Option("", "-c", help="Override the detected category"),
) -> None:
    """Read a receipt with your local vision model and record it as an expense.

    Uses LOCAL_LLM_URL / LOCAL_LLM_MODEL from .env. Any OpenAI-compatible server
    works (vLLM, Ollama, LM Studio, llama.cpp), and the image never leaves your
    machine. The model must be vision-capable.
    """
    logging.basicConfig(level=logging.ERROR)
    cfg = load_config()
    sec = get_secrets()
    base = sec.local_llm_url.strip()
    model = sec.local_llm_model.strip()

    if not base:
        console.print("[red]LOCAL_LLM_URL is not set in .env[/]")
        console.print("  e.g. LOCAL_LLM_URL=http://192.168.1.50:8000/v1")
        console.print("       LOCAL_LLM_MODEL=Qwen/Qwen3-VL-35B-A3B-Instruct")
        raise typer.Exit(1)
    if not model:
        console.print("[red]LOCAL_LLM_MODEL is not set in .env[/]")
        raise typer.Exit(1)

    if check:
        from .ocr import probe

        console.print(f"Probing [bold]{base}[/] …")
        p = probe(base, model, api_key=sec.local_llm_key.get_secret_value().strip())
        t = Table("check", "result")
        t.add_row("reachable", "[green]yes[/]" if p["reachable"] else "[red]no[/]")
        if p["models"]:
            t.add_row("models served", ", ".join(p["models"][:6]))
        t.add_row(
            f"'{model}' served",
            "[green]yes[/]" if p["model_present"] else "[yellow]not listed[/]",
        )
        t.add_row(
            "accepts images",
            {True: "[green]yes[/]", False: "[red]no[/]", None: "[dim]not tested[/]"}[p["vision"]],
        )
        console.print(t)
        if p["error"]:
            console.print(f"[yellow]{p['error']}[/]")
        if not p["reachable"]:
            console.print()
            console.print(
                "[yellow]Not reachable.[/] The endpoint is on your private mesh, so "
                "connect the VPN first — from an unconnected machine 100.64.x.x has "
                "nowhere to route to."
            )
        elif p["vision"] is False:
            console.print()
            console.print(
                "[yellow]The model will not take images.[/] "
                f"'{model}' has no -VL suffix, so it is almost certainly text-only. "
                "Receipt OCR needs a vision variant (e.g. a Qwen3-VL); serve one "
                "alongside it and point LOCAL_LLM_MODEL at that."
            )
        raise typer.Exit(0 if p["reachable"] else 1)

    if image is None:
        console.print("[red]Give me an image, or use --check to test the endpoint.[/]")
        raise typer.Exit(1)

    from .ocr import extract_receipt

    console.print(f"Reading [bold]{image.name}[/] with {model}…")
    r = extract_receipt(
        image, base_url=base, model=model,
        api_key=sec.local_llm_key.get_secret_value().strip(),
    )
    if r is None:
        console.print("[red]Could not read the receipt.[/]")
        console.print(
            "  If the model is text-only, OCR will not work — you need a vision "
            "(-VL) variant. Check the server log for the actual error."
        )
        raise typer.Exit(1)

    cat = (category or r.category).lower()
    t = Table("field", "value")
    t.add_row("merchant", r.merchant)
    t.add_row("amount", f"[bold]{_rupiah(r.amount)}[/]" if r.usable else "[red]unreadable[/]")
    t.add_row("date", r.date or "not visible")
    t.add_row("category", cat)
    t.add_row("method", r.method)
    if r.items:
        t.add_row("items", "\n".join(r.items))
    t.add_row(
        "confidence",
        f"{'[yellow]' if r.needs_review else '[green]'}{r.confidence:.0%}[/]",
    )
    console.print(t)

    if not r.usable:
        console.print("[red]No usable total — nothing recorded.[/]")
        console.print("  Record it by hand: lumbung spend <amount> --on '<what>'")
        raise typer.Exit(1)

    if r.needs_review:
        console.print(
            "[yellow]Low confidence.[/] Check the amount against the receipt before "
            "accepting — a misread digit becomes a wrong number you never notice."
        )

    if not yes and not typer.confirm(f"Record {_rupiah(r.amount)} as '{r.merchant}'?", default=True):
        console.print("[dim]Not recorded.[/]")
        raise typer.Exit(0)

    from .spending import connect, record

    conn = connect(cfg.db_path)
    record(
        conn, amount=r.amount, item=r.merchant, category=cat,
        method="cash" if r.method in ("unknown", "cash") else r.method,
        note=f"scanned from {image.name}",
    )
    console.print(f"[green]Recorded[/] {_rupiah(r.amount)} — {r.merchant} ({cat})")
