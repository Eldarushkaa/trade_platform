"""
VirtualPortfolio tracks a bot's simulated balances, open position and P&L.

One VirtualPortfolio instance is created per bot.
It is the single source of truth for the bot's financial state in simulation mode.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """Represents the currently held position in the traded asset."""
    symbol: str
    quantity: float = 0.0
    avg_entry_price: float = 0.0  # average price paid per unit

    @property
    def is_open(self) -> bool:
        return self.quantity > 0.0

    def unrealized_pnl(self, current_price: float) -> float:
        """P&L if the position were closed right now."""
        if not self.is_open:
            return 0.0
        return (current_price - self.avg_entry_price) * self.quantity


class VirtualPortfolio:
    """
    Manages virtual USDT balance and asset position for one bot.

    All amounts are in USDT unless noted otherwise.
    """

    def __init__(
        self,
        bot_id: str,
        symbol: str,
        initial_usdt: float,
    ) -> None:
        self.bot_id = bot_id
        self.symbol = symbol
        self.asset_symbol = symbol.replace("USDT", "")  # e.g. "BTC" from "BTCUSDT"

        self.usdt_balance: float = initial_usdt
        self.initial_balance: float = initial_usdt
        self.position: Position = Position(symbol=symbol)
        self.realized_pnl: float = 0.0
        self.total_fees_paid: float = 0.0   # cumulative USDT fees deducted by engine
        self.trade_count: int = 0

        self.logger = logging.getLogger(f"portfolio.{bot_id}")

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def execute_buy(self, quantity: float, price: float) -> dict:
        """
        Execute a simulated BUY order.

        Args:
            quantity: Amount of asset to buy.
            price:    Price per unit in USDT.

        Returns:
            Dict with order details and updated portfolio state.

        Raises:
            ValueError: If insufficient USDT balance.
        """
        cost = quantity * price

        if cost > self.usdt_balance:
            raise ValueError(
                f"[{self.bot_id}] Insufficient USDT balance. "
                f"Need {cost:.2f}, have {self.usdt_balance:.2f}"
            )

        # Update USDT balance
        self.usdt_balance -= cost

        # Update position (average entry price calculation)
        if self.position.is_open:
            total_qty = self.position.quantity + quantity
            self.position.avg_entry_price = (
                (self.position.avg_entry_price * self.position.quantity + price * quantity)
                / total_qty
            )
            self.position.quantity = total_qty
        else:
            self.position.quantity = quantity
            self.position.avg_entry_price = price

        self.trade_count += 1
        self.logger.info(
            f"BUY  {quantity:.6f} {self.asset_symbol} @ {price:.2f} USDT | "
            f"Cost: {cost:.2f} | USDT left: {self.usdt_balance:.2f}"
        )

        return {
            "side": "BUY",
            "symbol": self.symbol,
            "quantity": quantity,
            "price": price,
            "cost": cost,
            "realized_pnl": None,
        }

    def execute_sell(self, quantity: float, price: float) -> dict:
        """
        Execute a simulated SELL order.

        Args:
            quantity: Amount of asset to sell.
            price:    Price per unit in USDT.

        Returns:
            Dict with order details, realized P&L, and updated portfolio state.

        Raises:
            ValueError: If insufficient asset balance.
        """
        if quantity > self.position.quantity:
            raise ValueError(
                f"[{self.bot_id}] Insufficient {self.asset_symbol} balance. "
                f"Need {quantity:.6f}, have {self.position.quantity:.6f}"
            )

        proceeds = quantity * price
        cost_basis = quantity * self.position.avg_entry_price
        realized_pnl = proceeds - cost_basis

        # Update balances
        self.usdt_balance += proceeds
        self.position.quantity -= quantity
        self.realized_pnl += realized_pnl

        if self.position.quantity <= 1e-10:  # floating point safety
            self.position.quantity = 0.0
            self.position.avg_entry_price = 0.0

        self.trade_count += 1
        self.logger.info(
            f"SELL {quantity:.6f} {self.asset_symbol} @ {price:.2f} USDT | "
            f"Proceeds: {proceeds:.2f} | Realized P&L: {realized_pnl:+.2f} | "
            f"USDT: {self.usdt_balance:.2f}"
        )

        return {
            "side": "SELL",
            "symbol": self.symbol,
            "quantity": quantity,
            "price": price,
            "proceeds": proceeds,
            "realized_pnl": realized_pnl,
        }

    def deduct_fee(self, fee_usdt: float) -> None:
        """
        Deduct a trading fee from the USDT balance.
        Called by SimulationEngine after executing each order.
        Strategies never call this directly.
        """
        self.usdt_balance -= fee_usdt
        self.total_fees_paid += fee_usdt
        self.logger.debug(f"Fee deducted: {fee_usdt:.4f} USDT | Total fees: {self.total_fees_paid:.4f}")

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    def get_state(self, current_price: Optional[float] = None) -> dict:
        """Return a full snapshot of the portfolio state."""
        unrealized = (
            self.position.unrealized_pnl(current_price)
            if current_price is not None
            else 0.0
        )
        total_value = (
            self.usdt_balance + self.position.quantity * current_price
            if current_price is not None
            else self.usdt_balance
        )
        # Bug #4 fix: expose net_pnl = realized_pnl minus all fees paid.
        # realized_pnl is the gross spread (sell proceeds - buy cost_basis).
        # net_pnl is what the bot actually earned after exchange fees.
        net_pnl = self.realized_pnl - self.total_fees_paid
        return {
            "bot_id": self.bot_id,
            "symbol": self.symbol,
            "asset_symbol": self.asset_symbol,
            "usdt_balance": round(self.usdt_balance, 4),
            "asset_balance": round(self.position.quantity, 8),
            "avg_entry_price": round(self.position.avg_entry_price, 4),
            "realized_pnl": round(self.realized_pnl, 4),       # gross (before fees)
            "net_pnl": round(net_pnl, 4),                      # true profit after fees
            "unrealized_pnl": round(unrealized, 4),
            "total_value_usdt": round(total_value, 4),
            "return_pct": round(
                (total_value - self.initial_balance) / self.initial_balance * 100, 2
            ),
            "trade_count": self.trade_count,
            "total_fees_paid": round(self.total_fees_paid, 4),
        }
