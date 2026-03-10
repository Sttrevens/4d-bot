"""飞书妙记操作工具

通过飞书 Minutes v1 API 获取会议纪要：
- 获取妙记元信息（标题、时长、创建时间）
- 获取妙记文字转录内容

注意：妙记 API 需要 user_access_token，部分接口可能需要用户授权。
如果 tenant_access_token 无权限，会返回明确的错误提示。
"""

from __future__ import annotations

import logging
import re

from app.tools.feishu_api import feishu_get
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)


def _extract_minute_token(token_or_url: str) -> str:
    """从飞书妙记 URL 或纯 token 中提取 minute_token

    URL 格式: https://xxx.feishu.cn/minutes/obcn25hd72pjji56wptqs7ur
    """
    token_or_url = token_or_url.strip()
    m = re.search(r"feishu\.cn/minutes/([A-Za-z0-9]+)", token_or_url)
    if m:
        return m.group(1)
    if "?" in token_or_url:
        token_or_url = token_or_url.split("?")[0]
    return token_or_url


def get_minute(minute_token: str) -> ToolResult:
    """获取妙记元信息（标题、时长、参与人等）"""
    token = _extract_minute_token(minute_token)
    if not token:
        return ToolResult.invalid_param("请提供妙记 token 或飞书妙记链接")

    # 先用 user_token，如果 scope 不足则回退到 tenant_token
    user_token_denied = False
    data = feishu_get(f"/minutes/v1/minutes/{token}", use_user_token=True)
    if isinstance(data, str) and "99991679" in data:
        user_token_denied = True
        logger.info("get_minute: user_token scope不足，回退到 tenant_token")
        data = feishu_get(f"/minutes/v1/minutes/{token}", use_user_token=False)

    if isinstance(data, str):
        # feishu_api 返回的错误消息已自带行动指引，直接透传
        return ToolResult.api_error(data)

    minute = data.get("data", {}).get("minute", {})
    title = minute.get("title", "(无标题)")
    owner_id = minute.get("owner_id", "")
    create_time = minute.get("create_time", "")
    duration_ms = minute.get("duration", "0")

    # 转换时长
    try:
        duration_s = int(duration_ms) // 1000
        mins, secs = divmod(duration_s, 60)
        duration_str = f"{mins}分{secs}秒"
    except (ValueError, TypeError):
        duration_str = duration_ms

    url = minute.get("url", f"https://feishu.cn/minutes/{token}")

    return ToolResult.success(
        f"妙记: {title}\n"
        f"时长: {duration_str}\n"
        f"创建者: {owner_id}\n"
        f"链接: {url}"
    )


def _fetch_transcript(token: str, use_user_token: bool) -> str | dict:
    """请求转录 API，返回原始结果"""
    return feishu_get(
        f"/minutes/v1/minutes/{token}/transcript",
        params={"file_format": "txt", "need_speaker": "true"},
        use_user_token=use_user_token,
    )


def get_minute_transcript(minute_token: str) -> ToolResult:
    """获取妙记的文字转录内容"""
    token = _extract_minute_token(minute_token)
    if not token:
        return ToolResult.invalid_param("请提供妙记 token 或飞书妙记链接")

    # 先用 user_token，如果 scope 不足（99991679）则回退到 tenant_token
    data = _fetch_transcript(token, use_user_token=True)
    if isinstance(data, str) and "99991679" in data:
        logger.info("minutes transcript: user_token scope不足，回退到 tenant_token")
        data = _fetch_transcript(token, use_user_token=False)

    # 转录接口可能返回文件内容而非 JSON (feishu_get 已处理 200 OK 的非 JSON 情况)
    if isinstance(data, str):
        if data.startswith("[ERROR]"):
            # feishu_api 返回的错误消息已自带行动指引，直接透传给模型
            return ToolResult.api_error(data)
        # 成功获取到文本内容
        if len(data) > 30000:
            data = data[:30000] + "\n\n... (转录过长已截断)"
        return ToolResult.success(data)

    # 如果返回了 JSON 结构
    content = data.get("data", {}).get("content", "")
    if content:
        if len(content) > 30000:
            content = content[:30000] + "\n\n... (转录过长已截断)"
        return ToolResult.success(content)

    return ToolResult.success("未获取到转录内容，可能妙记还在处理中或无权限。")


# --------------- Tool definitions & map ---------------

TOOL_DEFINITIONS = [
    {
        "name": "get_feishu_minute",
        "description": "获取飞书妙记（会议纪要）的元信息：标题、时长、创建者。可传妙记 token 或飞书链接。",
        "input_schema": {
            "type": "object",
            "properties": {
                "minute_token": {
                    "type": "string",
                    "description": "妙记 token 或飞书妙记链接（如 https://xxx.feishu.cn/minutes/obcn25hd72pjji56wptqs7ur）",
                },
            },
            "required": ["minute_token"],
        },
    },
    {
        "name": "get_feishu_minute_transcript",
        "description": "获取飞书妙记的文字转录内容（会议录音转文字）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "minute_token": {
                    "type": "string",
                    "description": "妙记 token 或飞书妙记链接（如 https://xxx.feishu.cn/minutes/obcn25hd72pjji56wptqs7ur）",
                },
            },
            "required": ["minute_token"],
        },
    },
]

TOOL_MAP = {
    "get_feishu_minute": lambda args: get_minute(
        minute_token=args["minute_token"],
    ),
    "get_feishu_minute_transcript": lambda args: get_minute_transcript(
        minute_token=args["minute_token"],
    ),
}
