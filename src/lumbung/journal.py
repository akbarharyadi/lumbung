"""SQLite journal: the single source of truth for what the bot did and owns.

The process can die at any moment -- a crash, a reboot, a power cut -- so every
state change is written here before or immediately after it happens. On restart
the engine reconciles this journal against the exchange rather than assuming it
remembers anything.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id   TEXT PRIMARY KEY,
    exchange_order_id TEXT,
    pair              TEXT NOT NULL,
    side              TEXT NOT NULL,     -- buy | sell  (cancelOrder needs this)
    order_type        TEXT NOT NULL,     -- limit | market
    price             REAL,
    qty               REAL NOT NULL,
    status            TEXT NOT NULL,     -- pending|open|filled|cancelled|rejected
    purpose           TEXT NOT NULL,     -- entry|stop|trail_exit|trend_exit|flatten
    filled_qty        REAL DEFAULT 0,
    avg_fill_price    REAL DEFAULT 0,
    fee               REAL DEFAULT 0,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    note              TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    pair          TEXT PRIMARY KEY,
    qty           REAL NOT NULL,
    entry_price   REAL NOT NULL,
    stop          REAL NOT NULL,
    initial_risk  REAL NOT NULL,        -- per-coin distance entry->initial stop
    highest_close REAL NOT NULL,
    partial_done  INTEGER DEFAULT 0,
    opened_at     INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pair          TEXT NOT NULL,
    entry_ts      INTEGER NOT NULL,
    entry_price   REAL NOT NULL,
    qty           REAL NOT NULL,
    initial_stop  REAL NOT NULL,
    risk_idr      REAL NOT NULL,
    exit_ts       INTEGER,
    exit_price    REAL,
    realized_pnl  REAL,
    exit_reason   TEXT,
    status        TEXT NOT NULL          -- open | closed
);

CREATE TABLE IF NOT EXISTS equity (
    ts        INTEGER PRIMARY KEY,
    equity    REAL NOT NULL,
    cash      REAL NOT NULL,
    exposure  REAL NOT NULL,
    positions INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS engine_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_signals (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    ticker   TEXT NOT NULL,
    action   TEXT NOT NULL,
    entry    REAL, stop REAL, target REAL, lots INTEGER,
    reason   TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    INTEGER NOT NULL,
    level TEXT NOT NULL,
    kind  TEXT NOT NULL,
    msg   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
"""


@dataclass
class OpenPosition:
    pair: str
    qty: float
    entry_price: float
    stop: float
    initial_risk: float
    highest_close: float
    partial_done: bool
    opened_at: int


class Journal:
    def __init__(self, db_path: str | Path) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- key/value state ---------------------------------------------------
    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM engine_state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO engine_state(key,value) VALUES(?,?)",
            (key, json.dumps(value)),
        )
        self.conn.commit()

    def heartbeat(self) -> None:
        self.set_state("heartbeat", int(time.time()))

    def seconds_since_heartbeat(self) -> float:
        hb = self.get_state("heartbeat")
        return float("inf") if hb is None else time.time() - float(hb)

    # -- orders ------------------------------------------------------------
    def record_order(
        self,
        *,
        client_order_id: str,
        pair: str,
        side: str,
        order_type: str,
        qty: float,
        price: float | None,
        purpose: str,
        status: str = "pending",
        exchange_order_id: str | None = None,
        note: str = "",
    ) -> None:
        now = int(time.time())
        self.conn.execute(
            "INSERT OR REPLACE INTO orders(client_order_id,exchange_order_id,pair,side,"
            "order_type,price,qty,status,purpose,created_at,updated_at,note)"
            " VALUES(?,?,?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM orders WHERE"
            " client_order_id=?),?),?,?)",
            (
                client_order_id, exchange_order_id, pair, side, order_type, price, qty,
                status, purpose, client_order_id, now, now, note,
            ),
        )
        self.conn.commit()

    def update_order(self, client_order_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = int(time.time())
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE orders SET {sets} WHERE client_order_id=?",
            (*fields.values(), client_order_id),
        )
        self.conn.commit()

    def live_orders(self) -> list[sqlite3.Row]:
        """Orders we believe are still working on the exchange."""
        return self.conn.execute(
            "SELECT * FROM orders WHERE status IN ('pending','open') ORDER BY created_at"
        ).fetchall()

    def get_order(self, client_order_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM orders WHERE client_order_id=?", (client_order_id,)
        ).fetchone()

    # -- positions ---------------------------------------------------------
    def upsert_position(self, p: OpenPosition) -> None:
        now = int(time.time())
        self.conn.execute(
            "INSERT OR REPLACE INTO positions(pair,qty,entry_price,stop,initial_risk,"
            "highest_close,partial_done,opened_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                p.pair, p.qty, p.entry_price, p.stop, p.initial_risk, p.highest_close,
                int(p.partial_done), p.opened_at, now,
            ),
        )
        self.conn.commit()

    def delete_position(self, pair: str) -> None:
        self.conn.execute("DELETE FROM positions WHERE pair=?", (pair,))
        self.conn.commit()

    def positions(self) -> dict[str, OpenPosition]:
        rows = self.conn.execute("SELECT * FROM positions").fetchall()
        return {
            r["pair"]: OpenPosition(
                pair=r["pair"], qty=r["qty"], entry_price=r["entry_price"], stop=r["stop"],
                initial_risk=r["initial_risk"], highest_close=r["highest_close"],
                partial_done=bool(r["partial_done"]), opened_at=r["opened_at"],
            )
            for r in rows
        }

    # -- trades ------------------------------------------------------------
    def open_trade(
        self, *, pair: str, entry_price: float, qty: float, stop: float, risk_idr: float
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO trades(pair,entry_ts,entry_price,qty,initial_stop,risk_idr,status)"
            " VALUES(?,?,?,?,?,?,'open')",
            (pair, int(time.time()), entry_price, qty, stop, risk_idr),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def close_trade(self, pair: str, *, exit_price: float, pnl: float, reason: str) -> None:
        self.conn.execute(
            "UPDATE trades SET exit_ts=?, exit_price=?, realized_pnl=?, exit_reason=?,"
            " status='closed' WHERE pair=? AND status='open'",
            (int(time.time()), exit_price, pnl, reason, pair),
        )
        self.conn.commit()

    def closed_trades(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM trades WHERE status='closed' ORDER BY exit_ts DESC LIMIT ?", (limit,)
        ).fetchall()

    def realized_pnl_since(self, ts: int) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(realized_pnl),0) AS p FROM trades"
            " WHERE status='closed' AND exit_ts>=?",
            (ts,),
        ).fetchone()
        return float(row["p"])

    # -- equity & events ---------------------------------------------------
    def record_equity(self, equity: float, cash: float, exposure: float, positions: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO equity(ts,equity,cash,exposure,positions) VALUES(?,?,?,?,?)",
            (int(time.time()), equity, cash, exposure, positions),
        )
        self.conn.commit()

    def equity_curve(self, limit: int = 5000) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM (SELECT * FROM equity ORDER BY ts DESC LIMIT ?) ORDER BY ts", (limit,)
        ).fetchall()

    def event(self, kind: str, msg: str, level: str = "info") -> None:
        self.conn.execute(
            "INSERT INTO events(ts,level,kind,msg) VALUES(?,?,?,?)",
            (int(time.time()), level, kind, msg),
        )
        self.conn.commit()
        getattr(log, level if level in ("info", "warning", "error") else "info")("%s: %s", kind, msg)

    def recent_events(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()

    # -- stock signals -----------------------------------------------------
    def record_stock_signal(self, **kw: Any) -> None:
        self.conn.execute(
            "INSERT INTO stock_signals(ts,ticker,action,entry,stop,target,lots,reason)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                int(time.time()), kw["ticker"], kw["action"], kw.get("entry"), kw.get("stop"),
                kw.get("target"), kw.get("lots"), kw.get("reason", ""),
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
