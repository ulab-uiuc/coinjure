---
name: pm-live-run-ops
description: Use this skill when the user asks to run live trading, enforce promotion gates, and operate real-money sessions with strict safety controls.
---

# PM Live Run Ops

Use this skill only after paper/backtest evidence is acceptable.

## Inputs

- `exchange` (`polymarket` or `kalshi`)
- `strategy_ref`
- `strategy_kwargs_json`
- credentials (`POLYMARKET_PRIVATE_KEY` or Kalshi keys)

## Workflow

1. Promotion prerequisites (must pass):

- `coinjure strategy validate --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --json`
- `coinjure strategy dry-run --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --events 10 --json`
- `coinjure research strategy-gate --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --json`

2. Launch live run:

- Polymarket:
  - `coinjure live run --exchange polymarket --wallet-private-key "$POLYMARKET_PRIVATE_KEY" --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>'`
- Kalshi:
  - `coinjure live run --exchange kalshi --kalshi-api-key-id "$KALSHI_API_KEY_ID" --kalshi-private-key-path "$KALSHI_PRIVATE_KEY_PATH" --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>'`

3. Runtime control (always available):

- `coinjure trade status --json`
- `coinjure trade pause`
- `coinjure trade resume`
- `coinjure trade stop`
- `coinjure trade killswitch --on`

## Hard Rules

- Require explicit user approval before live launch.
- If risk/behavior is unclear, pause immediately.
- Keep kill-switch path known and tested before live session.
