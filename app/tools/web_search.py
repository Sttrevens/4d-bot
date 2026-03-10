"""网络搜索工具

通过 sandbox_caps.web_search 能力原语实现。
沙箱代码也可以直接 import sandbox_caps 使用同样的搜索能力。
"""

import re

from app.tools.tool_result import ToolResult
from app.tools.sandbox_caps import web_search as _do_search
from app.tools.source_registry import register_urls

# ── 第一层：已知平台 → 精确搜索 URL ──
# 匹配到具体平台时直接给出现成的站内搜索链接
_PLATFORM_SEARCH_URLS: dict[str, tuple[str, str]] = {
    "小红书": ("https://www.xiaohongshu.com/search_result?keyword={q}", "小红书"),
    "红书": ("https://www.xiaohongshu.com/search_result?keyword={q}", "小红书"),
    "抖音": ("https://www.douyin.com/search/{q}", "抖音"),
    "b站": ("https://search.bilibili.com/all?keyword={q}", "B站"),
    "bilibili": ("https://search.bilibili.com/all?keyword={q}", "B站"),
    "哔哩哔哩": ("https://search.bilibili.com/all?keyword={q}", "B站"),
    "微博": ("https://s.weibo.com/weibo?q={q}", "微博"),
    "weibo": ("https://s.weibo.com/weibo?q={q}", "微博"),
    "知乎": ("https://www.zhihu.com/search?type=content&q={q}", "知乎"),
    "zhihu": ("https://www.zhihu.com/search?type=content&q={q}", "知乎"),
    "快手": ("https://www.kuaishou.com/search/video?searchKey={q}", "快手"),
    "tiktok": ("https://www.tiktok.com/search?q={q}", "TikTok"),
    "twitter": ("https://x.com/search?q={q}", "X/Twitter"),
    "youtube": ("https://www.youtube.com/results?search_query={q}", "YouTube"),
    "linkedin": ("https://www.linkedin.com/search/results/all/?keywords={q}", "LinkedIn"),
    "ins": ("https://www.instagram.com/explore/tags/{q}/", "Instagram"),
    "instagram": ("https://www.instagram.com/explore/tags/{q}/", "Instagram"),
    "threads": ("https://www.threads.net/search?q={q}", "Threads"),
    "豆瓣": ("https://www.douban.com/search?q={q}", "豆瓣"),
    "闲鱼": ("https://www.goofish.com/search?q={q}", "闲鱼"),
}

_PLATFORM_RE = re.compile(
    "|".join(re.escape(k) for k in _PLATFORM_SEARCH_URLS),
    re.IGNORECASE,
)

# ── 第二层：社媒意图关键词 ──
# 没有匹配到具体平台，但 query 表达了社媒调研意图时，泛化提醒
_SOCIAL_INTENT_KEYWORDS = re.compile(
    r"博主|KOL|kol|达人|网红|UP主|up主|粉丝画像|互动率|"
    r"种草|测评|带货|直播|账号分析|"
    r"社媒|社交媒体|social.?media|influencer",
    re.IGNORECASE,
)


def _detect_platform_hint(query: str) -> str:
    """检测 query 中的社媒平台或社媒意图，生成 browser_open 提示。"""
    q_lower = query.lower()

    # 第一层：精确匹配已知平台 → 给出具体 URL
    matches = _PLATFORM_RE.findall(q_lower)
    if matches:
        seen: set[str] = set()
        hints = []
        for m in matches:
            url_tpl, name = _PLATFORM_SEARCH_URLS[m.lower()]
            if name in seen:
                continue
            seen.add(name)
            search_term = _PLATFORM_RE.sub("", query).strip() or query
            url = url_tpl.replace("{q}", search_term)
            hints.append(f"  - {name}: browser_open(\"{url}\")")

        if hints:
            return (
                "\n\n⚠️ 你搜索的内容涉及社媒平台，但 DuckDuckGo 无法索引站内帖子/博主主页。"
                "上面的结果大多是第三方文章，不是平台一手数据。\n"
                "你必须用 browser_open 直接去平台站内搜索获取真实数据：\n"
                + "\n".join(hints)
                + "\n不要跳过这一步，否则报告只有第三方转述，没有一手信息。"
            )

    # 第二层：没有匹配到具体平台，但有社媒意图关键词 → 泛化提醒
    if _SOCIAL_INTENT_KEYWORDS.search(query):
        return (
            "\n\n💡 你的搜索涉及社媒内容（博主/KOL/达人等），DuckDuckGo 搜到的主要是第三方文章。"
            "如果需要平台一手数据（真实账号/粉丝数/互动量），"
            "请用 browser_open 直接访问相关社媒平台的搜索页获取。"
        )

    return ""


def web_search(args: dict) -> ToolResult:
    """搜索互联网并返回摘要结果。"""
    query = args.get("query", "").strip()
    if not query:
        return ToolResult.error("请提供搜索关键词")

    max_results = args.get("max_results", 5)

    result = _do_search(query, max_results)
    if isinstance(result, str):
        return ToolResult.error(result)

    if not result:
        return ToolResult.success("没有找到相关搜索结果。")

    # 注册真实 URL 到来源注册表（供 export_file 验证）
    register_urls([r.href for r in result])

    lines = []
    for i, r in enumerate(result, 1):
        lines.append(
            f"[来源 {i}] {r.title}\n{r.body}\n链接: {r.href}"
        )

    footer = (
        "\n\n---\n"
        "以上是全部搜索结果。报告中引用数据时只能使用上面的 [来源 N] 链接，"
        "禁止自行编造任何 URL。"
    )

    # 检测社媒平台/意图 → 提示用 browser_open
    platform_hint = _detect_platform_hint(query)

    return ToolResult.success("\n\n".join(lines) + footer + platform_hint)


async def fetch_url(args: dict) -> ToolResult:
    """直接获取网页/文档内容（轻量级，不需要浏览器）。

    适合：文章、文档、API 响应、Google Docs/Sheets 等。
    不适合：需要 JS 交互的 SPA（用 browser_open 代替）。
    """
    import httpx

    url = args.get("url", "").strip()
    if not url:
        return ToolResult.invalid_param("URL 不能为空")
    if not url.startswith(("http://", "https://")):
        return ToolResult.invalid_param("URL 必须以 http:// 或 https:// 开头")

    # 已知 SPA 网站：fetch_url 无法获取内容，直接引导用 browser_open
    _SPA_DOMAINS = ("luma.com", "lu.ma", "splashthat.com", "splash.events")
    try:
        from urllib.parse import urlparse
        _host = urlparse(url).hostname or ""
        for _spa in _SPA_DOMAINS:
            if _host == _spa or _host.endswith("." + _spa):
                return ToolResult.error(
                    f"⚠️ {_spa} 是 JavaScript 单页应用（SPA），fetch_url 无法获取动态内容。\n"
                    f"请改用 browser_open 打开此 URL：browser_open({{\"url\": \"{url}\"}})\n"
                    "browser_open 会执行 JavaScript 并渲染完整页面。"
                )
    except Exception:
        pass

    offset = int(args.get("offset") or 0)
    query = (args.get("query") or "").strip()

    # Google Docs/Sheets 快速路径
    try:
        from app.tools.browser_ops import _try_google_doc_export
        google_result = await _try_google_doc_export(url, offset=offset, query=query)
        if google_result is not None:
            return google_result
    except Exception:
        pass

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Bot/1.0)"},
            trust_env=False,
        ) as client:
            resp = await client.get(url)

        if resp.status_code != 200:
            return ToolResult.error(f"HTTP {resp.status_code}: 无法获取 {url}")

        content_type = resp.headers.get("content-type", "")
        content = resp.text

        # HTML → 提取正文
        if "html" in content_type:
            try:
                # 用正则简单提取：去掉 script/style 标签，保留文本
                import re as _re
                text = content
                text = _re.sub(r"<script[^>]*>.*?</script>", "", text, flags=_re.DOTALL)
                text = _re.sub(r"<style[^>]*>.*?</style>", "", text, flags=_re.DOTALL)
                text = _re.sub(r"<[^>]+>", "\n", text)
                text = _re.sub(r"\n{3,}", "\n\n", text).strip()

                # SPA 检测：HTML 很大但 strip tags 后极少文本 → React/Vue SPA 空壳
                raw_html_len = len(content)
                stripped_len = len(text)
                spa_hint = ""
                if raw_html_len > 2000 and stripped_len < 500:
                    spa_hint = (
                        "\n\n⚠️ 该网页是 JavaScript 单页应用（SPA），fetch_url 无法获取动态渲染的内容。"
                        "\n请改用 browser_open 打开此 URL，它会执行 JavaScript 并渲染完整页面内容。"
                        "\n用法: browser_open({\"url\": \"" + url + "\"})"
                    )

                # 截断（与 _MAX_TOOL_RESULT_LEN 对齐）
                max_len = 16000
                truncated = ""
                if len(text) > max_len:
                    truncated = f"\n\n(已截断，原文约 {len(text)} 字符)"
                    text = text[:max_len]
                content = text + truncated + spa_hint
            except Exception:
                content = content[:16000]

        elif len(content) > 16000:
            content = content[:16000] + "\n\n(已截断)"

        register_urls([url])
        return ToolResult.success(
            f"URL: {url}\n"
            f"Content-Type: {content_type}\n\n"
            f"── 内容 ──\n{content}"
        )

    except Exception as e:
        return ToolResult.error(f"获取 URL 失败: {e}")


TOOL_DEFINITIONS = [
    {
        "name": "web_search",
        "description": (
            "搜索互联网获取最新信息（DuckDuckGo 后端）。这是你的通用搜索工具，"
            "适用于所有平台和话题：YouTube、今日头条、TikTok、Twitter、知乎、豆瓣等任何网站都能搜。"
            "当用户要求调研某个话题时，应该主动多次调用 web_search 从不同角度搜索。"
            "搜索技巧：用短查询（3-5 个词），不要堆砌关键词；"
            "限定平台用「平台名+关键词」比 site: 更可靠；"
            "大问题拆成多次小搜索。"
            "重要：搜索结果中的数据才能引用，搜不到的数据不要编造。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，如 'Unity NetworkBehaviour OnEnable timing'",
                },
                "max_results": {
                    "type": "integer",
                    "description": "返回结果数量，默认5",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_url",
        "description": (
            "直接获取网页/在线文档的内容（轻量级，不需要浏览器）。"
            "支持 Google Docs/Sheets/Slides（自动导出为文本/CSV）、"
            "普通网页（HTML → 纯文本提取）、API 响应等。"
            "当用户发送了 URL 链接并且你只需要读取内容时，优先使用此工具而不是 browser_open。"
            "注意：如果网页需要 JavaScript 交互（如 SPA 应用、需要登录的页面），请用 browser_open。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要获取的 URL 地址",
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        "从第 N 个字符开始读取（分页）。"
                        "当返回结果提示【内容已截断】时，用 offset 读取后续部分。"
                        "例如上次返回了前 15000 字符，设 offset=15000 读后面的内容。"
                    ),
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Google Sheets 专用：SQL-like 查询语句，在服务端过滤行再返回。"
                        "对于大型表格（超过 3 万字符），强烈建议用 query 而不是逐页翻阅。"
                        "示例：\"select * where B contains 'March 12'\"、"
                        "\"select A,B,C where D > 100\"、\"select * where A is not null limit 50\"。"
                        "列用 A/B/C... 表示（第一列=A）。语法参考 Google Visualization Query Language。"
                    ),
                },
            },
            "required": ["url"],
        },
    },
]

TOOL_MAP = {
    "web_search": web_search,
    "fetch_url": fetch_url,
}
