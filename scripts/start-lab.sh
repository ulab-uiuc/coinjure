#!/usr/bin/env bash
# start-lab.sh — Launch the two-pane Coinjure Strategy Lab tmux session.
#
# Usage:
#   ./scripts/start-lab.sh [exchange] [initial-strategy-ref]
#
# Defaults:
#   exchange      = polymarket
#   strategy-ref  = coinjure.strategy.simple_strategy:SimpleStrategy
#
# Layout:
#   ┌──────────────────────────────────────────────────────────────┐
#   │  Left (40%) — Claude Code agent   │ Right (60%) — Engine TUI │
#   │  `claude`                         │ `coinjure paper run ...` │
#   └──────────────────────────────────────────────────────────────┘

set -euo pipefail

SESSION="coinjure-lab"
EXCHANGE="${1:-polymarket}"
STRATEGY_REF="${2:-coinjure.strategy.simple_strategy:SimpleStrategy}"

# Kill any stale session from a previous run
tmux kill-session -t "$SESSION" 2>/dev/null || true

# Create a new session (detached) with generous dimensions.
# The right pane is created first so it becomes pane 0 and gets the larger share.
tmux new-session -d -s "$SESSION" -x 220 -y 50

# Right pane (pane 0): paper trading engine + Textual monitor
tmux send-keys -t "$SESSION" \
  "coinjure paper run --exchange $EXCHANGE --strategy-ref $STRATEGY_REF --monitor" Enter

# Left pane: split the window, giving 40% of width to the left side
tmux split-window -h -p 40 -t "$SESSION"

# Left pane (pane 1): Claude Code agent
tmux send-keys -t "$SESSION" "claude" Enter

# Focus the left pane so the user lands in Claude Code on attach
tmux select-pane -t "$SESSION:0.1"

echo "Attaching to tmux session '$SESSION' ..."
echo "  Right pane: coinjure paper run (exchange=$EXCHANGE)"
echo "  Left pane:  claude (strategy research agent)"
echo ""
echo "Useful commands once attached:"
echo "  coinjure trade status --json        # check engine health"
echo "  coinjure trade get-state --json     # full snapshot"
echo "  coinjure trade swap-strategy \\      # hot-swap strategy"
echo "    --strategy-ref strategies/X.py:X --json"
echo ""

tmux attach-session -t "$SESSION"
