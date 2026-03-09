"""
Database models as Python dataclasses.
These map 1:1 to SQLite table rows.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class BotRecord:
    """Represents a row in the 'bots' table."""
    id: str                      # strategy name, used as primary key
    symbol: str                  # e.g. "BTCUSDT"
    status: str                  # "running" | "stopped" | "error"
    initial_balance: float
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class TradeRecord:
    """Represents a row in the 'trades' table."""
    bot_id: str
    side: str                    # "BUY" | "SELL"
    symbol: str
    quantity: float
    price: float
    timestamp: datetime = field(default_factory=datetime.utcnow)
    realized_pnl: Optional[float] = None   # filled on SELL
    fee_usdt: Optional[float] = None       # trading fee deducted by SimulationEngine
    id: Optional[int] = None               # auto-assigned by DB


@dataclass
class PortfolioSnapshot:
    """Represents a row in the 'portfolio_snapshots' table."""
    bot_id: str
    usdt_balance: float
    asset_balance: float         # how much of the traded asset is held
    asset_symbol: str            # e.g. "BTC"
    total_value_usdt: float      # usdt_balance + asset_balance * current_price
    timestamp: datetime = field(default_factory=datetime.utcnow)
    id: Optional[int] = None     # auto-assigned by DB
