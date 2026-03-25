"""Cron Agent 调度器 —— NanoClaw 启发的定时 Agent 任务

核心理念（借鉴 NanoClaw）：
- 不只是定时提醒，而是定时运行完整的 AI Agent 任务
- Cron 风格的 schedule 表达式（"0 9 * * 1" = 每周一上午 9 点）
- 每个任务运行一个完整的 Agent loop（有工具访问权限）
- 执行结果自动回消息给用户

用例：
- 每日代码审查：每天早上扫描 GitHub PR，生成审查摘要
- 每周报告：每周五下午汇总本周工作，生成周报
- 竞品监控：每天搜索竞品动态，有重大更新时通知
- 数据同步：定时从外部 API 拉取数据，更新飞书多维表格

存储（Redis）：
    cron_agents:{tenant_id}  → HASH { agent_id → JSON config }
    cron_log:{tenant_id}     → LIST（最近 50 条执行日志）
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.services import redis_client

logger = logging.getLogger(__name__)

# ── 配置 ──
_CHECK_INTERVAL = 60        # 检查间隔（秒）
_MAX_EXECUTION_TIME = 300   # 单个 agent 任务最大执行时间（秒）
_MAX_LOG_ENTRIES = 50       # 每个租户保留的日志条数
_running = False


@dataclass
class CronAgentConfig:
    """定时 Agent 任务配置。"""
    agent_id: str = ""
    name: str = ""                  # 人类可读名称（如 "每日代码审查"）
    cron_expr: str = ""             # Cron 表达式（"0 9 * * 1-5" = 工作日上午 9 点）
    prompt: str = ""                # Agent 执行的 prompt（如 "审查今天的 PR 并汇总"）
    tool_groups: list[str] = field(default_factory=list)  # 可用工具组 ["code_dev", "feishu_collab"]
    notify_user_id: str = ""        # 执行完毕通知的用户 ID
    notify_chat_id: str = ""        # 执行完毕通知的群聊 ID
    enabled: bool = True
    timezone: str = "Asia/Shanghai"
    last_run: float = 0             # 上次执行的 Unix 时间戳
    created_at: float = 0
    created_by: str = ""            # 创建者 user_id

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> CronAgentConfig:
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known_fields}
        return cls(**filtered)


# ── Cron 表达式解析（轻量实现，不依赖外部库） ──

def _parse_cron_field(field_str: str, min_val: int, max_val: int) -> set[int]:
    """解析单个 cron 字段，返回匹配的值集合。

    支持: *, N, N-M, N-M/S, */S, N,M,O
    """
    values: set[int] = set()
    for part in field_str.split(","):
        part = part.strip()
        if part == "*":
            values.update(range(min_val, max_val + 1))
        elif "/" in part:
            base, step_str = part.split("/", 1)
            step = int(step_str)
            if base == "*":
                start = min_val
                end = max_val
            elif "-" in base:
                start, end = map(int, base.split("-", 1))
            else:
                start = int(base)
                end = max_val
            values.update(range(start, end + 1, step))
        elif "-" in part:
            start, end = map(int, part.split("-", 1))
            values.update(range(start, end + 1))
        else:
            values.add(int(part))
    return values


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    """检查 cron 表达式是否匹配给定的 datetime。

    标准 5 字段格式: minute hour day_of_month month day_of_week
    day_of_week: 0=Sunday, 1=Monday, ..., 6=Saturday
    """
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return False

    try:
        minutes = _parse_cron_field(parts[0], 0, 59)
        hours = _parse_cron_field(parts[1], 0, 23)
        days = _parse_cron_field(parts[2], 1, 31)
        months = _parse_cron_field(parts[3], 1, 12)
        weekdays = _parse_cron_field(parts[4], 0, 6)
    except (ValueError, IndexError):
        return False

    # Python isoweekday: Monday=1 ... Sunday=7; cron: Sunday=0
    cron_dow = dt.isoweekday() % 7  # Monday=1→1, Sunday=7→0

    return (
        dt.minute in minutes
        and dt.hour in hours
        and dt.day in days
        and dt.month in months
        and cron_dow in weekdays
    )


# ── Redis 操作 ──

def _agents_key(tenant_id: str) -> str:
    return f"cron_agents:{tenant_id}"


def _log_key(tenant_id: str) -> str:
    return f"cron_log:{tenant_id}"


def save_cron_agent(tenant_id: str, config: CronAgentConfig) -> bool:
    """保存/更新一个定时 Agent 任务。"""
    if not config.agent_id:
        config.agent_id = str(uuid.uuid4())[:8]
    if not config.created_at:
        config.created_at = time.time()

    result = redis_client.execute(
        "HSET", _agents_key(tenant_id),
        config.agent_id, json.dumps(config.to_dict(), ensure_ascii=False)
    )
    return result is not None


def delete_cron_agent(tenant_id: str, agent_id: str) -> bool:
    """删除一个定时 Agent 任务。"""
    result = redis_client.execute("HDEL", _agents_key(tenant_id), agent_id)
    return result == 1


def list_cron_agents(tenant_id: str) -> list[CronAgentConfig]:
    """列出租户的所有定时 Agent 任务。"""
    raw = redis_client.execute("HGETALL", _agents_key(tenant_id))
    if not raw or not isinstance(raw, dict):
        return []

    agents = []
    for agent_id, json_str in raw.items():
        try:
            d = json.loads(json_str)
            agents.append(CronAgentConfig.from_dict(d))
        except (json.JSONDecodeError, TypeError):
            pass
    return agents


def get_cron_agent(tenant_id: str, agent_id: str) -> CronAgentConfig | None:
    """获取一个定时 Agent 任务。"""
    raw = redis_client.execute("HGET", _agents_key(tenant_id), agent_id)
    if not raw:
        return None
    try:
        return CronAgentConfig.from_dict(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return None


def _log_execution(tenant_id: str, entry: dict) -> None:
    """记录一次执行日志。"""
    entry.setdefault("ts", time.time())
    redis_client.pipeline([
        ["RPUSH", _log_key(tenant_id), json.dumps(entry, ensure_ascii=False)],
        ["LTRIM", _log_key(tenant_id), str(-_MAX_LOG_ENTRIES), str(-1)],
    ])


def get_execution_log(tenant_id: str, limit: int = 20) -> list[dict]:
    """获取最近的执行日志。"""
    raw = redis_client.execute("LRANGE", _log_key(tenant_id), str(-limit), str(-1))
    if not raw or not isinstance(raw, list):
        return []
    entries = []
    for item in raw:
        try:
            entries.append(json.loads(item))
        except (json.JSONDecodeError, TypeError):
            pass
    return entries


# ── Agent 执行 ──

async def _execute_cron_agent(tenant_id: str, config: CronAgentConfig) -> dict:
    """执行一个定时 Agent 任务。

    设置租户上下文 → 构建 agent prompt → 运行 agent loop → 发送结果通知
    """
    from app.tenant.context import set_current_tenant
    from app.tenant.registry import tenant_registry

    tenant = tenant_registry.get(tenant_id)
    if not tenant:
        return {"success": False, "error": f"tenant not found: {tenant_id}"}

    set_current_tenant(tenant)

    start = time.time()
    result_text = ""
    success = False

    try:
        # 构建 agent prompt
        agent_prompt = (
            f"[定时任务] {config.name}\n"
            f"任务指令: {config.prompt}\n"
            f"执行时间: {datetime.now(ZoneInfo(config.timezone)).strftime('%Y-%m-%d %H:%M')}\n"
            f"请执行以上任务并给出结果摘要。"
        )

        # 使用 Gemini agent（主 provider）
        from app.services.gemini_provider import run_gemini_agent
        result_text = await asyncio.wait_for(
            run_gemini_agent(
                user_text=agent_prompt,
                sender_id=config.created_by or "cron_scheduler",
                sender_name="定时任务",
                history=[],
                tool_groups=set(config.tool_groups) if config.tool_groups else None,
            ),
            timeout=_MAX_EXECUTION_TIME,
        )
        success = True

    except asyncio.TimeoutError:
        result_text = f"定时任务 [{config.name}] 执行超时（{_MAX_EXECUTION_TIME}秒）"
        logger.warning("cron agent timeout: %s/%s", tenant_id, config.agent_id)
    except Exception as e:
        result_text = f"定时任务 [{config.name}] 执行失败: {e}"
        logger.warning("cron agent error: %s/%s: %s", tenant_id, config.agent_id, e, exc_info=True)

    duration = time.time() - start

    # 发送结果通知
    if config.notify_chat_id or config.notify_user_id:
        try:
            await _send_notification(tenant, config, result_text)
        except Exception as e:
            logger.warning("cron agent notification failed: %s", e)

    # 更新 last_run
    config.last_run = time.time()
    save_cron_agent(tenant_id, config)

    # 记录日志
    _log_execution(tenant_id, {
        "agent_id": config.agent_id,
        "name": config.name,
        "success": success,
        "duration_s": round(duration, 1),
        "result_preview": result_text[:200] if result_text else "",
    })

    return {
        "success": success,
        "duration_s": round(duration, 1),
        "result": result_text,
    }


async def _send_notification(tenant, config: CronAgentConfig, text: str) -> None:
    """通过平台消息发送任务执行结果。"""
    if not text:
        return

    # 截断过长的结果
    if len(text) > 2000:
        text = text[:2000] + "\n...(结果过长已截断)"

    notify_text = f"📋 定时任务完成: {config.name}\n\n{text}"

    if tenant.platform == "feishu":
        from app.tools.feishu_api import feishu_post
        if config.notify_chat_id:
            await asyncio.to_thread(
                feishu_post,
                "/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                json={
                    "receive_id": config.notify_chat_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": notify_text}),
                },
            )
        elif config.notify_user_id:
            await asyncio.to_thread(
                feishu_post,
                "/im/v1/messages",
                params={"receive_id_type": "open_id"},
                json={
                    "receive_id": config.notify_user_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": notify_text}),
                },
            )
    elif tenant.platform in ("wecom_kf", "wecom"):
        from app.services.wecom_kf import wecom_kf_client
        if config.notify_user_id:
            await wecom_kf_client.send_text(config.notify_user_id, notify_text)


# ── 主调度循环 ──

async def start_cron_scheduler() -> None:
    """启动 Cron Agent 调度器后台任务。

    每 60 秒扫描所有租户的 cron agents，检查是否有到期任务。
    """
    global _running
    if _running:
        return
    _running = True

    logger.info("cron_agent scheduler started")

    while _running:
        try:
            await _check_and_execute()
        except Exception:
            logger.warning("cron scheduler loop error", exc_info=True)

        await asyncio.sleep(_CHECK_INTERVAL)


async def _check_and_execute() -> None:
    """检查所有租户的 cron agents，执行到期任务。"""
    from app.tenant.registry import tenant_registry

    for tenant_id, tenant in tenant_registry.all_tenants().items():
        agents = list_cron_agents(tenant_id)
        if not agents:
            continue

        for agent_config in agents:
            if not agent_config.enabled:
                continue

            tz = ZoneInfo(agent_config.timezone or "Asia/Shanghai")
            now = datetime.now(tz)

            if not cron_matches(agent_config.cron_expr, now):
                continue

            # 防止同一分钟内重复执行（last_run 在同一分钟内 = 已执行）
            if agent_config.last_run:
                last_run_dt = datetime.fromtimestamp(agent_config.last_run, tz=tz)
                if (last_run_dt.year == now.year and last_run_dt.month == now.month
                        and last_run_dt.day == now.day and last_run_dt.hour == now.hour
                        and last_run_dt.minute == now.minute):
                    continue

            logger.info("cron agent triggered: %s/%s [%s]", tenant_id, agent_config.agent_id, agent_config.name)

            # 异步执行，不阻塞调度循环
            asyncio.create_task(
                _execute_cron_agent(tenant_id, agent_config)
            )


def stop_cron_scheduler() -> None:
    """停止调度器。"""
    global _running
    _running = False
    logger.info("cron_agent scheduler stopped")
