"""客户管理 + 开通审批 + Co-tenant 管理工具

安全模型：
- 超管专属工具（硬拦截）：bind_customer, list_customers, update_customer_notes,
  list_provision_requests, approve_provision_request, reject_provision_request,
  add_co_tenant, confirm_add_co_tenant, remove_co_tenant, list_co_tenants
- 任何人可用：request_provision（创建待审批请求）, lookup_customer, customer_instance_status
- 敏感工具（provision_ops.py 里的 provision_tenant 等）也需在 agent 层拦截

拦截机制：工具 handler 通过 get_current_sender() 读取 contextvar，
判断 is_super_admin，非超管调用超管工具直接返回权限错误。
这是硬拦截，不依赖 LLM 遵守 system prompt。

安全增强（两步确认）：
add_co_tenant 不再直接执行，而是返回预览信息 + 确认 token。
必须用户确认后调用 confirm_add_co_tenant(token) 才真正创建租户。
防止 LLM 误判意图直接创建新 bot。
"""

from __future__ import annotations

import json
import logging
import secrets
import time

from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

# ── 两步确认：add_co_tenant 的 pending 请求 ──
# {token: {"config": dict, "created_at": float, "sender_id": str}}
_pending_co_tenant: dict[str, dict] = {}
_PENDING_TTL = 300  # 5 分钟过期


def _cleanup_expired_pending():
    """清理过期的 pending 确认。"""
    now = time.time()
    expired = [k for k, v in _pending_co_tenant.items()
               if now - v["created_at"] > _PENDING_TTL]
    for k in expired:
        del _pending_co_tenant[k]


def _require_super_admin() -> ToolResult | None:
    """检查当前发送者是否为超管。非超管返回错误 ToolResult，超管返回 None（放行）。"""
    from app.tenant.context import get_current_sender
    sender = get_current_sender()
    if not sender.is_super_admin:
        return ToolResult.error(
            "该操作需要管理员权限。如需开通 bot 实例，请用 request_provision 提交申请。",
            code="permission",
        )
    return None


# ── 超管专属工具 ──────────────────────────────────────────────────


def _bind_customer(args: dict) -> ToolResult:
    """绑定客户与实例（超管专属）"""
    denied = _require_super_admin()
    if denied:
        return denied
    from app.services.customer_store import bind_customer

    external_userid = args.get("external_userid", "").strip()
    tenant_id = args.get("tenant_id", "").strip()
    name = args.get("name", "").strip()
    platform = args.get("platform", "").strip()
    port = args.get("port", 0)
    notes = args.get("notes", "").strip()

    if not external_userid:
        return ToolResult.invalid_param("Missing external_userid")
    if not tenant_id:
        return ToolResult.invalid_param("Missing tenant_id")

    ok = bind_customer(
        external_userid=external_userid,
        tenant_id=tenant_id,
        name=name,
        platform=platform,
        port=int(port) if port else 0,
        notes=notes,
    )
    if ok:
        return ToolResult.success(f"已绑定客户 {name or external_userid} → 实例 {tenant_id}")
    return ToolResult.error("绑定失败（Redis 不可用或参数缺失）")


def _list_customers(args: dict) -> ToolResult:
    """列出所有已绑定客户（超管专属）"""
    denied = _require_super_admin()
    if denied:
        return denied
    from app.services.customer_store import list_customers

    customers = list_customers()
    if not customers:
        return ToolResult.success("当前没有已绑定的客户。")
    return ToolResult.success(json.dumps(customers, indent=2, ensure_ascii=False))


def _update_customer_notes(args: dict) -> ToolResult:
    """更新客户备注（超管专属）"""
    denied = _require_super_admin()
    if denied:
        return denied
    from app.services.customer_store import update_customer

    external_userid = args.get("external_userid", "").strip()
    if not external_userid:
        return ToolResult.invalid_param("Missing external_userid")

    updates = {}
    if args.get("name"):
        updates["name"] = args["name"].strip()
    if args.get("notes"):
        updates["notes"] = args["notes"].strip()

    if not updates:
        return ToolResult.invalid_param("至少提供 name 或 notes 其中一个")

    ok = update_customer(external_userid, **updates)
    if ok:
        return ToolResult.success("客户信息已更新。")
    return ToolResult.error("更新失败（客户不存在或 Redis 不可用）")


# ── 审批工具（超管专属）──────────────────────────────────────────


def _list_provision_requests(args: dict) -> ToolResult:
    """列出待审批的开通请求（超管专属）"""
    denied = _require_super_admin()
    if denied:
        return denied
    from app.services.provision_approval import list_pending

    status_filter = args.get("status", "pending").strip()
    if status_filter == "all":
        from app.services.provision_approval import list_all
        requests = list_all()
    else:
        requests = list_pending()

    if not requests:
        return ToolResult.success("当前没有待审批的开通请求。")
    # 脱敏展示
    for r in requests:
        r.pop("provision_result", None)  # 太长
    return ToolResult.success(json.dumps(requests, indent=2, ensure_ascii=False))


def _approve_provision_request(args: dict) -> ToolResult:
    """审批通过开通请求 → 自动 provision（超管专属）"""
    denied = _require_super_admin()
    if denied:
        return denied
    from app.services.provision_approval import approve_request
    from app.tenant.context import get_current_sender

    request_id = args.get("request_id", "").strip()
    if not request_id:
        return ToolResult.invalid_param("Missing request_id")

    sender = get_current_sender()
    result = approve_request(request_id, approved_by=sender.sender_name or sender.sender_id)
    if not result:
        return ToolResult.error(f"请求 {request_id} 不存在", code="not_found")

    if result.get("status") != "approved":
        return ToolResult.success(f"请求已是 {result['status']} 状态，无需重复操作。")

    pr = result.get("provision_result", {})
    if pr and pr.get("ok"):
        return ToolResult.success(
            f"已批准并自动开通实例 {result['tenant_id']}。\n"
            f"端口: {pr.get('port')}\n"
            f"Webhook: {pr.get('webhook_path', '')}\n"
            f"客户 {result['requester_name']} 已自动绑定。"
        )
    else:
        error = (pr or {}).get("error", "未知错误")
        return ToolResult.success(
            f"已批准请求 {request_id}，但自动开通失败: {error}\n"
            f"可手动用 provision_tenant 重试。"
        )


def _reject_provision_request(args: dict) -> ToolResult:
    """拒绝开通请求（超管专属）"""
    denied = _require_super_admin()
    if denied:
        return denied
    from app.services.provision_approval import reject_request
    from app.tenant.context import get_current_sender

    request_id = args.get("request_id", "").strip()
    if not request_id:
        return ToolResult.invalid_param("Missing request_id")

    reason = args.get("reason", "").strip()
    sender = get_current_sender()
    result = reject_request(
        request_id,
        rejected_by=sender.sender_name or sender.sender_id,
        reason=reason,
    )
    if not result:
        return ToolResult.error(f"请求 {request_id} 不存在", code="not_found")
    return ToolResult.success(f"已拒绝请求 {request_id}。" + (f"原因: {reason}" if reason else ""))


# ── 任何人可用的工具 ──────────────────────────────────────────────


def _request_provision(args: dict) -> ToolResult:
    """客户请求开通 bot（任何人可用，创建待审批请求）"""
    from app.services.deploy_quota import check_deploy_quota, init_user_quota
    from app.services.provision_approval import create_request
    from app.tenant.context import get_current_sender, get_current_tenant

    sender = get_current_sender()
    tenant = get_current_tenant()

    # ── 部署配额检查（超管跳过）──
    if not sender.is_super_admin:
        free_quota = tenant.deploy_free_quota
        quota_info = check_deploy_quota(tenant.tenant_id, sender.sender_id, free_quota)
        if not quota_info["allowed"]:
            return ToolResult.error(
                f"您的免费部署额度已用完（{quota_info['used']}/{quota_info['total']}）。\n"
                f"如需更多部署名额，请联系管理员或了解付费方案。",
                code="quota_exceeded",
            )
        # 初始化配额记录（首次请求时）
        init_user_quota(tenant.tenant_id, sender.sender_id, free_quota)

    tenant_id = args.get("tenant_id", "").strip()
    name = args.get("name", "").strip()
    platform = args.get("platform", "").strip()
    credentials_json = args.get("credentials_json", "{}").strip()

    if not tenant_id:
        return ToolResult.invalid_param("Missing tenant_id")
    if not name:
        return ToolResult.invalid_param("Missing name")
    if not platform:
        return ToolResult.invalid_param("Missing platform")

    try:
        credentials = json.loads(credentials_json)
    except json.JSONDecodeError as e:
        return ToolResult.invalid_param(f"Invalid credentials_json: {e}")

    result = create_request(
        requester_id=sender.sender_id,
        requester_name=sender.sender_name or sender.sender_id,
        tenant_id=tenant_id,
        name=name,
        platform=platform,
        credentials=credentials,
        llm_system_prompt=args.get("llm_system_prompt", ""),
        custom_persona=bool(args.get("custom_persona", False)),
        capability_modules=args.get("capability_modules"),
        notes=args.get("notes", ""),
        source_tenant_id=tenant.tenant_id,
    )
    if not result:
        return ToolResult.error("提交失败（系统暂时不可用），请稍后重试。")

    return ToolResult.success(
        f"开通请求已提交（{result['request_id']}），正在等待管理员审批。\n"
        f"管理员会尽快处理，请耐心等待。"
    )


def _lookup_customer(args: dict) -> ToolResult:
    """查找客户绑定信息"""
    from app.services.customer_store import get_customer, get_customer_by_tenant

    external_userid = args.get("external_userid", "").strip()
    tenant_id = args.get("tenant_id", "").strip()

    if not external_userid and not tenant_id:
        return ToolResult.invalid_param("需要提供 external_userid 或 tenant_id 其中一个")

    info = None
    if external_userid:
        info = get_customer(external_userid)
    elif tenant_id:
        info = get_customer_by_tenant(tenant_id)

    if not info:
        return ToolResult.success("未找到该客户的绑定记录。可能尚未开通实例。")

    return ToolResult.success(json.dumps(info, indent=2, ensure_ascii=False))


def _customer_instance_status(args: dict) -> ToolResult:
    """查看客户实例状态+用量"""
    from app.services.customer_store import get_customer
    from app.services.provisioner import instance_status
    from app.services.metering import get_usage_summary

    external_userid = args.get("external_userid", "").strip()
    tenant_id = args.get("tenant_id", "").strip()

    if external_userid and not tenant_id:
        info = get_customer(external_userid)
        if not info:
            return ToolResult.success(
                "该用户尚未绑定实例。如果是新客户，请用 request_provision 提交开通申请。"
            )
        tenant_id = info.get("tenant_id", "")

    if not tenant_id:
        return ToolResult.invalid_param("需要提供 external_userid 或 tenant_id")

    status = instance_status(tenant_id)

    from datetime import datetime, timezone
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    usage = get_usage_summary(tenant_id, month)

    result = {
        "instance": status,
        "usage_this_month": usage,
    }
    return ToolResult.success(json.dumps(result, indent=2, ensure_ascii=False))


# ── 白名单管理（超管专属）──────────────────────────────────────────


def _manage_allowed_users(args: dict) -> ToolResult:
    """管理 bot 白名单（超管专属）。

    支持 add / remove / list 三种操作。
    add 时通过昵称从 user_registry 中匹配 external_userid。
    """
    denied = _require_super_admin()
    if denied:
        return denied

    from app.tenant.registry import tenant_registry
    from app.tenant.context import get_current_tenant, set_current_tenant
    from app.services import user_registry

    action = args.get("action", "list").strip().lower()
    tenant_id = args.get("tenant_id", "").strip()

    # 如果没指定 tenant_id，默认当前租户
    if not tenant_id:
        tenant_id = get_current_tenant().tenant_id

    target = tenant_registry.get(tenant_id)
    if not target:
        return ToolResult.error(f"租户 {tenant_id} 不存在")

    if action == "list":
        users = target.allowed_users or []
        if not users:
            return ToolResult.success(f"租户 {tenant_id} 的白名单为空（所有人可用）。")
        lines = [f"租户 {tenant_id} 白名单（{len(users)} 人）："]
        for u in users:
            nick = u.get("nickname", "未知")
            euid = u.get("external_userid", "")
            owner_mark = " [owner]" if euid == target.owner else ""
            lines.append(f"  - {nick} ({euid[:12]}...){owner_mark}")
        return ToolResult.success("\n".join(lines))

    if action == "add":
        nicknames = args.get("nicknames", [])
        if isinstance(nicknames, str):
            nicknames = [n.strip() for n in nicknames.split(",") if n.strip()]
        # 也支持直接传 external_userid（不需要昵称匹配）
        external_userids = args.get("external_userids", [])
        if isinstance(external_userids, str):
            external_userids = [e.strip() for e in external_userids.split(",") if e.strip()]

        if not nicknames and not external_userids:
            return ToolResult.invalid_param(
                "add 操作需要 nicknames（昵称列表）或 external_userids（用户ID列表）其中一个"
            )

        # 切换到目标租户的上下文来查找用户
        current = get_current_tenant()
        set_current_tenant(target)
        results = []
        added = 0
        current_allowed = list(target.allowed_users or [])
        current_ids = {u.get("external_userid", "") for u in current_allowed if isinstance(u, dict)}

        # 通过 external_userid 直接添加
        for euid in external_userids:
            if euid in current_ids:
                results.append(f"{euid[:12]}... 已在白名单中")
                continue
            # 尝试从 user_registry 获取昵称
            nick = user_registry.get_name(euid)
            if not nick:
                # 尝试从微信客服 API 获取昵称
                try:
                    import asyncio
                    from app.services.wecom_kf import wecom_kf_client
                    nick = asyncio.get_event_loop().run_until_complete(
                        wecom_kf_client.get_customer_name(euid)
                    )
                except Exception:
                    pass
            nick = nick or euid[:12] + "..."
            current_allowed.append({"external_userid": euid, "nickname": nick})
            current_ids.add(euid)
            added += 1
            results.append(f"已添加「{nick}」({euid[:12]}...)")

        # 通过昵称匹配添加
        for nick in nicknames:
            uid = user_registry.find_by_name(nick)
            if not uid:
                # 尝试在所有已知用户中模糊匹配
                all_known = user_registry.all_users()
                candidates = [(k, v) for k, v in all_known.items() if nick in v or v in nick]
                if len(candidates) == 1:
                    uid = candidates[0][0]
                elif len(candidates) > 1:
                    options = ", ".join(f"{v}({k[:8]}...)" for k, v in candidates[:5])
                    results.append(f"「{nick}」匹配到多个用户: {options}，请更精确")
                    continue
                else:
                    results.append(f"「{nick}」未找到（该用户可能还没跟 bot 聊过天，可以用 external_userids 直接传 ID 添加）")
                    continue

            if uid in current_ids:
                actual_name = user_registry.get_name(uid) or nick
                results.append(f"「{actual_name}」已在白名单中")
                continue

            actual_name = user_registry.get_name(uid) or nick
            current_allowed.append({"external_userid": uid, "nickname": actual_name})
            current_ids.add(uid)
            added += 1
            results.append(f"已添加「{actual_name}」")

        set_current_tenant(current)

        if added > 0:
            target.allowed_users = current_allowed
            _persist_allowed_users(tenant_id, current_allowed, target.owner)

        return ToolResult.success("\n".join(results))

    if action == "remove":
        nicknames = args.get("nicknames", [])
        if isinstance(nicknames, str):
            nicknames = [n.strip() for n in nicknames.split(",") if n.strip()]
        if not nicknames:
            return ToolResult.invalid_param("remove 操作需要 nicknames 参数")

        current = get_current_tenant()
        set_current_tenant(target)
        current_allowed = list(target.allowed_users or [])
        results = []
        removed = 0

        for nick in nicknames:
            found = False
            for i, u in enumerate(current_allowed):
                if isinstance(u, dict) and (
                    u.get("nickname", "") == nick
                    or nick in u.get("nickname", "")
                    or u.get("nickname", "") in nick
                ):
                    results.append(f"已移除「{u.get('nickname', '')}」")
                    current_allowed.pop(i)
                    removed += 1
                    found = True
                    break
            if not found:
                results.append(f"「{nick}」不在白名单中")

        set_current_tenant(current)

        if removed > 0:
            target.allowed_users = current_allowed
            _persist_allowed_users(tenant_id, current_allowed, target.owner)

        return ToolResult.success("\n".join(results))

    return ToolResult.invalid_param(f"不支持的操作: {action}，可用: add / remove / list")


def _persist_allowed_users(tenant_id: str, allowed_users: list, owner: str) -> None:
    """将 allowed_users 持久化到 Redis tenant_cfg + tenants.json"""
    try:
        from app.services.tenant_sync import publish_tenant_update
        publish_tenant_update("update", {
            "tenant_id": tenant_id,
            "allowed_users": allowed_users,
            "owner": owner,
        })
    except Exception:
        logger.warning("persist allowed_users to Redis failed for %s", tenant_id)

    # 也更新本地 tenants.json
    try:
        from pathlib import Path
        for candidate in ("/app/tenants.json", "tenants.json"):
            path = Path(candidate)
            if not path.exists():
                continue
            import json as json_mod
            data = json_mod.loads(path.read_text())
            tenants = data.get("tenants", [])
            for t in tenants:
                if t.get("tenant_id") == tenant_id:
                    t["allowed_users"] = allowed_users
                    t["owner"] = owner
                    break
            data["tenants"] = tenants
            path.write_text(json_mod.dumps(data, indent=2, ensure_ascii=False) + "\n")
            break
    except Exception:
        logger.warning("persist allowed_users to tenants.json failed for %s", tenant_id)


# ── Co-tenant Management ──────────────────────────────────────────


def _add_co_tenant(args: dict) -> ToolResult:
    """添加 co-tenant 第一步：预览并生成确认 token（不执行）。

    必须让用户看到预览信息并明确确认后，才调用 confirm_add_co_tenant 执行。
    """
    check = _require_super_admin()
    if check:
        return check

    from app.tenant.context import get_current_sender, get_current_tenant
    from app.tenant.registry import tenant_registry

    _cleanup_expired_pending()

    tenant = get_current_tenant()
    if tenant.platform != "wecom_kf":
        return ToolResult.error("Co-tenant 仅支持 wecom_kf 平台")

    tenant_id = args.get("tenant_id", "").strip()
    name = args.get("name", "").strip()
    open_kfid = args.get("wecom_kf_open_kfid", "").strip()

    if not tenant_id or not name or not open_kfid:
        return ToolResult.invalid_param(
            "需要 tenant_id, name, wecom_kf_open_kfid 三个必填字段"
        )

    # 检查是否已存在
    existing = tenant_registry.get(tenant_id)
    if existing:
        return ToolResult.error(f"租户 {tenant_id} 已存在")

    # 从 primary tenant 继承配置（与 dashboard api_add_co_tenant 逻辑对齐）
    new_config = {
        "tenant_id": tenant_id,
        "name": name,
        "platform": "wecom_kf",
        # 继承凭证
        "wecom_corpid": tenant.wecom_corpid,
        "wecom_kf_secret": tenant.wecom_kf_secret,
        "wecom_kf_token": tenant.wecom_kf_token,
        "wecom_kf_encoding_aes_key": tenant.wecom_kf_encoding_aes_key,
        "wecom_kf_open_kfid": open_kfid,
        # LLM 配置（用环境变量引用，不泄露实际 key）
        "llm_provider": tenant.llm_provider or "gemini",
        "llm_api_key": "${GEMINI_API_KEY}",
        "llm_model": tenant.llm_model or "gemini-3-flash-preview",
        "llm_model_strong": tenant.llm_model_strong or "gemini-3.1-pro-preview",
        "coding_model": "",
        # 继承运营配置
        "trial_enabled": tenant.trial_enabled,
        "trial_duration_hours": tenant.trial_duration_hours,
        "quota_user_tokens_6h": tenant.quota_user_tokens_6h,
        "memory_diary_enabled": tenant.memory_diary_enabled,
        "memory_context_enabled": tenant.memory_context_enabled,
        "memory_chat_rounds": tenant.memory_chat_rounds,
        "memory_chat_ttl": tenant.memory_chat_ttl,
        # co-tenant 安全默认
        "instance_management_enabled": False,
        "self_iteration_enabled": False,
    }

    # 可选覆盖字段
    if args.get("llm_system_prompt"):
        new_config["llm_system_prompt"] = args["llm_system_prompt"]
    if args.get("custom_persona") is not None:
        new_config["custom_persona"] = bool(args["custom_persona"])
    if args.get("tools_enabled"):
        new_config["tools_enabled"] = args["tools_enabled"]

    # 生成确认 token（不执行）
    token = f"cotenant_{secrets.token_hex(8)}"
    sender = get_current_sender()
    _pending_co_tenant[token] = {
        "config": new_config,
        "created_at": time.time(),
        "sender_id": sender.sender_id,
    }

    prompt_preview = new_config.get("llm_system_prompt", "(继承 primary)")
    if len(prompt_preview) > 80:
        prompt_preview = prompt_preview[:80] + "..."

    return ToolResult.success(
        f"⚠️ 即将创建新 co-tenant，请向用户确认以下信息：\n"
        f"• 租户 ID: {tenant_id}\n"
        f"• 名称: {name}\n"
        f"• open_kfid: {open_kfid}\n"
        f"• 继承凭证自: {tenant.tenant_id}\n"
        f"• 系统提示词: {prompt_preview}\n"
        f"\n确认 token: {token}\n"
        f"\n⚠️ 重要：你必须先把以上信息展示给用户，等用户明确说「确认」「好的」「创建」等确认词后，"
        f"才能调用 confirm_add_co_tenant(token='{token}') 执行创建。\n"
        f"如果用户没有明确要求创建新 bot，不要调用确认工具。"
    )


def _confirm_add_co_tenant(args: dict) -> ToolResult:
    """确认并执行 add_co_tenant（第二步）。需要 add_co_tenant 返回的确认 token。"""
    check = _require_super_admin()
    if check:
        return check

    from app.tenant.context import get_current_sender
    from app.tenant.registry import tenant_registry

    _cleanup_expired_pending()

    token = args.get("token", "").strip()
    if not token:
        return ToolResult.invalid_param("需要 add_co_tenant 返回的确认 token")

    pending = _pending_co_tenant.pop(token, None)
    if not pending:
        return ToolResult.error(
            "确认 token 无效或已过期（5 分钟有效期）。请重新调用 add_co_tenant 获取新 token。"
        )

    # 验证是同一个 sender
    sender = get_current_sender()
    if pending["sender_id"] != sender.sender_id:
        _pending_co_tenant[token] = pending  # put back
        return ToolResult.error("确认 token 与当前用户不匹配")

    new_config = pending["config"]
    tenant_id = new_config["tenant_id"]

    # 再次检查是否已存在（可能在等待确认期间被创建）
    existing = tenant_registry.get(tenant_id)
    if existing:
        return ToolResult.error(f"租户 {tenant_id} 已存在（可能在等待确认期间被创建）")

    # 发布到 Redis（tenant_cfg 持久化 + 队列通知 hot-load）
    from app.services.tenant_sync import publish_tenant_update
    ok = publish_tenant_update("add", new_config)
    if not ok:
        return ToolResult.error("发布到 Redis 失败，请稍后重试")

    # 同时注册到本地 registry（本容器立即可用，不用等 5 秒轮询）
    try:
        tenant_registry.register_from_dict(new_config)
    except Exception as e:
        logger.warning("confirm_add_co_tenant: local register failed: %s", e)

    logger.info("confirm_add_co_tenant: created %s by %s", tenant_id, sender.sender_id)

    return ToolResult.success(
        f"Co-tenant {tenant_id} 已添加成功！\n"
        f"• 名称: {new_config['name']}\n"
        f"• open_kfid: {new_config['wecom_kf_open_kfid']}\n"
        f"• 本容器已立即加载，其他容器 5 秒内同步\n"
        f"• 客户现在可以发消息了"
    )


def _remove_co_tenant(args: dict) -> ToolResult:
    """移除 co-tenant（超管专用）"""
    check = _require_super_admin()
    if check:
        return check

    from app.tenant.context import get_current_tenant
    from app.tenant.registry import tenant_registry
    from app.services.tenant_sync import publish_tenant_update

    tenant = get_current_tenant()
    co_tid = args.get("tenant_id", "").strip()
    if not co_tid:
        return ToolResult.invalid_param("需要 tenant_id")
    if co_tid == tenant.tenant_id:
        return ToolResult.error("不能移除自己（primary tenant）")

    # 从 Redis 移除 tenant_cfg
    publish_tenant_update("remove", {"tenant_id": co_tid})

    # 从本地 registry 移除
    try:
        tenant_registry.unregister(co_tid)
    except Exception:
        pass

    # 清理 kf_dispatch 路由
    try:
        from app.services import redis_client as redis_mod
        # 查找并删除该 co-tenant 的 kf_dispatch 路由
        existing = tenant_registry.get(co_tid)
        if existing and existing.wecom_kf_open_kfid:
            redis_mod.execute("DEL", f"kf_dispatch:{existing.wecom_kf_open_kfid}")
    except Exception:
        pass

    return ToolResult.success(
        f"Co-tenant {co_tid} 已移除。\n"
        f"• Redis tenant_cfg 已删除\n"
        f"• 本容器已卸载，其他容器 5 秒内同步"
    )


def _list_co_tenants(args: dict) -> ToolResult:
    """列出当前实例的所有 co-tenant（超管专用）"""
    check = _require_super_admin()
    if check:
        return check

    from app.tenant.context import get_current_tenant
    from app.tenant.registry import tenant_registry

    tenant = get_current_tenant()
    results = []

    for tid, t in tenant_registry.all_tenants().items():
        if t.platform != "wecom_kf":
            continue
        if t.wecom_corpid != tenant.wecom_corpid:
            continue
        if t.wecom_kf_secret != tenant.wecom_kf_secret:
            continue
        is_primary = (tid == tenant.tenant_id)
        results.append({
            "tenant_id": tid,
            "name": t.name,
            "open_kfid": t.wecom_kf_open_kfid,
            "role": "primary" if is_primary else "co-tenant",
        })

    if not results:
        return ToolResult.success("当前容器没有加载任何 wecom_kf 租户。")

    return ToolResult.success(json.dumps(results, indent=2, ensure_ascii=False))


# ── Tool Definitions ─────────────────────────────────────────────

TOOL_DEFINITIONS = [
    # ── 审批流（核心）──
    {
        "name": "request_provision",
        "description": (
            "为客户提交 bot 开通申请（任何人可用）。"
            "创建待审批请求，管理员审批通过后自动开通实例。"
            "客户提供凭证后用这个工具提交，不要直接用 provision_tenant。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tenant_id": {
                    "type": "string",
                    "description": "租户唯一标识（字母数字和连字符），如 'acme-support'",
                },
                "name": {
                    "type": "string",
                    "description": "租户显示名称，如 '某某公司 AI 助手'",
                },
                "platform": {
                    "type": "string",
                    "enum": ["feishu", "wecom", "wecom_kf", "qq"],
                    "description": "接入平台: feishu=飞书, wecom=企微, wecom_kf=微信客服, qq=QQ机器人",
                },
                "credentials_json": {
                    "type": "string",
                    "description": "平台凭证 JSON 字符串（同 provision_tenant 格式）",
                },
                "llm_system_prompt": {
                    "type": "string",
                    "description": "自定义系统提示词（可选）",
                },
                "custom_persona": {
                    "type": "boolean",
                    "description": "是否完全自定义人设（可选）",
                },
                "notes": {
                    "type": "string",
                    "description": "备注信息（客户需求描述等）",
                },
            },
            "required": ["tenant_id", "name", "platform", "credentials_json"],
        },
    },
    {
        "name": "list_provision_requests",
        "description": (
            "列出待审批的开通请求（仅管理员可用）。"
            "默认只显示 pending 状态，传 status='all' 显示全部。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "all"],
                    "description": "筛选状态（默认 pending）",
                },
            },
        },
    },
    {
        "name": "approve_provision_request",
        "description": (
            "审批通过客户的开通请求（仅管理员可用）。"
            "通过后自动创建实例、绑定客户。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "请求 ID（req_ 开头）",
                },
            },
            "required": ["request_id"],
        },
    },
    {
        "name": "reject_provision_request",
        "description": "拒绝客户的开通请求（仅管理员可用）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "请求 ID",
                },
                "reason": {
                    "type": "string",
                    "description": "拒绝原因（可选，会通知客户）",
                },
            },
            "required": ["request_id"],
        },
    },
    # ── 客户管理 ──
    {
        "name": "bind_customer",
        "description": (
            "将客户与 bot 实例绑定（仅管理员可用）。"
            "开通审批自动绑定，一般不需要手动调用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "external_userid": {
                    "type": "string",
                    "description": "客户的 external_userid",
                },
                "tenant_id": {
                    "type": "string",
                    "description": "实例 tenant_id",
                },
                "name": {"type": "string", "description": "客户名称"},
                "platform": {"type": "string", "description": "平台"},
                "port": {"type": "integer", "description": "端口"},
                "notes": {"type": "string", "description": "备注"},
            },
            "required": ["external_userid", "tenant_id"],
        },
    },
    {
        "name": "lookup_customer",
        "description": "查找客户绑定信息。可用 external_userid 或 tenant_id 查找。",
        "input_schema": {
            "type": "object",
            "properties": {
                "external_userid": {"type": "string", "description": "客户 external_userid"},
                "tenant_id": {"type": "string", "description": "实例 tenant_id"},
            },
        },
    },
    {
        "name": "list_customers",
        "description": "列出所有已绑定客户（仅管理员可用）。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "customer_instance_status",
        "description": (
            "查看客户实例状态+用量。"
            "可用 external_userid 自动查找或直接指定 tenant_id。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "external_userid": {"type": "string", "description": "客户 external_userid"},
                "tenant_id": {"type": "string", "description": "实例 tenant_id"},
            },
        },
    },
    {
        "name": "update_customer_notes",
        "description": "更新客户备注（仅管理员可用）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "external_userid": {"type": "string", "description": "客户 external_userid"},
                "name": {"type": "string", "description": "新客户名"},
                "notes": {"type": "string", "description": "备注"},
            },
            "required": ["external_userid"],
        },
    },
    # ── 白名单管理 ──
    {
        "name": "manage_allowed_users",
        "description": (
            "管理 bot 白名单（仅管理员可用）。"
            "控制哪些微信用户可以使用指定 bot。白名单为空则不限制。"
            "通过昵称或 external_userid 添加/移除用户。"
            "例：「把张三加到 xxx bot 的白名单」→ action=add, nicknames=['张三'], tenant_id='xxx'"
            "例：直接用 ID 添加 → action=add, external_userids=['wmXXXX'], tenant_id='xxx'"
            "例：「看看 xxx bot 谁能用」→ action=list, tenant_id='xxx'"
            "例：「把李四从 xxx bot 移除」→ action=remove, nicknames=['李四'], tenant_id='xxx'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "remove", "list"],
                    "description": "操作类型：add=添加用户, remove=移除用户, list=查看白名单",
                },
                "tenant_id": {
                    "type": "string",
                    "description": "目标 bot 的 tenant_id（不填则为当前 bot）",
                },
                "nicknames": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要添加/移除的用户昵称列表（通过昵称匹配 external_userid）",
                },
                "external_userids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要添加的用户 external_userid 列表（直接用 ID，无需昵称匹配。适用于用户没跟 bot 聊过天的情况）",
                },
            },
            "required": ["action"],
        },
    },
    # ── Co-tenant 管理（与 dashboard 对齐）──
    {
        "name": "add_co_tenant",
        "description": (
            "添加 co-tenant 到当前实例（仅管理员可用，wecom_kf 专属）。"
            "⚠️ 此工具只生成预览，不会立即创建！必须用户明确确认后再调用 confirm_add_co_tenant 执行。"
            "自动从当前租户继承全部凭证和配置，只需提供 tenant_id、name、open_kfid。"
            "仅当用户明确要求添加新客服账号/bot 时使用。不要自作主张创建。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tenant_id": {
                    "type": "string",
                    "description": "新租户唯一标识，如 'kf-nar'",
                },
                "name": {
                    "type": "string",
                    "description": "新租户显示名称，如 '纳尔 AI'",
                },
                "wecom_kf_open_kfid": {
                    "type": "string",
                    "description": "企微客服账号 open_kfid（wk 开头），从企微后台获取",
                },
                "llm_system_prompt": {
                    "type": "string",
                    "description": "自定义系统提示词（可选，不填则继承 primary）",
                },
                "custom_persona": {
                    "type": "boolean",
                    "description": "是否完全自定义人设（可选）",
                },
                "tools_enabled": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "启用的工具列表（可选，空数组=全启用，不填=继承 primary）",
                },
            },
            "required": ["tenant_id", "name", "wecom_kf_open_kfid"],
        },
    },
    {
        "name": "confirm_add_co_tenant",
        "description": (
            "确认并执行 add_co_tenant 创建的 co-tenant（仅管理员可用）。"
            "需要 add_co_tenant 返回的确认 token。"
            "⚠️ 必须在用户明确确认后才调用此工具。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "add_co_tenant 返回的确认 token",
                },
            },
            "required": ["token"],
        },
    },
    {
        "name": "remove_co_tenant",
        "description": (
            "移除 co-tenant（仅管理员可用）。"
            "从当前实例中移除一个 co-hosted 的租户。不可移除 primary tenant 自身。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tenant_id": {
                    "type": "string",
                    "description": "要移除的 co-tenant ID",
                },
            },
            "required": ["tenant_id"],
        },
    },
    {
        "name": "list_co_tenants",
        "description": (
            "列出当前实例的所有 co-tenant（仅管理员可用）。"
            "显示同 corp + 同 secret 下的所有租户，标注 primary / co-tenant 角色。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]

TOOL_MAP = {
    "request_provision": _request_provision,
    "list_provision_requests": _list_provision_requests,
    "approve_provision_request": _approve_provision_request,
    "reject_provision_request": _reject_provision_request,
    "bind_customer": _bind_customer,
    "lookup_customer": _lookup_customer,
    "list_customers": _list_customers,
    "customer_instance_status": _customer_instance_status,
    "update_customer_notes": _update_customer_notes,
    "manage_allowed_users": _manage_allowed_users,
    "add_co_tenant": _add_co_tenant,
    "confirm_add_co_tenant": _confirm_add_co_tenant,
    "remove_co_tenant": _remove_co_tenant,
    "list_co_tenants": _list_co_tenants,
}
