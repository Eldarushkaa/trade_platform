"""
SimulationEngine — the fake exchange used in simulation mode.

It sits between a trading strategy and the portfolio/database.
When a bot calls place_order(), the engine:
  1. Executes the trade against VirtualPortfolio (math only, no money)
  2. Persists the trade to the database
  3. Returns the trade result

Key design: SimulationEngine and (future) LiveBinanceEngine both inherit
from BaseOrderEngine. Strategies only ever call BaseOrderEngine methods,
so switching to live trading requires ZERO strategy code changes.
"""
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from core.virtual_portfolio import VirtualPortfolio
from db import repository as repo
from db.models import TradeRecord, PortfolioSnapshot
from config import settings

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Abstract interface — shared by SimulationEngine & LiveBinanceEngine
# ------------------------------------------------------------------

class BaseOrderEngine(ABC):
    """
    Abstract order engine interface.

    Strategies call these methods. The engine implementation decides
    whether to simulate or send real orders to Binance.
    """

    @abstractmethod
    async def place_order(
        self,
        bot_id: str,
        symbol: str,
        side: str,           # "BUY" or "SELL"
        quantity: float,
        price: float,
    ) -> dict:
        """Place an order. Returns order result dict."""
        ...

    @abstractmethod
    async def get_balance(self, bot_id: str, asset: str) -> float:
        """Return the current balance of an asset for a bot."""
        ...

    @abstractmethod
    async def get_portfolio_state(self, bot_id: str) -> dict:
        """Return the full portfolio state for a bot."""
        ...


# ------------------------------------------------------------------
# Simulation Engine
# ------------------------------------------------------------------

class SimulationEngine(BaseOrderEngine):
    """
    Paper-trading engine. All trades happen virtually.
    Uses VirtualPortfolio for balance tracking and persists to SQLite.

    Fee handling:
        A configurable fee (settings.simulation_fee_rate, default 0.15%) is
        deducted from the USDT balance on every order — AFTER the portfolio
        executes the trade. Strategies are completely unaware of this.

        Fee calculation:
            fee_usdt = quantity * price * fee_rate

        For BUY:  trade executes first, then fee is deducted from USDT.
        For SELL: trade executes first (USDT credited), then fee is deducted.
    """

    def __init__(self) -> None:
        # Keyed by bot_id → VirtualPortfolio
        self._portfolios: dict[str, VirtualPortfolio] = {}
        # Latest known price per symbol (updated by BinanceFeed)
        self._prices: dict[str, float] = {}
        # Per-bot fee rates — allows different tiers per bot in the future
        self._fee_rates: dict[str, float] = {}

    def register_bot(
        self,
        bot_id: str,
        symbol: str,
        initial_usdt: Optional[float] = None,
    ) -> VirtualPortfolio:
        """
        Register a bot with the engine and create its virtual portfolio.
        Called by BotManager when a bot is added.
        """
        balance = initial_usdt if initial_usdt is not None else settings.initial_usdt_balance
        self._fee_rates[bot_id] = settings.simulation_fee_rate  # Bug #6 fix: per-bot fee rate
        portfolio = VirtualPortfolio(
            bot_id=bot_id,
            symbol=symbol,
            initial_usdt=balance,
        )
        self._portfolios[bot_id] = portfolio
        logger.info(
            f"Registered bot '{bot_id}' with {balance:.2f} USDT virtual balance "
            f"(fee rate: {self._fee_rates[bot_id] * 100:.3f}%)"
        )
        return portfolio

    def update_price(self, symbol: str, price: float) -> None:
        """Update the latest price for a symbol. Called by BinanceFeed."""
        self._prices[symbol] = price

    def get_price(self, symbol: str) -> Optional[float]:
        """Return the latest known price for a symbol."""
        return self._prices.get(symbol)

    # ------------------------------------------------------------------
    # BaseOrderEngine implementation
    # ------------------------------------------------------------------

    async def place_order(
        self,
        bot_id: str,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
    ) -> dict:
        """
        Execute a simulated order, then silently deduct the trading fee.

        Fee = quantity * price * fee_rate (always in USDT, always deducted).
        Strategies never see or interact with the fee — it is applied here
        after the portfolio executes the trade.
        """
        portfolio = self._get_portfolio(bot_id)
        side = side.upper()

        # Bug #6 fix: look up fee rate per bot
        fee_rate = self._fee_rates.get(bot_id, settings.simulation_fee_rate)
        fee_usdt = round(quantity * price * fee_rate, 8)

        # Bug #1 fix: validate that balance covers cost + fee BEFORE executing
        if side == "BUY":
            total_needed = quantity * price + fee_usdt
            if total_needed > portfolio.usdt_balance:
                raise ValueError(
                    f"[{bot_id}] Insufficient USDT balance. "
                    f"Need {total_needed:.4f} (cost + fee), have {portfolio.usdt_balance:.4f}"
                )

        if side == "BUY":
            result = portfolio.execute_buy(quantity, price)
        elif side == "SELL":
            result = portfolio.execute_sell(quantity, price)
        else:
            raise ValueError(f"Invalid order side: '{side}'. Must be 'BUY' or 'SELL'.")

        # --- Apply fee (invisible to the strategy) ---
        portfolio.deduct_fee(fee_usdt)
        result["fee_usdt"] = fee_usdt

        logger.debug(
            f"[{bot_id}] {side} fee: {fee_usdt:.4f} USDT "
            f"({fee_rate * 100:.3f}% of {quantity * price:.2f})"
        )

        # Persist trade to database (fee stored alongside the trade)
        trade = TradeRecord(
            bot_id=bot_id,
            side=side,
            symbol=symbol,
            quantity=quantity,
            price=price,
            realized_pnl=result.get("realized_pnl"),
            fee_usdt=fee_usdt,
            timestamp=datetime.utcnow(),
        )
        trade_id = await repo.insert_trade(trade)
        result["trade_id"] = trade_id

        return result

    async def get_balance(self, bot_id: str, asset: str) -> float:
        """Return current balance for an asset ('USDT' or the asset symbol)."""
        portfolio = self._get_portfolio(bot_id)
        asset = asset.upper()
        if asset == "USDT":
            return portfolio.usdt_balance
        if asset == portfolio.asset_symbol:
            return portfolio.position.quantity
        raise ValueError(f"Unknown asset '{asset}' for bot '{bot_id}'")

    async def get_portfolio_state(self, bot_id: str) -> dict:
        """Return full portfolio state dict with unrealized P&L."""
        portfolio = self._get_portfolio(bot_id)
        current_price = self._prices.get(portfolio.symbol)
        return portfolio.get_state(current_price)

    # ------------------------------------------------------------------
    # Snapshot persistence
    # ------------------------------------------------------------------

    async def save_snapshot(self, bot_id: str) -> None:
        """Persist a portfolio snapshot to the DB. Called periodically."""
        portfolio = self._get_portfolio(bot_id)
        current_price = self._prices.get(portfolio.symbol, 0.0)
        state = portfolio.get_state(current_price)

        snap = PortfolioSnapshot(
            bot_id=bot_id,
            usdt_balance=state["usdt_balance"],
            asset_balance=state["asset_balance"],
            asset_symbol=state["asset_symbol"],
            total_value_usdt=state["total_value_usdt"],
            timestamp=datetime.utcnow(),
        )
        await repo.insert_snapshot(snap)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_portfolio(self, bot_id: str) -> VirtualPortfolio:
        portfolio = self._portfolios.get(bot_id)
        if portfolio is None:
            raise KeyError(
                f"No portfolio found for bot '{bot_id}'. "
                "Was register_bot() called?"
            )
        return portfolio


# ------------------------------------------------------------------
# Global engine singleton
# ------------------------------------------------------------------
# BotManager and strategies share this instance.
simulation_engine = SimulationEngine()
