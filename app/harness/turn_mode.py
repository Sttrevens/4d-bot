"""Shared turn-mode inference for agent runtimes."""

from __future__ import annotations

from dataclasses import dataclass
import re

_QUICK_KEYWORDS = {"你好", "hi", "hello", "在吗", "在不", "谢谢", "thx", "ok", "好的"}
_ANALYSIS_RE = re.compile(
    r"(为什么|怎么|如何|啥意思|什么意思|什么逻辑|怎么看|觉得|是不是|对吗|对嘛|"
    r"总结|介绍|解释|分析|详细|展开|具体|来源|链接|哲学|教义|宿命论|离散|"
    r"这个人|类似的观点|能不能看完|看完了吗)"
)
_RESEARCH_RE = re.compile(
    r"(调研|竞品|市场|行业报告|research|competitor|社媒|数据.*收集|"
    r"搜[一搜索]|查[一查找]|帮我[找查搜看]|最新|现状|什么情况|"
    r"价格|定价|报价|套餐|额度|配额|extra|充值|pricing|price|quota|subscription|plan)"
)
_PRICING_RE = re.compile(
    r"(价格|定价|报价|套餐|额度|配额|extra|充值|pricing|price|quota|subscription|plan)",
    re.IGNORECASE,
)
_COLLAB_RE = re.compile(
    r"(日历|日程|会议|calendar|任务|文档|多维表格|bitable|表格|飞书|doc|sheet|提醒|邮件|群消息|审批)"
)
_CODE_RE = re.compile(
    r"(代码|部署|分支|脚本|工程|重构|\bbug\b|\bfix\b|\bdeploy\b|\bgit\b|\bpr\b|\bcommit\b|\bpush\b|\bdebug\b)",
    re.IGNORECASE,
)
_DEVOPS_RE = re.compile(r"(服务器|日志|log|重启|restart|进程|docker|容器)")
_CONTENT_RE = re.compile(r"(导出|pdf|ppt|export|视频|video|youtube|bilibili|报告)")
_ADMIN_RE = re.compile(r"(创建.*bot|部署.*实例|开通|provision|租户|安装.*bot)")
_PRODUCT_RE = re.compile(r"(codex|cursor|chatgpt|openai|claude\s*code|gemini)", re.IGNORECASE)
_ACTION_RE = re.compile(
    r"(提醒我|帮我创建|帮我加|帮我发|帮我安排|帮我设置|"
    r"创建(一个|一下)?|添加(一个|一下)?|发送(一个|一下)?|"
    r"删除(一个|一下)?|修改(一个|一下)?|更新(一个|一下)?|"
    r"设个?提醒|安排一下|加个?日程|发个?消息|写入|导出一份)"
)


def has_explicit_code_intent(user_text: str) -> bool:
    return bool(_CODE_RE.search((user_text or "").strip()))


def is_product_pricing_turn(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    return bool(_PRICING_RE.search(text) and _PRODUCT_RE.search(text))


def sanitize_suggested_groups(user_text: str, suggested_groups: list[str] | set[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    groups = list(dict.fromkeys(suggested_groups or ("core",)))
    if "core" not in groups:
        groups.insert(0, "core")
    if is_product_pricing_turn(user_text) and not has_explicit_code_intent(user_text):
        groups = [g for g in groups if g not in {"code_dev", "devops"}]
        if "research" not in groups:
            groups.append("research")
    return tuple(dict.fromkeys(groups))


def should_run_code_preflight(user_text: str, suggested_groups: list[str] | set[str] | tuple[str, ...] | None) -> bool:
    groups = sanitize_suggested_groups(user_text, suggested_groups)
    return "code_dev" in groups and has_explicit_code_intent(user_text)


@dataclass(frozen=True)
class TurnMode:
    mode: str
    task_type: str
    groups: tuple[str, ...]


def is_non_actionable_turn(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    return bool(_ANALYSIS_RE.search(text)) and not bool(_ACTION_RE.search(text))


def infer_turn_mode(user_text: str) -> TurnMode:
    text = (user_text or "").strip()
    if not text:
        return TurnMode("chat", "normal", ("core",))

    lower = text.lower()
    if len(lower) < 5 or lower in _QUICK_KEYWORDS:
        return TurnMode("quick", "quick", ("core",))

    groups = ["core"]
    mode = "chat"
    task_type = "normal"

    if _RESEARCH_RE.search(text):
        mode = "research"
        task_type = "research"
        groups.append("research")
    if _CODE_RE.search(text):
        mode = "code"
        task_type = "deep"
        groups.append("code_dev")
    if _DEVOPS_RE.search(text):
        groups.append("devops")
        if mode == "chat":
            mode = "action"
    if _CONTENT_RE.search(text):
        groups.append("content")
        if mode == "chat":
            mode = "analysis"
    if _ADMIN_RE.search(text):
        groups.append("admin")
        mode = "action"
        task_type = "provision"
    if _COLLAB_RE.search(text):
        groups.append("feishu_collab")
        if mode == "chat":
            mode = "action" if _ACTION_RE.search(text) else "collab"
    if _ANALYSIS_RE.search(text) and not _ACTION_RE.search(text):
        if mode in {"chat", "content", "collab"}:
            mode = "analysis"
    if _ACTION_RE.search(text) and mode not in {"research", "code"}:
        mode = "action"

    return TurnMode(mode, task_type, tuple(dict.fromkeys(groups)))
