"""
Historical candle data downloader from Binance Futures API.

Downloads klines at a configurable interval (1m, 5m, 15m, 1h) and stores
them in the historical_candles table (keyed by symbol + interval + open_time).
No API key required — klines are public data.

Binance limits:
    - Max 1500 candles per request
    - Rate limit: ~1200 req/min (we stay well under)
    - Max supported download window: 5 years (1,825 days)
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

# Supported intervals and their properties
SUPPORTED_INTERVALS: dict[str, dict] = {
    "1m":  {"minutes": 1,   "candles_per_day": 1440},
    "5m":  {"minutes": 5,   "candles_per_day": 288},
    "15m": {"minutes": 15,  "candles_per_day": 96},
    "1h":  {"minutes": 60,  "candles_per_day": 24},
}

# Legacy constants kept for backward compatibility
INTERVAL = "15m"
CANDLES_PER_DAY = 96
CANDLE_STEP_MS  = 15 * 60_000


def _interval_step_ms(interval: str) -> int:
    """Return candle step in milliseconds for the given interval string."""
    info = SUPPORTED_INTERVALS.get(interval)
    if info is None:
        raise ValueError(f"Unsupported interval '{interval}'. Use one of: {list(SUPPORTED_INTERVALS)}")
    return info["minutes"] * 60_000


def _candles_per_day(interval: str) -> int:
    """Return number of candles per day for the given interval."""
    info = SUPPORTED_INTERVALS.get(interval)
    if info is None:
        raise ValueError(f"Unsupported interval '{interval}'.")
    return info["candles_per_day"]


async def download_klines(
    symbol: str,
    days: int = 14,
    start_date: str | None = None,
    interval: str = "15m",
    progress_callback=None,
) -> dict:
    """
    Download historical klines from Binance and store in DB.

    Args:
        symbol:     Trading pair, e.g. "BTCUSDT"
        days:       Number of days of history to download (max 1825 = ~5 years)
        start_date: Optional ISO date string "YYYY-MM-DD" (UTC).
                    When given, the download window is [start_date, start_date + days].
                    When omitted, the window is [now - days, now].
        interval:   Candle timeframe: "1m", "5m", "15m" (default), "1h"
        progress_callback: Optional async callable(pct: float, msg: str)

    Returns:
        Dict with {symbol, interval, days, candles_downloaded, time_range}
    """
    if interval not in SUPPORTED_INTERVALS:
        raise ValueError(f"Unsupported interval '{interval}'. Use one of: {list(SUPPORTED_INTERVALS)}")

    days = min(days, 1825)  # Safety cap — 5 years

    if start_date:
        start_time = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_time   = start_time + timedelta(days=days)
        # Never go beyond "now" even if the requested window extends into the future
        end_time   = min(end_time, datetime.now(timezone.utc))
    else:
        end_time   = datetime.now(timezone.utc)
        start_time = end_time - timedelta(days=days)

    start_ms = int(start_time.timestamp() * 1000)
    end_ms   = int(end_time.timestamp() * 1000)

    candle_step_ms   = _interval_step_ms(interval)
    candles_per_day  = _candles_per_day(interval)
    total_expected   = days * candles_per_day  # approximate
    all_rows: list[tuple] = []
    current_start = start_ms

    logger.info(
        f"Downloading {days}d of {symbol} {interval} klines "
        f"({total_expected} candles expected, "
        f"from {start_time.strftime('%Y-%m-%d')} to {end_time.strftime('%Y-%m-%d')})"
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        while current_start < end_ms:
            params = {
                "symbol": symbol,
                "interval": interval,
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
            current_start = last_open_time + candle_step_ms

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
            "interval": interval,
            "days": days,
            "start_date": start_date,
            "candles_downloaded": 0,
            "time_range": None,
        }

    # Bulk insert into DB (with interval)
    saved = await repo.save_historical_candles(all_rows, interval=interval)

    first_time = datetime.fromtimestamp(all_rows[0][1] / 1000, tz=timezone.utc)
    last_time  = datetime.fromtimestamp(all_rows[-1][1] / 1000, tz=timezone.utc)

    logger.info(
        f"Downloaded {saved} {interval} candles for {symbol}: "
        f"{first_time.strftime('%Y-%m-%d %H:%M')} → {last_time.strftime('%Y-%m-%d %H:%M')}"
    )

    if progress_callback:
        await progress_callback(100, f"Done! {saved} candles saved.")

    return {
        "symbol": symbol,
        "interval": interval,
        "days": days,
        "start_date": start_date,
        "candles_downloaded": saved,
        "time_range": {
            "start": first_time.isoformat(),
            "end": last_time.isoformat(),
        },
    }


async def download_all_symbols(
    symbols: list[str],
    days: int = 14,
    start_date: str | None = None,
    interval: str = "15m",
) -> list[dict]:
    """Download klines for multiple symbols sequentially."""
    results = []
    for sym in symbols:
        result = await download_klines(sym, days=days, start_date=start_date, interval=interval)
        results.append(result)
    return results


async def get_data_status(symbols: list[str], interval: str = "15m") -> dict:
    """Check what historical data is available for given symbols at the given interval."""
    status = {}
    for sym in symbols:
        info = await repo.get_historical_range(sym, interval=interval)
        if info:
            status[sym] = {
                "count": info["count"],
                "start": datetime.fromtimestamp(
                    info["min_time"] / 1000, tz=timezone.utc
                ).isoformat(),
                "end": datetime.fromtimestamp(
                    info["max_time"] / 1000, tz=timezone.utc
                ).isoformat(),
                "start_ms": info["min_time"],   # epoch ms — used by frontend year shortcuts
                "end_ms":   info["max_time"],   # epoch ms — used by frontend year shortcuts
                "days": round(
                    (info["max_time"] - info["min_time"]) / (1000 * 86400), 1
                ),
            }
        else:
            status[sym] = {"count": 0, "start": None, "end": None, "start_ms": None, "end_ms": None, "days": 0}
    return status
