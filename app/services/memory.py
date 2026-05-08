"""三层记忆管理器

Layer 1: 工作记忆（Working Memory）— ChatHistory，已存在
Layer 2: 情景记忆（Episodic Memory）— journal，全量日记（LLM 生成摘要+标签+偏好）
Layer 3: 语义记忆（Semantic Memory）— 用户画像 + 项目知识

记忆设计原则：
- 全量写日记：每次交互都记（LLM 判断是否值得记 + 生成摘要/标签/偏好）
- 智能回忆：新消息进来时，LLM 判断是否需要回忆 + 搜什么，按需召回
- bot 自己的行动也记：创建的文档 ID、发过的消息、处理过的任务等结构化信息
- 省 token：记忆上下文按需注入，不相关的不注入
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone

from app.services import memory_store

logger = logging.getLogger(__name__)

# 用户画像模板
_DEFAULT_USER_PROFILE = {
    "name": "",
    "preferences": [],       # 用户偏好/习惯
    "expertise": [],          # 用户擅长的领域
    "recent_topics": [],      # 最近关注的话题
    "identity_facts": [],     # 用户是谁/角色/背景
    "current_goals": [],      # 当前正在推进的目标
    "open_loops": [],         # 未完成/需要跟进的事
    "important_entities": [], # 用户关注的人、项目、产品、对象
    "communication_style": [],# 用户偏好的互动方式
    "last_user_need": "",
    "interaction_count": 0,
    "first_seen": "",
    "last_seen": "",
}

# 项目知识模板
_DEFAULT_PROJECT_KNOWLEDGE = {
    "repo": "",
    "architecture": "",       # 架构概述
    "key_files": [],          # 关键文件
    "conventions": [],        # 编码约定
    "common_issues": [],      # 常见问题
    "last_updated": "",
}


# ── 记忆索引（Memory Index Layer）──
# 轻量级索引：每条日记生成一行摘要 + 标签，存在单独的 Redis key
# 检索时先搜索索引（快），命中后再加载完整记忆（按需）
# 索引格式: [{idx: N, s: "摘要", t: ["标签"], ts: "2025-01-01"}]

_INDEX_KEY = "journal_index"
_INDEX_MAX = 500  # 索引最大条目数


def _append_index(summary: str, tags: list[str]) -> None:
    """追加一条索引条目。"""
    try:
        index = memory_store.read_json(_INDEX_KEY)
        if not isinstance(index, list):
            index = []
        idx = len(index)
        index.append({
            "idx": idx,
            "s": summary[:80],
            "t": tags,
            "ts": datetime.now(timezone.utc).isoformat()[:10],
        })
        # 超过上限时裁剪旧索引
        if len(index) > _INDEX_MAX:
            index = index[-_INDEX_MAX:]
        memory_store.write_json(_INDEX_KEY, index)
    except Exception:
        logger.debug("_append_index failed", exc_info=True)


def search_index(
    keyword: str = "",
    tags: list[str] | None = None,
    limit: int = 10,
) -> list[dict]:
    """搜索记忆索引（轻量级，不加载完整日记）。

    返回匹配的索引条目，调用方可据此决定是否加载完整 journal。
    """
    try:
        index = memory_store.read_json(_INDEX_KEY)
        if not isinstance(index, list):
            return []
    except Exception:
        return []

    keyword_lower = keyword.strip().lower() if keyword else ""
    results = []
    for entry in reversed(index):  # 最近的在前
        # 标签匹配
        if tags:
            entry_tags = set(entry.get("t", []))
            if not entry_tags.intersection(tags):
                if not keyword_lower:
                    continue

        # 关键词匹配
        if keyword_lower:
            if keyword_lower not in entry.get("s", "").lower():
                if not tags or not set(entry.get("t", [])).intersection(tags):
                    continue

        results.append(entry)
        if len(results) >= limit:
            break
    return results


# ── 情景记忆（Episodic）──


def remember(
    user_id: str,
    user_name: str,
    action: str,
    outcome: str = "",
    tags: list[str] | None = None,
    solution: bool = False,
) -> int:
    """记录一次交互到 journal。返回当前 journal 长度。

    solution=True 表示这是一个可复用的解决方案，会被组织级记忆召回。
    """
    entry = {
        "user_id": user_id[:12],
        "user_name": user_name,
        "action": action,
        "outcome": outcome,
        "tags": tags or [],
        "time": datetime.now(timezone.utc).isoformat(),
    }
    if solution:
        entry["solution"] = True
    try:
        length = memory_store.append_journal(entry)
        # 同步写入索引
        _append_index(action[:80], tags or [])
        return length
    except Exception:
        logger.warning("remember failed", exc_info=True)
        return 0


def recall(
    user_id: str = "",
    tags: list[str] | None = None,
    keyword: str = "",
    limit: int = 20,
    query_text: str = "",
) -> list[dict]:
    """检索相关记忆。支持按用户、标签、关键词过滤。

    两阶段搜索：先在最近 100 条中找，找不够再搜全部 journal。
    keyword 会在 action/details/outcome/summary 字段中做子串匹配。
    tags 和 keyword 是 OR 关系：任一匹配即纳入结果。
    """
    has_filter = bool(tags or (keyword and keyword.strip()))
    keyword_lower = keyword.strip().lower() if keyword else ""

    # 阶段 1: 先搜最近 100 条（大多数情况下够用，省 Redis 带宽）
    results = _filter_entries(
        _read_journal_safe(limit=100),
        user_id=user_id, tags=tags, keyword_lower=keyword_lower,
        has_filter=has_filter, limit=limit,
    )

    # 阶段 2: 最近 100 条没找够 → 搜全部（深度回忆）
    if len(results) < limit and has_filter:
        all_entries = _read_journal_safe(limit=0)
        if len(all_entries) > 100:
            results = _filter_entries(
                all_entries, user_id=user_id, tags=tags,
                keyword_lower=keyword_lower, has_filter=has_filter, limit=limit,
            )

    return results


def _read_journal_safe(limit: int = 100) -> list[dict]:
    """安全读取 journal，出错返回空列表。limit=0 读全部。"""
    try:
        if limit <= 0:
            return memory_store.read_journal_all()
        return memory_store.read_journal(limit=limit)
    except Exception:
        logger.warning("recall: journal read failed", exc_info=True)
        return []


def _filter_entries(
    entries: list[dict],
    *,
    user_id: str,
    tags: list[str] | None,
    keyword_lower: str,
    has_filter: bool,
    limit: int,
) -> list[dict]:
    """按条件过滤 journal 条目。"""
    results = []
    for e in reversed(entries):  # 最近的在前
        if user_id and e.get("user_id", "")[:12] != user_id[:12]:
            continue

        # 标签匹配
        tag_match = False
        if tags:
            entry_tags = set(e.get("tags", []))
            tag_match = bool(entry_tags.intersection(tags))

        # 关键词匹配（在多个文本字段中搜索）
        kw_match = False
        if keyword_lower:
            searchable = " ".join([
                str(e.get("action", "")),
                str(e.get("details", "")),
                str(e.get("outcome", "")),
                str(e.get("summary", "")),
            ]).lower()
            kw_match = keyword_lower in searchable

        # tags 和 keyword 是 OR 关系；都没指定则全部匹配
        if has_filter and not tag_match and not kw_match:
            continue

        results.append(e)
        if len(results) >= limit:
            break
    return results


def recall_org(
    tags: list[str] | None = None,
    keyword: str = "",
    limit: int = 10,
    exclude_user_id: str = "",
) -> list[dict]:
    """组织级记忆召回：搜索所有用户的解决方案类记忆。

    只返回标记了 solution=True 的条目（可复用的解决方案），
    排除当前用户自己的记忆（避免重复）。
    用于跨用户知识共享：用户 A 解决的问题，用户 B 遇到类似情况时自动借鉴。
    """
    has_filter = bool(tags or (keyword and keyword.strip()))
    keyword_lower = keyword.strip().lower() if keyword else ""
    exclude_uid = exclude_user_id[:12] if exclude_user_id else ""

    entries = _read_journal_safe(limit=200)
    results = []
    for e in reversed(entries):
        # 只看解决方案类条目
        if not e.get("solution"):
            continue
        # 排除当前用户
        if exclude_uid and e.get("user_id", "")[:12] == exclude_uid:
            continue

        # 标签匹配
        tag_match = False
        if tags:
            entry_tags = set(e.get("tags", []))
            tag_match = bool(entry_tags.intersection(tags))

        # 关键词匹配
        kw_match = False
        if keyword_lower:
            searchable = " ".join([
                str(e.get("action", "")),
                str(e.get("details", "")),
                str(e.get("outcome", "")),
                str(e.get("summary", "")),
            ]).lower()
            kw_match = keyword_lower in searchable

        if has_filter and not tag_match and not kw_match:
            continue

        results.append(e)
        if len(results) >= limit:
            break
    return results


def recall_org_text(
    tags: list[str] | None = None,
    keyword: str = "",
    limit: int = 10,
    exclude_user_id: str = "",
) -> str:
    """组织级记忆召回，格式化为文本。"""
    entries = recall_org(
        tags=tags, keyword=keyword, limit=limit,
        exclude_user_id=exclude_user_id,
    )
    if not entries:
        return "没有找到组织内其他成员的相关解决方案。"
    lines = []
    for e in entries:
        t = e.get("time", "?")[:16]
        user = e.get("user_name", "?")
        action = e.get("action", "?")
        outcome = e.get("outcome", "")
        tags_str = " ".join(f"#{t}" for t in e.get("tags", []))
        line = f"[{t}] {user}: {action}"
        if outcome:
            line += f" → {outcome}"
        if tags_str:
            line += f"  {tags_str}"
        lines.append(line)
    return "\n".join(lines)


def recall_text(
    user_id: str = "",
    tags: list[str] | None = None,
    keyword: str = "",
    limit: int = 10,
    query_text: str = "",
) -> str:
    """检索记忆并格式化为文本（供 LLM 工具返回）。"""
    entries = recall(
        user_id=user_id,
        tags=tags,
        keyword=keyword,
        limit=limit,
        query_text=query_text,
    )
    return format_recall_entries(entries)


def format_recall_entries(entries: list[dict]) -> str:
    """Format recalled memory entries for tool output."""
    if not entries:
        return "没有找到相关记忆。"
    lines = []
    for e in entries:
        t = e.get("time", "?")[:16]
        user = e.get("user_name", "?")
        action = e.get("action", "?")
        outcome = e.get("outcome", "")
        tags_str = " ".join(f"#{t}" for t in e.get("tags", []))
        line = f"[{t}] {user}: {action}"
        if outcome:
            line += f" → {outcome}"
        if tags_str:
            line += f"  {tags_str}"
        lines.append(line)
    return "\n".join(lines)


# ── Bot 行动日记 ──


# 只记录这些"写"操作，"读"操作不记（节省 Redis 调用）
_REMEMBER_TOOL_KEYWORDS = frozenset({
    "create", "write", "send", "update", "set", "add",
    "delete", "remove", "deploy", "fix", "edit",
})


def note_tool_action(
    tool_name: str,
    tool_args: dict,
    result_str: str,
    user_id: str = "",
    user_name: str = "",
) -> None:
    """工具执行后调用：记录 bot 的重要行动到日记。

    只记录写操作（创建/发送/修改等），读操作跳过。
    结构化提取关键信息（文档 ID、标题、URL 等），而不是记原始 result。
    """
    # 只记录写类操作
    name_lower = tool_name.lower()
    if not any(kw in name_lower for kw in _REMEMBER_TOOL_KEYWORDS):
        return

    # 失败的操作不记
    if "[ERROR]" in result_str:
        return

    details = _extract_action_details(tool_name, tool_args, result_str)
    if not details:
        return

    tags = _infer_tags([tool_name])

    entry = {
        "type": "bot_action",
        "tool": tool_name,
        "details": details,
        "user_id": user_id[:12] if user_id else "",
        "user_name": user_name,
        "tags": tags,
        "time": datetime.now(timezone.utc).isoformat(),
    }
    try:
        memory_store.append_journal(entry)
        # 同步写入索引
        _append_index(f"[bot] {details[:60]}", tags)
    except Exception:
        logger.debug("note_tool_action failed", exc_info=True)


def _extract_action_details(tool_name: str, args: dict, result: str) -> str:
    """从工具调用中提取结构化的关键信息摘要。"""
    name = tool_name.lower()

    # 文档操作
    if "doc" in name:
        title = args.get("title", "")
        doc_id = args.get("document_id", "")
        # 从结果中提取 document_id
        m = re.search(r"document_id:\s*(\S+)", result)
        if m:
            doc_id = m.group(1)
        # 从结果中提取 URL
        url_m = re.search(r"(https://\S*feishu\S*docx/\S+)", result)
        url = url_m.group(1) if url_m else ""
        if title and doc_id:
            s = f"创建文档「{title}」(ID: {doc_id})"
            if url:
                s += f" {url}"
            return s
        if doc_id:
            blocks_m = re.search(r"已写入 (\d+) 个", result)
            blocks = blocks_m.group(1) if blocks_m else ""
            return f"写入文档 {doc_id}" + (f" ({blocks}块)" if blocks else "")
        return ""

    # 消息操作
    if "message" in name or "send" in name:
        target = args.get("name_or_id", args.get("chat_id", ""))
        content = args.get("content", "")[:60]
        if target:
            return f"发消息给 {target}: {content}"
        return ""

    # 日历操作
    if "calendar" in name or "event" in name:
        summary = args.get("summary", args.get("title", ""))
        if summary:
            return f"日历事件: {summary}"
        return ""

    # 任务操作
    if "task" in name:
        summary = args.get("summary", args.get("title", args.get("content", "")))
        if summary:
            return f"任务: {summary[:60]}"
        return ""

    # 妙记操作
    if "minute" in name:
        token = args.get("minute_token", "")
        return f"处理妙记 {token[:20]}" if token else ""

    # 代码/部署操作
    if "deploy" in name or "edit" in name or "fix" in name:
        return result[:80] if result else ""

    # 其他写操作：取结果摘要
    if result and not result.startswith("[ERROR]"):
        return result[:80]
    return ""


# ── 语义记忆（Semantic）──


def get_user_profile(user_id: str) -> dict:
    """获取用户画像。不存在则返回默认模板。"""
    key = f"users/{user_id[:12]}"
    profile = memory_store.read_json(key)
    if profile is None:
        return dict(_DEFAULT_USER_PROFILE)
    for k, v in _DEFAULT_USER_PROFILE.items():
        profile.setdefault(k, [] if isinstance(v, list) else v)
    return profile


_PROFILE_LIST_FIELDS = (
    "preferences",
    "expertise",
    "recent_topics",
    "identity_facts",
    "current_goals",
    "open_loops",
    "important_entities",
    "communication_style",
)


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def update_user_profile(user_id: str, updates: dict) -> bool:
    """更新用户画像（合并更新，不覆盖）。"""
    profile = get_user_profile(user_id)

    # 合并简单字段
    for field in ("name", "architecture", "last_user_need"):
        if field in updates and str(updates[field] or "").strip():
            profile[field] = updates[field]

    # 合并列表字段（去重，保留最近 20 项）
    for field in _PROFILE_LIST_FIELDS:
        if field in updates:
            existing = _as_list(profile.get(field, []))
            new_items = _as_list(updates[field])
            merged = list(dict.fromkeys(existing + new_items))  # 去重保序
            profile[field] = merged[-20:]  # 只保留最近 20 项

    # 更新计数和时间
    profile["interaction_count"] = profile.get("interaction_count", 0) + 1
    profile["last_seen"] = datetime.now(timezone.utc).isoformat()
    if not profile.get("first_seen"):
        profile["first_seen"] = profile["last_seen"]

    key = f"users/{user_id[:12]}"
    return memory_store.write_json(key, profile)


def _profile_updates_from_diary(
    *,
    user_name: str,
    summary: str,
    tags: list[str],
    prefs: list[str],
    diary: dict,
) -> dict:
    """Convert diary extraction into durable user-model profile updates."""
    updates: dict = {"name": user_name}
    if tags:
        updates["recent_topics"] = tags[:3]
    if prefs:
        updates["preferences"] = prefs

    field_map = {
        "uf": "identity_facts",
        "g": "current_goals",
        "ol": "open_loops",
        "ent": "important_entities",
        "style": "communication_style",
    }
    for source, target in field_map.items():
        values = _as_list(diary.get(source))
        if values:
            updates[target] = values

    need = str(diary.get("need") or "").strip()
    if need:
        updates["last_user_need"] = need[:300]
    elif summary:
        updates["last_user_need"] = summary[:300]
    return updates


def get_project_knowledge(repo: str) -> dict:
    """获取项目知识。"""
    key = f"projects/{repo.replace('/', '_')}"
    knowledge = memory_store.read_json(key)
    if knowledge is None:
        return dict(_DEFAULT_PROJECT_KNOWLEDGE, repo=repo)
    return knowledge


def update_project_knowledge(repo: str, updates: dict) -> bool:
    """更新项目知识。"""
    knowledge = get_project_knowledge(repo)

    for field in ("architecture",):
        if field in updates:
            knowledge[field] = updates[field]

    for field in ("key_files", "conventions", "common_issues"):
        if field in updates:
            existing = knowledge.get(field, [])
            new_items = updates[field] if isinstance(updates[field], list) else [updates[field]]
            merged = list(dict.fromkeys(existing + new_items))
            knowledge[field] = merged[-30:]

    knowledge["last_updated"] = datetime.now(timezone.utc).isoformat()

    key = f"projects/{repo.replace('/', '_')}"
    return memory_store.write_json(key, knowledge)


# ── 上下文构建（注入 system prompt）──


def _memory_entry_text(e: dict) -> str:
    """提取记忆条目中的所有文本字段，用于相关性计算。"""
    parts = []
    for key in ("action", "outcome", "details", "summary"):
        v = e.get(key, "")
        if v:
            parts.append(str(v))
    for tag in e.get("tags", []):
        parts.append(str(tag))
    return " ".join(parts)


def _text_to_bigrams(text: str) -> set[str]:
    """将文本转为 2-gram 字符集合（中文友好，无需分词）。"""
    # 去除标点和空白，只保留有意义的字符
    cleaned = "".join(c for c in text.lower() if c.isalnum())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def _memory_relevance_score(entry: dict, query_bigrams: set[str]) -> float:
    """计算记忆条目与当前消息的相关性分数（0~1）。

    使用字符 bigram Jaccard 系数，对中文无需分词。
    """
    if not query_bigrams:
        return 1.0  # 无查询文本时全部通过
    entry_text = _memory_entry_text(entry)
    entry_bigrams = _text_to_bigrams(entry_text)
    if not entry_bigrams:
        return 0.0
    intersection = query_bigrams & entry_bigrams
    # 用较小集合做分母（Overlap coefficient），对短查询更友好
    denominator = min(len(query_bigrams), len(entry_bigrams))
    return len(intersection) / denominator if denominator else 0.0


_MEMORY_RELEVANCE_THRESHOLD = 0.08  # 至少有 8% bigram 重叠才注入

_NUMERIC_FACT_RE = re.compile(r"\d")
_NUMERIC_FACT_CUE_RE = re.compile(
    r"(预测|估算|竞猜|猜|承诺|结论|判断|预计|预估|销量|销售|营收|收入|"
    r"首周|首月|首年|长线|生命周期|愿望单|转化率|概率|份|万|亿|k|m|%)",
    re.IGNORECASE,
)
_NUMERIC_CONTEXT_CUE_RE = re.compile(
    r"(预测|估算|竞猜|猜|销量|销售|营收|收入|首周|首月|首年|愿望单|转化率)",
    re.IGNORECASE,
)


def remember_numeric_facts(
    *,
    user_id: str,
    user_name: str,
    user_text: str,
    reply: str,
) -> int:
    """Persist concrete numeric predictions/commitments from a turn.

    Diary summaries are intentionally short and can drop exact numbers. This
    deterministic side-channel keeps auditable numeric conclusions searchable.
    """
    snippets = _extract_numeric_fact_snippets(user_text=user_text, reply=reply)
    if not snippets:
        return 0

    saved = 0
    for snippet in snippets:
        action = f"数字事实: {snippet}"
        entry = {
            "type": "numeric_fact",
            "user_id": user_id[:12],
            "user_name": user_name,
            "action": action[:600],
            "tags": ["预测", "数字"],
            "time": datetime.now(timezone.utc).isoformat(),
        }
        try:
            memory_store.append_journal(entry)
            _append_index(action[:80], entry["tags"])
            saved += 1
        except Exception:
            logger.debug("remember_numeric_facts failed", exc_info=True)
            break
    if saved:
        logger.info("numeric facts: saved %d for %s", saved, user_name or user_id[:12])
    return saved


def _extract_numeric_fact_snippets(*, user_text: str, reply: str, limit: int = 5) -> list[str]:
    """Extract short searchable snippets containing entities, numbers and scope."""
    if not reply or not _NUMERIC_FACT_RE.search(reply):
        return []
    combined_context = f"{user_text}\n{reply}"
    if not _NUMERIC_CONTEXT_CUE_RE.search(combined_context):
        return []

    raw_lines = [re.sub(r"\s+", " ", line).strip(" -\t") for line in reply.splitlines()]
    lines = [line for line in raw_lines if line]
    snippets: list[str] = []
    seen: set[str] = set()

    for idx, line in enumerate(lines):
        if not _NUMERIC_FACT_RE.search(line):
            continue
        if not _NUMERIC_FACT_CUE_RE.search(line):
            continue
        window: list[str] = []
        if idx > 0 and len(lines[idx - 1]) <= 160:
            window.append(lines[idx - 1])
        window.append(line)
        for lookahead in (1, 2):
            next_idx = idx + lookahead
            if next_idx >= len(lines):
                break
            next_line = lines[next_idx]
            if len(next_line) > 160:
                continue
            if _NUMERIC_FACT_RE.search(next_line) or _NUMERIC_FACT_CUE_RE.search(next_line):
                window.append(next_line)
        snippet = " / ".join(window)
        snippet = re.sub(r"\s+", " ", snippet).strip()
        key = snippet.lower()
        if not snippet or key in seen:
            continue
        seen.add(key)
        snippets.append(snippet[:500])
        if len(snippets) >= limit:
            break
    return snippets


def _format_memory_entry(e: dict) -> str:
    """格式化单条记忆条目为人类可读文本。"""
    t = e.get("time", "?")[:10]

    # 压缩记忆（远期摘要）
    if e.get("type") == "compressed":
        tr = e.get("time_range", "")
        summary = e.get("summary", "?")
        return f"  [{tr or t}] (摘要) {summary}"

    # bot 自己的行动日记
    if e.get("type") == "bot_action":
        details = e.get("details", "")
        return f"  [{t}] 我做了: {details}"

    # 用户交互记忆
    action = e.get("action", "?")[:80]
    outcome = e.get("outcome", "")
    line = f"  [{t}] {action}"
    if outcome:
        line += f" → {outcome[:60]}"
    return line


async def build_memory_context(
    user_id: str,
    user_name: str = "",
    current_text: str = "",
) -> str:
    """构建记忆上下文，注入到 system prompt 中。

    智能回忆策略（全 LLM 驱动）：
    1. 用户画像（偏好/规则）始终注入
    2. LLM 判断当前消息是否需要回忆历史 → 返回搜索标签
    3. 按标签召回相关记忆 → 注入上下文
    """
    import asyncio

    parts = []

    # 1. 用户画像（始终注入）
    profile = get_user_profile(user_id)
    if profile.get("interaction_count", 0) > 0:
        prefs = profile.get("preferences", [])
        topics = profile.get("recent_topics", [])
        identity_facts = profile.get("identity_facts", [])
        current_goals = profile.get("current_goals", [])
        open_loops = profile.get("open_loops", [])
        important_entities = profile.get("important_entities", [])
        communication_style = profile.get("communication_style", [])
        last_need = str(profile.get("last_user_need", "") or "").strip()
        profile_lines = [f"用户画像({profile.get('name', user_name)}):"]
        if identity_facts:
            profile_lines.append("  身份/背景:")
            for item in identity_facts[-5:]:
                profile_lines.append(f"    - {item}")
        if current_goals or last_need:
            profile_lines.append("  当前目标/需要:")
            for item in current_goals[-5:]:
                profile_lines.append(f"    - {item}")
            if last_need:
                profile_lines.append(f"    - 最近需求: {last_need}")
        if open_loops:
            profile_lines.append("  未完成事项:")
            for item in open_loops[-5:]:
                profile_lines.append(f"    - {item}")
        if important_entities:
            profile_lines.append(f"  重要对象: {', '.join(important_entities[-8:])}")
        if communication_style:
            profile_lines.append("  沟通风格:")
            for item in communication_style[-5:]:
                profile_lines.append(f"    - {item}")
        if prefs:
            profile_lines.append("  偏好/规则:")
            for p in prefs[-5:]:
                profile_lines.append(f"    - {p}")
        if topics:
            profile_lines.append(f"  最近关注: {', '.join(topics[-3:])}")
        if len(profile_lines) > 1:
            parts.append("\n".join(profile_lines))

    # 2. LLM 智能回忆决策：判断要不要回忆 + 搜什么标签
    recalled = False
    decision = None
    if current_text:
        try:
            decision = await asyncio.wait_for(
                _llm_recall_decision(current_text), timeout=3.0
            )
        except (asyncio.TimeoutError, Exception):
            decision = None

        if decision and decision.get("r"):
            tags = decision.get("t", [])
            keyword = decision.get("k", "")
            if tags or keyword:
                # 先搜索索引（轻量级），看是否有相关记忆
                index_hits = search_index(keyword=keyword, tags=tags, limit=8)
                if index_hits:
                    # 索引命中 → 加载完整 journal 做精确匹配
                    # 注意：必须传 user_id 做用户隔离，否则会召回其他用户的记忆
                    relevant = recall(user_id=user_id, tags=tags, keyword=keyword, limit=8)
                else:
                    # 索引未命中 → 仍尝试 journal 搜索（兼容旧数据无索引）
                    relevant = recall(user_id=user_id, tags=tags, keyword=keyword, limit=5)
                if relevant:
                    memory_lines = [_format_memory_entry(e) for e in relevant]
                    label = ", ".join(tags)
                    parts.append(f"相关记忆({label}):\n" + "\n".join(memory_lines))
                    recalled = True

    # 3. LLM 没有建议回忆 → 注入最近交互（保底），但按相关性过滤
    if not recalled:
        recent = recall(user_id=user_id, limit=8)  # 多取几条，过滤后可能剩不多
        if recent and current_text:
            q_bigrams = _text_to_bigrams(current_text)
            scored = [
                (e, _memory_relevance_score(e, q_bigrams))
                for e in recent
            ]
            # 只注入相关性超过阈值的条目，最多 3 条
            relevant_recent = [
                e for e, score in scored
                if score >= _MEMORY_RELEVANCE_THRESHOLD
            ][:3]
            if relevant_recent:
                memory_lines = [_format_memory_entry(e) for e in relevant_recent]
                parts.append("最近相关交互:\n" + "\n".join(memory_lines))
        elif recent and not current_text:
            # 无当前消息文本时（极少见），退回到不过滤
            memory_lines = [_format_memory_entry(e) for e in recent[:3]]
            parts.append("最近交互:\n" + "\n".join(memory_lines))

    # 4. 组织级记忆共享：搜索其他用户的解决方案
    # 当 tenant 启用 memory_org_recall_enabled 时，自动搜索其他用户的已解决问题
    try:
        from app.tenant.context import get_current_tenant
        tenant = get_current_tenant()
        org_recall = getattr(tenant, "memory_org_recall_enabled", False)
    except Exception:
        org_recall = False

    if org_recall and current_text:
        try:
            # 用当前消息的标签/关键词搜索组织级解决方案
            search_tags = None
            search_kw = ""
            if recalled and decision:
                search_tags = decision.get("t", [])
                search_kw = decision.get("k", "")
            org_results = recall_org(
                tags=search_tags, keyword=search_kw,
                limit=3, exclude_user_id=user_id,
            )
            if org_results:
                org_lines = [_format_memory_entry(e) for e in org_results]
                parts.append(
                    "组织内相关经验（其他同事的解决方案）:\n"
                    + "\n".join(org_lines)
                )
        except Exception:
            logger.debug("org recall failed", exc_info=True)

    if not parts:
        return ""

    context = "\n\n".join(parts)
    if len(context) > 2000:
        context = context[:2000] + "..."
    return (
        "\n\n── 你的记忆（过去的交互，不是用户当前请求）──\n"
        f"{context}\n"
        "── 记忆结束 ──"
    )


# ── 日记系统（写入侧）──


async def write_diary(
    user_id: str,
    user_name: str,
    user_text: str,
    reply: str,
    tool_names_called: list[str] | None = None,
    action_outcomes: list[tuple[str, str]] | None = None,
) -> None:
    """每次交互结束后写日记。

    LLM 生成摘要 + 标签 + 偏好提取，全量记录。
    不值得记的（纯寒暄）由 LLM 判断跳过。
    """
    try:
        diary = await _llm_diary_entry(user_text, reply, tool_names_called, action_outcomes)
    except Exception:
        logger.debug("write_diary: LLM call failed", exc_info=True)
        # LLM 失败时回退：有工具调用就用老逻辑记录
        remember_numeric_facts(
            user_id=user_id,
            user_name=user_name,
            user_text=user_text,
            reply=reply,
        )
        if tool_names_called:
            _fallback_diary(user_id, user_name, user_text, reply, tool_names_called)
        return

    remember_numeric_facts(
        user_id=user_id,
        user_name=user_name,
        user_text=user_text,
        reply=reply,
    )

    if not diary or not diary.get("w", False):
        return  # LLM 判断不值得记录

    summary = diary.get("s", "")
    tags = diary.get("t", [])
    prefs = diary.get("p", [])
    is_solution = diary.get("sol", False)

    if not summary:
        return

    # 写入 journal（返回当前长度）
    journal_len = 0
    try:
        journal_len = remember(user_id, user_name, summary, "", tags,
                               solution=is_solution)
    except Exception:
        logger.debug("write_diary: remember failed", exc_info=True)

    # 累加 dream 会话计数器
    try:
        from app.services.dream import increment_session_counter
        increment_session_counter()
    except Exception:
        pass

    # 用户画像：把“用户是谁/正在做什么/需要什么”作为长期模型维护。
    try:
        update_user_profile(user_id, _profile_updates_from_diary(
            user_name=user_name,
            summary=summary,
            tags=tags,
            prefs=prefs,
            diary=diary,
        ))
        if prefs:
            # 偏好也写入 journal 以便按标签召回
            for pref in prefs:
                remember(user_id, user_name, f"用户偏好: {pref}", "", ["偏好"])
            logger.info("diary: saved %d preference(s) for %s: %s",
                        len(prefs), user_name, "; ".join(p[:40] for p in prefs))
    except Exception:
        logger.debug("write_diary: user profile save failed", exc_info=True)

    logger.info("diary: %s [%s] %s", user_name, ",".join(tags), summary[:60])

    # journal 达到压缩阈值时触发后台压缩
    # 阈值可通过 tenant.memory_journal_max 配置
    compress_threshold = _COMPRESS_THRESHOLD
    try:
        from app.tenant.context import get_current_tenant
        tenant_max = getattr(get_current_tenant(), "memory_journal_max", 0)
        if tenant_max > 0:
            compress_threshold = tenant_max
    except Exception:
        pass
    if journal_len >= compress_threshold:
        import asyncio
        try:
            asyncio.create_task(compress_old_entries())
        except Exception:
            logger.debug("compress task creation failed", exc_info=True)


def _fallback_diary(
    user_id: str, user_name: str,
    user_text: str, reply: str,
    tool_names_called: list[str],
) -> None:
    """LLM 不可用时的回退日记写入（用工具名推断标签）。"""
    tags = _infer_tags(tool_names_called)
    action = user_text[:80]
    outcome = reply[:100] if reply else ""
    try:
        remember(user_id, user_name, action, outcome, tags)
        update_user_profile(user_id, {
            "name": user_name,
            "recent_topics": tags[:3],
        })
    except Exception:
        logger.debug("_fallback_diary failed", exc_info=True)


def _infer_tags(tool_calls: list[str]) -> list[str]:
    """从工具调用列表推断 tags（回退用）。"""
    tags = set()
    tag_map = {
        "calendar": "日历",
        "task": "任务",
        "doc": "文档",
        "minute": "妙记",
        "git": "代码",
        "pr": "代码",
        "issue": "代码",
        "file": "代码",
        "search": "搜索",
        "message": "消息",
        "self_": "自修复",
        "plan": "规划",
        "memory": "记忆",
        "bitable": "表格",
    }
    for call in tool_calls:
        call_lower = call.lower()
        for keyword, tag in tag_map.items():
            if keyword in call_lower:
                tags.add(tag)
    return list(tags)[:5]


# ── 记忆压缩（远期记忆 → 摘要）──

# 触发压缩的 journal 长度阈值
_COMPRESS_THRESHOLD = 800
# 压缩后保留的近期详细条目数
_KEEP_RECENT = 500
# 每批压缩的条目数（避免单次 LLM 调用过大）
_COMPRESS_BATCH = 50

_COMPRESS_PROMPT = """\
你是记忆压缩助手。将以下多条日记条目压缩成尽量少的摘要条目。

要求：
- 合并同类事件（如多次日历操作→"处理了N个日历事件，包括xxx"）
- 保留关键信息：人名、文档ID/标题、重要决定、具体数字
- 偏好/规则/标准类条目必须完整保留，不可压缩
- 丢弃纯查询类（"查看了xxx"）除非结果有后续影响

输出严格 JSON 数组（不要输出其他内容）：
[{"s":"摘要内容","t":["标签1","标签2"]}]

每条摘要控制在 50 字以内。整个数组通常 3-8 条。\
"""


async def compress_old_entries() -> None:
    """压缩 journal 中的旧条目：近期保留详细，远期压缩为摘要。

    触发条件：journal 长度 >= _COMPRESS_THRESHOLD
    效果：300 条旧记忆 → ~30 条压缩摘要，信息不丢失只精炼。
    """
    all_entries = memory_store.read_journal_all()
    total = len(all_entries)
    if total < _COMPRESS_THRESHOLD:
        return

    # 分割：旧条目（要压缩）+ 近期条目（保留原样）
    split_idx = total - _KEEP_RECENT
    old_entries = all_entries[:split_idx]
    recent_entries = all_entries[split_idx:]

    logger.info("compressing journal: %d total, %d old → compress, %d recent → keep",
                total, len(old_entries), len(recent_entries))

    # 分批压缩旧条目
    compressed_all: list[dict] = []
    for i in range(0, len(old_entries), _COMPRESS_BATCH):
        batch = old_entries[i:i + _COMPRESS_BATCH]

        # 提取时间范围
        times = [e.get("time", "")[:10] for e in batch if e.get("time")]
        time_range = f"{times[0]}~{times[-1]}" if len(times) >= 2 else (times[0] if times else "")

        # 收集所有标签
        all_tags: set[str] = set()
        for e in batch:
            all_tags.update(e.get("tags", []))

        # 格式化条目为文本给 LLM
        lines = []
        for e in batch:
            if e.get("type") == "bot_action":
                lines.append(f"- {e.get('details', '')}")
            else:
                action = e.get("action", "")
                outcome = e.get("outcome", "")
                line = f"- {action}"
                if outcome:
                    line += f" → {outcome}"
                lines.append(line)

        batch_text = "\n".join(lines)
        try:
            result = await _llm_json_call(_COMPRESS_PROMPT, batch_text)
        except Exception:
            logger.debug("compress batch failed", exc_info=True)
            # 压缩失败时保留原始条目（不丢数据）
            compressed_all.extend(batch)
            continue

        if result and isinstance(result, list):
            for item in result:
                compressed_all.append({
                    "type": "compressed",
                    "summary": item.get("s", ""),
                    "tags": item.get("t", list(all_tags)),
                    "time_range": time_range,
                    "time": times[-1] if times else "",
                })
        elif result and isinstance(result, dict):
            # LLM 返回了单个 dict 而不是数组
            compressed_all.append({
                "type": "compressed",
                "summary": result.get("s", ""),
                "tags": result.get("t", list(all_tags)),
                "time_range": time_range,
                "time": times[-1] if times else "",
            })
        else:
            # 解析失败，保留原始
            compressed_all.extend(batch)

    # 重写 journal：压缩摘要 + 近期详细
    new_journal = compressed_all + recent_entries
    ok = memory_store.rewrite_journal(new_journal)
    if ok:
        logger.info("journal compressed: %d → %d entries (%d compressed + %d recent)",
                     total, len(new_journal), len(compressed_all), len(recent_entries))
    else:
        logger.warning("journal rewrite failed, keeping original")


# ── LLM 调用（日记 + 回忆决策）──


_DIARY_PROMPT = """\
你是日记助手。根据用户和bot的对话，生成一条日记条目。

输出严格 JSON（不要输出其他内容）：
{"s":"摘要","t":["标签"],"p":["偏好"],"uf":["用户事实"],"g":["当前目标"],"ol":["未完成事项"],"ent":["重要对象"],"style":["沟通风格"],"need":"最近需求","w":true,"sol":false}

字段说明：
- s: 摘要（50字以内）。如果涉及文档/文件/链接，务必把标题、ID或URL带上，方便以后找到。
  如果涉及预测、估算、承诺或结论，必须保留实体名、关键数字、时间口径和结论。
- t: 话题标签（1-3个，从：日历、任务、文档、代码、消息、搜索、表格、部署、规划、其他）
- p: 用户表达的偏好/规则/标准/习惯/约定（没有则空数组[]）。格式「领域: 内容」
- uf: 关于用户是谁、角色、团队、背景、长期职责的稳定事实（没有则空数组[]）。
- g: 用户正在做什么、当前目标、正在推进的项目或想达成的结果（没有则空数组[]）。
- ol: 需要后续跟进的未完成事项、待验证问题、承诺的下一步（没有则空数组[]）。
- ent: 用户当前关注的重要人、项目、产品、游戏、公司、bot、仓库等实体名（没有则空数组[]）。
- style: 用户希望你怎样互动、汇报、协作的风格（没有则空数组[]）。
- need: 用户这轮真正需要什么。尽量写成自然语言；如果没有明确需求则空字符串。
- w: 是否值得记录（true/false）。纯寒暄("你好""谢谢")、简单确认("好的""收到")= false
- sol: 是否包含可复用的解决方案（true/false）。当 bot 帮用户解决了具体问题（修 bug、配置、排错等），
  其他用户遇到类似问题也能借鉴时 = true。纯查询/闲聊 = false

示例：
用户: 帮我查一下明天有什么会 / Bot: 明天有3个会议...
→ {"s":"查询明天的会议安排，共3个","t":["日历"],"p":[],"uf":[],"g":["了解明天会议安排"],"ol":[],"ent":[],"style":[],"need":"查询明天有哪些会议","w":true,"sol":false}

用户: 以后开会标题统一用「部门-主题-日期」格式 / Bot: 好的，我记住了
→ {"s":"用户设定了会议命名规则","t":["日历"],"p":["日历命名: 会议标题格式为「部门-主题-日期」"],"uf":[],"g":[],"ol":[],"ent":[],"style":["偏好明确、可复用的格式约定"],"need":"记录会议命名规则","w":true,"sol":false}

用户: 碰撞检测那个 bug 怎么修？/ Bot: 发现是 hitbox 偏移了 2px，改了 collision.py 第 47 行
→ {"s":"修复碰撞检测 bug: hitbox 偏移 2px，改 collision.py:47","t":["代码"],"p":[],"uf":[],"g":["修复碰撞检测 bug"],"ol":[],"ent":["collision.py"],"style":[],"need":"确认碰撞检测 bug 的修复方法","w":true,"sol":true}

用户: dashboard 里的 memory 记得东西没用，不像智能体那样知道用户是谁、在做什么、需要什么 / Bot: 我会改成长期用户模型
→ {"s":"用户要求改进 memory: 记住用户是谁、在做什么、需要什么","t":["代码","规划"],"p":["协作: 直接指出问题并落地修复"],"uf":["用户负责评估 bot 产品体验和线上行为"],"g":["提升 bot 长期记忆的用户理解能力"],"ol":["验证 memory dashboard 能否展示有效用户画像"],"ent":["memory dashboard","4d-bot"],"style":["希望直接承认问题并给出可执行修复"],"need":"让 bot 的记忆更像自然智能体的长期用户模型","w":true,"sol":false}

用户: 谢谢 / Bot: 不客气
→ {"s":"","t":[],"p":[],"uf":[],"g":[],"ol":[],"ent":[],"style":[],"need":"","w":false,"sol":false}\
"""


_RECALL_PROMPT = """\
用户发来一条新消息。判断bot是否需要查阅历史记忆来更好地回应。

输出严格 JSON（不要输出其他内容）：
{"r":true,"t":["标签"],"k":"关键词"}

字段说明：
- r: 是否需要回忆（true/false）
  true: 用户提到了之前做过的事、涉及具体项目/人/文档、需要上下文才能理解
  false: 简单问候、明确独立的指令（如"翻译这段话"）、不需要历史上下文
- t: 应该搜索的标签（从：日历、任务、文档、代码、消息、搜索、表格、部署、规划、偏好）
- k: 搜索关键词（如人名、项目名、事件名，可选，无则空字符串""）

示例：
"上次帮我创建的那个文档叫什么" → {"r":true,"t":["文档"],"k":""}
"帮我建个明天下午3点的会" → {"r":true,"t":["日历","偏好"],"k":"会议"}
"你好" → {"r":false,"t":[],"k":""}
"把这段翻译成英文" → {"r":false,"t":[],"k":""}\
"""


# ── P2: 记忆自组织（空闲时经验蒸馏）──


_DISTILL_PROMPT = """\
你是经验蒸馏助手。分析以下日记条目，提炼出可复用的经验规则。

要求：
- 找出重复出现的模式（如"用户经常要求XX格式"、"XX工具配合YY效果好"）
- 提炼为简短、可操作的规则（如"创建文档后主动转让 owner 给用户"）
- 忽略一次性事件，只保留有普遍价值的经验
- 每条规则 30 字以内

输出严格 JSON 数组（不要输出其他内容）：
[{"rule":"规则内容","tags":["标签"],"source_count":N}]

source_count 表示这条规则基于多少条日记提炼。通常 3-8 条规则。\
"""


async def distill_experience() -> list[dict]:
    """空闲时经验蒸馏：从最近日记中提炼可复用的经验规则。

    返回提炼出的规则列表。规则会写入 capability_module 和用户画像。
    由 scheduler 在空闲时段调用。
    """
    # 读取最近 50 条日记（不含压缩条目）
    recent = _read_journal_safe(limit=50)
    if len(recent) < 10:
        return []  # 日记太少，不值得提炼

    # 过滤掉压缩条目
    detailed = [e for e in recent if e.get("type") != "compressed"]
    if len(detailed) < 8:
        return []

    # 格式化给 LLM
    lines = []
    for e in detailed:
        if e.get("type") == "bot_action":
            lines.append(f"- [bot] {e.get('details', '')}")
        else:
            action = e.get("action", "")
            tags = ",".join(e.get("tags", []))
            lines.append(f"- [{tags}] {action}")

    batch_text = "\n".join(lines)
    try:
        result = await _llm_json_call(_DISTILL_PROMPT, batch_text)
    except Exception:
        logger.debug("distill_experience: LLM call failed", exc_info=True)
        return []

    if not result or not isinstance(result, list):
        return []

    # 写入经验规则到 tool_tracker（作为 lesson）
    rules = []
    for item in result:
        rule = item.get("rule", "")
        tags = item.get("tags", [])
        if not rule:
            continue
        rules.append({"rule": rule, "tags": tags})

        # 写入 tool_tracker 作为经验教训
        try:
            from app.services.tool_tracker import record_lesson
            from app.tenant.context import get_current_tenant
            tid = get_current_tenant().tenant_id
            # 用第一个 tag 作为工具名（近似关联）
            tool_hint = tags[0] if tags else "general"
            record_lesson(tid, tool_hint, rule, context="distilled")
        except Exception:
            pass

    if rules:
        # 记录到 journal 便于追溯
        try:
            entry = {
                "type": "distilled",
                "summary": f"经验蒸馏: 提炼了 {len(rules)} 条规则",
                "rules": [r["rule"] for r in rules],
                "tags": ["经验蒸馏"],
                "time": datetime.now(timezone.utc).isoformat(),
            }
            memory_store.append_journal(entry)
        except Exception:
            pass

        logger.info("distill_experience: extracted %d rules", len(rules))

    return rules


async def _llm_diary_entry(
    user_text: str, reply: str, tool_names: list[str] | None,
    action_outcomes: list[tuple[str, str]] | None = None,
) -> dict | None:
    """用 LLM 生成日记条目。返回解析后的 dict 或 None。"""
    tools_str = ", ".join(tool_names) if tool_names else "无"
    content = (
        f"用户: {user_text[:400]}\n"
        f"Bot: {reply[:800]}\n"
        f"调用的工具: {tools_str}"
    )
    # 附加工具执行结果摘要（包含 URL、文档 ID 等关键数据）
    if action_outcomes:
        outcomes_str = "\n".join(
            f"  {name} {outcome}" for name, outcome in action_outcomes[:10]
        )
        content += f"\n工具执行结果:\n{outcomes_str}"
    return await _llm_json_call(_DIARY_PROMPT, content)


async def _llm_recall_decision(user_text: str) -> dict | None:
    """用 LLM 判断是否需要回忆。返回解析后的 dict 或 None。"""
    return await _llm_json_call(_RECALL_PROMPT, user_text[:200])


async def _llm_json_call(system_prompt: str, user_content: str) -> dict | list | None:
    """通用轻量 LLM 调用，返回 JSON dict 或 list。"""
    from openai import AsyncOpenAI
    from app.config import settings

    try:
        client = AsyncOpenAI(
            api_key=settings.kimi.api_key,
            base_url=settings.kimi.base_url,
        )
        resp = await client.chat.completions.create(
            model=settings.kimi.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0,
            max_tokens=420,
        )
        answer = resp.choices[0].message.content.strip()
        # 尝试提取 JSON（兼容 LLM 输出前后有多余文本）
        # 优先尝试 object {...}，再尝试 array [...]
        m = re.search(r"\{.*\}", answer, re.DOTALL)
        if m:
            return json.loads(m.group())
        m = re.search(r"\[.*\]", answer, re.DOTALL)
        if m:
            return json.loads(m.group())
        return None
    except Exception:
        logger.debug("_llm_json_call failed", exc_info=True)
        return None
