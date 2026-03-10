"""视频 URL 分析工具

通过 sandbox_caps 能力原语实现：下载视频 → Gemini 分析。
本文件的代码完全符合沙箱安全规则（只 import 白名单模块），
bot 理论上可以自己写出等价的 custom tool。

sandbox_caps 提供的能力:
- download_video(url) → VideoData | str
- gemini_analyze_video(data, prompt) → AnalysisResult | str
- is_youtube_url(url) → bool
- gemini_analyze_youtube_url(url, prompt) → AnalysisResult | str  (YouTube 直传，无需下载)
"""

import logging

from app.tools.tool_result import ToolResult
from app.tools.sandbox_caps import (
    download_video,
    gemini_analyze_video,
    gemini_analyze_youtube_url,
    get_video_info,
    is_youtube_url,
    VideoInfo,
)

logger = logging.getLogger(__name__)


def analyze_video_url(args: dict) -> ToolResult:
    """下载视频 URL 并通过 Gemini 分析内容。"""
    url = args.get("url", "").strip()
    question = args.get("question", "")

    if not url:
        return ToolResult.invalid_param("请提供视频 URL")

    # YouTube URL → 直传 Gemini（不需要下载/上传）
    if is_youtube_url(url):
        return _analyze_youtube_direct(url, question)

    # 非 YouTube → yt-dlp 下载再上传
    # 1. 下载视频
    result = download_video(url)
    if isinstance(result, str):
        return ToolResult.error(result)

    video = result  # VideoData

    # 2. 构建分析 prompt
    info = video.info
    prompt = (
        f"这是一段来自网络的视频。标题: {info.title}，作者: {info.uploader}。\n"
        f"请仔细观看视频的画面和音频内容。\n"
    )
    if question:
        prompt += f"\n用户的问题是: {question}\n请针对这个问题分析视频内容并回答。"
    else:
        prompt += "\n请详细描述视频的主要内容，包括画面、对话/旁白、关键信息等。"

    # 3. Gemini 分析
    analysis = gemini_analyze_video(video.data, prompt)
    if isinstance(analysis, str):
        return ToolResult.error(analysis)

    # 4. 构建结果
    header = f"**{info.title}**\n作者: {info.uploader}"
    if info.duration:
        mins, secs = divmod(info.duration, 60)
        header += f" | 时长: {mins}:{secs:02d}"

    return ToolResult.success(f"{header}\n\n{analysis.text}")


def _analyze_youtube_direct(url: str, question: str) -> ToolResult:
    """YouTube 视频直传 Gemini 分析（无需下载）。

    Gemini 原生支持 YouTube URL，通过 from_uri 直传。
    仍尝试用 yt-dlp 获取元信息（标题/作者/时长），失败不影响分析。
    """
    # 尝试获取元信息（可选，失败继续）
    info = get_video_info(url)
    has_info = isinstance(info, VideoInfo)

    if has_info and info.duration > 3600:
        return ToolResult.error(
            f"视频时长 {info.duration // 60} 分钟，超过 60 分钟限制"
        )

    # 构建 prompt
    if has_info:
        prompt = f"这是一段 YouTube 视频。标题: {info.title}，作者: {info.uploader}。\n"
    else:
        prompt = "这是一段 YouTube 视频。\n"
    prompt += "请仔细观看视频的画面和音频内容。\n"

    if question:
        prompt += f"\n用户的问题是: {question}\n请针对这个问题分析视频内容并回答。"
    else:
        prompt += "\n请详细描述视频的主要内容，包括画面、对话/旁白、关键信息等。"

    # Gemini 直接分析 YouTube URL（无需下载/上传）
    analysis = gemini_analyze_youtube_url(url, prompt)
    if isinstance(analysis, str):
        # from_uri 失败，回退到 yt-dlp 下载路径
        logger.warning("YouTube from_uri failed: %s, trying yt-dlp fallback", analysis)
        result = download_video(url)
        if isinstance(result, str):
            # yt-dlp 也失败，返回两个错误供诊断
            logger.error("YouTube yt-dlp fallback also failed: %s", result)
            return ToolResult.error(
                f"YouTube 视频分析失败。\n"
                f"直传错误: {analysis}\n"
                f"下载错误: {result}"
            )
        # 下载成功，上传到 Gemini File API 分析
        logger.info("YouTube yt-dlp download OK (%dMB), uploading to Gemini",
                     len(result.data) // (1024 * 1024))
        fallback_analysis = gemini_analyze_video(result.data, prompt)
        if isinstance(fallback_analysis, str):
            return ToolResult.error(fallback_analysis)
        analysis = fallback_analysis
        if not has_info:
            has_info = True
            info = result.info

    # 构建结果
    if has_info:
        header = f"**{info.title}**\n作者: {info.uploader}"
        if info.duration:
            mins, secs = divmod(info.duration, 60)
            header += f" | 时长: {mins}:{secs:02d}"
    else:
        header = f"**YouTube 视频**\n{url}"

    return ToolResult.success(f"{header}\n\n{analysis.text}")


TOOL_DEFINITIONS = [
    {
        "name": "analyze_video_url",
        "description": (
            "从 YouTube/Bilibili/抖音 等平台 URL 下载视频并分析内容。"
            "YouTube 链接会直传 Gemini 分析（无需下载），其他平台走 yt-dlp 下载。"
            "支持长视频（最长约 1 小时），当用户发送视频链接并想了解视频内容时使用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "视频 URL（支持 YouTube、Bilibili、抖音、Vimeo、TikTok 等）",
                },
                "question": {
                    "type": "string",
                    "description": "用户关于视频的具体问题（可选，不填则生成内容摘要）",
                    "default": "",
                },
            },
            "required": ["url"],
        },
    },
]

TOOL_MAP = {
    "analyze_video_url": analyze_video_url,
}
