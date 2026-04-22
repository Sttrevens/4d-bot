"""Agent 路由绑定系统

借鉴 OpenClaw 的 binding-based routing：
  不同 channel / chat / user 可以路由到不同的 agent 配置。
  每个 agent 配置有独立的 system prompt、工具集、模型选择。

OpenClaw 用 bindings 数组按 channel/peer/guild/role 匹配 agent。
我们简化为适合自己架构的版本：

  AgentProfile = 一套 agent 配置（人格 + 工具 + 模型）
  AgentBinding = 路由规则（匹配条件 → profile_id）

匹配优先级（从高到低）：
  1. chat_id 精确匹配（特定群/对话绑定特定人格）
  2. sender_id 精确匹配（特定用户绑定特定人格）
  3. channel_platform 匹配（整个平台绑定特定人格）
  4. 默认 profile（tenant 级别的 fallback）

使用方式:
  在 tenants.json 中配置：
  {
    "tenant_id": "my-bot",
    "agent_profiles": [
      {
        "profile_id": "support",
        "name": "客服小助手",
        "system_prompt": "你是一个友善的客服...",
        "tools_enabled": ["web_search", "create_document"]
      },
      {
        "profile_id": "dev",
        "name": "开发助手",
        "system_prompt": "你是一个代码专家...",
        "tools_enabled": []
      }
    ],
    "agent_bindings": [
      {"match": {"platform": "discord"}, "profile_id": "support"},
      {"match": {"platform": "feishu"}, "profile_id": "dev"},
      {"match": {"chat_id": "oc_abc123"}, "profile_id": "support"}
    ]
  }
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AgentProfile:
    """一套 agent 配置（人格）。

    同一个 bot（tenant）可以有多个 profile，
    不同 channel/chat/user 通过 binding 路由到不同 profile。
    """

    profile_id: str = "default"
    name: str = ""                    # 显示名（如 "客服小助手"）
    system_prompt: str = ""           # 覆盖 tenant 的 llm_system_prompt
    tools_enabled: list[str] = field(default_factory=list)  # 覆盖 tenant 的 tools_enabled
    model: str = ""                   # 覆盖 tenant 的 llm_model
    custom_persona: bool = False      # 覆盖 tenant 的 custom_persona（仅影响身份模板，不跳过全局政策）


@dataclass
class AgentBindingMatch:
    """路由匹配条件。

    所有字段都是可选的，只匹配设置了的字段。
    多个字段同时设置时，所有字段必须都匹配（AND 逻辑）。
    """

    platform: str = ""        # "feishu" / "discord" / "wecom" 等
    chat_id: str = ""         # 特定聊天 ID
    chat_type: str = ""       # "p2p" / "group"
    sender_id: str = ""       # 特定用户 ID


@dataclass
class AgentBinding:
    """路由规则：匹配条件 → profile_id"""

    match: AgentBindingMatch = field(default_factory=AgentBindingMatch)
    profile_id: str = "default"
    comment: str = ""         # 备注（不影响逻辑）


def resolve_agent_profile(
    profiles: list[AgentProfile],
    bindings: list[AgentBinding],
    *,
    platform: str = "",
    chat_id: str = "",
    chat_type: str = "",
    sender_id: str = "",
) -> Optional[AgentProfile]:
    """根据上下文匹配最佳 agent profile。

    匹配优先级按 specificity 排序（匹配条件越多越优先）。
    无匹配返回 None（调用方应 fallback 到 tenant 默认配置）。
    """
    if not profiles or not bindings:
        return None

    profiles_by_id = {p.profile_id: p for p in profiles}

    # 计算每个 binding 的匹配分数
    best_score = 0
    best_profile_id = ""

    for binding in bindings:
        m = binding.match
        score = 0

        # 检查每个匹配条件
        if m.sender_id:
            if m.sender_id != sender_id:
                continue  # 条件不满足，跳过
            score += 8  # sender 精确匹配最高优先级

        if m.chat_id:
            if m.chat_id != chat_id:
                continue
            score += 4  # chat 精确匹配次之

        if m.chat_type:
            if m.chat_type != chat_type:
                continue
            score += 2

        if m.platform:
            if m.platform != platform:
                continue
            score += 1  # platform 最低优先级

        # 所有条件都满足（或都为空）
        if score == 0:
            continue  # 空匹配不算

        if score > best_score:
            best_score = score
            best_profile_id = binding.profile_id

    if not best_profile_id:
        return None

    profile = profiles_by_id.get(best_profile_id)
    if not profile:
        logger.warning(
            "agent_routing: binding matched profile_id='%s' but profile not found",
            best_profile_id,
        )
        return None

    logger.debug(
        "agent_routing: matched profile='%s' (score=%d) for platform=%s chat=%s sender=%s",
        best_profile_id, best_score, platform, chat_id[:12] if chat_id else "", sender_id[:12] if sender_id else "",
    )
    return profile


def parse_profiles_from_config(raw_list: list[dict]) -> list[AgentProfile]:
    """从 tenants.json 的 agent_profiles 字段解析。"""
    profiles = []
    for item in raw_list:
        profiles.append(AgentProfile(
            profile_id=item.get("profile_id", "default"),
            name=item.get("name", ""),
            system_prompt=item.get("system_prompt", ""),
            tools_enabled=item.get("tools_enabled", []),
            model=item.get("model", ""),
            custom_persona=item.get("custom_persona", False),
        ))
    return profiles


def parse_bindings_from_config(raw_list: list[dict]) -> list[AgentBinding]:
    """从 tenants.json 的 agent_bindings 字段解析。"""
    bindings = []
    for item in raw_list:
        match_raw = item.get("match", {})
        bindings.append(AgentBinding(
            match=AgentBindingMatch(
                platform=match_raw.get("platform", ""),
                chat_id=match_raw.get("chat_id", ""),
                chat_type=match_raw.get("chat_type", ""),
                sender_id=match_raw.get("sender_id", ""),
            ),
            profile_id=item.get("profile_id", "default"),
            comment=item.get("comment", ""),
        ))
    return bindings
