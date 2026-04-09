"""Cerul 视频检索工具

通过 Cerul Search API 检索视频语义片段，返回可引用的时间戳与链接。
文档：https://cerul.ai/docs/search-api
"""

from __future__ import annotations

import logging
import os

import httpx

from app.tools.source_registry import register_urls
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

_CERUL_BASE_URL = "https://api.cerul.ai"
_CERUL_TIMEOUT = httpx.Timeout(connect=5.0, read=45.0, write=10.0, pool=5.0)
_RANKING_MODES = {"embedding", "rerank"}


def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _format_ts(seconds: float | int | None) -> str:
    if seconds is None:
        return "--:--"
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "--:--"
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _format_range(start: float | int | None, end: float | int | None) -> str:
    return f"{_format_ts(start)}-{_format_ts(end)}"


def _get_api_key() -> str:
    return os.getenv("CERUL_API_KEY", "").strip()


def _get_base_url() -> str:
    return (os.getenv("CERUL_BASE_URL", _CERUL_BASE_URL) or _CERUL_BASE_URL).rstrip("/")


def _build_client_args() -> dict:
    args: dict = {
        "timeout": _CERUL_TIMEOUT,
        "follow_redirects": True,
    }
    proxy = os.getenv("CERUL_PROXY", "").strip()
    if proxy:
        # 显式代理优先，避免混入全局 HTTPS_PROXY/HTTP_PROXY
        args["proxy"] = proxy
        args["trust_env"] = False
    else:
        # 无显式代理时，允许继承环境代理，兼容既有部署
        args["trust_env"] = True
    return args


def _cerul_request(
    method: str,
    path: str,
    *,
    api_key: str,
    payload: dict | None = None,
) -> tuple[int, dict, str]:
    url = f"{_get_base_url()}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(**_build_client_args()) as client:
        resp = client.request(method, url, headers=headers, json=payload)
    request_id = resp.headers.get("x-request-id", "")
    data: dict = {}
    try:
        if resp.content:
            maybe = resp.json()
            if isinstance(maybe, dict):
                data = maybe
    except ValueError:
        data = {}
    return resp.status_code, data, request_id


def _extract_error(data: dict, fallback: str) -> tuple[str, str]:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message") or fallback)
            code = str(err.get("subcode") or err.get("code") or "")
            return msg, code
    return fallback, ""


def cerul_search(args: dict) -> ToolResult:
    """语义检索视频，返回时间戳证据片段。"""
    query = str(args.get("query", "")).strip()
    if not query:
        return ToolResult.invalid_param("query 不能为空")

    api_key = _get_api_key()
    if not api_key:
        return ToolResult.invalid_param(
            "未配置 CERUL_API_KEY，无法调用 Cerul 搜索。",
            retry_hint="在运行环境设置 CERUL_API_KEY=cerul_xxx 后重试。",
        )

    try:
        max_results = int(args.get("max_results", 5))
    except (TypeError, ValueError):
        return ToolResult.invalid_param("max_results 必须是整数")
    max_results = max(1, min(max_results, 10))

    ranking_mode = str(args.get("ranking_mode", "embedding")).strip().lower() or "embedding"
    if ranking_mode not in _RANKING_MODES:
        return ToolResult.invalid_param("ranking_mode 只能是 embedding 或 rerank")

    include_answer = _to_bool(args.get("include_answer", False))

    payload: dict = {
        "query": query,
        "max_results": max_results,
        "ranking_mode": ranking_mode,
        "include_answer": include_answer,
    }
    filters = {}
    for key in ("speaker", "published_after", "source"):
        value = str(args.get(key, "")).strip()
        if value:
            filters[key] = value
    if filters:
        payload["filters"] = filters

    try:
        status, data, request_id = _cerul_request(
            "POST", "/v1/search", api_key=api_key, payload=payload
        )
    except httpx.TimeoutException:
        return ToolResult.api_error("Cerul 请求超时，请稍后重试。")
    except httpx.HTTPError as e:
        logger.warning("cerul_search http error: %s", e)
        return ToolResult.api_error(f"Cerul 请求失败: {e}")

    if status != 200:
        msg, err_code = _extract_error(data, f"Cerul search 失败（HTTP {status}）")
        req_text = f" request_id={request_id}" if request_id else ""
        return ToolResult.api_error(f"{msg}{req_text} [{err_code or status}]")

    results = data.get("results")
    if not isinstance(results, list):
        return ToolResult.api_error("Cerul 返回格式异常：缺少 results 字段")

    if not results:
        return ToolResult.success("Cerul 没有找到匹配的视频片段。")

    urls = [str(r.get("url", "")).strip() for r in results if str(r.get("url", "")).strip()]
    if urls:
        register_urls(urls)

    lines = []
    for idx, item in enumerate(results[:max_results], 1):
        title = str(item.get("title", "(无标题)")).strip()
        url = str(item.get("url", "")).strip()
        speaker = str(item.get("speaker", "") or "unknown")
        source = str(item.get("source", "") or "unknown")
        score = item.get("score")
        rerank = item.get("rerank_score")
        snippet = str(item.get("snippet", "")).strip().replace("\n", " ")
        if len(snippet) > 260:
            snippet = snippet[:260] + "..."

        score_text = ""
        if isinstance(score, (int, float)):
            score_text = f" score={float(score):.2f}"
        if isinstance(rerank, (int, float)):
            score_text += f" rerank={float(rerank):.2f}"

        lines.append(
            f"[结果 {idx}] {title}\n"
            f"speaker={speaker} source={source}{score_text} "
            f"time={_format_range(item.get('timestamp_start'), item.get('timestamp_end'))}\n"
            f"snippet: {snippet or '(无摘要)'}\n"
            f"link: {url or '(无链接)'}"
        )

    header = (
        f"Cerul 搜索完成：{len(lines)} 条结果，"
        f"credits_used={data.get('credits_used', '?')} "
        f"credits_remaining={data.get('credits_remaining', '?')}"
    )
    if request_id:
        header += f" request_id={request_id}"

    answer = data.get("answer")
    answer_block = ""
    if include_answer and isinstance(answer, str) and answer.strip():
        answer_block = f"\n\n[AI 汇总]\n{answer.strip()}"

    return ToolResult.success(header + "\n\n" + "\n\n".join(lines) + answer_block)


def cerul_usage(args: dict) -> ToolResult:
    """查询 Cerul 用量/额度。"""
    api_key = _get_api_key()
    if not api_key:
        return ToolResult.invalid_param(
            "未配置 CERUL_API_KEY，无法查询 Cerul 用量。",
            retry_hint="在运行环境设置 CERUL_API_KEY=cerul_xxx 后重试。",
        )

    try:
        status, data, request_id = _cerul_request("GET", "/v1/usage", api_key=api_key)
    except httpx.TimeoutException:
        return ToolResult.api_error("Cerul usage 请求超时，请稍后重试。")
    except httpx.HTTPError as e:
        logger.warning("cerul_usage http error: %s", e)
        return ToolResult.api_error(f"Cerul usage 请求失败: {e}")

    if status != 200:
        msg, err_code = _extract_error(data, f"Cerul usage 失败（HTTP {status}）")
        req_text = f" request_id={request_id}" if request_id else ""
        return ToolResult.api_error(f"{msg}{req_text} [{err_code or status}]")

    lines = [
        f"tier={data.get('tier', 'unknown')}",
        f"credits_used={data.get('credits_used', '?')}",
        f"credits_remaining={data.get('credits_remaining', '?')}",
        f"daily_free_remaining={data.get('daily_free_remaining', '?')}/{data.get('daily_free_limit', '?')}",
        f"rate_limit_per_sec={data.get('rate_limit_per_sec', '?')}",
    ]
    if request_id:
        lines.append(f"request_id={request_id}")

    return ToolResult.success("Cerul 用量信息\n" + "\n".join(lines))


TOOL_DEFINITIONS = [
    {
        "name": "cerul_search",
        "description": (
            "在视频语料中做语义检索，返回带时间戳的证据片段和直达链接。"
            "适用于“某人在哪段访谈说过什么”“找原话出处/时间点”等问题。"
            "该工具基于 Cerul API，每次调用会消耗额度。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索问题或语义描述（建议英文关键词更稳定）。",
                },
                "max_results": {
                    "type": "integer",
                    "description": "返回结果数，1-10，默认 5。",
                    "default": 5,
                },
                "ranking_mode": {
                    "type": "string",
                    "description": "排序模式：embedding(快) 或 rerank(更准)。默认 embedding。",
                    "enum": ["embedding", "rerank"],
                    "default": "embedding",
                },
                "include_answer": {
                    "type": "boolean",
                    "description": "是否让 Cerul 额外生成总结（更慢且更耗额度）。",
                    "default": False,
                },
                "speaker": {
                    "type": "string",
                    "description": "可选：按 speaker/channel 过滤。",
                },
                "published_after": {
                    "type": "string",
                    "description": "可选：发布日期下限，格式 YYYY-MM-DD。",
                },
                "source": {
                    "type": "string",
                    "description": "可选：来源过滤，如 youtube。",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "cerul_usage",
        "description": "查询 Cerul 账户额度与速率限制信息。",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


TOOL_MAP = {
    "cerul_search": cerul_search,
    "cerul_usage": cerul_usage,
}

