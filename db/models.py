"""
Database models as Python dataclasses.
These map 1:1 to SQLite table rows.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _utcnow():
    """Timezone-aware UTC timestamp (replaces deprecated datetime.utcnow)."""
    return datetime.now(timezone.utc)


@dataclass
class BotRecord:
    """Represents a row in the 'bots' table."""
    id: str                      # strategy name, used as primary key
    symbol: str                  # e.g. "BTCUSDT"
    status: str                  # "running" | "stopped" | "error"
    initial_balance: float
    live_enabled: bool = False   # whether live/simulation feed is active for this bot
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)


@dataclass
class TradeRecord:
    """Represents a row in the 'trades' table."""
    bot_id: str
    side: str                    # "BUY" | "SELL"
    symbol: str
    quantity: float
    price: float
    timestamp: datetime = field(default_factory=_utcnow)
    realized_pnl: Optional[float] = None   # filled on position close
    fee_usdt: Optional[float] = None       # trading fee deducted by SimulationEngine
    position_side: str = "LONG"             # "OPEN_LONG", "CLOSE_LONG", "OPEN_SHORT", "CLOSE_SHORT"
    id: Optional[int] = None               # auto-assigned by DB


@dataclass
class PortfolioSnapshot:
    """Represents a row in the 'portfolio_snapshots' table."""
    bot_id: str
    usdt_balance: float
    asset_balance: float         # how much of the traded asset is held
    asset_symbol: str            # e.g. "BTC"
    total_value_usdt: float      # usdt_balance + asset_balance * current_price
    timestamp: datetime = field(default_factory=_utcnow)
    asset_price: Optional[float] = None   # latest coin price at snapshot time
    id: Optional[int] = None              # auto-assigned by DB
