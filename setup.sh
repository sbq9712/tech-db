#!/bin/bash
# ============================================================
#  Tech-DB 环境搭建脚本
#  在新机器上一键部署：依赖 + 模型 + 索引 + 配置
# ============================================================
set -e

echo "============================================================"
echo "  Tech-DB 环境搭建"
echo "============================================================"

# ── 0. 检查 Python ──
if ! command -v python3 &> /dev/null; then
    echo "❌ 未找到 python3，请先安装 Python 3.11+"
    exit 1
fi
echo "✅ Python: $(python3 --version)"

# ── 1. 创建虚拟环境 ──
if [ ! -d ".venv" ]; then
    echo "📦 创建虚拟环境..."
    python3 -m venv .venv
fi
source .venv/bin/activate

# ── 2. 安装依赖 ──
echo "📦 安装 Python 依赖..."
pip install -r requirements.txt -q

# ── 3. 下载 bge-m3 模型（2.2GB，约 10-30 分钟取决于网速）──
MODEL_DIR="bge-m3-model"
if [ ! -d "$MODEL_DIR" ] || [ ! -f "$MODEL_DIR/pytorch_model.bin" ]; then
    echo "📦 下载 bge-m3 模型（2.2GB）..."
    python3 -c "
from huggingface_hub import snapshot_download
snapshot_download('BAAI/bge-m3', local_dir='$MODEL_DIR')
print('模型下载完成')
"
else
    echo "✅ bge-m3 模型已存在"
fi

# ── 4. 解压索引文件 ──
echo "📦 解压索引..."
if [ -f "data/lightrag/vector_index_v2.pkl.gz" ] && [ ! -f "data/lightrag/vector_index_v2.pkl" ]; then
    gunzip -k data/lightrag/vector_index_v2.pkl.gz
    echo "  ✅ 向量索引已解压"
else
    echo "  ⏭️ 向量索引已存在或压缩包不存在"
fi
if [ -f "data/lightrag/bm25_index.pkl.gz" ] && [ ! -f "data/lightrag/bm25_index.pkl" ]; then
    gunzip -k data/lightrag/bm25_index.pkl.gz
    echo "  ✅ BM25索引已解压"
else
    echo "  ⏭️ BM25索引已存在或压缩包不存在"
fi

# ── 5. 配置 API 密钥 ──
ENV_FILE="$HOME/.config/anthropic-proxy.env"
if [ ! -f "$ENV_FILE" ]; then
    echo ""
    echo "🔑 需要配置 API 密钥"
    echo "   请在 https://z.ai 注册获取 GLM API Key"
    read -p "   输入 ZAI_API_KEY: " API_KEY
    mkdir -p "$(dirname "$ENV_FILE")"
    echo "ZAI_API_KEY=$API_KEY" > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "  ✅ 密钥已保存到 $ENV_FILE"
else
    echo "✅ API 密钥已配置"
fi

# ── 6. 验证 ──
echo ""
echo "============================================================"
echo "  ✅ 环境搭建完成！"
echo "============================================================"
echo ""
echo "启动后端服务:"
echo "  .venv/bin/python qa-backend/server.py"
echo ""
echo "启动前端（可选，也可直接用 GitHub Pages）:"
echo "  python3 -m http.server 8097"
echo ""
echo "创建 Cloudflare 隧道（让外部能访问）:"
echo "  cloudflared tunnel --url http://localhost:8765"
echo ""
