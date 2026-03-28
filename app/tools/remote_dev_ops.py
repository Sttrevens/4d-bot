"""远程开发桥接工具 —— 转发开发任务到本地 Claude Code 实例

通过 tunnel（如 Cloudflare Tunnel）连接本地 Claude Code + MCP 集成的开发环境。
适用于需要本地 IDE/编辑器集成的场景（如 Unity Editor、VS Code 等）。

优先级：
1. CC 在线 → remote_dev_request 转发到本地 Claude Code（能验证编译、操作编辑器）
2. CC 离线 → 返回降级标记，告诉 LLM 用 search_code/read_file/write_file 通过 GitHub API 改
"""

from __future__ import annotations

import logging
import os

import httpx

from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

# 环境变量：本地 CC bridge 的 URL 和认证 token
# 通过 Cloudflare Tunnel 或其他隧道暴露本地 CC bridge 端点
_BRIDGE_URL = os.getenv("REMOTE_DEV_BRIDGE_URL", "")
_BRIDGE_TOKEN = os.getenv("REMOTE_DEV_BRIDGE_TOKEN", "")


async def remote_dev_request(args: dict) -> ToolResult:
    """转发开发任务到本地 Claude Code 实例。CC 离线时自动降级。"""
    request_text = (args.get("request") or args.get("command") or args.get("query") or "").strip()
    if not request_text:
        return ToolResult.invalid_param(
            "请描述你需要对工程做什么",
            retry_hint="request 参数不能为空",
        )

    # 未配置或连接失败 → 降级
    if not _BRIDGE_URL:
        return _fallback_result(request_text)

    from app.tenant.context import get_current_sender, get_current_channel, get_current_chat_id
    sender = get_current_sender()
    ch = get_current_channel()
    user_name = sender.sender_name

    # 工作流后缀：指导 CC 完成改动后走 branch → commit → PR 流程
    workflow_suffix = (
        "\n\n---\n"
        "[Workflow] After completing the changes:\n"
        "1. Create a new branch from current HEAD (name: feat/<short-description>)\n"
        "2. Commit all changes with a clear message\n"
        "3. Push the branch to origin\n"
        "4. Create a GitHub PR with a summary of what was changed and why\n"
        "5. Reply with the PR URL so the team can review and test\n"
        f"6. Mention that this request came from {user_name or 'a teammate'} via chat"
    )

    payload = {
        "platform": ch.platform if ch else "feishu",
        "chat_id": get_current_chat_id(),
        "user_id": sender.sender_id,
        "user_name": user_name,
        "text": request_text + workflow_suffix,
    }

    headers = {"Content-Type": "application/json"}
    if _BRIDGE_TOKEN:
        headers["Authorization"] = f"Bearer {_BRIDGE_TOKEN}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{_BRIDGE_URL}/inbound", json=payload, headers=headers)

        if resp.status_code == 200:
            logger.info("remote_dev_request forwarded: user=%s text=%s", user_name, request_text[:80])
            return ToolResult.success(
                "已转发到本地开发助手（Claude Code + MCP），处理完成后会直接回复到这个对话。"
            )
        else:
            logger.error("dev bridge HTTP %d: %s", resp.status_code, resp.text[:200])
            return _fallback_result(request_text)

    except (httpx.ConnectError, httpx.ConnectTimeout):
        logger.warning("dev bridge unreachable, falling back to GitHub API")
        return _fallback_result(request_text)
    except Exception as e:
        logger.exception("remote_dev_request failed")
        return _fallback_result(request_text)


def _fallback_result(request_text: str) -> ToolResult:
    """CC 离线时的降级指令——告诉 LLM 用 GitHub API 自己改。"""
    return ToolResult.success(
        "[CC 离线降级] 开发者本地 Claude Code 不可用。"
        "请直接用 search_code + read_file 定位代码，然后用 write_file 修改。"
        "改完后用 git_create_branch + create_pull_request 提交 PR。"
        "\n\n注意：此模式无法验证编译、无法修改二进制文件。"
        "仅限脚本和文本配置文件的修改。"
        f"\n\n原始需求：{request_text}"
    )


TOOL_DEFINITIONS = [
    {
        "name": "remote_dev_request",
        "description": (
            "将开发任务转发到本地 Claude Code 实例（通过 tunnel 连接）。"
            "本地 CC 可以访问 IDE/编辑器 MCP 集成，能验证编译、操作编辑器。"
            "开发者离线时自动降级：返回指令让你用 search_code/read_file/write_file 通过 GitHub API 直接改。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": "对工程的操作请求，用自然语言描述。",
                },
            },
            "required": ["request"],
        },
    },
]

TOOL_MAP = {
    "remote_dev_request": remote_dev_request,
}
