#!/usr/bin/env bash
# ============================================================
# Feishu AI Bot — 一键安装脚本
#
# 用法:
#   curl -fsSL https://raw.githubusercontent.com/Sttrevens/feishu-ai-bot/main/install.sh | bash
#   或:
#   git clone https://github.com/Sttrevens/feishu-ai-bot && cd feishu-ai-bot && bash install.sh
# ============================================================
set -euo pipefail

# ── 颜色 ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

# ── 检查依赖 ──
check_deps() {
    local missing=()
    for cmd in docker git; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done
    # docker compose (v2 plugin)
    if ! docker compose version &>/dev/null 2>&1; then
        if ! command -v docker-compose &>/dev/null; then
            missing+=("docker-compose")
        fi
    fi
    if [ ${#missing[@]} -gt 0 ]; then
        err "缺少依赖: ${missing[*]}"
        echo ""
        echo "请先安装:"
        echo "  Docker:  https://docs.docker.com/engine/install/"
        echo "  Git:     sudo apt-get install git"
        exit 1
    fi
    ok "依赖检查通过 (docker, git, docker compose)"
}

# ── 交互式输入（带默认值）──
ask() {
    local prompt="$1" default="${2:-}" var_name="$3" secret="${4:-false}"
    if [ -n "$default" ]; then
        prompt="$prompt [${default}]"
    fi
    echo -ne "${BOLD}$prompt: ${NC}"
    if [ "$secret" = "true" ]; then
        read -rs answer
        echo ""
    else
        read -r answer
    fi
    answer="${answer:-$default}"
    eval "$var_name='$answer'"
}

# ── 选择平台 ──
choose_platform() {
    echo ""
    echo -e "${BOLD}选择 Bot 接入平台:${NC}"
    echo "  1) 飞书 / Lark"
    echo "  2) 企业微信（自建应用）"
    echo "  3) 微信客服（企微客服）"
    echo ""
    local choice
    ask "请选择 (1/2/3)" "1" choice
    case "$choice" in
        1) PLATFORM="feishu" ;;
        2) PLATFORM="wecom" ;;
        3) PLATFORM="wecom_kf" ;;
        *) PLATFORM="feishu" ;;
    esac
    ok "平台: $PLATFORM"
}

# ── 收集飞书凭证 ──
collect_feishu() {
    echo ""
    echo -e "${BOLD}=== 飞书配置 ===${NC}"
    echo "在飞书开放平台创建应用: https://open.feishu.cn/app"
    echo ""
    ask "App ID (cli_xxxxxxx)" "" FEISHU_APP_ID
    ask "App Secret" "" FEISHU_APP_SECRET true
    ask "Verification Token (事件订阅)" "" FEISHU_VERIFICATION_TOKEN
    ask "Encrypt Key (留空=不加密)" "" FEISHU_ENCRYPT_KEY
}

# ── 收集企微凭证 ──
collect_wecom() {
    echo ""
    echo -e "${BOLD}=== 企业微信配置 ===${NC}"
    echo "在企业微信管理后台创建自建应用: https://work.weixin.qq.com/wework_admin/frame#apps"
    echo ""
    ask "Corp ID" "" WECOM_CORPID
    ask "应用 Secret" "" WECOM_CORPSECRET true
    ask "Agent ID" "" WECOM_AGENT_ID
    ask "回调 Token" "" WECOM_TOKEN
    ask "回调 EncodingAESKey" "" WECOM_AES_KEY
}

# ── 收集微信客服凭证 ──
collect_wecom_kf() {
    echo ""
    echo -e "${BOLD}=== 微信客服配置 ===${NC}"
    echo "在企业微信管理后台 > 微信客服 中配置"
    echo ""
    ask "Corp ID" "" WECOM_CORPID
    ask "客服应用 Secret" "" WECOM_KF_SECRET true
    ask "回调 Token" "" WECOM_KF_TOKEN
    ask "回调 EncodingAESKey" "" WECOM_KF_AES_KEY
    ask "客服账号 ID (wkXXXXXX)" "" WECOM_KF_OPEN_KFID
}

# ── 收集通用配置 ──
collect_common() {
    echo ""
    echo -e "${BOLD}=== LLM 配置 ===${NC}"
    echo "Gemini API Key: https://aistudio.google.com/apikey"
    echo ""
    ask "Gemini API Key" "" GEMINI_API_KEY true

    echo ""
    echo -e "${BOLD}=== Gemini 访问方式（国内服务器必选一种）===${NC}"
    echo "  1) Cloudflare Worker 代理（推荐，免费，部署教程见 cloudflare-worker/）"
    echo "  2) 本地代理（服务器上有梯子，如 http://127.0.0.1:7890）"
    echo "  3) 直连（海外服务器）"
    echo ""
    local gmethod
    ask "选择 (1/2/3)" "1" gmethod
    GOOGLE_GEMINI_BASE_URL=""
    GEMINI_PROXY=""
    case "$gmethod" in
        1)
            ask "CF Worker URL (如 https://your-worker.workers.dev)" "" GOOGLE_GEMINI_BASE_URL
            ;;
        2)
            ask "本地代理地址" "http://127.0.0.1:7890" GEMINI_PROXY
            ;;
        3)
            info "直连模式，无需额外配置"
            ;;
    esac

    echo ""
    echo -e "${BOLD}=== 搜索代理（可选，国内服务器建议配置）===${NC}"
    ask "DuckDuckGo 搜索代理 URL (留空=跳过)" "" DDG_SEARCH_PROXY_URL
    DDG_SEARCH_PROXY_TOKEN=""
    if [ -n "$DDG_SEARCH_PROXY_URL" ]; then
        ask "搜索代理 Token (留空=无认证)" "" DDG_SEARCH_PROXY_TOKEN
    fi

    echo ""
    echo -e "${BOLD}=== Redis（记忆/历史/状态存储）===${NC}"
    echo "推荐 Upstash Redis (免费额度足够): https://upstash.com"
    echo ""
    ask "Redis REST URL (如 https://xxx.upstash.io)" "" UPSTASH_REDIS_REST_URL
    ask "Redis REST Token" "" UPSTASH_REDIS_REST_TOKEN true

    echo ""
    echo -e "${BOLD}=== 可选配置 ===${NC}"
    ask "Bot 名称" "我的 AI 助手" BOT_NAME
    ask "Bot ID (英文，唯一标识)" "my-bot" TENANT_ID
    ask "管理员姓名 (飞书/企微显示名)" "Admin" ADMIN_NAME
    ask "System Prompt (Bot 人设)" "你是一个专业的 AI 助手，帮助团队解决问题。" SYSTEM_PROMPT

    echo ""
    ask "GitHub Token (可选，留空=不启用代码功能)" "" GITHUB_TOKEN true
    GITHUB_REPO_OWNER=""
    GITHUB_REPO_NAME=""
    if [ -n "$GITHUB_TOKEN" ]; then
        ask "GitHub Repo Owner" "" GITHUB_REPO_OWNER
        ask "GitHub Repo Name" "" GITHUB_REPO_NAME
    fi
}

# ── 生成 .env ──
generate_env() {
    info "生成 .env..."
    cat > .env << ENVEOF
# === 由 install.sh 自动生成 ===

# ---- Gemini LLM ----
GEMINI_API_KEY=${GEMINI_API_KEY}
GOOGLE_GEMINI_BASE_URL=${GOOGLE_GEMINI_BASE_URL}
GEMINI_PROXY=${GEMINI_PROXY}

# ---- DuckDuckGo 搜索代理 ----
DDG_SEARCH_PROXY_URL=${DDG_SEARCH_PROXY_URL}
DDG_SEARCH_PROXY_TOKEN=${DDG_SEARCH_PROXY_TOKEN}

# ---- Redis (Upstash) ----
UPSTASH_REDIS_REST_URL=${UPSTASH_REDIS_REST_URL}
UPSTASH_REDIS_REST_TOKEN=${UPSTASH_REDIS_REST_TOKEN}

# ---- GitHub ----
GITHUB_TOKEN=${GITHUB_TOKEN}
GITHUB_REPO_OWNER=${GITHUB_REPO_OWNER}
GITHUB_REPO_NAME=${GITHUB_REPO_NAME}
GITHUB_LOCAL_REPO_PATH=/tmp/workspace

# ---- 服务 ----
HOST=0.0.0.0
PORT=8000
DEBUG=false
ENVEOF
    ok ".env 已生成"
}

# ── 生成 tenants.json ──
generate_tenants() {
    info "生成 tenants.json..."

    local platform_creds=""
    case "$PLATFORM" in
        feishu)
            platform_creds=$(cat << CRED
            "app_id": "${FEISHU_APP_ID}",
            "app_secret": "${FEISHU_APP_SECRET}",
            "verification_token": "${FEISHU_VERIFICATION_TOKEN}",
            "encrypt_key": "${FEISHU_ENCRYPT_KEY}",
            "oauth_redirect_uri": "",
            "bot_open_id": "",
CRED
            )
            ;;
        wecom)
            platform_creds=$(cat << CRED
            "wecom_corpid": "${WECOM_CORPID}",
            "wecom_corpsecret": "${WECOM_CORPSECRET}",
            "wecom_agent_id": ${WECOM_AGENT_ID},
            "wecom_token": "${WECOM_TOKEN}",
            "wecom_encoding_aes_key": "${WECOM_AES_KEY}",
CRED
            )
            ;;
        wecom_kf)
            platform_creds=$(cat << CRED
            "wecom_corpid": "${WECOM_CORPID}",
            "wecom_kf_secret": "${WECOM_KF_SECRET}",
            "wecom_kf_token": "${WECOM_KF_TOKEN}",
            "wecom_kf_encoding_aes_key": "${WECOM_KF_AES_KEY}",
            "wecom_kf_open_kfid": "${WECOM_KF_OPEN_KFID}",
CRED
            )
            ;;
    esac

    cat > tenants.json << TENANTEOF
{
    "tenants": [
        {
            "tenant_id": "${TENANT_ID}",
            "name": "${BOT_NAME}",
            "platform": "${PLATFORM}",

${platform_creds}

            "github_token": "\${GITHUB_TOKEN}",
            "github_repo_owner": "${GITHUB_REPO_OWNER}",
            "github_repo_name": "${GITHUB_REPO_NAME}",

            "llm_provider": "gemini",
            "llm_api_key": "\${GEMINI_API_KEY}",
            "llm_base_url": "",
            "llm_model": "gemini-3-flash-preview",
            "llm_model_strong": "gemini-2.5-pro",
            "llm_system_prompt": "${SYSTEM_PROMPT}",

            "coding_model": "",
            "coding_api_key": "",
            "coding_base_url": "",

            "stt_api_key": "",
            "stt_base_url": "",
            "stt_model": "",

            "admin_open_ids": [],
            "admin_names": ["${ADMIN_NAME}"],
            "tools_enabled": [],

            "custom_persona": false,
            "self_iteration_enabled": false,

            "memory_diary_enabled": true,
            "memory_journal_max": 800,
            "memory_chat_rounds": 5,
            "memory_chat_ttl": 3600,
            "memory_context_enabled": true,

            "quota_monthly_api_calls": 0,
            "quota_monthly_tokens": 0,
            "rate_limit_rpm": 60,
            "rate_limit_user_rpm": 10
        }
    ],
    "default_tenant_id": "${TENANT_ID}"
}
TENANTEOF
    ok "tenants.json 已生成"
}

# ── 构建和启动 ──
build_and_start() {
    echo ""
    info "构建 Docker 镜像（首次约 3-5 分钟）..."
    echo ""

    # 国内服务器用阿里云 pip 镜像
    local pip_mirror="https://pypi.org/simple"
    local use_cn
    ask "使用国内镜像加速构建？(Y/n)" "Y" use_cn
    if [[ "$use_cn" =~ ^[Yy]$ ]] || [ -z "$use_cn" ]; then
        pip_mirror="https://mirrors.aliyun.com/pypi/simple"
    fi

    # 构建时不加载 .env（避免代理泄漏到构建环境）
    docker compose --env-file /dev/null build \
        --build-arg PIP_INDEX_URL="$pip_mirror" \
        2>&1 | tail -20

    ok "镜像构建完成"

    echo ""
    info "启动容器..."
    docker compose up -d

    # 等待启动
    sleep 3

    if docker compose ps | grep -q "running"; then
        ok "容器已启动"
    else
        warn "容器可能未正常启动，请检查: docker compose logs"
    fi
}

# ── 打印结果 ──
print_result() {
    local webhook_path=""
    case "$PLATFORM" in
        feishu)    webhook_path="/webhook/event/${TENANT_ID}" ;;
        wecom)     webhook_path="/webhook/wecom/${TENANT_ID}" ;;
        wecom_kf)  webhook_path="/webhook/wecom_kf/${TENANT_ID}" ;;
    esac

    echo ""
    echo -e "${GREEN}============================================${NC}"
    echo -e "${GREEN}  安装完成！${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo ""
    echo -e "Bot 服务运行在: ${BOLD}http://localhost:8000${NC}"
    echo -e "健康检查:       ${BOLD}http://localhost:8000/health${NC}"
    echo ""
    echo -e "${BOLD}Webhook URL (填入平台后台):${NC}"
    echo -e "  ${CYAN}https://YOUR_DOMAIN${webhook_path}${NC}"
    echo ""

    case "$PLATFORM" in
        feishu)
            echo -e "${BOLD}飞书配置步骤:${NC}"
            echo "  1. 打开飞书开放平台 → 你的应用 → 事件订阅"
            echo "  2. 请求地址填: https://YOUR_DOMAIN${webhook_path}"
            echo "  3. 添加事件: im.message.receive_v1"
            echo "  4. 权限管理 → 开通: im:message, im:message:send_as_bot"
            echo "  5. 版本管理 → 创建版本 → 发布"
            ;;
        wecom)
            echo -e "${BOLD}企微配置步骤:${NC}"
            echo "  1. 企微管理后台 → 应用管理 → 你的应用 → API 接收"
            echo "  2. URL 填: https://YOUR_DOMAIN${webhook_path}"
            ;;
        wecom_kf)
            echo -e "${BOLD}微信客服配置步骤:${NC}"
            echo "  1. 企微管理后台 → 微信客服 → API 接收消息"
            echo "  2. URL 填: https://YOUR_DOMAIN${webhook_path}"
            echo "  3. ⚠️  接待方式必须选「智能助手接待」（不是人工接待！）"
            ;;
    esac

    echo ""
    echo -e "${BOLD}常用命令:${NC}"
    echo "  docker compose logs -f    # 查看日志"
    echo "  docker compose restart    # 重启"
    echo "  docker compose down       # 停止"
    echo ""
    echo -e "${BOLD}配置文件:${NC}"
    echo "  .env           # 环境变量（API Key 等）"
    echo "  tenants.json   # Bot 配置（人设、工具、权限等）"
    echo ""
    echo -e "文档: ${CYAN}https://github.com/Sttrevens/feishu-ai-bot${NC}"
    echo ""
}

# ── 主流程 ──
main() {
    echo ""
    echo -e "${BOLD}╔══════════════════════════════════════╗${NC}"
    echo -e "${BOLD}║   Feishu AI Bot — 一键安装向导       ║${NC}"
    echo -e "${BOLD}║   多平台多租户 AI 助手               ║${NC}"
    echo -e "${BOLD}╚══════════════════════════════════════╝${NC}"
    echo ""

    # 如果不在项目目录，先 clone
    if [ ! -f "docker-compose.yml" ]; then
        info "克隆项目..."
        local install_dir="feishu-ai-bot"
        if [ -d "$install_dir" ]; then
            warn "目录 $install_dir 已存在"
            ask "删除并重新克隆？(y/N)" "N" _confirm
            if [[ "$_confirm" =~ ^[Yy]$ ]]; then
                rm -rf "$install_dir"
            else
                echo "请 cd $install_dir 后重新运行 bash install.sh"
                exit 0
            fi
        fi
        git clone https://github.com/Sttrevens/feishu-ai-bot.git "$install_dir"
        cd "$install_dir"
        ok "项目已克隆到 $(pwd)"
    fi

    check_deps
    choose_platform

    case "$PLATFORM" in
        feishu)    collect_feishu ;;
        wecom)     collect_wecom ;;
        wecom_kf)  collect_wecom_kf ;;
    esac

    collect_common
    generate_env
    generate_tenants

    echo ""
    local do_build
    ask "现在构建并启动？(Y/n)" "Y" do_build
    if [[ "$do_build" =~ ^[Yy]$ ]] || [ -z "$do_build" ]; then
        build_and_start
    else
        echo ""
        info "跳过构建。准备好后运行:"
        echo "  docker compose --env-file /dev/null build"
        echo "  docker compose up -d"
    fi

    print_result
}

main "$@"
