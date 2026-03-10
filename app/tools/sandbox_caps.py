"""沙箱能力原语（Sandbox Capabilities）

为沙箱内的自定义工具提供安全的系统级能力接口。

设计哲学：像浏览器给 JS 提供 fetch() 一样，
沙箱代码不能直接 import subprocess/os/google.genai，
但可以调用这些预审的、有限制的安全函数。

每个能力函数内置：
- 输入校验（URL 格式、大小限制等）
- 超时保护
- 错误处理
- 资源清理

用法（在沙箱代码中）：
    from app.tools.sandbox_caps import download_video, gemini_analyze_video
    from app.tools.sandbox_caps import web_search
    from app.tools.sandbox_caps import read_server_logs, get_process_info, search_logs

安全边界：
- download_video: 只允许已知视频平台 URL，200MB 上限，720p 限制
- gemini_analyze_video: 消耗 API 额度，有大小限制
- gemini_analyze_image: 同上，图片分析
- web_search: 走 CF Worker 代理或 DDG 直连，30s 超时，最多 10 条结果
- read_server_logs: 只读日志文件，限制 500 行
- search_logs: 只读日志文件关键词搜索，限制 200 行
- get_process_info: 只返回 pid/memory/uptime，无写操作
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════
#  安全限制常量
# ═══════════════════════════════════════════════════════

_MAX_DOWNLOAD_SIZE = 200 * 1024 * 1024  # 200MB
_MAX_VIDEO_DURATION = 3600  # 1 小时

# YouTube URL 检测（用于 from_uri 直传优化）
_YOUTUBE_URL = re.compile(
    r"^https?://(www\.|m\.)?(youtube\.com/(watch|shorts|live)|youtu\.be/)",
    re.IGNORECASE,
)

# 允许的视频平台 URL（防止 SSRF）
_ALLOWED_VIDEO_HOSTS = re.compile(
    r"^https?://(www\.)?"
    r"(youtube\.com|youtu\.be|bilibili\.com|b23\.tv"
    r"|vimeo\.com|twitch\.tv|tiktok\.com|douyin\.com"
    r"|twitter\.com|x\.com|instagram\.com"
    r"|v\.qq\.com|ixigua\.com|xiaohongshu\.com"
    r")/",
    re.IGNORECASE,
)

# yt-dlp 代理（中国大陆服务器访问 YouTube 等需要）
_YT_DLP_PROXY = (
    os.getenv("YT_DLP_PROXY", "").strip()
    or os.getenv("HTTPS_PROXY", "").strip()
    or os.getenv("HTTP_PROXY", "").strip()
    or None
)

# yt-dlp cookies 文件路径（YouTube 反爬需要认证 cookies）
_YT_DLP_COOKIES = os.getenv("YT_DLP_COOKIES", "").strip() or None


# ═══════════════════════════════════════════════════════
#  返回类型
# ═══════════════════════════════════════════════════════

@dataclass
class VideoInfo:
    """视频元信息（下载前获取）"""
    title: str
    uploader: str
    duration: int  # 秒
    url: str


@dataclass
class VideoData:
    """下载后的视频数据"""
    info: VideoInfo
    data: bytes
    mime_type: str  # 固定 video/mp4


@dataclass
class AnalysisResult:
    """Gemini 分析结果"""
    text: str
    model: str


# ═══════════════════════════════════════════════════════
#  能力 1: 视频下载
# ═══════════════════════════════════════════════════════

def _check_video_url(url: str) -> str | None:
    """校验 URL 是否是允许的视频平台，返回错误信息或 None。"""
    if not url or not isinstance(url, str):
        return "URL 不能为空"
    url = url.strip()
    if not _ALLOWED_VIDEO_HOSTS.match(url):
        return (
            f"不支持的视频平台。支持: YouTube, Bilibili, 抖音, TikTok, "
            f"Vimeo, Twitch, 小红书, 西瓜视频, 腾讯视频"
        )
    return None


def get_video_info(url: str) -> VideoInfo | str:
    """获取视频元信息（不下载），返回 VideoInfo 或错误字符串。

    安全: 只请求元数据，不下载视频内容。
    """
    err = _check_video_url(url)
    if err:
        return err

    return _run_async(_async_get_video_info(url))


def download_video(url: str) -> VideoData | str:
    """下载视频，返回 VideoData 或错误字符串。

    安全限制:
    - 仅允许已知视频平台 URL
    - 最大 200MB
    - 分辨率限制 720p
    - 时长限制 1 小时
    - 5 分钟超时
    """
    err = _check_video_url(url)
    if err:
        return err

    return _run_async(_async_download_video(url))


# ═══════════════════════════════════════════════════════
#  能力 2: Gemini 视频/图片分析
# ═══════════════════════════════════════════════════════

def gemini_analyze_video(video_data: bytes, prompt: str, mime_type: str = "video/mp4") -> AnalysisResult | str:
    """上传视频到 Gemini File API 并分析，返回 AnalysisResult 或错误字符串。

    安全限制:
    - 最大 200MB
    - 使用当前租户的 API key（自动获取）
    """
    if not video_data or not isinstance(video_data, bytes):
        return "video_data 必须是非空 bytes"
    if len(video_data) > _MAX_DOWNLOAD_SIZE:
        return f"视频太大（{len(video_data) // (1024*1024)}MB），最大 {_MAX_DOWNLOAD_SIZE // (1024*1024)}MB"
    if not prompt:
        return "prompt 不能为空"

    # 在调用线程捕获租户上下文（_run_async 的新线程不继承 contextvars）
    tenant = _get_tenant_or_none()
    return _run_async(_async_gemini_analyze(video_data, prompt, mime_type, tenant=tenant))


def gemini_analyze_image(image_data: bytes, prompt: str, mime_type: str = "image/png") -> AnalysisResult | str:
    """用 Gemini 分析图片，返回 AnalysisResult 或错误字符串。

    安全限制:
    - 最大 20MB
    - 使用当前租户的 API key
    """
    if not image_data or not isinstance(image_data, bytes):
        return "image_data 必须是非空 bytes"
    if len(image_data) > 20 * 1024 * 1024:
        return f"图片太大（{len(image_data) // (1024*1024)}MB），最大 20MB"
    if not prompt:
        return "prompt 不能为空"

    # 在调用线程捕获租户上下文（_run_async 的新线程不继承 contextvars）
    tenant = _get_tenant_or_none()
    return _run_async(_async_gemini_analyze(image_data, prompt, mime_type, tenant=tenant))


# ═══════════════════════════════════════════════════════
#  能力 2b: YouTube 视频直传分析
# ═══════════════════════════════════════════════════════

def is_youtube_url(url: str) -> bool:
    """检测是否是 YouTube URL。"""
    return bool(url and _YOUTUBE_URL.match(url.strip()))


def gemini_analyze_youtube_url(url: str, prompt: str) -> AnalysisResult | str:
    """直接将 YouTube URL 传给 Gemini 分析（不需要下载）。

    Gemini 原生支持 YouTube URL，通过 from_uri 直传，
    完全绕过 yt-dlp 下载 + File API 上传流程。
    限制：视频必须是公开的（不支持未列出/私享视频）。
    """
    if not url:
        return "URL 不能为空"
    if not prompt:
        return "prompt 不能为空"
    if not is_youtube_url(url):
        return "仅支持 YouTube URL"

    # 在调用线程捕获租户上下文（_run_async 的新线程不继承 contextvars）
    tenant = _get_tenant_or_none()
    return _run_async(_async_gemini_analyze_youtube(url.strip(), prompt, tenant=tenant))


# ═══════════════════════════════════════════════════════
#  能力 3: 网络搜索
# ═══════════════════════════════════════════════════════

_MAX_SEARCH_RESULTS = 10

# 搜索配置（从环境变量读取，沙箱代码无需感知）
_CF_SEARCH_URL = os.getenv("DDG_SEARCH_PROXY_URL", "").strip().rstrip("/") or None
_CF_SEARCH_TOKEN = os.getenv("DDG_SEARCH_PROXY_TOKEN", "").strip() or None


@dataclass
class SearchResult:
    """单条搜索结果"""
    title: str
    body: str
    href: str


def web_search(query: str, max_results: int = 5) -> list[SearchResult] | str:
    """搜索互联网，返回搜索结果列表或错误字符串。

    安全限制:
    - 最多 10 条结果
    - 30 秒超时
    - 通过 CF Worker 代理或 DDG 直连（沙箱代码无需关心代理配置）
    """
    if not query or not isinstance(query, str):
        return "搜索关键词不能为空"
    query = query.strip()
    if not query:
        return "搜索关键词不能为空"

    max_results = min(max(1, max_results), _MAX_SEARCH_RESULTS)

    try:
        if _CF_SEARCH_URL:
            results = _search_worker(query, max_results)
        else:
            results = _search_ddgs(query, max_results)

        return [
            SearchResult(
                title=r.get("title", ""),
                body=r.get("body", ""),
                href=r.get("href", ""),
            )
            for r in results
        ]
    except Exception as e:
        return f"搜索失败: {e}"


def _search_worker(query: str, max_results: int) -> list[dict]:
    """通过 Cloudflare Worker 代理搜索。"""
    import httpx

    headers = {}
    if _CF_SEARCH_TOKEN:
        headers["X-Proxy-Token"] = _CF_SEARCH_TOKEN

    resp = httpx.get(
        f"{_CF_SEARCH_URL}/search",
        params={"q": query, "max_results": max_results},
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data.get("results", [])


def _search_ddgs(query: str, max_results: int) -> list[dict]:
    """直连 DuckDuckGo。"""
    from duckduckgo_search import DDGS

    proxy = os.getenv("DDGS_PROXY", "").strip() or None
    if not proxy:
        # 回退到通用代理
        proxy = os.getenv("HTTPS_PROXY", "").strip() or os.getenv("HTTP_PROXY", "").strip() or None

    with DDGS(proxy=proxy, timeout=15) as ddgs:
        return list(ddgs.text(query, max_results=max_results))


# ═══════════════════════════════════════════════════════
#  能力 4: 服务器自查
# ═══════════════════════════════════════════════════════

_LOG_FILE = os.getenv("BOT_LOG_FILE", "/app/logs/bot.log")
_MAX_LOG_LINES = 500
_MAX_SEARCH_LOG_LINES = 200
_PROCESS_START_TIME = time.time()


@dataclass
class ProcessInfo:
    """进程运行信息"""
    status: str
    uptime_seconds: int
    memory_mb: float
    pid: int
    log_file: str
    log_size_kb: int


def get_process_info() -> ProcessInfo:
    """获取 bot 进程运行状态。

    安全: 只读，不修改任何状态。
    """
    import resource as _resource

    uptime = int(time.time() - _PROCESS_START_TIME)
    mem_kb = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
    mem_mb = mem_kb / 1024

    log_size_kb = 0
    if os.path.exists(_LOG_FILE):
        log_size_kb = os.path.getsize(_LOG_FILE) // 1024

    return ProcessInfo(
        status="运行中",
        uptime_seconds=uptime,
        memory_mb=round(mem_mb, 1),
        pid=os.getpid(),
        log_file=_LOG_FILE,
        log_size_kb=log_size_kb,
    )


def read_server_logs(num_lines: int = 100) -> str:
    """读取 bot 最近的运行日志。

    安全限制:
    - 只读 BOT_LOG_FILE 指定的文件（不可自定义路径）
    - 最多 500 行
    """
    num_lines = min(max(1, num_lines), _MAX_LOG_LINES)

    if not os.path.exists(_LOG_FILE):
        return "日志文件不存在，可能尚未生成。"

    try:
        with open(_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()

        tail = all_lines[-num_lines:]
        if not tail:
            return "日志文件为空。"

        return f"最近 {len(tail)} 条日志（共 {len(all_lines)} 行）：\n\n" + "".join(tail)
    except Exception as e:
        return f"读取日志失败: {e}"


def search_logs(keyword: str, num_lines: int = 50) -> str:
    """在日志中搜索包含关键词的行。

    安全限制:
    - 只读 BOT_LOG_FILE 指定的文件
    - 最多返回 200 条
    """
    if not keyword or not isinstance(keyword, str):
        return "关键词不能为空"

    num_lines = min(max(1, num_lines), _MAX_SEARCH_LOG_LINES)

    if not os.path.exists(_LOG_FILE):
        return "日志文件不存在。"

    try:
        with open(_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()

        keyword_lower = keyword.lower()
        matches = [line for line in all_lines if keyword_lower in line.lower()]
        tail = matches[-num_lines:]

        if not tail:
            return f"未找到包含 '{keyword}' 的日志。"

        return (
            f"找到 {len(matches)} 条包含 '{keyword}' 的日志"
            f"（显示最近 {len(tail)} 条）：\n\n" + "".join(tail)
        )
    except Exception as e:
        return f"搜索日志失败: {e}"


# ═══════════════════════════════════════════════════════
#  租户上下文辅助（解决 _run_async 线程不继承 contextvars）
# ═══════════════════════════════════════════════════════

def _get_tenant_or_none():
    """在当前线程安全地获取租户，返回 tenant 或 None。

    _run_async 在 ThreadPoolExecutor 的新线程中运行 async 代码，
    而 Python contextvars 不会自动传播到新线程。
    所以必须在调用 _run_async 之前（仍在原线程上）捕获 tenant，
    然后作为参数传给 async 函数。
    """
    try:
        from app.tenant.context import get_current_tenant
        return get_current_tenant()
    except Exception:
        return None


# ═══════════════════════════════════════════════════════
#  内部实现（async）— 视频/Gemini 相关
# ═══════════════════════════════════════════════════════

def _run_async(coro):
    """在新线程的独立 event loop 中执行协程。

    生产环境中沙箱代码是 sync 的但运行在 FastAPI async context 内，
    不能用 run_until_complete（会报 "loop already running"）。
    所以在新线程创建独立 loop 来跑，主线程同步等待结果。
    """
    import concurrent.futures

    result = None
    exception = None

    def _worker():
        nonlocal result, exception
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(coro)
        except Exception as e:
            exception = e
        finally:
            # 正确清理：先关闭异步生成器，再关闭 pending tasks，最后关闭 loop
            # 这样 subprocess transport 的 __del__ 不会在 loop 关闭后报错
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            try:
                _cancel_pending_tasks(loop)
            except Exception:
                pass
            loop.close()
            asyncio.set_event_loop(None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_worker)
        # 等待最多 6 分钟（留 buffer 给 yt-dlp 5 分钟超时）
        future.result(timeout=360)

    if exception:
        raise exception
    return result


def _cancel_pending_tasks(loop: asyncio.AbstractEventLoop) -> None:
    """取消所有 pending tasks，确保 event loop 可以干净关闭。"""
    tasks = asyncio.all_tasks(loop)
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))


def _close_proc(proc: asyncio.subprocess.Process) -> None:
    """关闭 subprocess transport，防止 GC 时报 'Event loop is closed'。

    asyncio.create_subprocess_exec 创建的 transport 在 __del__ 时会调用
    loop.call_soon()，如果 loop 已关闭就会抛 RuntimeError。
    显式调用 transport.close() 设置 _closed=True，避免 __del__ 重复操作。
    """
    try:
        transport = getattr(proc, "_transport", None)
        if transport and not getattr(transport, "_closed", True):
            transport.close()
    except Exception:
        pass


async def _async_get_video_info(url: str) -> VideoInfo | str:
    """yt-dlp 获取视频元信息。"""
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        return "服务器未安装 yt-dlp，管理员请运行: pip install yt-dlp"

    cmd = ["yt-dlp", "--no-download", "--print-json", "--no-warnings", "--no-playlist",
           "--js-runtimes", "node"]
    if _YT_DLP_PROXY:
        cmd += ["--proxy", _YT_DLP_PROXY]
    if _YT_DLP_COOKIES:
        cmd += ["--cookies", _YT_DLP_COOKIES]
    cmd.append(url)

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            return f"获取视频信息失败: {stderr.decode(errors='replace')[:200]}"

        import json
        info = json.loads(stdout.decode(errors="replace"))
        return VideoInfo(
            title=info.get("title", "未知标题"),
            uploader=info.get("uploader", "未知"),
            duration=info.get("duration", 0) or 0,
            url=url,
        )
    except asyncio.TimeoutError:
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()
        return "获取视频信息超时（30秒）"
    except Exception as e:
        return f"获取视频信息失败: {e}"
    finally:
        if proc:
            _close_proc(proc)


async def _async_download_video(url: str) -> VideoData | str:
    """yt-dlp 下载视频。"""
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        return "服务器未安装 yt-dlp，管理员请运行: pip install yt-dlp"

    # 先获取信息
    info = await _async_get_video_info(url)
    if isinstance(info, str):
        return info

    if info.duration > _MAX_VIDEO_DURATION:
        return f"视频时长 {info.duration // 60} 分钟，超过 {_MAX_VIDEO_DURATION // 60} 分钟限制"

    logger.info("sandbox_caps: downloading %s (title=%s, duration=%ds)", url, info.title, info.duration)

    tmpdir = tempfile.mkdtemp(prefix="sbcap_")
    output_template = os.path.join(tmpdir, "video.%(ext)s")

    cmd = [
        "yt-dlp",
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]/best",
        "--merge-output-format", "mp4",
        "--no-playlist", "--no-warnings",
        "--max-filesize", str(_MAX_DOWNLOAD_SIZE),
        "--js-runtimes", "node",
        "-o", output_template,
    ]
    if _YT_DLP_PROXY:
        cmd += ["--proxy", _YT_DLP_PROXY]
    if _YT_DLP_COOKIES:
        cmd += ["--cookies", _YT_DLP_COOKIES]
    cmd.append(url)

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

        if proc.returncode != 0:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return f"视频下载失败: {stderr.decode(errors='replace')[:200]}"

        import glob
        files = glob.glob(os.path.join(tmpdir, "video.*"))
        if not files:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return "下载完成但未找到视频文件"

        video_path = files[0]
        file_size = os.path.getsize(video_path)
        logger.info("sandbox_caps: downloaded %s → %dMB", info.title, file_size // (1024 * 1024))

        if file_size > _MAX_DOWNLOAD_SIZE:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return f"视频太大（{file_size // (1024*1024)}MB），超过 {_MAX_DOWNLOAD_SIZE // (1024*1024)}MB 限制"

        with open(video_path, "rb") as f:
            video_bytes = f.read()

        shutil.rmtree(tmpdir, ignore_errors=True)
        return VideoData(info=info, data=video_bytes, mime_type="video/mp4")

    except asyncio.TimeoutError:
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()
        shutil.rmtree(tmpdir, ignore_errors=True)
        return "视频下载超时（5分钟）"
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return f"视频下载失败: {e}"
    finally:
        if proc:
            _close_proc(proc)


async def _async_gemini_analyze(data: bytes, prompt: str, mime_type: str, *, tenant=None) -> AnalysisResult | str:
    """通过 Gemini File API 分析媒体内容。

    tenant 参数由调用者在原线程中捕获后传入，
    因为 _run_async 的新线程不继承 contextvars。
    """
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError as e:
        return f"Gemini SDK 未安装或导入失败: {e}"

    if not tenant or not tenant.llm_api_key:
        logger.warning("sandbox_caps: _async_gemini_analyze tenant missing or no API key")
        return "当前租户未配置 Gemini API Key"

    # 构建 client
    http_options: dict = {"timeout": 300_000}
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

    # 对于视频/音频，使用 File API 上传（代理也支持，CF Worker 转发 /upload/ 路径）
    is_media = mime_type.startswith(("video/", "audio/"))
    parts = []

    if is_media:
        # File API 上传（直连或通过代理均可）
        try:
            ext_map = {"video/mp4": ".mp4", "audio/ogg": ".ogg", "audio/mp3": ".mp3"}
            ext = ext_map.get(mime_type, ".bin")
            logger.info("sandbox_caps: uploading %s (%dMB) via File API",
                        ext, len(data) // (1024 * 1024))
            file = await client.aio.files.upload(
                file=io.BytesIO(data),
                config={"mime_type": mime_type, "display_name": f"sandbox_upload{ext}"},
            )
            logger.info("sandbox_caps: file upload name=%s state=%s", file.name, file.state)

            for _ in range(60):
                if not file.state or file.state.name != "PROCESSING":
                    break
                await asyncio.sleep(2)
                file = await client.aio.files.get(name=file.name)

            if file.state and file.state.name == "FAILED":
                return "Gemini 文件处理失败"

            parts.append(genai_types.Part(file_data=genai_types.FileData(
                file_uri=file.uri, mime_type=file.mime_type,
            )))
        except Exception as e:
            logger.warning("sandbox_caps: file API failed (%s), falling back to inline", e)
            parts.append(genai_types.Part(
                inline_data=genai_types.Blob(mime_type=mime_type, data=data)
            ))
    else:
        # inline data（图片等小文件）
        parts.append(genai_types.Part(
            inline_data=genai_types.Blob(mime_type=mime_type, data=data)
        ))

    parts.append(genai_types.Part(text=prompt))

    try:
        response = await client.aio.models.generate_content(
            model=model,
            contents=[genai_types.Content(role="user", parts=parts)],
            config=genai_types.GenerateContentConfig(temperature=0.3, max_output_tokens=4096),
        )
        if not response.text:
            return "Gemini 返回空结果"
        return AnalysisResult(text=response.text, model=model)
    except Exception as e:
        return f"Gemini 分析失败: {e}"


async def _async_gemini_analyze_youtube(url: str, prompt: str, *, tenant=None) -> AnalysisResult | str:
    """通过 Gemini from_uri 直接分析 YouTube 视频（无需下载/上传）。

    tenant 参数由调用者在原线程中捕获后传入，
    因为 _run_async 的新线程不继承 contextvars。
    """
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError as e:
        return f"Gemini SDK 未安装或导入失败: {e}"

    if not tenant or not tenant.llm_api_key:
        logger.warning("sandbox_caps: _async_gemini_analyze_youtube tenant missing or no API key")
        return "当前租户未配置 Gemini API Key"

    # 构建 client（同 _async_gemini_analyze）
    http_options: dict = {"timeout": 300_000}
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
    # 使用租户配置的模型，不要硬编码过期的预览版
    model = tenant.llm_model or "gemini-3-flash-preview"

    # YouTube URL 直传 — Gemini 服务端直接获取视频
    # 不指定 mime_type，让 API 自动识别（官方示例也不传，传 video/* 可能导致 403）
    parts = [
        genai_types.Part(file_data=genai_types.FileData(file_uri=url)),
        genai_types.Part(text=prompt),
    ]

    logger.info("sandbox_caps: YouTube from_uri starting, url=%s model=%s", url, model)
    try:
        response = await client.aio.models.generate_content(
            model=model,
            contents=[genai_types.Content(role="user", parts=parts)],
            config=genai_types.GenerateContentConfig(temperature=0.3, max_output_tokens=4096),
        )
        if not response.text:
            logger.warning("sandbox_caps: YouTube from_uri empty response, url=%s", url)
            return "Gemini 返回空结果"
        logger.info("sandbox_caps: YouTube from_uri analysis done, url=%s", url)
        return AnalysisResult(text=response.text, model=model)
    except Exception as e:
        logger.error("sandbox_caps: YouTube from_uri failed: %s url=%s", e, url)
        return f"Gemini 分析 YouTube 视频失败: {e}"
