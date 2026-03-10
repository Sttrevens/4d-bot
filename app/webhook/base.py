"""Webhook handler 共享基础设施

三个平台 handler（feishu/wecom/wecom_kf）的共享逻辑提取到此处，
消除重复代码、统一行为、降低维护成本。

提供:
- UserStateManager: 用户状态管理（锁、模式、活跃追踪、信箱）
- MessageDedup: 消息去重
- split_reply(): 回复分段
- tuk(): 租户隔离用户 key
- MODE_COMMANDS: 模式命令映射
- truncate_text(): 截断过长文本
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from collections import OrderedDict, defaultdict
from typing import Callable, Awaitable

from app.tenant.context import get_current_tenant

logger = logging.getLogger(__name__)

# ── 共享常量 ──

# 模式命令映射
MODE_COMMANDS: dict[str, str] = {
    "/full": "full_access",
    "/safe": "safe",
    "/yolo": "full_access",
}

# 默认超时和限制
DEFAULT_PROCESS_TIMEOUT = 600  # 10 分钟
DEFAULT_MAX_USER_TEXT_LEN = 8000
DEFAULT_STALE_MSG_THRESHOLD = 300  # 5 分钟


# ── 工具函数 ──

def tuk(sender_id: str) -> str:
    """生成租户隔离的用户 key（tenant_id:sender_id），防止跨租户状态串扰。

    不同租户可能存在相同 sender_id（如飞书 vs 企微），
    不加前缀会导致锁、模式、信箱等用户状态互相覆盖。
    """
    tenant = get_current_tenant()
    return f"{tenant.tenant_id}:{sender_id}"


def split_reply(text: str, max_len: int = 2000) -> list[str]:
    """将回复按消息限制分段（在换行处断开，避免硬切断句子）。

    适用于企微/企微客服等有消息长度限制的平台。
    飞书使用 bubble 方式发送，不使用此函数。
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # 尽量在换行处断开
        cut = text.rfind("\n", 0, max_len)
        if cut < max_len // 2:
            cut = max_len
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


def truncate_text(text: str, max_len: int, label: str = "文本") -> str:
    """截断过长文本，附加截断提示。"""
    if len(text) <= max_len:
        return text
    logger.warning("%s truncated: %d -> %d chars", label, len(text), max_len)
    return text[:max_len] + f"\n\n... ({label}过长已截断，原文共 {len(text)} 字符)"


# ── 消息去重 ──

class MessageDedup:
    """消息去重器，支持两种策略：

    - TTL 模式（OrderedDict）：按时间戳清理过期条目，适合飞书 event_id
    - Set 模式：简单 FIFO 清理，适合企微 msg_id
    """

    def __init__(self, max_cache: int = 2048, ttl: float = 0):
        """
        Args:
            max_cache: 最大缓存条目数
            ttl: TTL 秒数，>0 启用 TTL 模式（OrderedDict），0 使用 Set 模式
        """
        self._max_cache = max_cache
        self._ttl = ttl
        if ttl > 0:
            self._store: OrderedDict[str, float] = OrderedDict()
        else:
            self._store_set: set[str] = set()

    def is_duplicate(self, msg_id: str) -> bool:
        """检查是否重复。如果不重复则自动记录，返回 True 表示重复应跳过。"""
        if not msg_id:
            return False

        if self._ttl > 0:
            return self._check_ttl(msg_id)
        return self._check_set(msg_id)

    def _check_ttl(self, msg_id: str) -> bool:
        """TTL 模式去重（OrderedDict + 时间戳）"""
        now = _time.monotonic()
        if msg_id in self._store:
            return True
        self._store[msg_id] = now
        # 清理过期条目
        while self._store:
            oldest_id, oldest_ts = next(iter(self._store.items()))
            if now - oldest_ts > self._ttl:
                self._store.pop(oldest_id)
            else:
                break
        # 硬上限兜底
        while len(self._store) > self._max_cache:
            self._store.popitem(last=False)
        return False

    def _check_set(self, msg_id: str) -> bool:
        """Set 模式去重（简单 FIFO）"""
        if msg_id in self._store_set:
            return True
        self._store_set.add(msg_id)
        if len(self._store_set) > self._max_cache:
            to_remove = list(self._store_set)[:self._max_cache // 2]
            for mid in to_remove:
                self._store_set.discard(mid)
        return False


# ── 用户状态管理 ──

class UserStateManager:
    """管理用户级别的状态（锁、模式、活跃追踪、信箱），所有状态以 tuk 为 key。

    每个平台 handler 实例化一个 UserStateManager，共享相同的接口。
    """

    def __init__(
        self,
        mode_ttl: float = 7200,
        lock_idle_ttl: float = 3600,
        persist_mode: bool = False,
    ):
        """
        Args:
            mode_ttl: 模式自动重置时间（秒），0 = 不重置
            lock_idle_ttl: 锁闲置清理时间（秒），0 = 不清理
            persist_mode: 是否持久化模式到 Redis
        """
        self._user_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._user_lock_last_used: dict[str, float] = {}
        self._user_modes: dict[str, str] = {}
        self._user_mode_ts: dict[str, float] = {}

        # 活跃追踪 + 实时信箱（可选，飞书需要，企微不需要）
        self._user_active: set[str] = set()
        self._user_inboxes: dict[str, asyncio.Queue[dict]] = {}

        self._mode_ttl = mode_ttl
        self._lock_idle_ttl = lock_idle_ttl
        self._persist_mode = persist_mode

    # ── 锁 ──

    def get_lock(self, sender_id: str) -> asyncio.Lock:
        """获取用户锁并更新最后使用时间。"""
        uk = tuk(sender_id)
        self._user_lock_last_used[uk] = _time.monotonic()
        return self._user_locks[uk]

    # ── 模式 ──

    def get_mode(self, sender_id: str) -> str:
        """获取用户模式，默认 safe。"""
        return self._user_modes.get(tuk(sender_id), "safe")

    def set_mode(self, sender_id: str, mode: str) -> None:
        """设置用户模式。"""
        uk = tuk(sender_id)
        self._user_modes[uk] = mode
        self._user_mode_ts[uk] = _time.monotonic()
        if self._persist_mode:
            self._persist_mode_to_redis(uk, mode)

    def _persist_mode_to_redis(self, uk: str, mode: str) -> None:
        """持久化模式到 Redis。"""
        try:
            from app.services import redis_client as redis
            if redis.available():
                redis.execute("HSET", "bot:user_modes", uk, mode)
                redis.execute("EXPIRE", "bot:user_modes", str(int(self._mode_ttl)))
        except Exception:
            pass

    def load_modes_from_redis(self) -> int:
        """从 Redis 恢复用户模式（启动时调用）。返回恢复数量。"""
        try:
            from app.services import redis_client as redis
            if not redis.available():
                return 0
            data = redis.execute("HGETALL", "bot:user_modes")
            if not data:
                return 0
            if isinstance(data, list):
                for i in range(0, len(data) - 1, 2):
                    uk = data[i]
                    mode = data[i + 1]
                    if mode in ("full_access", "safe"):
                        self._user_modes[uk] = mode
                        self._user_mode_ts[uk] = _time.monotonic()
                return len(data) // 2
        except Exception:
            logger.warning("load_modes_from_redis failed", exc_info=True)
        return 0

    # ── 活跃追踪 + 信箱 ──

    def is_active(self, sender_id: str) -> bool:
        """检查用户是否正在被处理。"""
        return tuk(sender_id) in self._user_active

    def get_inbox(self, sender_id: str) -> asyncio.Queue[dict] | None:
        """获取用户信箱（如果活跃）。"""
        return self._user_inboxes.get(tuk(sender_id))

    def activate(self, sender_id: str) -> asyncio.Queue[dict]:
        """标记用户为活跃并创建信箱。返回信箱 Queue。"""
        uk = tuk(sender_id)
        inbox: asyncio.Queue[dict] = asyncio.Queue()
        self._user_inboxes[uk] = inbox
        self._user_active.add(uk)
        return inbox

    def deactivate(self, sender_id: str) -> None:
        """标记用户为非活跃，清理信箱。"""
        uk = tuk(sender_id)
        self._user_active.discard(uk)
        self._user_inboxes.pop(uk, None)

    # ── 清理 ──

    def cleanup_idle(self) -> None:
        """清理不活跃的用户状态（锁 + 过期模式），防止内存泄漏。"""
        now = _time.monotonic()

        # 清理空闲锁
        if self._lock_idle_ttl > 0:
            stale_locks = [
                uid for uid, ts in self._user_lock_last_used.items()
                if now - ts > self._lock_idle_ttl
                and uid not in self._user_active
                and uid in self._user_locks
                and not self._user_locks[uid].locked()
            ]
            for uid in stale_locks:
                self._user_locks.pop(uid, None)
                self._user_lock_last_used.pop(uid, None)
        else:
            stale_locks = []

        # 清理过期模式
        if self._mode_ttl > 0:
            stale_modes = [
                uid for uid, ts in self._user_mode_ts.items()
                if now - ts > self._mode_ttl
            ]
            for uid in stale_modes:
                self._user_modes.pop(uid, None)
                self._user_mode_ts.pop(uid, None)
        else:
            stale_modes = []

        if stale_locks or stale_modes:
            logger.debug("cleaned up %d idle locks, %d expired modes",
                         len(stale_locks), len(stale_modes))


# ── 处理模式命令 ──

async def handle_mode_command(
    cmd: str,
    sender_id: str,
    state: UserStateManager,
    reply_fn: Callable[[str], Awaitable[None]],
) -> bool:
    """处理模式相关的斜杠命令。

    Returns:
        True 如果命令已处理（调用方应 return），False 如果不是模式命令
    """
    cmd_lower = cmd.lower()

    if cmd_lower in MODE_COMMANDS:
        new_mode = MODE_COMMANDS[cmd_lower]
        state.set_mode(sender_id, new_mode)
        label = "Full Access" if new_mode == "full_access" else "Safe"
        await reply_fn(
            f"已切换到 {label} 模式。"
            + ("\n直接动手执行，不再确认。" if new_mode == "full_access"
               else "\n先分析方案再动手。")
        )
        return True

    if cmd_lower == "/mode":
        cur = state.get_mode(sender_id)
        label = "Full Access" if cur == "full_access" else "Safe"
        await reply_fn(
            f"当前模式: {label}\n发 /full 切换到 Full Access\n发 /safe 切换到 Safe"
        )
        return True

    return False


async def handle_status_command(
    sender_id: str,
    state: UserStateManager,
    platform_name: str,
    reply_fn: Callable[[str], Awaitable[None]],
    extra_lines: list[str] | None = None,
) -> None:
    """处理 /status 命令。"""
    tenant = get_current_tenant()
    model = tenant.llm_model or "default"
    mode = state.get_mode(sender_id)

    lines = [
        "=== Bot Status ===",
        f"平台: {platform_name}",
        f"租户: {tenant.name} ({tenant.tenant_id})",
        f"模式: {'Full Access' if mode == 'full_access' else 'Safe'}",
        f"LLM: {model}",
    ]

    if extra_lines:
        lines.extend(extra_lines)

    await reply_fn("\n".join(lines))
