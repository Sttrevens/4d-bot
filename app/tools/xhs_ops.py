"""小红书自动化工具 —— Playwright 浏览器驱动

基于 Playwright 直接操作小红书网页版，替代 TikHub API。
参考 xpzouying/xiaohongshu-mcp 的思路，但用 Gemini 视觉替代 DOM 硬编码。

架构：
- 独立 BrowserContext（不与通用 browser_ops 共享，避免 Cookie 冲突）
- Cookie 持久化到 Redis（xhs:cookies:{tenant_id}）
- Gemini 视觉分析页面，提取结构化数据（抗改版）
- 每个 tenant 独立登录态

工具列表：
- xhs_login          QR码登录
- xhs_check_login    检查登录态
- xhs_search         搜索笔记/用户
- xhs_get_note       获取笔记详情
- xhs_get_user       获取用户主页
- xhs_publish        发布图文笔记
- xhs_comment        评论/回复
- xhs_like           点赞/收藏

安全：
- 所有写操作需要已登录态
- 操作频率由 LLM agent 自行控制（建议间隔 3-5 秒）
- 禁止批量注册/刷量等违规行为
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import io

import httpx

from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

# ── 常量 ──

_XHS_BASE = "https://www.xiaohongshu.com"
_SESSION_TIMEOUT = 600  # 10 分钟不活动自动关闭（比通用 browser 长，因为社媒操作慢）
_COOKIE_REDIS_PREFIX = "xhs:cookies:"
_COOKIE_TTL = 86400 * 30  # Cookie 缓存 30 天（参考 xiaohongshu-mcp，cookie 实际有效期较长）
_MAX_TEXT = 5000  # 页面文本最大提取长度
_SEARCH_TIMEOUT = 60  # xhs_search 整体超时（秒），防止吃掉 agent 全部时间预算

# 登录轮询参数
_LOGIN_POLL_INTERVAL = 4  # 每 4 秒检查一次
_LOGIN_POLL_TIMEOUT = 120  # 最多等 2 分钟
_SMS_CODE_TIMEOUT = 120  # 等待用户回复验证码的超时（秒）
_SMS_PENDING_PREFIX = "xhs:sms_pending:"  # Redis key: 等待用户输入验证码
_SMS_CODE_PREFIX = "xhs:sms_code:"  # Redis key: 用户回复的验证码
_SMS_PHONE_PREFIX = "xhs:phone:"  # Redis key: 缓存用户手机号
_SMS_LAST_CODE_PREFIX = "xhs:last_code:"  # Redis key: 上次使用过的验证码（防重复）

# 企微 API
_WECOM_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
_WECOM_UPLOAD_URL = "https://qyapi.weixin.qq.com/cgi-bin/media/upload"
_WECOM_KF_SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg"
_WECOM_SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"

# token 缓存
_wecom_token_cache: dict[str, tuple[str, float]] = {}

_SCREENSHOT_EXTRACT_PROMPT = (
    "你是一个小红书数据提取助手。请从截图中提取结构化信息。\n"
    "根据页面类型提取不同数据：\n"
    "- 搜索结果页：提取每个笔记的标题、作者昵称、点赞数、链接（如果可见）\n"
    "- 笔记详情页：提取标题、作者、正文内容、点赞/收藏/评论数、评论列表\n"
    "- 用户主页：提取昵称、简介、粉丝数/关注数/获赞数、笔记列表\n"
    "- 登录页：描述登录状态和可用的登录方式\n"
    "用中文回复，尽量结构化（用 | 分隔字段），数据要准确。"
)

_LOGIN_QR_PROMPT = (
    "这是小红书的登录页面截图。请告诉我：\n"
    "1. 页面上是否显示了二维码？\n"
    "2. 二维码的位置在哪里？\n"
    "3. 页面上还有什么其他登录选项？\n"
    "4. 是否有任何错误提示？\n"
    "用中文简洁回复。"
)

_LOGIN_CHECK_PROMPT = (
    "请判断这个小红书页面的登录状态：\n"
    "1. 用户是否已登录？（检查右上角是否有头像/用户名，而不是'登录'按钮）\n"
    "2. 如果已登录，用户昵称是什么？\n"
    "3. 页面是否正常加载？\n"
    "用中文简洁回复，第一个词用'已登录'或'未登录'开头。"
)


# ── 会话管理 ──

@dataclass
class _XhsSession:
    """小红书浏览器会话（独立于通用 browser_ops）"""
    playwright: Any = None
    browser: Any = None
    context: Any = None
    page: Any = None
    creator_context: Any = None  # 独立的 creator 平台 context（CDP cookie 注入）
    creator_page: Any = None     # creator context 的 page
    tenant_id: str = ""
    logged_in: bool = False
    last_used: float = field(default_factory=time.time)


# tenant_id → XHS session
_xhs_sessions: dict[str, _XhsSession] = {}


def _get_tenant_id() -> str:
    try:
        from app.tenant.context import get_current_tenant
        return get_current_tenant().tenant_id or ""
    except Exception:
        return ""


def _get_user_id() -> str:
    """获取当前用户 ID（用于 per-user session/cookie 隔离）"""
    try:
        from app.tools.feishu_api import _current_user_open_id
        return _current_user_open_id.get("")
    except Exception:
        return ""


def _get_session_key() -> str:
    """生成 per-user session key: tenant_id:user_id
    如果 user_id 不可用则退化为 tenant_id（向后兼容）"""
    tid = _get_tenant_id()
    uid = _get_user_id()
    if tid and uid:
        return f"{tid}:{uid}"
    return tid


def _get_tenant_or_none():
    try:
        from app.tenant.context import get_current_tenant
        return get_current_tenant()
    except Exception:
        return None


# ── Redis Cookie 持久化 ──

async def _save_cookies_to_redis(session_key: str, cookies: list[dict]) -> None:
    """保存浏览器 Cookie 到 Redis（per-user 隔离）

    关键：将 xiaohongshu.com 子域名 cookie 的 domain 统一为 .xiaohongshu.com，
    这样 creator.xiaohongshu.com 等子域名也能使用这些 cookie（MCP 项目验证过的方案）。
    """
    try:
        from app.services.redis_client import execute as redis_execute
        # 统一 cookie domain 为 .xiaohongshu.com（覆盖 www/creator/edith 等子域名）
        broadened = []
        for c in cookies:
            cc = dict(c)
            domain = cc.get("domain", "")
            if domain.endswith("xiaohongshu.com") and not domain.startswith("."):
                cc["domain"] = ".xiaohongshu.com"
            broadened.append(cc)
        key = f"{_COOKIE_REDIS_PREFIX}{session_key}"
        result = redis_execute("SET", key, json.dumps(broadened, ensure_ascii=False), "EX", _COOKIE_TTL)
        if result:
            logger.info("xhs_ops: saved %d cookies for %s (domains broadened)", len(broadened), session_key)
    except Exception as e:
        logger.warning("xhs_ops: failed to save cookies to Redis: %s", e)


async def _load_cookies_from_redis(session_key: str) -> list[dict] | None:
    """从 Redis 加载 Cookie（per-user 隔离）"""
    try:
        from app.services.redis_client import execute as redis_execute
        key = f"{_COOKIE_REDIS_PREFIX}{session_key}"
        data = redis_execute("GET", key)
        if data:
            cookies = json.loads(data)
            logger.info("xhs_ops: loaded %d cookies for %s", len(cookies), session_key)
            return cookies
    except Exception as e:
        logger.warning("xhs_ops: failed to load cookies from Redis: %s", e)
    return None


async def _clear_cookies_from_redis(session_key: str) -> None:
    """清除 Redis 中的 Cookie（per-user 隔离）"""
    try:
        from app.services.redis_client import execute as redis_execute
        redis_execute("DEL", f"{_COOKIE_REDIS_PREFIX}{session_key}")
    except Exception:
        pass


# ── Gemini 视觉分析 ──

async def _analyze_screenshot(screenshot: bytes, prompt: str) -> str:
    """用 Gemini 视觉分析截图（复用 browser_ops 的模式）"""
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        return "(Gemini SDK 未安装，无法分析截图)"

    tenant = _get_tenant_or_none()
    if not tenant or not tenant.llm_api_key:
        return "(未配置 Gemini API Key)"

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
                temperature=0.1, max_output_tokens=4096,
            ),
        )
        return response.text or "(Gemini 返回空结果)"
    except Exception as e:
        logger.warning("xhs_ops: Gemini analysis failed: %s", e)
        return f"(截图分析失败: {e})"


# ── 发送二维码图片给用户 ──


def _get_wecom_token(corpid: str, secret: str, cache_key: str = "") -> str:
    """同步获取企微 access_token（带缓存）"""
    ck = cache_key or corpid
    cached = _wecom_token_cache.get(ck)
    if cached and time.time() < cached[1]:
        return cached[0]
    with httpx.Client(timeout=10, trust_env=False) as client:
        resp = client.get(_WECOM_TOKEN_URL, params={"corpid": corpid, "corpsecret": secret})
        data = resp.json()
    if data.get("errcode", -1) != 0:
        raise RuntimeError(f"wecom token error: {data}")
    token = data["access_token"]
    expire = time.time() + data.get("expires_in", 7200) - 300
    _wecom_token_cache[ck] = (token, expire)
    return token


def _send_qr_image_to_user(screenshot_png: bytes) -> bool:
    """将二维码截图作为图片消息发送给当前用户。

    支持企微客服(wecom_kf)和企微内部应用(wecom)。
    返回 True 表示发送成功。
    """
    try:
        from app.tenant.context import get_current_tenant
        from app.tools.feishu_api import _current_user_open_id

        tenant = get_current_tenant()
        sender_id = _current_user_open_id.get("")
        if not sender_id:
            logger.warning("xhs_ops: cannot send QR — no sender_id")
            return False

        platform = tenant.platform
        if platform not in ("wecom", "wecom_kf"):
            logger.info("xhs_ops: QR image send not supported on platform %s", platform)
            return False

        # 获取 token
        if platform == "wecom":
            token = _get_wecom_token(tenant.wecom_corpid, tenant.wecom_corpsecret)
        else:
            token = _get_wecom_token(
                tenant.wecom_corpid,
                tenant.wecom_kf_secret,
                cache_key=f"{tenant.wecom_corpid}:kf",
            )

        # 上传图片为临时素材（type=image）
        with httpx.Client(timeout=30, trust_env=False) as client:
            resp = client.post(
                _WECOM_UPLOAD_URL,
                params={"access_token": token, "type": "image"},
                files={"media": ("xhs_qr.png", io.BytesIO(screenshot_png), "image/png")},
            )
            data = resp.json()

        if data.get("errcode", 0) != 0:
            logger.error("xhs_ops: upload QR image failed: %s", data)
            return False

        media_id = data.get("media_id", "")
        if not media_id:
            return False

        # 发送图片消息
        if platform == "wecom":
            body = {
                "touser": sender_id,
                "msgtype": "image",
                "agentid": tenant.wecom_agent_id,
                "image": {"media_id": media_id},
            }
            send_url = _WECOM_SEND_URL
        else:
            body = {
                "touser": sender_id,
                "open_kfid": tenant.wecom_kf_open_kfid,
                "msgtype": "image",
                "image": {"media_id": media_id},
            }
            send_url = _WECOM_KF_SEND_URL

        with httpx.Client(timeout=10, trust_env=False) as client:
            resp = client.post(send_url, params={"access_token": token}, json=body)
            result = resp.json()

        if result.get("errcode", -1) != 0:
            logger.error("xhs_ops: send QR image failed: %s", result)
            return False

        logger.info("xhs_ops: QR image sent to user %s", sender_id)
        return True

    except Exception as e:
        logger.warning("xhs_ops: failed to send QR image: %s", e)
        return False


def _send_text_to_user(text: str) -> bool:
    """发送文本消息给当前用户（企微客服/企微内部应用）。"""
    try:
        from app.tenant.context import get_current_tenant
        from app.tools.feishu_api import _current_user_open_id

        tenant = get_current_tenant()
        sender_id = _current_user_open_id.get("")
        if not sender_id:
            logger.warning("xhs_ops: cannot send text — no sender_id")
            return False

        platform = tenant.platform
        if platform not in ("wecom", "wecom_kf"):
            return False

        if platform == "wecom":
            token = _get_wecom_token(tenant.wecom_corpid, tenant.wecom_corpsecret)
        else:
            token = _get_wecom_token(
                tenant.wecom_corpid,
                tenant.wecom_kf_secret,
                cache_key=f"{tenant.wecom_corpid}:kf",
            )

        if platform == "wecom":
            body = {
                "touser": sender_id,
                "msgtype": "text",
                "agentid": tenant.wecom_agent_id,
                "text": {"content": text},
            }
            send_url = _WECOM_SEND_URL
        else:
            body = {
                "touser": sender_id,
                "open_kfid": tenant.wecom_kf_open_kfid,
                "msgtype": "text",
                "text": {"content": text},
            }
            send_url = _WECOM_KF_SEND_URL

        with httpx.Client(timeout=10, trust_env=False) as client:
            resp = client.post(send_url, params={"access_token": token}, json=body)
            result = resp.json()

        if result.get("errcode", -1) != 0:
            logger.error("xhs_ops: send text failed: %s", result)
            return False

        logger.info("xhs_ops: text sent to user %s: %s", sender_id, text[:50])
        return True

    except Exception as e:
        logger.warning("xhs_ops: failed to send text: %s", e)
        return False


# ── SMS 验证码登录 ──

def _sms_redis_keys(session_key: str) -> tuple[str, str, str]:
    """返回 SMS 相关的 Redis key 三元组: (pending, code, phone)"""
    return (
        f"{_SMS_PENDING_PREFIX}{session_key}",
        f"{_SMS_CODE_PREFIX}{session_key}",
        f"{_SMS_PHONE_PREFIX}{session_key}",
    )


async def _detect_captcha(page) -> bool:
    """检测页面上是否存在滑块/旋转验证码。"""
    try:
        return await page.evaluate("""() => {
            // 小红书 redCaptcha 专用 selector（最高优先级）
            if (document.querySelector('div.red-captcha-slider')) return true;
            if (document.querySelector('#red-captcha-rotate')) return true;
            // 通用 selector
            const selectors = [
                '[class*="captcha"]', '[class*="verify"]',
                '[class*="geetest"]', '[class*="tcaptcha"]', '[id*="captcha"]',
                'iframe[src*="captcha"]',
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el && el.offsetParent !== null) return true;
            }
            return false;
        }""")
    except Exception:
        return False


async def _try_solve_slider_captcha(page) -> bool:
    """检测并自动解决小红书旋转验证码（redCaptcha）。

    小红书使用自研的旋转验证码：
    - 一张圆形图片被随机旋转了某个角度
    - 下方有一个水平滑块
    - 拖动滑块可以旋转图片
    - 需要将图片旋转到正确方向

    公式：drag_distance = (rotation_angle / 360) * track_width

    用 Gemini Vision 判断图片需要旋转多少度。

    返回 True 如果检测到并尝试了验证码，False 如果没有验证码。
    """
    import random

    # ── 检测是否有验证码 ──
    if not await _detect_captcha(page):
        return False

    logger.info("xhs_ops: CAPTCHA detected, attempting auto-solve")

    # ── 定位滑块和旋转图片 ──
    # 小红书 redCaptcha 专用 selector
    slider_selectors = [
        'div.red-captcha-slider',
        '[class*="red-captcha-slider"]',
        '[class*="captcha-slider"]',
        '[class*="slider-btn"]',
        '[class*="slider"] [class*="btn"]',
    ]
    rotate_img_selectors = [
        '#red-captcha-rotate img',
        'div#red-captcha-rotate > img',
        '[class*="red-captcha"] img',
        '[class*="captcha-rotate"] img',
    ]

    btn_box = None
    track_width = 280  # redCaptcha 默认轨道宽度

    # 找滑块按钮
    for sel in slider_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=500):
                btn_box = await loc.bounding_box()
                if btn_box:
                    logger.info("xhs_ops: found captcha slider via CSS: %s", sel)
                    # 获取滑块父容器（轨道）的宽度
                    try:
                        parent_box = await page.locator(sel).first.locator("..").bounding_box()
                        if parent_box and parent_box["width"] > 50:
                            track_width = parent_box["width"]
                    except Exception:
                        pass
                    break
        except Exception:
            continue

    # ── 提取旋转图片并用 Gemini 判断角度 ──
    rotation_angle = None

    # 尝试从 DOM 提取旋转图片
    img_src = None
    for sel in rotate_img_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=500):
                img_src = await loc.get_attribute("src")
                if img_src:
                    logger.info("xhs_ops: found captcha rotate image: %s", sel)
                    break
        except Exception:
            continue

    if img_src:
        # 获取图片数据发给 Gemini
        try:
            img_bytes = None
            if img_src.startswith("data:image"):
                import base64
                b64_data = img_src.split(",", 1)[1] if "," in img_src else img_src
                img_bytes = base64.b64decode(b64_data)
            elif img_src.startswith("http"):
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(img_src)
                    if resp.status_code == 200:
                        img_bytes = resp.content

            if img_bytes:
                result = await _analyze_screenshot(img_bytes, (
                    "这是一张被旋转了的圆形图片（来自旋转验证码）。\n"
                    "请判断这张图片被**顺时针**旋转了大约多少度才变成当前的样子。\n"
                    "也就是说，我需要将它**顺时针**再旋转多少度才能回到正常方向？\n"
                    "只回复一个数字（0-359），不要任何其他文字。\n"
                    "例如：如果图片看起来是倒着的，回复 180"
                ))
                import re
                angle_match = re.search(r'(\d{1,3})', result)
                if angle_match:
                    rotation_angle = int(angle_match.group(1))
                    if rotation_angle > 359:
                        rotation_angle = rotation_angle % 360
                    logger.info("xhs_ops: Gemini estimated rotation angle: %d degrees", rotation_angle)
        except Exception as e:
            logger.warning("xhs_ops: failed to analyze captcha image: %s", e)

    # 如果没有提取到图片或角度，用整页截图让 Gemini 判断
    if rotation_angle is None:
        try:
            screenshot = await page.screenshot(type="png", full_page=False)
            vp = page.viewport_size or {"width": 1280, "height": 800}
            result = await _analyze_screenshot(screenshot, (
                "这个页面上有一个小红书旋转验证码（redCaptcha）。\n"
                "页面中有一张圆形图片被旋转了某个角度，下方有一个水平滑块。\n"
                "请判断：\n"
                "1. 图片需要**顺时针**旋转多少度才能回到正常方向？\n"
                "2. 滑块按钮的位置（像素坐标）\n"
                "3. 滑块轨道的总宽度（像素）\n"
                f"页面尺寸是 {vp['width']}x{vp['height']} 像素。\n"
                "回复格式：\n"
                "ANGLE: 数字\n"
                "SLIDER_BTN: x=数字,y=数字\n"
                "TRACK_WIDTH: 数字\n"
                "如果没有旋转验证码，回复：NO_CAPTCHA"
            ))
            logger.info("xhs_ops: Gemini captcha analysis: %s", result[:300])

            if "NO_CAPTCHA" in result:
                return False

            import re
            angle_m = re.search(r'ANGLE:\s*(\d{1,3})', result)
            if angle_m:
                rotation_angle = int(angle_m.group(1)) % 360
                logger.info("xhs_ops: Gemini full-page angle: %d degrees", rotation_angle)

            # 如果还没有 btn_box，尝试从 Vision 结果定位
            if not btn_box:
                btn_m = re.search(r'SLIDER_BTN:\s*x\s*=\s*(\d+)\s*[,，]\s*y\s*=\s*(\d+)', result)
                if btn_m:
                    bx, by = int(btn_m.group(1)), int(btn_m.group(2))
                    btn_box = {"x": bx - 15, "y": by - 15, "width": 30, "height": 30}

            tw_m = re.search(r'TRACK_WIDTH:\s*(\d+)', result)
            if tw_m:
                track_width = int(tw_m.group(1))

        except Exception as e:
            logger.warning("xhs_ops: Gemini captcha analysis failed: %s", e)

    if not btn_box:
        logger.warning("xhs_ops: captcha detected but could not locate slider button")
        return True  # 检测到了但没定位到

    if rotation_angle is None:
        # 无法判断角度，随机试一个（50-70% 位置，避免太极端）
        rotation_angle = random.randint(90, 270)
        logger.info("xhs_ops: could not determine angle, trying random: %d", rotation_angle)

    # ── 计算拖动距离 ──
    # drag_distance = (angle / 360) * track_width
    drag_distance = (rotation_angle / 360.0) * track_width
    if drag_distance < 5:
        drag_distance = 5

    start_x = btn_box["x"] + btn_box["width"] / 2
    start_y = btn_box["y"] + btn_box["height"] / 2

    logger.info("xhs_ops: rotation CAPTCHA: angle=%d, track_width=%d, drag_distance=%.0f",
                rotation_angle, track_width, drag_distance)

    # ── 模拟人类拖动 ──
    await page.mouse.move(start_x, start_y)
    await asyncio.sleep(random.uniform(0.08, 0.15))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.08, 0.15))

    steps = random.randint(25, 40)
    for i in range(steps):
        progress = (i + 1) / steps
        # ease-out: 先快后慢
        eased = 1 - (1 - progress) ** 2
        x = start_x + drag_distance * eased
        y = start_y + random.uniform(-2, 2)
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.01, 0.03))

    # 到达终点后稍微停一下再松手
    await asyncio.sleep(random.uniform(0.05, 0.15))
    await page.mouse.up()

    logger.info("xhs_ops: captcha drag completed (angle=%d, distance=%.0f)",
                rotation_angle, drag_distance)
    return True


async def _handle_sms_login(page, session_key: str, phone: str = "") -> bool:
    """在创作者平台登录页用手机号+验证码登录。

    流程：
    1. 如果没有手机号 → 从 Redis 缓存读取
    2. 输入手机号 → 点击「获取验证码」
    3. 发消息告诉用户「验证码已发到手机」
    4. 轮询 Redis 等待用户回复验证码（webhook handler 写入）
    5. 输入验证码 → 点击登录
    """
    from app.services.redis_client import execute as redis_execute

    pending_key, code_key, phone_key = _sms_redis_keys(session_key)

    # 获取手机号：参数 > Redis 缓存
    if not phone:
        cached = redis_execute("GET", phone_key)
        if cached:
            phone = cached
            logger.info("xhs_ops: using cached phone for %s", session_key)

    if not phone:
        # 没有手机号，发消息询问用户
        _send_text_to_user("我需要用手机号登录小红书创作者平台，请回复你的手机号（11位数字）")
        # 设置等待标志
        redis_execute("SET", pending_key, "phone", "EX", _SMS_CODE_TIMEOUT)
        # 轮询等待手机号
        start = time.time()
        while time.time() - start < _SMS_CODE_TIMEOUT:
            await asyncio.sleep(_LOGIN_POLL_INTERVAL)
            val = redis_execute("GET", code_key)
            if val:
                phone = val
                redis_execute("DEL", code_key)
                break
        redis_execute("DEL", pending_key)
        if not phone:
            logger.warning("xhs_ops: SMS login timeout waiting for phone number")
            return False

    logger.info("xhs_ops: SMS login with phone %s***%s", phone[:3], phone[-2:])

    # 缓存手机号（30 天）
    redis_execute("SET", phone_key, phone, "EX", _COOKIE_TTL)

    # ── 在页面上输入手机号 ──
    # 注意：Playwright fill() 可能不触发 React onChange，
    # 需要用 dispatchEvent 确保 React 内部状态同步
    phone_entered = False
    phone_selectors = [
        'input[placeholder*="手机号"]',
        'input[placeholder*="手机"]',
        'input[placeholder*="phone"]',
        'input[type="tel"]',
        'input[name*="phone"]',
        'input[name*="mobile"]',
        '.phone-input input',
        '[class*="phone"] input',
        '[class*="login-form"] input[type="text"]',
    ]
    for sel in phone_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1000):
                await loc.click()
                # 清空再用键盘输入（比 fill() 更可靠触发 React 事件）
                await loc.fill("")
                await loc.type(phone, delay=30)
                # 手动 dispatch input/change 确保 React state 更新
                await page.evaluate("""(sel) => {
                    const el = document.querySelector(sel);
                    if (el) {
                        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ).set;
                        nativeInputValueSetter.call(el, el.value);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""", sel)
                phone_entered = True
                logger.info("xhs_ops: entered phone via selector: %s", sel)
                break
        except Exception:
            continue

    if not phone_entered:
        # Gemini Vision 兜底：定位手机号输入框
        try:
            screenshot = await page.screenshot(type="png", full_page=False)
            vp = page.viewport_size or {"width": 1280, "height": 800}
            location = await _analyze_screenshot(screenshot, (
                "这是小红书创作者平台的登录页面，默认显示手机号登录。\n"
                "请找到手机号输入框的位置。\n"
                f"页面尺寸是 {vp['width']}x{vp['height']} 像素。\n"
                "请回复输入框的大致像素坐标，格式：x=数字,y=数字"
            ))
            import re
            coord_match = re.search(r'x\s*=\s*(\d+)\s*[,，]\s*y\s*=\s*(\d+)', location)
            if coord_match:
                x, y = int(coord_match.group(1)), int(coord_match.group(2))
                await page.mouse.click(x, y)
                await asyncio.sleep(0.5)
                await page.keyboard.type(phone, delay=50)
                phone_entered = True
                logger.info("xhs_ops: entered phone via Vision at (%d, %d)", x, y)
        except Exception as e:
            logger.warning("xhs_ops: Vision phone input failed: %s", e)

    if not phone_entered:
        logger.error("xhs_ops: could not find phone input on creator login page")
        return False

    await asyncio.sleep(1)

    # ── 点击「获取验证码」按钮 ──
    code_btn_clicked = False
    code_btn_selectors = [
        'text=获取验证码', 'text=发送验证码', 'text=获取短信验证码',
        'text=获取验证码', 'text=发送', 'text=获取',
        '[class*="send-code"]', '[class*="get-code"]',
        '[class*="verify-btn"]', '[class*="code-btn"]',
        'button:has-text("验证码")', 'button:has-text("获取")',
    ]
    for sel in code_btn_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=800):
                await loc.click()
                code_btn_clicked = True
                logger.info("xhs_ops: clicked get-code button: %s", sel)
                break
        except Exception:
            continue

    if not code_btn_clicked:
        # JS 兜底
        try:
            clicked = await page.evaluate("""() => {
                const btns = document.querySelectorAll('button, span, a, div');
                for (const btn of btns) {
                    const text = (btn.textContent || '').trim();
                    if (/获取|发送|验证码/.test(text) && text.length < 10
                        && btn.offsetParent !== null) {
                        btn.click();
                        return text;
                    }
                }
                return null;
            }""")
            if clicked:
                code_btn_clicked = True
                logger.info("xhs_ops: clicked get-code via JS: %s", clicked)
        except Exception:
            pass

    if not code_btn_clicked:
        logger.error("xhs_ops: could not find get-code button")
        _send_text_to_user("找不到「获取验证码」按钮，请稍后重试或手动登录 creator.xiaohongshu.com")
        return False

    # ── 通知用户输入验证码 ──
    masked = f"{phone[:3]}****{phone[-4:]}"
    _send_text_to_user(f"验证码已发送到 {masked}，请回复验证码（4-6位数字）")

    # 设置 Redis 等待标志
    redis_execute("SET", pending_key, "code", "EX", _SMS_CODE_TIMEOUT)
    # 清除旧 code
    redis_execute("DEL", code_key)

    # ── 轮询 Redis 等待验证码 ──
    last_code_key = f"{_SMS_LAST_CODE_PREFIX}{session_key}"
    last_used_code = redis_execute("GET", last_code_key) or ""
    logger.info("xhs_ops: polling for SMS code (timeout=%ds)", _SMS_CODE_TIMEOUT)
    start = time.time()
    sms_code = ""
    while time.time() - start < _SMS_CODE_TIMEOUT:
        await asyncio.sleep(_LOGIN_POLL_INTERVAL)
        val = redis_execute("GET", code_key)
        if val:
            # 检测验证码是否与上次使用的相同（用户可能发了旧码）
            if val == last_used_code:
                logger.warning("xhs_ops: user sent same code as last time (%s), requesting new one", val)
                redis_execute("DEL", code_key)
                _send_text_to_user("这个验证码和上次一样，可能已经过期了。请查看手机上最新收到的验证码再回复一次")
                continue
            sms_code = val
            logger.info("xhs_ops: received SMS code: %s", sms_code)
            redis_execute("DEL", code_key)
            # 记录本次使用的验证码（5 分钟内防重复）
            redis_execute("SET", last_code_key, sms_code, "EX", 300)
            break

    redis_execute("DEL", pending_key)

    if not sms_code:
        logger.warning("xhs_ops: SMS code timeout after %ds", _SMS_CODE_TIMEOUT)
        _send_text_to_user("等待验证码超时，请重试")
        return False

    # ── 输入验证码 ──
    code_entered = False
    code_input_selectors = [
        'input[placeholder*="验证码"]',
        'input[placeholder*="code"]',
        'input[name*="code"]',
        'input[name*="verify"]',
        '[class*="verify-code"] input',
        '[class*="code-input"] input',
    ]
    for sel in code_input_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1000):
                await loc.click()
                await loc.fill("")
                await loc.type(sms_code, delay=30)
                # dispatch React events
                await page.evaluate("""(sel) => {
                    const el = document.querySelector(sel);
                    if (el) {
                        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ).set;
                        nativeInputValueSetter.call(el, el.value);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""", sel)
                code_entered = True
                logger.info("xhs_ops: entered SMS code via selector: %s", sel)
                break
        except Exception:
            continue

    if not code_entered:
        # 用 Tab 键从手机号输入框跳到验证码输入框
        try:
            await page.keyboard.press("Tab")
            await asyncio.sleep(0.3)
            await page.keyboard.type(sms_code, delay=50)
            code_entered = True
            logger.info("xhs_ops: entered SMS code via Tab key")
        except Exception as e:
            logger.warning("xhs_ops: Tab key code entry failed: %s", e)

    if not code_entered:
        logger.error("xhs_ops: could not find code input field")
        return False

    await asyncio.sleep(1)

    # ── 勾选协议/用户条款复选框（如果有的话）──
    try:
        agreed = await page.evaluate("""() => {
            // 找所有未勾选的 checkbox（可能是「同意协议」）
            const checkboxes = document.querySelectorAll(
                'input[type="checkbox"]:not(:checked), '
                + '[class*="checkbox"]:not([class*="checked"]), '
                + '[class*="agree"]:not([class*="checked"]), '
                + '[class*="protocol"]:not([class*="checked"])'
            );
            let clicked = 0;
            for (const cb of checkboxes) {
                // 只点击与协议/条款相关的，或在登录表单内的
                const parent = cb.closest('[class*="login"], [class*="Login"], form') || cb.parentElement;
                const text = (parent ? parent.textContent : '').toLowerCase();
                if (text.includes('协议') || text.includes('条款') || text.includes('同意')
                    || text.includes('agree') || text.includes('terms')
                    || cb.closest('form, [class*="login-form"]')) {
                    cb.click();
                    clicked++;
                }
            }
            return clicked;
        }""")
        if agreed:
            logger.info("xhs_ops: checked %d agreement checkbox(es)", agreed)
            await asyncio.sleep(0.5)
    except Exception as e:
        logger.debug("xhs_ops: agreement checkbox check: %s", e)

    # ── 验证输入框的值（确保 React state 已同步）──
    try:
        input_values = await page.evaluate("""() => {
            const inputs = document.querySelectorAll('input:not([type="hidden"]):not([type="checkbox"])');
            return Array.from(inputs).map(i => ({
                placeholder: i.placeholder || '',
                value: i.value || '',
                type: i.type || ''
            })).filter(i => i.value);
        }""")
        logger.info("xhs_ops: pre-login input values: %s", input_values)
    except Exception:
        pass

    # ── 点击登录按钮 ──
    login_clicked = False
    login_selectors = [
        # 优先匹配明确的登录按钮（避免匹配到导航栏的「登录」文字）
        '[class*="login-btn"] >> text=登录',
        '[class*="submit-btn"]',
        'button[type="submit"]',
        'button:has-text("登录")',
        '[class*="login-btn"]', '[class*="submit"]',
    ]
    for sel in login_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=800):
                await loc.click()
                login_clicked = True
                logger.info("xhs_ops: clicked login button: %s", sel)
                break
        except Exception:
            continue

    if not login_clicked:
        # JS 精确匹配：找 button 元素中文字为"登录"的
        try:
            clicked = await page.evaluate("""() => {
                const btns = document.querySelectorAll('button');
                for (const btn of btns) {
                    const text = (btn.textContent || '').trim();
                    if (text === '登录' && btn.offsetParent !== null) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }""")
            if clicked:
                login_clicked = True
                logger.info("xhs_ops: clicked login button via JS exact match")
        except Exception:
            pass

    if not login_clicked:
        # 尝试 Enter 键
        await page.keyboard.press("Enter")
        logger.info("xhs_ops: pressed Enter to submit login")

    # ── 等待登录结果（检测滑块验证码 + 登录成功 + 错误提示）──
    await asyncio.sleep(3)  # 多等一秒让网络请求完成

    # 自动解决滑块验证码（最多尝试 3 次）
    for attempt in range(3):
        solved = await _try_solve_slider_captcha(page)
        if not solved:
            break  # 没有检测到滑块，继续检查登录状态
        logger.info("xhs_ops: slider CAPTCHA solve attempt %d", attempt + 1)
        await asyncio.sleep(3)
        # 检查滑块是否还在
        still_has = await _detect_captcha(page)
        if not still_has:
            logger.info("xhs_ops: slider CAPTCHA solved on attempt %d", attempt + 1)
            break
    await asyncio.sleep(2)

    # ── 多层登录成功检测 ──
    # 不仅检查 URL，还检查 cookie 和页面元素
    cur_url = page.url
    logger.info("xhs_ops: post-login URL: %s", cur_url)

    # 检查 1：URL 已离开登录页
    if "/login" not in cur_url and "login" not in cur_url.split("/")[-1]:
        logger.info("xhs_ops: SMS login appears successful (left login page)")
        return True

    # 检查 2：web_session cookie 出现（登录成功的最可靠信号）
    has_session = await page.evaluate(
        "() => document.cookie.includes('web_session')"
    )
    if has_session:
        logger.info("xhs_ops: SMS login successful (web_session cookie present)")
        return True

    # 等一下再检查（SPA 可能需要时间渲染）
    await asyncio.sleep(4)
    cur_url = page.url
    if "/login" not in cur_url:
        return True

    has_session = await page.evaluate(
        "() => document.cookie.includes('web_session')"
    )
    if has_session:
        logger.info("xhs_ops: SMS login successful after wait (web_session cookie)")
        return True

    # 检查 3：尝试导航到发布页，看是否能访问
    try:
        await page.goto(
            "https://creator.xiaohongshu.com/publish/publish",
            wait_until="domcontentloaded", timeout=15000,
        )
        await asyncio.sleep(3)
        cur_url = page.url
        if "/login" not in cur_url:
            # 检查发布页元素
            _pub_selectors = [
                'input[type="file"]', '.upload-input',
                '[contenteditable="true"]', '.ql-editor',
                'div.d-input input', '.publish-page',
            ]
            for sel in _pub_selectors:
                try:
                    if await page.locator(sel).first.is_visible(timeout=1000):
                        logger.info("xhs_ops: SMS login successful (publish page accessible, found %s)", sel)
                        return True
                except Exception:
                    continue
    except Exception as e:
        logger.debug("xhs_ops: post-login publish page check failed: %s", e)

    # 检查是否有错误提示
    try:
        error_text = await page.evaluate("""() => {
            const errs = document.querySelectorAll('[class*="error"], [class*="tip"], .toast, [class*="msg"]');
            for (const e of errs) {
                const t = (e.textContent || '').trim();
                if (t && t.length > 2 && t.length < 100) return t;
            }
            return null;
        }""")
        if error_text:
            logger.warning("xhs_ops: SMS login error: %s", error_text)
            _send_text_to_user(f"登录遇到问题：{error_text}")
    except Exception:
        pass

    # 最后一搏：截个图看看当前页面状态（打日志辅助调试）
    try:
        ss = await page.screenshot(type="png", full_page=False)
        desc = await _analyze_screenshot(ss, (
            "这是小红书创作者平台的页面。请描述：\n"
            "1. 当前是登录页还是已经登录了？\n"
            "2. 页面上有没有错误提示？滑块验证码？\n"
            "3. 有没有发布笔记的上传图片/编辑器等元素？\n"
            "简短回复。"
        ))
        logger.info("xhs_ops: post-SMS-login page analysis: %s", desc[:300])
    except Exception:
        pass

    logger.warning("xhs_ops: SMS login did not redirect from login page")
    return False


async def _check_login_success(page) -> bool:
    """检查小红书页面是否已经登录成功。

    使用与 xiaohongshu-mcp (Go) 和 RedNote-MCP (TypeScript) 相同的检测方式：
    侧边栏 `.user.side-bar-component .channel` 元素存在且文本为「我」。
    这是两个主流开源项目验证过的最可靠选择器。

    ⚠️ 检测必须严格——XHS /explore 页面未登录也有公开 feeds 和类 avatar 元素，
    宽松检测会导致误判"已登录"，然后搜索结果不可用（链接打不开）。

    多层降级：精确 selector → __INITIAL_STATE__.user → cookie 检测
    """
    try:
        result = await page.evaluate("""() => {
            try {
                // 还在登录页 → 未成功
                if (window.location.href.includes('/login')) return false;

                // ── 第一优先：精确 selector（xiaohongshu-mcp + RedNote-MCP 通用）──
                const channelEl = document.querySelector('.user.side-bar-component .channel');
                if (channelEl) {
                    const text = (channelEl.textContent || '').trim();
                    if (text === '我') return true;
                }

                // ── 第二优先：__INITIAL_STATE__ 中有明确的用户信息 ──
                // 注意：只检查 user.userId，不检查 search.feeds
                //（/explore 未登录也有公开 feeds，不能用来判断登录态）
                const state = window.__INITIAL_STATE__;
                if (state && state.user) {
                    const user = state.user._rawValue || state.user._value || state.user;
                    if (user && user.userId) return true;
                }

                // ── 第三优先：cookie 检测 ──
                // 小红书登录后会设置 web_session cookie
                const cookies = document.cookie || '';
                if (cookies.includes('web_session')) return true;

                // ── 不再用 avatar 检测 ──
                // 公开页面也有类 avatar 元素，容易误判

                return false;
            } catch(e) {
                return false;
            }
        }""")
        return bool(result)
    except Exception:
        return False


# ── 浏览器会话管理 ──

def _check_playwright() -> str | None:
    """检查 Playwright 是否可用"""
    try:
        import playwright  # noqa: F401
        return None
    except ImportError:
        return (
            "浏览器引擎 (Playwright) 未安装。请让管理员执行：\n"
            "  pip install playwright && playwright install --with-deps chromium"
        )


async def _cleanup_xhs_session(session_key: str) -> None:
    """清理 XHS 浏览器会话"""
    session = _xhs_sessions.pop(session_key, None)
    if session is None:
        return
    try:
        # 关闭 creator context（如果存在）
        if session.creator_context:
            try:
                await session.creator_context.close()
            except Exception:
                pass
        # 保存最新 Cookie 再关闭
        if session.context:
            try:
                cookies = await session.context.cookies()
                if cookies:
                    await _save_cookies_to_redis(session_key, cookies)
            except Exception:
                pass
            await session.context.close()
        if session.browser:
            await session.browser.close()
        if session.playwright:
            await session.playwright.stop()
    except Exception as e:
        logger.warning("xhs_ops: cleanup error for %s: %s", session_key, e)


async def _cleanup_stale_sessions() -> None:
    """清理超时的 XHS 会话"""
    now = time.time()
    stale = [
        tid for tid, s in _xhs_sessions.items()
        if now - s.last_used > _SESSION_TIMEOUT
    ]
    for tid in stale:
        logger.info("xhs_ops: auto-closing stale session for %s", tid)
        await _cleanup_xhs_session(tid)


async def _get_or_create_xhs_session(session_key: str) -> _XhsSession:
    """获取或创建 XHS 专用浏览器会话（per-user 隔离）"""
    await _cleanup_stale_sessions()

    # 复用现有会话
    if session_key in _xhs_sessions:
        session = _xhs_sessions[session_key]
        session.last_used = time.time()
        try:
            if session.browser.is_connected():
                return session
        except Exception:
            pass
        await _cleanup_xhs_session(session_key)

    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )

    # 创建 context 时注入 Cookie（如果 Redis 中有保存的）
    saved_cookies = await _load_cookies_from_redis(session_key)

    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    )

    # 注入 Cookie
    if saved_cookies:
        try:
            await context.add_cookies(saved_cookies)
            logger.info("xhs_ops: restored %d cookies for %s", len(saved_cookies), session_key)
        except Exception as e:
            logger.warning("xhs_ops: failed to restore cookies: %s", e)

    page = await context.new_page()

    # 注入反检测脚本
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    """)

    session = _XhsSession(
        playwright=pw,
        browser=browser,
        context=context,
        page=page,
        tenant_id=session_key,
        logged_in=bool(saved_cookies),
        last_used=time.time(),
    )
    _xhs_sessions[session_key] = session
    return session


async def _take_screenshot_and_analyze(page: Any, prompt: str) -> tuple[str, str]:
    """截图 + Gemini 分析，返回 (analysis, page_text)"""
    await asyncio.sleep(1)  # 等页面渲染
    screenshot = await page.screenshot(type="png", full_page=False)
    analysis = await _analyze_screenshot(screenshot, prompt)

    try:
        page_text = await page.inner_text("body")
        page_text = page_text[:_MAX_TEXT]
    except Exception:
        page_text = ""

    return analysis, page_text


# ── 工具实现 ──

async def _handle_xhs_login(args: dict) -> ToolResult:
    """打开小红书登录页，发送二维码截图给用户，轮询等待扫码完成。

    流程（参考 xiaohongshu-mcp 的 login 流程）：
    1. 打开小红书首页，检查是否已登录
    2. 未登录则导航到登录页，截取二维码截图
    3. 通过企微 API 将二维码图片发送给用户
    4. 轮询等待用户扫码（最多 2 分钟）
    5. 扫码成功后保存 Cookie 到 Redis
    """
    err = _check_playwright()
    if err:
        return ToolResult.error(err)

    tenant_id = _get_tenant_id()
    if not tenant_id:
        return ToolResult.error("无法获取当前租户信息")
    session_key = _get_session_key() or tenant_id

    try:
        # ── 先尝试用 Redis 中保存的 cookie 恢复登录态 ──
        # 不要一上来就清 cookie！重启后 Redis 里的 cookie 可能仍然有效
        if session_key in _xhs_sessions:
            await _cleanup_xhs_session(session_key)

        session = await _get_or_create_xhs_session(session_key)
        page = session.page

        # 1. 先去首页看看是否已登录（cookie 恢复）
        await page.goto(f"{_XHS_BASE}/explore", wait_until="domcontentloaded", timeout=30000)
        # 多等一会儿，SPA 首次加载 cookie 后渲染侧边栏需要时间
        await asyncio.sleep(5)

        # 重试检查（SPA 渲染可能延迟）
        login_ok = await _check_login_success(page)
        if not login_ok:
            await asyncio.sleep(3)
            login_ok = await _check_login_success(page)

        if login_ok:
            session.logged_in = True
            cookies = await session.context.cookies()
            await _save_cookies_to_redis(session_key, cookies)
            logger.info("xhs_ops: already logged in for %s (cookie restored)", session_key)
            return ToolResult.success(
                "小红书已登录，无需重新登录。Cookie 已保存。\n"
                "可以直接使用 xhs_search 搜索。"
            )

        # Cookie 恢复失败，清掉过期 cookie 再走 QR 登录流程
        logger.info("xhs_ops: cookie restore failed for %s, clearing and starting fresh", session_key)
        await _clear_cookies_from_redis(session_key)

        # 2. 未登录 → 触发登录弹窗
        # xiaohongshu-mcp 和 RedNote-MCP 都是访问 /explore，XHS 自动弹出登录对话框
        # 如果已经在 /explore 页面（步骤 1 就去了），登录弹窗应该已经出现
        # 如果没有弹窗，尝试导航到 /login
        logger.info("xhs_ops: waiting for login dialog for %s", tenant_id)

        # 等待登录容器出现（/explore 页面会自动弹出）
        login_dialog_found = False
        try:
            await page.wait_for_selector(
                '.login-container, [class*="login-modal"], [class*="LoginModal"]',
                timeout=8000,
            )
            login_dialog_found = True
        except Exception:
            # /explore 没弹出登录框，尝试 /login 页面
            logger.info("xhs_ops: no login dialog on /explore, navigating to /login")
            await page.goto(f"{_XHS_BASE}/login", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

        # 等待二维码图片出现
        # xiaohongshu-mcp 用 .login-container .qrcode-img（src 是 base64 PNG）
        try:
            await page.wait_for_selector(
                '.qrcode-img, [class*="qrcode"] img, canvas',
                timeout=10000,
            )
            await asyncio.sleep(1)  # 等待二维码完全渲染
        except Exception:
            logger.warning("xhs_ops: QR code selector not found, taking screenshot anyway")
            await asyncio.sleep(2)

        # 3. 提取二维码图片
        # 优先从 DOM 获取 base64 QR（更清晰），遍历所有候选图片选最大的
        # 坑：.login-container img 会匹配到小红书 logo（~3KB），不是 QR 码
        qr_screenshot = None
        try:
            qr_base64 = await page.evaluate("""() => {
                // 策略1: 精确匹配 .qrcode-img
                const qrImg = document.querySelector('.qrcode-img');
                if (qrImg && qrImg.src && qrImg.src.startsWith('data:image')) {
                    return qrImg.src;
                }

                // 策略2: 找 qrcode 相关容器内的 img
                const qrcodeContainers = document.querySelectorAll('[class*="qrcode"], [class*="QRCode"], [class*="qr-code"]');
                for (const container of qrcodeContainers) {
                    const img = container.querySelector('img[src*="data:image"]');
                    if (img) return img.src;
                }

                // 策略3: canvas 元素（有些网站用 canvas 画 QR 码）
                const canvases = document.querySelectorAll('.login-container canvas, [class*="qrcode"] canvas, [class*="login"] canvas');
                for (const canvas of canvases) {
                    try {
                        const dataUrl = canvas.toDataURL('image/png');
                        if (dataUrl && dataUrl.length > 1000) return dataUrl;
                    } catch(e) {}
                }

                // 策略4: 遍历登录区域所有 base64 图片，选最大的（QR 码通常比 logo 大）
                const allImgs = document.querySelectorAll('.login-container img[src*="data:image"], [class*="login"] img[src*="data:image"]');
                let bestImg = null;
                let bestLen = 0;
                for (const img of allImgs) {
                    if (img.src && img.src.length > bestLen) {
                        bestLen = img.src.length;
                        bestImg = img;
                    }
                }
                // 只返回足够大的图片（QR 码 base64 通常 > 5000 字符，logo < 5000）
                if (bestImg && bestLen > 5000) return bestImg.src;

                return null;
            }""")
            if qr_base64 and qr_base64.startswith("data:image"):
                # 解码 base64 为 PNG bytes
                import base64
                # 去掉 "data:image/png;base64," 前缀
                b64_data = qr_base64.split(",", 1)[1] if "," in qr_base64 else qr_base64
                qr_screenshot = base64.b64decode(b64_data)
                logger.info("xhs_ops: extracted QR code from DOM (base64, %d bytes)", len(qr_screenshot))
            else:
                logger.warning("xhs_ops: no valid QR base64 found in DOM (got %s chars)",
                               len(qr_base64) if qr_base64 else 0)
        except Exception as e:
            logger.debug("xhs_ops: base64 QR extraction failed: %s", e)

        if not qr_screenshot:
            # fallback: 全页截图（用户至少能看到屏幕上的二维码）
            qr_screenshot = await page.screenshot(type="png", full_page=False)
            logger.info("xhs_ops: using full page screenshot as QR image (%d bytes)", len(qr_screenshot))

        # 4. 发送二维码图片给用户
        sent = _send_qr_image_to_user(qr_screenshot)
        if sent:
            logger.info("xhs_ops: QR code image sent to user for %s", tenant_id)
            qr_delivery_msg = (
                "✅ 二维码已发送到聊天窗口，请用小红书 App 扫码登录。\n"
                "⚠️ 风险提示：小红书可能对异常登录进行风控，建议使用小号扫码，避免主号受影响。"
            )
        else:
            logger.warning("xhs_ops: failed to send QR image, user must scan from description")
            # 用 Gemini 分析截图作为 fallback
            analysis, _ = await _take_screenshot_and_analyze(page, _LOGIN_QR_PROMPT)
            qr_delivery_msg = (
                "⚠️ 无法直接发送二维码图片。\n"
                f"页面分析：{analysis}\n"
                "请让管理员检查企微 API 配置。"
            )

        # 5. 轮询等待用户扫码
        logger.info("xhs_ops: polling for login success (timeout=%ds)", _LOGIN_POLL_TIMEOUT)
        start_time = time.time()
        poll_count = 0

        while time.time() - start_time < _LOGIN_POLL_TIMEOUT:
            await asyncio.sleep(_LOGIN_POLL_INTERVAL)
            poll_count += 1

            # 检查页面是否发生变化（扫码后会自动跳转）
            current_url = page.url
            if "/login" not in current_url:
                # 页面已跳转离开登录页 → 可能登录成功
                await asyncio.sleep(2)  # 等页面加载完
                if await _check_login_success(page):
                    break

            # 即使还在登录页，也检查一下（小红书可能不跳转而是刷新）
            if await _check_login_success(page):
                break

            # 每 32 秒检查一次二维码是否过期（页面可能显示"二维码已过期"）
            if poll_count % 8 == 0:
                try:
                    expired = await page.evaluate("""() => {
                        const text = document.body ? document.body.innerText : '';
                        return text.includes('过期') || text.includes('刷新') || text.includes('expired');
                    }""")
                    if expired:
                        logger.info("xhs_ops: QR code expired, refreshing page")
                        # 刷新当前页面以获取新二维码
                        await page.reload(wait_until="domcontentloaded", timeout=30000)
                        await asyncio.sleep(3)
                        # 重新截图发送
                        new_screenshot = await page.screenshot(type="png", full_page=False)
                        _send_qr_image_to_user(new_screenshot)
                except Exception:
                    pass

        # 6. 检查最终结果
        elapsed = int(time.time() - start_time)

        if await _check_login_success(page):
            session.logged_in = True
            cookies = await session.context.cookies()
            await _save_cookies_to_redis(session_key, cookies)
            logger.info("xhs_ops: login success for %s after %ds (%d polls)", session_key, elapsed, poll_count)
            return ToolResult.success(
                f"🎉 小红书登录成功！（等待 {elapsed} 秒）\n"
                "Cookie 已保存，后续搜索将使用登录态。\n"
                "现在可以使用 xhs_search 搜索小红书内容了。"
            )
        else:
            logger.warning("xhs_ops: login timeout for %s after %ds", session_key, elapsed)
            return ToolResult.success(
                f"{qr_delivery_msg}\n\n"
                f"⏰ 等待超时（{elapsed} 秒）。用户可能还没扫码。\n"
                "可以让用户扫码后，再调用 xhs_check_login 确认登录状态。\n"
                "二维码过期后需要重新调用 xhs_login。"
            )

    except Exception as e:
        logger.exception("xhs_ops: login failed")
        return ToolResult.error(f"登录失败: {e}")


async def _handle_xhs_check_login(args: dict) -> ToolResult:
    """检查小红书登录状态（通过 DOM/JS 检测，不依赖 Gemini Vision）"""
    err = _check_playwright()
    if err:
        return ToolResult.error(err)

    tenant_id = _get_tenant_id()
    if not tenant_id:
        return ToolResult.error("无法获取当前租户信息")
    session_key = _get_session_key() or tenant_id

    # 没有活跃会话时，创建一个（会自动从 Redis 加载 cookie）
    session = _xhs_sessions.get(session_key)
    if not session:
        session = await _get_or_create_xhs_session(session_key)

    session.last_used = time.time()

    try:
        page = session.page
        current_url = page.url

        # 必须在 /explore 页面才能检查侧边栏
        navigated = False
        if "xiaohongshu.com/explore" not in current_url:
            await page.goto(f"{_XHS_BASE}/explore", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(4)
            navigated = True

        # 检查登录态，SPA 首次加载可能需要额外等待
        login_ok = await _check_login_success(page)
        if not login_ok and navigated:
            # 首次导航，DOM 可能还没渲染完，多等一下重试
            await asyncio.sleep(3)
            login_ok = await _check_login_success(page)

        if login_ok:
            session.logged_in = True
            cookies = await session.context.cookies()
            await _save_cookies_to_redis(session_key, cookies)

            # 尝试获取用户昵称
            nickname = ""
            try:
                nickname = await page.evaluate("""() => {
                    const state = window.__INITIAL_STATE__;
                    if (state && state.user) {
                        const u = state.user._rawValue || state.user._value || state.user;
                        return u.nickname || u.nickName || '';
                    }
                    return '';
                }""")
            except Exception:
                pass

            name_str = f"（{nickname}）" if nickname else ""
            return ToolResult.success(
                f"✅ 小红书已登录{name_str}。Cookie 有效。\n"
                "可以直接使用 xhs_search 搜索。"
            )
        else:
            session.logged_in = False
            has_cookies = await _load_cookies_from_redis(session_key)
            if has_cookies:
                return ToolResult.success(
                    "❌ 小红书未登录（Cookie 已过期）。\n"
                    "请调用 xhs_login 重新扫码登录。"
                )
            else:
                return ToolResult.success(
                    "❌ 小红书未登录（从未登录过）。\n"
                    "请调用 xhs_login 扫码登录。"
                )

    except Exception as e:
        return ToolResult.error(f"检查登录状态失败: {e}")


async def _handle_xhs_search(args: dict) -> ToolResult:
    """搜索小红书笔记/用户"""
    err = _check_playwright()
    if err:
        return ToolResult.error(err)

    keyword = args.get("keyword", "").strip()
    if not keyword:
        return ToolResult.invalid_param("keyword 不能为空")

    search_type = args.get("search_type", "note")  # note / user
    sort = args.get("sort", "general")  # general / time_descending / popularity_descending
    is_retry = args.get("_retry_after_login", False)

    tenant_id = _get_tenant_id()
    if not tenant_id:
        return ToolResult.error("无法获取当前租户信息")
    session_key = _get_session_key() or tenant_id

    try:
        return await asyncio.wait_for(
            _xhs_search_impl(session_key, keyword, search_type, sort, is_retry=is_retry),
            timeout=_SEARCH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("xhs_ops: search '%s' timed out after %ds", keyword, _SEARCH_TIMEOUT)
        return ToolResult.error(
            f"小红书搜索「{keyword}」超时（>{_SEARCH_TIMEOUT}秒），可能原因：\n"
            f"1. 小红书需要登录才能搜索（请先用 xhs_login 登录）\n"
            f"2. 网络连接慢\n"
            f"建议：先用 search_social_media 工具通过 API 搜索，或换用 web_search 搜索。"
        )
    except Exception as e:
        logger.exception("xhs_ops: search failed")
        return ToolResult.error(f"搜索失败: {e}")


async def _xhs_search_impl(
    session_key: str, keyword: str, search_type: str, sort: str,
    *, is_retry: bool = False,
) -> ToolResult:
    """xhs_search 的核心实现（被 wait_for 包裹做超时控制）。"""
    try:
        session = await _get_or_create_xhs_session(session_key)
        page = session.page

        # 构造搜索 URL
        if search_type == "user":
            url = f"{_XHS_BASE}/search_result?keyword={keyword}&type=user"
        else:
            url = f"{_XHS_BASE}/search_result?keyword={keyword}&sort={sort}"

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)  # 等待搜索结果加载

        # 滚动一次加载更多内容
        await page.evaluate("window.scrollBy(0, 500)")
        await asyncio.sleep(1)

        type_label = "用户" if search_type == "user" else "笔记"
        extract_prompt = (
            f"这是小红书搜索「{keyword}」的{type_label}结果页面。\n"
            f"请提取搜索结果列表，每条包括：\n"
        )
        if search_type == "user":
            extract_prompt += (
                "- 用户昵称\n- 小红书号（如果可见）\n- 粉丝数\n- 简介\n"
                "请用以下格式列出每个用户：\n"
                "用户: 昵称 | 粉丝: xxx | 简介: xxx\n"
            )
        else:
            extract_prompt += (
                "- 笔记标题\n- 作者昵称\n- 点赞数\n"
                "请用以下格式列出每条笔记：\n"
                "笔记: 标题 | 作者: xxx | 点赞: xxx\n"
            )
        extract_prompt += "尽量提取所有可见结果，数据要准确。"

        # 优先从 __INITIAL_STATE__ 提取结构化数据（比截图更准确）
        is_user = search_type == "user"

        if is_user:
            # ── 用户搜索：提取用户列表 ──
            state_users = await _extract_search_users_from_state(page, 20)
            if state_users:
                from app.tools.source_registry import register_urls

                links_section = "\n── 用户搜索结果（从页面数据提取）──\n"
                all_urls = [page.url]
                for i, u in enumerate(state_users, 1):
                    uid = u.get("user_id", "")
                    profile_url = f"{_XHS_BASE}/user/profile/{uid}" if uid else ""
                    if profile_url:
                        all_urls.append(profile_url)
                    nickname = u.get("nickname", "?")
                    fans = u.get("fans", "")
                    fans_str = f" | 粉丝: {fans}" if fans else ""
                    desc = u.get("desc", "")
                    desc_str = f" | {desc[:50]}" if desc else ""
                    red_id = u.get("red_id", "")
                    red_id_str = f" | 小红书号: {red_id}" if red_id else ""
                    links_section += f"[{i}] {nickname}{fans_str}{red_id_str}{desc_str}\n"
                    if profile_url:
                        links_section += f"    主页: {profile_url}\n"

                register_urls(all_urls)

                analysis, _ = await _take_screenshot_and_analyze(page, extract_prompt)

                return ToolResult.success(
                    f"小红书搜索「{keyword}」({type_label})结果：\n\n"
                    f"{links_section}\n"
                    f"── AI 视觉补充 ──\n{analysis}\n\n"
                    f"搜索链接: {page.url}\n\n"
                    f"重要：上面的用户数据从页面内部数据提取，主页链接可直接分享。\n"
                    f"注意：不要自己编造或猜测小红书链接和账号 ID，只使用上面提取到的真实数据。"
                )
        else:
            # ── 笔记搜索：提取笔记列表 ──
            state_feeds = await _extract_search_feeds_from_state(page, 20)
            if state_feeds:
                from app.tools.source_registry import register_urls

                links_section = "\n── 搜索结果（从页面数据提取，链接可直接分享）──\n"
                all_urls = [page.url]
                for i, feed in enumerate(state_feeds, 1):
                    note_url = f"{_XHS_BASE}/explore/{feed['id']}"
                    if feed.get("xsec_token"):
                        note_url += f"?xsec_token={feed['xsec_token']}"
                    all_urls.append(note_url)
                    likes = feed.get("likes", "")
                    likes_str = f" | 点赞: {likes}" if likes else ""
                    links_section += f"[{i}] {feed.get('author', '?')}: {feed.get('title', '无标题')}{likes_str}\n    链接: {note_url}\n"

                register_urls(all_urls)

                analysis, _ = await _take_screenshot_and_analyze(page, extract_prompt)

                return ToolResult.success(
                    f"小红书搜索「{keyword}」({type_label})结果：\n\n"
                    f"{links_section}\n"
                    f"── AI 视觉补充 ──\n{analysis}\n\n"
                    f"搜索链接: {page.url}\n\n"
                    f"重要：上面的链接从页面内部数据提取，可直接分享给用户。\n"
                    f"注意：不要自己编造或猜测小红书链接和账号 ID，只使用上面提取到的真实数据。"
                )

        # fallback：__INITIAL_STATE__ 不可用，走 CSS selector + Vision
        analysis, page_text = await _take_screenshot_and_analyze(page, extract_prompt)

        dom_links = await _extract_search_links_from_dom(page, is_user, 20)

        from app.tools.source_registry import register_urls
        all_urls = [page.url] + dom_links
        register_urls(all_urls)

        links_section = ""
        if dom_links:
            links_section = "\n── 真实链接（从页面提取）──\n"
            for i, link in enumerate(dom_links, 1):
                links_section += f"[{i}] {link}\n"

        # 反幻觉 + 登录检测：无数据时判断是否被登录墙拦截
        no_link_warning = ""
        if not dom_links:
            # 检测是否是登录墙拦截
            is_login_wall = await _detect_login_wall(page)
            if is_login_wall:
                logger.warning("xhs_ops: search '%s' blocked by login wall", keyword)
                # Cookie 可能已过期，清除以避免下次重复加载无效 cookie
                await _clear_cookies_from_redis(session_key)
                session.logged_in = False

                if is_retry:
                    # 已经是登录后重试了，仍然被拦截 → 不再循环
                    return ToolResult.success(
                        f"小红书搜索「{keyword}」仍被登录墙拦截。\n"
                        "可能是登录状态未正确同步。请稍后重新调用 xhs_login 再试。"
                    )

                # ── 自动触发登录流程：直接发二维码，不依赖 LLM 二次决策 ──
                logger.info("xhs_ops: auto-triggering login from xhs_search for %s", tenant_id)
                login_result = await _handle_xhs_login({})
                login_msg = login_result.content if hasattr(login_result, "content") else str(login_result)

                # 判断登录是否成功（登录成功 → 自动重试搜索）
                if "登录成功" in login_msg:
                    logger.info("xhs_ops: login succeeded, retrying search '%s'", keyword)
                    # 重新搜索（递归一次，用新登录的 session）
                    retry_args = dict(args)
                    retry_args["_retry_after_login"] = True
                    return await _handle_xhs_search(retry_args)
                else:
                    # 登录未完成（超时/用户没扫码），返回状态
                    return ToolResult.success(
                        f"小红书搜索「{keyword}」需要登录。\n\n"
                        f"{login_msg}\n\n"
                        f"用户扫码登录后，再次搜索即可。"
                    )

            no_link_warning = (
                "\n⚠️ 警告：未能从页面提取到任何真实链接。"
                "不要编造或猜测小红书链接和用户 ID——告诉用户你只能提供搜索到的文字信息，"
                "建议用户自行在小红书 App 搜索上述关键词。\n"
            )
            logger.warning("xhs_ops: search '%s' returned 0 links — potential data quality issue", keyword)
            from app.services.error_log import record_error
            record_error(
                "data_quality",
                f"xhs_search returned 0 links for '{keyword}' (search_type={search_type})",
                detail=f"URL: {page.url}\n__INITIAL_STATE__ also empty. CSS selectors matched nothing.\n"
                       f"Vision analysis returned text but no extractable URLs.",
                tool_name="xhs_search",
                tool_args={"keyword": keyword, "search_type": search_type},
            )

        return ToolResult.success(
            f"小红书搜索「{keyword}」({type_label})结果：\n\n"
            f"── AI 提取的结构化数据 ──\n{analysis}\n\n"
            f"{links_section}{no_link_warning}\n"
            f"搜索链接: {page.url}\n\n"
            f"重要：只使用上面提取到的真实链接，不要自己编造小红书链接或用户 ID。"
        )

    except Exception as e:
        logger.exception("xhs_ops: search failed")
        return ToolResult.error(f"搜索失败: {e}")


async def _handle_xhs_get_note(args: dict) -> ToolResult:
    """获取小红书笔记详情"""
    err = _check_playwright()
    if err:
        return ToolResult.error(err)

    note_id = args.get("note_id", "").strip()
    note_url = args.get("url", "").strip()

    if not note_id and not note_url:
        return ToolResult.invalid_param("需要提供 note_id 或 url")

    # 处理各种 URL 格式：
    # - /discovery/item/{id}?secondshare=... (微信分享链接)
    # - /explore/{id} (标准链接)
    # - 纯 ID
    if note_url:
        import re as _re
        m = _re.search(r'/(?:discovery/item|explore)/([a-f0-9]+)', note_url)
        if m:
            note_id = note_id or m.group(1)
            # 统一转为 /explore/ 格式（去掉分享参数）
            note_url = f"{_XHS_BASE}/explore/{m.group(1)}"
    if note_id and not note_url:
        note_url = f"{_XHS_BASE}/explore/{note_id}"

    tenant_id = _get_tenant_id()
    if not tenant_id:
        return ToolResult.error("无法获取当前租户信息")
    session_key = _get_session_key() or tenant_id

    try:
        session = await _get_or_create_xhs_session(session_key)
        page = session.page

        await page.goto(note_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # 滚动加载评论
        await page.evaluate("window.scrollBy(0, 800)")
        await asyncio.sleep(1)

        extract_prompt = (
            "这是一条小红书笔记的详情页。请提取：\n"
            "1. 标题\n"
            "2. 作者昵称\n"
            "3. 发布时间\n"
            "4. 正文内容（完整）\n"
            "5. 标签/话题\n"
            "6. 互动数据：点赞数、收藏数、评论数\n"
            "7. 前几条热门评论（昵称+内容+点赞数）\n"
            "请结构化输出，数据要准确。"
        )

        analysis, page_text = await _take_screenshot_and_analyze(page, extract_prompt)

        from app.tools.source_registry import register_urls
        register_urls([note_url])

        return ToolResult.success(
            f"笔记详情：\n\n"
            f"── AI 提取的数据 ──\n{analysis}\n\n"
            f"链接: {note_url}"
        )

    except Exception as e:
        logger.exception("xhs_ops: get_note failed")
        return ToolResult.error(f"获取笔记详情失败: {e}")


async def _handle_xhs_get_user(args: dict) -> ToolResult:
    """获取小红书用户主页"""
    err = _check_playwright()
    if err:
        return ToolResult.error(err)

    user_id = args.get("user_id", "").strip()
    user_url = args.get("url", "").strip()

    if not user_id and not user_url:
        return ToolResult.invalid_param("需要提供 user_id 或 url")

    if user_id and not user_url:
        user_url = f"{_XHS_BASE}/user/profile/{user_id}"

    tenant_id = _get_tenant_id()
    if not tenant_id:
        return ToolResult.error("无法获取当前租户信息")
    session_key = _get_session_key() or tenant_id

    try:
        session = await _get_or_create_xhs_session(session_key)
        page = session.page

        await page.goto(user_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        extract_prompt = (
            "这是一个小红书用户的主页。请提取：\n"
            "1. 昵称\n"
            "2. 小红书号\n"
            "3. 简介/签名\n"
            "4. 关注数、粉丝数、获赞与收藏数\n"
            "5. 最近发布的笔记列表（标题+点赞数，尽量多提取）\n"
            "请结构化输出，数字要准确。"
        )

        analysis, _ = await _take_screenshot_and_analyze(page, extract_prompt)

        from app.tools.source_registry import register_urls
        register_urls([user_url])

        return ToolResult.success(
            f"用户主页：\n\n"
            f"── AI 提取的数据 ──\n{analysis}\n\n"
            f"链接: {user_url}"
        )

    except Exception as e:
        logger.exception("xhs_ops: get_user failed")
        return ToolResult.error(f"获取用户主页失败: {e}")


# ── 图片自动生成（文字卡片 + Gemini 配图）──


_TEXT_CARD_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    width: 1080px; height: 1440px;
    background: linear-gradient(135deg, {bg1} 0%, {bg2} 100%);
    display: flex; align-items: center; justify-content: center;
    font-family: "PingFang SC", "Noto Sans SC", "Microsoft YaHei", sans-serif;
}}
.card {{
    width: 920px; min-height: 600px; max-height: 1300px;
    background: rgba(255,255,255,0.95);
    border-radius: 24px; padding: 80px 64px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.08);
    display: flex; flex-direction: column; justify-content: center;
}}
.title {{
    font-size: 48px; font-weight: 700; color: #1a1a1a;
    margin-bottom: 40px; line-height: 1.4;
    border-bottom: 3px solid {accent};
    padding-bottom: 24px;
}}
.content {{
    font-size: 32px; color: #333; line-height: 1.8;
    white-space: pre-wrap; word-break: break-word;
}}
.footer {{
    margin-top: auto; padding-top: 40px;
    font-size: 24px; color: #999; text-align: right;
}}
</style>
</head>
<body>
<div class="card">
    <div class="title">{title}</div>
    <div class="content">{content}</div>
    <div class="footer">{footer}</div>
</div>
</body>
</html>
"""

# 预设配色方案（渐变背景 + 强调色）
_COLOR_SCHEMES = [
    {"bg1": "#667eea", "bg2": "#764ba2", "accent": "#667eea"},  # 紫蓝
    {"bg1": "#f093fb", "bg2": "#f5576c", "accent": "#f5576c"},  # 粉红
    {"bg1": "#4facfe", "bg2": "#00f2fe", "accent": "#4facfe"},  # 蓝青
    {"bg1": "#43e97b", "bg2": "#38f9d7", "accent": "#43e97b"},  # 绿
    {"bg1": "#fa709a", "bg2": "#fee140", "accent": "#fa709a"},  # 粉黄
    {"bg1": "#a18cd1", "bg2": "#fbc2eb", "accent": "#a18cd1"},  # 淡紫
    {"bg1": "#fccb90", "bg2": "#d57eeb", "accent": "#d57eeb"},  # 橙紫
    {"bg1": "#e0c3fc", "bg2": "#8ec5fc", "accent": "#8ec5fc"},  # 淡蓝紫
]


async def _generate_text_card_image(
    title: str, content: str, session: "_XhsSession",
    footer: str = "",
) -> str | None:
    """用 Playwright 渲染 HTML 文字卡片，截图保存为临时文件。

    返回临时文件路径，失败返回 None。
    """
    import tempfile
    import hashlib

    try:
        # 根据标题 hash 选配色（同一标题总是同一配色）
        scheme_idx = int(hashlib.md5(title.encode()).hexdigest(), 16) % len(_COLOR_SCHEMES)
        scheme = _COLOR_SCHEMES[scheme_idx]

        # 正文太长时截断（卡片空间有限）
        display_content = content
        if len(display_content) > 300:
            display_content = display_content[:297] + "..."

        html = _TEXT_CARD_HTML_TEMPLATE.format(
            title=title.replace("<", "&lt;").replace(">", "&gt;"),
            content=display_content.replace("<", "&lt;").replace(">", "&gt;"),
            footer=footer.replace("<", "&lt;").replace(">", "&gt;"),
            **scheme,
        )

        # 用现有 session 的 browser 创建临时页面渲染
        browser = session.context.browser
        if not browser:
            logger.warning("xhs_ops: no browser in session for text card")
            return None

        tmp_page = await browser.new_page(viewport={"width": 1080, "height": 1440})
        try:
            await tmp_page.set_content(html, wait_until="load")
            await asyncio.sleep(0.5)  # 等字体渲染

            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            await tmp_page.screenshot(path=tmp.name, type="png")
            logger.info("xhs_ops: text card generated: %s", tmp.name)
            return tmp.name
        finally:
            await tmp_page.close()

    except Exception as e:
        logger.warning("xhs_ops: text card generation failed: %s", e)
        return None


async def _generate_text_card_pages(
    title: str, content: str, session: "_XhsSession",
    footer: str = "", max_chars_per_page: int = 250,
) -> list[str]:
    """将长文本拆分成多张卡片图片（小红书图文最多 18 张）。

    短文本（<= max_chars_per_page）生成 1 张，长文本自动分页。
    返回临时文件路径列表。
    """
    if len(content) <= max_chars_per_page:
        path = await _generate_text_card_image(title, content, session, footer)
        return [path] if path else []

    # 按段落分页，尽量不拆段落
    paragraphs = content.split("\n")
    pages: list[str] = []
    current_page = ""
    page_num = 0

    for para in paragraphs:
        if len(current_page) + len(para) + 1 > max_chars_per_page and current_page:
            page_num += 1
            page_title = f"{title}（{page_num}）" if page_num > 1 else title
            path = await _generate_text_card_image(
                page_title, current_page.strip(), session, footer,
            )
            if path:
                pages.append(path)
            current_page = para + "\n"
            if len(pages) >= 17:  # 留 1 张给封面/配图
                break
        else:
            current_page += para + "\n"

    # 最后一页
    if current_page.strip() and len(pages) < 18:
        page_num += 1
        page_title = f"{title}（{page_num}）" if page_num > 1 else title
        path = await _generate_text_card_image(
            page_title, current_page.strip(), session, footer,
        )
        if path:
            pages.append(path)

    return pages


async def _generate_gemini_image(prompt: str) -> str | None:
    """调用 Gemini 生图 API 生成配图，保存为临时文件。

    使用 Gemini native image generation (response_modalities=["IMAGE"])。
    返回临时文件路径，失败返回 None。
    """
    import tempfile

    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        logger.warning("xhs_ops: google-genai not installed, cannot generate image")
        return None

    tenant = _get_tenant_or_none()
    if not tenant or not tenant.llm_api_key:
        logger.warning("xhs_ops: no Gemini API key for image generation")
        return None

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

    try:
        # gemini-2.5-flash-image: 快速生图，适合批量场景
        # gemini-3-pro-image-preview: 高质量生图，适合专业素材
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash-image",
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
                temperature=1.0,
            ),
        )

        # 从 response 中提取图片
        if not response.candidates:
            logger.warning("xhs_ops: Gemini image gen returned no candidates")
            return None

        for part in response.candidates[0].content.parts:
            if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                ext = ".png" if "png" in part.inline_data.mime_type else ".jpg"
                tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
                tmp.write(part.inline_data.data)
                tmp.flush()
                logger.info("xhs_ops: Gemini image generated: %s (%d bytes)",
                            tmp.name, len(part.inline_data.data))
                return tmp.name

        logger.warning("xhs_ops: Gemini response had no image parts")
        return None

    except Exception as e:
        logger.warning("xhs_ops: Gemini image generation failed: %s", e)
        return None


async def _auto_generate_images(
    title: str, content: str, session: "_XhsSession",
    image_prompt: str = "",
) -> list[str]:
    """自动生成小红书帖子配图。

    策略：
    1. 生成文字卡片（内容本身做成精美图片）
    2. 尝试用 Gemini 生成一张配图（封面图）
    3. 封面图 + 文字卡片 组合

    返回图片路径列表，至少 1 张。
    """
    images: list[str] = []

    # 1. 文字卡片（一定能成功，不依赖外部 API）
    cards = await _generate_text_card_pages(title, content, session)
    if cards:
        images.extend(cards)
        logger.info("xhs_ops: generated %d text card(s)", len(cards))

    # 2. Gemini 配图（尝试生成封面图，插入到最前面）
    if image_prompt:
        cover_prompt = image_prompt
    else:
        cover_prompt = (
            f"为小红书帖子生成一张精美的封面配图。\n"
            f"帖子标题：{title}\n"
            f"内容摘要：{content[:150]}\n\n"
            f"要求：\n"
            f"- 风格：现代、简洁、适合社交媒体\n"
            f"- 尺寸比例：3:4（竖版）\n"
            f"- 不要包含任何文字\n"
            f"- 色调温暖，吸引眼球"
        )
    try:
        cover = await asyncio.wait_for(
            _generate_gemini_image(cover_prompt), timeout=30,
        )
        if cover:
            images.insert(0, cover)  # 封面放第一张
            logger.info("xhs_ops: Gemini cover image generated")
    except asyncio.TimeoutError:
        logger.warning("xhs_ops: Gemini image generation timed out")
    except Exception as e:
        logger.warning("xhs_ops: Gemini cover generation failed: %s", e)

    return images


async def _handle_xhs_publish(args: dict) -> ToolResult:
    """发布小红书图文笔记

    小红书发帖必须通过创作者平台 creator.xiaohongshu.com，
    使用 contenteditable div 而不是标准 input/textarea。
    至少需要上传一张图片。
    """
    err = _check_playwright()
    if err:
        return ToolResult.error(err)

    title = args.get("title", "").strip()
    content = args.get("content", "").strip()
    images = args.get("images", [])  # URL 列表或本地路径
    auto_publish = args.get("auto_publish", True)  # 默认直接发布

    if not title:
        return ToolResult.invalid_param("title 不能为空")
    if not content:
        return ToolResult.invalid_param("content 不能为空")
    if len(title) > 20:
        return ToolResult.invalid_param(f"标题不能超过 20 个字符（当前 {len(title)} 个）")
    if len(content) > 1000:
        return ToolResult.invalid_param(f"正文不能超过 1000 个字符（当前 {len(content)} 个）")

    tenant_id = _get_tenant_id()
    session_key = _get_session_key() or tenant_id
    session = _xhs_sessions.get(session_key)
    if not session or not session.logged_in:
        return ToolResult.error("需要先登录小红书。请调用 xhs_login 并完成扫码。")

    session.last_used = time.time()

    try:
        # ── MCP 方案：为 creator 创建全新 context，通过 CDP 设置 cookie ──
        # xpzouying/xiaohongshu-mcp 的核心思路：
        # 1. 每次发帖用新浏览器 context（不复用 www 的 session）
        # 2. 通过 CDP Network.setCookies 在浏览器级别设置 cookie
        # 3. 导航前 cookie 已就绪，creator 直接认
        # 4. 不在 creator 上做任何登录操作

        # 清理之前的 creator context（如果 sub-agent 重复调用 xhs_publish）
        if hasattr(session, 'creator_context') and session.creator_context:
            try:
                await session.creator_context.close()
            except Exception:
                pass
            session.creator_context = None
            session.creator_page = None

        # ── 先访问 www/explore 刷新 session cookie ──
        # Creator 要求 fresh session，直接用 Redis 旧 cookies 会被 401 拒绝。
        # 访问 www 让服务器刷新 web_session，再保存到 Redis。
        try:
            await session.page.goto(
                f"{_XHS_BASE}/explore",
                wait_until="domcontentloaded", timeout=15000,
            )
            await asyncio.sleep(2)
            refreshed = await session.context.cookies()
            if refreshed:
                await _save_cookies_to_redis(session_key, refreshed)
                saved_cookies = refreshed
                logger.info("xhs_ops: refreshed %d cookies via www/explore before creator", len(refreshed))
            else:
                saved_cookies = await _load_cookies_from_redis(session_key)
        except Exception as e:
            logger.warning("xhs_ops: cookie refresh via www failed: %s, using Redis cookies", e)
            saved_cookies = await _load_cookies_from_redis(session_key)

        if not saved_cookies:
            # 从当前 context 提取作为 fallback
            saved_cookies = await session.context.cookies()

        # 创建发布专用 context
        creator_context = await session.browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        page = await creator_context.new_page()
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)

        # 通过 CDP 设置 cookies（和 MCP 的 Rod Browser.SetCookies 等效）
        cdp = await page.context.new_cdp_session(page)
        cdp_cookies = []
        for c in saved_cookies:
            cc = {
                "name": c.get("name", ""),
                "value": c.get("value", ""),
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
            }
            if c.get("expires"):
                cc["expires"] = c["expires"]
            if c.get("httpOnly"):
                cc["httpOnly"] = True
            if c.get("secure"):
                cc["secure"] = True
            if c.get("sameSite"):
                cc["sameSite"] = c["sameSite"]
            cdp_cookies.append(cc)

        if cdp_cookies:
            await cdp.send("Network.setCookies", {"cookies": cdp_cookies})
            logger.info("xhs_ops: set %d cookies via CDP for creator context", len(cdp_cookies))

        # 导航到创作者平台发布页（target=image 直接进图文模式，无需 tab 切换）
        _CREATOR_BASE = "https://creator.xiaohongshu.com"
        publish_url = f"{_CREATOR_BASE}/publish/publish?source=official&target=image"

        await page.goto(publish_url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            await asyncio.sleep(4)

        # ── 检查是否成功到达发布页（MCP 思路：不做登录，只检查结果）──
        current_url = page.url
        logger.info("xhs_ops: creator page URL: %s", current_url)

        creator_needs_login = False
        if "/login" in current_url:
            creator_needs_login = True
            logger.warning("xhs_ops: creator rejected cookies (URL has /login)")
        else:
            # 检查发布页元素（MCP 验证过的 selectors）
            _publish_selectors = [
                'input[type="file"]', '.upload-input',
                '[contenteditable="true"]', '.ql-editor',
                'div.d-input input', '.creator-tab', '.publish-page',
            ]
            has_publish_el = False
            for attempt in range(3):
                for sel in _publish_selectors:
                    try:
                        if await page.locator(sel).first.is_visible(timeout=500):
                            has_publish_el = True
                            logger.info("xhs_ops: found publish element: %s", sel)
                            break
                    except Exception:
                        continue
                if has_publish_el:
                    break
                await asyncio.sleep(2)

            if not has_publish_el:
                # 检查是否有登录表单
                has_login_form = await page.locator(
                    'input[type="password"], input[placeholder*="手机号"], '
                    '[class*="login-form"], .login-container'
                ).count() > 0
                if has_login_form:
                    creator_needs_login = True
                    logger.warning("xhs_ops: creator has login form, cookies not accepted")
                else:
                    logger.info("xhs_ops: no publish elements found but no login form either, continuing")

        if creator_needs_login:
            # 关闭临时 creator context
            await creator_context.close()
            logger.warning("xhs_ops: creator rejected cookies even after refresh, going to www QR login")

            # ── 直接去 www.xiaohongshu.com 做 QR 登录（MCP 方案 + 我们已验证 100% 可靠）──
            page = session.page
            qr_screenshot = None

            # 先检查 www 是否也需要登录
            www_logged_in = await _check_login_success(page)
            if www_logged_in:
                # www 已登录但 creator 拒绝 — session 可能在服务端失效
                # 先清除 cookies 让 www 也显示登录页
                logger.info("xhs_ops: www logged in but creator rejected, clearing cookies for fresh login")
                await session.context.clear_cookies()

            # ── 去 www.xiaohongshu.com 做 QR 登录（MCP 方案：只在 www 上 QR）──
            try:
                await page.goto(f"{_XHS_BASE}/explore", wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)
                # 等待 www 登录弹窗（www 默认显示 QR，不需要切换 tab）
                try:
                    await page.wait_for_selector(
                        '.login-container, [class*="login-modal"], [class*="LoginModal"]',
                        timeout=8000,
                    )
                except Exception:
                    await page.goto(f"{_XHS_BASE}/login", wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(3)
                # 等待 QR 码出现
                try:
                    await page.wait_for_selector('.qrcode-img, [class*="qrcode"] img', timeout=10000)
                    await asyncio.sleep(1)
                except Exception:
                    pass
                # 提取 QR 图片
                www_qr = await page.evaluate("""() => {
                    const qrImg = document.querySelector('.qrcode-img');
                    if (qrImg && qrImg.src && qrImg.src.startsWith('data:image')) return qrImg.src;
                    const containers = document.querySelectorAll('[class*="qrcode"], [class*="QRCode"]');
                    for (const c of containers) {
                        const img = c.querySelector('img[src*="data:image"]');
                        if (img) return img.src;
                    }
                    let best = null, bestLen = 0;
                    for (const img of document.querySelectorAll('img[src*="data:image"]')) {
                        if (img.src.length > bestLen) { bestLen = img.src.length; best = img; }
                    }
                    return (best && bestLen > 5000) ? best.src : null;
                }""")
                if www_qr and www_qr.startswith("data:image"):
                    import base64
                    b64_data = www_qr.split(",", 1)[1] if "," in www_qr else www_qr
                    qr_screenshot = base64.b64decode(b64_data)
                    logger.info("xhs_ops: QR extracted from www (%d bytes)", len(qr_screenshot))
            except Exception as e:
                logger.warning("xhs_ops: www QR login failed: %s", e)

            if not qr_screenshot:
                return ToolResult.error(
                    "小红书创作者平台需要重新登录，但无法获取二维码。\n"
                    "请先调用 xhs_login 完成扫码登录（会刷新 session），然后再试 xhs_publish。"
                )

            # 发送 QR 给用户
            sent = _send_qr_image_to_user(qr_screenshot)
            qr_msg = "二维码已发送" if sent else "无法发送二维码图片"

            # 轮询等待用户扫码（最多 90 秒）
            logger.info("xhs_ops: polling for www QR login (timeout=90s)")
            start = time.time()
            logged_in = False
            while time.time() - start < 90:
                await asyncio.sleep(_LOGIN_POLL_INTERVAL)
                if await _check_login_success(page):
                    logged_in = True
                    break

            if not logged_in:
                elapsed = int(time.time() - start)
                return ToolResult.error(
                    f"等待扫码超时（{elapsed}秒）。{qr_msg}。\n"
                    "请先调用 xhs_login 完成登录再试。"
                )

            logger.info("xhs_ops: www QR login success, saving cookies and retrying creator")
            # 保存新 cookies
            new_cookies = await session.context.cookies()
            await _save_cookies_to_redis(session_key, new_cookies)
            session.logged_in = True

            # 用新 cookies 创建新的 creator context（通过 CDP 设置，和上面一样）
            creator_context = await session.browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page = await creator_context.new_page()
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            """)
            cdp = await page.context.new_cdp_session(page)
            cdp_cookies = []
            for c in new_cookies:
                cc = {
                    "name": c.get("name", ""),
                    "value": c.get("value", ""),
                    "domain": c.get("domain", ""),
                    "path": c.get("path", "/"),
                }
                if c.get("expires"):
                    cc["expires"] = c["expires"]
                if c.get("httpOnly"):
                    cc["httpOnly"] = True
                if c.get("secure"):
                    cc["secure"] = True
                if c.get("sameSite"):
                    cc["sameSite"] = c["sameSite"]
                cdp_cookies.append(cc)
            if cdp_cookies:
                await cdp.send("Network.setCookies", {"cookies": cdp_cookies})
            await page.goto(publish_url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                await asyncio.sleep(4)

            if "/login" in page.url:
                await creator_context.close()
                return ToolResult.error(
                    "扫码登录成功，但创作者平台仍然需要登录。\n"
                    "这是小红书的限制——www 和 creator 是独立登录态。\n"
                    "建议：在手机小红书 App 上打开 creator.xiaohongshu.com 登录一次。"
                )
            logger.info("xhs_ops: creator context ready after QR login")

        # 保存 creator context/page 到 session，供 confirm_publish 使用
        session.creator_context = creator_context
        session.creator_page = page

        # ── 确保在"图文"模式（URL 已带 target=image，这里做 fallback 检查）──
        _upload_visible = False
        try:
            _upload_visible = await page.locator(
                '.upload-input, input[type="file"]'
            ).first.is_visible(timeout=3000)
        except Exception:
            pass

        if not _upload_visible:
            # URL target=image 没生效，尝试 JS 点击图文 tab
            logger.info("xhs_ops: upload input not visible, trying to click 图文 tab")
            try:
                clicked = await page.evaluate("""() => {
                    // 精确匹配最内层元素
                    const allEls = document.querySelectorAll('*');
                    for (const el of allEls) {
                        if (el.children.length > 1) continue;
                        const text = (el.textContent || '').trim();
                        if (text === '上传图文' || text === '图文') {
                            el.scrollIntoView({block: 'center'});
                            el.click();
                            return text;
                        }
                    }
                    return null;
                }""")
                if clicked:
                    logger.info("xhs_ops: clicked tab '%s' via JS", clicked)
                    await asyncio.sleep(2)
                else:
                    logger.info("xhs_ops: no 图文 tab found, continuing anyway")
            except Exception as e:
                logger.warning("xhs_ops: tab switch failed: %s", e)
        else:
            logger.info("xhs_ops: already in image-text mode (upload input visible)")

        # ── 上传图片（小红书发帖必须有图片）──
        if not images:
            # 无图片 → 自动生成：文字卡片 + Gemini 配图
            logger.info("xhs_ops: no images provided, auto-generating")
            image_prompt = args.get("image_prompt", "")
            generated = await _auto_generate_images(title, content, session, image_prompt)
            if generated:
                images = generated
            else:
                # 所有生成方式都失败，用纯色占位图
                logger.warning("xhs_ops: all image generation failed, using placeholder")
                import tempfile
                try:
                    from PIL import Image as PILImage
                    img = PILImage.new("RGB", (1080, 1080), color=(245, 245, 245))
                    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                    img.save(tmp.name)
                    images = [tmp.name]
                except ImportError:
                    import base64 as b64mod
                    _TINY_PNG = b64mod.b64decode(
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
                    )
                    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                    tmp.write(_TINY_PNG)
                    tmp.flush()
                    images = [tmp.name]

        # 查找文件上传 input（MCP 项目用 .upload-input，fallback 到 input[type="file"]）
        upload_loc = page.locator('.upload-input, input[type="file"]').first
        file_input = upload_loc
        if isinstance(images, str):
            images = [images]
        local_paths = []
        for img_path in images:
            if img_path.startswith(("http://", "https://")):
                import httpx
                import tempfile
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(img_path)
                    resp.raise_for_status()
                    suffix = ".jpg" if "jpg" in img_path or "jpeg" in img_path else ".png"
                    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
                        f.write(resp.content)
                        local_paths.append(f.name)
            else:
                local_paths.append(img_path)

        # 一次性上传所有图片（避免 DOM 刷新导致 input 引用失效）
        try:
            await file_input.set_input_files(local_paths)
            logger.info("xhs_ops: set_input_files with %d images", len(local_paths))
        except Exception as e:
            # fallback: 逐个上传
            logger.warning("xhs_ops: batch upload failed (%s), trying one by one", e)
            for lp in local_paths:
                try:
                    upload_el = page.locator('.upload-input, input[type="file"]').first
                    await upload_el.set_input_files(lp)
                    await asyncio.sleep(2)
                except Exception as e2:
                    logger.warning("xhs_ops: single file upload failed for %s: %s", lp, e2)

        # 等待图片上传完成（MCP 项目用 .img-preview-area .pr 计数确认）
        expected_count = len(local_paths)
        for _ in range(30):  # 最多等 60 秒
            preview_count = await page.locator('.img-preview-area .pr, .upload-preview img, .preview-item').count()
            if preview_count >= expected_count:
                logger.info("xhs_ops: %d/%d images uploaded", preview_count, expected_count)
                break
            await asyncio.sleep(2)
        else:
            logger.warning("xhs_ops: image upload may not be complete (expected %d)", expected_count)

        # ── 填写标题（contenteditable div）──
        # 多策略匹配：placeholder 属性 / aria / 通用 contenteditable
        title_filled = False
        title_selectors = [
            'div.d-input input',  # MCP 项目验证有效的 selector
            '#title-input',
            '[id*="title"] [contenteditable="true"]',
            'div[contenteditable="true"]:first-of-type',
            '[placeholder*="标题"]',
            '[data-placeholder*="标题"]',
        ]
        for sel in title_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click(timeout=3000)
                    await el.fill(title)
                    title_filled = True
                    logger.info("xhs_ops: title filled via selector: %s", sel)
                    break
            except Exception:
                continue

        if not title_filled:
            # 最后手段：用 Playwright get_by_placeholder
            try:
                await page.get_by_placeholder("标题").first.click(timeout=3000)
                await page.get_by_placeholder("标题").first.fill(title)
                title_filled = True
                logger.info("xhs_ops: title filled via get_by_placeholder")
            except Exception:
                pass

        if not title_filled:
            # 截图让 Gemini 帮忙定位
            analysis, _ = await _take_screenshot_and_analyze(
                page,
                "这是小红书创作者平台的发布页面。请描述页面上的输入框位置和状态。"
            )
            return ToolResult.error(
                f"无法找到标题输入框。\n页面状态：{analysis}\n"
                f"当前 URL: {page.url}\n"
                "可能是创作者平台页面结构已更新，请联系管理员检查 selector。"
            )

        await asyncio.sleep(0.5)

        # ── 填写正文（contenteditable div，通常是第二个）──
        content_filled = False
        content_selectors = [
            '.ql-editor',  # Quill 编辑器（MCP 项目验证有效）
            'div.ql-editor[contenteditable="true"]',
            '#post-content [contenteditable="true"]',
            '[id*="content"] [contenteditable="true"]',
            '[contenteditable="true"][data-placeholder]',
            '[placeholder*="正文"]',
            '[data-placeholder*="正文"]',
        ]
        for sel in content_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click(timeout=3000)
                    # contenteditable div 需要用 type 而不是 fill
                    await el.fill("")  # 清空
                    await page.keyboard.type(content, delay=10)
                    content_filled = True
                    logger.info("xhs_ops: content filled via selector: %s", sel)
                    break
            except Exception:
                continue

        if not content_filled:
            try:
                await page.get_by_placeholder("正文").first.click(timeout=3000)
                await page.keyboard.type(content, delay=10)
                content_filled = True
                logger.info("xhs_ops: content filled via get_by_placeholder")
            except Exception:
                pass

        if not content_filled:
            logger.warning("xhs_ops: could not fill content, title was filled")

        await asyncio.sleep(1)

        if auto_publish:
            # ── 直接点击发布按钮（MCP 一步到位方案）──
            publish_result = await _click_publish_button(page)
            # 发布完成后清理 creator context
            if session.creator_context:
                try:
                    await session.creator_context.close()
                except Exception:
                    pass
                session.creator_context = None
                session.creator_page = None
            return ToolResult.success(
                f"笔记已发布。\n\n"
                f"标题: {title}\n"
                f"正文: {content[:100]}...\n"
                f"图片: {len(images)} 张\n\n"
                f"── 发布结果 ──\n{publish_result}"
            )
        else:
            # 截图确认（仅预览模式）
            analysis, _ = await _take_screenshot_and_analyze(
                page,
                "这是小红书的发布页面。请确认：\n"
                "1. 标题和正文是否已填写\n"
                "2. 图片是否已上传\n"
                "3. 是否可以点击发布按钮\n"
                "4. 有没有任何错误提示"
            )

            return ToolResult.success(
                f"笔记已准备好（尚未发布）。\n\n"
                f"标题: {title}\n"
                f"正文: {content[:100]}...\n"
                f"图片: {len(images)} 张\n\n"
                f"── 页面状态 ──\n{analysis}\n\n"
                "如需发布，请调用 xhs_confirm_publish 确认。\n"
                "如需修改，可以用 browser_do 操作页面。"
            )

    except Exception as e:
        logger.exception("xhs_ops: publish failed")
        # 出错时清理 creator context
        if session.creator_context:
            try:
                await session.creator_context.close()
            except Exception:
                pass
            session.creator_context = None
            session.creator_page = None
        return ToolResult.error(f"发布准备失败: {e}")


async def _click_publish_button(page) -> str:
    """点击发布按钮并验证结果。返回发布结果描述。

    MCP 验证的 selectors（xpzouying 10k⭐）:
    - `.publish-page-publish-btn button.bg-red` — 最精确
    - `div.submit div.d-button-content` — 备选
    """
    # 多策略点击发布按钮
    clicked = False
    # 策略1: MCP 精确 selector（最可靠）
    for sel in [
        '.publish-page-publish-btn button.bg-red',
        '.publish-page-publish-btn button',
        'div.submit div.d-button-content',
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible(timeout=2000):
                await btn.click(timeout=5000)
                clicked = True
                logger.info("xhs_ops: clicked publish button via selector: %s", sel)
                break
        except Exception:
            continue

    # 策略2: JS 精确匹配（避免点到导航栏的"发布"文字）
    if not clicked:
        try:
            clicked = await page.evaluate("""() => {
                // 只在 .publish-page 区域内找按钮
                const area = document.querySelector('.publish-page, .creator-content, main');
                const root = area || document.body;
                const buttons = root.querySelectorAll('button');
                for (const btn of buttons) {
                    const text = (btn.textContent || '').trim();
                    if (text === '发布' || text === '发布笔记') {
                        btn.scrollIntoView({block: 'center'});
                        btn.click();
                        return true;
                    }
                }
                return false;
            }""")
            if clicked:
                logger.info("xhs_ops: clicked publish button via JS")
        except Exception:
            pass

    if not clicked:
        return "找不到发布按钮。页面可能已变化。"

    # 等待发布完成
    await asyncio.sleep(5)

    # 检查是否有 CAPTCHA
    has_captcha = False
    try:
        has_captcha = await page.locator(
            'div.red-captcha-slider, #red-captcha-rotate, [class*="captcha"]'
        ).count() > 0
    except Exception:
        pass

    if has_captcha:
        logger.info("xhs_ops: captcha detected after publish click")
        # 尝试用 Gemini Vision 解旋转验证码
        try:
            solved = await _try_solve_slider_captcha(page)
            if solved:
                await asyncio.sleep(3)
        except Exception as e:
            logger.warning("xhs_ops: captcha solve failed: %s", e)

    # 截图分析结果
    analysis, _ = await _take_screenshot_and_analyze(
        page,
        "请判断笔记是否发布成功：\n"
        "1. 页面是否跳转到了笔记详情、个人主页或'发布成功'页面？\n"
        "2. 是否有'发布成功'之类的提示？\n"
        "3. 是否有错误信息或验证码？\n"
        "请用一句话总结：发布成功/发布失败（原因）。"
    )

    return analysis


async def _handle_xhs_confirm_publish(args: dict) -> ToolResult:
    """确认发布小红书笔记（点击发布按钮）"""
    tenant_id = _get_tenant_id()
    session_key = _get_session_key() or tenant_id
    session = _xhs_sessions.get(session_key)
    if not session or not session.logged_in:
        return ToolResult.error("需要先登录并准备好笔记内容。")

    session.last_used = time.time()
    page = session.creator_page or session.page

    try:
        result = await _click_publish_button(page)

        # 发布完成后关闭 creator context
        if session.creator_context:
            try:
                await session.creator_context.close()
            except Exception:
                pass
            session.creator_context = None
            session.creator_page = None

        return ToolResult.success(f"发布操作已执行。\n\n── 结果 ──\n{result}")

    except Exception as e:
        if session.creator_context:
            try:
                await session.creator_context.close()
            except Exception:
                pass
            session.creator_context = None
            session.creator_page = None
        return ToolResult.error(f"发布失败: {e}")


async def _handle_xhs_comment(args: dict) -> ToolResult:
    """在小红书笔记下发表评论"""
    tenant_id = _get_tenant_id()
    session_key = _get_session_key() or tenant_id
    session = _xhs_sessions.get(session_key)
    if not session or not session.logged_in:
        return ToolResult.error("需要先登录小红书。")

    note_url = args.get("url", "").strip()
    comment_text = args.get("comment", "").strip()

    if not comment_text:
        return ToolResult.invalid_param("comment 不能为空")

    session.last_used = time.time()
    page = session.page

    try:
        # 如果提供了 URL，先导航
        if note_url:
            await page.goto(note_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

        # 找到评论输入框
        try:
            comment_input = page.locator('[placeholder*="评论"]').first
            await comment_input.click()
            await asyncio.sleep(0.5)
            await comment_input.fill(comment_text)
            await asyncio.sleep(0.5)

            # 按回车或点击发送
            await page.keyboard.press("Enter")
            await asyncio.sleep(2)
        except Exception:
            return ToolResult.error(
                "找不到评论输入框。请确认当前页面是笔记详情页，且已登录。"
            )

        analysis, _ = await _take_screenshot_and_analyze(
            page,
            "请判断评论是否发送成功：\n"
            "1. 评论内容是否出现在评论区？\n"
            "2. 是否有错误提示？"
        )

        return ToolResult.success(
            f"评论操作已执行。\n"
            f"评论内容: {comment_text}\n\n"
            f"── 结果 ──\n{analysis}"
        )

    except Exception as e:
        return ToolResult.error(f"评论失败: {e}")


async def _handle_xhs_like(args: dict) -> ToolResult:
    """点赞/收藏小红书笔记"""
    tenant_id = _get_tenant_id()
    session_key = _get_session_key() or tenant_id
    session = _xhs_sessions.get(session_key)
    if not session or not session.logged_in:
        return ToolResult.error("需要先登录小红书。")

    note_url = args.get("url", "").strip()
    action_type = args.get("action", "like")  # like / favorite / unlike / unfavorite

    session.last_used = time.time()
    page = session.page

    try:
        if note_url:
            await page.goto(note_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

        # 用 Gemini 视觉找到点赞/收藏按钮的位置
        analysis, _ = await _take_screenshot_and_analyze(
            page,
            f"这是一条小红书笔记。请找到{'点赞' if 'like' in action_type else '收藏'}按钮的位置，"
            f"描述它的 CSS 选择器或在页面中的大致位置。"
        )

        # 尝试点击点赞/收藏按钮
        if "like" in action_type:
            selectors = [
                '[class*="like"]', '[class*="Like"]',
                'svg[class*="like"]', '.like-wrapper',
            ]
            action_label = "点赞"
        else:
            selectors = [
                '[class*="collect"]', '[class*="Collect"]',
                '[class*="favorite"]', '.collect-wrapper',
            ]
            action_label = "收藏"

        clicked = False
        for sel in selectors:
            try:
                await page.click(sel, timeout=3000)
                clicked = True
                break
            except Exception:
                continue

        if not clicked:
            # fallback: 让 Gemini 看截图描述按钮位置，尝试文字匹配
            try:
                await page.get_by_role("button").filter(
                    has_text=re.compile(action_label)
                ).first.click(timeout=3000)
                clicked = True
            except Exception:
                pass

        if not clicked:
            return ToolResult.error(
                f"找不到{action_label}按钮。"
                f"可以用 browser_do(action='click', selector='...') 手动点击。"
            )

        await asyncio.sleep(1)
        analysis2, _ = await _take_screenshot_and_analyze(
            page,
            f"请确认{action_label}操作是否成功：按钮状态是否改变？"
        )

        return ToolResult.success(
            f"{action_label}操作已执行。\n\n── 结果 ──\n{analysis2}"
        )

    except Exception as e:
        return ToolResult.error(f"{action_type} 操作失败: {e}")


async def _handle_xhs_close(args: dict) -> ToolResult:
    """关闭小红书浏览器会话"""
    tenant_id = _get_tenant_id()
    session_key = _get_session_key() or tenant_id
    if session_key not in _xhs_sessions:
        return ToolResult.success("没有活跃的小红书会话。")

    await _cleanup_xhs_session(session_key)
    return ToolResult.success("小红书浏览器会话已关闭，Cookie 已保存。")


async def _handle_xhs_logout(args: dict) -> ToolResult:
    """退出小红书登录，清除 Cookie"""
    tenant_id = _get_tenant_id()
    if not tenant_id:
        return ToolResult.error("无法获取当前租户信息")
    session_key = _get_session_key() or tenant_id

    # 关闭浏览器会话
    if session_key in _xhs_sessions:
        session = _xhs_sessions.pop(session_key, None)
        if session:
            try:
                if session.context:
                    await session.context.close()
                if session.browser:
                    await session.browser.close()
                if session.playwright:
                    await session.playwright.stop()
            except Exception as e:
                logger.warning("xhs_ops: logout cleanup error: %s", e)

    # 清除 Redis 中的 Cookie（不保存，直接删）
    await _clear_cookies_from_redis(session_key)

    logger.info("xhs_ops: user logged out for %s", session_key)
    return ToolResult.success(
        "✅ 已退出小红书登录，Cookie 已清除。\n"
        "如需重新使用小红书功能，需要重新扫码登录。"
    )


# ── 登录检测 ──


async def _detect_login_wall(page) -> bool:
    """检测当前页面是否被小红书登录墙拦截。

    小红书未登录时会弹出登录弹窗或重定向到登录页，
    检测方式：查看 DOM 中是否有登录相关元素。
    """
    try:
        is_login = await page.evaluate("""() => {
            try {
                // 检查 URL 是否被重定向到登录页
                if (window.location.href.includes('/login')) return true;

                // 检查是否有登录弹窗（常见 class/id 模式）
                const loginSelectors = [
                    '.login-container', '.login-modal', '#login-modal',
                    '[class*="login-box"]', '[class*="LoginModal"]',
                    '[class*="login-panel"]', '.qrcode-login',
                ];
                for (const sel of loginSelectors) {
                    if (document.querySelector(sel)) return true;
                }

                // 检查页面文本是否包含登录提示
                const bodyText = document.body ? document.body.innerText.slice(0, 2000) : '';
                if (bodyText.includes('扫码登录') || bodyText.includes('手机号登录')) {
                    // 额外检查：不是搜索结果中恰好有这些关键词
                    // 如果同时有搜索结果卡片，说明不是登录墙
                    const hasResults = document.querySelectorAll('[class*="note-item"], [class*="user-item"], section').length > 2;
                    if (!hasResults) return true;
                }

                // 检查 __INITIAL_STATE__ 是否完全缺失（登录墙页面通常没有）
                const state = window.__INITIAL_STATE__;
                if (!state || !state.search) {
                    // 没有 search state + 页面看起来不像搜索结果 → 可能是登录墙
                    const hasContent = document.querySelectorAll('section, article, [class*="feed"], [class*="note"]').length > 0;
                    if (!hasContent) return true;
                }

                return false;
            } catch(e) {
                return false;
            }
        }""")
        return bool(is_login)
    except Exception as e:
        logger.debug("xhs_ops: login wall detection failed: %s", e)
        return False


# ── 供 social_media_ops.py 调用的内部搜索接口 ──


async def _extract_search_feeds_from_state(page, max_results: int) -> list[dict]:
    """从小红书页面的 __INITIAL_STATE__ 提取搜索结果。

    小红书用 React SSR，搜索结果在 window.__INITIAL_STATE__.search.feeds 中，
    包含 id（笔记 ID）、xsecToken、noteCard（标题/作者/互动数据）等字段。
    这比 CSS selector 可靠得多——selector 会因 class 名变更而失效，
    而 __INITIAL_STATE__ 是框架级结构，极少变动。

    参考：xpzouying/xiaohongshu-mcp (Go) + betars/xiaohongshu-mcp-python
    """
    try:
        raw = await page.evaluate("""(maxResults) => {
            try {
                const state = window.__INITIAL_STATE__;
                if (!state || !state.search) return {_debug: 'no state.search', _keys: []};

                const search = state.search;
                const allKeys = Object.keys(search);

                // 解包 Vue/Pinia 响应式对象的通用函数
                function unwrap(obj) {
                    if (!obj) return obj;
                    // Vue 3 Proxy 内部属性
                    if (obj.__v_raw) return obj.__v_raw;
                    if (obj._rawValue) return obj._rawValue;
                    if (obj._value !== undefined) return obj._value;
                    if (obj.value !== undefined) return obj.value;
                    return obj;
                }

                // 尝试多个可能的 feeds 来源
                const feedsCandidates = ['feeds', 'searchFeedsWrapper', 'searchFeeds', 'noteFeeds'];
                let feedsData = null;
                let hitKey = 'none';
                for (const key of feedsCandidates) {
                    const raw = search[key];
                    if (!raw) continue;
                    const val = unwrap(raw);
                    // 可能是数组，也可能是包含 feeds 子属性的对象
                    if (Array.isArray(val) && val.length > 0) {
                        feedsData = val;
                        hitKey = key;
                        break;
                    }
                    // searchFeedsWrapper 可能包含 feeds 子属性
                    if (val && typeof val === 'object' && !Array.isArray(val)) {
                        const inner = val.feeds || val.items || val.list || val.data;
                        const innerData = unwrap(inner);
                        if (Array.isArray(innerData) && innerData.length > 0) {
                            feedsData = innerData;
                            hitKey = key + '.inner';
                            break;
                        }
                    }
                }

                if (!feedsData) {
                    return {_debug: 'feeds not found', _keys: allKeys, _hit: hitKey};
                }

                return {_hit: hitKey, _data: feedsData.slice(0, maxResults).map(f => {
                    // 解包每个 feed 条目（也可能是响应式对象）
                    const item = unwrap(f) || f;
                    const nc = item.noteCard || item.note_card || {};
                    const user = (nc.user || {});
                    const interact = (nc.interactInfo || nc.interact_info || {});
                    return {
                        id: item.id || item.noteId || item.note_id || '',
                        xsec_token: item.xsecToken || item.xsec_token || '',
                        model_type: item.modelType || item.model_type || '',
                        title: nc.displayTitle || nc.display_title || nc.title || '',
                        author: user.nickname || user.nickName || '',
                        author_id: user.userId || user.user_id || '',
                        likes: interact.likedCount || interact.liked_count || '',
                    };
                })};
            } catch(e) {
                return {_debug: 'error: ' + e.message, _keys: []};
            }
        }""", max_results)

        if not raw or not isinstance(raw, dict):
            logger.warning("xhs_ops: __INITIAL_STATE__.search.feeds not found, falling back to CSS selectors")
            return []

        hit = raw.get("_hit", "none")
        data = raw.get("_data")
        debug = raw.get("_debug", "")
        keys = raw.get("_keys", [])

        if not data:
            logger.warning(
                "xhs_ops: __INITIAL_STATE__.search.feeds not found (hit=%s, debug=%s, keys=%s), falling back to CSS selectors",
                hit, debug, keys[:10],
            )
            return []

        # 过滤掉广告等非笔记内容
        feeds = [f for f in data if f.get("id")]
        logger.info("xhs_ops: extracted %d feeds from __INITIAL_STATE__ (hit=%s)", len(feeds), hit)
        return feeds

    except Exception as e:
        logger.warning("xhs_ops: __INITIAL_STATE__ extraction failed: %s", e)
        return []


async def _extract_search_users_from_state(page, max_results: int) -> list[dict]:
    """从小红书用户搜索页面的 __INITIAL_STATE__ 提取用户列表。

    用户搜索页 URL: /search_result?keyword=xxx&type=user
    数据路径未知（开源 MCP 项目均未实现用户搜索），
    所以先做 key 探测，再按常见路径尝试提取。

    返回格式: [{"user_id": "", "nickname": "", "desc": "", "fans": "", "red_id": ""}]
    """
    try:
        raw = await page.evaluate("""(maxResults) => {
            try {
                const state = window.__INITIAL_STATE__;
                if (!state || !state.search) return {_keys: [], _users: null};

                const search = state.search;
                // 收集 search 下所有 key 用于日志诊断
                const allKeys = Object.keys(search);

                // 解包 Vue/Pinia 响应式对象
                function unwrap(obj) {
                    if (!obj) return obj;
                    if (obj.__v_raw) return obj.__v_raw;
                    if (obj._rawValue) return obj._rawValue;
                    if (obj._value !== undefined) return obj._value;
                    if (obj.value !== undefined) return obj.value;
                    return obj;
                }

                // ── 尝试多个可能的 key 路径 ──
                const candidates = [
                    'userLists',  // ← 已确认正确 key（XHS-Downloader Tampermonkey 脚本）
                    'users', 'userList', 'userItems', 'user',
                    'userResults', 'searchUsers', 'creators',
                ];
                let usersData = null;
                let hitKey = '';
                for (const key of candidates) {
                    const raw = search[key];
                    if (!raw) continue;
                    const unwrapped = unwrap(raw);
                    if (Array.isArray(unwrapped) && unwrapped.length > 0) {
                        usersData = unwrapped;
                        hitKey = key;
                        break;
                    }
                }

                // 如果候选路径都没中，遍历所有 key 找数组
                if (!usersData) {
                    for (const key of allKeys) {
                        if (key === 'feeds') continue;  // feeds 是笔记，跳过
                        const raw = search[key];
                        if (!raw) continue;
                        const unwrapped = unwrap(raw);
                        if (Array.isArray(unwrapped) && unwrapped.length > 0) {
                            // 检查数组元素是否像用户对象（有 nickname/userId 等字段）
                            const first = unwrapped[0];
                            const userInfo = first.userInfo || first.user_info || first;
                            if (userInfo.nickname || userInfo.nickName || userInfo.userId || userInfo.user_id) {
                                usersData = unwrapped;
                                hitKey = key;
                                break;
                            }
                        }
                    }
                }

                if (!usersData) {
                    return {_keys: allKeys, _users: null};
                }

                const users = usersData.slice(0, maxResults).map(item => {
                    // 用户数据可能直接在 item 上，也可能嵌套在 userInfo/user_info 下
                    const u = item.userInfo || item.user_info || item;
                    return {
                        user_id: u.userId || u.user_id || u.id || item.id || '',
                        nickname: u.nickname || u.nickName || u.nick_name || '',
                        desc: u.desc || u.description || u.signature || '',
                        fans: u.fans || u.fansCount || u.followerCount || u.follower_count || '',
                        red_id: u.redId || u.red_id || '',
                        avatar: u.image || u.avatar || u.imageb || '',
                    };
                });

                return {_keys: allKeys, _hitKey: hitKey, _users: users};
            } catch(e) {
                return {_keys: [], _users: null, _error: e.message};
            }
        }""", max_results)

        if not raw:
            logger.warning("xhs_ops: __INITIAL_STATE__ not available on user search page")
            return []

        keys = raw.get("_keys", [])
        hit_key = raw.get("_hitKey", "")
        users = raw.get("_users")
        error = raw.get("_error", "")

        if error:
            logger.warning("xhs_ops: __INITIAL_STATE__ user extraction JS error: %s", error)
            return []

        # 关键诊断日志：记录 search 下所有 key，帮助定位正确路径
        logger.info("xhs_ops: __INITIAL_STATE__.search keys = %s (hit=%s)", keys, hit_key or "none")

        if not users:
            logger.warning("xhs_ops: __INITIAL_STATE__ user search found no user data (keys=%s)", keys)
            return []

        # 过滤掉无效条目
        valid_users = [u for u in users if u.get("user_id") or u.get("nickname")]
        logger.info("xhs_ops: extracted %d users from __INITIAL_STATE__ (key=%s)", len(valid_users), hit_key)
        return valid_users

    except Exception as e:
        logger.warning("xhs_ops: __INITIAL_STATE__ user extraction failed: %s", e)
        return []


async def _extract_search_links_from_dom(page, is_user_search: bool, max_results: int) -> list[str]:
    """从小红书搜索结果提取真实链接（CSS selector 兜底方案）。

    优先用 _extract_search_feeds_from_state()（更可靠），
    这个函数仅作为 fallback——当 __INITIAL_STATE__ 不可用时尝试 CSS selector。
    """
    try:
        if is_user_search:
            links = await page.evaluate("""(maxResults) => {
                const results = [];
                const anchors = document.querySelectorAll('a[href*="/user/profile/"]');
                const seen = new Set();
                for (const a of anchors) {
                    const href = a.getAttribute('href') || '';
                    if (href && !seen.has(href)) {
                        seen.add(href);
                        results.push(href.startsWith('http') ? href : 'https://www.xiaohongshu.com' + href);
                    }
                    if (results.length >= maxResults) break;
                }
                return results;
            }""", max_results)
        else:
            links = await page.evaluate("""(maxResults) => {
                const results = [];
                const anchors = document.querySelectorAll(
                    'a[href*="/explore/"], a[href*="/search_result/"], '
                    + 'a[href*="/discovery/item/"], section a[href]'
                );
                const seen = new Set();
                for (const a of anchors) {
                    const href = a.getAttribute('href') || '';
                    if (href && !seen.has(href) && !href.includes('/search_result?')) {
                        seen.add(href);
                        results.push(href.startsWith('http') ? href : 'https://www.xiaohongshu.com' + href);
                    }
                    if (results.length >= maxResults) break;
                }
                return results;
            }""", max_results)
        logger.info("xhs_ops: CSS selector fallback extracted %d links (user=%s)", len(links), is_user_search)
        return links
    except Exception as e:
        logger.warning("xhs_ops: CSS selector link extraction failed: %s", e)
        return []


async def xhs_playwright_search(keyword: str, search_type: str, max_results: int) -> dict | None:
    """Playwright 方式搜索小红书，供 social_media_ops fallback 使用。

    返回格式与 TikHub 一致：
    {
        "platform": "小红书",
        "platform_key": "xiaohongshu",
        "results": [{"title": ..., "body": ..., "href": ..., "metrics": {...}}],
        "source": "playwright_browser",
    }
    """
    err = _check_playwright()
    if err:
        return None

    tenant_id = _get_tenant_id()
    if not tenant_id:
        return None
    session_key = _get_session_key() or tenant_id

    try:
        return await asyncio.wait_for(
            _xhs_playwright_search_impl(session_key, keyword, search_type, max_results),
            timeout=_SEARCH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("xhs_ops: playwright_search '%s' timed out after %ds", keyword, _SEARCH_TIMEOUT)
        return None
    except Exception as e:
        logger.warning("xhs_ops: playwright search failed: %s", e)
        return None


async def _xhs_playwright_search_impl(
    session_key: str, keyword: str, search_type: str, max_results: int,
) -> dict | None:
    """xhs_playwright_search 的核心实现（被 wait_for 包裹做超时控制）。"""
    try:
        session = await _get_or_create_xhs_session(session_key)
        page = session.page

        is_user_search = "kol" in search_type
        if is_user_search:
            url = f"{_XHS_BASE}/search_result?keyword={keyword}&type=user"
        else:
            url = f"{_XHS_BASE}/search_result?keyword={keyword}"

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        await page.evaluate("window.scrollBy(0, 500)")
        await asyncio.sleep(1)

        # 优先从 __INITIAL_STATE__ 提取
        if is_user_search:
            state_users = await _extract_search_users_from_state(page, max_results)
            if state_users:
                results = []
                for u in state_users:
                    uid = u.get("user_id", "")
                    profile_url = f"{_XHS_BASE}/user/profile/{uid}" if uid else ""
                    results.append({
                        "title": u.get("nickname", ""),
                        "body": u.get("desc", ""),
                        "href": profile_url,
                        "metrics": {"followers": str(u.get("fans", ""))},
                    })
                if results:
                    return {
                        "platform": "小红书",
                        "platform_key": "xiaohongshu",
                        "results": results[:max_results],
                        "source": "playwright_initial_state",
                    }
        else:
            state_feeds = await _extract_search_feeds_from_state(page, max_results)
            if state_feeds:
                results = []
                for feed in state_feeds:
                    note_url = f"{_XHS_BASE}/explore/{feed['id']}"
                    if feed.get("xsec_token"):
                        note_url += f"?xsec_token={feed['xsec_token']}"
                    results.append({
                        "title": f"{feed.get('author', '')}: {feed.get('title', '')}",
                        "body": feed.get("title", ""),
                        "href": note_url,
                        "metrics": {"likes": str(feed.get("likes", ""))},
                        "author_id": feed.get("author_id", ""),
                    })
                if results:
                    return {
                        "platform": "小红书",
                        "platform_key": "xiaohongshu",
                        "results": results[:max_results],
                        "source": "playwright_initial_state",
                    }

        # fallback：CSS selector + Vision
        dom_links = await _extract_search_links_from_dom(page, is_user_search, max_results)

        type_label = "用户" if is_user_search else "笔记"
        extract_prompt = (
            f"小红书搜索「{keyword}」的{type_label}结果。\n"
            f"请提取前 {max_results} 条结果，每条用 JSON 格式：\n"
        )
        if is_user_search:
            extract_prompt += (
                '{"nickname": "昵称", "fans": "粉丝数", "desc": "简介"}\n'
                "一行一个 JSON，只输出 JSON，不要其他文字。"
            )
        else:
            extract_prompt += (
                '{"title": "标题", "author": "作者", "likes": "点赞数"}\n'
                "一行一个 JSON，只输出 JSON，不要其他文字。"
            )

        analysis, _ = await _take_screenshot_and_analyze(page, extract_prompt)

        results = []
        for idx, line in enumerate(analysis.strip().split("\n")):
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                item = json.loads(line)
                href = dom_links[len(results)] if len(results) < len(dom_links) else ""
                if is_user_search:
                    results.append({
                        "title": item.get("nickname", ""),
                        "body": item.get("desc", ""),
                        "href": href,
                        "metrics": {"followers": str(item.get("fans", ""))},
                    })
                else:
                    results.append({
                        "title": f"{item.get('author', '')}: {item.get('title', '')}",
                        "body": item.get("title", ""),
                        "href": href,
                        "metrics": {"likes": str(item.get("likes", ""))},
                    })
            except json.JSONDecodeError:
                continue

        # 输出验证：检测数据质量
        if results:
            empty_hrefs = sum(1 for r in results if not r.get("href"))
            if empty_hrefs > 0:
                logger.warning(
                    "xhs_ops: playwright_search '%s' — %d/%d results have empty href (data quality issue)",
                    keyword, empty_hrefs, len(results),
                )
                from app.services.error_log import record_error
                record_error(
                    "data_quality",
                    f"xhs_playwright_search: {empty_hrefs}/{len(results)} results have empty href for '{keyword}'",
                    tool_name="xhs_playwright_search",
                    tool_args={"keyword": keyword, "search_type": search_type},
                )

        if not results:
            return None

        return {
            "platform": "小红书",
            "platform_key": "xiaohongshu",
            "results": results[:max_results],
            "source": "playwright_browser",
        }

    except Exception as e:
        logger.warning("xhs_ops: playwright search failed: %s", e)
        return None


# ── 工具注册 ──

TOOL_DEFINITIONS = [
    {
        "name": "xhs_login",
        "description": (
            "小红书扫码登录。会发送二维码图片给用户，用户用小红书 App 扫码后自动完成登录。"
            "登录后 Cookie 保存 30 天，期间所有 xhs_search 等操作都能正常使用。"
            "⚠️ 当 xhs_search 被登录墙拦截时会自动调用此工具。"
            "⚠️ 重要：必须提醒用户小红书可能有风控风险，建议使用小号扫码，避免主号受影响。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "xhs_check_login",
        "description": "检查小红书当前的登录状态（是否已登录、登录的用户名）",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "xhs_logout",
        "description": (
            "退出小红书登录，清除当前用户的 Cookie 和浏览器会话。"
            "每个用户的登录态是独立的，退出不影响其他用户。"
            "适用：用户不想再让 bot 使用自己的小红书账号时调用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "xhs_search",
        "description": (
            "搜索小红书的笔记或用户。直接浏览小红书网页，提取搜索结果。"
            "比第三方 API 更准确——直接读取小红书真实页面数据。"
            "适用：找博主/KOL、搜索热门笔记、调研竞品内容。"
            "⚠️ 小红书网页版需要登录才能搜索——如果被登录墙拦截，请先调用 xhs_login 让用户扫码。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词",
                },
                "search_type": {
                    "type": "string",
                    "enum": ["note", "user"],
                    "description": "搜索类型：note（笔记，默认）或 user（用户）",
                    "default": "note",
                },
                "sort": {
                    "type": "string",
                    "enum": ["general", "time_descending", "popularity_descending"],
                    "description": "排序方式：general（综合）、time_descending（最新）、popularity_descending（最热）",
                    "default": "general",
                },
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "xhs_get_note",
        "description": (
            "获取小红书笔记的完整详情：标题、正文、作者、互动数据（点赞/收藏/评论数）、热门评论。"
            "可以用笔记 ID 或完整 URL。"
            "支持所有小红书链接格式：微信分享链接（/discovery/item/...）、标准链接（/explore/...）等。"
            "当用户发来小红书链接时，优先用此工具（比 web_search 更准确）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "note_id": {
                    "type": "string",
                    "description": "笔记 ID（与 url 二选一）",
                },
                "url": {
                    "type": "string",
                    "description": "笔记完整 URL（与 note_id 二选一）",
                },
            },
        },
    },
    {
        "name": "xhs_get_user",
        "description": (
            "获取小红书用户主页信息：昵称、简介、粉丝数/关注数/获赞数、最近发布的笔记列表。"
            "可以用用户 ID 或完整 URL。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "用户 ID（与 url 二选一）",
                },
                "url": {
                    "type": "string",
                    "description": "用户主页 URL（与 user_id 二选一）",
                },
            },
        },
    },
    {
        "name": "xhs_publish",
        "description": (
            "发布小红书图文笔记。需要已登录（xhs_login）。"
            "限制：标题最多 20 字，正文最多 1000 字。"
            "默认直接发布（auto_publish=true），不需要再调 xhs_confirm_publish。"
            "不提供 images 时会自动生成：文字卡片图 + Gemini AI 封面配图。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "笔记标题（最多 20 个字符）",
                },
                "content": {
                    "type": "string",
                    "description": "笔记正文（最多 1000 个字符）",
                },
                "images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "图片列表（URL 或本地路径）。为空时自动生成文字卡片 + AI 配图",
                },
                "image_prompt": {
                    "type": "string",
                    "description": "自定义 AI 配图提示词（仅在 images 为空时生效）",
                },
                "auto_publish": {
                    "type": "boolean",
                    "description": "是否直接发布（默认 true）。设为 false 时只预览不发布，需再调 xhs_confirm_publish",
                    "default": True,
                },
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "xhs_confirm_publish",
        "description": "确认发布小红书笔记（仅在 xhs_publish 设了 auto_publish=false 时需要）",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "xhs_comment",
        "description": (
            "在小红书笔记下发表评论。需要已登录。"
            "可以提供笔记 URL 自动导航，或在当前已打开的笔记页面直接评论。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "笔记 URL（可选，不填则在当前页面评论）",
                },
                "comment": {
                    "type": "string",
                    "description": "评论内容",
                },
            },
            "required": ["comment"],
        },
    },
    {
        "name": "xhs_like",
        "description": (
            "对小红书笔记点赞或收藏。需要已登录。"
            "支持：like（点赞）、favorite（收藏）、unlike（取消点赞）、unfavorite（取消收藏）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "笔记 URL（可选，不填则操作当前页面）",
                },
                "action": {
                    "type": "string",
                    "enum": ["like", "favorite", "unlike", "unfavorite"],
                    "description": "操作类型，默认 like",
                    "default": "like",
                },
            },
        },
    },
    {
        "name": "xhs_close",
        "description": "关闭小红书浏览器会话，释放资源。Cookie 会自动保存到 Redis。",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]

TOOL_MAP = {
    "xhs_login": _handle_xhs_login,
    "xhs_check_login": _handle_xhs_check_login,
    "xhs_logout": _handle_xhs_logout,
    "xhs_search": _handle_xhs_search,
    "xhs_get_note": _handle_xhs_get_note,
    "xhs_get_user": _handle_xhs_get_user,
    "xhs_publish": _handle_xhs_publish,
    "xhs_confirm_publish": _handle_xhs_confirm_publish,
    "xhs_comment": _handle_xhs_comment,
    "xhs_like": _handle_xhs_like,
    "xhs_close": _handle_xhs_close,
}
