"""Indodax public REST API: order book, ticker, pair metadata, OHLCV candles.

Reference: https://github.com/btcid/indodax-official-api-docs (Public-RestAPI.md)
Documented limit: 180 requests/minute. The throttle below keeps us under it.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://indodax.com"

# tf values accepted by /tradingview/history_v2
TIMEFRAMES = {"1", "15", "30", "60", "240", "1D", "3D", "1W"}
TF_SECONDS = {
    "1": 60,
    "15": 900,
    "30": 1800,
    "60": 3600,
    "240": 14400,
    "1D": 86400,
    "3D": 259200,
    "1W": 604800,
}


@dataclass(frozen=True)
class BookTop:
    """Top of the order book."""

    bid: float
    ask: float
    bid_size: float
    ask_size: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid if self.mid else 0.0


class IndodaxPublicClient:
    def __init__(
        self,
        *,
        timeout: float = 15.0,
        min_interval_sec: float = 0.35,
        force_ipv4: bool = True,
    ) -> None:
        # Match the private client so both halves present the same source IP.
        self._client = httpx.Client(
            timeout=timeout,
            base_url=BASE_URL,
            transport=httpx.HTTPTransport(local_address="0.0.0.0") if force_ipv4 else None,
        )
        self._lock = threading.Lock()
        self._min_interval = min_interval_sec
        self._last_call = 0.0
        self._pairs_cache: dict[str, dict[str, Any]] | None = None

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        with self._lock:
            elapsed = time.monotonic() - self._last_call
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_call = time.monotonic()
            resp = self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    # -- market data -------------------------------------------------------
    def server_time_ms(self) -> int:
        """Exchange clock in MILLIseconds. (Note: the ticker's own `server_time`
        field is in seconds -- this endpoint is not.)"""
        return int(self._get("/api/server_time")["server_time"])

    def ticker(self, pair: str) -> dict[str, Any]:
        return self._get(f"/api/ticker/{self.url_id(pair)}")["ticker"]

    def last_price(self, pair: str) -> float:
        return float(self.ticker(pair)["last"])

    def depth(self, pair: str) -> dict[str, list]:
        return self._get(f"/api/depth/{self.url_id(pair)}")

    def book_top(self, pair: str) -> BookTop:
        """Best bid/ask. This is what post-only entries quote against."""
        d = self.depth(pair)
        buys, sells = d.get("buy", []), d.get("sell", [])
        if not buys or not sells:
            raise RuntimeError(f"empty order book for {pair}")
        return BookTop(
            bid=float(buys[0][0]),
            ask=float(sells[0][0]),
            bid_size=float(buys[0][1]),
            ask_size=float(sells[0][1]),
        )

    # -- pair metadata -----------------------------------------------------
    def pairs(self, *, refresh: bool = False) -> dict[str, dict[str, Any]]:
        """Pair metadata keyed by ticker_id ('btc_idr'), incl. price_round and trade_min."""
        if self._pairs_cache is None or refresh:
            raw = self._get("/api/pairs")
            self._pairs_cache = {p["ticker_id"]: p for p in raw}
        return self._pairs_cache

    def url_id(self, pair: str) -> str:
        """`btc_idr` -> `btcidr`.

        The REST path segment uses the pair's `id`, NOT the `ticker_id` that the
        private TAPI expects. Getting this wrong returns {"error":"invalid_pair"}.
        """
        meta = self.pairs().get(pair)
        return str(meta["id"]) if meta else pair.replace("_", "")

    def price_increment(self, pair: str) -> float:
        """Smallest legal price step, in IDR.

        This is `pricescale` (BTC/ETH = 1000, most alts = 1). It is NOT
        `price_round`, which reads as 8 for every pair and is meaningless here --
        verified against live books: BTC bids are all multiples of 1000.
        """
        meta = self.pairs().get(pair)
        if meta and meta.get("pricescale"):
            return float(meta["pricescale"])
        return 1.0

    def amount_precision(self, pair: str) -> int:
        """Decimal places for the coin amount. The API's `volume_precision` reads 0
        for every pair while live books quote 8 dp, so 8 is the usable value."""
        return 8

    def trade_min_idr(self, pair: str) -> float:
        """Exchange minimum order value in IDR (`trade_min_base_currency`)."""
        meta = self.pairs().get(pair)
        try:
            return float(meta["trade_min_base_currency"]) if meta else 10_000.0
        except (KeyError, TypeError, ValueError):
            return 10_000.0

    def trade_min_coin(self, pair: str) -> float:
        meta = self.pairs().get(pair)
        try:
            return float(meta["trade_min_traded_currency"]) if meta else 0.0
        except (KeyError, TypeError, ValueError):
            return 0.0

    def fees(self, pair: str) -> tuple[float, float]:
        """(maker, taker) as fractions, from live pair metadata (e.g. 0.001, 0.002).

        Ground truth for what the exchange charges; the help-centre article
        disagrees, so `cli verify-costs` reconciles this against real fills.
        """
        meta = self.pairs().get(pair) or {}
        maker = float(meta.get("trade_fee_percent_maker", 0.1)) / 100.0
        taker = float(meta.get("trade_fee_percent_taker", 0.2)) / 100.0
        return maker, taker

    def is_tradable(self, pair: str) -> bool:
        meta = self.pairs().get(pair) or {}
        return not int(meta.get("is_maintenance", 0)) and not int(
            meta.get("is_market_suspended", 0)
        )

    # -- OHLCV -------------------------------------------------------------
    def ohlcv(self, pair: str, tf: str, start: int, end: int) -> list[dict[str, float]]:
        """Raw candles for one window. `start`/`end` are unix seconds.

        The endpoint caps how much it returns per call, so callers that need long
        history should use `ohlcv_range`, which pages.
        """
        if tf not in TIMEFRAMES:
            raise ValueError(f"tf must be one of {sorted(TIMEFRAMES)}, got {tf!r}")
        symbol = self.url_id(pair).upper()  # btc_idr -> BTCIDR
        data = self._get(
            "/tradingview/history_v2",
            {"symbol": symbol, "tf": tf, "from": int(start), "to": int(end)},
        )
        if not isinstance(data, list):
            return []
        out = []
        for row in data:
            try:
                out.append(
                    {
                        "time": int(row["Time"]),
                        "open": float(row["Open"]),
                        "high": float(row["High"]),
                        "low": float(row["Low"]),
                        "close": float(row["Close"]),
                        "volume": float(row.get("Volume", 0.0)),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def ohlcv_range(
        self, pair: str, tf: str, start: int, end: int, *, chunk_bars: int = 1000
    ) -> list[dict[str, float]]:
        """Page forward through `start`..`end`, de-duplicated and sorted by time."""
        step = TF_SECONDS[tf] * chunk_bars
        seen: dict[int, dict[str, float]] = {}
        cursor = int(start)
        end = int(end)
        while cursor < end:
            window_end = min(cursor + step, end)
            batch = self.ohlcv(pair, tf, cursor, window_end)
            for c in batch:
                seen[c["time"]] = c
            if not batch:
                # Gap in history (pair not listed yet); keep walking rather than stall.
                log.debug("%s %s: empty window %d..%d", pair, tf, cursor, window_end)
            cursor = window_end
        return [seen[t] for t in sorted(seen)]

    def close(self) -> None:
        self._client.close()


def round_to_increment(price: float, increment: float, *, mode: str = "nearest") -> float:
    """Round a price to a legal tick. `mode` is 'down' for bids, 'up' for asks.

    Uses math.floor/ceil rather than int(): int() truncates toward zero, so
    `-int(-steps)` floors instead of ceiling. That silently rounded post-only
    SELL orders one tick too low, where they cross the book and are rejected by
    MOC -- or fill as a taker if MOC were ever dropped.
    """
    if increment <= 0:
        return price
    steps = price / increment
    if mode == "down":
        steps = math.floor(steps)
    elif mode == "up":
        steps = math.ceil(steps)
    else:
        steps = round(steps)
    return round(steps * increment, 10)
