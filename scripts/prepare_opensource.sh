#!/usr/bin/env bash
# ============================================================
# prepare_opensource.sh — 一键生成脱敏的开源副本
#
# 用法:
#   bash scripts/prepare_opensource.sh [目标目录]
#
# 默认目标: /tmp/feishu-ai-bot-opensource
#
# 这个脚本不会修改当前仓库的任何文件。
# 它会复制整个项目到目标目录，然后在副本上执行脱敏操作。
# ============================================================
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DST_DIR="${1:-/tmp/feishu-ai-bot-opensource}"

echo "=== 开源副本生成器 ==="
echo "源目录: $SRC_DIR"
echo "目标目录: $DST_DIR"
echo ""

# ── Step 0: 确认 ──
if [ -d "$DST_DIR" ]; then
    echo "⚠️  目标目录已存在: $DST_DIR"
    read -p "删除并重新生成？(y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "取消。"
        exit 0
    fi
    rm -rf "$DST_DIR"
fi

# ── Step 1: 复制项目（排除不需要的文件）──
echo "📦 复制项目文件..."
mkdir -p "$DST_DIR"
# rsync 优先，fallback 到 cp + 手动清理
if command -v rsync &>/dev/null; then
    rsync -a \
        --exclude='.git' \
        --exclude='.bot-memory' \
        --exclude='instances/' \
        --exclude='.env' \
        --exclude='__pycache__' \
        --exclude='.pytest_cache' \
        --exclude='*.pyc' \
        --exclude='.ruff_cache' \
        --exclude='node_modules' \
        "$SRC_DIR/" "$DST_DIR/"
else
    cp -a "$SRC_DIR/." "$DST_DIR/"
    rm -rf "$DST_DIR/.git" "$DST_DIR/.bot-memory" "$DST_DIR/instances" \
           "$DST_DIR/.env" "$DST_DIR/.ruff_cache" "$DST_DIR/node_modules"
    find "$DST_DIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find "$DST_DIR" -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
    find "$DST_DIR" -name "*.pyc" -delete 2>/dev/null || true
fi

echo "✅ 文件复制完成"

# ── Step 2: 用示例文件替换 tenants.json ──
echo "🔑 替换 tenants.json..."
cp "$DST_DIR/tenants.example.json" "$DST_DIR/tenants.json"
echo "✅ tenants.json 已替换为示例文件"

# ── Step 3: 删除含敏感数据的运行时目录 ──
echo "🗑️  删除运行时数据..."
rm -rf "$DST_DIR/.bot-memory"
rm -rf "$DST_DIR/instances"
echo "✅ 运行时数据已清除"

# ── Step 4: 删除含内部信息的文件 ──
echo "🗑️  删除内部文档..."
rm -f "$DST_DIR/PM_BOT_PROMPT.md"
rm -f "$DST_DIR/PLAN.md"
rm -f "$DST_DIR/plan.md"
echo "✅ 内部文档已删除"

# ── Step 5: 替换服务器 IP ──
echo "🌐 替换服务器 IP..."
find "$DST_DIR" -type f \( -name "*.md" -o -name "*.json" -o -name "*.py" -o -name "*.sh" -o -name "*.yml" -o -name "*.yaml" -o -name "*.conf" -o -name "*.html" \) \
    -exec sed -i 's/139\.224\.235\.130/YOUR_SERVER_IP/g' {} +
echo "✅ 服务器 IP 已替换"

# ── Step 6: 替换代码中的硬编码真实姓名 ──
echo "👤 替换硬编码姓名..."

# app/config.py: 默认 admin_names
sed -i 's/ADMIN_NAMES", "吴天骄"/ADMIN_NAMES", "Admin"/' "$DST_DIR/app/config.py"

# app/services/super_admin.py: 默认超管名
sed -i 's/SUPER_ADMIN_NAME", "吴天骄"/SUPER_ADMIN_NAME", "Admin"/' "$DST_DIR/app/services/super_admin.py"

# app/tools/memory_ops.py: 示例文本
sed -i 's/帮吴天骄修了碰撞检测 bug/帮张三修了碰撞检测 bug/' "$DST_DIR/app/tools/memory_ops.py"

# tests/test_customer_store.py: 测试数据
sed -i 's/"name": "吴天骄"/"name": "Admin"/g' "$DST_DIR/tests/test_customer_store.py"

# app/tools/provision_ops.py: 示例名
sed -i "s/高梦科技 AI 助手/示例公司 AI 助手/g" "$DST_DIR/app/tools/provision_ops.py"

# app/webhook/handler.py: 注释中的示例名
sed -i 's/"name": "高梦"/"name": "BotName"/g' "$DST_DIR/app/webhook/handler.py"

# app/tools/doc_ops.py: 示例section名
sed -i 's/耀西的碎碎念/Bot的碎碎念/g' "$DST_DIR/app/tools/doc_ops.py"

echo "✅ 硬编码姓名已替换"

# ── Step 7: 替换 GitHub 用户名和私有仓库名 ──
echo "📂 替换 GitHub 信息..."

# docs/add-tenant.md
sed -i 's/Sttrevens/your-github-username/g' "$DST_DIR/docs/add-tenant.md"

# README.md
sed -i 's/Sttrevens/your-github-username/g' "$DST_DIR/README.md"

# CDREBIRTH 仓库名
find "$DST_DIR" -type f -name "*.md" -exec sed -i 's/CDREBIRTH/your-repo-name/g' {} +

echo "✅ GitHub 信息已替换"

# ── Step 8: 脱敏 sales_playbook.md ──
echo "📝 脱敏 knowledge 模块..."
PLAYBOOK="$DST_DIR/app/knowledge/modules/sales_playbook.md"
if [ -f "$PLAYBOOK" ]; then
    sed -i 's/吴天骄/创始人/g' "$PLAYBOOK"
fi
echo "✅ Knowledge 模块已脱敏"

# ── Step 9: 脱敏 health_monitor.sh ──
echo "🏥 脱敏运维脚本..."
HEALTH="$DST_DIR/scripts/health_monitor.sh"
if [ -f "$HEALTH" ]; then
    sed -i 's|/root/4dgames-feishu-code-bot|/opt/feishu-ai-bot|g' "$HEALTH"
    sed -i 's/NAMES=("code-bot" "pm-bot" "kf-steven-ai")/NAMES=("bot-1" "bot-2" "bot-3")/' "$HEALTH"
fi
echo "✅ 运维脚本已脱敏"

# ── Step 10: 脱敏 deploy.yml ──
echo "🚀 脱敏 CI/CD 配置..."
DEPLOY="$DST_DIR/.github/workflows/deploy.yml"
if [ -f "$DEPLOY" ]; then
    sed -i 's|/root/4dgames-feishu-code-bot|/opt/feishu-ai-bot|g' "$DEPLOY"
fi
echo "✅ CI/CD 配置已脱敏"

# ── Step 11: 脱敏 CLAUDE.md ──
echo "📖 脱敏 CLAUDE.md..."
CLAUDE_MD="$DST_DIR/CLAUDE.md"
if [ -f "$CLAUDE_MD" ]; then
    # 替换真实姓名
    sed -i 's/吴天骄/Admin/g' "$CLAUDE_MD"
    sed -i 's/Steven/Admin/g' "$CLAUDE_MD"
    sed -i 's/高梦/CodeBot/g' "$CLAUDE_MD"
    sed -i 's/耀西/PMBot/g' "$CLAUDE_MD"
    # 替换容器和租户名中的真名
    sed -i 's/kf-steven-ai/kf-ai-assistant/g' "$CLAUDE_MD"
    # 替换 corpid
    sed -i 's/ww_YOUR_CORP_ID/ww_YOUR_CORP_ID/g' "$CLAUDE_MD"
    # 替换 open_kfid
    sed -i 's/wk_YOUR_KF_ID_1/wk_YOUR_KF_ID_1/g' "$CLAUDE_MD"
    sed -i 's/wk_YOUR_KF_ID_2/wk_YOUR_KF_ID_2/g' "$CLAUDE_MD"
    # 替换 app_id
    sed -i 's/cli_YOUR_APP_ID_1/cli_YOUR_APP_ID_1/g' "$CLAUDE_MD"
    sed -i 's/cli_YOUR_APP_ID_2/cli_YOUR_APP_ID_2/g' "$CLAUDE_MD"
    # 替换服务器路径
    sed -i 's|/root/4dgames-feishu-code-bot|/opt/feishu-ai-bot|g' "$CLAUDE_MD"
    # 替换 GitHub
    sed -i 's/Sttrevens/your-github-username/g' "$CLAUDE_MD"
fi
echo "✅ CLAUDE.md 已脱敏"

# ── Step 11.5: 脱敏脚本自身（脚本中含有真实 ID 作为替换模式）──
echo "🔧 脱敏 prepare_opensource.sh 自身..."
SELF_SCRIPT="$DST_DIR/scripts/prepare_opensource.sh"
if [ -f "$SELF_SCRIPT" ]; then
    sed -i 's/wk_YOUR_KF_ID_1/wk_YOUR_KF_ID_1/g' "$SELF_SCRIPT"
    sed -i 's/wk_YOUR_KF_ID_2/wk_YOUR_KF_ID_2/g' "$SELF_SCRIPT"
    sed -i 's/cli_YOUR_APP_ID_1/cli_YOUR_APP_ID_1/g' "$SELF_SCRIPT"
    sed -i 's/cli_YOUR_APP_ID_2/cli_YOUR_APP_ID_2/g' "$SELF_SCRIPT"
    sed -i 's/ww_YOUR_CORP_ID/ww_YOUR_CORP_ID/g' "$SELF_SCRIPT"
    sed -i 's/YOUR_APP_SECRET_1/YOUR_APP_SECRET_1/g' "$SELF_SCRIPT"
    sed -i 's/YOUR_APP_SECRET_2/YOUR_APP_SECRET_2/g' "$SELF_SCRIPT"
    sed -i 's/YOUR_KF_SECRET/YOUR_KF_SECRET/g' "$SELF_SCRIPT"
    sed -i 's/YOUR_VERIFY_TOKEN_1/YOUR_VERIFY_TOKEN_1/g' "$SELF_SCRIPT"
    sed -i 's/YOUR_VERIFY_TOKEN_2/YOUR_VERIFY_TOKEN_2/g' "$SELF_SCRIPT"
    # 也替换验证变量中的部分 ID
    sed -i 's/wk_REPLACED/wk_REPLACED/g' "$SELF_SCRIPT"
    sed -i 's/cli_REPLACED1/cli_REPLACED1/g' "$SELF_SCRIPT"
    sed -i 's/cli_REPLACED2/cli_REPLACED2/g' "$SELF_SCRIPT"
    sed -i 's/SECRET_REPLACED1/SECRET_REPLACED1/g' "$SELF_SCRIPT"
    sed -i 's/SECRET_REPLACED2/SECRET_REPLACED2/g' "$SELF_SCRIPT"
fi
echo "✅ 脚本自身已脱敏"

# ── Step 12: 更新 .gitignore（确保开源版也忽略敏感文件）──
echo "📝 更新 .gitignore..."
cat >> "$DST_DIR/.gitignore" << 'GITIGNORE_APPEND'

# Sensitive runtime data
.bot-memory/
tenants.json
GITIGNORE_APPEND
echo "✅ .gitignore 已更新"

# ── Step 13: 创建 .env.example 补充说明 ──
echo "📝 检查 .env.example..."
# .env.example 已有，不需要额外操作

# ── Step 14: 验证 ──
echo ""
echo "=== 验证脱敏结果 ==="

ISSUES=0

# 验证模式用变量存储，避免 grep 匹配到验证代码自身
_P1="SECRET_REPLACED1"   # Feishu app_secret 前缀
_P2="SECRET_REPLACED2"   # WeCom kf_secret 前缀
_P3="YOUR_SERVER_IP"     # 服务器 IP
_P4="wk_REPLACED"          # open_kfid 前缀
_P5="cli_REPLACED1"        # app_id 1 前缀
_P6="cli_REPLACED2"        # app_id 2 前缀

# 检查是否还有真实凭证
if grep -rF "$_P1" "$DST_DIR" --include="*.json" --include="*.py" -l 2>/dev/null; then
    echo "❌ 仍有 Feishu app_secret 残留！"
    ISSUES=$((ISSUES + 1))
fi

if grep -rF "$_P2" "$DST_DIR" --include="*.json" --include="*.py" -l 2>/dev/null; then
    echo "❌ 仍有 WeCom kf_secret 残留！"
    ISSUES=$((ISSUES + 1))
fi

if grep -rF "$_P3" "$DST_DIR" -l 2>/dev/null; then
    echo "❌ 仍有服务器 IP 残留！"
    ISSUES=$((ISSUES + 1))
fi

if [ -d "$DST_DIR/.git" ]; then
    echo "❌ .git 目录仍存在！"
    ISSUES=$((ISSUES + 1))
fi

if [ -d "$DST_DIR/.bot-memory" ]; then
    echo "❌ .bot-memory 目录仍存在！"
    ISSUES=$((ISSUES + 1))
fi

if [ -d "$DST_DIR/instances" ]; then
    echo "❌ instances 目录仍存在！"
    ISSUES=$((ISSUES + 1))
fi

if [ -f "$DST_DIR/.env" ]; then
    echo "❌ .env 文件仍存在！"
    ISSUES=$((ISSUES + 1))
fi

# 检查是否还有 open_kfid / app_id 明文
if grep -rF "$_P4" "$DST_DIR" -l 2>/dev/null; then
    echo "❌ 仍有真实 open_kfid 残留！"
    ISSUES=$((ISSUES + 1))
fi

if grep -rF "$_P5" "$DST_DIR" -l 2>/dev/null || grep -rF "$_P6" "$DST_DIR" -l 2>/dev/null; then
    echo "❌ 仍有真实 app_id 残留！"
    ISSUES=$((ISSUES + 1))
fi

if [ $ISSUES -eq 0 ]; then
    echo "✅ 所有验证通过！"
else
    echo ""
    echo "⚠️  发现 $ISSUES 个问题，请手动检查并修复。"
fi

echo ""
echo "=== 完成 ==="
echo "脱敏副本已生成: $DST_DIR"
echo ""
echo "下一步操作："
echo "  cd $DST_DIR"
echo "  git init"
echo "  git add -A"
echo "  git commit -m 'Initial open source release'"
echo "  git remote add origin git@github.com:YOUR_ORG/feishu-ai-bot.git"
echo "  git push -u origin main"
echo ""
echo "⚠️  别忘了轮换以下密钥（它们已经在旧 repo 的 git 历史中）："
echo "  - 飞书 app_secret × 2"
echo "  - 飞书 verification_token × 2"
echo "  - 企微 kf_secret / kf_token / kf_encoding_aes_key"
