"""
Historical candle data downloader from Binance Futures API.

Downloads 5-minute klines and stores them in the historical_candles table.
No API key required — klines are public data.

Binance limits:
    - Max 1500 candles per request
    - 1 day = 288 candles (5m interval), 1 year ≈ 105,120 candles
    - Rate limit: ~1200 req/min (we stay well under)
    - Max supported download window: 3 years (1,095 days)
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
INTERVAL = "5m"
CANDLES_PER_DAY = 288          # 24h × 60min / 5 = 288 candles per day
CANDLE_STEP_MS  = 5 * 60_000   # 5 minutes in milliseconds


async def download_klines(
    symbol: str,
    days: int = 14,
    start_date: str | None = None,
    progress_callback=None,
) -> dict:
    """
    Download historical 5-minute klines from Binance and store in DB.

    Args:
        symbol:     Trading pair, e.g. "BTCUSDT"
        days:       Number of days of history to download (max 1095 = ~3 years)
        start_date: Optional ISO date string "YYYY-MM-DD" (UTC).
                    When given, the download window is [start_date, start_date + days].
                    When omitted, the window is [now - days, now].
        progress_callback: Optional async callable(pct: float, msg: str)

    Returns:
        Dict with {symbol, days, candles_downloaded, time_range}
    """
    days = min(days, 1095)  # Safety cap — 3 years

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

    total_expected = days * CANDLES_PER_DAY  # approximate
    all_rows: list[tuple] = []
    current_start = start_ms

    logger.info(
        f"Downloading {days}d of {symbol} 5m klines "
        f"({total_expected} candles expected, "
        f"from {start_time.strftime('%Y-%m-%d')} to {end_time.strftime('%Y-%m-%d')})"
    )

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

            # Move to next batch (advance by 5-minute step)
            last_open_time = int(klines[-1][0])
            current_start = last_open_time + CANDLE_STEP_MS

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
            "start_date": start_date,
            "candles_downloaded": 0,
            "time_range": None,
        }

    # Bulk insert into DB
    saved = await repo.save_historical_candles(all_rows)

    first_time = datetime.fromtimestamp(all_rows[0][1] / 1000, tz=timezone.utc)
    last_time  = datetime.fromtimestamp(all_rows[-1][1] / 1000, tz=timezone.utc)

    logger.info(
        f"Downloaded {saved} 5m candles for {symbol}: "
        f"{first_time.strftime('%Y-%m-%d %H:%M')} → {last_time.strftime('%Y-%m-%d %H:%M')}"
    )

    if progress_callback:
        await progress_callback(100, f"Done! {saved} candles saved.")

    return {
        "symbol": symbol,
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
) -> list[dict]:
    """Download klines for multiple symbols sequentially."""
    results = []
    for sym in symbols:
        result = await download_klines(sym, days=days, start_date=start_date)
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
