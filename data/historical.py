"""
Historical candle data downloader from Binance Futures API.

Downloads 1-minute klines and stores them in the historical_candles table.
No API key required — klines are public data.

Binance limits:
    - Max 1500 candles per request
    - 1 day = 1440 candles (1m interval)
    - Rate limit: ~1200 req/min (we stay well under)
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

import httpx

from db import repository as repo

logger = logging.getLogger(__name__)

# Binance Futures klines endpoint (public, no auth needed)
KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
MAX_CANDLES_PER_REQUEST = 1500
INTERVAL = "1m"


async def download_klines(
    symbol: str,
    days: int = 2,
    progress_callback=None,
) -> dict:
    """
    Download historical 1-minute klines from Binance and store in DB.

    Args:
        symbol: Trading pair, e.g. "BTCUSDT"
        days: Number of days of history to download (max 30)
        progress_callback: Optional async callable(pct: float, msg: str)

    Returns:
        Dict with {symbol, days, candles_downloaded, time_range}
    """
    days = min(days, 30)  # Safety cap
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=days)

    start_ms = int(start_time.timestamp() * 1000)
    end_ms = int(end_time.timestamp() * 1000)

    total_expected = days * 1440  # approximate
    all_rows = []
    current_start = start_ms

    logger.info(f"Downloading {days}d of {symbol} klines ({total_expected} candles expected)")

    async with httpx.AsyncClient(timeout=30.0) as client:
        while current_start < end_ms:
            params = {
                "symbol": symbol,
                "interval": INTERVAL,
                "startTime": current_start,
                "endTime": end_ms,
                "limit": MAX_CANDLES_PER_REQUEST,
            }

            try:
                resp = await client.get(KLINES_URL, params=params)
                resp.raise_for_status()
                klines = resp.json()
            except Exception as e:
                logger.error(f"Binance klines request failed: {e}")
                break

            if not klines:
                break

            for k in klines:
                # Binance kline format:
                # [open_time, open, high, low, close, volume,
                #  close_time, quote_vol, trades, taker_buy_base, taker_buy_quote, ignore]
                row = (
                    symbol,
                    int(k[0]),       # open_time (ms)
                    float(k[1]),     # open
                    float(k[2]),     # high
                    float(k[3]),     # low
                    float(k[4]),     # close
                    float(k[5]),     # volume
                    int(k[6]),       # close_time (ms)
                )
                all_rows.append(row)

            # Move to next batch
            last_open_time = int(klines[-1][0])
            current_start = last_open_time + 60_000  # next minute

            # Report progress
            progress = min(len(all_rows) / max(total_expected, 1) * 100, 99)
            if progress_callback:
                await progress_callback(
                    progress,
                    f"Downloaded {len(all_rows)} candles..."
                )

            # Small delay to be respectful to API
            await asyncio.sleep(0.1)

    if not all_rows:
        return {
            "symbol": symbol,
            "days": days,
            "candles_downloaded": 0,
            "time_range": None,
        }

    # Bulk insert into DB
    saved = await repo.save_historical_candles(all_rows)

    first_time = datetime.fromtimestamp(all_rows[0][1] / 1000, tz=timezone.utc)
    last_time = datetime.fromtimestamp(all_rows[-1][1] / 1000, tz=timezone.utc)

    logger.info(
        f"Downloaded {saved} candles for {symbol}: "
        f"{first_time.strftime('%Y-%m-%d %H:%M')} → {last_time.strftime('%Y-%m-%d %H:%M')}"
    )

    if progress_callback:
        await progress_callback(100, f"Done! {saved} candles saved.")

    return {
        "symbol": symbol,
        "days": days,
        "candles_downloaded": saved,
        "time_range": {
            "start": first_time.isoformat(),
            "end": last_time.isoformat(),
        },
    }


async def download_all_symbols(symbols: list[str], days: int = 2) -> list[dict]:
    """Download klines for multiple symbols sequentially."""
    results = []
    for sym in symbols:
        result = await download_klines(sym, days=days)
        results.append(result)
    return results


async def get_data_status(symbols: list[str]) -> dict:
    """Check what historical data is available for given symbols."""
    status = {}
    for sym in symbols:
        info = await repo.get_historical_range(sym)
        if info:
            status[sym] = {
                "count": info["count"],
                "start": datetime.fromtimestamp(
                    info["min_time"] / 1000, tz=timezone.utc
                ).isoformat(),
                "end": datetime.fromtimestamp(
                    info["max_time"] / 1000, tz=timezone.utc
                ).isoformat(),
                "days": round(
                    (info["max_time"] - info["min_time"]) / (1000 * 86400), 1
                ),
            }
        else:
            status[sym] = {"count": 0, "start": None, "end": None, "days": 0}
    return status
