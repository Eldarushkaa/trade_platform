# Bot Parameter Editing & LLM Agent Integration

## Overview

Two-phase feature: (1) Allow editing bot strategy parameters from the dashboard, (2) Let an LLM periodically manage those parameters and start/stop bots.

---

## Phase 1: Dashboard Parameter Editing

### Strategy Parameters to Expose

**RSI Bot:**
| Parameter | Type | Default | Min | Max | Description |
|---|---|---|---|---|---|
| RSI_PERIOD | int | 10 | 3 | 50 | RSI lookback window |
| OVERSOLD | float | 30.0 | 5.0 | 45.0 | Long entry threshold |
| OVERBOUGHT | float | 70.0 | 55.0 | 95.0 | Short entry threshold |
| TRADE_FRACTION | float | 0.80 | 0.10 | 1.0 | % of free USDT per trade |
| COOLDOWN_CANDLES | int | 3 | 0 | 30 | Min candles between trades |

**MA Crossover Bot:**
| Parameter | Type | Default | Min | Max | Description |
|---|---|---|---|---|---|
| FAST_PERIOD | int | 12 | 3 | 50 | Fast EMA period |
| SLOW_PERIOD | int | 26 | 10 | 100 | Slow EMA period |
| SIGNAL_PERIOD | int | 9 | 3 | 30 | Signal line smoothing |
| TRADE_FRACTION | float | 0.80 | 0.10 | 1.0 | % of free USDT per trade |

**Bollinger Bot:**
| Parameter | Type | Default | Min | Max | Description |
|---|---|---|---|---|---|
| BB_PERIOD | int | 20 | 5 | 50 | Bollinger lookback |
| BB_STD_DEV | float | 2.0 | 0.5 | 4.0 | Band width multiplier |
| TRADE_FRACTION | float | 0.80 | 0.10 | 1.0 | % of free USDT per trade |
| MIN_BANDWIDTH | float | 0.0005 | 0.0 | 0.01 | Squeeze filter threshold |
| COOLDOWN_CANDLES | int | 3 | 0 | 30 | Min candles between trades |
| STOP_LOSS_PCT | float | 0.01 | 0.001 | 0.05 | Stop-loss % from entry |

### Backend Changes

1. **BaseStrategy** — Add `PARAM_SCHEMA` class dict + `get_params()` / `set_params()` methods
2. **Each strategy** — Define `PARAM_SCHEMA` with metadata per tunable parameter  
3. **DB** — New `bot_params` table: `bot_id TEXT PK, params_json TEXT, updated_at TIMESTAMP`
4. **Repository** — `get_bot_params(bot_id)` and `save_bot_params(bot_id, params_dict)`
5. **BotManager** — On registration, load saved params from DB and apply to instance
6. **API** — `GET /api/bots/{name}/params` returns schema + current values; `PUT /api/bots/{name}/params` validates and applies changes

### Dashboard Changes

- Collapsible Settings panel below the stats grid for the selected bot
- Input fields generated dynamically from the param schema (with type, min, max)
- Save button → PUT to API → success/error toast notification
- Changes apply immediately (no restart needed) — next candle uses new values

### PARAM_SCHEMA Format

```python
PARAM_SCHEMA = {
    "RSI_PERIOD": {
        "type": "int",
        "default": 10,
        "min": 3,
        "max": 50,
        "description": "RSI lookback window"
    },
    "OVERSOLD": {
        "type": "float",
        "default": 30.0,
        "min": 5.0,
        "max": 45.0,
        "description": "Long entry threshold"
    },
}
```

### API Response Format

**GET /api/bots/{name}/params:**
```json
{
  "bot_id": "rsi_btc",
  "strategy": "RSIBot",
  "params": {
    "RSI_PERIOD": {"value": 10, "type": "int", "default": 10, "min": 3, "max": 50, "description": "RSI lookback window"},
    "OVERSOLD": {"value": 30.0, "type": "float", "default": 30.0, "min": 5.0, "max": 45.0, "description": "Long entry threshold"}
  }
}
```

**PUT /api/bots/{name}/params:**
```json
{
  "RSI_PERIOD": 14,
  "OVERSOLD": 25.0
}
```
→ Returns updated params or 422 with validation errors.

---

## Phase 2: LLM Agent Integration

### Architecture

```
┌─────────────────────────────────────────────────┐
│ core/llm_agent.py                               │
│                                                 │
│  Periodic asyncio task (every N minutes)        │
│  1. Collect all bot states, params, recent P&L  │
│  2. Build prompt with system instructions        │
│  3. Call LLM API (OpenAI / Anthropic / local)   │
│  4. Parse structured JSON response              │
│  5. Apply param changes via BotManager          │
│  6. Start/stop bots via BotManager              │
│  7. Log decision to DB                          │
└─────────────────────────────────────────────────┘
```

### Config additions (config.py)

- `llm_enabled: bool = False`
- `llm_provider: str = "openai"` (openai, anthropic, or custom URL)
- `llm_api_key: str = ""`
- `llm_model: str = "gpt-4o-mini"`
- `llm_interval_minutes: int = 10`
- `llm_max_tokens: int = 2000`

### LLM Request/Response

**System prompt** includes:
- Platform description (futures, 3x leverage, 9 bots)
- Current state of all bots (running/stopped, current P&L, position, params)
- Recent trade history (last 10 trades per bot)
- Available actions: change params, start/stop bots
- Constraints: parameter min/max bounds

**Expected response format:**
```json
{
  "reasoning": "BTC RSI bot has been losing; widen thresholds to reduce trades...",
  "actions": [
    {"type": "set_params", "bot_id": "rsi_btc", "params": {"OVERSOLD": 25.0, "OVERBOUGHT": 75.0}},
    {"type": "stop_bot", "bot_id": "bb_sol"},
    {"type": "start_bot", "bot_id": "bb_sol"}
  ]
}
```

### LLM Decision Log

- Stored in `llm_decisions` DB table
- Fields: `id, timestamp, prompt_summary, response_json, actions_taken, success`
- API endpoint: `GET /api/llm/log` — returns last N decisions
- Dashboard panel shows: last decision reasoning, next run countdown, enable/disable toggle

### Safety Guardrails

- All param changes validated against PARAM_SCHEMA bounds
- LLM cannot set balance or modify DB directly
- Maximum N actions per decision (prevent runaway changes)
- Dry-run mode: log what LLM would do without applying
- Manual override: user changes on dashboard always take priority

---

## File Changes Summary

### Phase 1 (Parameter Editing)
| File | Change |
|---|---|
| `core/base_strategy.py` | Add `PARAM_SCHEMA`, `get_params()`, `set_params()` |
| `strategies/example_rsi_bot.py` | Add `PARAM_SCHEMA` dict |
| `strategies/example_ma_crossover.py` | Add `PARAM_SCHEMA` dict |
| `strategies/bollinger_bot.py` | Add `PARAM_SCHEMA` dict |
| `db/database.py` | Add `bot_params` table creation |
| `db/repository.py` | Add `get_bot_params()`, `save_bot_params()` |
| `core/bot_manager.py` | Load saved params on registration |
| `api/routes/bots.py` | Add GET/PUT `/params` endpoints |
| `api/static/index.html` | Add settings panel UI |

### Phase 2 (LLM Agent)
| File | Change |
|---|---|
| `config.py` | Add LLM settings |
| `core/llm_agent.py` | New file — agent logic |
| `db/database.py` | Add `llm_decisions` table |
| `db/repository.py` | Add LLM log CRUD |
| `api/routes/llm.py` | New file — LLM status/log endpoints |
| `main.py` | Wire LLM agent into lifespan |
| `api/static/index.html` | Add LLM status panel |
