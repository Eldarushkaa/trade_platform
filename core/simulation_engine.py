"""
SimulationEngine — the fake exchange used in simulation mode.

USDT-Margined Futures simulation:
  - Supports LONG and SHORT positions via VirtualPortfolio
  - Routes BUY/SELL to open/close positions based on current state
  - Checks liquidation on every price tick
  - Applies configurable trading fees

Order routing logic:
    BUY + no position  → open LONG
    BUY + SHORT open   → close SHORT
    SELL + no position  → open SHORT
    SELL + LONG open    → close LONG

Key design: SimulationEngine and (future) LiveBinanceEngine both inherit
from BaseOrderEngine. Strategies only ever call BaseOrderEngine methods,
so switching to live trading requires ZERO strategy code changes.
"""
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
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
# Simulation Engine — Futures Mode
# ------------------------------------------------------------------

class SimulationEngine(BaseOrderEngine):
    """
    Paper-trading engine for USDT-Margined perpetual futures.
    All trades happen virtually. Uses VirtualPortfolio for position
    tracking and persists to SQLite.

    Fee handling:
        A configurable fee (settings.simulation_fee_rate, default 0.05%)
        is deducted from USDT on every order. Strategies are unaware.

    Liquidation:
        Checked on every price tick via update_price(). If the current
        price crosses the liquidation price, the position is force-closed
        and all margin is lost.
    """

    def __init__(self) -> None:
        self._portfolios: dict[str, VirtualPortfolio] = {}
        self._prices: dict[str, float] = {}
        self._fee_rates: dict[str, float] = {}
        self._skip_db: bool = False  # Set True during backtest to avoid DB writes

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
        self._fee_rates[bot_id] = settings.simulation_fee_rate
        portfolio = VirtualPortfolio(
            bot_id=bot_id,
            symbol=symbol,
            initial_usdt=balance,
            leverage=settings.leverage,
        )
        self._portfolios[bot_id] = portfolio
        logger.info(
            f"Registered bot '{bot_id}' with {balance:.2f} USDT virtual balance "
            f"(fee: {self._fee_rates[bot_id] * 100:.3f}%, leverage: {settings.leverage}x)"
        )
        return portfolio

    def update_price(self, symbol: str, price: float) -> None:
        """
        Update the latest price for a symbol. Called by BinanceFeed.
        Also checks liquidation for all portfolios trading this symbol.
        """
        self._prices[symbol] = price
        # Check liquidation on every tick
        for portfolio in self._portfolios.values():
            if portfolio.symbol == symbol:
                portfolio.check_liquidation(price)

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
        Execute a simulated futures order.

        Routing:
            BUY + no position  → open LONG
            BUY + SHORT open   → close SHORT
            SELL + no position  → open SHORT
            SELL + LONG open    → close LONG

        Fee is applied after the trade executes.
        """
        portfolio = self._get_portfolio(bot_id)
        side = side.upper()
        position = portfolio.position
        fee_rate = self._fee_rates.get(bot_id, settings.simulation_fee_rate)

        # --- Route order based on side + current position ---
        if side == "BUY":
            if position.side == "SHORT" and position.is_open:
                # Close SHORT — use position's quantity for fee calc
                close_qty = position.quantity
                fee_usdt = round(close_qty * price * fee_rate, 8)
                result = portfolio.close_short(price)
                quantity = close_qty  # for DB record
            elif not position.is_open:
                # Open LONG — check margin + fee before executing
                fee_usdt = round(quantity * price * fee_rate, 8)
                notional = quantity * price
                margin_needed = notional / portfolio.leverage
                if margin_needed + fee_usdt > portfolio.usdt_balance:
                    raise ValueError(
                        f"[{bot_id}] Insufficient margin. "
                        f"Need {margin_needed + fee_usdt:.4f} (margin + fee), "
                        f"have {portfolio.usdt_balance:.4f}"
                    )
                result = portfolio.open_long(quantity, price)
            else:
                raise ValueError(
                    f"[{bot_id}] Cannot BUY — already in {position.side} position"
                )

        elif side == "SELL":
            if position.side == "LONG" and position.is_open:
                # Close LONG — use position's quantity for fee calc
                close_qty = position.quantity
                fee_usdt = round(close_qty * price * fee_rate, 8)
                result = portfolio.close_long(price)
                quantity = close_qty  # for DB record
            elif not position.is_open:
                # Open SHORT — check margin + fee before executing
                fee_usdt = round(quantity * price * fee_rate, 8)
                notional = quantity * price
                margin_needed = notional / portfolio.leverage
                if margin_needed + fee_usdt > portfolio.usdt_balance:
                    raise ValueError(
                        f"[{bot_id}] Insufficient margin. "
                        f"Need {margin_needed + fee_usdt:.4f} (margin + fee), "
                        f"have {portfolio.usdt_balance:.4f}"
                    )
                result = portfolio.open_short(quantity, price)
            else:
                raise ValueError(
                    f"[{bot_id}] Cannot SELL — already in {position.side} position"
                )
        else:
            raise ValueError(f"Invalid order side: '{side}'. Must be 'BUY' or 'SELL'.")

        # --- Apply fee ---
        portfolio.deduct_fee(fee_usdt)
        result["fee_usdt"] = fee_usdt

        logger.debug(
            f"[{bot_id}] {side} fee: {fee_usdt:.4f} USDT "
            f"({fee_rate * 100:.3f}% of {quantity * price:.2f})"
        )

        # --- Persist trade to database (skip during backtest) ---
        if not self._skip_db:
            action = result.get("action", side)
            trade = TradeRecord(
                bot_id=bot_id,
                side=side,
                symbol=symbol,
                quantity=quantity,
                price=price,
                realized_pnl=result.get("realized_pnl"),
                fee_usdt=fee_usdt,
                position_side=action,
                timestamp=datetime.now(timezone.utc),
            )
            trade_id = await repo.insert_trade(trade)
            result["trade_id"] = trade_id
        else:
            result["trade_id"] = -1

        return result

    async def get_balance(self, bot_id: str, asset: str) -> float:
        """
        Return current balance for an asset.

        Special values:
            "USDT"     → free USDT balance
            "POSITION" → returns quantity (positive for LONG, negative for SHORT, 0 for none)
            asset name → position quantity (always positive)
        """
        portfolio = self._get_portfolio(bot_id)
        asset = asset.upper()
        if asset == "USDT":
            return portfolio.usdt_balance
        if asset == "POSITION":
            pos = portfolio.position
            if pos.side == "LONG":
                return pos.quantity
            elif pos.side == "SHORT":
                return -pos.quantity
            return 0.0
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
            asset_balance=state["position_qty"],
            asset_symbol=state["asset_symbol"],
            total_value_usdt=state["total_value_usdt"],
            asset_price=current_price if current_price else None,
            timestamp=datetime.now(timezone.utc),
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
simulation_engine = SimulationEngine()
