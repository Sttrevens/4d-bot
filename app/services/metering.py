"""用量计量系统

记录每个租户/用户的 LLM 调用量，支持月度配额限制。

数据存储在 Upstash Redis 中:
- meter:{tenant_id}:{YYYY-MM}       → HASH {input_tokens, output_tokens, api_calls, tool_calls}
- meter:{tenant_id}:{YYYY-MM}:daily → HASH {DD: json({input_tokens, output_tokens, api_calls})}
- meter:{tenant_id}:quota           → HASH {monthly_api_calls, monthly_input_tokens, ...}

设计原则:
- 写入失败不影响正常业务（fail-open for writes）
- 配额检查失败时放行（fail-open for reads, 避免误拒）
- 每次 LLM 调用结束后异步写入，不阻塞响应
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.services import redis_client as redis

logger = logging.getLogger(__name__)

# LLM provider 设置 (input_tokens, output_tokens)，intent.py 读取
last_usage_tokens: contextvars.ContextVar[tuple[int, int]] = contextvars.ContextVar(
    "last_usage_tokens", default=(0, 0),
)


@dataclass
class UsageRecord:
    """单次 LLM 调用的用量记录"""
    tenant_id: str
    sender_id: str = ""
    model: str = ""
    provider: str = ""          # "gemini" | "openai"
    input_tokens: int = 0
    output_tokens: int = 0
    api_calls: int = 1          # LLM API 调用次数（含重试）
    tool_calls: int = 0         # 工具调用次数
    rounds: int = 0             # agent 循环轮次
    latency_ms: int = 0         # 总耗时（毫秒）
    timestamp: float = field(default_factory=time.time)


def record_usage(rec: UsageRecord) -> None:
    """记录一次用量到 Redis（同步，fire-and-forget）。

    调用方应在 agent loop 结束后调用此函数。
    Redis 不可用时静默跳过，不影响业务。
    """
    if not redis.available() or not rec.tenant_id:
        return

    try:
        now = datetime.fromtimestamp(rec.timestamp, tz=timezone.utc)
        month_key = f"meter:{rec.tenant_id}:{now.strftime('%Y-%m')}"
        day = now.strftime("%d")

        # Pipeline: 原子累加月度汇总 + 每日明细
        commands: list[list[str | int]] = [
            # 月度汇总
            ["HINCRBY", month_key, "input_tokens", rec.input_tokens],
            ["HINCRBY", month_key, "output_tokens", rec.output_tokens],
            ["HINCRBY", month_key, "api_calls", rec.api_calls],
            ["HINCRBY", month_key, "tool_calls", rec.tool_calls],
            ["HINCRBY", month_key, "rounds", rec.rounds],
            # 月底自动过期（保留 90 天）
            ["EXPIRE", month_key, 7776000],
        ]

        # 每日明细（用 HINCRBY 分字段累加）
        daily_key = f"{month_key}:daily:{day}"
        commands.extend([
            ["HINCRBY", daily_key, "input_tokens", rec.input_tokens],
            ["HINCRBY", daily_key, "output_tokens", rec.output_tokens],
            ["HINCRBY", daily_key, "api_calls", rec.api_calls],
            ["HINCRBY", daily_key, "tool_calls", rec.tool_calls],
            ["EXPIRE", daily_key, 7776000],
        ])

        # 用户级别计数（可选，用于单用户异常检测）
        if rec.sender_id:
            user_key = f"meter:{rec.tenant_id}:user:{rec.sender_id}:{now.strftime('%Y-%m')}"
            commands.extend([
                ["HINCRBY", user_key, "api_calls", rec.api_calls],
                ["HINCRBY", user_key, "input_tokens", rec.input_tokens],
                ["EXPIRE", user_key, 7776000],
            ])

        redis.pipeline(commands)

    except Exception:
        logger.warning("metering record failed", exc_info=True)


def record_sub_agent_run(
    tenant_id: str,
    agent_type: str,
    rounds: int,
    tool_calls: int,
    elapsed_s: float,
    result_len: int,
    outcome: str,  # "success" | "fallback" | "error" | "timeout" | "stall"
    tools_used: list[str] | None = None,
) -> None:
    """记录子 agent 执行指标到 Redis。

    数据存储：
    - sub_agent:{tenant_id}:{YYYY-MM} → HASH 月度汇总
    - sub_agent:{tenant_id}:recent    → LIST 最近 50 次执行记录（用于 dashboard 展示）
    """
    if not redis.available() or not tenant_id:
        return
    try:
        now = datetime.fromtimestamp(time.time(), tz=timezone.utc)
        month_key = f"sub_agent:{tenant_id}:{now.strftime('%Y-%m')}"

        # 月度汇总（按 agent_type 分别计数）
        commands: list[list] = [
            ["HINCRBY", month_key, f"{agent_type}:runs", 1],
            ["HINCRBY", month_key, f"{agent_type}:rounds", rounds],
            ["HINCRBY", month_key, f"{agent_type}:tool_calls", tool_calls],
            ["HINCRBY", month_key, f"{agent_type}:{outcome}", 1],
            ["EXPIRE", month_key, 7776000],
        ]

        # 最近 50 次执行记录
        recent_key = f"sub_agent:{tenant_id}:recent"
        record = json.dumps({
            "type": agent_type,
            "rounds": rounds,
            "tools": tool_calls,
            "elapsed_s": round(elapsed_s, 1),
            "result_len": result_len,
            "outcome": outcome,
            "top_tools": (tools_used or [])[-5:],
            "ts": time.time(),
        }, ensure_ascii=False)
        commands.extend([
            ["LPUSH", recent_key, record],
            ["LTRIM", recent_key, "0", "49"],
            ["EXPIRE", recent_key, 7776000],
        ])

        redis.pipeline(commands)
    except Exception:
        logger.warning("sub-agent metering failed", exc_info=True)


def check_quota(tenant_id: str) -> tuple[bool, str]:
    """检查租户是否超出月度配额。

    Returns:
        (allowed, reason): allowed=True 表示放行，reason 为拒绝原因
    """
    if not redis.available() or not tenant_id:
        return True, ""  # fail-open

    try:
        from app.tenant.registry import tenant_registry
        tenant = tenant_registry.get(tenant_id)
        if not tenant:
            return True, ""

        # 从租户配置读取配额（0 = 无限制）
        monthly_api_limit = getattr(tenant, "quota_monthly_api_calls", 0)
        monthly_token_limit = getattr(tenant, "quota_monthly_tokens", 0)

        if not monthly_api_limit and not monthly_token_limit:
            return True, ""  # 无配额限制

        now = datetime.now(timezone.utc)
        month_key = f"meter:{tenant_id}:{now.strftime('%Y-%m')}"

        # 批量读取当月用量
        results = redis.pipeline([
            ["HGET", month_key, "api_calls"],
            ["HGET", month_key, "input_tokens"],
            ["HGET", month_key, "output_tokens"],
        ])

        api_calls = int(results[0] or 0)
        input_tokens = int(results[1] or 0)
        output_tokens = int(results[2] or 0)
        total_tokens = input_tokens + output_tokens

        if monthly_api_limit and api_calls >= monthly_api_limit:
            return False, f"本月 API 调用次数已达上限（{api_calls}/{monthly_api_limit}）"

        if monthly_token_limit and total_tokens >= monthly_token_limit:
            return False, f"本月 token 用量已达上限（{total_tokens}/{monthly_token_limit}）"

        return True, ""

    except Exception:
        logger.warning("quota check failed", exc_info=True)
        return True, ""  # fail-open


def get_usage_summary(tenant_id: str, month: str = "") -> dict:
    """获取租户月度用量摘要。

    Args:
        tenant_id: 租户 ID
        month: 月份字符串（YYYY-MM），为空则取当月

    Returns:
        {input_tokens, output_tokens, api_calls, tool_calls, rounds}
    """
    if not redis.available():
        return {}

    try:
        if not month:
            now = datetime.now(timezone.utc)
            month = now.strftime("%Y-%m")

        month_key = f"meter:{tenant_id}:{month}"
        data = redis.execute("HGETALL", month_key)

        if not data or not isinstance(data, list):
            return {}

        result = {}
        for i in range(0, len(data) - 1, 2):
            key = data[i]
            try:
                result[key] = int(data[i + 1])
            except (ValueError, TypeError):
                result[key] = data[i + 1]

        return result

    except Exception:
        logger.warning("get_usage_summary failed", exc_info=True)
        return {}


def get_daily_breakdown(tenant_id: str, month: str = "") -> dict[str, dict]:
    """获取租户每日用量明细。

    Returns:
        {"01": {api_calls: N, input_tokens: N, ...}, "02": {...}, ...}
    """
    if not redis.available():
        return {}

    try:
        if not month:
            now = datetime.now(timezone.utc)
            month = now.strftime("%Y-%m")

        # 用 pipeline 一次性查 31 天（1 次 HTTP 代替 31 次）
        commands = []
        day_strs = []
        for day in range(1, 32):
            day_str = f"{day:02d}"
            day_strs.append(day_str)
            commands.append(["HGETALL", f"meter:{tenant_id}:{month}:daily:{day_str}"])

        responses = redis.pipeline(commands)

        result = {}
        for day_str, data in zip(day_strs, responses):
            if data and isinstance(data, list) and len(data) >= 2:
                day_data = {}
                for i in range(0, len(data) - 1, 2):
                    try:
                        day_data[data[i]] = int(data[i + 1])
                    except (ValueError, TypeError):
                        day_data[data[i]] = data[i + 1]
                if day_data:
                    result[day_str] = day_data

        return result

    except Exception:
        logger.warning("get_daily_breakdown failed", exc_info=True)
        return {}
