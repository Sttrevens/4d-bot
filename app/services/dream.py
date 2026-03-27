"""Dream 记忆整合 —— 周期性清理和巩固记忆

灵感来源：人类睡眠中的记忆巩固（memory consolidation）。
Bot 定期"做梦"，整理累积的日记：
- 解决矛盾条目（同一话题的冲突记录）
- 绝对化相对日期（"昨天" → "2026-03-25"）
- 去重合并近似条目
- 刷新用户画像

触发条件：距上次 dream ≥24h 且累计 ≥5 次 write_diary 会话。
由 scheduler 在空闲时段调用。
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timezone

from app.services import memory_store

logger = logging.getLogger(__name__)

# ── 常量 ──

_DREAM_COOLDOWN = 86400           # 24 小时冷却
_DREAM_SESSION_THRESHOLD = 5      # 至少 5 次 write_diary 才触发
_DREAM_META_KEY = "dream_meta"    # Redis key（通过 memory_store read_json/write_json）

# 相对日期关键词
_RELATIVE_DATE_WORDS = {"yesterday", "昨天", "today", "今天", "前天",
                        "day before yesterday", "明天", "tomorrow"}


# ── 状态管理 ──


def get_dream_meta() -> dict:
    """读取 dream 元数据。"""
    meta = memory_store.read_json(_DREAM_META_KEY)
    if not isinstance(meta, dict):
        return {
            "last_dream_ts": 0,
            "session_counter": 0,
            "total_dreams": 0,
        }
    return meta


def update_dream_meta(updates: dict) -> None:
    """更新 dream 元数据（合并式写入）。"""
    meta = get_dream_meta()
    meta.update(updates)
    memory_store.write_json(_DREAM_META_KEY, meta)


def increment_session_counter() -> None:
    """write_diary 成功后调用，累加会话计数器。"""
    meta = get_dream_meta()
    meta["session_counter"] = meta.get("session_counter", 0) + 1
    memory_store.write_json(_DREAM_META_KEY, meta)


def should_dream() -> bool:
    """检查是否应该触发 dream。

    条件：距上次 dream ≥24h 且累计 ≥5 次 write_diary。
    """
    meta = get_dream_meta()
    elapsed = time.time() - meta.get("last_dream_ts", 0)
    sessions = meta.get("session_counter", 0)

    if elapsed < _DREAM_COOLDOWN:
        return False
    if sessions < _DREAM_SESSION_THRESHOLD:
        return False
    return True


# ── Phase 1: 方向定位 ──


def _phase_orientation() -> dict:
    """读取当前记忆状态，为后续阶段提供上下文。

    返回: {journal, index, profiles, stats}
    """
    journal = memory_store.read_journal_all()
    index = memory_store.read_json("journal_index")
    if not isinstance(index, list):
        index = []

    # 收集所有出现过的 user_id，读取其画像
    user_ids = set()
    for entry in journal:
        uid = entry.get("user_id", "")
        if uid:
            user_ids.add(uid[:12])

    profiles = {}
    for uid in user_ids:
        profile = memory_store.read_json(f"users/{uid}")
        if isinstance(profile, dict):
            profiles[uid] = profile

    stats = {
        "journal_length": len(journal),
        "index_length": len(index),
        "user_count": len(profiles),
        "oldest_ts": journal[0].get("ts", 0) if journal else 0,
        "newest_ts": journal[-1].get("ts", 0) if journal else 0,
    }

    logger.info("dream/orientation: journal=%d, index=%d, users=%d",
                len(journal), len(index), len(profiles))

    return {
        "journal": journal,
        "index": index,
        "profiles": profiles,
        "stats": stats,
    }


# ── Phase 2: 信号采集（纯 Python 模式匹配）──


def _phase_gather_signal(orientation: dict) -> list[dict]:
    """扫描 journal，找出需要整合的信号。

    信号类型：
    - solution: 标记为解决方案的条目（值得强化）
    - preference: 偏好类条目（可能需要合并去重）
    - contradiction: 同标签但结果矛盾的条目对
    - relative_date: 包含相对日期的条目（需要绝对化）

    返回信号列表，每个信号包含 {type, indices, reason, entries}。
    """
    journal = orientation["journal"]
    if not journal:
        return []

    signals: list[dict] = []

    # 按标签分组，用于检测矛盾
    tag_groups: dict[str, list[tuple[int, dict]]] = defaultdict(list)

    for idx, entry in enumerate(journal):
        tags = entry.get("tags", [])
        action = str(entry.get("action", ""))
        details = str(entry.get("details", ""))
        text = f"{action} {details}".lower()

        # 信号 1: solution 条目
        if entry.get("solution"):
            signals.append({
                "type": "solution",
                "indices": [idx],
                "reason": "标记为解决方案，值得强化和去重",
                "entries": [entry],
            })

        # 信号 2: 偏好类条目
        if "偏好" in tags or "preference" in text or action.startswith("用户偏好:"):
            signals.append({
                "type": "preference",
                "indices": [idx],
                "reason": "偏好数据，可能需要合并或更新画像",
                "entries": [entry],
            })

        # 信号 3: 相对日期
        for word in _RELATIVE_DATE_WORDS:
            if word in text:
                signals.append({
                    "type": "relative_date",
                    "indices": [idx],
                    "reason": f"包含相对日期词「{word}」，需要绝对化",
                    "entries": [entry],
                })
                break  # 每条只报一次

        # 收集标签分组（用于矛盾检测）
        for tag in tags:
            tag_groups[tag].append((idx, entry))

    # 信号 4: 同标签条目的矛盾检测
    # 找同标签下 outcome 不同的条目对（简单启发：同 action 前缀但不同 outcome）
    for tag, group in tag_groups.items():
        if len(group) < 2:
            continue
        # 按 action 前 20 字符分组
        action_groups: dict[str, list[tuple[int, dict]]] = defaultdict(list)
        for idx, entry in group:
            action_key = str(entry.get("action", ""))[:20].strip().lower()
            if action_key:
                action_groups[action_key].append((idx, entry))

        for action_key, items in action_groups.items():
            if len(items) < 2:
                continue
            # 检查 outcome 是否有差异
            outcomes = set()
            for _, e in items:
                outcome = str(e.get("outcome", "")).strip()
                if outcome:
                    outcomes.add(outcome)
            if len(outcomes) > 1:
                signals.append({
                    "type": "contradiction",
                    "indices": [i for i, _ in items],
                    "reason": f"标签「{tag}」下同类条目结果矛盾: {list(outcomes)[:3]}",
                    "entries": [e for _, e in items],
                })

    logger.info("dream/gather_signal: found %d signals", len(signals))
    return signals


# ── Phase 3: LLM 整合决策 ──


_CONSOLIDATION_PROMPT = """\
你是记忆整合助手。分析以下需要整合的日记信号，做出清理决策。

当前日期: {today}

待整合的信号:
{signals_text}

完整日记条目数: {journal_length}

用户画像摘要:
{profiles_text}

请执行以下整合操作：

1. **矛盾解决**: 如果同一话题有冲突记录，保留最新/最准确的，标记旧的删除
2. **日期绝对化**: 将"昨天"/"今天"等相对日期转换为绝对日期（基于条目的 time 字段推算）
3. **去重合并**: 近似条目合并为一条（保留最完整的信息）
4. **画像刷新**: 基于偏好类信号，建议更新用户画像

输出严格 JSON（不要输出其他内容）：
{
  "entries_to_remove": [索引号列表，要删除的条目],
  "entries_to_update": [{"idx": 索引号, "updates": {"字段名": "新值"}}],
  "profile_updates": {"user_id": {"preferences": ["更新后的偏好列表"]}},
  "summary": "本次整合摘要（一句话）"
}\
"""


async def _phase_consolidation(signals: list[dict], orientation: dict) -> dict | None:
    """用 LLM 分析信号，生成整合方案。

    返回 { entries_to_remove, entries_to_update, profile_updates, summary }
    """
    if not signals:
        return None

    # 格式化信号给 LLM
    signal_lines = []
    for i, sig in enumerate(signals[:30]):  # 限制数量避免 token 爆炸
        sig_type = sig["type"]
        reason = sig["reason"]
        indices = sig["indices"]
        entries_preview = []
        for entry in sig["entries"][:3]:
            time_str = str(entry.get("time", ""))[:16]
            action = str(entry.get("action", ""))[:80]
            outcome = str(entry.get("outcome", ""))[:40]
            entries_preview.append(f"  [{time_str}] {action}" +
                                   (f" → {outcome}" if outcome else ""))
        signal_lines.append(
            f"[{sig_type}] indices={indices} | {reason}\n" +
            "\n".join(entries_preview)
        )

    signals_text = "\n\n".join(signal_lines)

    # 格式化画像
    profiles = orientation.get("profiles", {})
    profiles_lines = []
    for uid, profile in list(profiles.items())[:5]:
        name = profile.get("name", uid)
        prefs = profile.get("preferences", [])[:5]
        profiles_lines.append(f"  {name}: prefs={prefs}")
    profiles_text = "\n".join(profiles_lines) if profiles_lines else "(无)"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = _CONSOLIDATION_PROMPT.format(
        today=today,
        signals_text=signals_text,
        journal_length=orientation["stats"]["journal_length"],
        profiles_text=profiles_text,
    )

    from app.services.memory import _llm_json_call
    result = await _llm_json_call(prompt, "请分析以上信号并输出整合方案。")

    if not isinstance(result, dict):
        logger.warning("dream/consolidation: LLM 返回非 dict: %s", type(result))
        return None

    logger.info("dream/consolidation: remove=%d, update=%d, profile_updates=%d",
                len(result.get("entries_to_remove", [])),
                len(result.get("entries_to_update", [])),
                len(result.get("profile_updates", {})))

    return result


# ── Phase 4: 执行修剪和索引重建 ──


def _phase_prune_and_index(journal: list[dict], consolidation: dict) -> dict:
    """应用整合结果：删除/更新条目，重写 journal，重建索引，更新画像。

    返回执行统计。
    """
    entries_to_remove = set(consolidation.get("entries_to_remove", []))
    entries_to_update = consolidation.get("entries_to_update", [])
    profile_updates = consolidation.get("profile_updates", {})

    # 应用更新
    update_map = {}
    for item in entries_to_update:
        idx = item.get("idx")
        updates = item.get("updates", {})
        if isinstance(idx, int) and isinstance(updates, dict):
            update_map[idx] = updates

    updated_count = 0
    for idx, updates in update_map.items():
        if 0 <= idx < len(journal) and idx not in entries_to_remove:
            for field, value in updates.items():
                journal[idx][field] = value
            updated_count += 1

    # 删除条目（从后往前删，保持索引稳定）
    removed_count = 0
    new_journal = []
    for idx, entry in enumerate(journal):
        if idx in entries_to_remove:
            removed_count += 1
        else:
            new_journal.append(entry)

    # 重写 journal
    if removed_count > 0 or updated_count > 0:
        memory_store.rewrite_journal(new_journal)

    # 重建索引
    _rebuild_index(new_journal)

    # 更新用户画像
    profile_updated = 0
    for uid, updates in profile_updates.items():
        if not isinstance(updates, dict):
            continue
        try:
            from app.services.memory import update_user_profile
            update_user_profile(uid, updates)
            profile_updated += 1
        except Exception:
            logger.debug("dream/prune: profile update failed for %s", uid, exc_info=True)

    stats = {
        "removed": removed_count,
        "updated": updated_count,
        "profiles_refreshed": profile_updated,
        "journal_size_after": len(new_journal),
    }

    logger.info("dream/prune: removed=%d, updated=%d, profiles=%d, journal_after=%d",
                removed_count, updated_count, profile_updated, len(new_journal))

    return stats


def _rebuild_index(journal: list[dict]) -> None:
    """根据 journal 重建索引。"""
    index = []
    for idx, entry in enumerate(journal):
        action = str(entry.get("action", entry.get("details", "")))
        tags = entry.get("tags", [])
        time_str = str(entry.get("time", ""))[:10]

        index.append({
            "idx": idx,
            "s": action[:80],
            "t": tags,
            "ts": time_str,
        })

    # 裁剪到上限
    if len(index) > 500:
        index = index[-500:]

    memory_store.write_json("journal_index", index)
    logger.info("dream: index rebuilt with %d entries", len(index))


# ── 主入口 ──


async def run_dream() -> dict:
    """执行一次完整的 dream 记忆整合。

    四阶段流水线：定位 → 采集信号 → LLM 整合 → 修剪写回。
    返回执行摘要。
    """
    logger.info("dream: === starting dream cycle ===")
    start_ts = time.time()

    # Phase 1: 方向定位
    orientation = _phase_orientation()
    if orientation["stats"]["journal_length"] < 10:
        logger.info("dream: journal too short (%d), skipping",
                     orientation["stats"]["journal_length"])
        return {"skipped": True, "reason": "journal too short"}

    # Phase 2: 信号采集
    signals = _phase_gather_signal(orientation)
    if not signals:
        logger.info("dream: no signals found, skipping consolidation")
        # 即使没有信号也更新 meta（重置计数器，防止反复触发）
        update_dream_meta({
            "last_dream_ts": time.time(),
            "session_counter": 0,
            "total_dreams": get_dream_meta().get("total_dreams", 0) + 1,
        })
        return {"skipped": True, "reason": "no signals"}

    # Phase 3: LLM 整合
    consolidation = await _phase_consolidation(signals, orientation)
    if not consolidation:
        logger.warning("dream: consolidation returned nothing, skipping prune")
        update_dream_meta({
            "last_dream_ts": time.time(),
            "session_counter": 0,
        })
        return {"skipped": True, "reason": "consolidation failed"}

    # Phase 4: 执行修剪
    prune_stats = _phase_prune_and_index(orientation["journal"], consolidation)

    # 更新 dream 元数据
    elapsed = time.time() - start_ts
    meta = get_dream_meta()
    update_dream_meta({
        "last_dream_ts": time.time(),
        "session_counter": 0,
        "total_dreams": meta.get("total_dreams", 0) + 1,
        "last_dream_duration": round(elapsed, 1),
        "last_dream_summary": consolidation.get("summary", ""),
    })

    # 写入 journal 记录 dream 事件本身
    try:
        memory_store.append_journal({
            "type": "dream",
            "action": f"记忆整合: 删除{prune_stats['removed']}条, "
                      f"更新{prune_stats['updated']}条, "
                      f"刷新{prune_stats['profiles_refreshed']}个画像",
            "tags": ["系统", "dream"],
            "time": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        logger.debug("dream: failed to log dream event", exc_info=True)

    summary = {
        "signals_found": len(signals),
        "duration_seconds": round(elapsed, 1),
        **prune_stats,
        "llm_summary": consolidation.get("summary", ""),
    }

    logger.info("dream: === completed in %.1fs — removed=%d, updated=%d ===",
                elapsed, prune_stats["removed"], prune_stats["updated"])

    return summary
