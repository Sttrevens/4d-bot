"""社媒数据搜索工具（Social Media Search）

专门针对小红书、抖音等社媒平台的数据检索工具。
对比 web_search 的优势：
- 针对每个平台构造优化的搜索查询
- 并行搜索多个平台
- 结构化输出（平台、类型、指标提取）
- 可选对接第三方数据 API（TikHub / 新榜等）

架构：
- 默认后端：web_search（DuckDuckGo）+ 平台定向查询
- TikHub 后端：精确数据（粉丝数/互动量/笔记详情），$0.001/次
- 新榜后端：预留（需企业认证）
- 未来扩展：住宅代理 + Playwright 直采

TikHub API:
- 中国直连: https://api.tikhub.dev
- 国际: https://api.tikhub.io
- 认证: Bearer token
- 文档: https://docs.tikhub.io
"""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.tools.tool_result import ToolResult
from app.tools.sandbox_caps import web_search as _do_search
from app.tools.source_registry import register_urls

logger = logging.getLogger(__name__)

# TikHub API base URL（中国直连，不需要代理）
_TIKHUB_BASE_URL = os.getenv("TIKHUB_BASE_URL", "https://api.tikhub.dev")

# ── 平台搜索策略 ──
# 每个平台定义多个搜索查询模板，提高召回率

_PLATFORM_QUERIES = {
    "xiaohongshu": {
        "name": "小红书",
        "kol_queries": [
            "{keyword} 小红书博主 粉丝",
            "小红书 {keyword} KOL 达人 推荐",
            "site:xiaohongshu.com {keyword}",
        ],
        "content_queries": [
            "小红书 {keyword} 笔记 热门",
            "{keyword} 小红书 种草 测评",
        ],
        "brand_queries": [
            "小红书 {keyword} 品牌 投放 案例",
            "{keyword} 小红书营销 合作",
        ],
    },
    "douyin": {
        "name": "抖音",
        "kol_queries": [
            "{keyword} 抖音博主 粉丝数",
            "抖音 {keyword} 达人 排行",
            "site:douyin.com {keyword}",
        ],
        "content_queries": [
            "抖音 {keyword} 视频 热门",
            "{keyword} 抖音 带货 直播",
        ],
        "brand_queries": [
            "抖音 {keyword} 品牌合作 投放",
            "{keyword} 抖音营销 案例",
        ],
    },
    "bilibili": {
        "name": "B站",
        "kol_queries": [
            "{keyword} B站UP主 粉丝",
            "bilibili {keyword} UP主 推荐",
        ],
        "content_queries": [
            "B站 {keyword} 视频 热门",
            "bilibili {keyword} 评测 分享",
        ],
        "brand_queries": [
            "B站 {keyword} 品牌合作 推广",
        ],
    },
    "weibo": {
        "name": "微博",
        "kol_queries": [
            "{keyword} 微博大V 粉丝",
            "微博 {keyword} 博主 推荐",
        ],
        "content_queries": [
            "微博 {keyword} 热搜 话题",
        ],
        "brand_queries": [
            "微博 {keyword} 品牌营销",
        ],
    },
    "kuaishou": {
        "name": "快手",
        "kol_queries": [
            "{keyword} 快手达人 粉丝",
            "快手 {keyword} 主播 推荐",
        ],
        "content_queries": [
            "快手 {keyword} 视频 热门",
        ],
        "brand_queries": [
            "快手 {keyword} 品牌合作",
        ],
    },
}

# 平台名别名映射
_PLATFORM_ALIASES = {
    "小红书": "xiaohongshu", "红书": "xiaohongshu", "xhs": "xiaohongshu",
    "抖音": "douyin", "dy": "douyin", "tiktok": "douyin",
    "b站": "bilibili", "哔哩哔哩": "bilibili",
    "微博": "weibo",
    "快手": "kuaishou",
}

# 搜索类型映射
_SEARCH_TYPES = {
    "kol": "kol_queries",
    "博主": "kol_queries",
    "达人": "kol_queries",
    "content": "content_queries",
    "内容": "content_queries",
    "笔记": "content_queries",
    "brand": "brand_queries",
    "品牌": "brand_queries",
    "投放": "brand_queries",
}

_executor = ThreadPoolExecutor(max_workers=4)


def _resolve_platforms(platform_input: str) -> tuple[list[str], list[str]]:
    """解析平台参数，返回 (支持的平台 key 列表, 不支持的平台名列表)。"""
    if not platform_input or platform_input in ("all", "全部"):
        return ["xiaohongshu", "douyin"], []

    result = []
    unsupported = []
    for part in re.split(r"[,，+\s]+", platform_input.lower().strip()):
        if not part:
            continue
        if part in _PLATFORM_QUERIES:
            result.append(part)
        elif part in _PLATFORM_ALIASES:
            result.append(_PLATFORM_ALIASES[part])
        else:
            unsupported.append(part)
    return result or ["xiaohongshu", "douyin"], unsupported


def _resolve_search_type(type_input: str) -> str:
    """解析搜索类型参数。"""
    if not type_input:
        return "kol_queries"
    lower = type_input.lower().strip()
    return _SEARCH_TYPES.get(lower, "kol_queries")


def _search_platform(
    platform_key: str,
    keyword: str,
    search_type: str,
    max_results: int,
) -> dict:
    """搜索单个平台，返回结构化结果。"""
    config = _PLATFORM_QUERIES.get(platform_key)
    if not config:
        return {"platform": platform_key, "results": [], "error": f"未知平台: {platform_key}"}

    queries = config.get(search_type, config.get("kol_queries", []))
    all_results = []
    seen_urls = set()

    for query_tpl in queries[:2]:  # 每个平台最多用 2 个查询模板
        query = query_tpl.format(keyword=keyword)
        try:
            results = _do_search(query, max_results)
            if isinstance(results, str):
                logger.debug("search failed for '%s': %s", query, results)
                continue
            for r in results:
                if r.href not in seen_urls:
                    seen_urls.add(r.href)
                    all_results.append({
                        "title": r.title,
                        "body": r.body,
                        "href": r.href,
                        "query": query,
                    })
        except Exception as e:
            logger.debug("search error for '%s': %s", query, e)

    return {
        "platform": config["name"],
        "platform_key": platform_key,
        "results": all_results[:max_results],
    }


def _try_api_search(
    platform_key: str,
    keyword: str,
    search_type: str,
    max_results: int,
    api_provider: str,
    api_key: str,
    api_secret: str,
) -> dict | None:
    """尝试使用第三方 API 搜索。返回 None 表示无可用 API / 不支持该平台。"""
    if not api_provider or not api_key:
        return None

    if api_provider == "tikhub":
        return _search_tikhub(platform_key, keyword, search_type, max_results, api_key)
    if api_provider == "newrank":
        return _search_newrank(platform_key, keyword, search_type, max_results, api_key, api_secret)

    logger.info("unknown social media API provider: %s", api_provider)
    return None


# ═══════════════════════════════════════════════════════
#  TikHub API 后端
#  文档: https://docs.tikhub.io
#  中国直连: https://api.tikhub.dev ($0.001/次)
# ═══════════════════════════════════════════════════════

def _tikhub_get(endpoint: str, params: dict, api_key: str) -> dict | None:
    """TikHub API GET 请求封装。"""
    import httpx

    url = f"{_TIKHUB_BASE_URL}{endpoint}"
    try:
        with httpx.Client(timeout=20, trust_env=False) as client:
            resp = client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 200:
                logger.warning("tikhub API error: endpoint=%s code=%s msg=%s",
                               endpoint, data.get("code"), data.get("message_zh", ""))
                return None
            return data.get("data")
    except Exception as e:
        logger.warning("tikhub API call failed: endpoint=%s error=%s", endpoint, e)
        return None


def _search_tikhub(
    platform_key: str,
    keyword: str,
    search_type: str,
    max_results: int,
    api_key: str,
) -> dict | None:
    """TikHub 统一搜索入口。根据平台和搜索类型分发到具体端点。"""
    if platform_key == "douyin":
        if "kol" in search_type:
            return _tikhub_douyin_user_search(keyword, max_results, api_key)
        return _tikhub_douyin_video_search(keyword, max_results, api_key)
    elif platform_key == "xiaohongshu":
        if "kol" in search_type:
            return _tikhub_xhs_user_search(keyword, max_results, api_key)
        return _tikhub_xhs_note_search(keyword, max_results, api_key)
    else:
        # TikHub 也支持 B站/快手/微博，但端点不同，暂不实现
        logger.debug("tikhub: platform %s not yet implemented, falling back", platform_key)
        return None


def _tikhub_douyin_user_search(keyword: str, max_results: int, api_key: str) -> dict | None:
    """抖音用户搜索 — /api/v1/douyin/web/fetch_user_search_result_v2"""
    data = _tikhub_get(
        "/api/v1/douyin/web/fetch_user_search_result_v2",
        {"keyword": keyword, "cursor": 0},
        api_key,
    )
    if not data:
        return None

    # 解析用户列表
    user_list = data.get("user_list") or data.get("data", [])
    if not user_list and isinstance(data, list):
        user_list = data

    results = []
    for item in user_list[:max_results]:
        user_info = item.get("user_info", item)
        nickname = user_info.get("nickname", "")
        uid = user_info.get("uid", user_info.get("short_id", ""))
        signature = user_info.get("signature", "")
        follower_count = user_info.get("follower_count", 0)
        total_favorited = user_info.get("total_favorited", 0)
        sec_uid = user_info.get("sec_uid", "")

        profile_url = f"https://www.douyin.com/user/{sec_uid}" if sec_uid else ""

        results.append({
            "title": nickname,
            "body": signature[:200] if signature else f"抖音号: {uid}",
            "href": profile_url,
            "metrics": {
                "followers": _format_number(follower_count),
                "likes": _format_number(total_favorited),
            },
        })

    return {
        "platform": "抖音",
        "platform_key": "douyin",
        "results": results,
        "source": "tikhub_api",
    }


def _tikhub_douyin_video_search(keyword: str, max_results: int, api_key: str) -> dict | None:
    """抖音视频/内容搜索 — /api/v1/douyin/web/fetch_video_search_result"""
    data = _tikhub_get(
        "/api/v1/douyin/web/fetch_video_search_result",
        {"keyword": keyword, "cursor": 0},
        api_key,
    )
    if not data:
        return None

    video_list = data.get("data") or data.get("video_list", [])
    if not video_list and isinstance(data, list):
        video_list = data

    results = []
    for item in video_list[:max_results]:
        aweme = item.get("aweme_info", item)
        desc = aweme.get("desc", "")
        author = aweme.get("author", {})
        nickname = author.get("nickname", "")
        stats = aweme.get("statistics", {})
        aweme_id = aweme.get("aweme_id", "")

        video_url = f"https://www.douyin.com/video/{aweme_id}" if aweme_id else ""

        results.append({
            "title": f"{nickname}: {desc[:60]}" if nickname else desc[:80],
            "body": desc[:200],
            "href": video_url,
            "metrics": {
                "likes": _format_number(stats.get("digg_count", 0)),
                "comments": _format_number(stats.get("comment_count", 0)),
                "shares": _format_number(stats.get("share_count", 0)),
            },
        })

    return {
        "platform": "抖音",
        "platform_key": "douyin",
        "results": results,
        "source": "tikhub_api",
    }


def _tikhub_xhs_note_search(keyword: str, max_results: int, api_key: str) -> dict | None:
    """小红书笔记搜索 — /api/v1/xiaohongshu/web/search_notes"""
    data = _tikhub_get(
        "/api/v1/xiaohongshu/web/search_notes",
        {"keyword": keyword, "page": 1, "sort": "general"},
        api_key,
    )
    if not data:
        return None

    # 小红书搜索结果可能在 data.items 或 data 本身
    items = data.get("items") or data.get("notes", [])
    if not items and isinstance(data, list):
        items = data

    results = []
    for item in items[:max_results]:
        note_card = item.get("note_card", item)
        title = note_card.get("display_title", note_card.get("title", ""))
        desc = note_card.get("desc", "")
        user = note_card.get("user", {})
        nickname = user.get("nickname", user.get("nick_name", ""))
        note_id = item.get("id", note_card.get("note_id", ""))
        interact_info = note_card.get("interact_info", {})
        liked_count = interact_info.get("liked_count", "")

        note_url = f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else ""

        entry_title = f"{nickname}: {title}" if nickname and title else title or nickname
        results.append({
            "title": entry_title,
            "body": desc[:200] if desc else title,
            "href": note_url,
            "metrics": {
                "likes": str(liked_count) if liked_count else "",
            },
        })

    return {
        "platform": "小红书",
        "platform_key": "xiaohongshu",
        "results": results,
        "source": "tikhub_api",
    }


def _tikhub_xhs_user_search(keyword: str, max_results: int, api_key: str) -> dict | None:
    """小红书用户搜索 — /api/v1/xiaohongshu/web_v2/search_users

    尝试 web_v2 端点；失败则回退到 note_search 从笔记中提取作者。
    """
    # 尝试用户搜索端点
    data = _tikhub_get(
        "/api/v1/xiaohongshu/web_v2/search_users",
        {"keyword": keyword, "page": 1},
        api_key,
    )
    if data:
        users = data.get("users") or data.get("items", [])
        if not users and isinstance(data, list):
            users = data

        if users:
            results = []
            for item in users[:max_results]:
                user_info = item.get("user_info", item)
                nickname = user_info.get("nickname", user_info.get("nick_name", ""))
                user_id = user_info.get("user_id", user_info.get("id", ""))
                desc = user_info.get("desc", user_info.get("signature", ""))
                fans = user_info.get("fans", user_info.get("fansCount", ""))

                profile_url = f"https://www.xiaohongshu.com/user/profile/{user_id}" if user_id else ""

                results.append({
                    "title": nickname,
                    "body": desc[:200] if desc else "",
                    "href": profile_url,
                    "metrics": {
                        "followers": str(fans) if fans else "",
                    },
                })

            return {
                "platform": "小红书",
                "platform_key": "xiaohongshu",
                "results": results,
                "source": "tikhub_api",
            }

    # 回退：通过笔记搜索提取作者信息
    logger.debug("tikhub: xhs user search fallback to note search")
    note_result = _tikhub_xhs_note_search(keyword, max_results * 2, api_key)
    if not note_result or not note_result.get("results"):
        return None

    # 从笔记结果中去重提取作者
    seen_authors = set()
    user_results = []
    for note in note_result["results"]:
        author = note["title"].split(":")[0] if ":" in note["title"] else ""
        if author and author not in seen_authors:
            seen_authors.add(author)
            user_results.append({
                "title": author,
                "body": f"来自笔记: {note['title']}",
                "href": note.get("href", ""),
                "metrics": note.get("metrics", {}),
            })
        if len(user_results) >= max_results:
            break

    return {
        "platform": "小红书",
        "platform_key": "xiaohongshu",
        "results": user_results,
        "source": "tikhub_api (via notes)",
    }


def _format_number(n) -> str:
    """格式化数字：10000 → '1.0万'，100000000 → '1.0亿'"""
    if not n:
        return ""
    try:
        n = int(n)
    except (ValueError, TypeError):
        return str(n)
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


# ═══════════════════════════════════════════════════════
#  新榜 API 后端（预留，需企业认证）
# ═══════════════════════════════════════════════════════

def _search_newrank(
    platform_key: str,
    keyword: str,
    search_type: str,
    max_results: int,
    api_key: str,
    api_secret: str,
) -> dict | None:
    """新榜 API 搜索。

    新榜 Open API 文档：https://open.newrank.cn/
    需要企业认证 + API key 申请。
    """
    import httpx
    import hashlib
    import time

    nonce = "social_media_ops"
    timestamp = str(int(time.time()))
    sign_str = api_key + nonce + timestamp + api_secret
    sign = hashlib.md5(sign_str.encode()).hexdigest()

    platform_map = {
        "xiaohongshu": "xhs", "douyin": "douyin",
        "bilibili": "bilibili", "weibo": "weibo", "kuaishou": "kuaishou",
    }
    nr_platform = platform_map.get(platform_key)
    if not nr_platform:
        return None

    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            resp = client.post(
                "https://open.newrank.cn/gw/nr/open/api/social/search",
                json={
                    "platform": nr_platform,
                    "keyword": keyword,
                    "type": "kol" if "kol" in search_type else "content",
                    "page": 1, "size": max_results,
                },
                headers={"Content-Type": "application/json", "nonce": nonce, "xyz": sign},
                params={"api_key": api_key, "timestamp": timestamp},
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 200:
                logger.warning("newrank API error: %s", data.get("msg", ""))
                return None

            items = data.get("data", {}).get("list", [])
            results = []
            for item in items[:max_results]:
                results.append({
                    "title": item.get("name", item.get("title", "")),
                    "body": item.get("desc", item.get("content", "")),
                    "href": item.get("url", item.get("link", "")),
                    "metrics": {
                        "followers": _format_number(item.get("followers", item.get("fans", 0))),
                        "likes": _format_number(item.get("likes", 0)),
                        "engagement_rate": item.get("engagement_rate", ""),
                    },
                })
            config = _PLATFORM_QUERIES.get(platform_key, {})
            return {
                "platform": config.get("name", platform_key),
                "platform_key": platform_key,
                "results": results,
                "source": "newrank_api",
            }
    except Exception as e:
        logger.warning("newrank API call failed: %s", e)
        return None


def _try_playwright_xhs(keyword: str, search_type: str, max_results: int) -> dict | None:
    """尝试用 Playwright 浏览器直接搜索小红书（替代 TikHub API）。

    同步包装异步调用，在 ThreadPoolExecutor 中运行。
    返回 None 表示不可用 / 失败，自动回退 web_search。
    """
    try:
        from app.tools.xhs_ops import xhs_playwright_search
    except ImportError:
        return None

    import asyncio

    async def _do():
        return await xhs_playwright_search(keyword, search_type, max_results)

    try:
        # 尝试获取当前 event loop 并在其中运行
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 在已有 loop 中，用 asyncio.ensure_future + 等待
            import concurrent.futures
            future = concurrent.futures.Future()

            async def _wrapper():
                try:
                    result = await xhs_playwright_search(keyword, search_type, max_results)
                    future.set_result(result)
                except Exception as e:
                    future.set_exception(e)

            asyncio.ensure_future(_wrapper())
            return future.result(timeout=60)
        else:
            return loop.run_until_complete(_do())
    except RuntimeError:
        # 没有 event loop，创建新的
        return asyncio.run(_do())
    except Exception as e:
        logger.debug("playwright xhs search failed, falling back: %s", e)
        return None


def _format_results(platform_results: list[dict], keyword: str) -> str:
    """将多平台搜索结果格式化为文本。"""
    if not platform_results:
        return f"没有找到关于「{keyword}」的社媒数据。"

    sections = []
    all_urls = []

    for pr in platform_results:
        platform_name = pr.get("platform", "未知")
        results = pr.get("results", [])
        source = pr.get("source", "web_search")

        if not results:
            sections.append(f"== {platform_name} ==\n（未找到相关结果）")
            continue

        lines = [f"== {platform_name} =="]
        if source != "web_search":
            lines[0] += f"（数据来源: {source}）"

        for i, r in enumerate(results, 1):
            title = r.get("title", "无标题")
            body = r.get("body", "")
            href = r.get("href", "")

            entry = f"[{i}] {title}"
            if body:
                # 截断过长的摘要
                body_clean = body[:200] + "..." if len(body) > 200 else body
                entry += f"\n    {body_clean}"
            if href:
                entry += f"\n    链接: {href}"
                all_urls.append(href)

            # 如果有结构化指标（来自 API）
            metrics = r.get("metrics")
            if metrics:
                parts = []
                if metrics.get("followers"):
                    parts.append(f"粉丝: {metrics['followers']}")
                if metrics.get("likes"):
                    parts.append(f"赞: {metrics['likes']}")
                if metrics.get("engagement_rate"):
                    parts.append(f"互动率: {metrics['engagement_rate']}")
                if parts:
                    entry += f"\n    指标: {' | '.join(parts)}"

            lines.append(entry)

        sections.append("\n".join(lines))

    # 注册 URL 到来源注册表
    if all_urls:
        register_urls(all_urls)

    # 判断数据来源
    has_api_data = any(pr.get("source", "").endswith("_api") for pr in platform_results)

    output = "\n\n".join(sections)
    if has_api_data:
        output += (
            "\n\n---\n"
            "以上数据来自 API 实时查询，粉丝数/互动量为平台真实数据。\n"
            "报告中引用的链接只能来自上面的搜索结果，禁止编造 URL。"
        )
    else:
        output += (
            "\n\n---\n"
            "以上数据来自网页搜索（非 API 直连），粉丝数/互动量仅供参考。\n"
            "如需精确数据，可用 browser_open 直接访问博主主页。\n"
            "报告中引用的链接只能来自上面的搜索结果，禁止编造 URL。"
        )

    return output


def search_social_media(args: dict) -> ToolResult:
    """多平台社媒搜索：并行搜索多个社媒平台，返回结构化结果。"""
    keyword = args.get("keyword", "").strip()
    if not keyword:
        return ToolResult.invalid_param("keyword 不能为空")

    platform_input = args.get("platform", "all")
    search_type_input = args.get("search_type", "kol")
    max_results = min(args.get("max_results", 5), 10)

    platforms, unsupported = _resolve_platforms(platform_input)
    search_type = _resolve_search_type(search_type_input)

    # 如果所有请求的平台都不支持，提示用 web_search
    if not platforms and unsupported:
        return ToolResult.success(
            f"本工具不支持以下平台：{'、'.join(unsupported)}。\n"
            f"支持的平台：小红书、抖音、B站、微博、快手。\n"
            f"请改用 web_search 搜索这些平台的信息，例如：web_search(\"{unsupported[0]} {keyword}\")"
        )

    # 检查是否有第三方 API 配置
    api_provider = ""
    api_key = ""
    api_secret = ""
    try:
        from app.tenant.context import get_current_tenant
        tenant = get_current_tenant()
        api_provider = getattr(tenant, "social_media_api_provider", "")
        api_key = getattr(tenant, "social_media_api_key", "")
        api_secret = getattr(tenant, "social_media_api_secret", "")
    except Exception:
        pass

    # 并行搜索所有平台
    platform_results = []
    futures = {}

    for pf in platforms:
        # 优先尝试 API
        if api_provider and api_key:
            api_result = _try_api_search(
                pf, keyword, search_type, max_results,
                api_provider, api_key, api_secret,
            )
            if api_result:
                platform_results.append(api_result)
                continue

        # 小红书：优先尝试 Playwright 浏览器直采
        if pf == "xiaohongshu":
            pw_result = _try_playwright_xhs(keyword, search_type, max_results)
            if pw_result:
                platform_results.append(pw_result)
                continue

        # 回退到 web_search
        future = _executor.submit(
            _search_platform, pf, keyword, search_type, max_results,
        )
        futures[future] = pf

    # 收集并行搜索结果
    for future in as_completed(futures, timeout=30):
        try:
            result = future.result()
            platform_results.append(result)
        except Exception as e:
            pf = futures[future]
            logger.warning("social media search failed for %s: %s", pf, e)

    output = _format_results(platform_results, keyword)

    # 提示不支持的平台应该用 web_search
    if unsupported:
        output += (
            f"\n\n⚠️ 以下平台本工具不支持：{'、'.join(unsupported)}。"
            f"请用 web_search 搜索这些平台，例如：web_search(\"{unsupported[0]} {keyword}\")"
        )

    return ToolResult.success(output)


def get_platform_search_url(args: dict) -> ToolResult:
    """获取社媒平台的站内搜索 URL，配合 browser_open 使用。"""
    platform_input = args.get("platform", "").strip()
    keyword = args.get("keyword", "").strip()

    if not platform_input:
        return ToolResult.invalid_param("platform 不能为空")
    if not keyword:
        return ToolResult.invalid_param("keyword 不能为空")

    # 解析平台
    platform_key = _PLATFORM_ALIASES.get(platform_input.lower(), platform_input.lower())

    url_map = {
        "xiaohongshu": f"https://www.xiaohongshu.com/search_result?keyword={keyword}",
        "douyin": f"https://www.douyin.com/search/{keyword}",
        "bilibili": f"https://search.bilibili.com/all?keyword={keyword}",
        "weibo": f"https://s.weibo.com/weibo?q={keyword}",
        "kuaishou": f"https://www.kuaishou.com/search/video?searchKey={keyword}",
    }

    url = url_map.get(platform_key)
    if not url:
        supported = "、".join(
            f"{v['name']}({k})" for k, v in _PLATFORM_QUERIES.items()
        )
        return ToolResult.invalid_param(
            f"不支持的平台: {platform_input}。支持的平台: {supported}"
        )

    name = _PLATFORM_QUERIES[platform_key]["name"]
    return ToolResult.success(
        f"{name}站内搜索链接: {url}\n"
        f"用 browser_open(\"{url}\") 可以直接在{name}内搜索「{keyword}」"
    )


# ── 工具定义 ──

TOOL_DEFINITIONS = [
    {
        "name": "search_social_media",
        "description": (
            "搜索小红书、抖音、B站、微博、快手的博主/内容/品牌数据。"
            "仅支持以上 5 个平台！YouTube、今日头条、TikTok、Twitter 等其他平台请直接用 web_search。"
            "适用场景：找 KOL/博主、分析竞品社媒布局、调研内容趋势。"
            "底层通过网页搜索获取数据（第三方文章+公开信息）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词，如 '游戏' '美妆测评' '3C数码'",
                },
                "platform": {
                    "type": "string",
                    "description": (
                        "目标平台，仅支持：xiaohongshu/小红书, douyin/抖音, "
                        "bilibili/B站, weibo/微博, kuaishou/快手, all/全部。"
                        "多个平台用逗号分隔。默认 all（小红书+抖音）。"
                        "不支持的平台（YouTube/今日头条/TikTok等）请改用 web_search"
                    ),
                    "default": "all",
                },
                "search_type": {
                    "type": "string",
                    "description": (
                        "搜索类型：kol/博主（找达人）, content/内容（找热门帖子）, "
                        "brand/品牌（找投放案例）。默认 kol"
                    ),
                    "default": "kol",
                },
                "max_results": {
                    "type": "integer",
                    "description": "每个平台返回的最大结果数，默认5，最大10",
                    "default": 5,
                },
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "get_platform_search_url",
        "description": (
            "获取社媒平台的站内搜索 URL。"
            "配合 browser_open 使用，直接在平台内搜索获取一手数据。"
            "示例：get_platform_search_url(platform='小红书', keyword='游戏博主') "
            "→ 返回小红书搜索链接 → browser_open 打开"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "description": "平台名：小红书/抖音/B站/微博/快手",
                },
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词",
                },
            },
            "required": ["platform", "keyword"],
        },
    },
]

TOOL_MAP = {
    "search_social_media": search_social_media,
    "get_platform_search_url": get_platform_search_url,
}
