"""跨平台身份关联工具

让 bot 能够：
1. 搜索记忆中的已知用户（按名字）
2. 发起跨平台身份验证（给已知 channel 的用户发验证码）
3. 确认验证码（完成跨平台链接）
4. 查看当前用户的跨平台身份信息
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ── Tool Definitions ──

TOOL_DEFINITIONS = [
    {
        "name": "search_known_user",
        "description": (
            "搜索已知用户（在记忆中按名字查找）。"
            "用于当一个陌生用户声称自己是某人时，验证该名字是否在其他平台/channel 有交互记录。"
            "返回匹配的用户列表及其关联的平台信息。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "要搜索的用户名（模糊匹配）",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "initiate_identity_verification",
        "description": (
            "发起跨平台身份验证。当你怀疑当前用户可能是另一个平台的已知用户时，"
            "调用此工具生成验证码并通过已知 channel 发送给目标用户。"
            "例：企微来了个人说'我是 Steven'，你搜索到飞书有个 Steven，"
            "就通过飞书给 Steven 发验证码，让他在企微回复确认。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "identity_id": {
                    "type": "string",
                    "description": "目标用户的 identity ID（从 search_known_user 获取）",
                },
                "target_platform": {
                    "type": "string",
                    "description": "要发验证码的目标平台（已知用户所在平台）",
                    "enum": ["feishu", "wecom", "wecom_kf"],
                },
                "target_user_id": {
                    "type": "string",
                    "description": "目标平台上的用户 ID（从 search_known_user 的 linked_platforms 获取）",
                },
                "message": {
                    "type": "string",
                    "description": "发给目标用户的验证消息（包含验证码），用自然语言",
                },
            },
            "required": ["identity_id", "target_platform", "target_user_id"],
        },
    },
    {
        "name": "confirm_identity_verification",
        "description": (
            "确认跨平台身份验证码。当用户回复了验证码时调用此工具完成身份关联。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "用户提供的 6 位验证码",
                },
            },
            "required": ["code"],
        },
    },
    {
        "name": "get_user_identity",
        "description": (
            "查看当前用户的跨平台身份信息。"
            "返回该用户关联的所有平台 ID 和基本信息。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


# ── Tool Handlers ──

async def search_known_user(args: dict[str, Any]) -> str:
    """搜索已知用户。"""
    from app.services.identity import search_identity_by_name
    from app.tenant.context import get_current_tenant

    name = args.get("name", "").strip()
    if not name:
        return "请提供要搜索的用户名"

    tenant = get_current_tenant()
    results = search_identity_by_name(name, bot_id=tenant.tenant_id)

    if not results:
        # 也搜索记忆系统中的用户
        return f"未找到名为 '{name}' 的已知用户。该用户可能还没有在任何 channel 和我交互过。"

    lines = [f"找到 {len(results)} 个匹配用户："]
    for u in results:
        linked = u.get("linked_platforms", {})
        platforms_str = ", ".join(f"{p}: {uid[:12]}..." for p, uid in linked.items()) if linked else "无关联平台"
        lines.append(
            f"- {u.get('name', '?')} (ID: {u.get('identity_id', '?')[:8]}...)\n"
            f"  平台: {platforms_str}\n"
            f"  创建: {u.get('created_at', '?')[:16]}"
        )
    return "\n".join(lines)


async def initiate_identity_verification(args: dict[str, Any]) -> str:
    """发起跨平台验证。"""
    from app.services.identity import initiate_verification, get_identity
    from app.tenant.context import get_current_tenant, get_current_sender

    identity_id = args.get("identity_id", "")
    target_platform = args.get("target_platform", "")
    target_user_id = args.get("target_user_id", "")
    custom_message = args.get("message", "")

    if not identity_id or not target_platform or not target_user_id:
        return "缺少必要参数：identity_id, target_platform, target_user_id"

    tenant = get_current_tenant()
    sender = get_current_sender()

    # 生成验证码
    code = initiate_verification(
        identity_id=identity_id,
        from_platform=sender.channel_platform,
        from_pid=sender.sender_id,
        target_platform=target_platform,
        target_pid=target_user_id,
    )
    if not code:
        return "验证码生成失败（Redis 不可用）"

    # 通过目标 channel 发送验证码
    send_result = await _send_verification_message(
        tenant, target_platform, target_user_id, code, custom_message,
    )

    if send_result:
        return (
            f"验证码已发送！\n"
            f"已通过 {target_platform} 给目标用户发送了验证码 {code}。\n"
            f"请告诉当前用户：在这里回复验证码即可完成身份关联。\n"
            f"验证码有效期 10 分钟。"
        )
    else:
        return (
            f"验证码已生成但发送失败。\n"
            f"验证码: {code}（有效期 10 分钟）\n"
            f"请告诉用户通过其他方式获取验证码，然后在这里回复。"
        )


async def confirm_identity_verification(args: dict[str, Any]) -> str:
    """确认验证码。"""
    from app.services.identity import verify_code
    from app.tenant.context import get_current_tenant, get_current_sender

    code = args.get("code", "").strip()
    if not code or len(code) != 6:
        return "请提供 6 位数字验证码"

    tenant = get_current_tenant()
    sender = get_current_sender()

    result = verify_code(
        code=code,
        sender_platform=sender.channel_platform,
        sender_id=sender.sender_id,
        bot_id=tenant.tenant_id,
    )

    if not result:
        return "验证码无效或已过期。请重新发起验证。"

    linked = result.get("linked", {})
    platforms_str = ", ".join(f"{p}: {uid[:12]}..." for p, uid in linked.items())
    return (
        f"身份验证成功！\n"
        f"已关联平台: {platforms_str}\n"
        f"现在你在所有关联平台上的记忆和对话上下文将自动共享。"
    )


async def get_user_identity(args: dict[str, Any]) -> str:
    """查看当前用户的跨平台身份。"""
    from app.services.identity import find_identity, get_identity, get_linked_platforms
    from app.tenant.context import get_current_tenant, get_current_sender

    tenant = get_current_tenant()
    sender = get_current_sender()

    identity_id = find_identity(sender.channel_platform, sender.sender_id)
    if not identity_id:
        return (
            f"当前用户 ({sender.channel_platform}: {sender.sender_id[:12]}...) "
            f"尚未关联跨平台身份。\n"
            f"如果你在其他平台也和我聊过，可以告诉我你的名字，我来帮你关联。"
        )

    info = get_identity(identity_id)
    linked = get_linked_platforms(identity_id, tenant.tenant_id)

    name = info.get("name", "未知") if info else "未知"
    platforms_lines = [f"  - {p}: {uid[:16]}..." for p, uid in linked.items()]
    return (
        f"用户身份: {name}\n"
        f"Identity ID: {identity_id[:8]}...\n"
        f"关联平台:\n" + ("\n".join(platforms_lines) if platforms_lines else "  (无)")
    )


# ── 辅助：通过指定 channel 发消息 ──

async def _send_verification_message(
    tenant,
    target_platform: str,
    target_user_id: str,
    code: str,
    custom_message: str = "",
) -> bool:
    """通过指定平台的 channel 给用户发送验证消息。"""
    msg = custom_message or (
        f"有人在另一个平台声称是你，验证码: {code}\n"
        f"如果是你本人，请在那个平台回复这个验证码。\n"
        f"如果不是你，请忽略此消息。（验证码 10 分钟后过期）"
    )

    try:
        if target_platform == "feishu":
            from app.services.feishu import feishu_client
            result = await feishu_client.send_to_chat(target_user_id, msg)
            return bool(result and "message_id" in str(result))

        elif target_platform == "wecom":
            from app.services.wecom import wecom_client
            result = await wecom_client.send_text(target_user_id, msg)
            return bool(result and result.get("errcode", -1) == 0)

        elif target_platform == "wecom_kf":
            from app.services.wecom_kf import wecom_kf_client
            ch = tenant.get_channel("wecom_kf")
            open_kfid = ch.wecom_kf_open_kfid if ch else tenant.wecom_kf_open_kfid
            result = await wecom_kf_client.send_text(target_user_id, msg, open_kfid=open_kfid)
            return bool(result and result.get("errcode", -1) == 0)

        else:
            logger.warning("identity: unsupported platform %s for verification", target_platform)
            return False
    except Exception:
        logger.warning("identity: send verification to %s:%s failed",
                       target_platform, target_user_id[:12], exc_info=True)
        return False


# ── Tool Map ──

TOOL_MAP = {
    "search_known_user": search_known_user,
    "initiate_identity_verification": initiate_identity_verification,
    "confirm_identity_verification": confirm_identity_verification,
    "get_user_identity": get_user_identity,
}
