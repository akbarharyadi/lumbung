"""One conversation, one front door.

Every command Lumbung answers lives here. It used to be split: half of them were
closures inside the engine's Telegram wiring and half were here, so the two
interfaces could only agree by copying -- and two copies of "what should I do
about BBCA" drift apart exactly when it matters. Telegram is gone now, and with
it the reason for the split.

What lives here: everything answerable from config, the database and the price
feed -- which, since the engine writes its whole state to the journal, includes
`status`, `positions` and `pnl`.

The four controls (`pause`, `resume`, `flat`, `kill`) also work through the
journal rather than by reaching into the engine object: they set state and the
engine acts on its next loop. Only the engine process may talk to the exchange,
or two writers race on the same positions. They are gated on `writable`, so the
read-only public deployment offers answers and no levers.

Anything that is not a known command is queued for a Claude Code session to
answer in prose.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)


def _rupiah(v: float) -> str:
    return f"Rp {v:,.0f}"


def _amount(raw: str) -> float:
    """Read '5jt', '500rb', '1.500.000' or '1500000' as rupiah."""
    t = str(raw).strip().lower().replace(",", "").replace(".", "").replace(" ", "")
    mult = 1_000_000 if "jt" in t else (1_000 if "rb" in t else 1)
    t = t.replace("jt", "").replace("rb", "")
    if not t:
        raise ValueError(f"could not read {raw!r} as an amount")
    return float(t) * mult


# --------------------------------------------------------------------- queue
def queue_question(path: str | Path, text: str, source: str = "app") -> bool:
    """Hand a free-form question to whoever is watching the queue."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"ts": int(time.time()), "text": text, "source": source},
                ensure_ascii=False,
            ) + "\n")
        return True
    except OSError as exc:
        log.warning("could not queue question: %s", exc)
        return False


def follow(path: str | Path, offset: int) -> tuple[list[str], int]:
    """Whole lines appended since `offset`, and the offset to resume from.

    Byte offset, never a line count: pruning the file makes a line counter see a
    smaller number and replay the entire backlog as new.

    A trailing fragment -- a line still being written -- is left unconsumed, so
    the next call returns it whole rather than as two broken halves.
    """
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError:
        return [], offset
    if size < offset:          # truncated or rewritten: follow it down
        return [], size
    if size == offset:
        return [], offset
    with p.open("rb") as fh:
        fh.seek(offset)
        chunk = fh.read(size - offset)
    consumed = chunk.rfind(b"\n") + 1
    if not consumed:           # no complete line yet
        return [], offset
    text = chunk[:consumed].decode("utf-8", "replace")
    return [ln for ln in (l.strip() for l in text.splitlines()) if ln], offset + consumed


def read_answers(path: str | Path, limit: int = 50) -> list[dict]:
    """Answers written back by a Claude Code session, newest last."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out[-limit:]


# ------------------------------------------------------------------ commands
def build_commands(cfg, *, writable: bool = False) -> dict[str, Callable[[list[str]], str]]:
    """Every command answerable without holding the engine object.

    `writable` adds the four controls. It is off by default so that a caller who
    forgets to think about it gets the safe deployment, not the loaded one.
    """

    def _nw():
        from .holdings import analyse, load_holdings
        from .networth import live_crypto_value, load_networth

        holds, _cash, alerts = load_holdings()
        try:
            reports = analyse(holds, cfg.stocks, alerts) if holds else []
        except Exception:  # noqa: BLE001 -- a price blip must not blank the answer
            reports = []
        sv = sum(r.market_value for r in reports)
        return load_networth(stock_value=sv, crypto_value=live_crypto_value()), reports

    def helpc(_: list[str]) -> str:
        return (
            "/todo — what to do next\n"
            "/networth — the whole picture\n"
            "/spend 250rb groceries food — log a purchase\n"
            "/income 5jt bonus tahunan bonus — money outside salary\n"
            "/expenses — last 30 days\n"
            "/stock BBRI 20 4520 — add or update a holding\n"
            "/asset — list; /asset rename <old> <new>; /sell <name> [amount]\n"
            "/wish — considering list; /wish Oven 730rb [note]\n"
            "/payday — deployment plan\n"
            "/status · /positions · /pnl — the bot\n"
            + ("/pause · /resume · /flat · /kill — the bot's levers\n" if writable else "")
            + "/about · /help\n\n"
            "Anything else is a question, and gets answered in words."
        )

    def about(_: list[str]) -> str:
        """Who this is.

        Answered here rather than through the ask-queue so the identity is the
        same whether or not anyone is reading the queue -- something that
        introduces itself differently depending on who happens to be watching
        has no identity at all.
        """
        return (
            "I am Lumbung — your rice granary.\n\n"
            "I keep what you put in, keep it dry, and tell you when it is time "
            "to add or to take out. I do not promise a big harvest. I make sure "
            "nothing rots.\n\n"
            "What I do:\n"
            "• trade crypto on Indodax, automatically, inside fixed risk limits\n"
            "• signal IDX stocks — you press the button\n"
            "• bonds, savings, gold — worked out net of tax\n"
            "• record spending, plan payday\n\n"
            "What I never do: promise a profit, guess the market, or move money "
            "off an exchange.\n\n"
            "I am a program, not a person — but my memory is long and my numbers "
            "are honest.\n\n"
            "/help for the command list."
        )

    def networth(_: list[str]) -> str:
        nw, _reports = _nw()
        out = [f"Net worth {_rupiah(nw.total)}", ""]
        for name, b in sorted(nw.buckets.items(), key=lambda kv: -kv[1].value):
            if b.value <= 0 and b.target_pct <= 0:
                continue
            drift = b.value / nw.total * 100 - b.target_pct * 100 if nw.total else 0
            out.append(f"{name} {_rupiah(b.value)} · {b.value / nw.total * 100:.0f}%"
                       f" (target {b.target_pct * 100:.0f}%, {drift:+.0f})")
        out.append("")
        out.append(f"Liquid {_rupiah(nw.liquid_now)} = "
                   f"{nw.months_covered_liquid:.1f} months")
        return "\n".join(out)

    def todo_cmd(_: list[str]) -> str:
        from .actions import build_actions
        from .bonds import load_bonds
        from .goal import sell_signals
        from .journal import Journal

        nw, reports = _nw()
        sv = sum(r.market_value for r in reports)
        reviews = [
            sell_signals(r, portfolio_value=sv or 1.0, net_worth=nw.total,
                         max_position_pct=cfg.stocks.max_position_pct)
            for r in reports
        ]
        offerings, _alts = load_bonds()
        j = Journal(cfg.db_path)
        acts = build_actions(
            nw, offerings=offerings, reviews=reviews,
            weight_ceiling=cfg.stocks.max_position_pct,
            bot={"halted": bool(j.get_state("halted", False))},
        )
        if not acts:
            return "Nothing outstanding — all within target."

        out = []
        for emoji, heading, sev in (
            ("🔴", "NOW", "urgent"), ("🟡", "SOON", "soon"),
            ("⚪", "WHEN YOU CAN", "idea"),
        ):
            rows = [a for a in acts if a.severity == sev]
            if not rows:
                continue
            out.append(f"{emoji} {heading}")
            out.append("")
            for a in rows:
                out.append(a.title)
                out.append(a.detail)
                if a.stale_hint:
                    out.append(f"→ then update {a.stale_hint}")
                out.append("")
        return "\n".join(out).rstrip()

    def expenses_cmd(_: list[str]) -> str:
        from .spending import by_category, connect, recent

        conn = connect(cfg.db_path)
        rows = recent(conn, 30)
        if not rows:
            return "Nothing logged in the last 30 days."
        total = sum(r["amount"] for r in rows)
        out = [f"Last 30 days: {_rupiah(total)} ({len(rows)} purchases)", ""]
        for cat, amt, n in by_category(conn, 30)[:6]:
            out.append(f"{cat}: {_rupiah(amt)} ({n})")
        return "\n".join(out)

    def spend_cmd(args: list[str]) -> str:
        from .spending import CATEGORIES, connect, record

        if not args:
            return ("Usage: /spend <amount> <what> [category]\n"
                    "e.g. /spend 250rb groceries food\n"
                    "categories: " + ", ".join(CATEGORIES))
        try:
            amount = _amount(args[0])
        except ValueError:
            return f"I could not read {args[0]!r} as an amount. Try 250rb or 1.5jt"
        if amount <= 0:
            return "Amount must be positive."

        rest, category = args[1:], "other"
        if rest and rest[-1].lower() in CATEGORIES:
            category, rest = rest[-1].lower(), rest[:-1]
        item = " ".join(rest) or "unspecified"

        record(connect(cfg.db_path), amount=amount, item=item,
               category=category, method="cash")
        out = [f"Logged {_rupiah(amount)} — {item} ({category})"]
        try:
            from .web.settings import add_to_cash

            out.append("Cash on hand: " + _rupiah(add_to_cash(-amount)))
        except Exception:  # noqa: BLE001 -- the log matters more than the tally
            pass
        return "\n".join(out)

    def income_cmd(args: list[str]) -> str:
        from .spending import INCOME_KINDS, connect, record_income, undeployed_income

        if not args:
            return ("Usage: /income <amount> <where from> [kind]\n"
                    "e.g. /income 5jt bonus tahunan bonus\n"
                    "kinds: " + ", ".join(INCOME_KINDS))
        try:
            amount = _amount(args[0])
        except ValueError:
            return f"I could not read {args[0]!r} as an amount. Try 500rb or 5jt"
        if amount <= 0:
            return "Amount must be positive."

        rest, kind = args[1:], "other"
        if rest and rest[-1].lower() in INCOME_KINDS:
            kind, rest = rest[-1].lower(), rest[:-1]
        source = " ".join(rest) or "unspecified"

        conn = connect(cfg.db_path)
        record_income(conn, amount=amount, source=source, kind=kind)
        out = [f"Recorded {_rupiah(amount)} — {source} ({kind})"]
        waiting = undeployed_income(conn)
        if waiting > amount:
            out.append("Not yet deployed: " + _rupiah(waiting))
        try:
            from .web.settings import add_to_cash

            out.append("Cash on hand: " + _rupiah(add_to_cash(amount)))
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(out)

    def stock_cmd(args: list[str]) -> str:
        from .web.settings import SettingsError, write_holding

        if len(args) < 3:
            return ("Usage: /stock <ticker> <lots> <avg price> [tp] [cl]\n"
                    "e.g. /stock BBRI 20 4520\n"
                    "Remove one with lots = 0.")
        try:
            r = write_holding({
                "ticker": args[0], "lots": args[1], "avg_price": args[2],
                "take_profit": args[3] if len(args) > 3 else 0,
                "cut_loss": args[4] if len(args) > 4 else 0,
            })
        except SettingsError as exc:
            return f"Could not save: {exc}"
        return (f"{r['ticker']} {r['action']}. {r['holdings']} holding(s) now.\n"
                "Morning research will start asking about its dividends and news.")

    def wish_cmd(args: list[str]) -> str:
        from .web.settings import SettingsError, read_wishes, write_wish

        if not args:
            out = ["Your considering list"]
            ws = read_wishes()
            for w in ws:
                note = f" — {w['note']}" if w["note"] else ""
                out.append(f"{w['name']} · {_rupiah(w['amount_idr'])}{note}")
            if not ws:
                out.append("(empty — nothing being considered yet)")
            out += ["", "/wish <name> <amount> [note] — add or update",
                    "/wish remove <name>",
                    "Wishes never bind the safety net; they only price a want."]
            return "\n".join(out)

        if args[0].lower() == "remove" and len(args) > 1:
            try:
                r = write_wish(" ".join(args[1:]), remove=True)
            except SettingsError as exc:
                return f"Could not remove: {exc}"
            return f"Removed {r['name']} from considering."

        if len(args) < 2:
            return ("Usage: /wish <name> <amount> [note]\n"
                    "e.g. /wish Oven 730rb Sharp EO-28LP, dual heater\n"
                    "Remove with /wish remove <name>")
        try:
            r = write_wish(args[0], amount=args[1],
                           note=" ".join(args[2:]))
        except SettingsError as exc:
            return f"Could not save: {exc}"
        return (f"{r['name']} ({_rupiah(r['amount_idr'])}) {r['action']} to "
                "considering. It prices the want — it never binds the "
                "safety net.")

    def asset_cmd(args: list[str]) -> str:
        from .web.settings import (
            ASSET_KINDS,
            SettingsError,
            read_assets,
            rename_asset,
            write_asset,
        )

        if not args:
            out = ["Your assets"]
            for a in read_assets():
                r = f" · {a['rate'] * 100:.2f}%" if a["rate"] else ""
                out.append(f"{a['name']} · {a['kind']} · {_rupiah(a['value_idr'])}{r}")
            out += ["", "/asset <name> <kind> <value> [rate%]",
                    "/asset rename <old> <new>",
                    "/sell <name> [amount] — into cash",
                    "kinds: " + ", ".join(ASSET_KINDS)]
            return "\n".join(out)
        try:
            if args[0].lower() == "rename":
                if len(args) < 3:
                    return "Usage: /asset rename <old name> <new name>"
                r = rename_asset(args[1], " ".join(args[2:]))
                return f"Renamed {r['was']} → {r['name']}"
            kind = args[1].lower() if len(args) > 1 and args[1].lower() in ASSET_KINDS else ""
            rest = args[2:] if kind else args[1:]
            value = _amount(rest[0]) if rest else None
            r = write_asset(args[0], kind=kind, value=value,
                            rate=rest[1] if len(rest) > 1 else None)
        except (SettingsError, ValueError) as exc:
            return f"Could not save: {exc}"
        return f"{r['name']} {r['action']}. {r['assets']} asset(s) now."

    def sell_cmd(args: list[str]) -> str:
        from .web.settings import SettingsError, sell_asset

        if not args:
            return ("Usage: /sell <name> [amount]\n"
                    "e.g. /sell SR025      (all of it)\n"
                    "     /sell gold 5jt   (part)\n"
                    "Proceeds are added to cash on hand.")
        try:
            r = sell_asset(args[0], _amount(args[1]) if len(args) > 1 else None)
        except (SettingsError, ValueError) as exc:
            return f"Could not sell: {exc}"
        out = [f"Sold {_rupiah(r['sold'])} of {r['name']}."]
        out.append("Remaining: " + _rupiah(r["remaining"]) if r["remaining"] > 0
                   else "Position closed.")
        out.append("Cash on hand: " + _rupiah(r["cash"]))
        return "\n".join(out)

    def payday_cmd(args: list[str]) -> str:
        nw, _reports = _nw()
        amount = _amount(args[0]) if args else nw.cashflow.surplus
        split = nw.allocate_surplus(amount)
        if not split:
            return "Nothing to deploy — everything is at or above target."
        out = [f"Deploy {_rupiah(amount)}", ""]
        for bucket, amt in sorted(split, key=lambda x: -x[1]):
            if amt < 50_000:
                continue
            out.append(f"{bucket}: {_rupiah(amt)}")
            where = nw.providers.get(bucket, "")
            if where:
                out.append(f"  {where}")
        return "\n".join(out)

    _snap: dict = {}

    def _portfolio(max_age: float = 300.0):
        """Whole-portfolio snapshot, cached briefly.

        Stock prices do not move enough in five minutes to justify making
        someone wait for yfinance on every tap.
        """
        if _snap and time.time() - _snap.get("at", 0) < max_age:
            return _snap.get("data")
        try:
            from .holdings import SUBSCRIPTION_IDR, analyse, load_holdings
            from .networth import live_crypto_value, load_networth

            holds, _cash, alerts = load_holdings()
            reports = analyse(holds, cfg.stocks, alerts) if holds else []
            nw = load_networth(
                stock_value=sum(r.market_value for r in reports),
                crypto_value=live_crypto_value(),
            )
            data = {
                "nw": nw, "reports": reports, "subscription": SUBSCRIPTION_IDR,
                "div": sum(r.monthly_income for r in reports),
                "interest": nw.savings_income_monthly,
            }
        except Exception as exc:  # noqa: BLE001 -- a price blip must not blank status
            log.warning("portfolio snapshot failed: %s", exc)
            return None
        _snap["at"] = time.time()
        _snap["data"] = data
        return data

    # ------------------------------------------------------------- engine
    # Read through the journal, never through the engine object: the web server
    # and the engine are separate processes, and the journal is the only thing
    # they both already hold open.
    def _journal():
        from .journal import Journal

        return Journal(cfg.db_path)

    def status_cmd(_: list[str]) -> str:
        """The whole picture, not just the bot -- the bot is a small part of it.

        Everything here is read from the journal rather than from a live
        `Engine`. The engine writes its state there on every loop, so this is
        the same answer without needing to be the same process -- and it still
        answers when the engine is down, which is exactly when it is asked.
        """
        j = _journal()
        mode = str(j.get_state("mode", "paper"))
        halted = bool(j.get_state("halted", False))
        halt_reason = str(j.get_state("halt_reason", "") or "")
        hb = j.seconds_since_heartbeat()
        stale = hb > cfg.execution.heartbeat_stale_sec

        curve = j.equity_curve(1)
        eq = float(curve[-1]["equity"]) if curve else 0.0
        peak = float(j.get_state("peak_equity", eq) or eq)
        day_start = float(j.get_state("day_start_equity", eq) or eq)
        drawdown = (eq / peak - 1) * 100 if peak else 0.0
        day_pnl = (eq / day_start - 1) * 100 if day_start else 0.0
        pos = j.positions()

        out = ["🌾 LUMBUNG — " + mode.upper() + (" — HALTED" if halted else "")]

        # Bad news first and plainly. A dead engine leads the message; it is
        # never left for the reader to infer from a stale heartbeat further
        # down.
        if halted:
            out.append("HALTED: " + (halt_reason or "no reason recorded"))
        elif stale:
            out.append(
                "NO HEARTBEAT for "
                + ("ever" if hb == float("inf") else f"{hb / 60:.0f} min")
                + " — the engine may not be running"
            )

        d = _portfolio()
        if d:
            nw = d["nw"]
            total = nw.total
            passive = d["div"] + d["interest"]
            out.append("")
            out.append("PASSIVE INCOME")
            if d["div"]:
                out.append("  stock dividends  " + _rupiah(d["div"]))
            if d["interest"]:
                out.append("  interest+coupons " + _rupiah(d["interest"]))
            out.append("  total            " + _rupiah(passive) + "/month")
            if d["subscription"]:
                out.append(
                    f"  = {passive / d['subscription'] * 100:.0f}% of your subscription"
                )

            out.append("")
            out.append("NET WORTH  " + _rupiah(total))
            for label in ("stocks", "bonds", "gold", "savings", "cash", "crypto"):
                b = nw.buckets.get(label)
                if not b or (b.value == 0 and b.target_pct == 0):
                    continue
                w = b.weight(total) * 100
                flag = " <" if abs(w - b.target_pct * 100) >= 10 else ""
                out.append(
                    f"  {label:9s} " + _rupiah(b.value).rjust(15)
                    + f"  {w:4.1f}% (t{b.target_pct * 100:.0f}%){flag}"
                )

            out.append("")
            out.append("LIQUID  " + _rupiah(nw.liquid_now)
                       + f"  = {nw.months_covered_liquid:.1f} months")

            from .goal import DEFAULT_BLENDED, blended_yield, plan_income_goal

            target = nw.goals.monthly_income_target
            # The same blend the dashboard uses: derived from the allocation, so
            # a bucket that pays nothing drags it down exactly as it does in life.
            rate = blended_yield(
                {n: b.target_pct for n, b in nw.buckets.items()}
            ) or DEFAULT_BLENDED
            g = plan_income_goal(
                monthly_target=target, current_capital=total,
                monthly_contribution=nw.cashflow.surplus, blended_return=rate,
            )
            yrs = g.years_to_target()
            out.append("GOAL    " + (f"{yrs} years" if yrs else "not within 60y")
                       + f" to {_rupiah(target)}/month"
                       + (f"  ({min(100, total / g.capital_required * 100):.0f}% there)"
                          if g.capital_required else ""))

        out.append("")
        out.append(f"BOT ({mode})")
        out.append("  equity    " + _rupiah(eq))
        out.append(f"  positions {len(pos)}"
                   + (f"  {', '.join(sorted(pos))}" if pos else ""))
        out.append(f"  drawdown  {drawdown:.2f}%")
        out.append(f"  day P&L   {day_pnl:+.2f}%")
        out.append("  heartbeat "
                   + ("never" if hb == float("inf") else f"{hb:.0f}s ago"))

        # Only surface what would change a decision.
        notes = []
        if d:
            nw = d["nw"]
            lbl, _val, wt = nw.largest_position()
            if wt > 0.40:
                notes.append(f"{lbl} is {wt * 100:.0f}% of net worth")
            if nw.emergency_shortfall > 0:
                notes.append("safety net short by " + _rupiah(nw.emergency_shortfall))
            try:
                from .spending import connect as _sc
                from .spending import spending_profile as _sp

                prof = _sp(_sc(cfg.db_path), nw.cashflow.spending_monthly)
                if not prof.tracked:
                    notes.append("spending is still an estimate, not measured")
                elif prof.verdict == "over budget":
                    notes.append(f"spending {prof.variance_pct():+.0f}% over budget")
            except Exception:  # noqa: BLE001 -- a note is never worth an error
                pass
        if notes:
            out.append("")
            out.append("ATTENTION")
            out.extend("  ! " + n for n in notes)
        return "\n".join(out)

    def positions_cmd(_: list[str]) -> str:
        from .exchanges.indodax_public import IndodaxPublicClient

        pos = _journal().positions()
        if not pos:
            return "No open positions."
        pub = IndodaxPublicClient()
        lines = []
        for pair, p in sorted(pos.items()):
            try:
                px = pub.last_price(pair)
            except Exception:  # noqa: BLE001 -- a dead feed must not blank the list
                px = p.entry_price
            lines.append(
                f"{pair} {p.qty:g} @ {p.entry_price:,.0f} → {px:,.0f}"
                f"  {(px / p.entry_price - 1) * 100 if p.entry_price else 0:+.1f}%"
            )
            lines.append(f"  stop {p.stop:,.0f} · R {p.r_multiple(px):+.2f}")
        return "\n".join(lines)

    def pnl_cmd(_: list[str]) -> str:
        j = _journal()
        now = int(time.time())
        return "\n".join([
            "Realized P&L",
            f"24h  {_rupiah(j.realized_pnl_since(now - 86400))}",
            f"7d   {_rupiah(j.realized_pnl_since(now - 7 * 86400))}",
            f"30d  {_rupiah(j.realized_pnl_since(now - 30 * 86400))}",
        ])

    def pause_cmd(_: list[str]) -> str:
        j = _journal()
        j.set_state("halted", True)
        j.set_state("halt_reason", "paused from chat")
        j.event("pause", "paused from chat")
        return "Paused. No new entries. Open positions are still managed. /resume to continue."

    def resume_cmd(_: list[str]) -> str:
        j = _journal()
        curve = j.equity_curve(1)
        eq = float(curve[-1]["equity"]) if curve else 0.0
        j.set_state("halted", False)
        j.set_state("halt_reason", "")
        # Rebase the peak, or the drawdown gate re-trips on the next loop.
        if eq:
            j.set_state("peak_equity", eq)
            j.set_state("day_start_equity", eq)
        j.event("resume", "resumed from chat")
        return f"Resumed. Equity peak rebased to {_rupiah(eq)}."

    def flat_cmd(_: list[str]) -> str:
        j = _journal()
        j.set_state("flatten_request", int(time.time()))
        j.event("flatten_request", "requested from chat")
        return "Flatten requested. The engine sells at market on its next loop."

    def kill_cmd(_: list[str]) -> str:
        cfg.halt_path.write_text("killed from chat\n", encoding="utf-8")
        j = _journal()
        j.set_state("flatten_request", int(time.time()))
        j.event("kill", "kill switch from chat", level="error")
        return (
            "Killed: flattening and halting.\n"
            f"Delete {cfg.halt_path} to let it start again."
        )

    cmds: dict[str, Callable[[list[str]], str]] = {}
    for names, fn in (
        (("help", "start"), helpc),
        (("about", "who", "whoami", "siapa"), about),
        (("networth", "net", "worth"), networth),
        (("todo", "recommend", "recomendation", "recommendation", "rekomendasi"), todo_cmd),
        (("expenses", "expense"), expenses_cmd),
        (("spend",), spend_cmd),
        (("income", "bonus", "masuk"), income_cmd),
        (("stock", "saham"), stock_cmd),
        (("wish", "considering", "wishlist"), wish_cmd),
        (("asset", "assets", "aset"), asset_cmd),
        (("sell", "jual"), sell_cmd),
        (("payday",), payday_cmd),
        (("status", "bot"), status_cmd),
        (("positions", "position", "posisi"), positions_cmd),
        (("pnl", "profit"), pnl_cmd),
    ):
        for n in names:
            cmds[n] = fn

    if writable:
        for names, fn in (
            (("pause", "stop"), pause_cmd),
            (("resume", "lanjut"), resume_cmd),
            (("flat", "flatten"), flat_cmd),
            (("kill",), kill_cmd),
        ):
            for n in names:
                cmds[n] = fn
    return cmds


def dispatch(
    cfg, text: str, *, ask_queue: str | Path | None = None, writable: bool = False
) -> dict:
    """Answer one message. Returns {reply, queued}.

    Unknown input is a question, not an error. Refusing it would make the chat a
    command line with a worse keyboard.
    """
    text = (text or "").strip()
    if not text:
        return {"reply": "", "queued": False}

    if text.startswith("/"):
        parts = text.split()
        name = parts[0].lstrip("/").split("@")[0].lower()
        fn = build_commands(cfg, writable=writable).get(name)
        if fn is not None:
            try:
                return {"reply": fn(parts[1:]), "queued": False}
            except Exception as exc:  # noqa: BLE001 -- never lose the chat to one bad command
                log.exception("chat command /%s failed", name)
                return {"reply": f"/{name} failed: {exc}", "queued": False}

    if ask_queue and queue_question(ask_queue, text):
        return {
            "reply": "Noted. Give me a moment to think about that.",
            "queued": True,
        }
    return {
        "reply": "I only understand commands right now. Try /help",
        "queued": False,
    }
