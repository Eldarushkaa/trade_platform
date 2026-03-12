"""
Orderbook Wall Bot — trades liquidity gaps in the order book (DOM).

Strategy concept — "Gap before a wall":
    A "wall" is a price level with unusually large resting order volume
    (e.g. 3× the average notional per level). A "gap" is a zone of thin
    liquidity BETWEEN the current price and the wall.

    When a gap exists before a wall, price can move through the thin zone
    rapidly to reach the wall:

    ┌─────────────────────────────────────────────────────────┐
    │  ASK wall above + gap below wall  → price jumps UP  → LONG  │
    │  BID wall below + gap above wall  → price drops DOWN → SHORT │
    └─────────────────────────────────────────────────────────┘

Signal logic (per candle):
    1. Fetch the latest orderbook snapshot from DB for this symbol.
    2. Detect walls on the ASK side (sell walls) and BID side (buy walls).
    3. Score the gap quality: gap_width / price × 100 (% distance to wall).
    4. If a large ASK wall is present with a significant thin-liquidity gap
       above current price → open LONG (target = wall price).
    5. If a large BID wall is present with a significant thin-liquidity gap
       below current price → open SHORT (target = wall price).
    6. Exit on: candle close past target, stop-loss hit, or signal reversal.

Multi-coin usage:
    OrderbookWallBot.for_symbol("BTCUSDT")
    OrderbookWallBot.for_symbol("ETHUSDT")
    OrderbookWallBot.for_symbol("SOLUSDT")

Requires:
    scripts/collect_orderbook.py running (fills orderbook_snapshots table).
    If no fresh snapshot is available (<= staleness_minutes old), the bot
    skips the candle silently.
"""
import json
import logging
from typing import TYPE_CHECKING, Optional

from db import repository as repo
from core.base_strategy import BaseStrategy
from core.simulation_engine import BaseOrderEngine

if TYPE_CHECKING:
    from data.candle_aggregator import Candle

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper: parse orderbook snapshot into typed lists
# ---------------------------------------------------------------------------

def _parse_levels(json_str: str) -> list[tuple[float, float]]:
    """Parse bids/asks JSON → list of (price, qty) tuples."""
    try:
        raw = json.loads(json_str) if isinstance(json_str, str) else json_str
        return [(float(p), float(q)) for p, q in raw]
    except Exception:
        return []


def _notional(levels: list[tuple[float, float]]) -> list[float]:
    """Convert (price, qty) levels to notional (price × qty)."""
    return [p * q for p, q in levels]


def detect_gap_and_wall(
    levels: list[tuple[float, float]],
    wall_multiplier: float,
    gap_min_pct: float,
    current_price: float,
    side: str,
) -> Optional[dict]:
    """
    Scan a side of the orderbook for a gap-then-wall pattern.

    Args:
        levels:          Sorted list of (price, qty) — asks ascending, bids descending.
        wall_multiplier: A level is a "wall" if its notional ≥ this × avg notional.
        gap_min_pct:     Minimum gap width as % of current price before the wall.
        current_price:   Used to compute distances.
        side:            "ask" or "bid".

    Returns:
        None if no pattern found, else:
        {
            "wall_price":      float,   # price of the wall level
            "wall_notional":   float,   # USD notional at the wall
            "gap_start_price": float,   # price of first thin level after current
            "gap_pct":         float,   # gap width as % of current price
            "levels_in_gap":   int,     # number of thin levels in the gap
        }
    """
    if len(levels) < 3:
        return None

    notionals = _notional(levels)
    avg_notional = sum(notionals) / len(notionals) if notionals else 0.0
    if avg_notional <= 0:
        return None

    wall_threshold = avg_notional * wall_multiplier

    # Find the first wall level
    wall_idx = None
    for i, (price, qty) in enumerate(levels):
        if notionals[i] >= wall_threshold:
            wall_idx = i
            break

    if wall_idx is None or wall_idx == 0:
        return None  # wall is at best price — no gap

    wall_price = levels[wall_idx][0]
    wall_notional = notionals[wall_idx]

    # Gap = levels between current price and the wall that are thin
    gap_start_price = levels[0][0]

    # Measure gap width as % of current price
    if side == "ask":
        gap_pct = abs(wall_price - current_price) / current_price * 100
    else:
        gap_pct = abs(current_price - wall_price) / current_price * 100

    if gap_pct < gap_min_pct:
        return None  # gap too small

    # Count thin levels in the gap
    thin_levels_in_gap = sum(
        1 for i in range(0, wall_idx)
        if notionals[i] < avg_notional * 0.5  # < 50% of avg = thin
    )

    # Require at least half the gap levels to be thin
    if wall_idx > 0 and thin_levels_in_gap < wall_idx * 0.4:
        return None

    return {
        "wall_price": wall_price,
        "wall_notional": wall_notional,
        "gap_start_price": gap_start_price,
        "gap_pct": round(gap_pct, 4),
        "levels_in_gap": wall_idx,
        "avg_notional": round(avg_notional, 2),
    }


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class OrderbookWallBot(BaseStrategy):
    """Trades liquidity gaps before order-book walls."""

    name = "ob_wall_bot"
    symbol = "BTCUSDT"

    # --- Tunable parameters ---
    WALL_MULTIPLIER: float = 3.0      # Wall ≥ N× average level notional
    GAP_MIN_PCT: float = 0.10         # Minimum gap (% of price) to the wall
    TRADE_FRACTION: float = 0.8       # Fraction of free USDT for margin
    STALENESS_MINUTES: int = 3        # Skip if latest snapshot is older than this
    COOLDOWN_CANDLES: int = 5         # Min candles between new entries
    STOP_LOSS_PCT: float = 0.30       # Stop loss: % move against us (from entry)
    TAKE_PROFIT_PCT: float = 0.50     # Take profit: % move in our direction

    PARAM_SCHEMA = {
        "WALL_MULTIPLIER": {
            "type": "float", "default": 3.0, "min": 1.5, "max": 10.0,
            "description": "Wall is N× average level notional",
        },
        "GAP_MIN_PCT": {
            "type": "float", "default": 0.10, "min": 0.01, "max": 2.0,
            "description": "Min gap width (% of price) before wall to trigger signal",
        },
        "TRADE_FRACTION": {
            "type": "float", "default": 0.8, "min": 0.10, "max": 1.0,
            "description": "Fraction of free USDT to risk per trade",
            "optimize": False,
        },
        "STALENESS_MINUTES": {
            "type": "int", "default": 3, "min": 1, "max": 10,
            "description": "Max age (minutes) of orderbook snapshot to use",
        },
        "COOLDOWN_CANDLES": {
            "type": "int", "default": 5, "min": 0, "max": 30,
            "description": "Min candles between new entries",
        },
        "STOP_LOSS_PCT": {
            "type": "float", "default": 0.30, "min": 0.05, "max": 2.0,
            "description": "Stop loss % from entry price",
        },
        "TAKE_PROFIT_PCT": {
            "type": "float", "default": 0.50, "min": 0.05, "max": 3.0,
            "description": "Take profit % from entry price",
        },
    }

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def for_symbol(cls, symbol: str) -> type:
        asset = symbol.replace("USDT", "").lower()
        return type(
            f"{cls.__name__}_{asset.upper()}",
            (cls,),
            {"name": f"ob_wall_{asset}", "symbol": symbol},
        )

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def __init__(self, engine: "BaseOrderEngine") -> None:
        super().__init__(engine)
        self._candle_count: int = 0
        self._last_trade_candle: int = -999
        # Track open position targets for exit logic
        self._entry_price: Optional[float] = None
        self._position_side: Optional[str] = None  # "LONG" or "SHORT"
        self._take_profit: Optional[float] = None
        self._stop_loss: Optional[float] = None
        # Backtest injection: set by engine before each candle in replay mode
        self._bt_snapshot: Optional[dict] = None

    def _inject_orderbook(self, snapshot: dict) -> None:
        """
        Called by the backtest engine before each on_candle() call.
        Stores the nearest historical orderbook snapshot so that
        _get_fresh_snapshot() returns it instead of hitting the live DB.
        """
        self._bt_snapshot = snapshot

    # ------------------------------------------------------------------
    # Candle logic
    # ------------------------------------------------------------------

    async def on_candle(self, candle: "Candle") -> None:
        self._candle_count += 1
        price = candle.close

        # --- Check exit conditions for open position ---
        position_qty = await self.engine.get_balance(self.name, "POSITION")
        has_position = abs(position_qty) > 1e-9

        if has_position and self._entry_price is not None:
            exited = await self._check_exit(price, position_qty)
            if exited:
                return  # Don't open a new position same candle we exited

        # --- Cooldown check ---
        if self._candle_count - self._last_trade_candle < self.COOLDOWN_CANDLES:
            return

        # --- Already in a position, skip entry signals ---
        if has_position:
            return

        # --- Fetch latest orderbook snapshot ---
        snapshot = await self._get_fresh_snapshot()
        if snapshot is None:
            return

        bids = _parse_levels(snapshot.get("bids_json") or snapshot.get("bids", []))
        asks = _parse_levels(snapshot.get("asks_json") or snapshot.get("asks", []))

        if not bids or not asks:
            return

        # --- Detect gap+wall patterns ---
        # ASK side: gap above current price → potential LONG
        ask_signal = detect_gap_and_wall(
            asks, self.WALL_MULTIPLIER, self.GAP_MIN_PCT, price, "ask"
        )
        # BID side: gap below current price → potential SHORT
        bid_signal = detect_gap_and_wall(
            bids, self.WALL_MULTIPLIER, self.GAP_MIN_PCT, price, "bid"
        )

        # Prioritise the larger wall (more conviction)
        if ask_signal and bid_signal:
            if ask_signal["wall_notional"] >= bid_signal["wall_notional"]:
                bid_signal = None
            else:
                ask_signal = None

        if ask_signal:
            self.logger.info(
                f"GAP+WALL (ASK) detected: wall_price={ask_signal['wall_price']:.4f} "
                f"gap={ask_signal['gap_pct']:.3f}% "
                f"wall={ask_signal['wall_notional']:.0f} USD "
                f"({ask_signal['levels_in_gap']} thin levels)"
            )
            await self._open(price, "BUY", ask_signal)

        elif bid_signal:
            self.logger.info(
                f"GAP+WALL (BID) detected: wall_price={bid_signal['wall_price']:.4f} "
                f"gap={bid_signal['gap_pct']:.3f}% "
                f"wall={bid_signal['wall_notional']:.0f} USD "
                f"({bid_signal['levels_in_gap']} thin levels)"
            )
            await self._open(price, "SELL", bid_signal)

    # ------------------------------------------------------------------
    # Exit logic
    # ------------------------------------------------------------------

    async def _check_exit(self, price: float, position_qty: float) -> bool:
        """Return True if we exited the position."""
        if self._position_side is None:
            return False

        side = self._position_side
        tp = self._take_profit
        sl = self._stop_loss

        should_exit = False
        reason = ""

        if side == "LONG":
            if tp and price >= tp:
                should_exit = True
                reason = f"TP hit @ {price:.4f} (target {tp:.4f})"
            elif sl and price <= sl:
                should_exit = True
                reason = f"SL hit @ {price:.4f} (stop {sl:.4f})"
        elif side == "SHORT":
            if tp and price <= tp:
                should_exit = True
                reason = f"TP hit @ {price:.4f} (target {tp:.4f})"
            elif sl and price >= sl:
                should_exit = True
                reason = f"SL hit @ {price:.4f} (stop {sl:.4f})"

        if should_exit:
            close_side = "SELL" if side == "LONG" else "BUY"
            await self._close(price, close_side, reason)
            return True

        return False

    # ------------------------------------------------------------------
    # Order helpers
    # ------------------------------------------------------------------

    async def _open(self, price: float, side: str, signal: dict) -> None:
        """Open a position based on the gap+wall signal."""
        usdt = await self.engine.get_balance(self.name, "USDT")
        if usdt < 10:
            self.logger.warning("Insufficient USDT for margin")
            return

        spend = usdt * self.TRADE_FRACTION
        quantity = round(spend / price, 6)
        direction = "LONG" if side == "BUY" else "SHORT"

        try:
            result = await self.engine.place_order(
                bot_id=self.name,
                symbol=self.symbol,
                side=side,
                quantity=quantity,
                price=price,
            )
            self._last_trade_candle = self._candle_count
            self._entry_price = price
            self._position_side = direction

            # Set exit levels
            if direction == "LONG":
                self._take_profit = price * (1 + self.TAKE_PROFIT_PCT / 100)
                self._stop_loss   = price * (1 - self.STOP_LOSS_PCT / 100)
            else:
                self._take_profit = price * (1 - self.TAKE_PROFIT_PCT / 100)
                self._stop_loss   = price * (1 + self.STOP_LOSS_PCT / 100)

            self.logger.info(
                f"OPEN {direction} {quantity:.6f} @ {price:.4f}  "
                f"wall={signal['wall_price']:.4f} gap={signal['gap_pct']:.3f}%  "
                f"TP={self._take_profit:.4f}  SL={self._stop_loss:.4f}  "
                f"fee={result.get('fee_usdt', 0):.4f}"
            )
        except ValueError as exc:
            self.logger.error(f"OPEN {direction} failed: {exc}")

    async def _close(self, price: float, side: str, reason: str) -> None:
        """Close the current position."""
        try:
            result = await self.engine.place_order(
                bot_id=self.name,
                symbol=self.symbol,
                side=side,
                quantity=0,
                price=price,
            )
            self._last_trade_candle = self._candle_count
            self._entry_price = None
            self._position_side = None
            self._take_profit = None
            self._stop_loss = None

            pnl = result.get("realized_pnl", 0)
            self.logger.info(
                f"{reason}  P&L={pnl:+.4f}  fee={result.get('fee_usdt', 0):.4f}"
            )
        except ValueError as exc:
            self.logger.error(f"Close failed: {exc}")

    # ------------------------------------------------------------------
    # Snapshot fetching
    # ------------------------------------------------------------------

    async def _get_fresh_snapshot(self) -> Optional[dict]:
        """
        Return the current orderbook snapshot.

        - In backtest mode: returns the pre-injected snapshot (no DB call,
          no staleness check — the engine already matched it by timestamp).
        - In live mode: fetches the latest snapshot from DB and checks staleness.
        """
        # --- Backtest mode: use injected snapshot ---
        if self._bt_snapshot is not None:
            snap = self._bt_snapshot
            self._bt_snapshot = None  # consume — prevents reuse across candles
            return snap

        # --- Live mode: fetch from DB and check staleness ---
        from datetime import datetime, timezone, timedelta

        snap = await repo.get_orderbook_full(self.symbol)
        if snap is None:
            self.logger.debug(f"No orderbook snapshot for {self.symbol}")
            return None

        try:
            ts = datetime.fromisoformat(snap["timestamp"].replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - ts
            if age > timedelta(minutes=self.STALENESS_MINUTES):
                self.logger.debug(
                    f"Orderbook snapshot too old: {age.total_seconds() / 60:.1f} min "
                    f"(max {self.STALENESS_MINUTES} min)"
                )
                return None
        except Exception:
            return None

        return snap
