"""自我迭代工具 —— 让 bot 能读写自己的源代码并安全部署修复

部署流程：
1. self_edit_file / self_write_file 直接推到 main 分支
2. 推送到 main 后 GitHub Actions CI/CD 自动构建和重启
3. self_safe_deploy 记录回滚点 + 保存任务上下文（重启后自动继续）
4. 如果部署失败 → startup_rollback_check 自动回滚

与普通 file_ops / repo_search 的区别：
- self_* 工具始终操作 bot 自己的仓库（SELF_REPO_OWNER / SELF_REPO_NAME）
- 当 SELF_REPO 与 GITHUB_REPO 相同时，两套工具操作同一个仓库

配套工具:
- server_ops: 查看运行日志、进程状态
- error_log: 查看运行时错误缓冲区
"""

from __future__ import annotations

import base64
import logging
import time

import httpx

from app.config import settings
from app.services.error_log import format_errors, clear_errors
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

_API_BASE = "https://api.github.com"

# 安全部署的 staging 分支名
_STAGING_BRANCH = "bot-fix"

# 回滚记录：上一次 safe_deploy 前 main 的 commit SHA
_rollback_sha: str = ""


def _validate_python_syntax(content: str, path: str) -> str | None:
    """对 .py 文件做语法检查，返回错误信息或 None（通过）"""
    if not path.endswith(".py"):
        return None
    try:
        compile(content, path, "exec")
        return None
    except SyntaxError as exc:
        return (
            f"[BLOCKED] Python 语法错误，拒绝推送！修复后重试。\n"
            f"  文件: {path}\n"
            f"  行号: {exc.lineno}\n"
            f"  错误: {exc.msg}\n"
            f"  代码: {exc.text.strip() if exc.text else '(无)'}"
        )


def _deep_validate_python(content: str, path: str) -> list[str]:
    """对 .py 文件做深度检查（超越语法）：检查 import 是否可解析、常见错误模式。

    返回问题列表（空 = 通过）。
    """
    issues: list[str] = []
    if not path.endswith(".py"):
        return issues

    # 1. 语法检查
    try:
        import ast
        tree = ast.parse(content, filename=path)
    except SyntaxError as exc:
        issues.append(f"语法错误: 行 {exc.lineno}: {exc.msg}")
        return issues  # 语法都不过，后面的检查没意义

    # 2. 检查 import 语句是否引用了项目中存在的模块
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
                # 只检查 app.* 内部导入（外部库依赖 requirements.txt）
                if mod.startswith("app."):
                    # 转换模块路径为文件路径并检查
                    mod_path = mod.replace(".", "/")
                    # 检查 mod_path.py 或 mod_path/__init__.py 是否存在
                    file_check = _self_get(f"/contents/{mod_path}.py", params={"ref": "main"})
                    dir_check = _self_get(f"/contents/{mod_path}/__init__.py", params={"ref": "main"})
                    if isinstance(file_check, str) and isinstance(dir_check, str):
                        # 两个都找不到，可能是有效的子模块导入，只发出警告
                        issues.append(f"疑似无效导入: 'from {mod} import ...' (行 {node.lineno})")

    # 3. 检查常见危险模式
    lines = content.split("\n")
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        # 检查 print() 调试语句（在生产代码中不应该有）
        # 不报错，只是提示
        # 检查 sys.exit() 调用
        if "sys.exit(" in stripped or "os._exit(" in stripped:
            issues.append(f"危险: 行 {i} 包含 exit 调用，可能导致 bot 崩溃")
        # 检查裸 except（吃掉所有异常）
        if stripped == "except:" and "pass" in lines[i] if i < len(lines) else False:
            issues.append(f"注意: 行 {i} 裸 except+pass 可能隐藏重要错误")

    return issues


def _headers() -> dict:
    return {
        "Authorization": f"token {settings.github.token}",
        "Accept": "application/vnd.github.v3+json",
    }


def _self_repo_url(path: str = "") -> str:
    owner = settings.self_repo_owner
    name = settings.self_repo_name
    return f"{_API_BASE}/repos/{owner}/{name}{path}"


def _self_get(path: str, params: dict | None = None):
    url = _self_repo_url(path)
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(url, headers=_headers(), params=params)
    except Exception as exc:
        logger.exception("_self_get connection error: %s", url)
        return f"[ERROR] GitHub API 连接失败 ({url}): {exc}"
    if resp.status_code >= 400:
        return f"[ERROR] {resp.status_code}: {resp.text[:500]}"
    return resp.json()


def _self_put(path: str, json_body: dict):
    url = _self_repo_url(path)
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.put(url, headers=_headers(), json=json_body)
    except Exception as exc:
        logger.exception("_self_put connection error: %s", url)
        return f"[ERROR] GitHub API 连接失败 ({url}): {exc}"
    if resp.status_code >= 400:
        return f"[ERROR] {resp.status_code}: {resp.text[:500]}"
    return resp.json()


def _self_post(path: str, json_body: dict, retries: int = 0):
    url = _self_repo_url(path)
    last_exc: Exception | None = None
    for attempt in range(1 + retries):
        try:
            with httpx.Client(timeout=60) as client:
                resp = client.post(url, headers=_headers(), json=json_body)
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                import time as _t
                _t.sleep(2 ** (attempt + 1))
                continue
            logger.exception("_self_post connection error: %s", url)
            return f"[ERROR] GitHub API 连接失败 ({url}): {exc}"
        if resp.status_code >= 400:
            return f"[ERROR] {resp.status_code}: {resp.text[:500]}"
        # 204 No Content (e.g. merge already up-to-date) — no JSON body
        if resp.status_code == 204:
            return {"status": 204, "message": "no content"}
        try:
            return resp.json()
        except Exception:
            return {"status": resp.status_code, "body": resp.text[:500]}
    return f"[ERROR] GitHub API 连接失败 (重试 {retries} 次): {last_exc}"


def _ensure_staging_branch() -> str | None:
    """确保 staging 分支存在。不存在则从 main 创建。返回错误信息或 None。"""
    # 检查分支是否已存在
    data = _self_get(f"/branches/{_STAGING_BRANCH}")
    if isinstance(data, dict) and "name" in data:
        return None  # 已存在

    # 获取 main 的最新 SHA
    main_data = _self_get("/git/ref/heads/main")
    if isinstance(main_data, str):
        return main_data
    main_sha = main_data.get("object", {}).get("sha", "")
    if not main_sha:
        return "[ERROR] 无法获取 main 分支 SHA"

    # 创建分支
    result = _self_post("/git/refs", {
        "ref": f"refs/heads/{_STAGING_BRANCH}",
        "sha": main_sha,
    })
    if isinstance(result, str):
        return result
    logger.info("created staging branch '%s' from main (%s)", _STAGING_BRANCH, main_sha[:7])
    return None


def _get_main_head_sha() -> str:
    """获取 main 分支当前 HEAD 的 commit SHA"""
    data = _self_get("/git/ref/heads/main")
    if isinstance(data, dict):
        return data.get("object", {}).get("sha", "")
    return ""


def _check_deploy_health(timeout_seconds: int = 180) -> tuple[bool, str]:
    """检查部署健康状态。

    阿里云 + GitHub Actions 部署：推送到 main 后 CI/CD 自动构建并重启容器。
    由于当前进程会在部署时被替换，健康检查由新进程的 startup_health_check 负责。
    这里直接返回成功。

    返回 (is_healthy, status_message)。
    """
    # Railway 向后兼容：如果还配了 Railway，用 Railway API 检查
    if settings.railway.api_token:
        from app.tools.railway_ops import get_deploy_status

        start = time.time()
        poll_interval = 15
        last_status = ""

        while time.time() - start < timeout_seconds:
            time.sleep(poll_interval)

            status_text = get_deploy_status(1)
            if isinstance(status_text, str) and status_text.startswith("[ERROR]"):
                logger.warning("deploy status check failed: %s", status_text)
                continue

            if "[SUCCESS]" in status_text:
                return True, f"部署成功: {status_text}"
            if "[CRASHED]" in status_text or "[FAILED]" in status_text:
                return False, f"部署失败: {status_text}"

            last_status = status_text
            logger.info("deploy status: %s (waiting...)", status_text[:100])

        return False, f"部署超时 ({timeout_seconds}s)，最后状态: {last_status}"

    # 阿里云 CI/CD：推送到 main 后 GitHub Actions 自动部署
    return True, "代码已推送到 main，CI/CD 将自动部署。重启后会执行启动健康检查。"


# ── 自我诊断工具 ──


def get_bot_errors(count: int = 20) -> ToolResult:
    """获取 bot 最近的运行时错误（内存缓冲区）"""
    return ToolResult.success(format_errors(count))


def clear_bot_errors() -> ToolResult:
    """清空错误缓冲区"""
    return ToolResult.success(clear_errors())


# ── 自我代码读写 ──


# 读文件返回的最大行数：超过则截断，避免 context 爆炸
_MAX_READ_LINES = 1000


def self_validate_file(path: str, branch: str = "") -> ToolResult:
    """深度验证 bot 仓库中的 Python 文件：语法 + 导入检查 + 危险模式检测。

    在推送到 main 前调用，发现问题可以先修。
    """
    if not branch:
        branch = "main"

    data = _self_get(f"/contents/{path}", params={"ref": branch})
    if isinstance(data, str):
        return ToolResult.api_error(data)
    if isinstance(data, list):
        return ToolResult.error(f"{path} 是目录", code="invalid_param")

    try:
        content = base64.b64decode(data.get("content", "")).decode("utf-8")
    except Exception:
        return ToolResult.api_error("无法解码文件")

    issues = _deep_validate_python(content, path)
    if not issues:
        return ToolResult.success(f"验证通过: {path} 没有发现问题。")

    lines = [f"验证发现 {len(issues)} 个问题：\n"]
    for issue in issues:
        lines.append(f"  - {issue}")
    return ToolResult.error("\n".join(lines), code="blocked")


def self_read_file(path: str, branch: str = "main", start_line: int = 0, end_line: int = 0) -> ToolResult:
    """读取 bot 自己仓库中的文件。支持行范围读取（避免大文件截断问题）。"""
    data = _self_get(f"/contents/{path}", params={"ref": branch})
    if isinstance(data, str):
        return ToolResult.api_error(data)
    if isinstance(data, list):
        items = [f"{'dir' if f['type'] == 'dir' else 'file'}: {f['name']}" for f in data]
        return ToolResult.success("\n".join(items))

    file_size = data.get("size", 0)
    if file_size > 1_000_000:
        return ToolResult.error(f"文件 {path} 大小 {file_size:,} 字节，超过 API 限制", code="api_error")

    content_b64 = data.get("content", "")
    try:
        content = base64.b64decode(content_b64).decode("utf-8")
    except Exception:
        return ToolResult.api_error("无法解码文件内容（可能是二进制文件）")

    lines = content.split("\n")
    total_lines = len(lines)

    # 如果指定了行范围
    if start_line > 0 or end_line > 0:
        start = max(0, start_line - 1)  # 转为 0-based
        end = min(total_lines, end_line) if end_line > 0 else total_lines
        selected = lines[start:end]
        # 带行号输出
        numbered = []
        for i, line in enumerate(selected, start=start + 1):
            numbered.append(f"{i:4d} | {line}")
        header = f"文件 {path} (行 {start + 1}-{min(end, total_lines)}/{total_lines}):\n"
        return ToolResult.success(header + "\n".join(numbered))

    # 默认：全文读取，超长截断
    if total_lines > _MAX_READ_LINES:
        truncated = "\n".join(lines[:_MAX_READ_LINES])
        return ToolResult.success(
            truncated
            + f"\n\n... (文件共 {total_lines} 行，仅展示前 {_MAX_READ_LINES} 行。"
            f"可用 start_line/end_line 参数读取特定范围，或用 self_search_code 搜索关键词)"
        )
    return ToolResult.success(content)


_BLOCKED_WRITE_PATHS = (
    ".github/", ".env", "tenants.json", "docker-compose",
    "Dockerfile", "deploy.yml", "instances/",
)


def _check_write_path(path: str) -> str | None:
    """检查写入路径是否安全，返回错误消息或 None（通过）"""
    for blocked in _BLOCKED_WRITE_PATHS:
        if path.startswith(blocked) or path == blocked or ("/" + blocked) in path:
            return (
                f"禁止写入 {path}（安全限制）。\n"
                f"以下路径不允许通过 self_write_file/self_edit_file 修改：\n"
                f"  {', '.join(_BLOCKED_WRITE_PATHS)}\n"
                f"如需修改基础设施文件，请联系管理员。"
            )
    return None


def self_write_file(path: str, content: str, message: str = "", branch: str = "") -> ToolResult:
    """写入 bot 自己仓库的文件（直接推到 main，CI/CD 自动部署）"""
    path_err = _check_write_path(path)
    if path_err:
        return ToolResult.blocked(path_err)
    if not branch:
        branch = "main"

    if not message:
        message = f"bot self-fix: update {path}"

    content_bytes = content.encode("utf-8")
    if len(content_bytes) > 1_000_000:
        return ToolResult.invalid_param(f"内容过大 ({len(content_bytes):,} 字节)，超过 API 限制")

    # 获取文件 sha（更新已有文件需要）
    existing = _self_get(f"/contents/{path}", params={"ref": branch})
    sha = None
    old_line_count = 0
    if isinstance(existing, dict) and "sha" in existing:
        sha = existing["sha"]
        # 读取旧文件行数，用于缩水保护
        try:
            old_content = base64.b64decode(existing.get("content", "")).decode("utf-8")
            old_line_count = len(old_content.split("\n"))
        except Exception:
            pass

    # ── 缩水保护：新文件行数不到旧文件 50% 时拒绝写入 ──
    # 阈值 50%（而非 80%）：合理的重构/清理可能减少 20-40% 行数，
    # 但减少超过一半通常说明文件被截断了。
    new_line_count = len(content.split("\n"))
    if old_line_count > 20 and new_line_count < old_line_count * 0.5:
        return ToolResult.blocked(
            f"写入被拒绝：文件 {path} 原有 {old_line_count} 行，"
            f"新内容仅 {new_line_count} 行（缩水 {100 - new_line_count * 100 // old_line_count}%）。"
            f"这通常意味着你只读取到了文件的前半部分。"
            f"推荐改用 self_edit_file 做精确替换，不需要完整文件内容。"
        )

    # ── 语法检查：.py 文件必须通过 compile() 才允许推送 ──
    syntax_err = _validate_python_syntax(content, path)
    if syntax_err:
        return ToolResult.blocked(syntax_err)

    payload: dict = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    result = _self_put(f"/contents/{path}", json_body=payload)
    if isinstance(result, str):
        return ToolResult.api_error(result)

    deploy_hint = ""
    if branch == "main":
        deploy_hint = "  已直接推送到 main，CI/CD 将自动部署。"
    return ToolResult.success(
        f"已将修复推送到 {branch} 分支: {path} ({len(content)} 字符, {new_line_count} 行)"
        f"{deploy_hint}"
    )


def self_edit_file(
    path: str,
    old_text: str,
    new_text: str,
    message: str = "",
    branch: str = "",
) -> ToolResult:
    """在 bot 自己仓库的文件中做 search-and-replace 编辑。

    比 self_write_file 安全：不需要完整文件内容，只需要知道要改的那段代码。
    直接推到 main 分支，CI/CD 自动部署。
    """
    path_err = _check_write_path(path)
    if path_err:
        return ToolResult.blocked(path_err)
    if not branch:
        branch = "main"

    if not message:
        message = f"bot self-fix: edit {path}"

    # 读取当前文件（从 staging 分支读，可能已有之前的修改）
    existing = _self_get(f"/contents/{path}", params={"ref": branch})
    if isinstance(existing, str):
        return ToolResult.api_error(existing)
    if isinstance(existing, list):
        return ToolResult.invalid_param(f"{path} 是目录，不是文件")

    sha = existing.get("sha")
    try:
        content = base64.b64decode(existing.get("content", "")).decode("utf-8")
    except Exception:
        return ToolResult.api_error("无法解码文件内容")

    # 查找并替换
    if old_text not in content:
        # 尝试忽略首尾空白匹配
        stripped = old_text.strip()
        if stripped and stripped in content:
            content = content.replace(stripped, new_text.strip(), 1)
        else:
            return ToolResult.not_found(
                f"在 {path} 中找不到要替换的文本片段。\n"
                f"请用 self_search_code 或 self_read_file 确认准确的代码内容后重试。\n"
                f"old_text 前 100 字符: {old_text[:100]}"
            )
    else:
        count = content.count(old_text)
        if count > 1:
            return ToolResult.invalid_param(
                f"old_text 在 {path} 中出现了 {count} 次，需要唯一匹配。"
                f"请提供更多上下文使其唯一。"
            )
        content = content.replace(old_text, new_text, 1)

    # ── 语法检查：.py 文件必须通过 compile() 才允许推送 ──
    syntax_err = _validate_python_syntax(content, path)
    if syntax_err:
        return ToolResult.blocked(syntax_err)

    content_bytes = content.encode("utf-8")
    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    result = _self_put(f"/contents/{path}", json_body=payload)
    if isinstance(result, str):
        return ToolResult.api_error(result)

    deploy_hint = ""
    if branch == "main":
        deploy_hint = "  已直接推送到 main，CI/CD 将自动部署。"
    return ToolResult.success(
        f"已编辑并推送: {path} (替换了 {len(old_text)} 字符 → {len(new_text)} 字符)"
        f"{deploy_hint}"
    )


_REVIEW_PROMPT = """\
你是一个严格的 code reviewer。下面是即将部署到生产环境的 diff。
请检查以下问题（只报告你确定存在的问题，不要猜测）：

1. **语法/运行时错误**：会导致 import 失败、启动崩溃的问题
2. **并发安全**：共享状态没有加锁、asyncio 竞态
3. **无限循环/递归**：可能导致进程挂死
4. **安全漏洞**：密钥泄露、注入、未验证的输入
5. **API 误用**：错误的参数、遗漏的错误处理

如果没有发现上述类型的问题，只回复一个词：LGTM
如果发现问题，用这个格式：
BLOCK: [问题描述]

只报告会导致生产故障的严重问题。代码风格、命名、注释不够好等不算。
"""


def _review_staging_diff() -> tuple[bool, str]:
    """获取 staging 分支的 diff 并用 LLM 做 code review。

    返回 (passed, review_text)。
    passed=True 表示 review 通过（LGTM 或无法获取 diff），
    passed=False 表示发现阻塞问题。
    """
    # 获取 diff
    data = _self_get(f"/compare/main...{_STAGING_BRANCH}")
    if isinstance(data, str):
        logger.warning("review: cannot get diff, skipping review: %s", data[:100])
        return True, "跳过 review（无法获取 diff）"

    files = data.get("files", [])
    if not files:
        return True, "没有文件变更"

    # 构建 diff 文本（限制长度避免超 token）
    diff_parts = []
    total_len = 0
    for f in files:
        patch = f.get("patch", "")
        filename = f.get("filename", "")
        chunk = f"--- {filename}\n{patch}\n"
        if total_len + len(chunk) > 12000:
            diff_parts.append(f"\n... 还有 {len(files) - len(diff_parts)} 个文件的 diff 被截断")
            break
        diff_parts.append(chunk)
        total_len += len(chunk)

    diff_text = "\n".join(diff_parts)

    # 调用 LLM review
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=settings.kimi.api_key,
            base_url=settings.kimi.base_url,
        )
        resp = client.chat.completions.create(
            model=settings.kimi.model,
            messages=[
                {"role": "system", "content": _REVIEW_PROMPT},
                {"role": "user", "content": diff_text},
            ],
            temperature=0,
            max_tokens=1024,
        )
        review = resp.choices[0].message.content.strip()
    except Exception as exc:
        logger.warning("review: LLM call failed, skipping: %s", exc)
        return True, f"跳过 review（LLM 调用失败: {exc}）"

    logger.info("code review result: %s", review[:200])

    if "LGTM" in review.upper() and "BLOCK" not in review.upper():
        return True, f"Code review 通过: {review[:200]}"

    if "BLOCK" in review.upper():
        return False, review

    # 模糊情况：有内容但没有明确 BLOCK → 通过但附带建议
    return True, f"Code review 建议（非阻塞）: {review[:300]}"


def _save_pending_task_for_restart() -> None:
    """保存当前用户任务到 Redis，让 bot 重启后能自动继续。

    从 in-flight 请求中获取当前正在处理的用户消息上下文，
    存入 Redis bot:pending_resume，重启后 _recover_missed_messages 读取并重新投递。
    """
    import json

    try:
        from app.webhook.handler import get_in_flight_messages
        in_flight = get_in_flight_messages()
        if not in_flight:
            logger.info("save_pending_task: no in-flight requests to save")
            return

        from app.services import redis_client as redis
        if not redis.available():
            logger.warning("save_pending_task: Redis not available")
            return

        # 保存所有 in-flight 请求 — 重启后逐个恢复
        resume_tasks = {}
        for msg_id, info in in_flight.items():
            resume_tasks[msg_id] = {
                "sender_id": info.get("sender_id", ""),
                "chat_id": info.get("chat_id", ""),
                "chat_type": info.get("chat_type", ""),
                "tenant_id": info.get("tenant_id", ""),
                "text_preview": info.get("text_preview", ""),
                "reason": "self_deploy",
            }

        redis.execute(
            "SET", "bot:pending_resume",
            json.dumps(resume_tasks, ensure_ascii=False),
            "EX", "600",  # 10 分钟 TTL
        )
        logger.info("save_pending_task: saved %d tasks for post-restart resume", len(resume_tasks))

    except Exception:
        logger.warning("save_pending_task: failed to save", exc_info=True)


def self_safe_deploy() -> ToolResult:
    """确认部署：代码已直推 main，保存当前任务上下文以便重启后继续。

    现在代码直接推到 main（不走 staging 分支），CI/CD 自动部署。
    这个工具的作用：
    1. 记录回滚点（main 当前 HEAD）
    2. 保存当前用户任务到 Redis，重启后自动继续未完成的工作
    3. 返回部署确认信息
    """
    global _rollback_sha

    # 记录回滚点
    main_sha = _get_main_head_sha()
    if main_sha:
        _rollback_sha = main_sha
        logger.info("safe_deploy: rollback point = %s", main_sha[:7])

    # 保存当前用户任务到 Redis，重启后自动继续
    _save_pending_task_for_restart()

    return ToolResult.success(
        "代码已推送到 main，CI/CD 将自动部署并重启服务。\n"
        "重启后我会自动继续完成之前的任务。\n"
        f"  回滚点: {main_sha[:7] if main_sha else '未知'}"
    )


def self_rollback() -> ToolResult:
    """手动回滚：将 main 恢复到上一次 safe_deploy 前的状态。"""
    if not _rollback_sha:
        return ToolResult.error("没有可回滚的记录。只有通过 self_safe_deploy 部署后才能回滚。", code="invalid_param")
    result = _do_rollback(_rollback_sha)
    if isinstance(result, str) and result.startswith("[ERROR]"):
        return ToolResult.api_error(result)
    return ToolResult.success(result)


def _do_rollback(target_sha: str) -> str:
    """将 main 分支强制重置到指定 commit SHA"""
    url = _self_repo_url("/git/refs/heads/main")
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.patch(
                url,
                headers=_headers(),
                json={"sha": target_sha, "force": True},
            )
        if resp.status_code >= 400:
            return f"[ERROR] 回滚失败: {resp.status_code}: {resp.text[:300]}"
        logger.info("rollback: main reset to %s", target_sha[:7])
        return f"已回滚 main 到 {target_sha[:7]}，CI/CD 将自动重新部署。"
    except Exception as exc:
        logger.exception("rollback failed")
        return f"[ERROR] 回滚失败: {exc}"


def _cleanup_staging_branch() -> None:
    """删除 staging 分支（下次修复重新创建）"""
    url = _self_repo_url(f"/git/refs/heads/{_STAGING_BRANCH}")
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.delete(url, headers=_headers())
        if resp.status_code < 400:
            logger.info("cleaned up staging branch '%s'", _STAGING_BRANCH)
    except Exception:
        logger.warning("failed to cleanup staging branch", exc_info=True)


def startup_rollback_check() -> str | None:
    """启动时检查：如果上一次部署是失败的 self-fix，自动回滚。

    容器崩溃后 Docker 会重启（restart: unless-stopped）。
    如果崩溃是由 bot self-fix 造成的，这个函数在重启时自动回滚到上一个好的 commit。

    返回回滚信息或 None（无需回滚）。
    """
    if not settings.github.token:
        return None

    # 检查最新 commit 是否是 bot self-fix
    commits = _self_get("/commits", params={"per_page": 2})
    if isinstance(commits, str) or not isinstance(commits, list) or len(commits) < 2:
        return None

    latest = commits[0]
    previous = commits[1]
    msg = latest.get("commit", {}).get("message", "")

    # 只处理 bot 自己的 commit
    if not msg.startswith("bot self-fix:") and not msg.startswith("bot safe-deploy:"):
        return None

    # 检查方式 1：Railway API（向后兼容）
    if settings.railway.api_token:
        from app.tools.railway_ops import get_deploy_status
        status_text = get_deploy_status(1)
        if "[CRASHED]" in status_text or "[FAILED]" in status_text:
            prev_sha = previous["sha"]
            logger.warning(
                "startup_rollback: last deploy crashed after self-fix commit '%s', "
                "rolling back to %s",
                msg[:50], prev_sha[:7],
            )
            result = _do_rollback(prev_sha)
            return f"启动时检测到上次 self-fix 导致崩溃，已自动回滚。\n  崩溃 commit: {msg[:60]}\n  {result}"

    # 检查方式 2：本地日志文件（阿里云部署）
    import os
    log_file = os.getenv("BOT_LOG_FILE", "/app/logs/bot.log")
    if os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            # 检查最后 50 行是否有崩溃信号
            tail = "".join(lines[-50:])
            crash_signals = ["SyntaxError", "ImportError", "ModuleNotFoundError", "CRASHED"]
            if any(sig in tail for sig in crash_signals):
                prev_sha = previous["sha"]
                logger.warning(
                    "startup_rollback: crash detected in logs after self-fix commit '%s', "
                    "rolling back to %s",
                    msg[:50], prev_sha[:7],
                )
                result = _do_rollback(prev_sha)
                return f"启动时检测到上次 self-fix 导致崩溃，已自动回滚。\n  崩溃 commit: {msg[:60]}\n  {result}"
        except Exception:
            logger.warning("startup_rollback: failed to check log file", exc_info=True)

    return None


def self_list_tree(path: str = "") -> ToolResult:
    """浏览 bot 自己仓库的目录结构"""
    data = _self_get(f"/contents/{path}", params={"ref": "main"})
    if isinstance(data, str):
        return ToolResult.api_error(data)
    if isinstance(data, list):
        dirs = []
        files = []
        for item in data:
            if item["type"] == "dir":
                dirs.append(f"dir: {item['name']}/")
            else:
                size = item.get("size", 0)
                size_str = f" ({size // 1024}KB)" if size > 1024 else ""
                files.append(f"file: {item['name']}{size_str}")
        dirs.sort()
        files.sort()
        return ToolResult.success("\n".join(dirs + files) or f"目录 '{path}' 为空")
    return ToolResult.success(str(data))


def self_search_code(query: str) -> ToolResult:
    """在 bot 自己仓库中搜索代码"""
    owner = settings.self_repo_owner
    name = settings.self_repo_name
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(
                f"{_API_BASE}/search/code",
                headers={
                    **_headers(),
                    "Accept": "application/vnd.github.v3.text-match+json",
                },
                params={"q": f"{query} repo:{owner}/{name}", "per_page": 15},
            )
        if resp.status_code >= 400:
            return ToolResult.api_error(f"{resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        items = data.get("items", [])
        if not items:
            return ToolResult.success(f"在 bot 代码中没有找到 '{query}' 的匹配。")
        lines = [f"找到 {data.get('total_count', len(items))} 处匹配：\n"]
        for item in items:
            lines.append(f"  {item['path']}")
            for match in item.get("text_matches", []):
                fragment = match.get("fragment", "").strip()
                if fragment:
                    lines.append(f"    > {fragment[:200]}")
            lines.append("")
        return ToolResult.success("\n".join(lines))
    except Exception as exc:
        logger.exception("self_search_code failed")
        return ToolResult.api_error(f"搜索失败: {exc}")


def self_git_log(count: int = 10) -> ToolResult:
    """查看 bot 自己仓库的最近提交记录"""
    data = _self_get("/commits", params={"per_page": count})
    if isinstance(data, str):
        return ToolResult.api_error(data)
    lines = []
    for c in data:
        sha = c["sha"][:7]
        msg = c["commit"]["message"].split("\n")[0]
        author = c["commit"]["author"]["name"]
        date = c["commit"]["author"]["date"][:10]
        lines.append(f"{sha} {date} [{author}] {msg}")
    return ToolResult.success("\n".join(lines) or "没有提交记录。")


# ── 自我知识库管理 ──

import os as _os

_KNOWLEDGE_FILE = _os.path.join(
    _os.path.dirname(__file__), "..", "knowledge", "self_awareness.md"
)


def read_self_knowledge() -> ToolResult:
    """读取 bot 的自我认知知识库（self_awareness.md）"""
    try:
        with open(_KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        return ToolResult.success(content)
    except FileNotFoundError:
        return ToolResult.success("知识库文件不存在。将在首次写入时自动创建。")


def update_self_knowledge(entry: str, section: str = "") -> ToolResult:
    """向知识库追加一条新经验/已知坑。

    Parameters
    ----------
    entry : 要追加的内容（一行或多行，markdown 格式）
    section : 追加到哪个 section 标题下（如 "飞书 API 已知坑"）。
              留空则追加到文件末尾。
    """
    if not entry or not entry.strip():
        return ToolResult.invalid_param("entry 不能为空")

    # 确保条目以 `- ` 开头（列表格式）
    entry = entry.strip()
    if not entry.startswith("- "):
        entry = "- " + entry

    try:
        existing = ""
        if _os.path.exists(_KNOWLEDGE_FILE):
            with open(_KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
                existing = f.read()

        if section:
            # 找到指定 section，在其末尾追加
            marker = f"## {section}"
            idx = existing.find(marker)
            if idx == -1:
                # section 不存在，创建新 section
                new_content = existing.rstrip() + f"\n\n## {section}\n\n{entry}\n"
            else:
                # 找到下一个 ## 或文件末尾
                next_section = existing.find("\n## ", idx + len(marker))
                if next_section == -1:
                    # 追加到文件末尾
                    new_content = existing.rstrip() + f"\n{entry}\n"
                else:
                    # 插入到下一个 section 之前
                    new_content = (
                        existing[:next_section].rstrip()
                        + f"\n{entry}\n"
                        + existing[next_section:]
                    )
        else:
            new_content = existing.rstrip() + f"\n{entry}\n"

        # 写回文件
        _os.makedirs(_os.path.dirname(_KNOWLEDGE_FILE), exist_ok=True)
        with open(_KNOWLEDGE_FILE, "w", encoding="utf-8") as f:
            f.write(new_content)

        # 同时推到 GitHub（通过 self_edit_file 的底层机制）
        _push_knowledge_to_github(new_content)

        return ToolResult.success(
            f"已追加到知识库{f' [{section}]' if section else ''}。"
            f"下次对话将自动加载新内容。"
        )
    except Exception as exc:
        logger.exception("update_self_knowledge failed")
        return ToolResult.error(f"更新知识库失败: {exc}")


def _push_knowledge_to_github(content: str) -> None:
    """将知识库同步推送到 GitHub，确保部署后不丢失。"""
    path = "app/knowledge/self_awareness.md"
    token = settings.github.token
    owner = settings.self_repo_owner
    name = settings.self_repo_name
    if not all([token, owner, name]):
        return

    url = f"{_API_BASE}/repos/{owner}/{name}/contents/{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    try:
        # 获取当前文件 SHA（更新需要）
        resp = httpx.get(url, headers=headers, params={"ref": "main"}, timeout=15)
        sha = resp.json().get("sha", "") if resp.status_code == 200 else ""

        payload = {
            "message": "bot: update self-awareness knowledge base",
            "content": base64.b64encode(content.encode()).decode(),
            "branch": "main",
        }
        if sha:
            payload["sha"] = sha

        httpx.put(url, headers=headers, json=payload, timeout=15)
    except Exception:
        logger.debug("push knowledge to github failed (non-critical)", exc_info=True)


# ── 工具注册 ──

TOOL_DEFINITIONS = [
    {
        "name": "get_bot_errors",
        "description": (
            "获取 bot 最近的运行时错误日志（内存缓冲区）。"
            "当怀疑自己工作不正常、或用户反馈 bot 出错时，先用这个工具查看最近的错误。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "查看最近几条错误，默认20",
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "clear_bot_errors",
        "description": "清空 bot 错误缓冲区。修复完问题后可以清空，方便观察新错误。",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "self_read_file",
        "description": (
            "读取 bot 自己源代码仓库中的文件。支持行范围读取（大文件可以分段看）。"
            "用于自我诊断：查看出错的代码、理解代码逻辑。"
            "也可以传目录路径查看目录列表。"
            "大文件建议用 start_line/end_line 参数读取特定范围。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件相对路径，如 app/webhook/handler.py",
                },
                "branch": {
                    "type": "string",
                    "description": "分支名，默认 main",
                    "default": "main",
                },
                "start_line": {
                    "type": "integer",
                    "description": "起始行号（从 1 开始），不填则从头读取",
                    "default": 0,
                },
                "end_line": {
                    "type": "integer",
                    "description": "结束行号（包含），不填则读到末尾",
                    "default": 0,
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "self_validate_file",
        "description": (
            "深度验证 bot 仓库中的 Python 文件：语法检查 + 导入验证 + 危险模式检测。"
            "在推送代码前调用，提前发现潜在问题。"
            "默认从 main 分支读取。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件相对路径，如 app/tools/calendar_ops.py",
                },
                "branch": {
                    "type": "string",
                    "description": "分支名，默认 main",
                    "default": "",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "self_write_file",
        "description": (
            "修改 bot 自己源代码仓库中的文件。"
            "直接推送到 main 分支，CI/CD 自动部署。"
            "修改完成后，调用 self_safe_deploy 记录回滚点并确认部署。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件相对路径，如 app/tools/calendar_ops.py",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的完整文件内容",
                },
                "message": {
                    "type": "string",
                    "description": "commit message，描述修复了什么问题",
                    "default": "",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "self_edit_file",
        "description": (
            "在 bot 自己源代码中做精确的 search-and-replace 编辑（推荐！比 self_write_file 更安全）。"
            "只需提供要替换的旧代码片段和新代码片段，不需要整个文件内容。"
            "适合小范围修复：改一个函数、修一个 bug、加几行代码。"
            "直接推到 main 分支，CI/CD 自动部署。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件相对路径",
                },
                "old_text": {
                    "type": "string",
                    "description": "要被替换的原文代码片段（必须在文件中唯一匹配）",
                },
                "new_text": {
                    "type": "string",
                    "description": "替换后的新代码片段",
                },
                "message": {
                    "type": "string",
                    "description": "commit message",
                    "default": "",
                },
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "self_safe_deploy",
        "description": (
            "确认部署：代码已直推 main，CI/CD 自动部署。"
            "记录回滚点，保存当前任务上下文以便重启后自动继续未完成的工作。"
            "在 self_write_file 或 self_edit_file 之后调用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "self_rollback",
        "description": (
            "手动回滚：将 main 恢复到上一次 self_safe_deploy 之前的状态。"
            "用于部署后发现运行时问题但自动回滚未触发的情况。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "self_list_tree",
        "description": "浏览 bot 自己源代码仓库的目录结构，了解项目布局。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "目录路径，留空表示根目录",
                    "default": "",
                },
            },
        },
    },
    {
        "name": "self_search_code",
        "description": "在 bot 自己的源代码中搜索关键词。用于查找出错的函数、变量、导入等。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，如 'def handle_message' 或 'RateLimitError'",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "self_git_log",
        "description": "查看 bot 自己仓库的最近提交记录。用于了解最近做了什么改动。",
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "显示多少条提交，默认10",
                    "default": 10,
                },
            },
        },
    },
    {
        "name": "read_self_knowledge",
        "description": (
            "读取 bot 的自我认知知识库（已知坑、诊断经验、架构认知）。"
            "想查看或确认知识库中有什么内容时使用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "update_self_knowledge",
        "description": (
            "向知识库追加一条新经验或已知坑。"
            "当你通过诊断发现了一个新的 API 限制、常见错误模式、或排查技巧时，"
            "用这个工具记录下来，下次遇到同类问题就能直接避坑。"
            "内容会自动同步到 GitHub，部署后不丢失。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entry": {
                    "type": "string",
                    "description": "要追加的经验条目（如 '飞书任务 API 的 due 字段必须是 UTC 时间戳'）",
                },
                "section": {
                    "type": "string",
                    "description": (
                        "追加到哪个 section（如 '飞书 API 已知坑'、'常见排查路径'）。"
                        "留空则追加到文件末尾。"
                    ),
                    "default": "",
                },
            },
            "required": ["entry"],
        },
    },
]

# 辅助函数：检查必需参数
def _check_required(args: dict, required: list[str]) -> ToolResult | None:
    """检查必需参数是否存在，返回错误信息或 None"""
    for key in required:
        if key not in args:
            return ToolResult.invalid_param(f"缺少必需参数 '{key}'")
    return None

TOOL_MAP = {
    "get_bot_errors": lambda args: get_bot_errors(args.get("count", 20)),
    "clear_bot_errors": lambda args: clear_bot_errors(),
    "self_read_file": lambda args: (
        (error := _check_required(args, ["path"])) and error
    ) or self_read_file(
        args["path"], args.get("branch", "main"),
        args.get("start_line", 0), args.get("end_line", 0),
    ),
    "self_validate_file": lambda args: (
        (error := _check_required(args, ["path"])) and error
    ) or self_validate_file(args["path"], args.get("branch", "")),
    "self_write_file": lambda args: (
        (error := _check_required(args, ["path", "content"])) and error
    ) or self_write_file(
        args["path"], args["content"], args.get("message", ""),
    ),
    "self_edit_file": lambda args: (
        (error := _check_required(args, ["path", "old_text", "new_text"])) and error
    ) or self_edit_file(
        args["path"], args["old_text"], args["new_text"],
        args.get("message", ""),
    ),
    "self_safe_deploy": lambda args: self_safe_deploy(),
    "self_rollback": lambda args: self_rollback(),
    "self_list_tree": lambda args: self_list_tree(args.get("path", "")),
    "self_search_code": lambda args: (
        (error := _check_required(args, ["query"])) and error
    ) or self_search_code(args["query"]),
    "self_git_log": lambda args: self_git_log(args.get("count", 10)),
    "read_self_knowledge": lambda args: read_self_knowledge(),
    "update_self_knowledge": lambda args: (
        (error := _check_required(args, ["entry"])) and error
    ) or update_self_knowledge(args["entry"], args.get("section", "")),
}
