"""Question-pack templates for high-risk or high-ambiguity workflows.

这些模板不直接替代工具或 workflow，只负责告诉模型：
- 哪些任务在信息不足时必须先补问
- 最少应该补齐哪些关键信息
"""

from __future__ import annotations

import re


QUESTION_PACKS = {
    "research": """
[问题包: research]
- 调研/搜索/获客类任务如果目标画像不清楚，先补齐最少必要信息再开始。
- 优先补这几项：目标对象是谁、平台范围、关注维度、输出格式、时间范围。
- 如果用户已经给了 80% 以上信息，就别机械追问，直接开始查；只补最缺的一项。
""".strip(),
    "bot_onboarding": """
[问题包: bot_onboarding]
- bot 开通/部署前，至少确认：客户名称、目标平台、bot 用途/服务对象、联系人或负责人。
- 如果涉及独立实例，再确认是否已拿到对应平台凭证；如果是复用现有实例，再确认是不是同一套平台账号/应用。
- 没补齐这些信息前，不要直接假设部署方案。
""".strip(),
    "document_change": """
[问题包: document_change]
- 文档改写/更新前，至少确认：要改哪份文档、要改哪一段或哪一类内容、是直接改原文还是先出草稿。
- 如果用户给的是链接或文档 ID，优先直接读取原文，不要凭空重写。
- 如果修改范围模糊，先用一句最短问题把目标补清楚。
""".strip(),
    "schedule_task": """
[问题包: schedule_task]
- 日程/任务类请求若信息不完整，先补齐：最终日期、具体时间、时区、参与人/负责人、标题或目的。
- 用户说“今天/明天/下周”时，必须用系统时间换算成绝对日期再确认。
- 没有明确写操作意图时，不要直接创建或改动日程/任务。
""".strip(),
    "code_change": """
[问题包: code_change]
- 代码修改前，优先确认：目标仓库/文件或报错范围、当前问题、预期行为、是否允许直接改动现有实现。
- 如果请求更像“想法/方案/重构方向”，先澄清边界再改，不要第一轮就拍脑袋写代码。
- 用户已经给出明确文件、函数、报错或验收标准时，可以少问或不问，直接开始。
""".strip(),
}


_RESEARCH_RE = re.compile(r"(调研|研究|获客|竞品|搜索|社媒|小红书|抖音|微博|博主|KOL|线索)", re.IGNORECASE)
_BOT_ONBOARDING_RE = re.compile(r"(开通|部署|bot|助手|租户|实例|客服账号|接入|provision)", re.IGNORECASE)
_DOC_RE = re.compile(r"(文档|doc|docs|文章|纪要|合同|说明|改文案|改稿|改写)", re.IGNORECASE)
_SCHEDULE_RE = re.compile(r"(日程|日历|会议|提醒|task|任务|待办|排期|约个会)", re.IGNORECASE)
_CODE_RE = re.compile(r"(代码|bug|报错|修复|fix|pr|pull request|重构|实现|开发|repo|仓库)", re.IGNORECASE)


def select_question_packs(
    user_text: str,
    task_type: str = "",
    actual_tool_names: set[str] | None = None,
) -> list[str]:
    """根据当前任务判断需要哪些问题包。"""
    text = (user_text or "").strip()
    packs: list[str] = []

    if task_type == "research" or _RESEARCH_RE.search(text):
        packs.append("research")
    if _BOT_ONBOARDING_RE.search(text):
        packs.append("bot_onboarding")
    if _DOC_RE.search(text):
        packs.append("document_change")
    if _SCHEDULE_RE.search(text):
        packs.append("schedule_task")
    if _CODE_RE.search(text):
        packs.append("code_change")

    # 保持稳定顺序并去重
    ordered = []
    for name in ["research", "bot_onboarding", "document_change", "schedule_task", "code_change"]:
        if name in packs:
            ordered.append(name)
    return ordered


def render_question_packs(names: list[str]) -> str:
    blocks = [QUESTION_PACKS[name] for name in names if name in QUESTION_PACKS]
    return "\n\n".join(blocks)
