"""沙箱执行器 —— 安全加载并运行 bot 动态生成的自定义工具代码

安全策略：
1. import 白名单 —— 只允许安全的标准库 + 有限第三方库 + 动态安装的包
2. 内建函数白名单 —— 禁止 open / exec / eval / __import__ 等
3. 执行超时 —— 每个工具调用最多 30 秒
4. 结果归一化 —— 确保返回 ToolResult
5. 动态白名单 —— install_package 安装的包通过 Redis 持久化，重启不丢失
"""

from __future__ import annotations

import ast
import importlib
import logging
import signal
import time
import types
from typing import Any, Callable

from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

# ── import 白名单 ──

_ALLOWED_MODULES: frozenset[str] = frozenset({
    # 标准库（只读 / 纯计算）
    "json", "re", "time", "datetime", "math", "statistics",
    "hashlib", "hmac", "base64", "urllib.parse", "html",
    "collections", "itertools", "functools", "operator",
    "string", "textwrap", "difflib", "uuid", "copy",
    "csv", "io",
    # 第三方（解析）
    "bs4",
    # 项目内
    "app.tools.tool_result",
    "app.tools.sandbox_caps",  # 沙箱能力原语（视频下载、Gemini 分析等）
})

# ── 动态白名单（install_package 安装的包，从 Redis 加载）──

_dynamic_modules_cache: dict[str, tuple[float, frozenset[str]]] = {}  # tenant_id → (timestamp, modules)
_DYNAMIC_CACHE_TTL = 60  # 缓存 60 秒


def _get_dynamic_modules() -> frozenset[str]:
    """获取当前租户的动态白名单模块（带缓存）。

    install_package 安装的包会写入 Redis，这里读取并缓存。
    fail-open：Redis 不可用时返回空集合，不影响静态白名单。
    """
    try:
        from app.tenant.context import get_current_tenant
        tenant_id = get_current_tenant().tenant_id
    except Exception:
        return frozenset()

    if not tenant_id:
        return frozenset()

    now = time.time()
    cached = _dynamic_modules_cache.get(tenant_id)
    if cached and now - cached[0] < _DYNAMIC_CACHE_TTL:
        return cached[1]

    try:
        from app.services.redis_client import execute
        result = execute("SMEMBERS", f"sandbox:dynamic_modules:{tenant_id}")
        modules = frozenset(result) if result else frozenset()
    except Exception:
        modules = frozenset()

    _dynamic_modules_cache[tenant_id] = (now, modules)
    return modules


def _is_module_allowed(name: str) -> bool:
    """检查模块是否在白名单中（静态 + 动态）"""
    return name in _ALLOWED_MODULES or name in _get_dynamic_modules()


# ── 内建函数白名单 ──

_SAFE_BUILTINS: dict[str, Any] = {
    # 类型 & 构造
    "True": True, "False": False, "None": None,
    "int": int, "float": float, "str": str, "bool": bool,
    "bytes": bytes, "bytearray": bytearray,
    "list": list, "tuple": tuple, "dict": dict, "set": set, "frozenset": frozenset,
    "complex": complex, "memoryview": memoryview,
    # 数学 & 比较
    "abs": abs, "round": round, "min": min, "max": max, "sum": sum,
    "pow": pow, "divmod": divmod,
    # 迭代
    "range": range, "len": len, "enumerate": enumerate, "zip": zip,
    "map": map, "filter": filter, "sorted": sorted, "reversed": reversed,
    "all": all, "any": any, "next": next, "iter": iter,
    # 类型判断
    "isinstance": isinstance, "issubclass": issubclass,
    "callable": callable, "hasattr": hasattr,
    # 字符串 & 格式化
    "repr": repr, "format": format, "chr": chr, "ord": ord,
    "hex": hex, "oct": oct, "bin": bin,
    # 其他安全内建
    "id": id, "hash": hash,
    "print": print,  # print 只输出到 stdout，不影响安全
    "ValueError": ValueError,
    "TypeError": TypeError,
    "KeyError": KeyError,
    "IndexError": IndexError,
    "AttributeError": AttributeError,
    "RuntimeError": RuntimeError,
    "StopIteration": StopIteration,
    "Exception": Exception,
    "NotImplementedError": NotImplementedError,
}

# 执行超时（秒）
_EXEC_TIMEOUT = 30
_EXEC_TIMEOUT_EXTENDED = 360  # 使用 sandbox_caps 的工具需要更长超时（视频下载等）


# ── AST 安全检查 ──

class _ImportValidator(ast.NodeVisitor):
    """遍历 AST，检查所有 import 语句是否在白名单内"""

    def __init__(self) -> None:
        self.violations: list[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if not _is_module_allowed(alias.name):
                self.violations.append(f"禁止的 import: {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if not _is_module_allowed(module):
            self.violations.append(f"禁止的 import: {module}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # 禁止 __import__()
        if isinstance(node.func, ast.Name) and node.func.id == "__import__":
            self.violations.append("禁止直接调用 __import__()")
        # 禁止 eval() / exec()
        if isinstance(node.func, ast.Name) and node.func.id in ("eval", "exec", "compile"):
            self.violations.append(f"禁止调用 {node.func.id}()")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # 禁止访问 __subclasses__, __bases__, __globals__ 等危险属性
        if node.attr.startswith("__") and node.attr.endswith("__"):
            dangerous = {"__subclasses__", "__bases__", "__globals__",
                         "__code__", "__builtins__", "__import__",
                         "__loader__", "__spec__"}
            if node.attr in dangerous:
                self.violations.append(f"禁止访问 {node.attr}")
        self.generic_visit(node)


def validate_code(source: str) -> list[str]:
    """静态检查代码安全性，返回违规列表（空 = 通过）"""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"语法错误: {e}"]

    validator = _ImportValidator()
    validator.visit(tree)
    return validator.violations


# ── 受限 import 函数 ──

def _restricted_import(name: str, *args: Any, **kwargs: Any) -> Any:
    """只允许白名单内的模块被 import（静态 + 动态白名单）"""
    if not _is_module_allowed(name):
        dynamic = _get_dynamic_modules()
        all_allowed = sorted(_ALLOWED_MODULES | dynamic)
        raise ImportError(
            f"沙箱禁止 import '{name}'，允许的模块: {all_allowed}\n"
            f"提示：可以用 install_package 工具安装新的包。"
        )
    return importlib.import_module(name)


# ── 超时信号处理 ──

class _TimeoutError(Exception):
    pass


def _timeout_handler(signum: int, frame: Any) -> None:
    raise _TimeoutError(f"工具执行超时（{_EXEC_TIMEOUT}秒限制）")


# ── 核心：编译并加载工具代码 ──

def compile_tool(source: str) -> tuple[dict[str, Any], list[str]]:
    """编译自定义工具代码，返回 (module_dict, errors)

    成功时 module_dict 包含 TOOL_DEFINITIONS 和 TOOL_MAP。
    失败时 errors 非空。
    """
    # 1) 静态安全检查
    violations = validate_code(source)
    if violations:
        return {}, violations

    # 2) 构建受限执行环境
    sandbox_globals: dict[str, Any] = {
        "__builtins__": dict(_SAFE_BUILTINS),
    }
    # 注入受限 import
    sandbox_globals["__builtins__"]["__import__"] = _restricted_import

    # 3) 编译
    try:
        code_obj = compile(source, "<custom_tool>", "exec")
    except SyntaxError as e:
        return {}, [f"编译失败: {e}"]

    # 4) 执行（带超时）
    try:
        # 设置超时（仅 Unix）
        old_handler = None
        try:
            old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(_EXEC_TIMEOUT)
        except (ValueError, AttributeError):
            pass  # Windows 或非主线程，跳过超时

        exec(code_obj, sandbox_globals)  # noqa: S102

        # 取消超时
        try:
            signal.alarm(0)
            if old_handler is not None:
                signal.signal(signal.SIGALRM, old_handler)
        except (ValueError, AttributeError):
            pass

    except _TimeoutError:
        return {}, ["代码执行超时"]
    except ImportError as e:
        return {}, [str(e)]
    except Exception as e:
        return {}, [f"执行失败: {type(e).__name__}: {e}"]

    # 5) 验证接口
    errors: list[str] = []
    if "TOOL_DEFINITIONS" not in sandbox_globals:
        errors.append("代码缺少 TOOL_DEFINITIONS 列表")
    if "TOOL_MAP" not in sandbox_globals:
        errors.append("代码缺少 TOOL_MAP 字典")

    if errors:
        return {}, errors

    tool_defs = sandbox_globals["TOOL_DEFINITIONS"]
    tool_map = sandbox_globals["TOOL_MAP"]

    if not isinstance(tool_defs, list) or not tool_defs:
        errors.append("TOOL_DEFINITIONS 必须是非空列表")
    if not isinstance(tool_map, dict) or not tool_map:
        errors.append("TOOL_MAP 必须是非空字典")

    if errors:
        return {}, errors

    # 6) 验证每个工具定义的格式
    for td in tool_defs:
        if not isinstance(td, dict):
            errors.append(f"TOOL_DEFINITIONS 中的元素必须是字典，得到 {type(td)}")
            continue
        for key in ("name", "description", "input_schema"):
            if key not in td:
                errors.append(f"工具定义缺少 '{key}' 字段: {td}")
        name = td.get("name", "")
        if name and name not in tool_map:
            errors.append(f"工具 '{name}' 在 TOOL_DEFINITIONS 中有定义但不在 TOOL_MAP 中")

    if errors:
        return {}, errors

    return {"TOOL_DEFINITIONS": tool_defs, "TOOL_MAP": tool_map}, []


def execute_tool(handler: Callable, args: dict, *, extended_timeout: bool = False) -> ToolResult:
    """安全执行自定义工具的 handler 函数（带超时保护）

    Args:
        extended_timeout: 使用 sandbox_caps 的工具需要更长超时（视频下载等）
    """
    timeout = _EXEC_TIMEOUT_EXTENDED if extended_timeout else _EXEC_TIMEOUT
    try:
        old_handler = None
        try:
            old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(timeout)
        except (ValueError, AttributeError):
            pass

        result = handler(args)

        try:
            signal.alarm(0)
            if old_handler is not None:
                signal.signal(signal.SIGALRM, old_handler)
        except (ValueError, AttributeError):
            pass

    except _TimeoutError:
        return ToolResult.error(f"工具执行超时（{timeout}秒限制）", code="internal")
    except Exception as e:
        logger.exception("custom tool handler failed")
        return ToolResult.error(f"工具执行出错: {type(e).__name__}: {e}", code="internal")

    # 归一化返回值
    if isinstance(result, ToolResult):
        return result
    if isinstance(result, str):
        return ToolResult.success(result)
    return ToolResult.success(str(result))
