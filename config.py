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

    # --- Market Type ---
    # "futures" = USDT-M perpetual futures (leverage, short positions)
    # "spot"    = legacy spot trading (LONG only, no leverage)
    market_type: Literal["futures", "spot"] = "futures"

    # --- Binance API ---
    binance_api_key: str = Field(default="", description="Binance API key")
    binance_api_secret: str = Field(default="", description="Binance API secret")
    binance_testnet: bool = Field(default=True, description="Use Binance testnet")

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
    # Binance Futures taker fee: 0.05% (we always simulate market orders).
    # This is roughly half of the spot fee (0.10%).
    # 0.0005 = 0.05% per trade (deducted from USDT, invisible to strategies).
    simulation_fee_rate: float = Field(default=0.0005, description="Fee rate per trade (0.0005 = 0.05%)")

    # --- Portfolio snapshot interval (seconds) ---
    snapshot_interval_seconds: int = Field(default=60)

    # --- LLM Agent ---
    # Set llm_enabled=True and provide llm_api_key to activate.
    # The agent calls the LLM every llm_interval_minutes to manage bot params.
    llm_enabled: bool = Field(default=False, description="Enable LLM agent (off by default)")
    llm_api_key: str = Field(default="", description="OpenAI API key")
    llm_model: str = Field(default="gpt-4o-mini", description="OpenAI model name")
    llm_interval_minutes: int = Field(default=10, description="Minutes between LLM calls")
    llm_max_actions: int = Field(default=5, description="Max actions per LLM decision (safety)")
    llm_dry_run: bool = Field(default=False, description="Log LLM decisions without applying")


# Singleton settings instance used across the app
settings = Settings()
