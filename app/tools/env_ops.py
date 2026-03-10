"""动态包管理工具 —— 打通「依赖墙」

让 bot 能在运行时安装新的 Python 包并扩展沙箱能力。
当 bot 发现缺少某个库时，可以自主安装并立即在自定义工具中使用。

安全策略：
1. 包名字符集校验（防止命令注入）
2. 危险包黑名单拦截（SSH、网络攻击、系统操作等）
3. pip install 隔离执行（subprocess，不影响当前进程）
4. 安装后写入 Redis 动态白名单（沙箱 import 校验读取）
5. 重启后通过 Redis 恢复白名单（不丢失）
"""

from __future__ import annotations

import importlib
import logging
import re
import subprocess
import time

from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

# ── 安全：危险包黑名单 ──

_BLOCKED_PACKAGES = frozenset({
    # 远程访问 / shell 控制
    "paramiko", "fabric", "fabric2", "pexpect", "sh", "plumbum",
    "ansible", "salt", "invoke",
    # 网络扫描 / 攻击
    "scapy", "impacket", "python-nmap", "nmap", "masscan",
    # 系统级操作
    "pyinstaller", "cx-freeze", "py2exe", "nuitka",
    # 提权 / 逃逸
    "docker", "kubernetes", "boto3", "azure-mgmt-compute",
    "google-cloud-compute",
})

# 包名合法字符（PyPI 规范：字母、数字、连字符、下划线、点）
_VALID_PACKAGE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

# 版本约束合法字符
_VALID_VERSION_SPEC = re.compile(r"^[a-zA-Z0-9_.,-<>=!~*\[\] ]+$")

# pip install 超时（秒）
_PIP_TIMEOUT = 120


# ── Redis 动态白名单 ──

def _redis_key(tenant_id: str) -> str:
    return f"sandbox:dynamic_modules:{tenant_id}"


def _get_tenant_id() -> str:
    """获取当前租户 ID"""
    try:
        from app.tenant.context import get_current_tenant
        tenant = get_current_tenant()
        return tenant.tenant_id or ""
    except Exception:
        return ""


def _add_to_whitelist(tenant_id: str, module_name: str) -> None:
    """将模块名加入 Redis 动态白名单"""
    try:
        from app.services.redis_client import execute
        execute("SADD", _redis_key(tenant_id), module_name)
    except Exception as e:
        logger.warning("env_ops: 写入 Redis 动态白名单失败: %s", e)


def _remove_from_whitelist(tenant_id: str, module_name: str) -> None:
    """将模块名从 Redis 动态白名单移除"""
    try:
        from app.services.redis_client import execute
        execute("SREM", _redis_key(tenant_id), module_name)
    except Exception as e:
        logger.warning("env_ops: 从 Redis 动态白名单移除失败: %s", e)


def _get_whitelist(tenant_id: str) -> list[str]:
    """获取 Redis 中的动态白名单"""
    try:
        from app.services.redis_client import execute
        result = execute("SMEMBERS", _redis_key(tenant_id))
        return sorted(result) if result else []
    except Exception:
        return []


# ── 工具实现 ──

def _handle_install_package(args: dict) -> ToolResult:
    """安装 Python 包并加入沙箱动态白名单"""
    package_name = args.get("package_name", "").strip().lower()
    module_name = args.get("module_name", "").strip() or package_name.replace("-", "_")
    version = args.get("version", "").strip()

    # 1. 参数校验
    if not package_name:
        return ToolResult.invalid_param("package_name 不能为空")
    if not _VALID_PACKAGE_NAME.match(package_name):
        return ToolResult.invalid_param(
            f"包名 '{package_name}' 格式无效（只允许字母、数字、连字符、下划线、点）"
        )
    if version and not _VALID_VERSION_SPEC.match(version):
        return ToolResult.invalid_param(f"版本约束 '{version}' 格式无效")

    # 2. 黑名单检查
    if package_name in _BLOCKED_PACKAGES:
        return ToolResult.blocked(
            f"包 '{package_name}' 在安全黑名单中，禁止安装。\n"
            f"原因：该包可能被用于系统访问、网络攻击或权限提升。"
        )

    # 3. 检查是否已安装
    try:
        importlib.import_module(module_name)
        # 已安装，确保在白名单中
        tenant_id = _get_tenant_id()
        if tenant_id:
            _add_to_whitelist(tenant_id, module_name)
        return ToolResult.success(
            f"包 '{package_name}'（模块名: {module_name}）已安装，"
            f"已加入沙箱白名单。"
        )
    except ImportError:
        pass  # 未安装，继续安装流程

    # 4. pip install
    pip_spec = f"{package_name}{version}" if version else package_name
    cmd = ["pip", "install", "--no-input", pip_spec]

    logger.info("env_ops: installing package: %s", pip_spec)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_PIP_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return ToolResult.error(f"安装超时（{_PIP_TIMEOUT}秒），包可能太大或网络不通")
    except Exception as e:
        return ToolResult.error(f"安装失败: {e}")

    if result.returncode != 0:
        stderr = result.stderr[-500:] if result.stderr else "(无错误输出)"
        return ToolResult.error(f"pip install 失败:\n{stderr}")

    # 5. 验证安装成功
    try:
        importlib.import_module(module_name)
    except ImportError:
        return ToolResult.error(
            f"pip install 成功但 import {module_name} 失败。\n"
            f"可能模块名与包名不同，请用 module_name 参数指定正确的 import 名。\n"
            f"例：包名 'beautifulsoup4' → module_name='bs4'"
        )

    # 6. 写入 Redis 动态白名单
    tenant_id = _get_tenant_id()
    if tenant_id:
        _add_to_whitelist(tenant_id, module_name)

    # 7. 特殊包的额外提示
    extra_hints = ""
    if package_name == "playwright":
        extra_hints = (
            "\n\n⚠️ Playwright 还需要安装浏览器引擎。\n"
            "请让管理员在服务器上运行：playwright install --with-deps chromium\n"
            "或使用 guide_human 工具引导管理员操作。"
        )

    return ToolResult.success(
        f"✓ 包 '{package_name}' 安装成功！\n"
        f"模块名: {module_name}（可在自定义工具中 import {module_name}）\n"
        f"已加入沙箱动态白名单。{extra_hints}"
    )


def _handle_list_dynamic_packages(args: dict) -> ToolResult:
    """列出当前租户已动态安装的包"""
    tenant_id = _get_tenant_id()
    if not tenant_id:
        return ToolResult.error("无法获取当前租户信息")

    modules = _get_whitelist(tenant_id)
    if not modules:
        return ToolResult.success(
            "当前没有动态安装的包。\n"
            "使用 install_package 工具安装新的 Python 包。"
        )

    # 检查每个模块是否仍然可用
    lines = []
    for mod in modules:
        try:
            importlib.import_module(mod)
            lines.append(f"  ✓ {mod}")
        except ImportError:
            lines.append(f"  ✗ {mod}（未安装或已卸载）")

    return ToolResult.success(
        f"动态安装的包（{len(modules)} 个）：\n" + "\n".join(lines)
    )


def _handle_uninstall_package(args: dict) -> ToolResult:
    """卸载动态安装的包"""
    package_name = args.get("package_name", "").strip().lower()
    module_name = args.get("module_name", "").strip() or package_name.replace("-", "_")

    if not package_name:
        return ToolResult.invalid_param("package_name 不能为空")

    tenant_id = _get_tenant_id()

    # 从白名单移除
    if tenant_id:
        _remove_from_whitelist(tenant_id, module_name)

    # pip uninstall
    cmd = ["pip", "uninstall", "-y", package_name]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return ToolResult.error(f"卸载失败: {result.stderr[-300:]}")
    except Exception as e:
        return ToolResult.error(f"卸载失败: {e}")

    return ToolResult.success(
        f"✓ 包 '{package_name}' 已卸载，模块 '{module_name}' 已从沙箱白名单移除。"
    )


# ── 工具注册（标准接口）──

TOOL_DEFINITIONS = [
    {
        "name": "install_package",
        "description": (
            "安装 Python 包并加入沙箱白名单。安装后可在 create_custom_tool 的代码中 import 使用。"
            "例：install_package('playwright') 后可在自定义工具中 import playwright。"
            "注意：某些包的 import 名与包名不同，需用 module_name 指定（如 beautifulsoup4 → bs4）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "package_name": {
                    "type": "string",
                    "description": "PyPI 包名（如 'pandas', 'playwright', 'pillow'）",
                },
                "module_name": {
                    "type": "string",
                    "description": "import 时的模块名（与包名不同时必填，如 beautifulsoup4 → bs4, Pillow → PIL）",
                },
                "version": {
                    "type": "string",
                    "description": "版本约束（可选，如 '>=1.0,<2', '==3.2.1'）",
                },
            },
            "required": ["package_name"],
        },
    },
    {
        "name": "list_dynamic_packages",
        "description": "列出已动态安装的 Python 包及其状态",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "uninstall_package",
        "description": "卸载动态安装的 Python 包并从沙箱白名单移除",
        "input_schema": {
            "type": "object",
            "properties": {
                "package_name": {
                    "type": "string",
                    "description": "要卸载的包名",
                },
                "module_name": {
                    "type": "string",
                    "description": "import 时的模块名（与包名不同时需指定）",
                },
            },
            "required": ["package_name"],
        },
    },
]

TOOL_MAP = {
    "install_package": _handle_install_package,
    "list_dynamic_packages": _handle_list_dynamic_packages,
    "uninstall_package": _handle_uninstall_package,
}
