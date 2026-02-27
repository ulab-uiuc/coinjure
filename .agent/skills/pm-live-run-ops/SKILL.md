---
name: pm-live-run-ops
description: Use this skill when asked to run live trading with explicit approval and strict promotion/safety controls.
---

# PM Live Run Ops

Use this skill only after acceptable backtest and paper evidence.

## Inputs

- `exchange` (`polymarket` or `kalshi`)
- `strategy_ref`
- optional `strategy_kwargs_json`
- credentials (`POLYMARKET_PRIVATE_KEY` or Kalshi keypair)

## Workflow

1. Promotion prerequisites (must pass):

- `coinjure strategy validate --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --dry-run --events 10 --json`
- `coinjure research strategy-gate --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --json`
- verify latest paper run artifacts are acceptable.

2. Get explicit user approval for live launch.

3. Launch live run:

- Polymarket:
- `coinjure live run --exchange polymarket --wallet-private-key "$POLYMARKET_PRIVATE_KEY" --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>'`
- Kalshi:
- `coinjure live run --exchange kalshi --kalshi-api-key-id "$KALSHI_API_KEY_ID" --kalshi-private-key-path "$KALSHI_PRIVATE_KEY_PATH" --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>'`

4. Runtime control:

- `coinjure trade status --json`
- `coinjure trade state --json`
- `coinjure trade pause`
- `coinjure trade resume`
- `coinjure trade stop`
- `coinjure trade killswitch --on`

## Hard Rules

- If risk or behavior is unclear, pause immediately.
- Keep kill-switch path tested before live session.
- Do not bypass user consent for live deployment.
