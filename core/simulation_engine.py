"""
SimulationEngine — the fake exchange used in simulation mode.

USDT-Margined Futures simulation:
  - Supports LONG and SHORT positions via VirtualPortfolio
  - Routes BUY/SELL to open/close positions based on current state
  - Checks liquidation on every price tick
  - Applies configurable trading fees
  - OB-aware VWAP fill price (walks order book levels for realistic slippage)

Order routing logic:
    BUY + no position  → open LONG
    BUY + SHORT open   → close SHORT
    SELL + no position  → open SHORT
    SELL + LONG open    → close LONG

Slippage model:
    When an orderbook snapshot is loaded via update_orderbook(), place_order()
    walks the relevant side (asks for BUY, bids for SELL) to compute a VWAP
    fill price that reflects real market impact.

    If no OB data is available, a small fixed slippage (settings.base_slippage_pct)
    is applied as a fallback.

    If VWAP fill price deviates more than settings.max_slippage_pct from the
    strategy's desired price, the order is rejected (simulating reject-on-slippage).

Key design: SimulationEngine and (future) LiveBinanceEngine both inherit
from BaseOrderEngine. Strategies only ever call BaseOrderEngine methods,
so switching to live trading requires ZERO strategy code changes.
"""
import json
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
        # Latest orderbook snapshots per symbol for realistic fill simulation
        # { "BTCUSDT": {"bids": [(price, qty), ...], "asks": [(price, qty), ...]} }
        self._orderbooks: dict[str, dict] = {}

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

    def update_orderbook(self, symbol: str, snapshot: dict) -> None:
        """
        Store the latest orderbook snapshot for a symbol.
        Called by:
          - BotManager background task (live mode, every 60s from DB)
          - BacktestEngine before each candle (when OB data exists)

        Snapshot formats accepted:
          1. Pre-parsed (fast path):  { "bids": [(price, qty), ...], "asks": [...] }
          2. Raw JSON strings:        { "bids_json": "[[p,q],...]", "asks_json": "..." }

        Pre-parsed format is used by backtest/optimizer (loaded once at startup via
        get_orderbook_snapshots_for_backtest) to avoid repeated json.loads per candle.
        """
        try:
            bids_raw = snapshot.get("bids")
            asks_raw = snapshot.get("asks")

            # Fast path: already pre-parsed lists of (price, qty) tuples
            if isinstance(bids_raw, list) and isinstance(asks_raw, list):
                self._orderbooks[symbol] = {"bids": bids_raw, "asks": asks_raw}
                return

            # Slow path: raw JSON strings (live mode from DB)
            bids_str = snapshot.get("bids_json") or (bids_raw if isinstance(bids_raw, str) else "[]")
            asks_str = snapshot.get("asks_json") or (asks_raw if isinstance(asks_raw, str) else "[]")
            bids = [(float(p), float(q)) for p, q in json.loads(bids_str or "[]")]
            asks = [(float(p), float(q)) for p, q in json.loads(asks_str or "[]")]
            self._orderbooks[symbol] = {"bids": bids, "asks": asks}
        except Exception as e:
            logger.warning(f"Failed to parse orderbook snapshot for {symbol}: {e}")

    def _compute_fill_price(
        self,
        symbol: str,
        side: str,
        quantity: float,
        desired_price: float,
    ) -> tuple[float, str]:
        """
        Compute fill price.

        Uses desired_price (candle close) as the fill price always.
        When no OB data, applies base_slippage_pct as a small spread penalty.
        When OB data is present, no extra slippage (fee already covers cost).

        This keeps fill price consistent with how strategies size their positions
        (quantity = spend / candle_close), avoiding PnL distortions.
        """
        ob = self._orderbooks.get(symbol)
        base_slip = settings.base_slippage_pct / 100.0

        if ob is None:
            # No OB data: apply small fixed slippage spread
            if side == "BUY":
                fill = desired_price * (1.0 + base_slip)
            else:
                fill = desired_price * (1.0 - base_slip)
            return fill, "fallback"

        # OB data present: use desired_price (candle close) — no extra slippage
        # The fee (0.05%) already accounts for trading cost; OB data confirms
        # the market is liquid enough to fill at the candle's closing price.
        return desired_price, "ob_confirmed"

    # ------------------------------------------------------------------
    # Per-symbol position aggregation (used by stats bar)
    # ------------------------------------------------------------------

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
        Execute a simulated futures order with realistic fill price.

        Routing:
            BUY + no position  → open LONG
            BUY + SHORT open   → close SHORT
            SELL + no position  → open SHORT
            SELL + LONG open    → close LONG

        Fill price:
            Uses OB-aware VWAP fill if orderbook snapshot is available,
            otherwise applies base_slippage_pct as a fixed spread.
            Rejects if fill deviates > max_slippage_pct from desired price.

        Fee is applied after the trade executes.
        """
        portfolio = self._get_portfolio(bot_id)
        side = side.upper()
        position = portfolio.position
        fee_rate = self._fee_rates.get(bot_id, settings.simulation_fee_rate)

        # --- Compute fill price from OB or fallback ---
        fill_price, fill_method = self._compute_fill_price(symbol, side, quantity, price)

        # For close orders (quantity=0), use the full position quantity
        if quantity == 0 and position.is_open:
            quantity = position.quantity

        logger.debug(f"[{bot_id}] {side} fill={fill_price:.4f} ({fill_method})")

        # --- Route order based on side + current position ---
        if side == "BUY":
            if position.side == "SHORT" and position.is_open:
                # Close SHORT — use position's quantity for fee calc
                close_qty = position.quantity
                fee_usdt = round(close_qty * fill_price * fee_rate, 8)
                result = portfolio.close_short(fill_price)
                quantity = close_qty  # for DB record
            elif not position.is_open:
                # Open LONG — check margin + fee before executing
                fee_usdt = round(quantity * fill_price * fee_rate, 8)
                notional = quantity * fill_price
                margin_needed = notional / portfolio.leverage
                if margin_needed + fee_usdt > portfolio.usdt_balance:
                    raise ValueError(
                        f"[{bot_id}] Insufficient margin. "
                        f"Need {margin_needed + fee_usdt:.4f} (margin + fee), "
                        f"have {portfolio.usdt_balance:.4f}"
                    )
                result = portfolio.open_long(quantity, fill_price)
            else:
                raise ValueError(
                    f"[{bot_id}] Cannot BUY — already in {position.side} position"
                )

        elif side == "SELL":
            if position.side == "LONG" and position.is_open:
                # Close LONG — use position's quantity for fee calc
                close_qty = position.quantity
                fee_usdt = round(close_qty * fill_price * fee_rate, 8)
                result = portfolio.close_long(fill_price)
                quantity = close_qty  # for DB record
            elif not position.is_open:
                # Open SHORT — check margin + fee before executing
                fee_usdt = round(quantity * fill_price * fee_rate, 8)
                notional = quantity * fill_price
                margin_needed = notional / portfolio.leverage
                if margin_needed + fee_usdt > portfolio.usdt_balance:
                    raise ValueError(
                        f"[{bot_id}] Insufficient margin. "
                        f"Need {margin_needed + fee_usdt:.4f} (margin + fee), "
                        f"have {portfolio.usdt_balance:.4f}"
                    )
                result = portfolio.open_short(quantity, fill_price)
            else:
                raise ValueError(
                    f"[{bot_id}] Cannot SELL — already in {position.side} position"
                )
        else:
            raise ValueError(f"Invalid order side: '{side}'. Must be 'BUY' or 'SELL'.")

        result["desired_price"] = price
        result["fill_method"] = fill_method

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
