"""
In-memory log buffer — captures WARNING+ log records from all loggers
and exposes them via GET /api/logs.

The handler is installed once at import time (called from main.py).
It is capped at MAX_RECORDS entries (FIFO).
"""
import logging
from collections import deque
from datetime import datetime, timezone

from fastapi import APIRouter, Query

router = APIRouter(prefix="/logs", tags=["Logs"])

MAX_RECORDS = 500   # ring-buffer size


class _MemoryLogHandler(logging.Handler):
    """Thread-safe in-memory handler that keeps the last N WARNING+ records."""

    def __init__(self, maxlen: int = MAX_RECORDS) -> None:
        super().__init__(level=logging.WARNING)
        self._buf: deque[dict] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buf.append({
                "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
            })
        except Exception:
            self.handleError(record)

    def get_records(self, level_filter: str | None = None) -> list[dict]:
        records = list(self._buf)
        if level_filter:
            lv = level_filter.upper()
            records = [r for r in records if r["level"] == lv]
        return records

    def clear(self) -> None:
        self._buf.clear()


# Singleton handler — installed by install_handler()
_handler: _MemoryLogHandler | None = None


def install_handler() -> None:
    """
    Install the in-memory log handler on the root logger.
    Call this once from main.py after logging.basicConfig().
    """
    global _handler
    _handler = _MemoryLogHandler(maxlen=MAX_RECORDS)
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.getLogger().addHandler(_handler)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@router.get("")
async def get_logs(
    level: str = Query(default="WARNING", description="Minimum level: WARNING or ERROR"),
    limit: int = Query(default=200, ge=1, le=MAX_RECORDS),
):
    """
    Return recent log records at WARNING level or above.

    - **level**: filter to WARNING or ERROR only
    - **limit**: max records to return (newest last)
    """
    if _handler is None:
        return {"records": [], "total": 0, "note": "Log handler not installed"}

    records = _handler.get_records()

    # Filter: if level=ERROR only return ERROR+CRITICAL
    if level.upper() == "ERROR":
        records = [r for r in records if r["level"] in ("ERROR", "CRITICAL")]

    # Return newest last, capped at limit
    records = records[-limit:]
    return {"records": records, "total": len(records)}


@router.delete("")
async def clear_logs():
    """Clear the in-memory log buffer."""
    if _handler:
        _handler.clear()
    return {"cleared": True}
