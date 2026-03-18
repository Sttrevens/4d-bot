"""工具性能追踪器 — 记录工具调用成功/失败率，沉淀经验

GenericAgent 式的"自适应学习"：bot 调用工具的经验会沉淀到 Redis，
下次遇到类似任务时 system prompt 注入历史经验提示。

功能:
1. 基础统计: 工具调用成功/失败率、延迟
2. 经验教训: 手动记录 + 自动生成（连续失败模式检测）
3. 工具组合模式: 追踪常见工具调用序列，发现高频组合
4. 经验提示注入: system prompt 中注入工具使用经验

Redis 数据结构:
    tool_stats:{tenant_id}:{tool_name} → HASH {
        calls, successes, failures, last_error, last_error_at,
        avg_latency_ms, total_latency_ms
    }
    tool_lessons:{tenant_id} → LIST [JSON lesson entries, max 50]
    tool_seq:{tenant_id}    → LIST [JSON sequence entries, max 200]
    tool_combos:{tenant_id} → HASH {combo_key → count}

设计原则: fail-open — Redis 不可用时静默跳过，不影响业务。
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from typing import Any

logger = logging.getLogger(__name__)

# 内存缓存：减少 Redis 查询
# 工具统计是慢变数据（一次对话才几次调用），10 分钟缓存足够
_stats_cache: dict[str, tuple[float, dict]] = {}  # key → (timestamp, data)
_CACHE_TTL = 600

# ── 会话内连续失败追踪（内存，per-request 生命周期）──
# tenant_id → {tool_name → consecutive_fail_count}
_session_failures: dict[str, dict[str, int]] = {}
_AUTO_LESSON_THRESHOLD = 3  # 连续失败 N 次触发自动经验生成


def record_tool_call(
    tenant_id: str,
    tool_name: str,
    success: bool,
    latency_ms: float = 0,
    error_msg: str = "",
) -> None:
    """记录一次工具调用结果（异步友好的同步接口，fire-and-forget）。

    增强: 连续失败时自动生成经验教训。
    """
    if not tenant_id or tool_name in ("think", "request_more_tools"):
        return

    # ── 连续失败追踪 + 自动经验生成 ──
    _track_consecutive_failures(tenant_id, tool_name, success, error_msg)

    try:
        from app.services.redis_client import execute, available
        if not available():
            return
        key = f"tool_stats:{tenant_id}:{tool_name}"
        # HINCRBY 原子递增
        execute("HINCRBY", key, "calls", 1)
        if success:
            execute("HINCRBY", key, "successes", 1)
        else:
            execute("HINCRBY", key, "failures", 1)
            if error_msg:
                # 只保留最近一次错误摘要（截断到 200 字符）
                execute("HSET", key, "last_error", error_msg[:200])
                execute("HSET", key, "last_error_at", str(int(time.time())))
        if latency_ms > 0:
            execute("HINCRBYFLOAT", key, "total_latency_ms", str(latency_ms))
        # 30 天过期（不会无限积累）
        execute("EXPIRE", key, 2592000)
        # 清除缓存
        _stats_cache.pop(key, None)
    except Exception:
        logger.debug("tool_tracker: record failed", exc_info=True)


def _track_consecutive_failures(
    tenant_id: str, tool_name: str, success: bool, error_msg: str,
) -> None:
    """追踪连续失败，达到阈值时自动生成经验教训。"""
    if tenant_id not in _session_failures:
        _session_failures[tenant_id] = {}
    tracker = _session_failures[tenant_id]

    if success:
        tracker.pop(tool_name, None)
        return

    tracker[tool_name] = tracker.get(tool_name, 0) + 1
    count = tracker[tool_name]

    if count == _AUTO_LESSON_THRESHOLD:
        # 自动生成经验教训
        lesson = _generate_failure_lesson(tool_name, error_msg, count)
        if lesson:
            record_lesson(tenant_id, tool_name, lesson, context="auto-detected")
            logger.info("tool_tracker: auto-lesson for %s: %s", tool_name, lesson[:80])
        # 重置计数（避免同一 session 反复生成）
        tracker[tool_name] = 0


def _generate_failure_lesson(tool_name: str, error_msg: str, count: int) -> str:
    """根据工具名和错误信息生成经验教训。"""
    err_lower = error_msg.lower() if error_msg else ""

    # 常见错误模式 → 具体建议
    if "timeout" in err_lower or "timed out" in err_lower:
        return f"{tool_name} 容易超时，建议简化参数或拆分为多步"
    if "rate limit" in err_lower or "429" in err_lower:
        return f"{tool_name} 触发限流，调用间隔需加大"
    if "permission" in err_lower or "403" in err_lower or "权限" in err_lower:
        return f"{tool_name} 权限不足，检查 token/授权状态"
    if "not found" in err_lower or "404" in err_lower:
        return f"{tool_name} 目标资源不存在，先验证 ID/路径是否正确"
    if "invalid" in err_lower or "参数" in err_lower:
        return f"{tool_name} 参数格式有误，仔细检查参数要求"

    # 通用兜底
    if error_msg:
        return f"{tool_name} 连续失败{count}次，错误: {error_msg[:100]}"
    return f"{tool_name} 连续失败{count}次，考虑换一种方式或工具"


def reset_session_failures(tenant_id: str) -> None:
    """清空会话级失败追踪（新对话开始时调用）。"""
    _session_failures.pop(tenant_id, None)


def record_lesson(
    tenant_id: str,
    tool_name: str,
    lesson: str,
    context: str = "",
) -> None:
    """记录一条工具使用经验教训（如"小红书搜索需要配合 browser_read"）。"""
    if not tenant_id or not lesson:
        return
    try:
        from app.services.redis_client import execute, available
        if not available():
            return
        key = f"tool_lessons:{tenant_id}"
        entry = json.dumps({
            "tool": tool_name,
            "lesson": lesson[:300],
            "context": context[:200],
            "at": int(time.time()),
        }, ensure_ascii=False)
        execute("LPUSH", key, entry)
        execute("LTRIM", key, 0, 49)  # 最多保留 50 条
        execute("EXPIRE", key, 2592000)
    except Exception:
        logger.debug("tool_tracker: record_lesson failed", exc_info=True)


def get_tool_stats(tenant_id: str, tool_name: str) -> dict[str, Any]:
    """获取单个工具的历史统计。"""
    key = f"tool_stats:{tenant_id}:{tool_name}"
    # 检查缓存
    cached = _stats_cache.get(key)
    if cached and (time.time() - cached[0]) < _CACHE_TTL:
        return cached[1]
    try:
        from app.services.redis_client import execute, available
        if not available():
            return {}
        raw = execute("HGETALL", key)
        if not raw or not isinstance(raw, list):
            return {}
        # HGETALL 返回 [k1, v1, k2, v2, ...]
        stats = {}
        for i in range(0, len(raw), 2):
            stats[raw[i]] = raw[i + 1]
        _stats_cache[key] = (time.time(), stats)
        return stats
    except Exception:
        logger.debug("tool_tracker: get_stats failed", exc_info=True)
        return {}


def get_recent_lessons(tenant_id: str, limit: int = 10) -> list[dict]:
    """获取最近的工具使用经验。"""
    try:
        from app.services.redis_client import execute, available
        if not available():
            return []
        key = f"tool_lessons:{tenant_id}"
        raw = execute("LRANGE", key, 0, limit - 1)
        if not raw or not isinstance(raw, list):
            return []
        lessons = []
        for entry in raw:
            try:
                lessons.append(json.loads(entry))
            except (json.JSONDecodeError, TypeError):
                continue
        return lessons
    except Exception:
        logger.debug("tool_tracker: get_lessons failed", exc_info=True)
        return []


def _bulk_get_tool_stats(tenant_id: str, tool_names: set[str]) -> dict[str, dict]:
    """批量获取工具统计（单次 pipeline 调用），避免逐个 HTTP 请求到 Upstash。"""
    if not tool_names:
        return {}

    now = time.time()
    result: dict[str, dict] = {}
    uncached: list[str] = []

    # 先从缓存取
    for name in tool_names:
        key = f"tool_stats:{tenant_id}:{name}"
        cached = _stats_cache.get(key)
        if cached and (now - cached[0]) < _CACHE_TTL:
            if cached[1]:
                result[name] = cached[1]
        else:
            uncached.append(name)

    if not uncached:
        return result

    # 批量从 Redis 取（单次 pipeline 请求）
    try:
        from app.services.redis_client import pipeline as redis_pipeline, available
        if not available():
            return result
        commands = [["HGETALL", f"tool_stats:{tenant_id}:{name}"] for name in uncached]
        raw_results = redis_pipeline(commands)
        for name, raw in zip(uncached, raw_results):
            key = f"tool_stats:{tenant_id}:{name}"
            if not raw or not isinstance(raw, list):
                _stats_cache[key] = (now, {})
                continue
            stats = {}
            for i in range(0, len(raw), 2):
                stats[raw[i]] = raw[i + 1]
            _stats_cache[key] = (now, stats)
            if stats:
                result[name] = stats
    except Exception:
        logger.debug("tool_tracker: bulk_get_stats failed", exc_info=True)

    return result


def build_experience_hint(tenant_id: str, active_tool_names: set[str]) -> str:
    """构建工具经验提示，注入到 system prompt。

    只注入经验教训（lessons），不再逐工具查统计。
    理由：失败率高的工具应该修工具本身，不是靠 prompt 告诉 LLM。
    逐工具查 Redis 即使 pipeline 也是浪费（绝大多数情况无输出）。
    """
    if not tenant_id:
        return ""

    # 最近经验教训（最多 3 条与当前工具相关的，单次 Redis LRANGE）
    lessons = get_recent_lessons(tenant_id, limit=20)
    relevant = [
        l for l in lessons
        if l.get("tool") in active_tool_names
    ][:3]

    if not relevant:
        return ""

    hints = [f"  · 经验: {l.get('lesson', '')}" for l in relevant]
    return "\n\n[工具使用经验]\n" + "\n".join(hints)


# ── P3: 工具组合模式追踪（Tool Combination Pattern Discovery）──

# 会话内工具调用序列（内存）
# tenant_id → [tool_name, tool_name, ...]
_session_sequences: dict[str, list[str]] = {}
_COMBO_WINDOW = 3  # 滑动窗口大小（连续 N 个工具视为一个组合）
_COMBO_MIN_FREQ = 5  # 最少出现次数才算"高频组合"
_SKIP_COMBO_TOOLS = frozenset({"think", "request_more_tools", "recall_memory"})


def record_tool_sequence(tenant_id: str, tool_name: str) -> None:
    """记录工具调用到当前会话序列（用于组合模式发现）。"""
    if not tenant_id or tool_name in _SKIP_COMBO_TOOLS:
        return
    if tenant_id not in _session_sequences:
        _session_sequences[tenant_id] = []
    _session_sequences[tenant_id].append(tool_name)


def flush_session_sequence(tenant_id: str) -> None:
    """会话结束时，将工具调用序列中的组合写入 Redis。"""
    seq = _session_sequences.pop(tenant_id, [])
    if len(seq) < 2:
        return

    # 提取所有 2-3 元组合
    combos: list[str] = []
    for window in (2, 3):
        for i in range(len(seq) - window + 1):
            sub = seq[i:i + window]
            # 去重：同工具连续调用不算组合
            if len(set(sub)) < 2:
                continue
            combos.append("→".join(sub))

    if not combos:
        return

    try:
        from app.services.redis_client import execute, available
        if not available():
            return
        key = f"tool_combos:{tenant_id}"
        for combo in combos:
            execute("HINCRBY", key, combo, 1)
        execute("EXPIRE", key, 2592000)  # 30 天
    except Exception:
        logger.debug("tool_tracker: flush_sequence failed", exc_info=True)


def get_frequent_combos(tenant_id: str, min_freq: int = 0) -> list[tuple[str, int]]:
    """获取高频工具组合。返回 [(combo_str, count), ...] 按频次降序。"""
    if not tenant_id:
        return []
    if min_freq <= 0:
        min_freq = _COMBO_MIN_FREQ
    try:
        from app.services.redis_client import execute, available
        if not available():
            return []
        key = f"tool_combos:{tenant_id}"
        raw = execute("HGETALL", key)
        if not raw or not isinstance(raw, list):
            return []
        # HGETALL: [k1, v1, k2, v2, ...]
        combos = []
        for i in range(0, len(raw), 2):
            count = int(raw[i + 1])
            if count >= min_freq:
                combos.append((raw[i], count))
        combos.sort(key=lambda x: x[1], reverse=True)
        return combos[:20]
    except Exception:
        logger.debug("tool_tracker: get_combos failed", exc_info=True)
        return []


def build_combo_hint(tenant_id: str, active_tool_names: set[str]) -> str:
    """构建工具组合提示（注入到 system prompt）。

    只提示与当前活跃工具相关的高频组合。
    """
    combos = get_frequent_combos(tenant_id)
    if not combos:
        return ""

    hints: list[str] = []
    for combo_str, count in combos[:5]:
        tools = combo_str.split("→")
        # 至少一个工具在当前活跃集中
        if any(t in active_tool_names for t in tools):
            hints.append(f"  · {combo_str}（已成功 {count} 次）")

    if not hints:
        return ""
    return "\n\n[常用工具组合]\n" + "\n".join(hints)


# ── Per-Tool 可观测性（GTC OpenTelemetry 借鉴）──
# 提供全量工具性能概览，用于 admin dashboard 展示和性能优化决策

def get_all_tool_stats_summary(tenant_id: str) -> list[dict[str, Any]]:
    """获取租户下所有工具的性能统计汇总。

    返回按调用次数降序排列的工具列表，每个包含：
    - name: 工具名
    - calls: 总调用次数
    - successes / failures: 成功/失败次数
    - success_rate: 成功率（百分比）
    - avg_latency_ms: 平均延迟（毫秒）
    - last_error: 最近一次错误信息
    - last_error_at: 最近错误时间戳
    """
    if not tenant_id:
        return []
    try:
        from app.services.redis_client import execute, available
        if not available():
            return []
        # SCAN 所有 tool_stats:{tenant_id}:* 的 key
        cursor = "0"
        prefix = f"tool_stats:{tenant_id}:"
        all_keys: list[str] = []
        for _ in range(100):  # 安全上限，避免无限循环
            result = execute("SCAN", cursor, "MATCH", f"{prefix}*", "COUNT", "100")
            if not result or not isinstance(result, list) or len(result) < 2:
                break
            cursor = result[0]
            keys = result[1] if isinstance(result[1], list) else []
            all_keys.extend(keys)
            if cursor == "0":
                break

        if not all_keys:
            return []

        # 批量获取所有工具的统计
        from app.services.redis_client import pipeline as redis_pipeline
        commands = [["HGETALL", k] for k in all_keys]
        raw_results = redis_pipeline(commands)

        summaries: list[dict[str, Any]] = []
        for key, raw in zip(all_keys, raw_results):
            if not raw or not isinstance(raw, list):
                continue
            # 解析 HGETALL 结果
            stats: dict[str, str] = {}
            for i in range(0, len(raw), 2):
                stats[raw[i]] = raw[i + 1]

            tool_name = key[len(prefix):]  # 去掉前缀得到工具名
            calls = int(stats.get("calls", 0))
            successes = int(stats.get("successes", 0))
            failures = int(stats.get("failures", 0))
            total_latency = float(stats.get("total_latency_ms", 0))

            summaries.append({
                "name": tool_name,
                "calls": calls,
                "successes": successes,
                "failures": failures,
                "success_rate": round(successes / calls * 100, 1) if calls > 0 else 0,
                "avg_latency_ms": round(total_latency / calls, 1) if calls > 0 else 0,
                "last_error": stats.get("last_error", ""),
                "last_error_at": stats.get("last_error_at", ""),
            })

        # 按调用次数降序
        summaries.sort(key=lambda x: x["calls"], reverse=True)
        return summaries
    except Exception:
        logger.debug("tool_tracker: get_all_stats_summary failed", exc_info=True)
        return []
