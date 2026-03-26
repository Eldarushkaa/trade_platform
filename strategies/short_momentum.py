"""
Short Momentum Bot — ловля пробоя вниз + ускорения импульса, SHORT only.

Работает на 5-МИНУТНЫХ СВЕЧАХ. USDT-маржинальные бессрочные фьючерсы с плечом.

Концепция (directional momentum):
    Ловим пробой минимума N последних свечей + объёмное подтверждение +
    импульсная свеча по ATR. Входим в продолжение движения, выходим по
    фиксированному TP/SL (в ATR) или по ослаблению импульса (RSI).

    Стратегия SHORT ONLY — не торгует лонги.

Тренд-фильтр (контекст):
    close < EMA_SLOW (EMA200)   — цена в медвежьем поле
    EMA_FAST < EMA_SLOW         — быстрая EMA ниже медленной
    ADX > ADX_MIN               — есть тренд, не флэт

Momentum-триггер (сигнал входа):
    1. Breakout: close < min(low[i-N : i])  — пробой N-свечного минимума
    2. Volume:   volume > EMA(volume, 20) × VOLUME_MULT
    3. ATR size: (high - low) > ATR_MULT_ENTRY × ATR  — импульсная свеча
    4. Bear close: (close - low) / (high - low) < 0.35  — закрытие в нижних 35%

Anti-flush защита:
    Если подряд ≥ MAX_IMPULSE_STREAK импульсных медвежьих свечей → вход блокируется.
    Цель: не входить в финальный flush/cascade, который часто разворачивается.

Выход (SHORT позиция):
    TP:   close ≤ entry_price - TP_ATR × entry_atr   (цель ниже)
    SL:   close ≥ entry_price + SL_ATR × entry_atr   (стоп выше)
    RSI:  rsi > RSI_EXIT                              (импульс иссяк)
    Time: candles_held ≥ MAX_HOLD_CANDLES              (time-stop)

Оптимизируемые параметры:
    BREAKOUT_LOOKBACK  5–40    Период пробоя минимума
    VOLUME_MULT        1.2–3.0 Множитель среднего объёма для подтверждения
    ATR_MULT_ENTRY     1.0–2.0 Мин. размер свечи в ATR
    ADX_MIN            18–30   Мин. ADX для входа (фильтр флэта)

Настраиваемые в UI (не оптимизируются):
    EMA_FAST_PERIOD    = 50     Быстрая EMA (тренд-контекст)
    TP_ATR             = 1.5    TP в единицах ATR от цены входа
    SL_ATR             = 0.8    SL в единицах ATR от цены входа
    RSI_EXIT           = 52     RSI выше этого → выход (импульс иссяк)
    MAX_IMPULSE_STREAK = 3      Макс. импульсных свечей подряд до блокировки входа
    MAX_HOLD_CANDLES   = 48     Time-stop (48 × 5m = 4 часа)
    TRADE_FRACTION     = 1.0    Доля баланса на сделку

Фиксированные:
    EMA_SLOW_PERIOD    = 200   EMA200 — тренд-якорь
    ATR_PERIOD         = 14    Wilder ATR
    EMA_ATR_PERIOD     = 50    EMA(ATR) — базовая волатильность
    RSI_PERIOD         = 14    Wilder RSI
    VOLUME_EMA_PERIOD  = 20    EMA объёма для сравнения
    ADX_PERIOD         = 14    Wilder ADX (DI+/DI-/ADX)
    HISTORY_MAX        = 80    Глубина буфера high/low (≥ max BREAKOUT_LOOKBACK + запас)

Fitness (compute_fitness override):
    Momentum требует хорошего Profit Factor и Return, жёсткий контроль просадки.
    Штраф если trades_per_day выходит за 0.5–5 (слишком редко или слишком часто).
    Оценка периода: 1 свеча = 5m → 288 свечей/день.
    Гарантию нормального кол-ва сделок: hard gate < 30 trades.

Warmup:
    ~250 свечей до начала торговли (EMA200 + ADX + ATR + RSI).
"""
import math
from typing import Optional, TYPE_CHECKING

from core.base_strategy import BaseStrategy
from core.simulation_engine import BaseOrderEngine

if TYPE_CHECKING:
    from data.candle_aggregator import Candle


CANDLES_PER_DAY_5M = 288  # 5-минутные свечи: 24h × 12 = 288


class ShortMomentumBot(BaseStrategy):
    name_prefix = "smom"
    name = "smom_bot"
    symbol = "ETHUSDT"

    # ------------------------------------------------------------------
    # Фиксированные периоды индикаторов
    # ------------------------------------------------------------------
    EMA_SLOW_PERIOD   = 200   # EMA200 — основной тренд-якорь
    ATR_PERIOD        = 14    # Wilder ATR
    EMA_ATR_PERIOD    = 50    # EMA(ATR) — базовый уровень волатильности
    RSI_PERIOD        = 14    # Wilder RSI
    VOLUME_EMA_PERIOD = 20    # EMA объёма
    ADX_PERIOD        = 14    # Wilder ADX
    HISTORY_MAX       = 80    # Глубина буфера high/low

    # ------------------------------------------------------------------
    # Оптимизируемые параметры (defaults — разумные стартовые значения)
    # ------------------------------------------------------------------
    BREAKOUT_LOOKBACK = 15     # N свечей для пробоя минимума
    VOLUME_MULT       = 1.5    # Объём > EMA_vol × VOLUME_MULT
    ATR_MULT_ENTRY    = 1.2    # Свеча > ATR_MULT_ENTRY × ATR
    ADX_MIN           = 22     # Мин. ADX для подтверждения тренда

    # ------------------------------------------------------------------
    # Настраиваемые в UI (optimize=False)
    # ------------------------------------------------------------------
    EMA_FAST_PERIOD    = 50    # Быстрая EMA для тренд-контекста
    TP_ATR             = 1.5   # TP = entry - TP_ATR × entry_atr
    SL_ATR             = 0.8   # SL = entry + SL_ATR × entry_atr
    RSI_EXIT           = 52    # Выход если RSI > RSI_EXIT
    MAX_IMPULSE_STREAK = 3     # Блокировка входа после N подряд импульсных свечей
    MAX_HOLD_CANDLES   = 48    # Time-stop (48 × 5m ≈ 4 ч)
    TRADE_FRACTION     = 1.0   # Доля баланса на сделку

    # ------------------------------------------------------------------
    # Fitness — SHORT momentum objectives
    # ------------------------------------------------------------------

    @classmethod
    def compute_fitness(
        cls,
        sharpe: float,
        return_pct: float,
        max_dd: float,
        trade_count: int,
        profit_factor: float = 1.0,
        total_candles: int = 0,
    ) -> float:
        """
        Momentum-oriented IS fitness для ShortMomentumBot.

        Целевые метрики:
          - Return:        35% (реальная прибыль важна)
          - Profit Factor: 35% (качество направленности)
          - Drawdown:      20% (momentum DDs бывают резкими)
          - log(trades):   10% (наличие сделок)

        Частота торговли: 0.5–5 сделок/день (5m: 288 свечей = 1 день).
        Оптимизатор всегда передаёт total_candles → частота считается точно.

        Границы gates через trades_per_day:
          tpd < 0.5  → -1000 + trade_count   (слишком редко — дисквалификация)
          tpd > 5.0  → -1000 - trade_count   (слишком часто — дисквалификация)
          0.5–5.0    → нормальная оценка
        """
        days = total_candles / CANDLES_PER_DAY_5M if total_candles > 0 else 1.0
        tpd = trade_count / days if days > 0 else 0.0

        # Hard gates через частоту
        if tpd < 0.5:
            return -1000.0 + trade_count   # слишком редко
        if tpd > 5.0:
            return -1000.0 - trade_count   # слишком часто (шум/оверфит)

        pf = min(4.0, max(0.0, profit_factor))
        r  = max(-2.0, min(2.5, return_pct / 100.0))
        dd = abs(max_dd) / 100.0

        return (
            r  * 0.35
            + pf * 0.35
            - dd * 0.20
            + math.log(max(1, trade_count)) * 0.10
        )

    # ------------------------------------------------------------------
    # PARAM_SCHEMA
    # ------------------------------------------------------------------

    PARAM_SCHEMA = {
        # --- Оптимизируемые ---
        "BREAKOUT_LOOKBACK": {
            "type": "int", "default": 15, "min": 5, "max": 40,
            "description": "Период пробоя минимума: close < min(low[i-N : i])",
        },
        "VOLUME_MULT": {
            "type": "float", "default": 1.5, "min": 1.2, "max": 3.0,
            "description": "Множитель EMA объёма для подтверждения входа",
        },
        "ATR_MULT_ENTRY": {
            "type": "float", "default": 1.2, "min": 1.0, "max": 2.0,
            "description": "Мин. размер свечи (H-L) в единицах ATR для входа",
        },
        "ADX_MIN": {
            "type": "float", "default": 22.0, "min": 18.0, "max": 30.0,
            "description": "Мин. ADX для разрешения входа (фильтр флэта)",
        },
        # --- UI параметры (optimize=False) ---
        "EMA_FAST_PERIOD": {
            "type": "int", "default": 50, "min": 20, "max": 100,
            "description": "Период быстрой EMA для тренд-контекста (EMA_FAST < EMA200)",
            "optimize": False,
        },
        "TP_ATR": {
            "type": "float", "default": 1.5, "min": 0.5, "max": 4.0,
            "description": "Take Profit в единицах ATR от цены входа (SHORT: entry - TP_ATR × atr)",
            "optimize": False,
        },
        "SL_ATR": {
            "type": "float", "default": 0.8, "min": 0.3, "max": 3.0,
            "description": "Stop Loss в единицах ATR от цены входа (SHORT: entry + SL_ATR × atr)",
            "optimize": False,
        },
        "RSI_EXIT": {
            "type": "float", "default": 52.0, "min": 40.0, "max": 70.0,
            "description": "Выход из SHORT если RSI > RSI_EXIT (импульс иссяк)",
            "optimize": False,
        },
        "MAX_IMPULSE_STREAK": {
            "type": "int", "default": 3, "min": 2, "max": 6,
            "description": "Блокировка входа после N подряд импульсных медвежьих свечей (anti-flush)",
            "optimize": False,
        },
        "MAX_HOLD_CANDLES": {
            "type": "int", "default": 48, "min": 12, "max": 200,
            "description": "Time-stop: максимум свечей в позиции (48 × 5m = 4 часа)",
            "optimize": False,
        },
        "TRADE_FRACTION": {
            "type": "float", "default": 1.0, "min": 0.10, "max": 1.0,
            "description": "Доля баланса USDT на одну сделку",
            "optimize": False,
        },
    }

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def __init__(self, engine: BaseOrderEngine) -> None:
        super().__init__(engine)

        # --- EMA_SLOW (EMA200) ---
        self._ema_slow: Optional[float] = None
        self._ema_slow_warmup: list[float] = []

        # --- EMA_FAST (динамический период из EMA_FAST_PERIOD) ---
        self._ema_fast: Optional[float] = None
        self._ema_fast_warmup: list[float] = []
        self._ema_fast_ready_count: int = 0  # счётчик для warmup

        # --- ATR + EMA(ATR) ---
        self._atr: Optional[float] = None
        self._ema_atr: Optional[float] = None
        self._warmup_tr: list[float] = []
        self._prev_close: Optional[float] = None

        # --- RSI (Wilder) ---
        self._rsi: Optional[float] = None
        self._rsi_avg_gain: Optional[float] = None
        self._rsi_avg_loss: Optional[float] = None
        self._rsi_warmup: list[float] = []  # close prices для warmup
        self._rsi_prev_close: Optional[float] = None

        # --- Volume EMA ---
        self._volume_ema: Optional[float] = None
        self._vol_warmup: list[float] = []

        # --- ADX (Wilder DI+/DI-/ADX) ---
        self._adx: Optional[float] = None
        self._di_plus: Optional[float] = None
        self._di_minus: Optional[float] = None
        self._smoothed_tr:  Optional[float] = None
        self._smoothed_dmp: Optional[float] = None  # Directional Movement +
        self._smoothed_dmm: Optional[float] = None  # Directional Movement -
        self._adx_smoothed: Optional[float] = None
        self._adx_warmup_dx: list[float] = []
        self._adx_prev_high:  Optional[float] = None
        self._adx_prev_low:   Optional[float] = None

        # --- Rolling high/low buffer для breakout ---
        self._high_buf: list[float] = []
        self._low_buf:  list[float] = []

        # --- Позиционное состояние ---
        self._entry_price: Optional[float] = None  # цена входа в SHORT
        self._entry_atr:   Optional[float] = None  # ATR на момент входа
        self._candles_held: int = 0                # свечей в позиции

        # --- Anti-flush счётчик ---
        self._impulse_streak: int = 0  # подряд импульсных медвежьих свечей

    # ------------------------------------------------------------------
    # Prewarm — прогрев индикаторов по историческим данным
    # ------------------------------------------------------------------

    def prewarm_candles(self, candles: list[dict]) -> None:
        """
        Прогоняет исторические свечи через все индикаторы без торговли.
        Вызывается BotManager перед стартом live-торговли.

        Каждый dict должен содержать: open, high, low, close, volume.
        """
        for c in candles:
            open_  = float(c["open"])
            high   = float(c["high"])
            low    = float(c["low"])
            close  = float(c["close"])
            volume = float(c.get("volume", 0.0))

            self._update_ema_slow(close)
            self._update_ema_fast(close)
            self._update_atr(high, low, close)
            self._update_rsi(close)
            self._update_volume_ema(volume)
            self._update_adx(high, low, close)
            self._append_history(high, low)

        self.logger.info(
            f"Prewarm done: {len(candles)} candles | "
            f"EMA200={'✓' if self._ema_slow else '✗'} "
            f"EMAfast={'✓' if self._ema_fast else '✗'} "
            f"ATR={'✓' if self._atr else '✗'} "
            f"RSI={'✓' if self._rsi is not None else '✗'} "
            f"ADX={'✓' if self._adx is not None else '✗'} "
            f"VolEMA={'✓' if self._volume_ema else '✗'} "
            f"history={len(self._low_buf)} bars"
        )

    # ------------------------------------------------------------------
    # Main candle handler
    # ------------------------------------------------------------------

    async def on_candle(self, candle: "Candle") -> None:
        self._candle_count += 1
        open_  = candle.open
        high   = candle.high
        low    = candle.low
        close  = candle.close
        volume = candle.volume

        # === 1. Обновить все индикаторы ===
        self._update_ema_slow(close)
        self._update_ema_fast(close)
        self._update_atr(high, low, close)
        self._update_rsi(close)
        self._update_volume_ema(volume)
        self._update_adx(high, low, close)

        # === 2. Anti-flush счётчик (до guard, нужен для истории) ===
        candle_range = high - low
        is_impulse_bear = (
            self._atr is not None
            and candle_range > self._atr
            and close < open_
        )
        if is_impulse_bear:
            self._impulse_streak += 1
        else:
            self._impulse_streak = 0

        # === 3. Warmup guard — ждём готовности всех индикаторов ===
        if not self._all_indicators_ready():
            self._append_history(high, low)
            return

        lookback = self.BREAKOUT_LOOKBACK
        if len(self._low_buf) < lookback:
            self._append_history(high, low)
            return

        # === 4. Получаем позицию ===
        position = await self.engine.get_balance(self.name, "POSITION")

        # === 5. EXIT LOGIC (до entry, независимо от фильтров) ===
        if position < 0 and self._entry_price is not None and self._entry_atr is not None:
            self._candles_held += 1

            tp_price = self._entry_price - self.TP_ATR * self._entry_atr
            sl_price = self._entry_price + self.SL_ATR * self._entry_atr

            exit_tp   = close <= tp_price
            exit_sl   = close >= sl_price
            exit_rsi  = (self._rsi is not None and self._rsi > self.RSI_EXIT)
            exit_time = self._candles_held >= self.MAX_HOLD_CANDLES

            if exit_tp or exit_sl or exit_rsi or exit_time:
                reason_parts = []
                if exit_tp:   reason_parts.append(f"TP(close={close:.2f}≤{tp_price:.2f})")
                if exit_sl:   reason_parts.append(f"SL(close={close:.2f}≥{sl_price:.2f})")
                if exit_rsi:  reason_parts.append(f"RSI({self._rsi:.1f}>{self.RSI_EXIT})")
                if exit_time: reason_parts.append(f"TIME({self._candles_held}candles)")
                reason = "exit SHORT: " + " | ".join(reason_parts)

                await self._close_position(close, "BUY", reason)
                self.logger.info(
                    f"CLOSE SHORT @ {close:.2f}  {reason}  "
                    f"entry={self._entry_price:.2f} "
                    f"atr={self._entry_atr:.4f}"
                )
                self._entry_price = None
                self._entry_atr   = None
                self._candles_held = 0
                position = 0

        elif position == 0:
            self._candles_held = 0

        # === 6. ENTRY LOGIC (только если нет позиции) ===
        if position == 0:

            # --- Тренд-фильтр ---
            trend_ok = (
                close < self._ema_slow
                and self._ema_fast < self._ema_slow
                and self._adx > self.ADX_MIN
            )

            if trend_ok:
                # --- Momentum-триггер ---
                low_N = min(self._low_buf[-lookback:])

                breakout_ok  = close < low_N
                volume_ok    = (self._volume_ema > 0 and volume > self._volume_ema * self.VOLUME_MULT)
                atr_size_ok  = (self._atr is not None and candle_range > self.ATR_MULT_ENTRY * self._atr)
                bear_close_ok = (
                    (close - low) / candle_range < 0.35
                    if candle_range > 1e-9 else False
                )

                signal = breakout_ok and volume_ok and atr_size_ok and bear_close_ok

                # --- Anti-flush защита ---
                flush_guard = self._impulse_streak >= self.MAX_IMPULSE_STREAK

                if signal and not flush_guard:
                    result = await self._open_position(
                        close, "SELL",
                        low_N=f"{low_N:.2f}",
                        adx=f"{self._adx:.1f}",
                        vol_ratio=f"{volume / self._volume_ema:.2f}" if self._volume_ema > 0 else "n/a",
                        atr_mult=f"{candle_range / self._atr:.2f}" if self._atr else "n/a",
                        streak=str(self._impulse_streak),
                    )
                    if result is not None:
                        self._entry_price  = close
                        self._entry_atr    = self._atr
                        self._candles_held = 0
                        self.logger.info(
                            f"OPEN SHORT @ {close:.2f}  "
                            f"low_N={low_N:.2f}  ADX={self._adx:.1f}  "
                            f"vol×{volume / self._volume_ema:.2f}  "
                            f"atr×{candle_range / self._atr:.2f}  "
                            f"streak={self._impulse_streak}  "
                            f"TP={close - self.TP_ATR * self._atr:.2f}  "
                            f"SL={close + self.SL_ATR * self._atr:.2f}"
                        )

        # === 7. Append в историю (всегда в конце) ===
        self._append_history(high, low)

    # ------------------------------------------------------------------
    # Warmup check
    # ------------------------------------------------------------------

    def _all_indicators_ready(self) -> bool:
        return (
            self._ema_slow is not None
            and self._ema_fast is not None
            and self._atr is not None
            and self._ema_atr is not None
            and self._rsi is not None
            and self._volume_ema is not None
            and self._adx is not None
        )

    # ------------------------------------------------------------------
    # EMA_SLOW (EMA200)
    # ------------------------------------------------------------------

    def _update_ema_slow(self, close: float) -> None:
        """EMA200: SMA первых 200 свечей → стандартная EMA (k = 2/201)."""
        self._ema_slow_warmup.append(close)
        n = len(self._ema_slow_warmup)
        k = 2.0 / (self.EMA_SLOW_PERIOD + 1)

        if self._ema_slow is None:
            if n >= self.EMA_SLOW_PERIOD:
                self._ema_slow = sum(self._ema_slow_warmup[:self.EMA_SLOW_PERIOD]) / self.EMA_SLOW_PERIOD
                self._ema_slow_warmup.clear()
                self.logger.info(f"EMA{self.EMA_SLOW_PERIOD}={self._ema_slow:.4f} готов")
        else:
            self._ema_slow = close * k + self._ema_slow * (1 - k)

    # ------------------------------------------------------------------
    # EMA_FAST (период из EMA_FAST_PERIOD)
    # ------------------------------------------------------------------

    def _update_ema_fast(self, close: float) -> None:
        """Быстрая EMA с периодом EMA_FAST_PERIOD. SMA init."""
        period = self.EMA_FAST_PERIOD
        k = 2.0 / (period + 1)

        if self._ema_fast is None:
            self._ema_fast_warmup.append(close)
            if len(self._ema_fast_warmup) >= period:
                self._ema_fast = sum(self._ema_fast_warmup[:period]) / period
                self._ema_fast_warmup.clear()
        else:
            self._ema_fast = close * k + self._ema_fast * (1 - k)

    # ------------------------------------------------------------------
    # ATR (Wilder) + EMA(ATR)
    # ------------------------------------------------------------------

    def _update_atr(self, high: float, low: float, close: float) -> None:
        """
        True Range = max(H-L, |H-Prev|, |L-Prev|)
        ATR: Wilder smoothing (alpha = 1/ATR_PERIOD)
        EMA(ATR): standard EMA(ATR, EMA_ATR_PERIOD)
        """
        prev = self._prev_close if self._prev_close is not None else close
        tr = max(high - low, abs(high - prev), abs(low - prev))
        self._prev_close = close

        if self._atr is None:
            self._warmup_tr.append(tr)
            if len(self._warmup_tr) >= self.ATR_PERIOD:
                self._atr     = sum(self._warmup_tr) / self.ATR_PERIOD
                self._ema_atr = self._atr
                self._warmup_tr.clear()
        else:
            alpha = 1.0 / self.ATR_PERIOD
            self._atr = alpha * tr + (1 - alpha) * self._atr
            k = 2.0 / (self.EMA_ATR_PERIOD + 1)
            self._ema_atr = self._atr * k + self._ema_atr * (1 - k)

    # ------------------------------------------------------------------
    # RSI (Wilder)
    # ------------------------------------------------------------------

    def _update_rsi(self, close: float) -> None:
        """
        Wilder RSI: первые RSI_PERIOD свечей → SMA gain/loss init,
        затем Wilder smoothing.
        """
        if self._rsi_prev_close is None:
            self._rsi_prev_close = close
            return

        delta = close - self._rsi_prev_close
        self._rsi_prev_close = close
        gain = max(0.0, delta)
        loss = max(0.0, -delta)

        if self._rsi_avg_gain is None:
            self._rsi_warmup.append((gain, loss))
            if len(self._rsi_warmup) >= self.RSI_PERIOD:
                avg_g = sum(x[0] for x in self._rsi_warmup) / self.RSI_PERIOD
                avg_l = sum(x[1] for x in self._rsi_warmup) / self.RSI_PERIOD
                self._rsi_avg_gain = avg_g
                self._rsi_avg_loss = avg_l
                rs = avg_g / avg_l if avg_l > 0 else float("inf")
                self._rsi = 100.0 - (100.0 / (1.0 + rs))
                self._rsi_warmup.clear()
        else:
            alpha = 1.0 / self.RSI_PERIOD
            self._rsi_avg_gain = alpha * gain + (1 - alpha) * self._rsi_avg_gain
            self._rsi_avg_loss = alpha * loss + (1 - alpha) * self._rsi_avg_loss
            rs = self._rsi_avg_gain / self._rsi_avg_loss if self._rsi_avg_loss > 0 else float("inf")
            self._rsi = 100.0 - (100.0 / (1.0 + rs))

    # ------------------------------------------------------------------
    # Volume EMA
    # ------------------------------------------------------------------

    def _update_volume_ema(self, volume: float) -> None:
        """EMA объёма: SMA init → standard EMA."""
        period = self.VOLUME_EMA_PERIOD
        k = 2.0 / (period + 1)

        if self._volume_ema is None:
            self._vol_warmup.append(volume)
            if len(self._vol_warmup) >= period:
                self._volume_ema = sum(self._vol_warmup) / period
                self._vol_warmup.clear()
        else:
            self._volume_ema = volume * k + self._volume_ema * (1 - k)

    # ------------------------------------------------------------------
    # ADX (Wilder DI+/DI-/ADX)
    # ------------------------------------------------------------------

    def _update_adx(self, high: float, low: float, close: float) -> None:
        """
        Полный Wilder ADX:
          DM+ = max(high - prev_high, 0) если > max(prev_low - low, 0) иначе 0
          DM- = max(prev_low - low, 0) если > max(high - prev_high, 0) иначе 0
          TR  = max(H-L, |H-Prev_close|, |L-Prev_close|)
          Первые ADX_PERIOD свечей → sum init (Wilder первичный расчёт)
          ADX = Wilder EMA первых ADX_PERIOD значений DX
        """
        if self._adx_prev_high is None:
            self._adx_prev_high = high
            self._adx_prev_low  = low
            return

        prev_high = self._adx_prev_high
        prev_low  = self._adx_prev_low
        prev_close = self._prev_close if self._prev_close is not None else close

        # True Range
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))

        # Directional Movement
        up_move   = high - prev_high
        down_move = prev_low - low

        dmp = up_move   if (up_move   > down_move and up_move   > 0) else 0.0
        dmm = down_move if (down_move > up_move   and down_move > 0) else 0.0

        self._adx_prev_high = high
        self._adx_prev_low  = low

        period = self.ADX_PERIOD

        if self._smoothed_tr is None:
            # Накапливаем сырые значения для первого Wilder-суммирования
            self._adx_warmup_dx.append((tr, dmp, dmm))
            if len(self._adx_warmup_dx) >= period:
                sm_tr  = sum(x[0] for x in self._adx_warmup_dx)
                sm_dmp = sum(x[1] for x in self._adx_warmup_dx)
                sm_dmm = sum(x[2] for x in self._adx_warmup_dx)
                self._smoothed_tr  = sm_tr
                self._smoothed_dmp = sm_dmp
                self._smoothed_dmm = sm_dmm

                di_plus  = 100.0 * sm_dmp / sm_tr if sm_tr > 0 else 0.0
                di_minus = 100.0 * sm_dmm / sm_tr if sm_tr > 0 else 0.0
                self._di_plus  = di_plus
                self._di_minus = di_minus

                di_sum  = di_plus + di_minus
                dx = 100.0 * abs(di_plus - di_minus) / di_sum if di_sum > 0 else 0.0
                self._adx_warmup_dx.clear()
                self._adx_warmup_dx.append(dx)
        else:
            # Wilder smoothing
            self._smoothed_tr  = self._smoothed_tr  - (self._smoothed_tr  / period) + tr
            self._smoothed_dmp = self._smoothed_dmp - (self._smoothed_dmp / period) + dmp
            self._smoothed_dmm = self._smoothed_dmm - (self._smoothed_dmm / period) + dmm

            di_plus  = 100.0 * self._smoothed_dmp / self._smoothed_tr if self._smoothed_tr > 0 else 0.0
            di_minus = 100.0 * self._smoothed_dmm / self._smoothed_tr if self._smoothed_tr > 0 else 0.0
            self._di_plus  = di_plus
            self._di_minus = di_minus

            di_sum = di_plus + di_minus
            dx = 100.0 * abs(di_plus - di_minus) / di_sum if di_sum > 0 else 0.0

            if self._adx is None:
                # Накапливаем DX до ADX_PERIOD значений → SMA init для ADX
                self._adx_warmup_dx.append(dx)
                if len(self._adx_warmup_dx) >= period:
                    self._adx = sum(self._adx_warmup_dx) / period
                    self._adx_warmup_dx.clear()
            else:
                # Wilder smoothing ADX
                self._adx = ((self._adx * (period - 1)) + dx) / period

    # ------------------------------------------------------------------
    # Rolling high/low buffer
    # ------------------------------------------------------------------

    def _append_history(self, high: float, low: float) -> None:
        self._high_buf.append(high)
        self._low_buf.append(low)
        if len(self._high_buf) > self.HISTORY_MAX:
            self._high_buf = self._high_buf[-self.HISTORY_MAX:]
            self._low_buf  = self._low_buf[-self.HISTORY_MAX:]
