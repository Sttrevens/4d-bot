"""Anthropic Coding Agent (tool use)

接收编码任务描述，使用 Claude + tool use 自动完成:
1. 创建 feature branch
2. 读写文件
3. git commit + push
4. 创建 PR

工具定义从 app/tools/ 下各模块汇聚而来。
"""

from __future__ import annotations

import logging

import anthropic

from app.config import settings
from app.tools.file_ops import (
    TOOL_DEFINITIONS as FILE_TOOLS,
    TOOL_MAP as FILE_TOOL_MAP,
)
from app.tools.git_ops import (
    TOOL_DEFINITIONS as GIT_TOOLS,
    TOOL_MAP as GIT_TOOL_MAP,
)
from app.tools.github_ops import (
    TOOL_DEFINITIONS as GITHUB_TOOLS,
    TOOL_MAP as GITHUB_TOOL_MAP,
)
from app.tools.issue_ops import (
    TOOL_DEFINITIONS as ISSUE_TOOLS,
    TOOL_MAP as ISSUE_TOOL_MAP,
)
from app.tools.repo_search import (
    TOOL_DEFINITIONS as REPO_SEARCH_TOOLS,
    TOOL_MAP as REPO_SEARCH_TOOL_MAP,
)
from app.tools.web_search import (
    TOOL_DEFINITIONS as WEB_TOOLS,
    TOOL_MAP as WEB_TOOL_MAP,
)

logger = logging.getLogger(__name__)

# 合并所有工具
ALL_TOOLS = FILE_TOOLS + GIT_TOOLS + GITHUB_TOOLS + ISSUE_TOOLS + REPO_SEARCH_TOOLS + WEB_TOOLS
ALL_TOOL_MAP = {
    **FILE_TOOL_MAP, **GIT_TOOL_MAP, **GITHUB_TOOL_MAP,
    **ISSUE_TOOL_MAP, **REPO_SEARCH_TOOL_MAP, **WEB_TOOL_MAP,
}

_SYSTEM_PROMPT = """\
你是一个专业的编码助手，在一个 Git 仓库中工作。

你可以使用以下工具完成编码任务:
- read_file / write_file: 读写仓库中的文件
- git_create_branch: 创建 feature 分支（禁止直接操作 main）
- git_commit: 暂存并提交改动
- git_push: 推送当前分支到远端
- create_pull_request: 在 GitHub 上创建 PR

请遵循以下工作流程:
1. 先创建一个有意义的 feature 分支
2. 读取相关文件了解上下文
3. 编写或修改代码
4. commit 并 push
5. 创建 PR

重要安全规则:
- 永远不要直接在 main/master 分支上操作
- commit message 要清晰有意义
- 一次 PR 只做一件事\
"""

# tool use 最大轮次，防止死循环
_MAX_ROUNDS = 20


async def handle_code_task(user_text: str) -> str:
    """核心编码处理流程: 多轮 tool use 直到完成"""
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic.api_key)

    messages: list[dict] = [{"role": "user", "content": user_text}]

    for round_num in range(_MAX_ROUNDS):
        logger.info("coding agent round %d", round_num + 1)

        response = await client.messages.create(
            model=settings.anthropic.model,
            max_tokens=4096,
            system=_SYSTEM_PROMPT,
            tools=ALL_TOOLS,
            messages=messages,
        )

        # 收集本轮 assistant 的所有 content blocks
        assistant_content = response.content

        # 如果模型结束了（没有 tool_use），返回文本回复
        if response.stop_reason == "end_turn":
            text_parts = [
                block.text
                for block in assistant_content
                if block.type == "text"
            ]
            return "\n".join(text_parts) or "任务完成。"

        # 处理 tool_use blocks
        tool_results = []
        for block in assistant_content:
            if block.type != "tool_use":
                continue

            tool_name = block.name
            tool_input = block.input
            logger.info("tool call: %s(%s)", tool_name, tool_input)

            handler = ALL_TOOL_MAP.get(tool_name)
            if handler is None:
                result = f"[ERROR] unknown tool: {tool_name}"
            else:
                try:
                    result = handler(tool_input)
                except Exception as exc:
                    logger.exception("tool %s failed", tool_name)
                    result = f"[ERROR] {exc}"

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(result),
                }
            )

        # 把 assistant 回复和 tool 结果加入上下文
        messages.append({"role": "assistant", "content": assistant_content})
        messages.append({"role": "user", "content": tool_results})

    return "编码任务超过最大轮次限制，请简化任务后重试。"
