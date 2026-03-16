"""
Global configuration for the trading platform.
All settings are loaded from environment variables or .env file.
To switch from simulation to live trading, set TRADING_MODE=live.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Literal


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- Trading Mode ---
    # "simulation" = virtual portfolio, no real orders
    # "live"       = real Binance orders (Phase 5)
    trading_mode: Literal["simulation", "live"] = "simulation"

    # --- Binance API ---
    binance_api_key: str = Field(default="", description="Binance API key")
    binance_api_secret: str = Field(default="", description="Binance API secret")

    # --- Database ---
    db_path: str = Field(default="trade_platform.db", description="Path to SQLite DB file")

    # --- Virtual Portfolio Starting Balance ---
    initial_usdt_balance: float = Field(default=10_000.0, description="Starting USDT per bot")

    # --- Futures Settings ---
    leverage: int = Field(default=3, description="Futures leverage multiplier (3x = liquidation at ~33% move)")

    # --- Server ---
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8000)

    # --- Logging ---
    log_level: str = Field(default="INFO")

    # --- Trading fees ---
    # Binance Futures taker fee: 0.07% is a realistic estimate including
    # exchange fee (0.05%) + funding cost approximation + spread overhead.
    # All simulated orders are market orders (taker), so one rate applies.
    simulation_fee_rate: float = Field(default=0.0007, description="Fee rate for simulated market orders (0.0007 = 0.07%)")

    # --- Slippage simulation ---
    # When an orderbook snapshot is available, we walk the OB levels to compute
    # a realistic VWAP fill price (market order eats through levels).
    # In backtest mode (no OB data), the fill price equals the candle close price —
    # the fee already accounts for spread overhead (0.07% = exchange fee + spread).
    # max_slippage_pct: reject order if OB VWAP fill would exceed this % vs desired price.
    max_slippage_pct: float = Field(default=0.10, description="Max allowed slippage % (0.10 = 10 bps) — reject if exceeded")

    # --- Portfolio snapshot interval (seconds) ---
    snapshot_interval_seconds: int = Field(default=60)


# Singleton settings instance used across the app
settings = Settings()
