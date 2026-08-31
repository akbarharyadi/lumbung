"""`lumbung doctor` -- pre-flight check.

Answers one question: what is still missing before this can run? Each check
reports PASS / WARN / FAIL plus the exact command or edit that fixes it, so
nothing has to be looked up elsewhere.

Nothing here mutates anything or places an order.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass

from .config import PROJECT_ROOT, get_secrets, load_config

OK, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""

    @property
    def blocking(self) -> bool:
        """FAILs block live trading; WARNs are things you can live without."""
        return self.status == FAIL


def _fmt_rp(v: float) -> str:
    return f"Rp {v:,.0f}"


def run_checks(*, want_live: bool = False) -> list[Check]:
    checks: list[Check] = []
    cfg = load_config()
    sec = get_secrets()

    # --- 1. environment file -------------------------------------------
    env = PROJECT_ROOT / ".env"
    if not env.exists():
        # Paper mode needs no credentials at all -- it prices against the public
        # API and fakes the balance. Only live trading truly requires .env.
        checks.append(
            Check(
                ".env file", FAIL if want_live else WARN,
                "not created yet (paper mode works without it)",
                "copy .env.example to .env, then fill in your keys:\n"
                "     copy .env.example .env",
            )
        )
    else:
        checks.append(Check(".env file", OK, str(env)))

    # --- 2. Indodax credentials ----------------------------------------
    if not sec.has_indodax:
        checks.append(
            Check(
                "Indodax API key", FAIL if want_live else WARN,
                "INDODAX_KEY / INDODAX_SECRET are empty",
                "create at https://indodax.com/trade_api with view + trade ONLY\n"
                "     (never enable withdraw), then paste both into .env",
            )
        )
    else:
        checks.append(Check("Indodax API key", OK, "present in .env"))

    # --- 3. Alerts ------------------------------------------------------
    # Alerts are appended to the chat transcript the dashboard reads, so the
    # only way they can go missing is the directory not being writable. Check
    # that rather than assume it: an alert that silently fails to arrive is
    # indistinguishable from nothing having gone wrong.
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.data_dir / ".doctor-write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        checks.append(
            Check(
                "Alerts", WARN,
                f"cannot write to {cfg.data_dir} ({exc}) — alerts will only print to this console",
                f"check permissions on {cfg.data_dir}",
            )
        )
    else:
        checks.append(Check("Alerts", OK, "delivered to the in-app chat"))

    # --- 3b. Indodax IP whitelist ---------------------------------------
    if sec.has_indodax:
        try:
            from .netcheck import check as ip_check

            st = ip_check(cfg.db_path.parent / "last_ip.json", sec.indodax_whitelist_ip)
            if st.unknown:
                checks.append(Check("Indodax IP whitelist", WARN, "could not determine your IP"))
            elif st.not_configured:
                checks.append(
                    Check(
                        "Indodax IP whitelist", WARN,
                        f"not recorded; your IP is {st.current}",
                        f"set INDODAX_WHITELIST_IP={st.current} in .env",
                    )
                )
            elif st.ok:
                checks.append(Check("Indodax IP whitelist", OK, f"{st.current} matches"))
            else:
                checks.append(
                    Check(
                        "Indodax IP whitelist", FAIL,
                        f"IP changed: key allows {st.whitelisted}, you are now {st.current}",
                        "update IP Permission at https://indodax.com/trade_api, or move "
                        "to a VPS with a static IP",
                    )
                )
        except Exception as exc:  # noqa: BLE001
            checks.append(Check("Indodax IP whitelist", WARN, str(exc)))

    # --- 3c. which mode would a bare `lumbung run` use? -------------------
    from .config import env_mode

    m = env_mode()
    if m == "live":
        checks.append(
            Check(
                "default run mode", WARN,
                "LIVE -- a bare `lumbung run`, and the logon autostart, use REAL MONEY",
                "set TA_MODE=paper in .env to go back to simulation",
            )
        )
    else:
        checks.append(Check("default run mode", OK, f"{m} (autostart uses this too)"))

    # --- 4. candle data --------------------------------------------------
    try:
        from .data import candles as cm

        conn = cm.connect(cfg.db_path)
        missing, oldest = [], None
        for pair in cfg.universe.pairs:
            lo, hi = cm.coverage(conn, pair, cfg.universe.timeframe)
            if hi is None:
                missing.append(pair)
            else:
                oldest = hi if oldest is None else min(oldest, hi)
        if missing:
            # If another .db in the same folder holds the data, the configured
            # path is pointing at a fresh empty file -- which is exactly what a
            # careless rename does. Say so, rather than sending you off to
            # re-download four years of candles.
            others = [
                f for f in cfg.db_path.parent.glob("*.db")
                if f != cfg.db_path and f.stat().st_size > 1_000_000
            ]
            if others:
                checks.append(
                    Check(
                        "candle data", FAIL,
                        f"{cfg.db_path.name} is empty, but {others[0].name} "
                        f"holds {others[0].stat().st_size // 1_000_000} MB",
                        f"you are pointed at the wrong database. Either set paths.db "
                        f"to {others[0].name} in config/config.yaml, or rename that "
                        f"file to {cfg.db_path.name}",
                    )
                )
            else:
                checks.append(
                    Check(
                        "candle data", FAIL, f"{len(missing)} pairs have no candles",
                        "lumbung sync",
                    )
                )
        else:
            age_h = (time.time() - (oldest or 0)) / 3600
            checks.append(
                Check(
                    "candle data", OK if age_h < 24 else WARN,
                    f"{len(cfg.universe.pairs)} pairs, newest bar {age_h:.0f}h old",
                    "lumbung sync" if age_h >= 24 else "",
                )
            )
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("candle data", FAIL, str(exc), "lumbung sync"))

    # --- 5. backtest gate ------------------------------------------------
    state_note = "run `lumbung backtest` and confirm it prints GATE: PASS"
    checks.append(Check("backtest gate", WARN, "not verified in this session", state_note))

    # --- 6. holdings config ---------------------------------------------
    try:
        from .holdings import load_holdings

        holds, cash, _ = load_holdings()
        if not holds and cash <= 0:
            checks.append(
                Check("holdings.yaml", WARN, "no holdings or cash recorded",
                      "edit config/holdings.yaml")
            )
        else:
            checks.append(
                Check(
                    "holdings.yaml", OK,
                    f"{len(holds)} stock position(s), {_fmt_rp(cash)} cash",
                )
            )
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("holdings.yaml", FAIL, str(exc), "check config/holdings.yaml"))

    # --- 7. sleeve sanity -------------------------------------------------
    sleeve = cfg.capital.sleeve_idr
    try:
        from .holdings import load_holdings

        _, cash, _ = load_holdings()
        if sleeve > cash > 0:
            checks.append(
                Check(
                    "crypto sleeve", WARN,
                    f"sleeve {_fmt_rp(sleeve)} exceeds recorded cash {_fmt_rp(cash)}",
                    "lower capital.sleeve_idr in config/config.yaml",
                )
            )
        else:
            checks.append(Check("crypto sleeve", OK, _fmt_rp(sleeve)))
    except Exception:  # noqa: BLE001
        checks.append(Check("crypto sleeve", OK, _fmt_rp(sleeve)))

    # --- 7b. sleeve vs the REAL Indodax balance ---------------------------
    # The sleeve drives position sizing, so a sleeve larger than the money on
    # the exchange means every trade is sized against capital that is not there.
    if sec.has_indodax:
        try:
            from .exchanges.indodax_v2 import IndodaxV2Client

            c = IndodaxV2Client(
                sec.indodax_key.get_secret_value(), sec.indodax_secret.get_secret_value()
            )
            avail, held = c.balances()
            idr = avail.get("idr", 0.0) + held.get("idr", 0.0)
            coins = sum(v for k, v in avail.items() if k != "idr" and v > 0)
            if idr <= 0 and coins <= 0:
                checks.append(
                    Check(
                        "Indodax balance", FAIL if want_live else WARN,
                        "Rp 0 on the exchange - nothing to trade with",
                        "deposit IDR to Indodax, then set capital.sleeve_idr to match",
                    )
                )
            elif idr + 1 < cfg.capital.sleeve_idr:
                checks.append(
                    Check(
                        "Indodax balance", FAIL if want_live else WARN,
                        f"Rp {idr:,.0f} on the exchange but sleeve is "
                        f"Rp {cfg.capital.sleeve_idr:,.0f}",
                        "lower capital.sleeve_idr to the deposited amount, or deposit more",
                    )
                )
            else:
                checks.append(Check("Indodax balance", OK, f"Rp {idr:,.0f} available"))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check("Indodax balance", WARN, f"could not read: {exc}"))


    # --- 8. kill switch ---------------------------------------------------
    if cfg.halt_path.exists():
        checks.append(
            Check(
                "HALT file", WARN, "present — the engine will refuse to trade",
                "lumbung halt --remove",
            )
        )
    else:
        checks.append(Check("HALT file", OK, "absent (good)"))

    # --- 9. engine heartbeat ---------------------------------------------
    try:
        from .journal import Journal

        j = Journal(cfg.db_path)
        hb = j.seconds_since_heartbeat()
        if hb == float("inf"):
            checks.append(Check("engine heartbeat", WARN, "engine has never run", ""))
        elif hb > cfg.execution.heartbeat_stale_sec:
            checks.append(
                Check(
                    "engine heartbeat", WARN,
                    f"last beat {hb / 60:.0f} min ago — if a position is open, "
                    "its stop-loss is NOT being monitored",
                    "start the engine: lumbung run --mode paper",
                )
            )
        else:
            checks.append(Check("engine heartbeat", OK, f"{hb:.0f}s ago — running"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("engine heartbeat", WARN, str(exc)))

    # --- 10. scheduled daily digest --------------------------------------
    if shutil.which("schtasks"):
        checks.append(
            Check(
                "daily digest schedule", WARN,
                "not verified — run `schtasks /query /tn Lumbung-Daily` to check",
                'schtasks /create /tn "Lumbung-Daily" /sc daily /st 16:15 '
                '/tr "<venv>\\Scripts\\python.exe -m lumbung.cli daily"',
            )
        )
    return checks


def next_steps(checks: list[Check], *, want_live: bool) -> list[str]:
    """Ordered list of what to do next, derived from the failures."""
    steps: list[str] = []
    by_name = {c.name: c for c in checks}

    if by_name.get("candle data", Check("", OK, "")).status != OK:
        steps.append("lumbung sync                 # download candles")
    steps.append("lumbung backtest             # must print GATE: PASS")
    if by_name.get("Alerts", Check("", OK, "")).status != OK:
        steps.append("fix write access to data/         # so alerts reach the app")
    steps.append("lumbung run --mode paper     # leave running ~72h, costs nothing")
    if by_name.get("Indodax API key", Check("", OK, "")).status != OK:
        steps.append("create an Indodax key (view + trade, NO withdraw), paste into .env")
        steps.append("lumbung check-keys           # read-only credential test")
    steps.append("lumbung run --mode live --dry-run   # logs orders, sends none")
    steps.append("lumbung run --mode live      # real money, asks to confirm")
    return steps
