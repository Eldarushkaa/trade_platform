"""
VirtualPortfolio — tracks a bot's simulated futures balances, positions and P&L.

USDT-Margined Perpetual Futures simulation:
- Supports LONG and SHORT positions
- Configurable leverage (margin = notional / leverage)
- Automatic liquidation when losses exceed margin
- P&L is settled in USDT

One VirtualPortfolio instance is created per bot.
It is the single source of truth for the bot's financial state in simulation mode.
"""
import logging
from dataclasses import dataclass
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class FuturesPosition:
    """
    Represents an open futures position (LONG or SHORT).

    Margin is the USDT collateral locked for this position.
    Liquidation occurs when unrealized losses consume all margin.
    """
    symbol: str
    side: str = "NONE"            # "LONG", "SHORT", "NONE"
    quantity: float = 0.0          # position size in asset units
    entry_price: float = 0.0       # average entry price
    leverage: int = 1              # leverage multiplier
    margin: float = 0.0            # USDT collateral locked
    liquidation_price: float = 0.0 # price at which position is liquidated

    @property
    def is_open(self) -> bool:
        return self.side != "NONE" and self.quantity > 0.0

    def unrealized_pnl(self, current_price: float) -> float:
        """P&L if the position were closed right now."""
        if not self.is_open:
            return 0.0
        if self.side == "LONG":
            return (current_price - self.entry_price) * self.quantity
        elif self.side == "SHORT":
            return (self.entry_price - current_price) * self.quantity
        return 0.0

    def margin_ratio(self, current_price: float) -> float:
        """
        How close to liquidation: 0.0 = safe, 1.0 = liquidated.
        Ratio of unrealized loss to margin.
        """
        if not self.is_open or self.margin <= 0:
            return 0.0
        pnl = self.unrealized_pnl(current_price)
        if pnl >= 0:
            return 0.0
        return min(abs(pnl) / self.margin, 1.0)

    def notional_value(self, current_price: float) -> float:
        """Current notional value of the position."""
        return self.quantity * current_price if self.is_open else 0.0

    def reset(self) -> None:
        """Clear the position to NONE."""
        self.side = "NONE"
        self.quantity = 0.0
        self.entry_price = 0.0
        self.margin = 0.0
        self.liquidation_price = 0.0


class VirtualPortfolio:
    """
    Manages virtual USDT balance and futures position for one bot.

    Futures model:
    - usdt_balance: free USDT (not locked in positions)
    - position.margin: USDT locked as collateral for open position
    - total_value = usdt_balance + margin + unrealized_pnl
    """

    def __init__(
        self,
        bot_id: str,
        symbol: str,
        initial_usdt: float,
        leverage: int = None,
    ) -> None:
        self.bot_id = bot_id
        self.symbol = symbol
        self.asset_symbol = symbol.replace("USDT", "")  # e.g. "BTC" from "BTCUSDT"
        self.leverage = leverage if leverage is not None else settings.leverage

        self.usdt_balance: float = initial_usdt
        self.initial_balance: float = initial_usdt
        self.position: FuturesPosition = FuturesPosition(
            symbol=symbol, leverage=self.leverage
        )
        self.realized_pnl: float = 0.0
        self.total_fees_paid: float = 0.0
        self.trade_count: int = 0
        self.liquidation_count: int = 0

        self.logger = logging.getLogger(f"portfolio.{bot_id}")

    # ------------------------------------------------------------------
    # Position opening
    # ------------------------------------------------------------------

    def open_long(self, quantity: float, price: float) -> dict:
        """
        Open a LONG futures position.

        Margin = notional / leverage is deducted from USDT balance.
        Liquidation price = entry × (1 - 1/leverage) approximately.

        Raises:
            ValueError: If position already open or insufficient margin.
        """
        if self.position.is_open:
            raise ValueError(
                f"[{self.bot_id}] Cannot open LONG — already in {self.position.side} position"
            )

        notional = quantity * price
        margin = notional / self.leverage

        if margin > self.usdt_balance:
            raise ValueError(
                f"[{self.bot_id}] Insufficient margin. "
                f"Need {margin:.2f} USDT, have {self.usdt_balance:.2f}"
            )

        # Lock margin
        self.usdt_balance -= margin

        # Calculate liquidation price (LONG: price drops → loss)
        # Loss = (entry - liq) × qty = margin → liq = entry - margin/qty
        liq_price = price - (margin / quantity) if quantity > 0 else 0.0

        self.position.side = "LONG"
        self.position.quantity = quantity
        self.position.entry_price = price
        self.position.margin = margin
        self.position.liquidation_price = max(liq_price, 0.0)

        self.trade_count += 1
        self.logger.info(
            f"OPEN LONG {quantity:.6f} {self.asset_symbol} @ {price:.2f} | "
            f"Margin: {margin:.2f} | Liq: {self.position.liquidation_price:.2f} | "
            f"Leverage: {self.leverage}x | Free USDT: {self.usdt_balance:.2f}"
        )

        return {
            "side": "BUY",
            "action": "OPEN_LONG",
            "symbol": self.symbol,
            "quantity": quantity,
            "price": price,
            "margin": margin,
            "leverage": self.leverage,
            "liquidation_price": self.position.liquidation_price,
            "realized_pnl": None,
        }

    def open_short(self, quantity: float, price: float) -> dict:
        """
        Open a SHORT futures position.

        Margin = notional / leverage is deducted from USDT balance.
        Liquidation price = entry × (1 + 1/leverage) approximately.

        Raises:
            ValueError: If position already open or insufficient margin.
        """
        if self.position.is_open:
            raise ValueError(
                f"[{self.bot_id}] Cannot open SHORT — already in {self.position.side} position"
            )

        notional = quantity * price
        margin = notional / self.leverage

        if margin > self.usdt_balance:
            raise ValueError(
                f"[{self.bot_id}] Insufficient margin. "
                f"Need {margin:.2f} USDT, have {self.usdt_balance:.2f}"
            )

        # Lock margin
        self.usdt_balance -= margin

        # Calculate liquidation price (SHORT: price rises → loss)
        # Loss = (liq - entry) × qty = margin → liq = entry + margin/qty
        liq_price = price + (margin / quantity) if quantity > 0 else 0.0

        self.position.side = "SHORT"
        self.position.quantity = quantity
        self.position.entry_price = price
        self.position.margin = margin
        self.position.liquidation_price = liq_price

        self.trade_count += 1
        self.logger.info(
            f"OPEN SHORT {quantity:.6f} {self.asset_symbol} @ {price:.2f} | "
            f"Margin: {margin:.2f} | Liq: {self.position.liquidation_price:.2f} | "
            f"Leverage: {self.leverage}x | Free USDT: {self.usdt_balance:.2f}"
        )

        return {
            "side": "SELL",
            "action": "OPEN_SHORT",
            "symbol": self.symbol,
            "quantity": quantity,
            "price": price,
            "margin": margin,
            "leverage": self.leverage,
            "liquidation_price": self.position.liquidation_price,
            "realized_pnl": None,
        }

    # ------------------------------------------------------------------
    # Position closing
    # ------------------------------------------------------------------

    def close_long(self, price: float, quantity: Optional[float] = None) -> dict:
        """
        Close a LONG position (fully or partially) at the given price.

        Args:
            price:    Execution price.
            quantity: Amount to close. If None or >= position quantity, closes fully.

        Returns margin portion + PnL to USDT balance.
        """
        if self.position.side != "LONG" or not self.position.is_open:
            raise ValueError(f"[{self.bot_id}] No LONG position to close")

        total_qty = self.position.quantity
        close_qty = quantity if (quantity is not None and quantity < total_qty) else total_qty
        partial = close_qty < total_qty

        # Proportional margin for the closed slice
        margin_fraction = close_qty / total_qty
        margin_used = self.position.margin * margin_fraction

        pnl = (price - self.position.entry_price) * close_qty
        returned = margin_used + pnl
        returned = max(returned, 0.0)

        self.usdt_balance += returned
        self.realized_pnl += pnl
        self.trade_count += 1

        self.logger.info(
            f"CLOSE{'_PARTIAL' if partial else ''} LONG {close_qty:.6f}/{total_qty:.6f} "
            f"{self.asset_symbol} @ {price:.2f} | "
            f"P&L: {pnl:+.2f} | Returned: {returned:.2f} | "
            f"USDT: {self.usdt_balance:.2f}"
        )

        result = {
            "side": "SELL",
            "action": "CLOSE_LONG" if not partial else "CLOSE_LONG_PARTIAL",
            "symbol": self.symbol,
            "quantity": close_qty,
            "price": price,
            "realized_pnl": pnl,
            "margin_returned": returned,
        }

        if partial:
            # Reduce position size and margin proportionally
            self.position.quantity -= close_qty
            self.position.margin -= margin_used
        else:
            self.position.reset()

        return result

    def close_short(self, price: float, quantity: Optional[float] = None) -> dict:
        """
        Close a SHORT position (fully or partially) at the given price.

        Args:
            price:    Execution price.
            quantity: Amount to close. If None or >= position quantity, closes fully.

        Returns margin portion + PnL to USDT balance.
        """
        if self.position.side != "SHORT" or not self.position.is_open:
            raise ValueError(f"[{self.bot_id}] No SHORT position to close")

        total_qty = self.position.quantity
        close_qty = quantity if (quantity is not None and quantity < total_qty) else total_qty
        partial = close_qty < total_qty

        margin_fraction = close_qty / total_qty
        margin_used = self.position.margin * margin_fraction

        pnl = (self.position.entry_price - price) * close_qty
        returned = margin_used + pnl
        returned = max(returned, 0.0)

        self.usdt_balance += returned
        self.realized_pnl += pnl
        self.trade_count += 1

        self.logger.info(
            f"CLOSE{'_PARTIAL' if partial else ''} SHORT {close_qty:.6f}/{total_qty:.6f} "
            f"{self.asset_symbol} @ {price:.2f} | "
            f"P&L: {pnl:+.2f} | Returned: {returned:.2f} | "
            f"USDT: {self.usdt_balance:.2f}"
        )

        result = {
            "side": "BUY",
            "action": "CLOSE_SHORT" if not partial else "CLOSE_SHORT_PARTIAL",
            "symbol": self.symbol,
            "quantity": close_qty,
            "price": price,
            "realized_pnl": pnl,
            "margin_returned": returned,
        }

        if partial:
            self.position.quantity -= close_qty
            self.position.margin -= margin_used
        else:
            self.position.reset()

        return result

    # ------------------------------------------------------------------
    # Liquidation
    # ------------------------------------------------------------------

    def check_liquidation(self, current_price: float) -> bool:
        """
        Check if the current price triggers liquidation.
        Called on every price tick by SimulationEngine.

        Returns True if liquidation occurred.
        """
        if not self.position.is_open:
            return False

        liquidated = False
        if self.position.side == "LONG" and current_price <= self.position.liquidation_price:
            liquidated = True
        elif self.position.side == "SHORT" and current_price >= self.position.liquidation_price:
            liquidated = True

        if liquidated:
            self.logger.warning(
                f"🔴 LIQUIDATED {self.position.side} {self.position.quantity:.6f} "
                f"{self.asset_symbol} | Entry: {self.position.entry_price:.2f} | "
                f"Liq price: {self.position.liquidation_price:.2f} | "
                f"Current: {current_price:.2f} | Margin lost: {self.position.margin:.2f}"
            )
            # Lose all margin — nothing returned
            loss = -self.position.margin
            self.realized_pnl += loss
            self.liquidation_count += 1
            self.position.reset()

        return liquidated

    # ------------------------------------------------------------------
    # Fee handling
    # ------------------------------------------------------------------

    def deduct_fee(self, fee_usdt: float) -> None:
        """
        Deduct a trading fee from the USDT balance.
        Called by SimulationEngine after executing each order.

        The engine performs a combined (margin + fee) sufficiency check before
        calling open_long/open_short, so under normal operation this will not
        push the balance below zero. The clamp is a safety net for rounding
        edge cases (e.g. float arithmetic on very small balances).
        """
        if fee_usdt > self.usdt_balance:
            self.logger.warning(
                f"Fee {fee_usdt:.6f} USDT exceeds remaining balance "
                f"{self.usdt_balance:.6f} USDT — clamping to zero "
                f"(total underrun: {fee_usdt - self.usdt_balance:.8f} USDT)"
            )
            self.total_fees_paid += self.usdt_balance
            self.usdt_balance = 0.0
        else:
            self.usdt_balance -= fee_usdt
            self.total_fees_paid += fee_usdt

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    def get_state(self, current_price: Optional[float] = None) -> dict:
        """Return a full snapshot of the portfolio state."""
        unrealized = (
            self.position.unrealized_pnl(current_price)
            if current_price is not None and self.position.is_open
            else 0.0
        )

        # Total value = free USDT + locked margin + unrealized PnL
        margin_locked = self.position.margin if self.position.is_open else 0.0
        total_value = self.usdt_balance + margin_locked + unrealized

        margin_ratio = (
            self.position.margin_ratio(current_price)
            if current_price is not None and self.position.is_open
            else 0.0
        )

        net_pnl = self.realized_pnl - self.total_fees_paid

        return {
            "bot_id": self.bot_id,
            "symbol": self.symbol,
            "asset_symbol": self.asset_symbol,
            "usdt_balance": round(self.usdt_balance, 4),
            # Futures-specific fields
            "position_side": self.position.side,
            "position_qty": round(self.position.quantity, 8),
            "entry_price": round(self.position.entry_price, 4),
            "leverage": self.leverage,
            "margin_locked": round(margin_locked, 4),
            "liquidation_price": round(self.position.liquidation_price, 4),
            "margin_ratio": round(margin_ratio, 4),
            # P&L
            "realized_pnl": round(self.realized_pnl, 4),
            "net_pnl": round(net_pnl, 4),
            "unrealized_pnl": round(unrealized, 4),
            "total_value_usdt": round(total_value, 4),
            "return_pct": round(
                (total_value - self.initial_balance) / self.initial_balance * 100, 2
            ),
            # Counters
            "trade_count": self.trade_count,
            "total_fees_paid": round(self.total_fees_paid, 4),
            "liquidation_count": self.liquidation_count,
        }
