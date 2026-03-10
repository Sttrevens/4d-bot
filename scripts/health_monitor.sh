#!/usr/bin/env bash
# 容器健康监控脚本 — 定期检查 provisioned 容器状态
#
# 用法：
#   1. 配置飞书 webhook（可选）：
#      export FEISHU_ALERT_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
#   2. 手动运行：bash scripts/health_monitor.sh
#   3. 加入 crontab（每 5 分钟检查一次）：
#      */5 * * * * cd /opt/feishu-ai-bot && bash scripts/health_monitor.sh >> /var/log/bot-health.log 2>&1

set -euo pipefail

# ── 配置 ──
PORTS=(8101 8102 8103)
NAMES=("bot-1" "bot-2" "bot-3")
TIMEOUT=5
ALERT_WEBHOOK="${FEISHU_ALERT_WEBHOOK:-}"
STATE_DIR="/tmp/bot-health-state"

mkdir -p "$STATE_DIR"

NOW=$(date '+%Y-%m-%d %H:%M:%S')
ALL_HEALTHY=true
ALERTS=""

for i in "${!PORTS[@]}"; do
    port="${PORTS[$i]}"
    name="${NAMES[$i]}"
    state_file="$STATE_DIR/$name"

    if curl -sf --max-time "$TIMEOUT" "http://127.0.0.1:$port/health" > /dev/null 2>&1; then
        # 健康
        if [ -f "$state_file" ]; then
            # 从故障恢复
            echo "[$NOW] $name (port $port): RECOVERED"
            rm -f "$state_file"
            ALERTS="${ALERTS}$name (port $port): 已恢复\n"
        fi
    else
        # 不健康
        ALL_HEALTHY=false
        echo "[$NOW] $name (port $port): UNHEALTHY"

        if [ ! -f "$state_file" ]; then
            # 首次故障，记录并告警
            echo "$NOW" > "$state_file"
            ALERTS="${ALERTS}$name (port $port): 无响应！\n"
        else
            # 持续故障，检查是否需要再次告警（每 30 分钟重复告警一次）
            first_fail=$(cat "$state_file")
            # 简单处理：如果文件存在超过 30 分钟，更新时间戳并再次告警
            file_age=$(( $(date +%s) - $(stat -c %Y "$state_file" 2>/dev/null || echo 0) ))
            if [ "$file_age" -gt 1800 ]; then
                echo "$NOW" > "$state_file"
                ALERTS="${ALERTS}$name (port $port): 持续无响应（首次故障: $first_fail）\n"
            fi
        fi
    fi
done

# ── 发送告警 ──
if [ -n "$ALERTS" ] && [ -n "$ALERT_WEBHOOK" ]; then
    # 飞书自定义机器人 webhook
    ALERT_TEXT="Bot 容器健康告警\n时间: $NOW\n\n${ALERTS}"
    curl -sf --max-time 10 -X POST "$ALERT_WEBHOOK" \
        -H 'Content-Type: application/json' \
        -d "{\"msg_type\": \"text\", \"content\": {\"text\": \"$ALERT_TEXT\"}}" \
        > /dev/null 2>&1 || echo "[$NOW] WARN: 告警发送失败"
fi

if $ALL_HEALTHY; then
    echo "[$NOW] All containers healthy"
fi
