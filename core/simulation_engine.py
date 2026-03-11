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
        self.enable_netting: bool = True  # Cross-bot position netting to reduce fees
        # Netting stats: symbol → {events, qty_netted, fees_saved_usdt}
        self._netting_stats: dict[str, dict] = {}

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
    # Cross-bot netting helpers
    # ------------------------------------------------------------------

    def _find_opposing_bots(self, requesting_bot_id: str, symbol: str, opening_side: str) -> list[str]:
        """
        Find bots that hold the opposite position on the same symbol.

        Args:
            requesting_bot_id: The bot about to open a new position.
            symbol:            The trading pair (e.g. "BTCUSDT").
            opening_side:      "LONG" or "SHORT" — the side the requester wants to open.

        Returns:
            List of bot IDs with an opposite open position on `symbol`.
        """
        opposing_side = "SHORT" if opening_side == "LONG" else "LONG"
        result = []
        for other_id, other_portfolio in self._portfolios.items():
            if other_id == requesting_bot_id:
                continue
            if other_portfolio.symbol != symbol:
                continue
            if other_portfolio.position.is_open and other_portfolio.position.side == opposing_side:
                result.append(other_id)
        return result

    async def _net_opposing_position(
        self,
        opposing_bot_id: str,
        symbol: str,
        price: float,
        max_qty: float,
    ) -> float:
        """
        Partially or fully close an opposing bot's position to net against an
        incoming order of size `max_qty`.

        Only `min(opposing_position_qty, max_qty)` is closed, so a large opposing
        position is only partially reduced when the requesting order is smaller.

        Args:
            opposing_bot_id: Bot whose position to close.
            symbol:          Trading pair.
            price:           Execution price.
            max_qty:         The requesting bot's order size — cap for netting.

        Returns:
            Quantity actually netted (may be less than max_qty if opposing position
            was smaller — caller can use this to track remaining qty to net elsewhere).
        """
        other_portfolio = self._portfolios[opposing_bot_id]
        pos = other_portfolio.position
        fee_rate = self._fee_rates.get(opposing_bot_id, settings.simulation_fee_rate)

        # Net only up to the requesting order size
        net_qty = min(pos.quantity, max_qty)
        fee_usdt = round(net_qty * price * fee_rate, 8)

        if pos.side == "LONG":
            result = other_portfolio.close_long(price, quantity=net_qty)
            action_label = "CLOSE_LONG_NETTED"
            db_side = "SELL"
        else:
            result = other_portfolio.close_short(price, quantity=net_qty)
            action_label = "CLOSE_SHORT_NETTED"
            db_side = "BUY"

        other_portfolio.deduct_fee(fee_usdt)
        result["fee_usdt"] = fee_usdt

        logger.info(
            f"[NETTING] {action_label} {net_qty:.6f}/{pos.quantity + net_qty:.6f} "
            f"on {opposing_bot_id} @ {price:.2f} | "
            f"PnL: {result.get('realized_pnl', 0.0):+.4f} USDT"
        )

        # Accumulate netting stats
        ns = self._netting_stats.setdefault(symbol, {"events": 0, "qty_netted": 0.0, "fees_saved_usdt": 0.0})
        ns["events"] += 1
        ns["qty_netted"] += net_qty
        # Fee saved = the open fee the opposing bot would have paid on its NEXT trade
        # (it now re-enters fresh instead of holding a stale position until its own signal)
        # Conservative estimate: one open fee on net_qty at current price
        ns["fees_saved_usdt"] += fee_usdt  # opposing bot's close fee is what we calculate

        if not self._skip_db:
            trade = TradeRecord(
                bot_id=opposing_bot_id,
                side=db_side,
                symbol=symbol,
                quantity=net_qty,
                price=price,
                realized_pnl=result.get("realized_pnl"),
                fee_usdt=fee_usdt,
                position_side=action_label,
                timestamp=datetime.now(timezone.utc),
            )
            await repo.insert_trade(trade)

        return net_qty

    def get_netting_stats(self) -> dict:
        """
        Return accumulated cross-bot netting statistics.

        Returns a dict keyed by symbol with:
          - events:          Number of netting operations performed
          - qty_netted:      Total asset quantity netted across all operations
          - fees_saved_usdt: Estimated fees saved (close fees on netted quantity)
        Also includes a 'total' entry summing all symbols.
        """
        result = dict(self._netting_stats)
        total_events = sum(v["events"] for v in result.values())
        total_qty = sum(v["qty_netted"] for v in result.values())
        total_fees = sum(v["fees_saved_usdt"] for v in result.values())
        result["_total"] = {
            "events": total_events,
            "qty_netted": round(total_qty, 8),
            "fees_saved_usdt": round(total_fees, 6),
        }
        return result

    def get_coin_positions(self) -> dict:
        """
        Per-symbol aggregate position view across all bots.

        For each symbol returns:
          - total_long_qty:   Sum of all open LONG quantities
          - total_short_qty:  Sum of all open SHORT quantities
          - net_qty:          total_long_qty - total_short_qty
          - net_side:         "LONG" / "SHORT" / "FLAT"
          - long_bots:        List of bot_ids holding LONGs
          - short_bots:       List of bot_ids holding SHORTs
        """
        by_symbol: dict[str, dict] = {}
        for bot_id, portfolio in self._portfolios.items():
            sym = portfolio.symbol
            if sym not in by_symbol:
                by_symbol[sym] = {
                    "total_long_qty": 0.0,
                    "total_short_qty": 0.0,
                    "long_bots": [],
                    "short_bots": [],
                }
            pos = portfolio.position
            if pos.is_open:
                if pos.side == "LONG":
                    by_symbol[sym]["total_long_qty"] += pos.quantity
                    by_symbol[sym]["long_bots"].append(bot_id)
                elif pos.side == "SHORT":
                    by_symbol[sym]["total_short_qty"] += pos.quantity
                    by_symbol[sym]["short_bots"].append(bot_id)

        for sym, data in by_symbol.items():
            net = data["total_long_qty"] - data["total_short_qty"]
            data["net_qty"] = round(net, 8)
            data["net_side"] = "LONG" if net > 1e-10 else ("SHORT" if net < -1e-10 else "FLAT")
            data["total_long_qty"] = round(data["total_long_qty"], 8)
            data["total_short_qty"] = round(data["total_short_qty"], 8)

        return by_symbol

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
            BUY + no position  → open LONG  (with cross-bot netting check)
            BUY + SHORT open   → close SHORT
            SELL + no position  → open SHORT (with cross-bot netting check)
            SELL + LONG open    → close LONG

        Cross-bot netting:
            Before opening a NEW position, if any other bot on the same symbol
            holds the opposite position, that bot's position is closed first at
            the current price. This avoids redundant open+close round trips and
            saves fees for the opposing bot's next trade cycle.

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
                # Net opposing SHORT positions before opening LONG
                if self.enable_netting:
                    remaining = quantity
                    for opposing_id in self._find_opposing_bots(bot_id, symbol, "LONG"):
                        if remaining <= 0:
                            break
                        netted = await self._net_opposing_position(opposing_id, symbol, price, remaining)
                        remaining -= netted
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
                # Net opposing LONG positions before opening SHORT
                if self.enable_netting:
                    remaining = quantity
                    for opposing_id in self._find_opposing_bots(bot_id, symbol, "SHORT"):
                        if remaining <= 0:
                            break
                        netted = await self._net_opposing_position(opposing_id, symbol, price, remaining)
                        remaining -= netted
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
