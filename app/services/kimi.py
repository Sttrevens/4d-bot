"""Kimi (Moonshot) API 客户端

两个职责:
1. classify_intent  — 判断用户消息是「编码任务」还是「普通对话」
2. chat             — 普通对话 / 闲聊 / 日常问答
"""

from __future__ import annotations

import logging
import re
from enum import Enum

from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)


class Intent(str, Enum):
    CODE = "code"
    CHAT = "chat"


_INTENT_SYSTEM_PROMPT = """\
你是一个意图分类器。用户会发送一条消息，你需要判断它属于以下哪一类：

1. **code** — 编码/开发任务以及仓库操作。包括但不限于：
   - 写代码、改 bug、重构
   - 读取文件内容、创建/修改文件
   - 任何 git 相关操作：查看分支、创建分支、删除分支、查看 commit、diff
   - 查看仓库状态、列出文件/目录
   - 提交 PR、代码审查
   - 项目构建、部署相关
   只要涉及代码仓库的**查询或操作**，都算 code。

2. **chat** — 普通对话。包括闲聊、知识问答、翻译、写作等与代码仓库无关的任务。

示例:
- "现在git里有哪些分支" → code
- "帮我创建一个新分支" → code
- "看看 main 最近的提交记录" → code
- "读一下 README.md" → code
- "帮我写个登录页面" → code
- "今天天气怎么样" → chat
- "帮我翻译一下这段话" → chat

只回复一个单词: code 或 chat，不要输出其他内容。\
"""

# 关键词预检测：包含这些关键词的消息直接路由到 CODE，不需要 LLM 判断
_CODE_KEYWORDS = re.compile(
    r"(git|branch|分支|commit|提交|PR|pull.?request|merge|合并"
    r"|代码|代码审查|review|bug|文件|读取|写入|创建文件|修改文件"
    r"|push|pull|clone|fetch|diff|log|仓库|repo|部署|deploy"
    r"|重构|refactor|build|构建|编译|compile)",
    re.IGNORECASE,
)


def _build_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=settings.kimi.api_key,
        base_url=settings.kimi.base_url,
    )


def _is_k2_model() -> bool:
    """检查当前模型是否是 K2/K2.5 系列（需要特殊参数）"""
    model = settings.kimi.model.lower()
    return "k2" in model


def _extra_body() -> dict | None:
    """K2.5 模型需要关闭 thinking 模式以使用 instant 模式"""
    if _is_k2_model():
        return {"chat_template_kwargs": {"thinking": False}}
    return None


async def classify_intent(text: str) -> Intent:
    """调用 Kimi 判断用户意图，带关键词快速通道"""

    # 快速通道：包含明确的代码/仓库关键词时直接返回 CODE
    if _CODE_KEYWORDS.search(text):
        logger.info("intent fast-path: CODE (keyword match)")
        return Intent.CODE

    client = _build_client()
    try:
        kwargs: dict = dict(
            model=settings.kimi.model,
            messages=[
                {"role": "system", "content": _INTENT_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=1 if _is_k2_model() else 0,
            max_tokens=10,
        )
        extra = _extra_body()
        if extra:
            kwargs["extra_body"] = extra

        resp = await client.chat.completions.create(**kwargs)
        answer = resp.choices[0].message.content.strip().lower()
        if answer.startswith("code"):
            return Intent.CODE
        return Intent.CHAT
    except Exception as exc:
        logger.exception("kimi classify_intent failed: %s", exc)
        return Intent.CHAT


async def chat(text: str, history: list[dict] | None = None) -> str:
    """调用 Kimi 进行普通对话"""
    client = _build_client()
    messages: list[dict] = [
        {"role": "system", "content": settings.kimi.chat_system_prompt}
    ]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": text})

    try:
        kwargs: dict = dict(
            model=settings.kimi.model,
            messages=messages,
            temperature=1 if _is_k2_model() else 0.7,
            max_tokens=4096,
        )
        extra = _extra_body()
        if extra:
            kwargs["extra_body"] = extra

        resp = await client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        logger.exception("kimi chat failed: %s", exc)
        return "抱歉，我暂时无法回答，请稍后再试。"
