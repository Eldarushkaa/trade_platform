"""
orderbook_feed — on-demand Binance Futures order-book depth fetcher.

``fetch_depth(symbol)`` makes a single REST call to Binance Futures and returns
pre-parsed bids/asks ready for use by SimulationEngine as an ``ob_fetcher``.

Every time a bot places an order in live mode, SimulationEngine calls
``fetch_depth`` to get a fresh depth snapshot and walks the levels for a
realistic VWAP fill price, reflecting real market impact / slippage.

Binance Futures public depth endpoint — no API key required:
    GET https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=20
"""
import logging

import httpx

logger = logging.getLogger(__name__)

# Binance Futures public depth endpoint (no auth required)
_DEPTH_URL = "https://fapi.binance.com/fapi/v1/depth"

# How many levels per side to fetch.
# 20 is plenty for VWAP walk on typical bot order sizes (95% of USDT balance).
# Deeper = more accurate for very large orders, but slower REST response.
_DEPTH_LIMIT = 20


async def fetch_depth(symbol: str) -> dict | None:
    """
    Fetch the current order-book depth for *symbol* from Binance Futures.

    Returns a dict with pre-parsed bids/asks ready for ``engine.update_orderbook()``:
        {
            "bids": [(price, qty), ...],   # descending price (best bid first)
            "asks": [(price, qty), ...],   # ascending price  (best ask first)
        }

    Returns None on any network or parse error (caller falls back to fixed slippage).

    This function is designed to be passed as ``ob_fetcher`` to ``SimulationEngine``:
        engine = SimulationEngine(ob_fetcher=fetch_depth)
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                _DEPTH_URL,
                params={"symbol": symbol.upper(), "limit": _DEPTH_LIMIT},
            )
            resp.raise_for_status()
            data = resp.json()

        bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in data.get("asks", [])]

        if not bids or not asks:
            logger.warning(f"fetch_depth: empty bids/asks for {symbol}")
            return None

        return {"bids": bids, "asks": asks}

    except httpx.TimeoutException:
        logger.warning(f"fetch_depth {symbol}: timeout — falling back to fixed slippage")
        return None
    except Exception as exc:
        logger.warning(f"fetch_depth {symbol}: {exc} — falling back to fixed slippage")
        return None
