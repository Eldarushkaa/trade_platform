"""
Backtest Engine — replays historical candles through a strategy.

Creates an isolated SimulationEngine + Strategy instance (no contamination
with live data) and feeds stored candles one by one, collecting:
    - Equity curve (portfolio value at each candle)
    - All trades
    - Performance metrics (Sharpe, max drawdown, win rate, etc.)

Fill model: every order executes at the candle close price.
Cost model: fee_rate is the only cost (no slippage).

Usage:
    result = await run_backtest("rsi_btc", "BTCUSDT", RSIBot, params={...})
"""
import logging
import math
from dataclasses import dataclass, field

from core.utils import safe_round as _sr

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
            "return_pct": _sr(self.return_pct, 2),
            "total_trades": self.total_trades,
            "trade_count": self.trade_count,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "win_rate": _sr(self.win_rate, 1),
            "avg_win": _sr(self.avg_win, 4),
            "avg_loss": _sr(self.avg_loss, 4),
            "profit_factor": _sr(self.profit_factor, 2),
            "sharpe_ratio": _sr(self.sharpe_ratio, 2),
            "max_drawdown_pct": _sr(self.max_drawdown_pct, 2),
            "total_fees": _sr(self.total_fees, 4),
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
            # Annualize: 5-min candles → 105,120 per year (288 candles/day × 365)
            annualization = math.sqrt(105_120)
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
    fee_rate: float | None = None,
    equity_interval: int = 5,
    candle_data: list | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> BacktestResult:
    """
    Run a full backtest for one strategy on historical data.

    Fill model: every order executes at the candle close price.
    Cost model: fee_rate is the only cost (configurable from UI, default 0.07%).

    Args:
        bot_id: Identifier for this backtest run (e.g. "rsi_btc")
        symbol: Trading pair (e.g. "BTCUSDT")
        strategy_class: The strategy class to instantiate (already for_symbol'd)
        params: Optional param overrides {name: value}
        initial_balance: Starting USDT (defaults to settings)
        fee_rate: Fee rate override (e.g. 0.0007 = 0.07%).
                  Defaults to settings.simulation_fee_rate when None.
                  Set from UI when starting a backtest/optimization run.
        equity_interval: Record equity point every N candles (saves memory)
        candle_data: Pre-loaded candle rows (skips DB read if provided).
                     Used by the optimizer to avoid redundant DB queries.
        start_ms: Optional start filter (epoch ms) — only candles >= start_ms
        end_ms:   Optional end filter (epoch ms) — only candles <= end_ms

    Returns:
        BacktestResult with metrics, equity curve, and trades.
    """
    import time
    start_time = time.monotonic()

    balance = initial_balance or settings.initial_usdt_balance

    # --- Load historical candles (from cache or DB) ---
    if candle_data is not None:
        candle_rows = candle_data
    else:
        candle_rows = await repo.get_historical_candles(symbol, start_ms=start_ms, end_ms=end_ms)
    if not candle_rows:
        raise ValueError(f"No historical data for {symbol}. Download it first.")

    # --- Create isolated engine + portfolio ---
    # skip_db=True: this engine is ephemeral — all trades stay in memory only.
    # Previously done as engine._skip_db = True (external private flag mutation),
    # which required a try/finally restore block. Constructor param is safer.
    engine = SimulationEngine(skip_db=True)

    # --- Create fresh strategy instance ---
    bot = strategy_class(engine=engine)

    # Register portfolio under the strategy's actual name (bot.name)
    # because strategies call self.engine.place_order(self.name, ...)
    # fee_rate=None → uses settings.simulation_fee_rate (global default).
    # fee_rate=X    → overrides for this specific backtest run (from UI).
    engine.register_bot(bot.name, symbol, initial_usdt=balance, fee_rate=fee_rate)

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

    # --- Track trades by intercepting engine ---
    trade_index = [0]       # mutable counter
    # Holds the open_time of the candle currently being processed.
    # Updated each loop iteration BEFORE on_candle() fires so that any
    # trade placed inside on_candle() records the correct candle timestamp.
    # Using result.candles_processed for this was off-by-one because that
    # counter is incremented AFTER on_candle() returns.
    current_candle_open_time = [0]
    original_place_order = engine.place_order

    async def intercepting_place_order(bot_id, symbol, side, quantity, price):
        order_result = await original_place_order(bot_id, symbol, side, quantity, price)
        trade_index[0] += 1
        # Use the actual quantity from the result (engine resolves 0 → full position qty)
        actual_qty = order_result.get("quantity", quantity)
        result.trades.append({
            "index": trade_index[0],
            "timestamp": current_candle_open_time[0],
            "side": side,
            "action": order_result.get("action", side),
            "price": round(price, 2),
            "quantity": round(actual_qty, 6),
            "realized_pnl": round(order_result.get("realized_pnl", 0), 4) if order_result.get("realized_pnl") else None,
            "fee_usdt": round(order_result.get("fee_usdt", 0), 4) if order_result.get("fee_usdt") else None,
        })
        return order_result

    engine.place_order = intercepting_place_order

    # --- Replay candles ---
    error_count = [0]
    for i, row in enumerate(candle_rows):
        # Set before on_candle() so the intercepting_place_order closure
        # captures the correct timestamp for any trades placed this candle.
        current_candle_open_time[0] = row["open_time"]

        candle = Candle(
            symbol=symbol,
            interval_seconds=900,   # 15-minute candles
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

            # Capture EMA trend direction from bot if available (RSIBot with trend filter)
            # Поддерживаем два варианта:
            #   - old RSIBot (EMA50/EMA200 кросс): _ema_fast + _ema_slow оба присутствуют
            #   - new RSIBot (только EMA200): только _ema_slow, тренд = цена vs EMA200
            ema_fast = getattr(bot, "_ema_fast", None)
            ema_slow = getattr(bot, "_ema_slow", None)
            if ema_slow is None:
                trend = "warmup"
            elif ema_fast is not None:
                # EMA-кросс: bull если fast > slow
                trend = "bull" if ema_fast > ema_slow else "bear"
            else:
                # Только EMA200: bull если цена выше EMA200
                trend = "bull" if candle.close > ema_slow else "bear"

            result.equity_curve.append({
                "time": row["open_time"],
                "value": round(total, 2),
                "usdt": round(usdt, 2),
                "price": round(candle.close, 2),
                "side": state.get("position_side", "NONE"),
                "trend": trend,
            })

    # --- Final state ---
    final_state = await engine.get_portfolio_state(bot.name)
    result.total_fees = round(final_state.get("total_fees_paid", 0), 4)
    result.liquidations = final_state.get("liquidation_count", 0)

    if error_count[0] > 0:
        logger.warning(f"Backtest {bot_id}: {error_count[0]} candle errors occurred")

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
