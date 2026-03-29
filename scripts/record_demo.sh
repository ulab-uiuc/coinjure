#!/bin/bash
# ============================================================
# Coinjure Demo Recording Script
#
# Layout: iTerm2 左右分屏
#   左: Claude Code 终端 (discover → paper-run)
#   右: coinjure engine monitor (TUI 仪表盘)
#
# 使用方法:
#   1. 打开 iTerm2
#   2. Cmd+Shift+5 开始录屏
#   3. 运行: bash scripts/record_demo.sh
#   4. 等脚本跑完后 Cmd+Shift+5 停止录屏
# ============================================================

set -e
cd "$(dirname "$0")/.."

# 颜色
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m'

RELATION_ID="excl-559652-559653-559654-+41"
DURATION=120

echo -e "${CYAN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   Coinjure Demo - Prediction Market Trading  ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════╝${NC}"
echo ""

# ── Step 0: 确保 hub 没在跑 ──
echo -e "${YELLOW}[0/5] Cleaning up...${NC}"
coinjure hub stop 2>/dev/null || true
sleep 1

# ── Step 1: 启动 Hub ──
echo -e "${GREEN}[1/5] Starting Market Data Hub...${NC}"
coinjure hub start --detach --poly-interval 15 --kalshi-interval 30
echo "  Hub started. Waiting 15s for initial data..."
sleep 15
coinjure hub status

# ── Step 2: Discover markets ──
echo ""
echo -e "${GREEN}[2/5] Discovering market relations...${NC}"
echo -e "  ${CYAN}> coinjure market discover --exchange polymarket --limit 100${NC}"
coinjure market discover --exchange polymarket --limit 100 2>&1 | tail -5
echo ""

# ── Step 3: List relations ──
echo -e "${GREEN}[3/5] Available relations:${NC}"
echo -e "  ${CYAN}> coinjure market relations list${NC}"
coinjure market relations list 2>&1 | head -20
echo "  ..."
echo ""

# ── Step 4: Run paper trading ──
echo -e "${GREEN}[4/5] Starting paper trading for ${DURATION}s...${NC}"
echo -e "  ${CYAN}> coinjure engine paper-run --exchange polymarket --duration ${DURATION}${NC}"
echo -e "  ${CYAN}  --strategy-ref group_arb_strategy.py:GroupArbStrategy${NC}"
echo -e "  ${CYAN}  --relation ${RELATION_ID}${NC}"
echo ""
echo -e "${YELLOW}  TIP: Open another terminal and run 'coinjure engine monitor'${NC}"
echo ""

coinjure engine paper-run \
  --exchange polymarket \
  --duration "$DURATION" \
  --initial-capital 5000 \
  --strategy-ref "coinjure/strategy/builtin/group_arb_strategy.py:GroupArbStrategy" \
  --strategy-kwargs-json "{\"relation_id\": \"${RELATION_ID}\", \"trade_size\": 20, \"min_edge\": 0.005, \"min_markets\": 3}"

# ── Step 5: Cleanup ──
echo ""
echo -e "${GREEN}[5/5] Stopping hub...${NC}"
coinjure hub stop 2>/dev/null || true

echo ""
echo -e "${CYAN}╔══════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   Demo complete! Stop recording now.  ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════╝${NC}"
