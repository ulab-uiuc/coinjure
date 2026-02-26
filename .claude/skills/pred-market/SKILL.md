---
name: pred-market
description: Control the prediction market trading system. Use when the user asks to start/stop trading, check positions, run backtests, or manage strategies on Polymarket or Kalshi.
allowed-tools: Bash, Read, Grep, Glob
---

# Prediction Market CLI - Trading System Control

## Setup

```bash
cd /private/tmp/pred-market-cli
```

All commands must be prefixed with `poetry run` unless the package is installed globally.

## Environment Variables

Before trading, ensure these are set:

```bash
# LLM Strategy (required for SimpleStrategy)
export DEEPSEEK_API_KEY='<your-deepseek-key>'
# OR
export OPENAI_API_KEY='<your-openai-key>'

# Kalshi (required for Kalshi live trading)
export KALSHI_API_KEY_ID='<your-kalshi-api-key-id>'
export KALSHI_PRIVATE_KEY_PATH='<path-to-kalshi-private-key.pem>'

# Polymarket (pass via --wallet-private-key or env)
export POLYMARKET_PRIVATE_KEY='<your-polymarket-private-key>'
```

## Core Commands

### Live Trading (Real Orders)

```bash
# Kalshi live trading with LLM strategy + TUI dashboard
poetry run pred-market-cli live run --exchange kalshi --strategy-ref pred_market_cli.strategy.simple_strategy:SimpleStrategy -m

# Polymarket live trading with LLM strategy + TUI dashboard
poetry run pred-market-cli live run --exchange polymarket --wallet-private-key $POLYMARKET_PRIVATE_KEY --strategy-ref pred_market_cli.strategy.simple_strategy:SimpleStrategy -m

# Without dashboard (background-friendly)
poetry run pred-market-cli live run --exchange kalshi --strategy-ref pred_market_cli.strategy.simple_strategy:SimpleStrategy

# With duration limit (seconds)
poetry run pred-market-cli live run --exchange kalshi --strategy-ref pred_market_cli.strategy.simple_strategy:SimpleStrategy --duration 3600
```

### Paper Trading (Simulated)

```bash
# Paper trade on Polymarket data
poetry run pred-market-cli paper run --exchange polymarket --strategy-ref pred_market_cli.strategy.simple_strategy:SimpleStrategy -m

# Paper trade on Kalshi data
poetry run pred-market-cli paper run --exchange kalshi --strategy-ref pred_market_cli.strategy.simple_strategy:SimpleStrategy -m

# Paper trade on RSS news
poetry run pred-market-cli paper run --exchange rss --strategy-ref pred_market_cli.strategy.simple_strategy:SimpleStrategy -m

# Custom initial capital
poetry run pred-market-cli paper run --exchange polymarket --initial-capital 50000 -m
```

### Monitor (Connect to Running Engine)

```bash
# Live TUI dashboard (connects via Unix socket)
poetry run pred-market-cli monitor --watch

# One-shot snapshot
poetry run pred-market-cli monitor
```

### Trade Control (While Engine is Running)

```bash
poetry run pred-market-cli trade status    # Check engine state
poetry run pred-market-cli trade pause     # Pause trading
poetry run pred-market-cli trade resume    # Resume trading
poetry run pred-market-cli trade stop      # Stop engine gracefully
```

### Strategy Management

```bash
# Create a new strategy template
poetry run pred-market-cli strategy create --output my_strategy.py --class-name MyStrategy

# Validate a strategy is loadable
poetry run pred-market-cli strategy validate --strategy-ref path/to/file.py:ClassName --json
```

### Backtest

```bash
poetry run pred-market-cli backtest run \
  --history-file data/history.jsonl \
  --market-id M1 --event-id E1 \
  --strategy-ref pred_market_cli.strategy.simple_strategy:SimpleStrategy
```

## Available Strategies

| Strategy | Reference | Description |
|----------|-----------|-------------|
| SimpleStrategy (LLM) | `pred_market_cli.strategy.simple_strategy:SimpleStrategy` | Uses DeepSeek/OpenAI to estimate event probabilities, trades when edge > 10% |
| TestStrategy | `pred_market_cli.strategy.test_strategy:TestStrategy` | Simple price momentum (buy on up, sell on down). No LLM. |
| Custom | `path/to/file.py:ClassName` | Any class extending `Strategy` |

## The `-m` Flag

Adding `-m` or `--monitor` to `paper run` or `live run` launches the Textual TUI dashboard inline. It shows:
- Portfolio value, cash, P&L
- Active positions with real-time prices
- LLM probability vs market price comparison
- Recent orders and fills
- Event stream

Without `-m`, the engine runs in headless mode (suitable for background/cron).

## Running in Background

```bash
# Start in background, redirect output to log
nohup poetry run pred-market-cli live run --exchange kalshi \
  --strategy-ref pred_market_cli.strategy.simple_strategy:SimpleStrategy \
  > trading.log 2>&1 &

# Check status from another terminal
poetry run pred-market-cli trade status

# View log
tail -f trading.log

# Stop
poetry run pred-market-cli trade stop
```

## Autonomous Operation Checklist

When asked to start trading autonomously:

1. `cd /private/tmp/pred-market-cli`
2. Verify env vars are set: `echo $KALSHI_API_KEY_ID` / `echo $DEEPSEEK_API_KEY`
3. Start the engine with appropriate command
4. If running with `-m`, the TUI will show real-time status
5. If running headless, use `trade status` to check periodically
6. Use `trade stop` to shut down gracefully

## Troubleshooting

- **401 Unauthorized (Kalshi)**: Check `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` are set correctly
- **No LLM analysis**: Ensure `DEEPSEEK_API_KEY` or `OPENAI_API_KEY` is set
- **0 USDC (Polymarket)**: Wallet needs funding before trades can execute
- **poetry not found**: Run from project directory `/private/tmp/pred-market-cli`
- **Module not found**: Run `poetry install` first
