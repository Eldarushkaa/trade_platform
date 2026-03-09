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
    binance_testnet: bool = Field(default=True, description="Use Binance testnet")

    # --- Database ---
    db_path: str = Field(default="trade_platform.db", description="Path to SQLite DB file")

    # --- Virtual Portfolio Starting Balance ---
    initial_usdt_balance: float = Field(default=10_000.0, description="Starting USDT per bot")

    # --- Server ---
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8000)

    # --- Logging ---
    log_level: str = Field(default="INFO")

    # --- Trading fees ---
    # Applied per transaction in simulation mode (mirrors Binance maker/taker fee).
    # 0.0015 = 0.15% per trade (deducted from USDT, invisible to strategies).
    simulation_fee_rate: float = Field(default=0.0015, description="Fee rate per trade (0.0015 = 0.15%)")

    # --- Portfolio snapshot interval (seconds) ---
    snapshot_interval_seconds: int = Field(default=60)


# Singleton settings instance used across the app
settings = Settings()
