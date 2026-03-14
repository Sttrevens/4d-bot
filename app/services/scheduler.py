"""自主调度器 —— 让 bot 能自己安排时间执行计划步骤

核心思路：
- 后台 asyncio 循环，每 60 秒检查一次有没有到期的任务
- 到期了就自动执行（调用 LLM agent），完成后通过对应平台发消息汇报
  （飞书用 send_message，企微客服用 wecom_kf.send_text — 48h 窗口内）
- bot 根据步骤复杂度和当前时间自主决定下一步什么时候执行

安全机制：
- 工作时间窗口（默认 9:00-21:00），深夜不执行不打扰
- 同一时间只跑一个自主任务（防止并发冲突）
- 单步超时 5 分钟
- 执行失败不会阻塞后续步骤（标记 blocked，通知用户）
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services import planner as bot_planner
from app.services import memory_store
from app.services import redis_client

logger = logging.getLogger(__name__)

# ── 配置 ──

_CHECK_INTERVAL = 120       # 计划步骤检查间隔（秒）(was 60→120; saves ~720 cmds/day per container)
_REMINDER_MAX_SLEEP = 1800  # 提醒循环最大 sleep（30 分钟，无提醒时）
_WORK_HOUR_START = 9        # 工作时间开始（点）
_WORK_HOUR_END = 21         # 工作时间结束（点）
_STEP_TIMEOUT = 300         # 单步执行超时（秒）
_running = False             # 调度器是否在运行
_exec_lock = asyncio.Lock()  # 同一时间只跑一个任务（替代 bool flag 避免竞态）


def _get_timezone() -> ZoneInfo:
    """获取当前租户的调度时区，回退到 Asia/Shanghai。"""
    try:
        from app.tenant.context import get_current_tenant
        tenant = get_current_tenant()
        if tenant.scheduler_timezone:
            return ZoneInfo(tenant.scheduler_timezone)
    except Exception:
        pass
    return ZoneInfo("Asia/Shanghai")


def _now_tz() -> datetime:
    """获取当前时间（租户时区，回退到 Asia/Shanghai）。"""
    return datetime.now(_get_timezone())


def _in_work_hours() -> bool:
    """是否在工作时间窗口内。"""
    now = _now_tz()
    return _WORK_HOUR_START <= now.hour < _WORK_HOUR_END


# ── 调度元数据 ──
# 存在 .bot-memory/schedule.json，格式：
# [{"plan_id": "...", "step_id": "...", "scheduled_at": "ISO时间", "notify_user_id": "..."}]


def get_pending_schedules() -> list[dict]:
    """获取所有待执行的调度项。"""
    data = memory_store.read_json("schedule")
    if not isinstance(data, list):
        return []
    return data


def add_schedule(
    plan_id: str,
    step_id: str,
    scheduled_at: str,
    notify_user_id: str = "",
    notify_user_name: str = "",
    tenant_id: str = "",
) -> bool:
    """添加一个调度项。scheduled_at 为 ISO 格式时间。"""
    schedules = get_pending_schedules()
    # 去重
    for s in schedules:
        if s.get("plan_id") == plan_id and s.get("step_id") == step_id:
            s["scheduled_at"] = scheduled_at
            s["notify_user_id"] = notify_user_id
            s["notify_user_name"] = notify_user_name
            if tenant_id:
                s["tenant_id"] = tenant_id
            return memory_store.write_json("schedule", schedules, "schedule: update")
    schedules.append({
        "plan_id": plan_id,
        "step_id": step_id,
        "scheduled_at": scheduled_at,
        "notify_user_id": notify_user_id,
        "notify_user_name": notify_user_name,
        "tenant_id": tenant_id,
    })
    return memory_store.write_json("schedule", schedules, "schedule: add")


def remove_schedule(plan_id: str, step_id: str) -> bool:
    """移除已执行/取消的调度项。"""
    schedules = get_pending_schedules()
    schedules = [s for s in schedules if not (s.get("plan_id") == plan_id and s.get("step_id") == step_id)]
    return memory_store.write_json("schedule", schedules, "schedule: remove")


def _is_due(scheduled_at: str) -> bool:
    """检查调度时间是否已到。"""
    try:
        target = datetime.fromisoformat(scheduled_at)
        now = _now_tz()
        # 如果 target 没有时区信息，假设是本地时区
        if target.tzinfo is None:
            target = target.replace(tzinfo=now.tzinfo)
        return now >= target
    except Exception:
        return False


# ── 自主执行 ──


async def _execute_step(plan_id: str, step_id: str, notify_user_id: str, notify_user_name: str) -> str:
    """执行计划的一个步骤：调用 LLM agent，完成后更新计划。"""
    plan = bot_planner.get_plan(plan_id)
    if not plan:
        return f"计划 {plan_id} 不存在"

    # 找到步骤
    step = None
    for s in plan.get("steps", []):
        if s["id"] == step_id:
            step = s
            break
    if not step:
        return f"步骤 {step_id} 不存在"

    if step["status"] == "completed":
        return f"步骤 {step_id} 已完成，跳过"

    # 构建执行 prompt
    step_title = step.get("title", "")
    step_desc = step.get("description", "")
    plan_title = plan.get("title", "")

    exec_prompt = (
        f"你正在自主执行计划「{plan_title}」的步骤。\n\n"
        f"当前步骤: {step_id} - {step_title}\n"
    )
    if step_desc:
        exec_prompt += f"步骤描述: {step_desc}\n"
    exec_prompt += (
        f"\n请执行这个步骤。完成后用 update_plan_step 标记为 completed 并写明结果。"
        f"如果遇到阻塞，标记为 blocked 并说明原因。"
    )

    # 调用 agent — 根据租户 LLM provider 选择正确的 handler
    from app.tenant.context import get_current_tenant
    tenant = get_current_tenant()
    if tenant.llm_provider == "gemini":
        from app.services.gemini_provider import handle_message
    else:
        from app.services.kimi_coder import handle_message

    try:
        result = await asyncio.wait_for(
            handle_message(
                user_text=exec_prompt,
                sender_name=notify_user_name or "scheduler",
                sender_id=notify_user_id or "system",
                mode="full_access",  # 自主执行不需要确认
            ),
            timeout=_STEP_TIMEOUT,
        )
    except asyncio.TimeoutError:
        result = f"步骤执行超时（{_STEP_TIMEOUT}秒）"
        bot_planner.update_step(plan_id, step_id, status="blocked", outcome=result)
    except Exception as exc:
        result = f"步骤执行异常: {exc}"
        bot_planner.update_step(plan_id, step_id, status="blocked", outcome=result)
        logger.exception("scheduler: step execution failed")

    return result


async def _notify_user(user_id: str, text: str) -> None:
    """根据当前租户平台发消息给用户。"""
    if not user_id:
        return
    try:
        from app.tenant.context import get_current_tenant
        tenant = get_current_tenant()
        platform = tenant.platform

        if platform == "wecom_kf":
            # 企微客服：48h 内可主动发消息
            from app.services.wecom_kf import wecom_kf_client
            await wecom_kf_client.send_text(user_id, text)
        elif platform == "wecom":
            from app.services.wecom import wecom_client
            await wecom_client.send_text(user_id, text)
        else:
            # 飞书
            from app.tools.message_ops import send_message
            loop = asyncio.get_running_loop()
            ctx = contextvars.copy_context()
            await loop.run_in_executor(None, ctx.run, send_message, user_id, text, "open_id", "text")
    except Exception:
        logger.warning("scheduler: notify user %s failed", user_id[:12], exc_info=True)


async def _process_due_schedules() -> None:
    """检查并执行所有到期的调度项。"""
    if _exec_lock.locked():
        return

    if not _in_work_hours():
        return

    schedules = get_pending_schedules()
    due_items = [s for s in schedules if _is_due(s.get("scheduled_at", ""))]

    if not due_items:
        return

    async with _exec_lock:
        from app.tenant.registry import tenant_registry
        from app.tenant.context import set_current_tenant

        for item in due_items:
            # 设置正确的租户上下文（优先用 schedule 项中记录的 tenant_id）
            item_tenant_id = item.get("tenant_id", "")
            if item_tenant_id:
                target_tenant = tenant_registry.get(item_tenant_id)
            else:
                # 兼容旧数据：没有 tenant_id 的 schedule 项，回退到飞书租户
                target_tenant = None
                for t in tenant_registry.all_tenants().values():
                    if t.platform == "feishu" and t.app_id and t.app_secret:
                        target_tenant = t
                        break
            set_current_tenant(target_tenant or tenant_registry.get_default())

            plan_id = item.get("plan_id", "")
            step_id = item.get("step_id", "")
            user_id = item.get("notify_user_id", "")
            user_name = item.get("notify_user_name", "")

            logger.info("scheduler: executing %s/%s", plan_id, step_id)

            # 通知用户开始执行
            plan = bot_planner.get_plan(plan_id)
            plan_title = plan.get("title", "?") if plan else "?"
            step_info = ""
            if plan:
                for s in plan.get("steps", []):
                    if s["id"] == step_id:
                        step_info = s.get("title", "")
                        break

            await _notify_user(
                user_id,
                f"开始自动执行计划「{plan_title}」\n步骤: {step_info}\n\n执行中，完成后会通知你。",
            )

            # 执行
            try:
                result = await _execute_step(plan_id, step_id, user_id, user_name)
            except Exception:
                logger.exception("scheduler: step %s/%s failed", plan_id, step_id)
                result = "步骤执行异常"

            # 移除调度
            remove_schedule(plan_id, step_id)

            # 通知用户结果
            result_short = result[:500] if result else "完成"
            await _notify_user(
                user_id,
                f"计划「{plan_title}」步骤完成\n\n{result_short}",
            )

            # 检查是否还有下一步，自动安排
            updated_plan = bot_planner.get_plan(plan_id)
            if updated_plan and updated_plan.get("status") == "active":
                _auto_schedule_next(updated_plan, user_id, user_name, item_tenant_id)


def _auto_schedule_next(plan: dict, user_id: str, user_name: str, tenant_id: str = "") -> None:
    """自动安排计划的下一个步骤。

    根据步骤复杂度和当前时间智能安排：
    - 简单步骤（标题短、无复杂描述）：30 分钟后
    - 中等步骤：2 小时后
    - 复杂步骤（描述长、有"重构/迁移/设计"关键词）：次日上午
    """
    for step in plan.get("steps", []):
        if step["status"] in ("pending", "in_progress"):
            step_id = step["id"]
            plan_id = plan["plan_id"]

            # 判断复杂度
            delay_minutes = _estimate_delay(step)

            now = _now_tz()
            from datetime import timedelta
            target = now + timedelta(minutes=delay_minutes)

            # 如果目标时间超出今天工作时间，推到明天 9:30
            if target.hour >= _WORK_HOUR_END:
                target = target.replace(hour=9, minute=30, second=0, microsecond=0)
                target += timedelta(days=1)
                # 跳过周末（简单处理）
                while target.weekday() >= 5:  # 5=Saturday, 6=Sunday
                    target += timedelta(days=1)

            add_schedule(
                plan_id=plan_id,
                step_id=step_id,
                scheduled_at=target.isoformat(),
                notify_user_id=user_id,
                notify_user_name=user_name,
                tenant_id=tenant_id,
            )

            logger.info(
                "scheduler: auto-scheduled %s/%s at %s (delay=%dm)",
                plan_id, step_id, target.isoformat(), delay_minutes,
            )
            return  # 只安排一步


def _estimate_delay(step: dict) -> int:
    """根据步骤内容估算延迟分钟数。"""
    title = step.get("title", "")
    desc = step.get("description", "")
    combined = f"{title} {desc}".lower()

    # 复杂关键词
    complex_keywords = {"重构", "迁移", "设计", "架构", "refactor", "migrate", "design"}
    if any(kw in combined for kw in complex_keywords):
        return 240  # 4 小时

    # 中等复杂度
    if len(desc) > 100 or len(title) > 30:
        return 120  # 2 小时

    # 简单
    return 30


# ── 记忆自组织（空闲时经验蒸馏）──

_DISTILL_INTERVAL = 3600 * 6  # 6 小时检查一次
_last_distill_ts: float = 0


async def _maybe_distill_memory() -> None:
    """空闲时触发记忆蒸馏（GenericAgent 式自组织）。

    条件：工作时间内 + 无待执行任务 + 距上次蒸馏 >6 小时。
    遍历所有租户，逐个蒸馏。
    """
    global _last_distill_ts

    if not _in_work_hours():
        return
    if time.time() - _last_distill_ts < _DISTILL_INTERVAL:
        return
    # 有待执行任务时不蒸馏（优先执行任务）
    if get_pending_schedules():
        return

    _last_distill_ts = time.time()
    logger.info("scheduler: starting idle-time memory distillation")

    try:
        from app.tenant.registry import tenant_registry
        from app.tenant.context import set_current_tenant
        from app.services.memory import distill_experience

        for tid, tenant in tenant_registry.all_tenants().items():
            # 只对启用日记的租户蒸馏
            if not getattr(tenant, "memory_diary_enabled", True):
                continue
            try:
                set_current_tenant(tenant)
                rules = await asyncio.wait_for(distill_experience(), timeout=30)
                if rules:
                    logger.info("scheduler: distilled %d rules for %s", len(rules), tid)
            except asyncio.TimeoutError:
                logger.debug("scheduler: distill timeout for %s", tid)
            except Exception:
                logger.debug("scheduler: distill failed for %s", tid, exc_info=True)
    except Exception:
        logger.debug("scheduler: memory distillation failed", exc_info=True)


# ── 提醒系统 ──

_ACTION_MAX_RETRIES = 2  # 最多重试 2 次（共执行 3 次）
_ACTION_RETRY_DELAY = 10  # 重试间隔秒数


async def _execute_action_with_retry(
    tenant, prompt: str, reminder: dict, user_id: str, text: str, action: str,
) -> str | None:
    """执行 reminder action，失败时重试最多 _ACTION_MAX_RETRIES 次。"""
    if tenant.llm_provider == "gemini":
        from app.services.gemini_provider import handle_message
    else:
        from app.services.kimi_coder import handle_message

    rem_id = reminder.get("id", "?")
    last_err = None

    for attempt in range(_ACTION_MAX_RETRIES + 1):
        try:
            result = await asyncio.wait_for(
                handle_message(
                    user_text=prompt,
                    sender_name=reminder.get("user_name", "scheduler"),
                    sender_id=user_id or "system",
                    mode="full_access",
                ),
                timeout=_STEP_TIMEOUT,
            )
            return result
        except asyncio.TimeoutError:
            last_err = "timeout"
            logger.warning("reminder: action timeout for %s (attempt %d/%d)",
                           rem_id, attempt + 1, _ACTION_MAX_RETRIES + 1)
        except Exception as exc:
            last_err = str(exc)
            logger.warning("reminder: action failed for %s (attempt %d/%d): %s",
                           rem_id, attempt + 1, _ACTION_MAX_RETRIES + 1, exc)

        if attempt < _ACTION_MAX_RETRIES:
            await asyncio.sleep(_ACTION_RETRY_DELAY)

    # 所有重试都失败
    await _notify_user(user_id, f"⏰ 提醒：{text}\n\n（动作执行失败，已重试{_ACTION_MAX_RETRIES}次：{last_err}。请手动处理：{action}）")
    return None


def _log_execution(tenant_id: str, reminder_id: str, action: str, success: bool) -> None:
    """记录提醒执行日志到 Redis（保留最近 100 条）。"""
    try:
        import json as _json
        log_key = f"reminder_log:{tenant_id}"
        entry = _json.dumps({
            "id": reminder_id,
            "action": action[:100] if action else "",
            "success": success,
            "ts": time.time(),
        }, ensure_ascii=False)
        redis_client.execute("LPUSH", log_key, entry)
        redis_client.execute("LTRIM", log_key, "0", "99")
    except Exception:
        logger.debug("reminder: failed to log execution", exc_info=True)


async def _process_due_reminders() -> None:
    """检查并处理所有租户的到期提醒。

    不受工作时间限制——提醒是用户主动设的，深夜的提醒也要准时发。
    """
    from app.tenant.registry import tenant_registry
    from app.tenant.context import set_current_tenant
    from app.tools.reminder_ops import (
        get_due_reminders, _remove_reminder_by_member,
        _save_reminder, calc_next_trigger, _parse_time,
        migrate_legacy_key,
    )

    for tid, tenant in tenant_registry.all_tenants().items():
        try:
            set_current_tenant(tenant)
        except Exception:
            continue

        # 一次性迁移旧 key（reminders:{tid} → reminders:{tid}:{uid}）
        try:
            migrate_legacy_key(tid)
        except Exception:
            logger.debug("reminder: legacy migration skipped for %s", tid)

        due = get_due_reminders(tid)
        if not due:
            continue

        for reminder, raw_member in due:
            user_id = reminder.get("user_id", "")
            text = reminder.get("text", "")
            action = reminder.get("action", "")
            rem_id = reminder.get("id", "?")

            logger.info("reminder: triggered %s for user %s — %s",
                        rem_id, user_id[:12] if user_id else "?", text[:50])

            # ── crash safety: 先调度下次触发，再删旧的，最后执行 ──
            # 这样即使执行中 crash，recurring chain 不会断
            recurrence = reminder.get("recurrence", {})
            is_recurring = isinstance(recurrence, dict) and recurrence.get("type", "none") != "none"

            if is_recurring:
                current_trigger = _parse_time(reminder.get("next_trigger", ""))
                next_dt = calc_next_trigger(recurrence, after=current_trigger)
                if next_dt:
                    next_reminder = {**reminder, "next_trigger": next_dt.isoformat()}
                    _save_reminder(tid, next_reminder)
                    logger.info("reminder: recurring %s rescheduled to %s (before execution)",
                                rem_id, next_dt.isoformat())

            # 删除当前到期的（旧 score 的 member）
            _remove_reminder_by_member(tid, user_id, raw_member)

            # ── 执行 ──
            if action:
                # 带动作：调 LLM agent 执行（带重试）
                prompt = (
                    f"定时提醒触发！\n"
                    f"提醒内容：{text}\n"
                    f"需要执行的动作：{action}\n\n"
                    f"请执行上述动作，完成后把结果和提醒内容一起告诉用户。\n"
                    f"重要：如果动作涉及小红书/XHS，先用 xhs_check_login 检查登录状态，"
                    f"未登录则通知用户需要重新扫码登录，不要盲目尝试发帖。"
                )

                result = await _execute_action_with_retry(
                    tenant, prompt, reminder, user_id, text, action,
                )

                if result and user_id:
                    await _notify_user(user_id, f"⏰ 提醒：{text}\n\n{result[:800]}")
            else:
                # 纯文本提醒
                await _notify_user(user_id, f"⏰ 提醒：{text}")

            # 记录执行日志
            _log_execution(tid, rem_id, action, bool(action))


async def _get_global_nearest_ts() -> float | None:
    """获取所有租户中最近的提醒触发时间戳。"""
    from app.tenant.registry import tenant_registry
    from app.tools.reminder_ops import get_nearest_trigger_ts

    nearest = None
    for tid in tenant_registry.all_tenants():
        ts = get_nearest_trigger_ts(tid)
        if ts is not None:
            if nearest is None or ts < nearest:
                nearest = ts
    return nearest


async def _reminder_loop() -> None:
    """提醒系统独立循环——动态 sleep。

    无提醒时 30 分钟一查（几乎零成本）；
    有即将到期的提醒时精确 sleep 到触发时刻。
    """
    logger.info("reminder: loop started (max sleep %ds)", _REMINDER_MAX_SLEEP)

    while _running:
        try:
            await _process_due_reminders()
        except Exception:
            logger.exception("reminder: processing failed")

        # 动态 sleep：找最近的提醒，sleep 到它到期
        try:
            nearest_ts = await _get_global_nearest_ts()
        except Exception:
            nearest_ts = None

        if nearest_ts is not None:
            wait = max(nearest_ts - time.time(), 1)  # 至少 sleep 1 秒防忙等
            sleep_time = min(wait, _REMINDER_MAX_SLEEP)
        else:
            sleep_time = _REMINDER_MAX_SLEEP

        logger.debug("reminder: sleeping %.0fs", sleep_time)
        await asyncio.sleep(sleep_time)


# ── 调度器主循环 ──

async def _scheduler_loop() -> None:
    """后台调度循环。"""
    global _running
    _running = True
    logger.info("scheduler: started (check every %ds, work hours %d:00-%d:00)",
                _CHECK_INTERVAL, _WORK_HOUR_START, _WORK_HOUR_END)

    _cycle = 0
    while _running:
        try:
            await _process_due_schedules()
        except Exception:
            logger.exception("scheduler: loop iteration failed")

        # 每 10 个循环（~10 分钟）检查一次是否需要蒸馏
        _cycle += 1
        if _cycle % 10 == 0:
            try:
                await _maybe_distill_memory()
            except Exception:
                logger.debug("scheduler: distill check failed", exc_info=True)

        await asyncio.sleep(_CHECK_INTERVAL)


def start_scheduler() -> None:
    """启动后台调度器（在 app startup 中调用）。"""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_scheduler_loop())
        loop.create_task(_reminder_loop())
        logger.info("scheduler: background tasks created (plan scheduler + reminder loop)")
    except RuntimeError:
        logger.warning("scheduler: no running event loop, cannot start")


def stop_scheduler() -> None:
    """停止调度器。"""
    global _running
    _running = False
    logger.info("scheduler: stopped")


# ── 重启恢复 ──


def recover_stale_steps() -> int:
    """重启后扫描所有活跃计划，把卡在 in_progress 的步骤重置为 pending 并重新调度。

    场景：容器被 kill 时步骤正在执行 → 步骤永远卡在 in_progress，
    调度器不会重新调度（只调度 due 的 schedule 项，而 schedule 项已被移除）。

    返回恢复的步骤数。
    """
    recovered = 0
    try:
        index = memory_store.read_json("plans/_index")
        if not isinstance(index, list):
            return 0

        active_plan_ids = [
            e["plan_id"] for e in index
            if e.get("status") in ("active",)
        ]

        for plan_id in active_plan_ids:
            plan = bot_planner.get_plan(plan_id)
            if not plan:
                continue

            for step in plan.get("steps", []):
                if step["status"] != "in_progress":
                    continue

                # 重置为 pending
                step["status"] = "pending"
                step["started_at"] = ""
                logger.warning(
                    "scheduler recovery: reset stuck step %s/%s '%s' → pending",
                    plan_id, step["id"], step.get("title", ""),
                )
                recovered += 1

            if recovered:
                # 保存更新后的计划
                memory_store.write_json(
                    f"plans/{plan_id}", plan,
                    message=f"plan: recover stuck steps after restart",
                )

                # 找到第一个 pending 步骤并安排调度
                for step in plan.get("steps", []):
                    if step["status"] == "pending":
                        # 找到关联的用户信息（从 schedule 或 plan 的创建者）
                        schedules = get_pending_schedules()
                        user_id = ""
                        user_name = ""
                        for s in schedules:
                            if s.get("plan_id") == plan_id:
                                user_id = s.get("notify_user_id", "")
                                user_name = s.get("notify_user_name", "")
                                break

                        # 5 分钟后重新执行（给系统一些启动缓冲）
                        from datetime import timedelta
                        target = _now_tz() + timedelta(minutes=5)
                        add_schedule(
                            plan_id=plan_id,
                            step_id=step["id"],
                            scheduled_at=target.isoformat(),
                            notify_user_id=user_id,
                            notify_user_name=user_name,
                        )
                        logger.info(
                            "scheduler recovery: rescheduled %s/%s at %s",
                            plan_id, step["id"], target.isoformat(),
                        )
                        break
    except Exception:
        logger.exception("scheduler recovery: failed")

    return recovered
