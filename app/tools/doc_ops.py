"""飞书文档操作工具

通过飞书 Docx v1 API 读写文档：
- 读取文档内容（blocks→markdown，保留标题层级结构）
- 创建新文档
- 局部编辑文档（edit_feishu_doc，按章节增删改，自动保留未修改部分）
- 全量替换文档（update_feishu_doc，章节级丢失检测 + 退回重试）
"""

from __future__ import annotations

import logging
import re

from app.tools.feishu_api import feishu_get, feishu_post, feishu_patch, feishu_delete, _current_user_open_id
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

# ── 文档快照缓存 ──
# read_feishu_doc 时自动缓存 markdown + 标题列表，供 update/edit 做内容丢失检测。
# key: doc_id, value: (markdown_text, [heading_text, ...], already_bounced)
_doc_snapshot: dict[str, tuple[str, list[str], bool]] = {}


# ═══════════════════════════════════════════════════════
#  Block 解析工具：blocks API → markdown
# ═══════════════════════════════════════════════════════

def _extract_block_text(block: dict, key: str) -> str:
    """从 block 的 elements 数组中提取纯文本。"""
    elements = block.get(key, {}).get("elements", [])
    return "".join(e.get("text_run", {}).get("content", "") for e in elements)


def _blocks_to_markdown(doc_id: str) -> tuple[str, list[str]] | ToolResult:
    """读取文档 blocks 并重建 markdown + 标题列表。

    比 raw_content 更好：标题带 # 标记，LLM 能理解文档结构。
    Returns: (markdown_text, [heading_text, ...]) or ToolResult on error.
    """
    all_items: list[dict] = []
    page_token = None
    while True:
        params: dict = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        data = feishu_get(
            f"/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
            params=params,
        )
        if isinstance(data, str):
            return ToolResult.api_error(f"读取文档块失败: {data}")
        items = data.get("data", {}).get("items", [])
        all_items.extend(items)
        if not data.get("data", {}).get("has_more"):
            break
        page_token = data.get("data", {}).get("page_token")

    _BT_KEY = {3: "heading1", 4: "heading2", 5: "heading3"}
    _BT_PREFIX = {3: "# ", 4: "## ", 5: "### "}

    lines: list[str] = []
    headings: list[str] = []

    for item in all_items:
        bt = item.get("block_type", 0)
        if bt in _BT_KEY:
            text = _extract_block_text(item, _BT_KEY[bt])
            lines.append(f"{_BT_PREFIX[bt]}{text}")
            headings.append(text.strip())
        elif bt == 2:  # text
            text = _extract_block_text(item, "text")
            lines.append(text)
        elif bt == 12:  # bullet
            text = _extract_block_text(item, "bullet")
            lines.append(f"- {text}")
        elif bt == 13:  # ordered
            text = _extract_block_text(item, "ordered")
            lines.append(f"1. {text}")
        elif bt == 14:  # code
            text = _extract_block_text(item, "code")
            lines.append(f"```\n{text}\n```")
        elif bt == 15:  # quote
            text = _extract_block_text(item, "quote")
            lines.append(f"> {text}")
        elif bt == 22:  # divider
            lines.append("---")
        else:
            # 未知 block 类型：尽力提取文本，不丢内容
            for key in ("text", "heading1", "heading2", "heading3",
                        "bullet", "ordered", "code", "quote"):
                if key in item:
                    text = _extract_block_text(item, key)
                    if text.strip():
                        lines.append(text)
                        break

    return "\n".join(lines), headings


# ═══════════════════════════════════════════════════════
#  章节解析工具：markdown → sections
# ═══════════════════════════════════════════════════════

def _parse_sections(text: str) -> list[dict]:
    """将 markdown 按标题拆分为章节列表。

    Returns: [{"heading": "## Title", "body": "content...", "level": 2}, ...]
    第一个标题之前的内容 heading="" level=0。
    """
    sections: list[dict] = []
    current_heading = ""
    current_level = 0
    body_lines: list[str] = []

    for line in text.split("\n"):
        m = re.match(r"^(#{1,3})\s+(.+)", line.strip())
        if m:
            if body_lines or current_heading:
                sections.append({
                    "heading": current_heading,
                    "body": "\n".join(body_lines),
                    "level": current_level,
                })
            current_heading = line.strip()
            current_level = len(m.group(1))
            body_lines = []
        else:
            body_lines.append(line)

    if body_lines or current_heading:
        sections.append({
            "heading": current_heading,
            "body": "\n".join(body_lines),
            "level": current_level,
        })

    return sections


def _find_section_idx(sections: list[dict], target: str) -> int | None:
    """模糊匹配章节标题，返回 index。

    匹配策略：精确 > 包含 > 子串。去掉 # 前缀后比较。
    """
    target_clean = target.lower().strip().lstrip("#").strip()
    if not target_clean:
        return None

    # 精确匹配
    for i, s in enumerate(sections):
        h = s["heading"].lower().strip().lstrip("#").strip()
        if h == target_clean:
            return i

    # 包含匹配
    for i, s in enumerate(sections):
        h = s["heading"].lower().strip().lstrip("#").strip()
        if h and (target_clean in h or h in target_clean):
            return i

    return None


def _sections_to_text(sections: list[dict]) -> str:
    """将章节列表重新拼接为 markdown 文本。"""
    parts: list[str] = []
    for s in sections:
        if s["heading"] and s["body"].strip():
            parts.append(f"{s['heading']}\n{s['body']}")
        elif s["heading"]:
            parts.append(s["heading"])
        elif s["body"].strip():
            parts.append(s["body"])
    return "\n\n".join(parts)


def _write_blocks(doc_id: str, content: str) -> ToolResult | int:
    """将 markdown 内容转为 blocks 并分批写入文档，返回写入块数或错误。"""
    blocks = _text_to_blocks(content)
    if not blocks:
        return 0

    BATCH = 50
    total_written = 0
    for i in range(0, len(blocks), BATCH):
        batch = blocks[i : i + BATCH]
        batch_num = i // BATCH + 1
        data = feishu_post(
            f"/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
            json={"children": batch},
            params={"document_revision_id": "-1"},
        )
        if isinstance(data, str):
            if total_written:
                return ToolResult.api_error(
                    f"部分写入成功({total_written}/{len(blocks)}块)，"
                    f"第 {batch_num} 批失败: {data}\n"
                    f"已写入的内容不会丢失。"
                )
            return ToolResult.api_error(f"写入失败: {data}")
        total_written += len(batch)

    return total_written


# ═══════════════════════════════════════════════════════
#  既有工具函数（保持不变）
# ═══════════════════════════════════════════════════════

def _grant_doc_permission(document_id: str, user_open_id: str, perm: str = "full_access") -> bool:
    """创建文档后自动给请求用户加协作者权限。

    飞书 Drive Permission API:
    POST /drive/v1/permissions/{token}/members?type=docx&need_notification=false
    """
    try:
        data = feishu_post(
            f"/drive/v1/permissions/{document_id}/members",
            json={
                "member_type": "openid",
                "member_id": user_open_id,
                "perm": perm,  # "view" | "edit" | "full_access"
            },
            params={"type": "docx", "need_notification": "false"},
        )
        if isinstance(data, str):
            logger.warning("grant doc perm failed: %s (doc=%s user=%s)", data, document_id, user_open_id[:15])
            return False
        logger.info("granted %s on doc %s to %s", perm, document_id, user_open_id[:15])
        return True
    except Exception:
        logger.warning("grant doc perm exception (doc=%s)", document_id, exc_info=True)
        return False


def _transfer_doc_owner(document_id: str, new_owner_open_id: str, doc_type: str = "docx") -> bool:
    """将文档所有权转让给指定用户。

    飞书 Drive Permission API:
    POST /drive/v1/permissions/{token}/members/transfer_owner?type=docx
    """
    try:
        data = feishu_post(
            f"/drive/v1/permissions/{document_id}/members/transfer_owner",
            json={
                "member_type": "openid",
                "member_id": new_owner_open_id,
            },
            params={"type": doc_type, "need_notification": "false"},
        )
        if isinstance(data, str):
            logger.warning("transfer owner failed: %s (doc=%s user=%s)", data, document_id, new_owner_open_id[:15])
            return False
        logger.info("transferred owner of doc %s to %s", document_id, new_owner_open_id[:15])
        return True
    except Exception:
        logger.warning("transfer owner exception (doc=%s)", document_id, exc_info=True)
        return False


def _set_doc_link_share(document_id: str, link_share: str = "tenant_readable", doc_type: str = "docx") -> bool:
    """设置文档的链接分享权限。

    飞书 Drive Permission API:
    PATCH /drive/v1/permissions/{token}/public?type=docx

    link_share 可选值:
    - tenant_readable: 组织内获得链接的人可阅读
    - tenant_editable: 组织内获得链接的人可编辑
    - anyone_readable: 互联网上获得链接的人可阅读
    - anyone_editable: 互联网上获得链接的人可编辑
    - closed: 关闭链接分享
    """
    try:
        data = feishu_patch(
            f"/drive/v1/permissions/{document_id}/public",
            json={"link_share_entity": link_share},
            params={"type": doc_type},
        )
        if isinstance(data, str):
            logger.warning("set link share failed: %s (doc=%s)", data, document_id)
            return False
        logger.info("set link share on doc %s to %s", document_id, link_share)
        return True
    except Exception:
        logger.warning("set link share exception (doc=%s)", document_id, exc_info=True)
        return False


def _extract_doc_info(doc_id_or_url: str) -> tuple[str, str]:
    """从飞书文档 URL 或纯 ID 中提取 (id, type)

    支持:
    - 纯 ID: doxcnxxxxxx (docx), wikcnxxxxxx (wiki)
    - URL: https://xxx.feishu.cn/docx/doxcnxxxxxx
    - URL: https://xxx.feishu.cn/wiki/xxxxxx
    - URL: https://xxx.feishu.cn/docs/xxxxxx
    """
    doc_id_or_url = doc_id_or_url.strip()

    # 从 URL 提取类型和 ID
    m = re.search(r"feishu\.cn/(docx|wiki|docs)/([A-Za-z0-9]+)", doc_id_or_url)
    if m:
        return m.group(2), m.group(1)

    # 去掉可能的查询参数
    if "?" in doc_id_or_url:
        doc_id_or_url = doc_id_or_url.split("?")[0]

    # 根据前缀猜测类型
    if doc_id_or_url.startswith("wik"):
        return doc_id_or_url, "wiki"
    if doc_id_or_url.startswith("dox"):
        return doc_id_or_url, "docx"

    return doc_id_or_url, "docx"


def _resolve_docx_id(doc_id: str, doc_type: str) -> str | ToolResult:
    """将 wiki_token 或其他类型的 ID 转为 docx document_id"""
    if doc_type == "wiki":
        logger.info("resolving wiki token: %s", doc_id)
        wiki_data = feishu_get(f"/wiki/v2/spaces/get_node?token={doc_id}")
        if isinstance(wiki_data, str):
            return ToolResult.api_error(f"无法解析 Wiki 链接: {wiki_data}")

        node = wiki_data.get("data", {}).get("node", {})
        obj_type = node.get("obj_type")
        if obj_type != "docx":
            return ToolResult.error(f"目前仅支持操作 Docx 类型的 Wiki 页面，该页面类型为: {obj_type}")

        resolved_id = node.get("obj_token")
        if not resolved_id:
            return ToolResult.error("无法从 Wiki 节点中获取 obj_token")
        return resolved_id

    if doc_type == "docs":
        return ToolResult.error("目前仅支持 Docx 格式，不支持旧版文档(docs)。请将文档升级为新版文档。")

    return doc_id


def _resolve_doc_id(document_id: str) -> str | ToolResult:
    """从用户输入解析出 docx document_id，失败返回 ToolResult。"""
    doc_id, doc_type = _extract_doc_info(document_id)
    if not doc_id:
        return ToolResult.invalid_param("请提供文档 ID 或飞书文档链接")
    resolved = _resolve_docx_id(doc_id, doc_type)
    if isinstance(resolved, ToolResult):
        return resolved
    return resolved


# ═══════════════════════════════════════════════════════
#  核心文档操作
# ═══════════════════════════════════════════════════════

def read_document(document_id: str) -> ToolResult:
    """读取飞书文档内容（blocks→markdown，保留标题结构）。"""
    doc_id = _resolve_doc_id(document_id)
    if isinstance(doc_id, ToolResult):
        return doc_id

    # 优先用 blocks→markdown（保留标题标记），fallback 到 raw_content
    result = _blocks_to_markdown(doc_id)
    if isinstance(result, ToolResult):
        # blocks 失败，fallback 到 raw_content
        logger.info("read_document: blocks failed, falling back to raw_content (doc=%s)", doc_id)
        data = feishu_get(f"/docx/v1/documents/{doc_id}/raw_content")
        if isinstance(data, str):
            return ToolResult.api_error(data)
        content = data.get("data", {}).get("content", "")
        headings: list[str] = []
    else:
        content, headings = result

    if not content:
        return ToolResult.success("文档内容为空。")

    # 缓存快照（markdown + 标题列表），供 update/edit 检测内容丢失
    _doc_snapshot[doc_id] = (content, headings, False)

    # 截断过长内容
    if len(content) > 30000:
        content = content[:30000] + f"\n\n... (文档过长已截断，共 {len(content)} 字符)"

    return ToolResult.success(content)


def create_document(title: str, folder_token: str = "", content: str = "") -> ToolResult:
    """创建新的飞书文档，可选地写入内容"""
    body: dict = {}
    if title:
        body["title"] = title
    if folder_token:
        body["folder_token"] = folder_token

    data = feishu_post("/docx/v1/documents", json=body)
    if isinstance(data, str):
        return ToolResult.api_error(data)

    doc = data.get("data", {}).get("document", {})
    doc_id = doc.get("document_id", "")
    doc_title = doc.get("title", title)

    # 自动给请求用户加协作者权限 + 转让所有权
    # bot 创建的文档默认只有 bot 能访问，转让 owner 后用户可迁入知识库、管理权限等
    user_oid = _current_user_open_id.get("")
    owner_transferred = False
    if doc_id and user_oid:
        _grant_doc_permission(doc_id, user_oid, perm="full_access")
        owner_transferred = _transfer_doc_owner(doc_id, user_oid)

    result = (
        f"文档已创建: {doc_title}\n"
        f"document_id: {doc_id}\n"
        f"链接: https://feishu.cn/docx/{doc_id}"
    )
    if owner_transferred:
        result += "\n所有权已转让给你（可自由管理权限、迁入知识库等）"

    # 如果有内容，写入到文档
    if content and doc_id:
        write_result = write_document(doc_id, content)
        if not write_result.ok:
            result += f"\n\n写入内容失败: {write_result.content}"
        else:
            result += "\n内容已写入"

    return ToolResult.success(result)


def _parse_inline_styles(text: str) -> list[dict]:
    """解析加粗等行内样式 (将 **text** 转为带 bold 的 text_run)"""
    if not text:
        return []
    # 匹配 **bold**，非贪婪
    pattern = r"(\*\*[^*]+\*\*)"
    parts = re.split(pattern, text)
    elements = []
    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            elements.append({
                "text_run": {
                    "content": part[2:-2],
                    "text_element_style": {"bold": True}
                }
            })
        else:
            elements.append({
                "text_run": {
                    "content": part,
                    "text_element_style": {}
                }
            })
    return elements


def _text_to_blocks(text: str) -> list[dict]:
    """将纯文本/简易 markdown 转为飞书文档 block 列表

    飞书 Docx v1 block_type 枚举：
      2=Text, 3=Heading1, 4=Heading2, 5=Heading3,
      12=Bullet, 13=Ordered, 14=Code, 15=Quote, 22=Divider
    """
    blocks: list[dict] = []
    in_code_block = False
    code_lines: list[str] = []

    for line in text.split("\n"):
        stripped = line.strip()

        # 处理代码块 ```
        if stripped.startswith("```"):
            if in_code_block:
                # 结束代码块 → 合并为一个 code block
                blocks.append({
                    "block_type": 14,
                    "code": {
                        "elements": [{"text_run": {"content": "\n".join(code_lines)}}],
                    },
                })
                code_lines = []
                in_code_block = False
            else:
                in_code_block = True
            continue

        if in_code_block:
            code_lines.append(line.rstrip())
            continue

        if not stripped:
            continue

        # Heading 检测
        if stripped.startswith("### "):
            blocks.append({
                "block_type": 5,
                "heading3": {
                    "elements": _parse_inline_styles(stripped[4:]),
                },
            })
        elif stripped.startswith("## "):
            blocks.append({
                "block_type": 4,
                "heading2": {
                    "elements": _parse_inline_styles(stripped[3:]),
                },
            })
        elif stripped.startswith("# "):
            blocks.append({
                "block_type": 3,
                "heading1": {
                    "elements": _parse_inline_styles(stripped[2:]),
                },
            })
        elif stripped.startswith("- ") or stripped.startswith("* "):
            blocks.append({
                "block_type": 12,  # bullet
                "bullet": {
                    "elements": _parse_inline_styles(stripped[2:]),
                },
            })
        elif re.match(r"^\d+\.\s", stripped):
            content = re.sub(r"^\d+\.\s", "", stripped)
            blocks.append({
                "block_type": 13,  # ordered
                "ordered": {
                    "elements": _parse_inline_styles(content),
                },
            })
        elif stripped == "---":
            blocks.append({"block_type": 22, "divider": {}})
        else:
            blocks.append({
                "block_type": 2,
                "text": {
                    "elements": _parse_inline_styles(line),  # 普通行保留原始缩进/空格，解析加粗
                },
            })

    # 未闭合的代码块也输出
    if code_lines:
        blocks.append({
            "block_type": 14,
            "code": {
                "elements": [{"text_run": {"content": "\n".join(code_lines)}}],
            },
        })

    return blocks


def grant_doc_permission(document_id: str, perm: str = "full_access") -> ToolResult:
    """为当前用户开通飞书文档的协作权限"""
    doc_id = _resolve_doc_id(document_id)
    if isinstance(doc_id, ToolResult):
        return doc_id

    if perm not in ("view", "edit", "full_access"):
        return ToolResult.invalid_param("权限类型只能是 view、edit 或 full_access")

    user_oid = _current_user_open_id.get("")
    if not user_oid:
        return ToolResult.api_error("无法获取当前用户的 open_id，无法授权")

    ok = _grant_doc_permission(doc_id, user_oid, perm=perm)
    if ok:
        perm_label = {"view": "阅读", "edit": "编辑", "full_access": "完全访问"}.get(perm, perm)
        return ToolResult.success(f"已为你开通文档 {doc_id} 的{perm_label}权限")
    return ToolResult.api_error(f"权限开通失败，可能是 bot 对该文档没有管理权限。文档 ID: {doc_id}")


def grant_doc_permission_to_user(document_id: str, user_name: str, perm: str = "full_access") -> ToolResult:
    """为指定用户（按姓名）开通飞书文档的协作权限"""
    doc_id = _resolve_doc_id(document_id)
    if isinstance(doc_id, ToolResult):
        return doc_id

    if perm not in ("view", "edit", "full_access"):
        return ToolResult.invalid_param("权限类型只能是 view、edit 或 full_access")

    from app.services.user_registry import find_by_name
    user_oid = find_by_name(user_name)
    if not user_oid:
        return ToolResult.error(f"找不到用户「{user_name}」，请确认姓名是否正确。已知用户才能授权。")

    ok = _grant_doc_permission(doc_id, user_oid, perm=perm)
    if ok:
        perm_label = {"view": "阅读", "edit": "编辑", "full_access": "完全访问"}.get(perm, perm)
        return ToolResult.success(f"已为用户「{user_name}」开通文档 {doc_id} 的{perm_label}权限")
    return ToolResult.api_error(f"权限开通失败，可能是 bot 对该文档没有管理权限。文档 ID: {doc_id}")


def transfer_doc_owner(document_id: str, user_name: str = "") -> ToolResult:
    """将文档所有权转让给指定用户，或转让给当前对话用户（不指定 user_name 时）"""
    doc_id = _resolve_doc_id(document_id)
    if isinstance(doc_id, ToolResult):
        return doc_id

    if user_name:
        from app.services.user_registry import find_by_name
        target_oid = find_by_name(user_name)
        if not target_oid:
            return ToolResult.error(f"找不到用户「{user_name}」，请确认姓名是否正确。")
        target_label = f"用户「{user_name}」"
    else:
        target_oid = _current_user_open_id.get("")
        if not target_oid:
            return ToolResult.api_error("无法获取当前用户的 open_id，无法转让")
        target_label = "你"

    ok = _transfer_doc_owner(doc_id, target_oid)
    if ok:
        return ToolResult.success(
            f"已将文档 {doc_id} 的所有权转让给{target_label}。"
            f"现在{target_label}是文档的所有者，可以迁入知识库、管理权限等。"
        )
    return ToolResult.api_error(
        f"所有权转让失败，可能是 bot 不是该文档的所有者，或该用户不在同一组织。文档 ID: {doc_id}"
    )


def set_doc_sharing(document_id: str, link_share: str = "tenant_readable") -> ToolResult:
    """设置文档的链接分享权限"""
    doc_id = _resolve_doc_id(document_id)
    if isinstance(doc_id, ToolResult):
        return doc_id

    valid = ("tenant_readable", "tenant_editable", "anyone_readable", "anyone_editable", "closed")
    if link_share not in valid:
        return ToolResult.invalid_param(f"link_share 必须是: {', '.join(valid)}")

    ok = _set_doc_link_share(doc_id, link_share)
    if ok:
        labels = {
            "tenant_readable": "组织内获得链接可阅读",
            "tenant_editable": "组织内获得链接可编辑",
            "anyone_readable": "互联网获得链接可阅读",
            "anyone_editable": "互联网获得链接可编辑",
            "closed": "关闭链接分享",
        }
        return ToolResult.success(f"已设置文档 {doc_id} 的链接分享权限为: {labels.get(link_share, link_share)}")
    return ToolResult.api_error(f"设置分享权限失败，可能是 bot 对该文档没有管理权限。文档 ID: {doc_id}")


def write_document(document_id: str, content: str) -> ToolResult:
    """向已有飞书文档写入内容（追加到文档末尾）"""
    doc_id = _resolve_doc_id(document_id)
    if isinstance(doc_id, ToolResult):
        return doc_id

    # 防止内容过长导致写入耗时过久（模型被卡住）
    if len(content) > 50000:
        content = content[:50000] + "\n\n... (内容过长已截断，共 " + str(len(content)) + " 字符)"
        logger.warning("write_document: content truncated to 50000 chars (doc=%s)", doc_id)

    blocks = _text_to_blocks(content)
    if not blocks:
        return ToolResult.invalid_param("内容为空，没有可写入的内容")

    # 飞书 API 限制每次最多 50 个子块，需要分批
    BATCH = 50
    total_batches = (len(blocks) + BATCH - 1) // BATCH
    logger.info("write_document: doc=%s, %d blocks, %d batches", doc_id, len(blocks), total_batches)

    total_written = 0
    for i in range(0, len(blocks), BATCH):
        batch = blocks[i : i + BATCH]
        batch_num = i // BATCH + 1
        logger.info("write_document: batch %d/%d (%d blocks)", batch_num, total_batches, len(batch))

        data = feishu_post(
            f"/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
            json={"children": batch},
            params={"document_revision_id": -1},
        )
        if isinstance(data, str):
            logger.warning("write_document: batch %d failed: %s", batch_num, data[:300])
            if total_written:
                return ToolResult.api_error(
                    f"部分写入成功({total_written}/{len(blocks)}块)，"
                    f"第 {batch_num} 批失败: {data}\n"
                    f"已写入的内容不会丢失。不要重复写入已成功的部分。"
                )
            return ToolResult.api_error(
                f"写入失败: {data}\n"
                f"如果是权限问题，告诉用户。不要反复重试同样的写入操作。"
            )
        total_written += len(batch)
        logger.info("write_document: batch %d success (%d/%d blocks done)", batch_num, total_written, len(blocks))

    return ToolResult.success(f"已写入 {total_written} 个内容块到文档 {doc_id}")


def _clear_document_blocks(doc_id: str) -> int | ToolResult:
    """清空文档所有顶层子块，返回删除的块数。失败返回 ToolResult。"""
    # 1. 获取所有子块（分页）
    all_block_ids: list[str] = []
    page_token = None
    while True:
        params: dict = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        data = feishu_get(
            f"/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
            params=params,
        )
        if isinstance(data, str):
            return ToolResult.api_error(f"获取文档块列表失败: {data}")
        items = data.get("data", {}).get("items", [])
        for item in items:
            bid = item.get("block_id", "")
            if bid and bid != doc_id:  # 跳过 page block 本身
                all_block_ids.append(bid)
        if not data.get("data", {}).get("has_more"):
            break
        page_token = data.get("data", {}).get("page_token")

    if not all_block_ids:
        return 0  # 文档已空

    # 2. 批量删除（start_index=0, end_index=count）
    count = len(all_block_ids)
    logger.info("_clear_document_blocks: doc=%s, deleting %d blocks", doc_id, count)
    result = feishu_delete(
        f"/docx/v1/documents/{doc_id}/blocks/{doc_id}/children/batch_delete",
        json={"start_index": 0, "end_index": count},
        params={"document_revision_id": "-1"},
    )
    if isinstance(result, str):
        return ToolResult.api_error(f"删除文档块失败: {result}")
    return count


def update_document(document_id: str, content: str) -> ToolResult:
    """替换飞书文档的全部内容（清空后重写）"""
    doc_id = _resolve_doc_id(document_id)
    if isinstance(doc_id, ToolResult):
        return doc_id

    if not content or not content.strip():
        return ToolResult.invalid_param("内容不能为空")

    # 防止内容过长
    if len(content) > 50000:
        content = content[:50000] + "\n\n... (内容过长已截断，共 " + str(len(content)) + " 字符)"
        logger.warning("update_document: content truncated to 50000 chars (doc=%s)", doc_id)

    # ── 内容丢失检测（章节级 + 字数双重检查）──
    # 如果 LLM 之前 read 过这个文档，我们有 markdown 快照和标题列表。
    # 检测到丢失 → 把原文 + 具体丢失的章节名塞回 tool response，让 LLM 重试（仅一次）。
    cached = _doc_snapshot.get(doc_id)
    if cached:
        original_text, orig_headings, already_bounced = cached
        if not already_bounced:
            orig_len = len(original_text)
            new_len = len(content)

            # 检查 1: 字数缩水 > 50%
            char_shrink = orig_len > 500 and new_len < orig_len * 0.5

            # 检查 2: 章节标题丢失 > 30%
            missing_headings: list[str] = []
            if orig_headings:
                new_lower = content.lower()
                missing_headings = [h for h in orig_headings if h.lower() not in new_lower]
                heading_loss = len(missing_headings) > max(1, len(orig_headings) * 0.3)
            else:
                heading_loss = False

            if char_shrink or heading_loss:
                # 标记已退回，下次直接放行
                _doc_snapshot[doc_id] = (original_text, orig_headings, True)

                # 构建具体反馈
                parts: list[str] = []
                if char_shrink:
                    parts.append(
                        f"新内容（{new_len}字）比原文（{orig_len}字）"
                        f"短了 {100 - new_len * 100 // orig_len}%"
                    )
                if missing_headings:
                    missing_str = "、".join(f"「{h}」" for h in missing_headings[:10])
                    parts.append(f"以下原文章节在新版本中缺失: {missing_str}")

                feedback = "；".join(parts) + "。"

                # 附上原文供参考
                ref = original_text[:8000]
                if len(original_text) > 8000:
                    ref += f"\n... (原文共 {orig_len} 字，已截断)"

                logger.info(
                    "update_document: content loss detected doc=%s char_shrink=%s "
                    "missing_headings=%d/%d, bouncing with original",
                    doc_id, char_shrink, len(missing_headings), len(orig_headings),
                )
                return ToolResult.api_error(
                    f"{feedback}\n"
                    f"如果只需要修改部分章节，建议改用 edit_feishu_doc 工具（自动保留未修改的部分）。\n"
                    f"如果确实要全量替换，请补充缺失内容后重试：\n\n"
                    f"--- 原文 ---\n{ref}\n--- 原文结束 ---"
                )

    # 1. 清空现有内容
    clear_result = _clear_document_blocks(doc_id)
    if isinstance(clear_result, ToolResult):
        return clear_result
    logger.info("update_document: cleared %d blocks from doc %s", clear_result, doc_id)

    # 2. 写入新内容
    write_result = _write_blocks(doc_id, content)
    if isinstance(write_result, ToolResult):
        return write_result

    # 写入成功，清掉快照缓存
    _doc_snapshot.pop(doc_id, None)

    return ToolResult.success(
        f"文档 {doc_id} 已更新：清除了 {clear_result} 个旧块，写入了 {write_result} 个新块"
    )


def edit_document(document_id: str, edits: list[dict]) -> ToolResult:
    """局部编辑飞书文档：自动读取原文 → 按章节应用修改 → 保留未修改部分 → 回写。

    edits 格式:
    [
      {"action": "replace", "section": "核心痛点", "content": "新内容..."},
      {"action": "insert_after", "section": "核心痛点", "content": "新增章节..."},
      {"action": "delete", "section": "Bot的碎碎念"},
      {"action": "append", "content": "追加到文档末尾的内容..."},
    ]
    """
    doc_id = _resolve_doc_id(document_id)
    if isinstance(doc_id, ToolResult):
        return doc_id

    if not edits:
        return ToolResult.invalid_param("edits 不能为空")

    # 1. 读取当前文档（blocks→markdown）
    result = _blocks_to_markdown(doc_id)
    if isinstance(result, ToolResult):
        return result
    markdown, _ = result

    if not markdown.strip():
        return ToolResult.error("文档为空，无法编辑。请使用 write_feishu_doc 写入内容。")

    # 2. 按标题拆分章节
    sections = _parse_sections(markdown)
    available = [s["heading"] for s in sections if s["heading"]]

    # 3. 逐个应用编辑操作
    applied: list[str] = []
    for edit in edits:
        action = edit.get("action", "replace")
        target = edit.get("section", "")
        new_content = edit.get("content", "")

        if action == "append":
            sections.append({"heading": "", "body": new_content, "level": 0})
            applied.append("append: 追加了新内容")
            continue

        if not target:
            return ToolResult.invalid_param(f"{action} 操作必须指定 section（目标章节标题）")

        idx = _find_section_idx(sections, target)
        if idx is None:
            return ToolResult.error(
                f"找不到章节「{target}」。\n"
                f"可用章节: {', '.join(available) if available else '(无标题章节)'}"
            )

        if action == "replace":
            if not new_content:
                return ToolResult.invalid_param("replace 操作的 content 不能为空")
            heading = sections[idx]["heading"]
            # 如果 LLM 在 content 里重复了标题，自动去掉
            if heading and new_content.strip().startswith(heading):
                new_content = new_content.strip()[len(heading):].lstrip("\n")
            sections[idx]["body"] = new_content
            applied.append(f"replace: {heading}")

        elif action == "insert_after":
            if not new_content:
                return ToolResult.invalid_param("insert_after 操作的 content 不能为空")
            sections.insert(idx + 1, {"heading": "", "body": new_content, "level": 0})
            applied.append(f"insert_after: {sections[idx]['heading']}")

        elif action == "delete":
            removed = sections.pop(idx)
            applied.append(f"delete: {removed['heading']}")

        else:
            return ToolResult.invalid_param(
                f"不支持的操作: {action}，可选: replace / insert_after / delete / append"
            )

    # 4. 重建 markdown 并回写
    full_content = _sections_to_text(sections)
    if not full_content.strip():
        return ToolResult.error("编辑后文档为空，已取消操作")

    # 清空 + 写入
    clear_result = _clear_document_blocks(doc_id)
    if isinstance(clear_result, ToolResult):
        return clear_result

    write_result = _write_blocks(doc_id, full_content)
    if isinstance(write_result, ToolResult):
        return write_result

    _doc_snapshot.pop(doc_id, None)

    ops = "\n".join(f"  - {a}" for a in applied)
    return ToolResult.success(
        f"文档 {doc_id} 局部编辑完成：\n{ops}\n"
        f"清除了 {clear_result} 个旧块，写入了 {write_result} 个新块（未修改的章节已保留）"
    )


# ═══════════════════════════════════════════════════════
#  Tool definitions & map
# ═══════════════════════════════════════════════════════

TOOL_DEFINITIONS = [
    {
        "name": "read_feishu_doc",
        "description": "读取飞书文档内容（返回带标题结构的 Markdown）。可以传文档 ID 或飞书文档链接。",
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "飞书文档 ID 或完整链接（如 https://xxx.feishu.cn/docx/doxcnxxxxxx）",
                },
            },
            "required": ["document_id"],
        },
    },
    {
        "name": "create_feishu_doc",
        "description": "创建飞书文档并写入内容。支持 Markdown 格式（标题用 #，列表用 - 或 1.）。返回文档 ID 和链接。",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "文档标题",
                },
                "content": {
                    "type": "string",
                    "description": "文档内容（支持简易 Markdown：# 标题、- 列表、--- 分割线）",
                    "default": "",
                },
                "folder_token": {
                    "type": "string",
                    "description": "目标文件夹 token（不填则创建在根目录）",
                    "default": "",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "write_feishu_doc",
        "description": "向已有飞书文档末尾追加内容（不影响现有内容）。支持 Markdown 格式。",
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "飞书文档 ID 或链接",
                },
                "content": {
                    "type": "string",
                    "description": "要追加的内容（支持简易 Markdown）",
                },
            },
            "required": ["document_id", "content"],
        },
    },
    {
        "name": "edit_feishu_doc",
        "description": (
            "局部编辑飞书文档的指定章节（自动读取原文 → 替换指定部分 → 保留其余内容）。"
            "适合只需要修改部分章节的场景，比 update_feishu_doc 更安全，不会丢失未修改的内容。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "飞书文档 ID 或链接",
                },
                "edits": {
                    "type": "array",
                    "description": "编辑操作列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["replace", "insert_after", "delete", "append"],
                                "description": (
                                    "replace=替换章节正文（标题保留）, "
                                    "insert_after=在章节后插入新内容, "
                                    "delete=删除整个章节, "
                                    "append=追加到文档末尾"
                                ),
                            },
                            "section": {
                                "type": "string",
                                "description": (
                                    "目标章节标题（模糊匹配，如 '核心痛点' 可匹配 '## 二、核心痛点拆解'）。"
                                    "append 操作时可不填。"
                                ),
                            },
                            "content": {
                                "type": "string",
                                "description": "新内容（支持 Markdown）。delete 操作时不需要。",
                            },
                        },
                        "required": ["action"],
                    },
                },
            },
            "required": ["document_id", "edits"],
        },
    },
    {
        "name": "update_feishu_doc",
        "description": (
            "替换飞书文档的全部内容（先清空再重写）。content 必须是完整的新文档，"
            "原文中需要保留的部分也要包含在内。"
            "如果只需要修改部分章节，优先使用 edit_feishu_doc（自动保留未修改内容）。"
            "如果只需追加内容，请用 write_feishu_doc。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "飞书文档 ID 或链接",
                },
                "content": {
                    "type": "string",
                    "description": "完整的新文档内容（支持简易 Markdown）",
                },
            },
            "required": ["document_id", "content"],
        },
    },
    {
        "name": "grant_feishu_doc_permission",
        "description": "为当前用户开通飞书文档的协作权限（阅读/编辑/完全访问）。当用户说「给我开权限」「我打不开文档」时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "飞书文档 ID 或完整链接",
                },
                "perm": {
                    "type": "string",
                    "description": "权限类型: view(阅读), edit(编辑), full_access(完全访问，默认)",
                    "enum": ["view", "edit", "full_access"],
                    "default": "full_access",
                },
            },
            "required": ["document_id"],
        },
    },
    {
        "name": "grant_feishu_doc_permission_to_user",
        "description": (
            "为指定用户（按姓名）开通飞书文档的协作权限。"
            "当需要给其他人（不是当前对话用户）开权限时使用，如「给张三开 XXX 文档的编辑权限」。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "飞书文档 ID 或完整链接",
                },
                "user_name": {
                    "type": "string",
                    "description": "目标用户的姓名（模糊匹配）",
                },
                "perm": {
                    "type": "string",
                    "description": "权限类型: view(阅读), edit(编辑), full_access(完全访问，默认)",
                    "enum": ["view", "edit", "full_access"],
                    "default": "full_access",
                },
            },
            "required": ["document_id", "user_name"],
        },
    },
    {
        "name": "transfer_feishu_doc_owner",
        "description": (
            "将飞书文档的所有权转让给指定用户。转让后该用户成为文档所有者，"
            "可以迁入知识库、管理所有权限等。不指定 user_name 时转让给当前对话用户。"
            "当用户说「把文档转给我」「我要迁入知识库」时使用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "飞书文档 ID 或完整链接",
                },
                "user_name": {
                    "type": "string",
                    "description": "目标用户姓名（不填则转让给当前对话用户）",
                    "default": "",
                },
            },
            "required": ["document_id"],
        },
    },
    {
        "name": "set_feishu_doc_sharing",
        "description": (
            "设置飞书文档的链接分享权限。控制谁可以通过链接访问文档。"
            "当用户说「把文档设成公开」「让组织内的人都能看」时使用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "飞书文档 ID 或完整链接",
                },
                "link_share": {
                    "type": "string",
                    "description": (
                        "链接分享权限: "
                        "tenant_readable(组织内可读), tenant_editable(组织内可编辑), "
                        "anyone_readable(互联网可读), anyone_editable(互联网可编辑), "
                        "closed(关闭链接分享)"
                    ),
                    "enum": ["tenant_readable", "tenant_editable", "anyone_readable", "anyone_editable", "closed"],
                    "default": "tenant_readable",
                },
            },
            "required": ["document_id"],
        },
    },
]

TOOL_MAP = {
    "read_feishu_doc": lambda args: read_document(
        document_id=args["document_id"],
    ),
    "create_feishu_doc": lambda args: create_document(
        title=args["title"],
        content=args.get("content", ""),
        folder_token=args.get("folder_token", ""),
    ),
    "write_feishu_doc": lambda args: write_document(
        document_id=args["document_id"],
        content=args["content"],
    ),
    "edit_feishu_doc": lambda args: edit_document(
        document_id=args["document_id"],
        edits=args["edits"],
    ),
    "update_feishu_doc": lambda args: update_document(
        document_id=args["document_id"],
        content=args["content"],
    ),
    "grant_feishu_doc_permission": lambda args: grant_doc_permission(
        document_id=args["document_id"],
        perm=args.get("perm", "full_access"),
    ),
    "grant_feishu_doc_permission_to_user": lambda args: grant_doc_permission_to_user(
        document_id=args["document_id"],
        user_name=args["user_name"],
        perm=args.get("perm", "full_access"),
    ),
    "transfer_feishu_doc_owner": lambda args: transfer_doc_owner(
        document_id=args["document_id"],
        user_name=args.get("user_name", ""),
    ),
    "set_feishu_doc_sharing": lambda args: set_doc_sharing(
        document_id=args["document_id"],
        link_share=args.get("link_share", "tenant_readable"),
    ),
}
