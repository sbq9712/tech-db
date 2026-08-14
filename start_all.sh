#!/usr/bin/env bash
# start_all.sh — 重启后一键恢复全部服务 + 同步最新数据
#
# 用法（在终端里执行）：
#   cd ~/tech-db-fresh && ./start_all.sh
#
# 做的事：
#   1. git pull（拉取关机期间 CI 产生的新数据）
#   2. 重建 all-records-lite.json
#   3. 增量更新 BM25 + 向量索引（只处理新记录）
#   4. 启动 Q&A 后端 (8765) + 前端 (8097)
#   5. 启动 Cloudflare 隧道（让公网页面的问答/图谱可用）
set -euo pipefail
cd "$(dirname "$0")"
VENV=".venv/bin/python"

echo "════════════════════════════════════════"
echo "  Tech-DB 一键启动 + 数据同步"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "════════════════════════════════════════"

# ── 0. 清理残留进程 ──
echo "[0/6] 清理残留进程..."
pkill -f "qa-backend/server.py" 2>/dev/null || true
pkill -f "http.server 8097" 2>/dev/null || true
pkill -f "cloudflared tunnel" 2>/dev/null || true
sleep 2

# ── 1. 同步数据 ──
echo "[1/6] 拉取最新数据（关机期间 CI 的新数据）..."
git pull --ff-only origin main 2>&1 | head -3

# ── 2. 重建 lite JSON ──
echo "[2/6] 重建 all-records-lite.json..."
$VENV scripts/rebuild_lite_from_shards.py

# ── 3. BM25 索引 ──
echo "[3/6] BM25 索引（~1 分钟）..."
$VENV qa-backend/bm25_index.py 2>&1 | tail -1

# ── 4. 向量索引（增量，只嵌入新记录）──
echo "[4/6] 向量索引增量更新..."
$VENV qa-backend/vector_index.py 2>&1 | tail -3

# ── 5. 启动服务 ──
echo "[5/6] 启动 Q&A 服务..."
nohup ./start.sh > runtime/server.log 2>&1 &
sleep 15
if curl -sf http://localhost:8765/api/health > /dev/null 2>&1; then
    echo "  ✅ Q&A 后端启动成功"
else
    echo "  ⚠️  后端启动可能失败，检查 runtime/server.log"
fi

# ── 6. Cloudflare 隧道 ──
echo "[6/6] 启动 Cloudflare 隧道..."
nohup /home/rhett/bin/cloudflared tunnel --url http://localhost:8765 > runtime/cloudflared.log 2>&1 &
sleep 8
TUNNEL_URL=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' runtime/cloudflared.log 2>/dev/null | head -1)
if [ -n "$TUNNEL_URL" ]; then
    # 检查隧道 URL 是否跟 qa.js 里的一致
    CURRENT_URL=$(grep -oP "https://[a-z0-9-]+\.trycloudflare\.com" qa.js 2>/dev/null | head -1)
    if [ "$TUNNEL_URL" != "$CURRENT_URL" ]; then
        echo ""
        echo "  ⚠️  隧道地址变了！需要更新 qa.js 并推送："
        echo "     旧: $CURRENT_URL"
        echo "     新: $TUNNEL_URL"
        echo ""
        echo "  正在自动更新..."
        sed -i "s|https://[a-z0-9-]*\.trycloudflare\.com|$TUNNEL_URL|" qa.js
        # 版本号 +1
        sed -i -E 's/qa\.js\?v=([0-9]+)/echo "qa.js?v=$(($(echo \1)+1))"/e' index.html 2>/dev/null || \
          sed -i 's/qa\.js?v=161/qa.js?v=162/' index.html
        export $(grep -v '^#' .gh_env | xargs)
        git add qa.js index.html
        git commit -m "fix: 更新隧道URL — $TUNNEL_URL" 2>/dev/null
        git push "https://sbq9712:${GH_TOKEN}@github.com/sbq9712/tech-db.git" main 2>&1 | head -3
        echo "  ✅ 已自动更新并推送，等 Pages 部署后公网问答即可用"
    else
        echo "  ✅ 隧道地址未变: $TUNNEL_URL"
    fi
else
    echo "  ⚠️  隧道 URL 获取失败，检查 runtime/cloudflared.log"
fi

echo ""
echo "════════════════════════════════════════"
echo "  全部完成！"
echo "  本地门户:  http://localhost:8097"
echo "  Q&A API:  http://localhost:8765/api/health"
echo "  公网门户:  https://sbq9712.github.io/tech-db/"
echo "════════════════════════════════════════"
