"""飞书消息工具

- 发私信（按姓名或 open_id）
- 发群消息（按 chat_id）
- 列出 bot 所在的群
- 读取聊天记录（用于注入上下文）
"""

from __future__ import annotations

import json as _json
import re

import logging

from app.tools.feishu_api import feishu_get, feishu_post
from app.tools._fuzzy import fuzzy_filter
from app.tools.tool_result import ToolResult
from app.services import user_registry

logger = logging.getLogger(__name__)


def _replace_at_mentions(text: str) -> str:
    """把 LLM 输出的 @name 转换成飞书 <at> 标签。

    匹配模式：
    - @姓名（中文名 2-4 字）
    - @name（英文名）
    - @ou_xxx（直接 open_id）
    已经是 <at ...> 格式的不重复处理。
    """
    if "<at " in text:
        # 已经包含飞书 at 标签，跳过
        return text

    def _replacer(m: re.Match) -> str:
        name = m.group(1)
        # 直接是 open_id
        if name.startswith("ou_"):
            display = user_registry.get_name(name) or name
            return f'<at user_id="{name}">{display}</at>'
        # 按名字查找
        open_id = user_registry.find_by_name(name)
        if open_id:
            return f'<at user_id="{open_id}">{name}</at>'
        # 找不到，保持原样
        return m.group(0)

    # @中文名 或 @英文名 或 @ou_xxx
    return re.sub(r"@([\w\u4e00-\u9fff]{2,20})", _replacer, text)


def send_message(
    receive_id: str,
    content: str,
    receive_id_type: str = "open_id",
    msg_type: str = "text",
) -> str:
    """发送消息（底层）"""
    if msg_type == "text":
        import json
        content = _replace_at_mentions(content)
        body_content = json.dumps({"text": content})
    else:
        body_content = content

    data = feishu_post(
        "/im/v1/messages",
        json={
            "receive_id": receive_id,
            "msg_type": msg_type,
            "content": body_content,
        },
        params={"receive_id_type": receive_id_type},
    )
    if isinstance(data, str):
        return data

    resp_data = data.get("data", {})
    msg_id = resp_data.get("message_id", "")

    # 发送到 open_id 时，注册对方的 P2P chat_id 映射，
    # 这样后续 fetch_chat_history(ou_xxx) 可以拉到和对方的聊天记录
    if receive_id_type == "open_id" and receive_id.startswith("ou_"):
        chat_id = resp_data.get("chat_id", "")
        if chat_id:
            user_registry.register_p2p_chat(receive_id, chat_id)
            logger.info("registered p2p chat for %s → %s", receive_id[:15], chat_id[:15])

    return f"消息已发送 (message_id: {msg_id})"


def _find_shared_group(open_id: str) -> tuple[str, str]:
    """找到 bot 和目标用户共同所在的群，返回 (chat_id, group_name)。"""
    chats_data = feishu_get("/im/v1/chats", params={"page_size": 50})
    if isinstance(chats_data, str):
        return "", ""

    for chat in chats_data.get("data", {}).get("items", []):
        chat_id = chat.get("chat_id", "")
        if not chat_id:
            continue
        # 检查目标用户是否在这个群里
        member_data = feishu_get(
            f"/im/v1/chats/{chat_id}/members",
            params={"member_id_type": "open_id", "page_size": 100},
        )
        if isinstance(member_data, str):
            continue
        members = member_data.get("data", {}).get("items", [])
        for m in members:
            if m.get("member_id") == open_id:
                return chat_id, chat.get("name", "")
    return "", ""


def send_to_user(name_or_id: str, content: str) -> ToolResult:
    """给用户发私信。支持姓名（通过 user_registry 查找）或 open_id。
    如果私信失败（对方没跟 bot 聊过），自动降级到共同群聊 @对方。
    """
    open_id = name_or_id
    user_name = name_or_id  # 保留原始名字用于 @mention
    if not name_or_id.startswith("ou_"):
        found = user_registry.find_by_name(name_or_id)
        if not found:
            # 本地没找到 → 从通讯录同步再查一次
            try:
                user_registry.sync_org_contacts()
                found = user_registry.find_by_name(name_or_id)
            except Exception:
                logger.warning("sync_org_contacts failed in send_to_user", exc_info=True)
        if not found:
            return ToolResult.not_found(f"找不到用户「{name_or_id}」，请确认姓名或直接使用 open_id")
        open_id = found
    else:
        # open_id 传入时，尝试查名字
        user_name = user_registry.get_name(open_id) or open_id

    # 先尝试私信
    result = send_message(open_id, content, receive_id_type="open_id")

    # 230013 = bot 没有发私信的权限（对方没跟 bot 说过话）
    if "230013" in result:
        logger.info("DM failed (230013) for %s, falling back to group @mention", user_name)
        chat_id, group_name = _find_shared_group(open_id)
        if chat_id:
            # 用 @mention 格式在群里发消息
            at_content = f'<at user_id="{open_id}">{user_name}</at> {content}'
            group_result = send_message(chat_id, at_content, receive_id_type="chat_id")
            if not group_result.startswith("[ERROR]"):
                return ToolResult.success(f"私信权限不足（对方还没跟 bot 说过话），已改为在「{group_name}」群里 @{user_name} 发送。")
            return ToolResult.api_error(f"私信失败且群消息也发不出去：{group_result}")
        return ToolResult.error(
            f"私信失败：{user_name} 还没跟 bot 说过话，bot 无法主动私信。"
            f"而且找不到共同群聊来 @TA。\n"
            f"建议：让 {user_name} 先给 bot 发条消息，或者在群里 @TA。",
            code="blocked",
        )

    # result is the str from send_message — wrap it
    if result.startswith("[ERROR]"):
        return ToolResult.api_error(result)
    return ToolResult.success(result)


def send_to_group(chat_id: str, content: str) -> ToolResult:
    """往群里发消息。需要 chat_id，可先用 list_bot_groups 查看。"""
    if not chat_id.startswith("oc_"):
        return ToolResult.invalid_param(f"chat_id 格式不对（应以 oc_ 开头）: {chat_id}")
    result = send_message(chat_id, content, receive_id_type="chat_id")
    if result.startswith("[ERROR]"):
        return ToolResult.api_error(result)
    return ToolResult.success(result)


def _extract_post_text(content_raw: str) -> str:
    """从 post（富文本）消息中提取纯文本摘要"""
    try:
        content = _json.loads(content_raw)
    except (ValueError, TypeError):
        return "[富文本]"

    texts: list[str] = []

    # 收集所有 lang_content（zh_cn / en_us / ja_jp …）
    # 飞书 API 有时返回 {"zh_cn": {"content": ...}}，
    # 有时返回 {"content": [...]} 不带语言包装
    lang_blocks: list[dict] = []
    for val in content.values():
        if isinstance(val, dict) and "content" in val:
            lang_blocks.append(val)
    # 如果没找到语言子节点，把 content 自身当作 lang_content 试一次
    if not lang_blocks and "content" in content:
        lang_blocks.append(content)

    for lang_content in lang_blocks:
        if title := lang_content.get("title"):
            texts.append(title)
        for block in lang_content.get("content", []):
            if not isinstance(block, list):
                continue
            for item in block:
                tag = item.get("tag", "")
                if tag == "text":
                    texts.append(item.get("text", ""))
                elif tag == "at":
                    texts.append(f"@{item.get('user_name', '')}")
                elif tag == "a":
                    texts.append(item.get("text", ""))
    return " ".join(t for t in texts if t) or "[富文本]"


def list_bot_groups(page_size: int = 20, keyword: str = "") -> ToolResult:
    """列出 bot 所在的所有群，支持 keyword 模糊过滤群名"""
    data = feishu_get(
        "/im/v1/chats",
        params={"page_size": page_size},
    )
    if isinstance(data, str):
        return ToolResult.api_error(data)

    items = data.get("data", {}).get("items", [])
    if keyword:
        items = fuzzy_filter(items, keyword, ["name", "description"])
    if not items:
        msg = "bot 目前不在任何群里。" if not keyword else f"没有找到匹配「{keyword}」的群。"
        return ToolResult.success(msg)

    lines = [f"bot 在 {len(items)} 个群中：\n"]
    for chat in items:
        name = chat.get("name", "(未命名)")
        chat_id = chat.get("chat_id", "")
        desc = chat.get("description", "")
        member_count = chat.get("user_count") or chat.get("member_count") or "?"

        line = f"  - {name} ({member_count}人)"
        line += f"\n    chat_id: {chat_id}"
        if desc:
            line += f"\n    描述: {desc}"
        lines.append(line)

    return ToolResult.success("\n".join(lines))


def list_group_members(chat_id: str, keyword: str = "") -> ToolResult:
    """列出某个群里的所有成员，支持 keyword 模糊过滤姓名"""
    if not chat_id.startswith("oc_"):
        return ToolResult.invalid_param(f"chat_id 格式不对（应以 oc_ 开头）: {chat_id}")

    all_members = []
    page_token = ""
    while True:
        params: dict = {
            "member_id_type": "open_id",
            "page_size": 100,
        }
        if page_token:
            params["page_token"] = page_token

        data = feishu_get(
            f"/im/v1/chats/{chat_id}/members",
            params=params,
        )
        if isinstance(data, str):
            return ToolResult.api_error(data)

        members = data.get("data", {}).get("items", [])
        all_members.extend(members)

        if not data.get("data", {}).get("has_more", False):
            break
        page_token = data.get("data", {}).get("page_token", "")
        if not page_token:
            break

    # 先注册所有成员，再过滤
    for m in all_members:
        member_id = m.get("member_id", "")
        name = m.get("name", "")
        if member_id and name:
            user_registry.register(member_id, name)

    if keyword:
        all_members = fuzzy_filter(all_members, keyword, ["name"])

    if not all_members:
        msg = "该群没有成员（或无权限查看）。" if not keyword else f"没有找到匹配「{keyword}」的成员。"
        return ToolResult.success(msg)

    lines = [f"群成员（共 {len(all_members)} 人）：\n"]
    for m in all_members:
        name = m.get("name", "")
        member_id = m.get("member_id", "")

        if name:
            lines.append(f"  - {name} (open_id: {member_id})")
        else:
            lines.append(f"  - {member_id}")

    return ToolResult.success("\n".join(lines))


def fetch_chat_history(chat_id: str, count: int = 15) -> dict:
    """拉取最近的聊天记录，返回 dict 包含文本和图片引用。

    返回格式：
        {"text": "格式化聊天记录文本", "image_refs": [{"message_id": ..., "image_key": ...}, ...]}
    text 可直接用于 LLM 上下文；image_refs 可供调用方异步下载后传给 LLM。
    错误时返回 {"text": "[ERROR] ...", "image_refs": []}。

    chat_id 支持以下格式：
    - oc_xxx: 直接使用（群聊或私聊的真实 chat_id）
    - ou_xxx: 自动查找该用户与 bot 的私聊 chat_id
    - p2p_ou_xxx: 自动提取 open_id 后查找
    """
    _empty = {"text": "", "image_refs": []}
    if not chat_id:
        return _empty

    # 兼容错误格式：p2p_ou_xxx → 提取 ou_xxx
    if chat_id.startswith("p2p_"):
        chat_id = chat_id[4:]  # 去掉 "p2p_" 前缀

    # open_id → 查私聊 chat_id 映射
    if chat_id.startswith("ou_"):
        from app.services.user_registry import get_p2p_chat_id, get_name
        real_chat_id = get_p2p_chat_id(chat_id)
        if not real_chat_id:
            name = get_name(chat_id) or chat_id
            return {"text": f"[ERROR] 找不到与 {name} 的私聊记录。该用户可能还没跟 bot 私聊过。", "image_refs": []}
        chat_id = real_chat_id

    # 根据 chat_id 前缀判断容器类型
    if chat_id.startswith("oc_"):
        container_id_type = "chat"  # 群聊
    else:
        container_id_type = "p2p"   # 私聊

    # 飞书 API page_size 上限 50，超过会 400
    _PAGE_MAX = 50
    items: list[dict] = []
    page_token = ""
    remaining = max(count, 1)

    while remaining > 0:
        page_size = min(remaining, _PAGE_MAX)
        params: dict = {
            "container_id": chat_id,
            "container_id_type": container_id_type,
            "page_size": page_size,
            "sort_type": "ByCreateTimeDesc",
        }
        if page_token:
            params["page_token"] = page_token

        data = feishu_get("/im/v1/messages", params=params)
        if isinstance(data, str):
            if items:
                break  # 已经拿到一些消息，不要因为翻页失败全丢
            logger.warning("fetch_chat_history failed: %s", data)
            return {"text": data, "image_refs": []}

        page_items = data.get("data", {}).get("items", [])
        items.extend(page_items)
        remaining -= len(page_items)

        # 检查是否还有更多
        has_more = data.get("data", {}).get("has_more", False)
        page_token = data.get("data", {}).get("page_token", "")
        if not has_more or not page_token or not page_items:
            break

    if not items:
        return _empty

    # items 是倒序（最新在前），反转为时间正序
    items.reverse()

    lines = []
    image_refs: list[dict] = []   # 收集图片引用，供调用方下载
    img_counter = 0

    # debug: 记录消息类型分布，帮助排查图片解析问题
    from collections import Counter
    _type_counts = Counter(m.get("msg_type", "unknown") for m in items)
    logger.info("fetch_chat_history: %d msgs, types=%s", len(items), dict(_type_counts))

    for msg in items:
        sender = msg.get("sender", {})
        sender_type = sender.get("sender_type", "")
        sender_id = sender.get("id", "")
        msg_type = msg.get("msg_type", "")
        message_id = msg.get("message_id", "")
        body = msg.get("body", {})
        content_raw = body.get("content", "")

        # 解析发送者
        if sender_type == "app":
            name = "[bot]"
        else:
            from app.services import user_registry
            name = user_registry.get_name(sender_id) or sender_id[:12]

        # 解析消息内容
        text = ""
        if msg_type == "text":
            try:
                text = _json.loads(content_raw).get("text", content_raw)
            except (ValueError, TypeError):
                text = content_raw
        elif msg_type == "post":
            text = _extract_post_text(content_raw)
            # post 中也可能嵌入图片
            if message_id:
                try:
                    post_content = _json.loads(content_raw)
                    # 复用 _extract_post_text 相同的逻辑找 lang_blocks
                    _lang_blocks: list[dict] = []
                    for val in post_content.values():
                        if isinstance(val, dict) and "content" in val:
                            _lang_blocks.append(val)
                    if not _lang_blocks and "content" in post_content:
                        _lang_blocks.append(post_content)
                    for _lb in _lang_blocks:
                        for block in _lb.get("content", []):
                            if not isinstance(block, list):
                                continue
                            for elem in block:
                                if elem.get("tag") == "img" and elem.get("image_key"):
                                    img_counter += 1
                                    image_refs.append({
                                        "message_id": message_id,
                                        "image_key": elem["image_key"],
                                    })
                                    text += f" [图片{img_counter}]"
                except (ValueError, TypeError):
                    pass
        elif msg_type == "image":
            # 提取 image_key 并记录引用
            try:
                img_content = _json.loads(content_raw)
                image_key = img_content.get("image_key", "")
            except (ValueError, TypeError):
                image_key = ""
            if image_key and message_id:
                img_counter += 1
                image_refs.append({
                    "message_id": message_id,
                    "image_key": image_key,
                })
                text = f"[图片{img_counter}]"
            else:
                text = "[图片]"
        elif msg_type == "file":
            text = "[文件]"
        elif msg_type == "interactive":
            text = "[卡片消息]"
        elif msg_type == "hongbao":
            text = "[红包]"
        elif msg_type == "sticker":
            text = "[表情包]"
        elif msg_type == "audio":
            text = "[语音]"
        elif msg_type == "video":
            text = "[视频]"
        elif msg_type == "share_chat":
            text = "[分享群名片]"
        elif msg_type == "share_user":
            text = "[分享个人名片]"
        elif msg_type == "system":
            continue
        else:
            text = f"[{msg_type}消息]"

        if text:
            lines.append(f"{name}: {text}")

    if not lines:
        return _empty

    if image_refs:
        logger.info("fetch_chat_history: found %d image_refs", len(image_refs))

    header = "以下是当前对话的最近聊天记录"
    if image_refs:
        header += f"（含 {len(image_refs)} 张图片，已附在消息中，按编号对应）"
    header += "：\n"
    return {"text": header + "\n".join(lines), "image_refs": image_refs}


# --------------- Tool definitions & map ---------------

TOOL_DEFINITIONS = [
    {
        "name": "send_message_to_user",
        "description": "给飞书用户发消息。可以用姓名（自动查找 open_id）或直接用 open_id。如果私信发不出去（对方没跟 bot 聊过），会自动降级到共同群聊 @对方。",
        "input_schema": {
            "type": "object",
            "properties": {
                "name_or_id": {
                    "type": "string",
                    "description": "用户姓名或 open_id。姓名会自动从已知用户中匹配。",
                },
                "content": {
                    "type": "string",
                    "description": "消息内容（纯文本）。可以用 @姓名 来 at 某人，会自动转换为飞书 at 标签。",
                },
            },
            "required": ["name_or_id", "content"],
        },
    },
    {
        "name": "send_message_to_group",
        "description": "往飞书群里发消息。需要 chat_id，可先用 list_bot_groups 查看 bot 所在的群。支持 @姓名 来 at 群成员。",
        "input_schema": {
            "type": "object",
            "properties": {
                "chat_id": {
                    "type": "string",
                    "description": "群的 chat_id（以 oc_ 开头）。可先用 list_bot_groups 查看。",
                },
                "content": {
                    "type": "string",
                    "description": "消息内容（纯文本）。可以用 @姓名 来 at 某人，会自动转换为飞书 at 标签。",
                },
            },
            "required": ["chat_id", "content"],
        },
    },
    {
        "name": "list_bot_groups",
        "description": "列出 bot 所在的所有飞书群，返回群名和 chat_id。支持 keyword 模糊过滤群名。",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "按群名模糊过滤（支持子串、多词、去标点匹配）",
                    "default": "",
                },
            },
        },
    },
    {
        "name": "list_group_members",
        "description": "列出某个飞书群里的所有成员（姓名 + open_id）。支持 keyword 模糊过滤姓名。需要 chat_id，可先用 list_bot_groups 查看。",
        "input_schema": {
            "type": "object",
            "properties": {
                "chat_id": {
                    "type": "string",
                    "description": "群的 chat_id（以 oc_ 开头）。可先用 list_bot_groups 查看。",
                },
                "keyword": {
                    "type": "string",
                    "description": "按成员姓名模糊过滤",
                    "default": "",
                },
            },
            "required": ["chat_id"],
        },
    },
    {
        "name": "fetch_chat_history",
        "description": "读取某个群或私聊的最近聊天记录。可以看到谁说了什么。群聊用 chat_id（oc_ 开头），私聊可以直接传对方的 open_id（ou_ 开头，会自动查找私聊 chat_id）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "chat_id": {
                    "type": "string",
                    "description": "群的 chat_id（oc_ 开头）或用户的 open_id（ou_ 开头，自动查找私聊）。",
                },
                "count": {
                    "type": "integer",
                    "description": "要拉取的消息数量，默认 15。支持翻页，可传 >50 的值来获取更早的历史消息（如需回溯较久前的消息可传 100-200）。",
                    "default": 15,
                },
            },
            "required": ["chat_id"],
        },
    },
]

TOOL_MAP = {
    "send_message_to_user": lambda args: send_to_user(
        name_or_id=args["name_or_id"],
        content=args["content"],
    ),
    "send_message_to_group": lambda args: send_to_group(
        chat_id=args["chat_id"],
        content=args["content"],
    ),
    "list_bot_groups": lambda args: list_bot_groups(keyword=args.get("keyword", "")),
    "list_group_members": lambda args: list_group_members(chat_id=args["chat_id"], keyword=args.get("keyword", "")),
    "fetch_chat_history": lambda args: fetch_chat_history(
        chat_id=args["chat_id"],
        count=args.get("count", 15),
    ),
}