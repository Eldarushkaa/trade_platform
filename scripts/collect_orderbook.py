#!/usr/bin/env python3
"""
Orderbook (DOM) Collector — standalone script.

Fetches order-book depth snapshots from Binance Futures every 60 seconds
for all configured symbols and stores them in the same SQLite database
used by the main trade platform.

Run standalone:
    python scripts/collect_orderbook.py

Run as systemd service:
    [Unit]
    Description=Orderbook DOM Collector
    After=network.target

    [Service]
    ExecStart=/path/to/venv/bin/python /path/to/scripts/collect_orderbook.py
    Restart=always
    RestartSec=10

    [Install]
    WantedBy=multi-user.target

Environment variables (optional, reads from ../.env):
    DB_PATH           — SQLite file path  (default: trade_platform.db)
    DOM_INTERVAL      — seconds between snapshots (default: 60)
    DOM_DEPTH_LIMIT   — orderbook levels per side  (default: 50)
"""
import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Resolve project root (one level up from scripts/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Try loading .env from project root
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

DB_PATH = os.getenv("DB_PATH", str(PROJECT_ROOT / "trade_platform.db"))
INTERVAL_SECONDS = int(os.getenv("DOM_INTERVAL", "60"))
DEPTH_LIMIT = int(os.getenv("DOM_DEPTH_LIMIT", "50"))

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

# Binance Futures public depth endpoint (no auth required)
DEPTH_URL = "https://fapi.binance.com/fapi/v1/depth"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("orderbook_collector")

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_running = True


def _handle_signal(signum, _frame):
    global _running
    logger.info(f"Received signal {signum}, shutting down...")
    _running = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    timestamp       TEXT    NOT NULL,           -- ISO-8601 UTC
    depth_limit     INTEGER NOT NULL,           -- how many levels per side
    bids_json       TEXT    NOT NULL,           -- JSON [[price, qty], ...]
    asks_json       TEXT    NOT NULL,           -- JSON [[price, qty], ...]
    best_bid        REAL,
    best_ask        REAL,
    spread          REAL,
    bid_depth_usdt  REAL,                       -- sum(price*qty) for all bid levels
    ask_depth_usdt  REAL,                       -- sum(price*qty) for all ask levels
    mid_price       REAL,
    imbalance       REAL                        -- bid_depth / (bid_depth + ask_depth)
);

CREATE INDEX IF NOT EXISTS idx_ob_symbol    ON orderbook_snapshots(symbol);
CREATE INDEX IF NOT EXISTS idx_ob_timestamp ON orderbook_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_ob_sym_ts    ON orderbook_snapshots(symbol, timestamp);
"""

INSERT_SQL = """
INSERT INTO orderbook_snapshots
    (symbol, timestamp, depth_limit, bids_json, asks_json,
     best_bid, best_ask, spread, bid_depth_usdt, ask_depth_usdt,
     mid_price, imbalance)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


async def init_table(db: aiosqlite.Connection) -> None:
    """Create the orderbook_snapshots table if it doesn't exist."""
    await db.executescript(CREATE_TABLE_SQL)
    await db.commit()
    logger.info("orderbook_snapshots table ready")


# ---------------------------------------------------------------------------
# Binance API
# ---------------------------------------------------------------------------
async def fetch_depth(
    client: httpx.AsyncClient,
    symbol: str,
) -> dict | None:
    """
    Fetch orderbook depth for a single symbol.

    Returns dict with keys: bids, asks (lists of [price_float, qty_float])
    or None on error.
    """
    try:
        resp = await client.get(
            DEPTH_URL,
            params={"symbol": symbol, "limit": DEPTH_LIMIT},
        )
        resp.raise_for_status()
        data = resp.json()

        bids = [[float(p), float(q)] for p, q in data.get("bids", [])]
        asks = [[float(p), float(q)] for p, q in data.get("asks", [])]

        return {"bids": bids, "asks": asks}

    except Exception as exc:
        logger.error(f"Failed to fetch depth for {symbol}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Processing & storage
# ---------------------------------------------------------------------------
def compute_metrics(bids: list, asks: list) -> dict:
    """Derive summary metrics from raw bid/ask levels."""
    best_bid = bids[0][0] if bids else 0.0
    best_ask = asks[0][0] if asks else 0.0
    spread = best_ask - best_bid if (best_bid and best_ask) else 0.0
    mid_price = (best_bid + best_ask) / 2.0 if (best_bid and best_ask) else 0.0

    bid_depth = sum(p * q for p, q in bids)
    ask_depth = sum(p * q for p, q in asks)
    total_depth = bid_depth + ask_depth
    imbalance = bid_depth / total_depth if total_depth > 0 else 0.5

    return {
        "best_bid": round(best_bid, 8),
        "best_ask": round(best_ask, 8),
        "spread": round(spread, 8),
        "mid_price": round(mid_price, 8),
        "bid_depth_usdt": round(bid_depth, 2),
        "ask_depth_usdt": round(ask_depth, 2),
        "imbalance": round(imbalance, 6),
    }


async def collect_and_store(
    client: httpx.AsyncClient,
    db: aiosqlite.Connection,
) -> int:
    """
    Fetch depth for all symbols and insert into DB.
    Returns the number of symbols successfully stored.
    """
    ts = datetime.now(timezone.utc).isoformat()
    stored = 0

    for symbol in SYMBOLS:
        depth = await fetch_depth(client, symbol)
        if depth is None:
            continue

        bids = depth["bids"]
        asks = depth["asks"]
        metrics = compute_metrics(bids, asks)

        await db.execute(INSERT_SQL, (
            symbol,
            ts,
            DEPTH_LIMIT,
            json.dumps(bids),
            json.dumps(asks),
            metrics["best_bid"],
            metrics["best_ask"],
            metrics["spread"],
            metrics["bid_depth_usdt"],
            metrics["ask_depth_usdt"],
            metrics["mid_price"],
            metrics["imbalance"],
        ))
        stored += 1

    if stored:
        await db.commit()

    return stored


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
async def main() -> None:
    logger.info(
        f"Orderbook Collector starting | "
        f"symbols={SYMBOLS} | interval={INTERVAL_SECONDS}s | "
        f"depth={DEPTH_LIMIT} levels | db={DB_PATH}"
    )

    db = await aiosqlite.connect(DB_PATH)
    await init_table(db)

    async with httpx.AsyncClient(timeout=15.0) as client:
        cycle = 0
        while _running:
            cycle += 1
            try:
                stored = await collect_and_store(client, db)
                logger.info(
                    f"Cycle #{cycle}: stored {stored}/{len(SYMBOLS)} orderbook snapshots"
                )
            except Exception as exc:
                logger.error(f"Cycle #{cycle} failed: {exc}", exc_info=True)

            # Sleep in small chunks so we can respond to SIGTERM quickly
            for _ in range(INTERVAL_SECONDS):
                if not _running:
                    break
                await asyncio.sleep(1)

    await db.close()
    logger.info("Orderbook Collector stopped cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Process exiting")
