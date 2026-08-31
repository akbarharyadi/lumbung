"""Read-mostly dashboard API + PWA host.

Design decisions worth stating:

* **Bind to 0.0.0.0 but expect Tailscale.** The dashboard is meant to be reached
  over a private mesh, not the public internet. There is still a bearer token on
  every endpoint, because "it's on a private network" is exactly the assumption
  that stops being true one router change later.
* **Reads are cached.** yfinance is slow and rate-limited; a phone refreshing a
  dashboard must never trigger a fresh scrape. Prices cache for a few minutes,
  the journal (local SQLite) is read live.
* **Control endpoints do not touch the exchange directly.** They set flags the
  engine picks up on its next loop. Two processes issuing orders against one
  account is a good way to double-sell.
"""

from __future__ import annotations

import logging
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import get_secrets, load_config
from ..goal import DEFAULT_BLENDED, plan_income_goal, sell_signals
from ..journal import Journal

if TYPE_CHECKING:
    from .access import AccessAuth

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"

_cache: dict[str, tuple[float, Any, float]] = {}
CACHE_TTL = 300  # seconds; market prices move slower than a pull-to-refresh


def _config_stamp() -> float:
    """Newest mtime across the config files a snapshot depends on.

    Prices can be a few minutes stale without harm, but *your own numbers* must
    not be: editing holdings.yaml and then staring at an unchanged dashboard for
    five minutes reads as a broken app. Any config edit invalidates immediately.
    """
    from ..config import PROJECT_ROOT

    newest = 0.0
    for name in ("holdings.yaml", "config.yaml", "bonds.yaml"):
        f = PROJECT_ROOT / "config" / name
        try:
            newest = max(newest, f.stat().st_mtime)
        except OSError:
            continue
    return newest


def _cached(key: str, fn, ttl: int = CACHE_TTL):
    now = time.time()
    stamp = _config_stamp()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl and hit[2] == stamp:
        return hit[1]
    val = fn()
    _cache[key] = (now, val, stamp)
    return val


def _ui_version() -> str:
    """Cheap identity for index.html: modification time and size."""
    try:
        st = (STATIC / "index.html").stat()
        return f"{int(st.st_mtime)}-{st.st_size}"
    except OSError:
        return ""


def _wishes_soonest_first(nw) -> list[dict]:
    """Wishes with their buy-date simulation, nearest first.

    Ordered by when you could have the thing, not by what it costs: the card is
    there to answer "what is next", and price ordering answers a question nobody
    asked.
    """
    rows = []
    for w in nw.wishes():
        s = nw.purchase_plan(w.amount_idr)
        rows.append({
            "name": w.name, "amount": w.amount_idr, "note": w.note,
            "months_of_saving": (
                w.amount_idr / nw.cashflow.surplus
                if nw.cashflow.surplus > 0 else None
            ),
            "safe_now": s["safe_now"],
            "safe_when": s["when"].isoformat() if s["when"] else None,
            "months_after": s["months_after"],
            "months_if_bought_now": s.get("now_months"),
        })

    def _soonest(r):
        if r["safe_now"]:
            return (0, "", -r["amount"])
        if r["safe_when"]:
            return (1, r["safe_when"], -r["amount"])   # ISO dates sort correctly
        return (2, "", -r["amount"])

    return sorted(rows, key=_soonest)


def create_app(
    *, token: str, readonly: bool = False, access: AccessAuth | None = None,
) -> FastAPI:
    """`readonly` drops every control endpoint.

    Use it whenever the dashboard is reachable from outside your own network.
    Reading your net worth from a stolen URL is embarrassing; flattening your
    positions from one is expensive, and the two risks do not deserve the same
    protection.
    """
    app = FastAPI(title="Lumbung", docs_url=None, redoc_url=None)
    cfg = load_config()

    # Brute-force damping. A 32-char token is not guessable in practice, but an
    # exposed endpoint attracts credential-stuffing noise, and an unbounded
    # guess rate turns a weak token into no token at all.
    fails: dict[str, list[float]] = {}
    LOCKOUT_AFTER, LOCKOUT_WINDOW = 8, 300.0

    def auth(request: Request) -> None:
        """Bearer token, constant-time compared.

        The PWA sends it as a header; a browser opening the page directly gets it
        from the ?t= query parameter on first load and stores it locally.
        """
        client = request.client.host if request.client else "?"
        now = time.time()
        recent = [t for t in fails.get(client, []) if now - t < LOCKOUT_WINDOW]
        fails[client] = recent
        if len(recent) >= LOCKOUT_AFTER:
            raise HTTPException(status_code=429, detail="too many failed attempts")

        # Cloudflare Access first. When it has already authenticated the visitor
        # there is nothing for a shared token to add: the assertion names the
        # person, cannot be copied out of a bookmark, and expires on its own.
        if access is not None:
            email = access.email_for(
                request.headers.get("cf-access-jwt-assertion", "")
            )
            if email:
                request.state.email = email
                fails.pop(client, None)
                return

        supplied = ""
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            supplied = header[7:]
        elif "t" in request.query_params:
            supplied = request.query_params["t"]
        if not secrets.compare_digest(supplied, token):
            fails.setdefault(client, []).append(now)
            log.warning("auth failure from %s (%d in window)", client, len(fails[client]))
            raise HTTPException(status_code=401, detail="bad or missing token")
        fails.pop(client, None)

    def writable(request: Request) -> None:
        """Gate for anything that moves money or stops the engine.

        Two gates rather than one, because reading your net worth from a stolen
        URL is embarrassing and flattening your positions from one is expensive.
        `readonly` is what the public deployment runs with, so the levers are
        not merely hidden in the UI -- they are not reachable.
        """
        auth(request)
        if readonly:
            raise HTTPException(
                status_code=403,
                detail="read-only mode: controls are disabled on this deployment",
            )

    # ---------------------------------------------------------------- data
    def _holdings_snapshot() -> dict:
        from ..holdings import SUBSCRIPTION_IDR, analyse, load_holdings
        from ..networth import live_crypto_value, load_networth

        holds, cash, alerts = load_holdings()
        reports = analyse(holds, cfg.stocks, alerts) if holds else []
        stock_value = sum(r.market_value for r in reports)
        nw = load_networth(stock_value=stock_value, crypto_value=live_crypto_value())

        from ..spending import category_plan
        from ..spending import connect as spend_connect
        from ..spending import spending_profile

        prof = spending_profile(spend_connect(cfg.db_path), nw.cashflow.spending_monthly)
        for r in reports:
            nw.positions.append((r.holding.ticker.replace(".JK", ""), r.market_value))
        total = nw.total
        pv = total

        return {
            "net_worth": total,
            "cash": cash,
            # Total passive income, not just equities: savings interest and bond
            # coupons are the larger half now and were being left out.
            # Named for what it is. It was called `dividend_monthly`, the UI
            # printed "Dividends", and two thirds of it was bond coupons and
            # savings interest. A label that misnames its own contents is a
            # slower kind of wrong number.
            "passive_monthly": (
                sum(r.monthly_income for r in reports) + nw.savings_income_monthly
            ),
            "stock_dividends": sum(r.monthly_income for r in reports),
            "interest_monthly": nw.savings_income_monthly,
            "subscription_pct": (
                (sum(r.monthly_income for r in reports) + nw.savings_income_monthly)
                / SUBSCRIPTION_IDR * 100
            ),
            "buckets": [
                {
                    "name": n,
                    "value": b.value,
                    "weight": b.weight(total) * 100,
                    "target": b.target_pct * 100,
                    "drift": b.drift(total),
                }
                for n, b in nw.buckets.items()
                if b.value > 0 or b.target_pct > 0
            ],
            # Bucket totals alone do not tell you WHAT you own. "savings 7%"
            # is not an answer to "what is savings?" -- naming Superbank is.
            "assets": [
                {
                    "name": a.name, "kind": a.kind, "value": a.value_idr,
                    "rate": a.rate * 100, "monthly": a.annual_income / 12,
                    "note": a.note,
                }
                for a in nw.other_assets
            ],
            "holdings": [
                {
                    "ticker": r.holding.ticker.replace(".JK", ""),
                    "lots": r.holding.lots,
                    "price": r.price,
                    "avg": r.holding.avg_price,
                    "value": r.market_value,
                    "pnl": r.unrealised,
                    "pnl_pct": r.unrealised_pct,
                    "yield_pct": r.yield_on_market_pct,
                    "monthly": r.monthly_income,
                    "weight": r.market_value / pv * 100 if pv else 0,
                    "signal": r.signal,
                    "verdict": _verdict(
                        sell_signals(r, portfolio_value=pv, net_worth=nw.total)
                    ),
                    "alerts": r.alerts,
                }
                for r in reports
            ],
            # Spending is shown as MEASURED once enough is logged, and clearly
            # labelled as assumed until then. Showing a budgeted figure as if it
            # were fact is how a plan quietly drifts from reality.
            "cashflow": {
                "income": nw.cashflow.income_monthly,
                "spending": prof.basis,
                "spending_budgeted": nw.cashflow.spending_monthly,
                "spending_basis": prof.basis_label,
                "spending_tracked": prof.tracked,
                "spending_low": prof.lowest,
                "spending_high": prof.highest,
                "spending_swing_pct": prof.swing_pct,
                "spending_verdict": prof.verdict,
                "current_month": prof.current[1] if prof.current else 0.0,
                # "Saving (real)" comes from the CONFIGURED baseline, not the
                # raw trailing average. The average is a total: one car repair
                # or one month of paying a freelance team lands in it whole and
                # can swamp it for months. Driving the headline off it reported
                # "you keep Rp 0 each month" to someone whose bank balance had
                # just grown by Rp 26jt in two months.
                "surplus": max(0.0, nw.cashflow.surplus),
                "surplus_assumed": nw.cashflow.surplus,
                # Income minus the raw average, kept so the UI can still show
                # what recent months actually cost.
                "surplus_measured": prof.surplus(nw.cashflow.income_monthly),
                "spending_limit": nw.cashflow.spending_limit,
                "budget": nw.cashflow.budget,
                "savings_rate": (
                    max(0.0, nw.cashflow.surplus) / nw.cashflow.income_monthly * 100
                    if nw.cashflow.income_monthly else 0.0
                ),
            },
            # What each category costs against what it is meant to cost. Built
            # on the same tracked months and the same exclusions as the headline
            # figure, so the rows always sum to it.
            "cut_plan": category_plan(
                spend_connect(cfg.db_path),
                nw.goals.spending_targets,
                budgeted=nw.cashflow.spending_monthly,
                exclude=tuple(nw.goals.spending_excludes),
            ),
            "emergency": {
                "target": nw.emergency_target,
                "liquid": nw.liquid_now,
                "months_cash": nw.months_covered_cash,
                "months_liquid": nw.months_covered_liquid,
                # Net of anything already promised. Equal to the gross figures
                # when nothing is committed, so the card can simply use these.
                "committed": nw.committed_total(),
                "free": nw.free_liquid(),
                "months_free": nw.months_covered_free(),
                "shortfall": nw.emergency_shortfall,
            },
            # Obligations only. A wish listed as "already promised" is the
            # confusion the split exists to prevent.
            "commitments": [
                {
                    "name": c.name, "amount": c.amount_idr,
                    "due": c.due.isoformat() if c.due else None,
                    "days": c.days_away(), "note": c.note,
                }
                for c in sorted(
                    (x for x in nw.commitments if not x.is_wish),
                    key=lambda x: (x.due is None, x.due),
                )
            ],
            "wishes": _wishes_soonest_first(nw),
            "emergency_months_target": nw.emergency_months_target,
            # Reported separately from net_worth on purpose: these are owned but
            # never part of the pool the allocation rules divide by.
            "possessions": [
                {"name": p.name, "value": p.value_idr, "note": p.note,
                 "depreciating": p.depreciating}
                for p in sorted(nw.possessions, key=lambda x: -x.value_idr)
            ],
            "possessions_total": nw.possessions_total,
            "total_with_possessions": nw.total_with_possessions,
            "spendable_now": (
                nw.buckets["cash"].value + nw.buckets["savings"].value
                - nw.committed_net_of_income()
            ),
            "surplus_plan": [
                {"bucket": n, "amount": a} for n, a in sorted(
                    nw.allocate_surplus(), key=lambda x: -x[1]
                )
            ],
        }

    # ----------------------------------------------------------- identity
    # Takes the bearer token only, and sits in front of everything else: it is
    # what the login screen asks before it knows whether it needs to ask for
    # anything.
    @app.get("/api/whoami")
    def whoami(request: Request, _: None = Depends(auth)) -> dict:
        """Who Access says you are, if anyone. Drives the login screen."""
        return {
            "access": access is not None,
            "email": getattr(request.state, "email", ""),
        }

    @app.get("/api/config")
    def client_config(_: None = Depends(auth)) -> dict:
        """What the UI needs to know about this deployment."""
        return {"readonly": readonly}

    @app.get("/api/summary")
    def summary(_: None = Depends(auth)) -> dict:
        snap = _cached("holdings", _holdings_snapshot)
        # Deliberately outside the cache: this is the one field whose whole job
        # is to be current. Cached for five minutes it would report the page as
        # up to date for five minutes after it stopped being so.
        snap = {**snap, "ui_version": _ui_version()}
        j = Journal(cfg.db_path)
        curve = j.equity_curve(1)
        eq = float(curve[-1]["equity"]) if curve else 0.0
        hb = j.seconds_since_heartbeat()

        # Target and blended yield both come from the configuration rather than
        # from constants. The 3jt was hardcoded here; the 7% was worse, because
        # it assumed every rupiah earns. A target allocation holding 10% gold,
        # 7% cash and 3% crypto has a fifth of itself paying nothing, so the
        # honest blended figure is nearer 4.4% -- and that difference moves the
        # capital required for Rp 3jt/month from Rp 514jt to about Rp 818jt.
        from ..goal import blended_yield
        from ..networth import load_networth as _load_nw

        _nw = _load_nw(stock_value=snap.get("stocks_value", 0.0))
        target = _nw.goals.monthly_income_target
        weights = {n: b.target_pct for n, b in _nw.buckets.items()}
        rate = blended_yield(weights) or DEFAULT_BLENDED
        plan = plan_income_goal(
            monthly_target=target,
            current_capital=snap["net_worth"],
            monthly_contribution=snap["cashflow"]["surplus"],
            blended_return=rate,
        )
        return {
            **snap,
            # Surfaced deliberately: a years-to-target figure is only as good as
            # the rate behind it, and that rate was invisible.
            "goal_yield_pct": rate * 100,
            "bot": {
                "mode": j.get_state("mode", "paper"),
                "equity": eq,
                "halted": bool(j.get_state("halted", False)),
                "halt_reason": j.get_state("halt_reason", ""),
                "positions": len(j.positions()),
                "heartbeat_sec": None if hb == float("inf") else round(hb),
                "stale": hb > cfg.execution.heartbeat_stale_sec,
                "pnl_30d": j.realized_pnl_since(int(time.time()) - 30 * 86400),
            },
            "goal": {
                "target_monthly": target,
                "capital_required": plan.capital_required,
                "years": plan.years_to_target(),
                "income_now": plan.income_now,
                "progress_pct": min(
                    100, snap["net_worth"] / plan.capital_required * 100
                ) if plan.capital_required else 0,
            },
            "server_time": int(time.time()),
        }

    @app.get("/api/equity")
    def equity(_: None = Depends(auth), limit: int = 500) -> dict:
        j = Journal(cfg.db_path)
        rows = j.equity_curve(limit)
        return {"points": [{"t": r["ts"], "v": r["equity"]} for r in rows]}

    @app.get("/api/positions")
    def positions(_: None = Depends(auth)) -> dict:
        from ..exchanges.indodax_public import IndodaxPublicClient

        j = Journal(cfg.db_path)
        pub = IndodaxPublicClient()
        out = []
        for pair, p in j.positions().items():
            try:
                px = _cached(f"px:{pair}", lambda pr=pair: pub.last_price(pr), ttl=30)
            except Exception:  # noqa: BLE001
                px = p.entry_price
            out.append(
                {
                    "pair": pair, "qty": p.qty, "entry": p.entry_price, "price": px,
                    "stop": p.stop, "value": p.qty * px,
                    "pnl": (px - p.entry_price) * p.qty,
                    "pnl_pct": (px / p.entry_price - 1) * 100 if p.entry_price else 0,
                }
            )
        return {"positions": out}

    @app.get("/api/expenses")
    def expenses(_: None = Depends(auth), days: int = 90) -> dict:
        from ..spending import by_category, connect, monthly_totals, recent

        conn = connect(cfg.db_path)
        rows = recent(conn, days)
        total = sum(r["amount"] for r in rows)
        return {
            "days": days,
            "total": total,
            "count": len(rows),
            "categories": [
                {"name": c, "total": amt, "n": n, "share": (amt / total * 100) if total else 0}
                for c, amt, n in by_category(conn, days)
            ],
            "months": [{"month": m, "total": t} for m, t in monthly_totals(conn, months=6)],
            "recent": [
                {
                    "ts": r["ts"], "item": r["item"], "amount": r["amount"],
                    "category": r["category"], "method": r["method"],
                }
                for r in rows[:25]
            ],
        }

    @app.get("/api/events")
    def events(_: None = Depends(auth), limit: int = 40) -> dict:
        j = Journal(cfg.db_path)
        return {
            "events": [
                {"ts": e["ts"], "kind": e["kind"], "msg": e["msg"], "level": e["level"]}
                for e in j.recent_events(limit)
            ]
        }

    # ------------------------------------------------------------- control
    @app.post("/api/pause")
    def pause(_: None = Depends(writable)) -> dict:
        j = Journal(cfg.db_path)
        j.set_state("halted", True)
        j.set_state("halt_reason", "paused from dashboard")
        j.event("pause", "paused from dashboard")
        return {"ok": True, "halted": True}

    @app.post("/api/resume")
    def resume(_: None = Depends(writable)) -> dict:
        j = Journal(cfg.db_path)
        curve = j.equity_curve(1)
        eq = float(curve[-1]["equity"]) if curve else 0.0
        j.set_state("halted", False)
        j.set_state("halt_reason", "")
        # Rebase the peak, or the drawdown gate re-trips immediately.
        if eq:
            j.set_state("peak_equity", eq)
            j.set_state("day_start_equity", eq)
        j.event("resume", "resumed from dashboard")
        return {"ok": True, "halted": False}

    @app.post("/api/flat")
    def flat(_: None = Depends(writable)) -> dict:
        """Ask the engine to flatten. It acts on its next loop.

        Deliberately a request, not a direct sell: only the engine process may
        talk to the exchange, or two writers race on the same positions.
        """
        j = Journal(cfg.db_path)
        j.set_state("flatten_request", int(time.time()))
        j.event("flatten_request", "requested from dashboard")
        return {"ok": True, "queued": True}

    @app.post("/api/kill")
    def kill(_: None = Depends(writable)) -> dict:
        cfg.halt_path.write_text("killed from dashboard\n", encoding="utf-8")
        j = Journal(cfg.db_path)
        j.set_state("flatten_request", int(time.time()))
        j.event("kill", "kill switch from dashboard", level="error")
        return {"ok": True, "halt_file": str(cfg.halt_path)}

    @app.post("/api/refresh")
    def refresh(_: None = Depends(writable)) -> dict:
        _cache.clear()
        return {"ok": True}

    # ----------------------------------------------------------- checklist
    @app.get("/api/actions")
    def actions_list(_: None = Depends(auth)) -> dict:
        from ..actions import build_actions
        from ..bonds import load_bonds
        from ..holdings import analyse, load_holdings
        from ..networth import live_crypto_value, load_networth

        holds, _cash, alerts = load_holdings()
        reports = analyse(holds, cfg.stocks, alerts) if holds else []
        stock_value = sum(r.market_value for r in reports)
        nw = load_networth(stock_value=stock_value, crypto_value=live_crypto_value())

        # Net worth has to be known before the signals are built, so that
        # concentration is measured against everything owned rather than against
        # the stock sleeve alone.
        reviews = [
            sell_signals(
                r, portfolio_value=stock_value or 1.0, net_worth=nw.total,
                max_position_pct=cfg.stocks.max_position_pct,
            )
            for r in reports
        ]
        offerings, _alts = load_bonds()

        j = Journal(cfg.db_path)
        bot = {
            "halted": bool(j.get_state("halted", False)),
            "halt_reason": j.get_state("halt_reason", ""),
        }
        acts = build_actions(nw, offerings=offerings, reviews=reviews, bot=bot,
                             weight_ceiling=cfg.stocks.max_position_pct)

        return {
            "actions": [a.as_dict() for a in acts],
            "open": len(acts),
        }

    # ---------------------------------------------------------------- chat
    @app.get("/api/chat/history")
    def chat_history(_: None = Depends(auth)) -> dict:
        """Questions asked and answers written back, oldest first.

        A chat reset (POST /api/chat/reset) hides everything before it: the
        jsonl files stay intact -- they are the record -- but the view and
        the model's memory both start from the marker.
        """
        import json

        from ..chat import read_answers

        d = cfg.data_dir
        try:
            state = json.loads(
                (d / "chat_state.json").read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            state = {}
        since = int(state.get("chat_reset_ts") or 0)
        asked = read_answers(d / "ask_queue.jsonl", limit=40)
        answered = read_answers(d / "answers.jsonl", limit=40)
        msgs = (
            [{"role": "you", "text": a.get("text", ""), "ts": a.get("ts", 0)}
             for a in asked if a.get("text")]
            + [{"role": "lumbung", "text": a.get("text", ""), "ts": a.get("ts", 0)}
               for a in answered if a.get("text")]
        )
        msgs = [m for m in msgs if m["ts"] >= since]
        msgs.sort(key=lambda m: m["ts"])
        return {"messages": msgs[-60:]}

    @app.post("/api/chat/reset")
    def chat_reset(_: None = Depends(auth)) -> dict:
        """Drop a reset marker for the chat. The queue and answers files are
        never touched -- they are the record of what was asked -- but the
        view starts from the marker and the answering worker opens a fresh
        model session. Not an engine control: it touches chat state only,
        so it is registered on read-only deployments too."""
        import json

        (cfg.data_dir / "chat_state.json").write_text(
            json.dumps({"chat_reset_ts": int(time.time())}), encoding="utf-8")
        return {"ok": True}

    @app.get("/api/chat/stream")
    async def chat_stream(_: None = Depends(auth)) -> StreamingResponse:
        """Server-sent events: one frame per answer, as it is written.

        Starts at the end of the file on purpose. History arrives from
        /api/chat/history, and replaying it here would double every message on
        screen.
        """
        import asyncio

        from ..chat import follow

        path = cfg.data_dir / "answers.jsonl"

        async def frames():
            try:
                offset = path.stat().st_size
            except OSError:
                offset = 0
            quiet = 0.0
            # Bounded so a forgotten tab cannot hold a worker forever; the
            # client reconnects, and reconnecting is cheap.
            deadline = time.monotonic() + 30 * 60
            while time.monotonic() < deadline:
                lines, offset = follow(path, offset)
                if lines:
                    for line in lines:
                        yield f"data: {line}\n\n"
                    quiet = 0.0
                else:
                    await asyncio.sleep(1.0)
                    quiet += 1.0
                    # Cloudflare drops an idle tunnel connection; a comment
                    # frame is ignored by the client and keeps it open.
                    if quiet >= 20:
                        quiet = 0.0
                        yield ": keepalive\n\n"

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",   # do not let a proxy buffer the stream
                "Connection": "keep-alive",
            },
        )

    @app.post("/api/chat")
    def chat_send(payload: dict, _: None = Depends(auth)) -> dict:
        """Every command Lumbung answers, in one place.

        Free-form text is queued for a Claude Code session rather than refused:
        a chat that only accepts commands is a command line with a worse
        keyboard.

        `writable` is passed through rather than assumed. On the public
        deployment the four engine controls are not registered at all, so
        `/kill` there is an unknown command that becomes a question -- not a
        lever hidden behind a disabled button.
        """
        from ..chat import dispatch

        text = str(payload.get("text", ""))
        return dispatch(
            cfg, text,
            ask_queue=cfg.data_dir / "ask_queue.jsonl",
            writable=not readonly,
        )

    @app.post("/api/chat/upload")
    async def chat_upload(request: Request, _: None = Depends(auth)) -> dict:
        """Take a receipt photo, screenshot or PDF and queue it to be read.

        The file is stored and a reference is queued; nothing here tries to
        parse it. Reading a receipt reliably means looking at it, and the thing
        that can look at it is the Claude session watching the queue -- which
        also means no dependency on a GPU box being up.
        """
        from ..chat import queue_question

        form = await request.form()
        uploads = [u for u in form.getlist("file") if getattr(u, "filename", "")]
        if not uploads:
            raise HTTPException(status_code=400, detail="no file")
        # A statement can run to several pages photographed separately, so one
        # file per message was the wrong unit. Bounded because each one is read
        # by a person-scale process at the other end, not a parser.
        MAX_FILES = 8
        if len(uploads) > MAX_FILES:
            raise HTTPException(
                status_code=400, detail=f"at most {MAX_FILES} files at once"
            )

        MAX = 12 * 1024 * 1024
        MAX_TOTAL = 40 * 1024 * 1024
        ALLOWED = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".pdf"}

        # Validate EVERY file before writing ANY of them. Writing as we go would
        # leave half an upload on disk and a queue entry naming files that are
        # not all there.
        staged: list[tuple[str, bytes]] = []
        total = 0
        for upload in uploads:
            raw = await upload.read()
            name = Path(str(upload.filename)).name
            if len(raw) > MAX:
                raise HTTPException(
                    status_code=400, detail=f"{name} is larger than 12 MB"
                )
            total += len(raw)
            if total > MAX_TOTAL:
                raise HTTPException(status_code=400, detail="40 MB total at most")
            if Path(name).suffix.lower() not in ALLOWED:
                raise HTTPException(
                    status_code=400, detail=f"{name}: images and PDFs only"
                )
            staged.append((name, raw))

        dest_dir = cfg.data_dir / "uploads"
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time())
        written: list[Path] = []
        for i, (name, raw) in enumerate(staged):
            ext = Path(name).suffix.lower()
            # Timestamped so two photos of the same receipt cannot overwrite,
            # and sanitised so a crafted filename cannot escape the directory.
            # The index matters: several files arrive within the same second.
            safe = "".join(c for c in name if c.isalnum() or c in "._- ")[:60]
            dest = dest_dir / f"{stamp}-{i}-{safe or 'upload' + ext}"
            dest.write_bytes(raw)
            written.append(dest)

        # The caption leads. Everything under it is a path, so what the sender
        # meant is the first thing read rather than the last -- the old entry
        # buried the note between a path and a canned instruction, and every
        # upload needed a follow-up message explaining what it was.
        note = str(form.get("note", "") or "").strip()
        header = note or (
            "Read these and tell me what they are: expense or income, the "
            "amount, the merchant, and where they should be allocated."
        )
        body = "\n".join(str(d) for d in written)
        queue_question(
            cfg.data_dir / "ask_queue.jsonl",
            f"[FILES {len(written)}] {header}\n{body}",
            source="upload",
        )
        n = len(written)
        return {"ok": True, "stored": [d.name for d in written], "count": n,
                "reply": f"Got {n} file{'s' if n != 1 else ''}. Reading now."}

    # ------------------------------------------------------------ settings
    @app.get("/api/settings")
    def settings_read(_: None = Depends(auth)) -> dict:
        from .settings import GROUPS, env_status, read_settings

        return {
            "fields": read_settings(),
            "secrets": env_status(),
            # Sent rather than hardcoded in the page, so adding a field is one
            # edit in one file instead of two that can drift apart.
            "groups": [
                {"title": t, "hint": h, "keys": k} for t, h, k in GROUPS
            ],
        }

    @app.post("/api/settings")
    def settings_write(payload: dict, _: None = Depends(auth)) -> dict:
        from .settings import SettingsError, write_settings

        try:
            result = write_settings(payload.get("fields", {}) or {})
        except SettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        _cache.clear()
        return {"ok": True, **result}

    @app.post("/api/settings/secrets")
    def settings_secrets(payload: dict, _: None = Depends(auth)) -> dict:
        """Onboarding: set API keys. Values go in, nothing ever comes back out."""
        from .settings import SettingsError, write_env

        try:
            result = write_env(payload.get("secrets", {}) or {})
        except SettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"ok": True, **result}

    # -------------------------------------------------------------- static
    @app.get("/manifest.json")
    def manifest() -> FileResponse:
        return FileResponse(STATIC / "manifest.json", media_type="application/manifest+json")

    @app.get("/sw.js")
    def service_worker() -> FileResponse:
        # Must be served from the root scope or it cannot control the whole app.
        return FileResponse(STATIC / "sw.js", media_type="application/javascript")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.exception_handler(HTTPException)
    def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app


def _verdict(rev) -> str:
    if rev.worst == "act":
        return "ACTION" if not rev.business_is_intact else "TRIM?"
    return "WATCH" if rev.worst == "watch" else "HOLD"


def resolve_token() -> str:
    """Dashboard token from .env, or a generated one printed at startup."""
    sec = get_secrets()
    tok = getattr(sec, "dashboard_token", "")
    tok = tok.get_secret_value() if hasattr(tok, "get_secret_value") else str(tok)
    return tok or secrets.token_urlsafe(24)
