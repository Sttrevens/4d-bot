"""Task Watchdog — 后台任务完成度监控

当 agent loop 异常退出（模型返回空、超时等）但任务未完成时，
watchdog 会在后台检测并尝试自动补救（重新投递任务）。

两层防线：
- Layer 1（同步）：gemini_provider 退出前的交付物检查（在 gemini_provider.py 中）
- Layer 2（异步）：本模块 — 后台扫描未完成任务，主动重试

设计原则：
- fail-open：Redis 不可用时不阻塞任何流程
- 最多重试 1 次：避免死循环
- 30 秒冷却期：给 Layer 1 留出时间，不过早介入
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

_WATCHDOG_INTERVAL = 60  # 扫描间隔（秒）(was 30; reduced to save Redis commands)
_TASK_TTL = 300  # 任务记录 TTL（秒），过期自动清理
_COOLDOWN = 30  # 任务记录后等多久再检查（秒），给 Layer 1 留时间
_MAX_RETRIES = 1  # 每个任务最多重试次数


@dataclass
class IncompleteTask:
    """记录一个可能未完成的任务"""
    tenant_id: str
    platform: str  # feishu / wecom_kf / wecom
    sender_id: str
    sender_name: str
    chat_id: str  # 飞书用；企微客服为空
    chat_type: str
    user_text: str
    tools_called: list[str]
    missing_deliverables: list[str]
    reply_sent: str  # 已发给用户的回复
    recorded_at: float
    retries: int = 0


def record_incomplete_task(task: IncompleteTask) -> None:
    """将未完成任务写入 Redis，供 watchdog 后台扫描。"""
    try:
        from app.services import redis_client as redis
        if not redis.available():
            return
        key = f"watchdog:task:{task.tenant_id}:{task.sender_id}:{int(task.recorded_at)}"
        redis.execute("SET", key, json.dumps(asdict(task), ensure_ascii=False), "EX", str(_TASK_TTL))
        logger.info(
            "watchdog: recorded incomplete task for sender=%s missing=%s",
            task.sender_id[:12], task.missing_deliverables,
        )
    except Exception:
        logger.debug("watchdog: failed to record task", exc_info=True)


def _scan_incomplete_tasks() -> list[tuple[str, IncompleteTask]]:
    """扫描 Redis 中所有未完成任务。返回 (key, task) 列表。"""
    try:
        from app.services import redis_client as redis
        if not redis.available():
            return []
        # SCAN 匹配 watchdog:task:* 前缀
        cursor = "0"
        tasks = []
        for _ in range(20):  # 最多扫 20 轮，防止大量 key 时阻塞
            result = redis.execute("SCAN", cursor, "MATCH", "watchdog:task:*", "COUNT", "50")
            if not result:
                break
            cursor, keys = result[0], result[1]
            for k in keys:
                raw = redis.execute("GET", k)
                if raw:
                    try:
                        data = json.loads(raw)
                        task = IncompleteTask(**data)
                        tasks.append((k, task))
                    except (json.JSONDecodeError, TypeError):
                        redis.execute("DEL", k)
            if cursor == "0" or cursor == b"0":
                break
        return tasks
    except Exception:
        logger.debug("watchdog: scan failed", exc_info=True)
        return []


async def _retry_task(task: IncompleteTask) -> None:
    """重新投递未完成任务。

    核心逻辑：
    1. 设置租户上下文
    2. 构建"继续完成"的指令
    3. 调用 route_message 重新处理
    4. 将结果发送给用户
    """
    from app.tenant.registry import tenant_registry
    from app.tenant.context import set_current_tenant

    tenant = tenant_registry.get(task.tenant_id)
    if not tenant:
        logger.warning("watchdog: tenant %s not found, skipping retry", task.tenant_id)
        return

    set_current_tenant(tenant)

    # 构建"继续"指令 — 让模型知道之前的情况
    retry_text = (
        f"[系统消息：你之前在处理用户的请求时，没有完成以下交付物的生成："
        f"{'、'.join(task.missing_deliverables)}。"
        f"请立即完成这些文件的生成。]\n"
        f"用户原始请求：{task.user_text}"
    )

    logger.info(
        "watchdog: retrying task for sender=%s tenant=%s missing=%s",
        task.sender_id[:12], task.tenant_id, task.missing_deliverables,
    )

    try:
        if task.platform == "feishu":
            await _retry_feishu(tenant, task, retry_text)
        elif task.platform == "wecom_kf":
            await _retry_wecom_kf(tenant, task, retry_text)
        else:
            logger.info("watchdog: unsupported platform %s, skipping", task.platform)
    except Exception:
        logger.warning("watchdog: retry failed for sender=%s", task.sender_id[:12], exc_info=True)


async def _retry_feishu(tenant, task: IncompleteTask, retry_text: str) -> None:
    """飞书平台重试：调用 route_message 后通过 feishu API 发消息。"""
    from app.router.intent import route_message
    from app.services.feishu import FeishuClient

    feishu = FeishuClient(tenant)

    async def _progress(msg: str) -> None:
        # 重试时不发进度消息，避免打扰用户
        pass

    reply = await asyncio.wait_for(
        route_message(
            user_text=retry_text,
            sender_id=task.sender_id,
            sender_name=task.sender_name,
            on_progress=_progress,
            mode="safe",
            chat_id=task.chat_id,
            chat_type=task.chat_type,
        ),
        timeout=120,
    )

    if reply and reply.strip():
        # 发送到用户的聊天
        if task.chat_id:
            await feishu.send_text(task.chat_id, reply)
        logger.info("watchdog: feishu retry completed, reply=%s", reply[:80])


async def _retry_wecom_kf(tenant, task: IncompleteTask, retry_text: str) -> None:
    """企微客服平台重试：调用 route_message 后通过 wecom_kf API 回复。"""
    from app.router.intent import route_message
    from app.services.wecom_kf import wecom_kf_client

    kf_client = wecom_kf_client

    async def _progress(msg: str) -> None:
        pass

    reply = await asyncio.wait_for(
        route_message(
            user_text=retry_text,
            sender_id=task.sender_id,
            sender_name=task.sender_name,
            on_progress=_progress,
            mode="safe",
        ),
        timeout=120,
    )

    if reply and reply.strip():
        await kf_client.reply_text(task.sender_id, reply)
        logger.info("watchdog: wecom_kf retry completed, reply=%s", reply[:80])


async def watchdog_loop() -> None:
    """后台 watchdog 循环：定期扫描未完成任务并重试。"""
    from app.services import redis_client as redis
    logger.info("watchdog: background loop started (interval=%ds)", _WATCHDOG_INTERVAL)

    while True:
        try:
            await asyncio.sleep(_WATCHDOG_INTERVAL)

            if not redis.available():
                continue

            tasks = _scan_incomplete_tasks()
            now = time.time()

            for key, task in tasks:
                # 冷却期未到 → 跳过（Layer 1 可能还在处理）
                if now - task.recorded_at < _COOLDOWN:
                    continue

                # 已重试过 → 删除，不再尝试
                if task.retries >= _MAX_RETRIES:
                    redis.execute("DEL", key)
                    logger.info("watchdog: task exhausted retries, removing key=%s", key)
                    continue

                # 执行重试
                task.retries += 1
                # 更新重试计数（防止并发重复重试）
                redis.execute(
                    "SET", key,
                    json.dumps(asdict(task), ensure_ascii=False),
                    "EX", str(_TASK_TTL),
                )

                try:
                    await _retry_task(task)
                except Exception:
                    logger.warning("watchdog: retry exception", exc_info=True)
                finally:
                    # 重试后删除任务（无论成功失败，只试一次）
                    redis.execute("DEL", key)

        except Exception:
            logger.debug("watchdog: loop iteration error", exc_info=True)
