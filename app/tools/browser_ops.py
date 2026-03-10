"""浏览器自动化工具 —— 打通「交互墙」

让 bot 能通过 Playwright 控制浏览器，访问任意网页并执行操作。
用于没有 API 的平台（小红书、各种网站表单、后台管理系统等）。

工作流程（vision-language-action 循环）：
1. browser_open(url) → 打开页面 → 截图 → Gemini 分析 → 返回页面描述
2. LLM 根据描述决定下一步操作
3. browser_do(action, selector) → 执行操作 → 截图 → 分析 → 返回结果
4. 重复直到任务完成
5. browser_close() → 关闭会话

安全策略：
- 禁止访问内网 IP / file:// / localhost（防 SSRF）
- 每个租户最多 1 个浏览器会话
- 会话 5 分钟不活动自动关闭
- headless 模式，无 GUI 泄露
- Playwright 未安装时优雅降级，提供安装指引
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

# ── 安全：URL 过滤 ──

_BLOCKED_URL_PATTERNS = re.compile(
    r"^file://"
    r"|^https?://(localhost|127\.\d|10\.\d|172\.(1[6-9]|2\d|3[01])\."
    r"|192\.168\.|169\.254\.|0\.0\.0\.0|\[::1\])",
    re.IGNORECASE,
)

# ── 会话管理 ──

_SESSION_TIMEOUT = 300  # 5 分钟不活动自动关闭
_MAX_TEXT_LENGTH = 3000  # 提取文本最大长度
_SCREENSHOT_PROMPT = (
    "你是一个网页分析助手。请详细描述这个网页截图的内容和布局：\n"
    "1. 页面类型（登录页、首页、搜索结果、表单等）\n"
    "2. 主要可见内容（文字、图片、按钮）\n"
    "3. 可交互元素（输入框、按钮、链接、下拉菜单）及其位置\n"
    "4. 当前状态（是否已登录、是否有弹窗、是否加载完成）\n"
    "用中文回复，简洁准确。"
)


@dataclass
class _BrowserSession:
    """单个浏览器会话"""
    playwright: Any = None  # Playwright instance
    browser: Any = None     # Browser
    context: Any = None     # BrowserContext
    page: Any = None        # Page
    tenant_id: str = ""
    last_used: float = field(default_factory=time.time)


# 全局会话存储（tenant_id → session）
_sessions: dict[str, _BrowserSession] = {}


def _get_tenant_id() -> str:
    try:
        from app.tenant.context import get_current_tenant
        return get_current_tenant().tenant_id or ""
    except Exception:
        return ""


def _get_tenant_or_none():
    try:
        from app.tenant.context import get_current_tenant
        return get_current_tenant()
    except Exception:
        return None


# ── Google Docs/Sheets/Slides 直接导出（不需要浏览器）──

_GOOGLE_DOC_PATTERNS = {
    # Google Sheets: /spreadsheets/d/{id}/...  → export as CSV
    "sheets": re.compile(r"docs\.google\.com/spreadsheets/d/([^/]+)"),
    # Google Docs: /document/d/{id}/...  → export as text
    "docs": re.compile(r"docs\.google\.com/document/d/([^/]+)"),
    # Google Slides: /presentation/d/{id}/...  → export as text
    "slides": re.compile(r"docs\.google\.com/presentation/d/([^/]+)"),
}

_GOOGLE_EXPORT_FORMATS = {
    "sheets": ("csv", "text/csv"),
    "docs": ("txt", "text/plain"),
    "slides": ("txt", "text/plain"),
}


async def _try_google_doc_export(
    url: str, *, offset: int = 0, query: str = "",
) -> ToolResult | None:
    """尝试直接导出 Google 文档内容（无需浏览器，更快更可靠）。

    Google Docs/Sheets/Slides 支持通过 URL 参数直接导出为纯文本/CSV。
    对于公开或链接共享的文档，无需认证即可获取。
    offset: 从第 N 个字符开始返回（分页读取大文档）。
    query: Google Visualization Query（SQL-like），仅对 Sheets 生效。
           例如 "select * where B contains 'March 12'"

    返回 ToolResult（成功）或 None（不是 Google 文档 / 导出失败 → 回退到浏览器）。
    """
    import httpx

    doc_type = None
    doc_id = None
    for dtype, pattern in _GOOGLE_DOC_PATTERNS.items():
        m = pattern.search(url)
        if m:
            doc_type = dtype
            doc_id = m.group(1)
            break

    if not doc_type or not doc_id:
        return None

    fmt, content_type = _GOOGLE_EXPORT_FORMATS[doc_type]

    # 提取 gid 参数（Sheets 多工作表）
    gid = ""
    gid_match = re.search(r"[?&#]gid=(\d+)", url)
    if gid_match:
        gid = gid_match.group(1)

    # ── Google Sheets: 如果有 query，优先用 gviz/tq 服务端过滤 ──
    # gviz/tq 支持 SQL-like 查询，可以在服务端过滤行/列，返回结果远小于全量 CSV
    if doc_type == "sheets" and query:
        from urllib.parse import quote as _url_quote
        gviz_url = (
            f"https://docs.google.com/spreadsheets/d/{doc_id}"
            f"/gviz/tq?tqx=out:csv&tq={_url_quote(query)}"
        )
        if gid:
            gviz_url += f"&gid={gid}"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as client:
                gviz_resp = await client.get(gviz_url)
            if gviz_resp.status_code == 200 and len(gviz_resp.text) > 10:
                gviz_text = gviz_resp.text
                logger.info("google gviz/tq: query succeeded (%d chars): %s",
                           len(gviz_text), query[:80])
                try:
                    from app.tools.source_registry import register_urls
                    register_urls([url])
                except Exception:
                    pass
                # gviz 结果通常较小，不需要分页
                page_size = 15000
                truncated = ""
                if len(gviz_text) > page_size:
                    gviz_text = gviz_text[:page_size]
                    truncated = f"\n\n⚠️ 查询结果被截断（{page_size} 字符）。请缩小查询范围。"
                return ToolResult.success(
                    f"✅ Google 表格查询结果\n"
                    f"URL: {url}\n"
                    f"查询: {query}\n"
                    f"结果: {len(gviz_text)} 字符\n"
                    f"提示: 这是静态导出模式，没有活跃的浏览器会话。\n\n"
                    f"── 查询结果（CSV）──\n{gviz_text}{truncated}\n\n"
                    f"⛔ 上面是查询返回的真实数据。所有链接/日期/名称必须逐字复制，绝不能编造。"
                )
            else:
                logger.info("google gviz/tq: query failed (status=%d, len=%d), falling back to full export",
                           gviz_resp.status_code, len(gviz_resp.text) if gviz_resp.text else 0)
        except Exception as e:
            logger.info("google gviz/tq: query error: %s, falling back to full export", e)

    # 构造导出 URL
    if doc_type == "sheets":
        export_url = f"https://docs.google.com/spreadsheets/d/{doc_id}/export?format={fmt}"
        if gid:
            export_url += f"&gid={gid}"
    elif doc_type == "docs":
        export_url = f"https://docs.google.com/document/d/{doc_id}/export?format={fmt}"
    elif doc_type == "slides":
        export_url = f"https://docs.google.com/presentation/d/{doc_id}/export?format={fmt}"
    else:
        return None

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            resp = await client.get(export_url)

        if resp.status_code == 200:
            full_text = resp.text
            total_len = len(full_text)
            if not full_text or total_len < 10:
                logger.info("google export: empty content for %s, falling back to browser", doc_type)
                return None

            # 注册 URL 到来源注册表，防止后续工具调用触发 hallucination 警告
            try:
                from app.tools.source_registry import register_urls
                register_urls([url])
            except Exception:
                pass

            # 分页：offset > 0 时跳过前面的内容
            if offset > 0:
                if offset >= total_len:
                    return ToolResult.success(
                        f"已到达文档末尾（总共 {total_len} 字符，offset={offset}）。没有更多内容了。"
                    )
                full_text = full_text[offset:]

            # 截断过长内容（每页最多 15000 字符）
            page_size = 15000
            truncated = ""
            content = full_text
            if len(content) > page_size:
                content = content[:page_size]
                next_offset = offset + page_size
                # 大型 Sheets：提示用 query 参数过滤，而不是逐页翻
                query_hint = ""
                if doc_type == "sheets" and total_len > 30000:
                    query_hint = (
                        "\n\n💡 这个表格很大（{total}字符）。与其逐页翻阅，"
                        "强烈建议使用 query 参数直接过滤你需要的行，例如：\n"
                        '  fetch_url(url="...", query="select * where B contains \'March 12\'")\n'
                        "这会在服务端过滤，只返回匹配的行，数据完整且 URL 不会丢失。"
                    ).format(total=total_len)
                truncated = (
                    f"\n\n⚠️ 内容已截断（本页 {page_size} 字符，文档总共 {total_len} 字符）。"
                    f"\n还有 {total_len - next_offset} 字符未显示。"
                    f"\n要读取下一页，请调用 fetch_url 并设置 offset={next_offset}"
                    "\n⛔ 绝对不要猜测或编造你没看到的内容！只用你实际看到的数据。"
                    f"{query_hint}"
                )

            type_names = {"sheets": "Google 表格", "docs": "Google 文档", "slides": "Google 幻灯片"}
            offset_hint = f" (offset={offset})" if offset > 0 else ""
            logger.info("google export: successfully fetched %s (%d/%d chars%s)",
                       doc_type, len(content), total_len, offset_hint)
            return ToolResult.success(
                f"✅ {type_names[doc_type]}内容已获取{offset_hint}\n"
                f"URL: {url}\n"
                f"格式: {fmt.upper()} | 本页: {len(content)} 字符 | 总共: {total_len} 字符\n"
                f"提示: 这是静态导出模式，没有活跃的浏览器会话，无法执行 browser_do (scroll/click) 操作。\n\n"
                f"── 文档内容 ──\n{content}{truncated}"
            )

        if resp.status_code in (401, 403):
            logger.info("google export: %s needs auth (status=%d), falling back to browser",
                       doc_type, resp.status_code)
            return None  # 需要认证，回退到浏览器

        logger.info("google export: unexpected status %d for %s", resp.status_code, doc_type)
        return None

    except Exception as e:
        logger.info("google export: failed for %s: %s, falling back to browser", doc_type, e)
        return None


def _validate_url(url: str) -> str | None:
    """校验 URL 安全性，返回错误信息或 None"""
    if not url:
        return "URL 不能为空"
    if not url.startswith(("http://", "https://")):
        return "URL 必须以 http:// 或 https:// 开头"
    if _BLOCKED_URL_PATTERNS.match(url):
        return f"禁止访问内部网络地址: {url}"
    return None


async def _cleanup_session(tenant_id: str) -> None:
    """清理指定租户的浏览器会话"""
    session = _sessions.pop(tenant_id, None)
    if session is None:
        return
    try:
        if session.context:
            await session.context.close()
        if session.browser:
            await session.browser.close()
        if session.playwright:
            await session.playwright.stop()
    except Exception as e:
        logger.warning("browser_ops: cleanup error for %s: %s", tenant_id, e)


async def _cleanup_stale_sessions() -> None:
    """清理所有超时的会话"""
    now = time.time()
    stale = [
        tid for tid, s in _sessions.items()
        if now - s.last_used > _SESSION_TIMEOUT
    ]
    for tid in stale:
        logger.info("browser_ops: auto-closing stale session for %s", tid)
        await _cleanup_session(tid)


async def _get_or_create_session(tenant_id: str) -> _BrowserSession:
    """获取或创建浏览器会话"""
    # 清理超时会话
    await _cleanup_stale_sessions()

    # 复用现有会话
    if tenant_id in _sessions:
        session = _sessions[tenant_id]
        session.last_used = time.time()
        # 检查浏览器是否还活着
        try:
            if session.browser.is_connected():
                return session
        except Exception:
            pass
        # 连接断了，清理重建
        await _cleanup_session(tenant_id)

    # 创建新会话
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    context = await browser.new_context(
        viewport={"width": 1280, "height": 720},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
    )
    page = await context.new_page()

    session = _BrowserSession(
        playwright=pw,
        browser=browser,
        context=context,
        page=page,
        tenant_id=tenant_id,
        last_used=time.time(),
    )
    _sessions[tenant_id] = session
    return session


async def _analyze_screenshot(screenshot: bytes, prompt: str) -> str:
    """用 Gemini 视觉分析浏览器截图"""
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        return "(Gemini SDK 未安装，无法分析截图，仅返回页面文本)"

    tenant = _get_tenant_or_none()
    if not tenant or not tenant.llm_api_key:
        return "(未配置 Gemini API Key，无法分析截图)"

    # 构建 client（同 sandbox_caps 模式）
    http_options: dict = {"timeout": 60_000}
    _custom_base = tenant.llm_base_url
    if _custom_base and "moonshot" not in _custom_base and "openai.com" not in _custom_base:
        http_options["base_url"] = _custom_base
    env_base = os.getenv("GOOGLE_GEMINI_BASE_URL", "")
    if not http_options.get("base_url") and env_base:
        http_options["base_url"] = env_base
    proxy_url = os.getenv("GEMINI_PROXY", "")
    if proxy_url:
        http_options["async_client_args"] = {"proxy": proxy_url}

    client = genai.Client(api_key=tenant.llm_api_key, http_options=http_options)
    model = tenant.llm_model or "gemini-3-flash-preview"

    try:
        parts = [
            genai_types.Part(inline_data=genai_types.Blob(
                mime_type="image/png", data=screenshot,
            )),
            genai_types.Part(text=prompt),
        ]
        response = await client.aio.models.generate_content(
            model=model,
            contents=[genai_types.Content(role="user", parts=parts)],
            config=genai_types.GenerateContentConfig(
                temperature=0.2, max_output_tokens=2048,
            ),
        )
        return response.text or "(Gemini 返回空结果)"
    except Exception as e:
        logger.warning("browser_ops: Gemini screenshot analysis failed: %s", e)
        return f"(截图分析失败: {e})"


def _check_playwright() -> str | None:
    """检查 Playwright 是否可用，返回错误提示或 None"""
    try:
        import playwright  # noqa: F401
        return None
    except ImportError:
        return (
            "浏览器引擎 (Playwright) 未安装。请按以下步骤安装：\n\n"
            "方式 1 — 使用 install_package 工具：\n"
            "  调用 install_package(package_name='playwright')\n"
            "  然后让管理员在服务器上运行: playwright install --with-deps chromium\n\n"
            "方式 2 — 手动安装：\n"
            "  pip install playwright\n"
            "  playwright install --with-deps chromium\n\n"
            "安装完成后重试即可。"
        )


# ── 工具处理函数 ──

async def _handle_browser_open(args: dict) -> ToolResult:
    """打开浏览器访问 URL"""
    url = args.get("url", "").strip()
    wait_for = args.get("wait_for", "").strip()

    # URL 安全检查
    url_err = _validate_url(url)
    if url_err:
        return ToolResult.invalid_param(url_err)

    # ── 快速路径：Google Docs/Sheets/Slides 直接导出 ──
    # 比浏览器渲染快 10 倍，且不受 SPA 动态加载影响
    google_result = await _try_google_doc_export(url)
    if google_result is not None:
        return google_result

    # 检查 Playwright
    err = _check_playwright()
    if err:
        return ToolResult.error(err)

    tenant_id = _get_tenant_id()
    if not tenant_id:
        return ToolResult.error("无法获取当前租户信息")

    try:
        session = await _get_or_create_session(tenant_id)
        page = session.page

        # 导航
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # 等待特定元素（可选）
        if wait_for:
            try:
                await page.wait_for_selector(wait_for, timeout=10000)
            except Exception:
                pass  # 超时不阻塞

        # 等待页面稳定（SPA 友好：先等 networkidle，超时就算了）
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            await asyncio.sleep(2)  # networkidle 超时，给 SPA 渲染额外时间

        # 截图
        screenshot = await page.screenshot(type="png", full_page=False)
        title = await page.title()
        current_url = page.url

        # 提取可见文本
        try:
            body_text = await page.inner_text("body")
            body_text = body_text[:_MAX_TEXT_LENGTH]
        except Exception:
            body_text = "(无法提取页面文本)"

        # AI 分析截图
        analysis = await _analyze_screenshot(screenshot, _SCREENSHOT_PROMPT)

        # 注册浏览器访问的真实 URL 到来源注册表
        from app.tools.source_registry import register_urls
        register_urls([current_url])

        return ToolResult.success(
            f"页面已打开\n"
            f"标题: {title}\n"
            f"URL: {current_url}\n\n"
            f"── AI 页面分析 ──\n{analysis}\n\n"
            f"── 页面文本（前 {_MAX_TEXT_LENGTH} 字符）──\n{body_text}"
        )

    except Exception as e:
        logger.exception("browser_ops: browser_open failed")
        return ToolResult.error(f"打开页面失败: {e}")


async def _handle_browser_do(args: dict) -> ToolResult:
    """在当前页面执行操作"""
    err = _check_playwright()
    if err:
        return ToolResult.error(err)

    action = args.get("action", "").strip()
    selector = args.get("selector", "").strip()
    value = args.get("value", "").strip()

    if not action:
        return ToolResult.invalid_param("action 不能为空")

    tenant_id = _get_tenant_id()
    session = _sessions.get(tenant_id)
    if not session:
        return ToolResult.error(
            "没有活跃的浏览器会话。请先调用 browser_open 打开页面。\n"
            "注意：如果之前打开的是 Google 文档/表格，系统会使用快速导出模式，不产生浏览器会话。 "
            "这种情况下你已经拿到了全部内容，不需要（也无法）执行滚动或点击操作。"
        )

    session.last_used = time.time()
    page = session.page

    try:
        if action == "click":
            if not selector:
                return ToolResult.invalid_param("click 操作需要 selector")
            # 尝试 CSS 选择器，失败则尝试文本匹配
            try:
                await page.click(selector, timeout=5000)
            except Exception:
                await page.get_by_text(selector, exact=False).first.click(timeout=5000)
            action_desc = f"点击了 '{selector}'"

        elif action == "fill":
            if not selector:
                return ToolResult.invalid_param("fill 操作需要 selector")
            if value is None:
                return ToolResult.invalid_param("fill 操作需要 value")
            try:
                await page.fill(selector, value, timeout=5000)
            except Exception:
                await page.get_by_placeholder(selector).first.fill(value, timeout=5000)
            action_desc = f"在 '{selector}' 中输入了 '{value[:50]}'"

        elif action == "scroll":
            direction = value or "down"
            distance = 500
            if direction == "up":
                distance = -500
            await page.evaluate(f"window.scrollBy(0, {distance})")
            action_desc = f"页面向{'上' if distance < 0 else '下'}滚动"

        elif action == "select":
            if not selector or not value:
                return ToolResult.invalid_param("select 操作需要 selector 和 value")
            await page.select_option(selector, value, timeout=5000)
            action_desc = f"在 '{selector}' 中选择了 '{value}'"

        elif action == "hover":
            if not selector:
                return ToolResult.invalid_param("hover 操作需要 selector")
            await page.hover(selector, timeout=5000)
            action_desc = f"悬停在 '{selector}'"

        elif action == "press":
            key = value or "Enter"
            if selector:
                await page.press(selector, key, timeout=5000)
            else:
                await page.keyboard.press(key)
            action_desc = f"按下了 '{key}'"

        elif action == "wait":
            wait_time = min(int(value or "3"), 10)
            await asyncio.sleep(wait_time)
            action_desc = f"等待了 {wait_time} 秒"

        elif action == "goto":
            if not value:
                return ToolResult.invalid_param("goto 操作需要 value (URL)")
            url_err = _validate_url(value)
            if url_err:
                return ToolResult.invalid_param(url_err)
            await page.goto(value, wait_until="domcontentloaded", timeout=30000)
            action_desc = f"导航到 {value}"

        else:
            return ToolResult.invalid_param(
                f"不支持的操作 '{action}'。"
                f"支持: click, fill, scroll, select, hover, press, wait, goto"
            )

        # 操作后截图分析
        await asyncio.sleep(0.5)
        screenshot = await page.screenshot(type="png", full_page=False)
        title = await page.title()
        current_url = page.url

        analysis = await _analyze_screenshot(
            screenshot,
            f"刚刚执行了操作：{action_desc}。请描述操作后的页面变化和当前状态。{_SCREENSHOT_PROMPT}",
        )

        # 注册导航后的真实 URL 到来源注册表
        from app.tools.source_registry import register_urls
        register_urls([current_url])

        return ToolResult.success(
            f"操作完成: {action_desc}\n"
            f"当前页面: {title} ({current_url})\n\n"
            f"── AI 页面分析 ──\n{analysis}"
        )

    except Exception as e:
        logger.warning("browser_ops: browser_do failed: %s", e)
        return ToolResult.error(f"操作失败: {e}")


async def _handle_browser_read(args: dict) -> ToolResult:
    """提取当前页面的文本内容"""
    tenant_id = _get_tenant_id()
    session = _sessions.get(tenant_id)
    if not session:
        return ToolResult.error("没有活跃的浏览器会话。请先调用 browser_open 打开页面。")

    session.last_used = time.time()
    page = session.page
    selector = args.get("selector", "").strip() or "body"

    try:
        text = await page.inner_text(selector, timeout=5000)
        text = text[:_MAX_TEXT_LENGTH * 2]  # 读取时允许更多文本

        current_url = page.url
        title = await page.title()

        return ToolResult.success(
            f"页面: {title} ({current_url})\n"
            f"选择器: {selector}\n\n"
            f"── 提取的文本 ──\n{text}"
        )
    except Exception as e:
        return ToolResult.error(f"提取文本失败: {e}")


async def _handle_browser_close(args: dict) -> ToolResult:
    """关闭浏览器会话"""
    tenant_id = _get_tenant_id()
    if tenant_id not in _sessions:
        return ToolResult.success("没有活跃的浏览器会话。")

    await _cleanup_session(tenant_id)
    return ToolResult.success("浏览器会话已关闭。")


# ── 工具注册（标准接口）──

TOOL_DEFINITIONS = [
    {
        "name": "browser_open",
        "description": (
            "打开浏览器访问指定 URL。自动截图并用 AI 分析页面内容。"
            "用于访问任何网页（社交媒体、管理后台、表单等）。"
            "需要先安装 Playwright（用 install_package 工具）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要访问的 URL（必须 http/https）",
                },
                "wait_for": {
                    "type": "string",
                    "description": "等待特定 CSS 选择器出现后再截图（可选）",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_do",
        "description": (
            "在当前浏览器页面执行操作。操作后自动截图分析。"
            "支持: click（点击）, fill（输入文字）, scroll（滚动）, "
            "select（下拉选择）, hover（悬停）, press（按键）, wait（等待）, goto（导航到新URL）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["click", "fill", "scroll", "select", "hover", "press", "wait", "goto"],
                    "description": "操作类型",
                },
                "selector": {
                    "type": "string",
                    "description": "CSS 选择器或按钮/链接上的文字。click/fill/select/hover 必填。",
                },
                "value": {
                    "type": "string",
                    "description": "输入值（fill 必填）、按键名（press 用，如 Enter/Tab）、URL（goto 必填）、方向（scroll 用 up/down）",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "browser_read",
        "description": "提取当前浏览器页面的文本内容（全部或指定选择器区域）",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS 选择器（可选，默认读取整个 body）",
                },
            },
        },
    },
    {
        "name": "browser_close",
        "description": "关闭当前浏览器会话，释放资源",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]

TOOL_MAP = {
    "browser_open": _handle_browser_open,
    "browser_do": _handle_browser_do,
    "browser_read": _handle_browser_read,
    "browser_close": _handle_browser_close,
}
