"""
Backtest Engine — replays historical candles through a strategy.

Creates an isolated SimulationEngine + Strategy instance (no contamination
with live data) and feeds stored candles one by one, collecting:
    - Equity curve (portfolio value at each candle)
    - All trades
    - Performance metrics (Sharpe, max drawdown, win rate, etc.)

Usage:
    result = await run_backtest("rsi_btc", "BTCUSDT", RSIBot, params={...})
"""
import logging
import math
from dataclasses import dataclass, field

from config import settings
from core.simulation_engine import SimulationEngine
from data.candle_aggregator import Candle
from db import repository as repo

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Trade record (lightweight, no DB needed)
# ------------------------------------------------------------------

@dataclass
class BacktestTrade:
    """A single trade during backtesting."""
    index: int
    timestamp: str
    side: str           # BUY / SELL
    action: str         # OPEN_LONG, CLOSE_LONG, OPEN_SHORT, CLOSE_SHORT
    price: float
    quantity: float
    realized_pnl: float | None
    fee_usdt: float | None


# ------------------------------------------------------------------
# Backtest result
# ------------------------------------------------------------------

@dataclass
class BacktestResult:
    """Complete result of a backtest run."""
    bot_id: str
    symbol: str
    strategy_name: str
    params: dict                                # param_name → value
    candles_processed: int
    duration_seconds: float

    # Equity curve: list of {time, value, price}
    equity_curve: list[dict] = field(default_factory=list)

    # Trades
    trades: list[dict] = field(default_factory=list)

    # Metrics
    initial_balance: float = 10_000.0
    final_balance: float = 0.0
    net_pnl: float = 0.0
    return_pct: float = 0.0
    total_trades: int = 0        # all trades (open + close)
    trade_count: int = 0         # closing trades only (have realized_pnl)
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    total_fees: float = 0.0
    liquidations: int = 0
    longest_win_streak: int = 0
    longest_loss_streak: int = 0

    def to_dict(self) -> dict:
        def _safe(v):
            """Replace inf/nan with JSON-safe values."""
            if isinstance(v, float):
                if math.isinf(v):
                    return 9999.99 if v > 0 else -9999.99
                if math.isnan(v):
                    return 0.0
            return v

        return {
            "bot_id": self.bot_id,
            "symbol": self.symbol,
            "strategy_name": self.strategy_name,
            "params": self.params,
            "candles_processed": self.candles_processed,
            "duration_seconds": round(self.duration_seconds, 2),
            "initial_balance": self.initial_balance,
            "final_balance": round(self.final_balance, 2),
            "net_pnl": round(self.net_pnl, 2),
            "return_pct": round(_safe(self.return_pct), 2),
            "total_trades": self.total_trades,
            "trade_count": self.trade_count,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "win_rate": round(_safe(self.win_rate), 1),
            "avg_win": round(_safe(self.avg_win), 4),
            "avg_loss": round(_safe(self.avg_loss), 4),
            "profit_factor": round(_safe(self.profit_factor), 2),
            "sharpe_ratio": round(_safe(self.sharpe_ratio), 2),
            "max_drawdown_pct": round(_safe(self.max_drawdown_pct), 2),
            "total_fees": round(_safe(self.total_fees), 4),
            "liquidations": self.liquidations,
            "longest_win_streak": self.longest_win_streak,
            "longest_loss_streak": self.longest_loss_streak,
            "equity_curve": self.equity_curve,
            "trades": self.trades,
        }


# ------------------------------------------------------------------
# Metrics computation
# ------------------------------------------------------------------

def _compute_metrics(result: BacktestResult) -> None:
    """Compute all performance metrics from equity curve and trades."""

    # --- Basic P&L ---
    result.final_balance = result.equity_curve[-1]["value"] if result.equity_curve else result.initial_balance
    result.net_pnl = result.final_balance - result.initial_balance
    result.return_pct = (result.net_pnl / result.initial_balance) * 100

    # --- Win/loss analysis ---
    result.total_trades = len(result.trades)
    closing_trades = [t for t in result.trades if t.get("realized_pnl") is not None]
    result.trade_count = len(closing_trades)

    wins = [t["realized_pnl"] for t in closing_trades if t["realized_pnl"] > 0]
    losses = [t["realized_pnl"] for t in closing_trades if t["realized_pnl"] <= 0]

    result.win_count = len(wins)
    result.loss_count = len(losses)
    result.win_rate = (len(wins) / len(closing_trades) * 100) if closing_trades else 0

    result.avg_win = sum(wins) / len(wins) if wins else 0
    result.avg_loss = sum(losses) / len(losses) if losses else 0

    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0
    result.profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_profit > 0 else 0
    )

    # --- Streaks ---
    win_streak = 0
    loss_streak = 0
    max_win_streak = 0
    max_loss_streak = 0
    for t in closing_trades:
        if t["realized_pnl"] > 0:
            win_streak += 1
            loss_streak = 0
            max_win_streak = max(max_win_streak, win_streak)
        else:
            loss_streak += 1
            win_streak = 0
            max_loss_streak = max(max_loss_streak, loss_streak)
    result.longest_win_streak = max_win_streak
    result.longest_loss_streak = max_loss_streak

    # --- Max drawdown ---
    peak = result.initial_balance
    max_dd = 0.0
    for point in result.equity_curve:
        val = point["value"]
        if val > peak:
            peak = val
        dd = (peak - val) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    result.max_drawdown_pct = max_dd

    # --- Sharpe ratio (annualized from per-candle returns) ---
    if len(result.equity_curve) > 1:
        returns = []
        for i in range(1, len(result.equity_curve)):
            prev_val = result.equity_curve[i - 1]["value"]
            curr_val = result.equity_curve[i]["value"]
            if prev_val > 0:
                returns.append((curr_val - prev_val) / prev_val)

        if returns and len(returns) > 1:
            mean_ret = sum(returns) / len(returns)
            std_ret = math.sqrt(sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1))
            # Annualize: 1-min candles → 525,600 per year
            annualization = math.sqrt(525_600)
            result.sharpe_ratio = (mean_ret / std_ret * annualization) if std_ret > 0 else 0
        else:
            result.sharpe_ratio = 0
    else:
        result.sharpe_ratio = 0


# ------------------------------------------------------------------
# Core backtest runner
# ------------------------------------------------------------------

async def run_backtest(
    bot_id: str,
    symbol: str,
    strategy_class: type,
    params: dict | None = None,
    initial_balance: float | None = None,
    equity_interval: int = 5,
    candle_data: list | None = None,
    orderbook_data: list | None = None,
) -> BacktestResult:
    """
    Run a full backtest for one strategy on historical data.

    Args:
        bot_id: Identifier for this backtest run (e.g. "rsi_btc")
        symbol: Trading pair (e.g. "BTCUSDT")
        strategy_class: The strategy class to instantiate (already for_symbol'd)
        params: Optional param overrides {name: value}
        initial_balance: Starting USDT (defaults to settings)
        equity_interval: Record equity point every N candles (saves memory)
        candle_data: Pre-loaded candle rows (skips DB read if provided).
                     Used by the optimizer to avoid redundant DB queries.
        orderbook_data: Pre-loaded orderbook snapshot rows ordered oldest→newest.
                        If provided AND the strategy has _inject_orderbook(), each
                        candle injects the closest-in-time orderbook snapshot.
                        Used by OrderbookWallBot for backtesting/optimization.

    Returns:
        BacktestResult with metrics, equity curve, and trades.
    """
    import time
    start_time = time.monotonic()

    balance = initial_balance or settings.initial_usdt_balance

    # --- Load historical candles (from cache or DB) ---
    candle_rows = candle_data if candle_data is not None else await repo.get_historical_candles(symbol)
    if not candle_rows:
        raise ValueError(f"No historical data for {symbol}. Download it first.")

    # --- For OB-driven strategies, trim candles to orderbook data time range ---
    # No point simulating 7 days of candles when OB data covers only ~1 day.
    if orderbook_data and hasattr(strategy_class, "_inject_orderbook") and len(orderbook_data) > 0:
        from datetime import datetime, timezone as _tz
        first_ob_ts = orderbook_data[0].get("timestamp", "")
        last_ob_ts = orderbook_data[-1].get("timestamp", "")
        try:
            ob_start_ms = int(datetime.fromisoformat(first_ob_ts.replace("Z", "+00:00")).timestamp() * 1000)
            ob_end_ms = int(datetime.fromisoformat(last_ob_ts.replace("Z", "+00:00")).timestamp() * 1000)
            original_count = len(candle_rows)
            candle_rows = [r for r in candle_rows if ob_start_ms <= r["open_time"] <= ob_end_ms]
            if not candle_rows:
                raise ValueError(
                    f"No candles overlap with orderbook data range "
                    f"({first_ob_ts} → {last_ob_ts}). "
                    f"Download candles covering the OB period."
                )
            logger.info(
                f"Backtest {bot_id}: trimmed candles {original_count} → {len(candle_rows)} "
                f"to match OB data range ({len(orderbook_data)} snapshots)"
            )
        except ValueError:
            raise   # re-raise our "no overlap" error
        except Exception as e:
            logger.warning(f"Could not parse OB timestamps for trimming: {e}")

    # --- Create isolated engine + portfolio ---
    engine = SimulationEngine()

    # --- Create fresh strategy instance ---
    bot = strategy_class(engine=engine)

    # Register portfolio under the strategy's actual name (bot.name)
    # because strategies call self.engine.place_order(self.name, ...)
    engine.register_bot(bot.name, symbol, initial_usdt=balance)

    # Apply param overrides
    if params:
        try:
            bot.set_params(params)
        except ValueError as e:
            logger.warning(f"Backtest param override error: {e}")

    # Capture current params for result
    current_params = {k: v["value"] for k, v in bot.get_params().items()}

    # --- Prepare result ---
    result = BacktestResult(
        bot_id=bot_id,
        symbol=symbol,
        strategy_name=strategy_class.__name__,
        params=current_params,
        candles_processed=0,
        duration_seconds=0,
        initial_balance=balance,
    )

    # --- Tell engine to skip DB writes during backtest ---
    engine._skip_db = True

    # --- Track trades by intercepting engine ---
    trade_index = [0]  # mutable counter
    original_place_order = engine.place_order

    async def intercepting_place_order(bot_id, symbol, side, quantity, price):
        order_result = await original_place_order(bot_id, symbol, side, quantity, price)
        trade_index[0] += 1
        # Use the actual quantity from the result (engine resolves 0 → full position qty)
        actual_qty = order_result.get("quantity", quantity)
        result.trades.append({
            "index": trade_index[0],
            "timestamp": candle_rows[min(result.candles_processed, len(candle_rows) - 1)]["open_time"],
            "side": side,
            "action": order_result.get("action", side),
            "price": round(price, 2),
            "quantity": round(actual_qty, 6),
            "realized_pnl": round(order_result.get("realized_pnl", 0), 4) if order_result.get("realized_pnl") else None,
            "fee_usdt": round(order_result.get("fee_usdt", 0), 4) if order_result.get("fee_usdt") else None,
        })
        return order_result

    engine.place_order = intercepting_place_order

    # --- Sequential orderbook injection setup ---
    # Both candles and orderbook snapshots are 1-minute frequency.
    # We use a simple forward pointer — no binary search needed.
    ob_has_inject = bool(orderbook_data) and hasattr(bot, "_inject_orderbook")
    ob_index = [0]   # mutable pointer into orderbook_data

    if ob_has_inject:
        # Pre-parse timestamps to epoch_ms for fast comparison
        ob_times_ms: list[int] = []
        for ob_row in orderbook_data:
            try:
                ts = ob_row["timestamp"].replace("Z", "+00:00")
                dt = datetime.fromisoformat(ts)
                ob_times_ms.append(int(dt.timestamp() * 1000))
            except Exception:
                ob_times_ms.append(0)
        logger.info(
            f"Backtest {bot_id}: {len(orderbook_data)} orderbook snapshots "
            f"→ sequential injection into {strategy_class.__name__}"
        )
    else:
        ob_times_ms = []

    def _advance_ob_pointer(candle_open_ms: int) -> dict | None:
        """
        Advance the sequential pointer to the last snapshot at or before
        candle_open_ms. Since both series are ~1-min, this typically
        advances 0 or 1 step per candle.
        """
        if not ob_times_ms:
            return None
        while (ob_index[0] + 1 < len(ob_times_ms)
               and ob_times_ms[ob_index[0] + 1] <= candle_open_ms):
            ob_index[0] += 1
        # Return current snapshot only if it's at or before candle time
        if ob_times_ms[ob_index[0]] <= candle_open_ms:
            return orderbook_data[ob_index[0]]
        return None

    # --- Replay candles (wrapped in try/finally to always restore flag) ---
    error_count = [0]
    try:
        for i, row in enumerate(candle_rows):
            candle = Candle(
                symbol=symbol,
                interval_seconds=60,
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                open_time=row["open_time"] / 1000.0,    # convert ms → seconds
                close_time=row["close_time"] / 1000.0,
            )

            # Update engine price (for liquidation checks etc.)
            engine.update_price(symbol, candle.close)

            # Inject current orderbook snapshot before the candle fires
            if ob_has_inject:
                ob_snap = _advance_ob_pointer(row["open_time"])
                if ob_snap is not None:
                    bot._inject_orderbook(ob_snap)  # type: ignore[attr-defined]

            # Feed candle to strategy
            try:
                await bot.on_candle(candle)
            except Exception as e:
                error_count[0] += 1
                if error_count[0] <= 5:  # Log first 5 errors with traceback
                    logger.error(f"Backtest candle {i} error: {e}", exc_info=True)

            result.candles_processed = i + 1

            # Record equity point at intervals
            if i % equity_interval == 0 or i == len(candle_rows) - 1:
                state = await engine.get_portfolio_state(bot.name)
                total = state["total_value_usdt"]
                usdt = state.get("usdt_balance", total)
                result.equity_curve.append({
                    "time": row["open_time"],
                    "value": round(total, 2),
                    "usdt": round(usdt, 2),
                    "price": round(candle.close, 2),
                    "side": state.get("position_side", "NONE"),
                })

        # --- Final state ---
        final_state = await engine.get_portfolio_state(bot.name)
        result.total_fees = round(final_state.get("total_fees_paid", 0), 4)
        result.liquidations = final_state.get("liquidation_count", 0)

        if error_count[0] > 0:
            logger.warning(f"Backtest {bot_id}: {error_count[0]} candle errors occurred")
    finally:
        # --- Always restore DB writes ---
        engine._skip_db = False

    result.duration_seconds = time.monotonic() - start_time

    # --- Compute metrics ---
    _compute_metrics(result)

    logger.info(
        f"Backtest {bot_id}: {result.candles_processed} candles, "
        f"{result.trade_count} trades, "
        f"return {result.return_pct:+.2f}%, "
        f"Sharpe {result.sharpe_ratio:.2f}, "
        f"max DD {result.max_drawdown_pct:.2f}% "
        f"({result.duration_seconds:.1f}s)"
    )

    return result
