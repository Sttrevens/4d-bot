"""容器级沙箱 —— NanoClaw 启发的 OS 级隔离执行

核心理念（借鉴 NanoClaw）：
- 代码执行在独立 Docker 容器中运行，OS 级隔离
- 比应用层 AST 检查 + import 白名单更安全
- 容器有内存限制（64MB）、网络隔离、只读文件系统
- 执行超时后强制 kill 容器

降级策略：
- Docker 可用 → 容器隔离执行
- Docker 不可用 → 回退到现有 sandbox.py（进程级隔离）

使用：
    from app.services.container_sandbox import execute_in_container
    result = await execute_in_container(code, timeout=30)
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── 配置 ──
_CONTAINER_IMAGE = "python:3.12-slim"
_MEMORY_LIMIT = "64m"
_CPU_LIMIT = "0.5"
_DEFAULT_TIMEOUT = 30
_NETWORK_DISABLED = True

# ── Docker 可用性检测（启动时检测一次）──
_docker_available: bool | None = None


def _check_docker() -> bool:
    """检测 Docker 是否可用（同步，启动时调用一次）。"""
    global _docker_available
    if _docker_available is not None:
        return _docker_available

    docker_path = shutil.which("docker")
    if not docker_path:
        logger.info("container_sandbox: docker not found, will use process sandbox")
        _docker_available = False
        return False

    import subprocess
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=5
        )
        _docker_available = result.returncode == 0
    except Exception:
        _docker_available = False

    if _docker_available:
        logger.info("container_sandbox: docker available, container isolation enabled")
    else:
        logger.info("container_sandbox: docker not accessible, will use process sandbox")
    return _docker_available


def is_container_sandbox_available() -> bool:
    """检查容器沙箱是否可用。"""
    return _check_docker()


async def execute_in_container(
    code: str,
    timeout: int = _DEFAULT_TIMEOUT,
    allowed_modules: list[str] | None = None,
    env_vars: dict[str, str] | None = None,
) -> dict[str, Any]:
    """在隔离的 Docker 容器中执行 Python 代码。

    Args:
        code: 要执行的 Python 代码
        timeout: 超时秒数
        allowed_modules: 允许 pip install 的额外模块（容器内）
        env_vars: 传入容器的环境变量

    Returns:
        {"success": bool, "output": str, "error": str, "duration_ms": int}
    """
    if not _check_docker():
        return await _fallback_process_sandbox(code, timeout)

    # 创建临时目录放代码文件
    with tempfile.TemporaryDirectory(prefix="sandbox_") as tmpdir:
        code_file = Path(tmpdir) / "run.py"
        code_file.write_text(code, encoding="utf-8")

        # 构建 docker run 命令
        cmd = [
            "docker", "run",
            "--rm",                          # 执行完自动清理
            f"--memory={_MEMORY_LIMIT}",     # 内存限制
            f"--cpus={_CPU_LIMIT}",          # CPU 限制
            "--read-only",                   # 只读文件系统
            "--tmpfs", "/tmp:size=10m",      # 临时目录（有大小限制）
            "--security-opt", "no-new-privileges",  # 禁止提权
            "--pids-limit", "32",            # 限制进程数
        ]

        if _NETWORK_DISABLED:
            cmd.append("--network=none")     # 网络隔离

        # 环境变量
        if env_vars:
            for k, v in env_vars.items():
                cmd.extend(["-e", f"{k}={v}"])

        # 挂载代码文件
        cmd.extend(["-v", f"{code_file}:/sandbox/run.py:ro"])

        # 镜像和执行命令
        cmd.extend([_CONTAINER_IMAGE, "python", "/sandbox/run.py"])

        import time
        start = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout + 5  # 给 Docker 启动一点额外时间
            )
            duration_ms = int((time.monotonic() - start) * 1000)

            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode == 0:
                return {
                    "success": True,
                    "output": stdout_str,
                    "error": "",
                    "duration_ms": duration_ms,
                    "isolation": "container",
                }
            elif proc.returncode == 137:  # OOM killed
                return {
                    "success": False,
                    "output": stdout_str,
                    "error": "内存超限 (OOM)：代码使用了超过 64MB 内存",
                    "duration_ms": duration_ms,
                    "isolation": "container",
                }
            else:
                return {
                    "success": False,
                    "output": stdout_str,
                    "error": stderr_str or f"退出码: {proc.returncode}",
                    "duration_ms": duration_ms,
                    "isolation": "container",
                }

        except asyncio.TimeoutError:
            # 超时 → 强制停止容器
            duration_ms = int((time.monotonic() - start) * 1000)
            # docker run --rm 在进程被 kill 后会自动清理容器
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            return {
                "success": False,
                "output": "",
                "error": f"执行超时（{timeout}秒）",
                "duration_ms": duration_ms,
                "isolation": "container",
            }
        except Exception as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.warning("container_sandbox exec error: %s", e, exc_info=True)
            return {
                "success": False,
                "output": "",
                "error": f"容器执行异常: {e}",
                "duration_ms": duration_ms,
                "isolation": "container",
            }


async def _fallback_process_sandbox(code: str, timeout: int) -> dict[str, Any]:
    """Docker 不可用时回退到进程级沙箱。"""
    try:
        from app.tools.sandbox import run_tool_code
        from app.tools.tool_result import ToolResult

        result = run_tool_code(code, func_name="main", func_args={})
        if isinstance(result, ToolResult):
            return {
                "success": result.success,
                "output": result.data if result.success else "",
                "error": "" if result.success else result.data,
                "duration_ms": 0,
                "isolation": "process",
            }
        return {
            "success": True,
            "output": str(result),
            "error": "",
            "duration_ms": 0,
            "isolation": "process",
        }
    except Exception as e:
        return {
            "success": False,
            "output": "",
            "error": str(e),
            "duration_ms": 0,
            "isolation": "process",
        }


async def execute_browser_in_container(
    script: str,
    timeout: int = 60,
) -> dict[str, Any]:
    """在带 Playwright 的容器中执行浏览器自动化脚本。

    使用更大的资源限制（浏览器需要更多内存）。
    """
    if not _check_docker():
        return {
            "success": False,
            "output": "",
            "error": "容器沙箱不可用，浏览器隔离需要 Docker",
            "isolation": "none",
        }

    with tempfile.TemporaryDirectory(prefix="browser_sandbox_") as tmpdir:
        code_file = Path(tmpdir) / "run.py"
        code_file.write_text(script, encoding="utf-8")

        cmd = [
            "docker", "run",
            "--rm",
            "--memory=512m",         # 浏览器需要更多内存
            "--cpus=1",
            "--tmpfs", "/tmp:size=100m",
            "--security-opt", "no-new-privileges",
            "--shm-size=256m",       # Chrome 需要共享内存
            "-v", f"{code_file}:/sandbox/run.py:ro",
            "mcr.microsoft.com/playwright/python:v1.40.0-jammy",
            "python", "/sandbox/run.py",
        ]

        import time
        start = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout + 10
            )
            duration_ms = int((time.monotonic() - start) * 1000)

            return {
                "success": proc.returncode == 0,
                "output": stdout.decode("utf-8", errors="replace").strip(),
                "error": stderr.decode("utf-8", errors="replace").strip() if proc.returncode != 0 else "",
                "duration_ms": duration_ms,
                "isolation": "container",
            }
        except asyncio.TimeoutError:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            return {
                "success": False,
                "output": "",
                "error": f"浏览器执行超时（{timeout}秒）",
                "duration_ms": int((time.monotonic() - start) * 1000),
                "isolation": "container",
            }
        except Exception as e:
            return {
                "success": False,
                "output": "",
                "error": str(e),
                "duration_ms": int((time.monotonic() - start) * 1000),
                "isolation": "container",
            }
