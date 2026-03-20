"""租户实例管理工具 — Phase 2 Control Plane

允许管理员 bot（如销售/售后 bot）为客户创建、管理独立 bot 实例。
每个实例运行在独立 Docker 容器中，端口隔离、配置独立。

权限：需要 instance_management_enabled=True（在 tenant config 中设置）
"""

from __future__ import annotations

import json
import logging
import shutil

from app.tools.tool_result import ToolResult

_DOCKER_AVAILABLE: bool | None = None


def _check_docker() -> ToolResult | None:
    """检查 Docker CLI 是否可用。不可用时返回错误 ToolResult。"""
    global _DOCKER_AVAILABLE
    if _DOCKER_AVAILABLE is None:
        _DOCKER_AVAILABLE = shutil.which("docker") is not None
    if not _DOCKER_AVAILABLE:
        return ToolResult.error(
            "Docker CLI 不可用（当前环境是容器内，没有 Docker 命令）。"
            "实例管理操作（provision/list/restart/destroy）需要在宿主机上执行，"
            "或者通过 Admin Dashboard 操作。",
            code="env_error",
        )
    return None

logger = logging.getLogger(__name__)


# ── Handlers ──────────────────────────────────────────────────────


def _provision_tenant(args: dict) -> ToolResult:
    """创建新的租户 bot 实例"""
    docker_err = _check_docker()
    if docker_err:
        return docker_err
    from app.services.provisioner import provision

    tenant_id = args.get("tenant_id", "").strip()
    name = args.get("name", "").strip()
    platform = args.get("platform", "").strip()
    credentials_json = args.get("credentials_json", "{}").strip()

    if not tenant_id:
        return ToolResult.invalid_param(
            "Missing tenant_id",
            retry_hint="tenant_id 格式: 公司名-用途，如 'acme-support'，只能用字母数字和-_",
        )
    if not name:
        return ToolResult.invalid_param(
            "Missing name",
            retry_hint="name 是显示名称，如 '某某公司 AI 助手'",
        )
    if not platform:
        return ToolResult.invalid_param(
            "Missing platform",
            retry_hint="platform 必须是 feishu/wecom/wecom_kf/qq 之一",
        )

    try:
        credentials = json.loads(credentials_json)
    except json.JSONDecodeError as e:
        return ToolResult.invalid_param(
            f"Invalid credentials_json: {e}",
            retry_hint="credentials_json 必须是合法 JSON 字符串，注意双引号和转义",
        )

    # 可选参数
    kwargs: dict = {}
    if args.get("llm_system_prompt"):
        kwargs["llm_system_prompt"] = args["llm_system_prompt"]
    if args.get("custom_persona") is not None:
        kwargs["custom_persona"] = bool(args["custom_persona"])
    if args.get("capability_modules"):
        kwargs["capability_modules"] = args["capability_modules"]

    result = provision(
        tenant_id=tenant_id,
        name=name,
        platform=platform,
        credentials=credentials,
        **kwargs,
    )

    if result.get("ok"):
        return ToolResult.success(json.dumps(result, indent=2, ensure_ascii=False))
    return ToolResult.error(result.get("error", "Unknown error"), code="api_error")


def _list_instances(args: dict) -> ToolResult:
    """列出所有已部署的租户实例"""
    docker_err = _check_docker()
    if docker_err:
        return docker_err
    from app.services.provisioner import list_instances

    instances = list_instances()
    if not instances:
        return ToolResult.success("当前没有已部署的独立实例。")
    return ToolResult.success(json.dumps(instances, indent=2, ensure_ascii=False))


def _instance_status(args: dict) -> ToolResult:
    """查看特定实例的详细状态"""
    from app.services.provisioner import instance_status

    tenant_id = args.get("tenant_id", "").strip()
    if not tenant_id:
        return ToolResult.invalid_param("Missing tenant_id")

    result = instance_status(tenant_id)
    if result.get("ok"):
        return ToolResult.success(json.dumps(result, indent=2, ensure_ascii=False))
    return ToolResult.error(result.get("error", "Unknown error"))


def _restart_instance(args: dict) -> ToolResult:
    """重启租户实例"""
    docker_err = _check_docker()
    if docker_err:
        return docker_err
    from app.services.provisioner import restart_instance

    tenant_id = args.get("tenant_id", "").strip()
    if not tenant_id:
        return ToolResult.invalid_param("Missing tenant_id")

    result = restart_instance(tenant_id)
    if result.get("ok"):
        return ToolResult.success(result.get("message", "OK"))
    return ToolResult.error(result.get("error", "Unknown error"))


def _destroy_instance(args: dict) -> ToolResult:
    """销毁租户实例（停止容器 + 删除配置）"""
    docker_err = _check_docker()
    if docker_err:
        return docker_err
    from app.services.provisioner import destroy_instance

    tenant_id = args.get("tenant_id", "").strip()
    if not tenant_id:
        return ToolResult.invalid_param("Missing tenant_id")

    result = destroy_instance(tenant_id)
    if result.get("ok"):
        return ToolResult.success(result.get("message", "OK"))
    return ToolResult.error(result.get("error", "Unknown error"))


async def _list_kf_accounts(args: dict) -> ToolResult:
    """列出当前 corp 下所有企微客服账号（调 API，非查本地配置）"""
    from app.services.wecom_kf import wecom_kf_client

    try:
        accounts = await wecom_kf_client.list_accounts()
    except Exception as e:
        return ToolResult.error(f"调用企微客服 API 失败: {e}", code="api_error")

    if not accounts:
        return ToolResult.success("当前 corp 下没有客服账号。")
    return ToolResult.success(json.dumps(accounts, indent=2, ensure_ascii=False))


async def _add_kf_account(args: dict) -> ToolResult:
    """创建新的企微客服账号"""
    from app.services.wecom_kf import wecom_kf_client

    name = args.get("name", "").strip()
    if not name:
        return ToolResult.invalid_param("Missing name")

    media_id = args.get("media_id", "").strip()
    if not media_id:
        return ToolResult.invalid_param(
            "Missing required parameter: media_id. "
            "企微 API 强制要求创建账号时必须提供头像 media_id。"
            "请先通过企微素材上传接口获取 media_id，或让用户在企微后台手动创建账号。"
        )
    try:
        result = await wecom_kf_client.add_account(name, media_id)
    except Exception as e:
        return ToolResult.error(f"创建客服账号失败: {e}", code="api_error")

    if result.get("errcode", -1) != 0:
        return ToolResult.error(
            f"企微 API 错误: {result.get('errmsg', 'unknown')} (code={result.get('errcode')})",
            code="api_error",
        )
    return ToolResult.success(json.dumps(result, indent=2, ensure_ascii=False))


async def _delete_kf_account(args: dict) -> ToolResult:
    """删除企微客服账号"""
    from app.services.wecom_kf import wecom_kf_client

    open_kfid = args.get("open_kfid", "").strip()
    if not open_kfid:
        return ToolResult.invalid_param("Missing open_kfid")

    try:
        result = await wecom_kf_client.delete_account(open_kfid)
    except Exception as e:
        return ToolResult.error(f"删除客服账号失败: {e}", code="api_error")

    if result.get("errcode", -1) != 0:
        return ToolResult.error(
            f"企微 API 错误: {result.get('errmsg', 'unknown')} (code={result.get('errcode')})",
            code="api_error",
        )
    return ToolResult.success("客服账号已删除。")


async def _update_kf_account(args: dict) -> ToolResult:
    """修改企微客服账号名称/头像"""
    from app.services.wecom_kf import wecom_kf_client

    open_kfid = args.get("open_kfid", "").strip()
    if not open_kfid:
        return ToolResult.invalid_param("Missing open_kfid")

    name = args.get("name", "").strip()
    media_id = args.get("media_id", "").strip()
    if not name and not media_id:
        return ToolResult.invalid_param("至少提供 name 或 media_id 其中一个")

    try:
        result = await wecom_kf_client.update_account(open_kfid, name, media_id)
    except Exception as e:
        return ToolResult.error(f"修改客服账号失败: {e}", code="api_error")

    if result.get("errcode", -1) != 0:
        return ToolResult.error(
            f"企微 API 错误: {result.get('errmsg', 'unknown')} (code={result.get('errcode')})",
            code="api_error",
        )
    return ToolResult.success("客服账号已更新。")


async def _get_kf_account_link(args: dict) -> ToolResult:
    """获取客服账号的接入链接（可嵌入网页/生成二维码）"""
    from app.services.wecom_kf import wecom_kf_client

    open_kfid = args.get("open_kfid", "").strip()
    if not open_kfid:
        return ToolResult.invalid_param("Missing open_kfid")

    scene = args.get("scene", "").strip()
    try:
        result = await wecom_kf_client.get_account_link(open_kfid, scene)
    except Exception as e:
        return ToolResult.error(f"获取客服链接失败: {e}", code="api_error")

    if result.get("errcode", -1) != 0:
        return ToolResult.error(
            f"企微 API 错误: {result.get('errmsg', 'unknown')} (code={result.get('errcode')})",
            code="api_error",
        )
    return ToolResult.success(json.dumps(result, indent=2, ensure_ascii=False))


# ── Tool Definitions ─────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "provision_tenant",
        "description": (
            "为客户创建一个新的 AI bot 实例。"
            "系统会自动判断部署方式：\n"
            "- 如果凭证（corpid + kf_secret）和某个已有实例相同，"
            "说明用的是同一个企微自建应用，会自动 co-host 到该容器"
            "（共享容器，按 open_kfid 分发到不同人设）。\n"
            "- 如果凭证不同或是新平台，则创建独立 Docker 容器。\n"
            "co-host 场景下客户不需要提供新凭证（复用宿主实例的），"
            "只需提供 tenant_id、name、open_kfid 和 system_prompt。"
            "创建完成后会返回 webhook URL，客户需要在飞书/企微后台配置这个 URL。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tenant_id": {
                    "type": "string",
                    "description": (
                        "租户唯一标识，只能用字母数字和连字符/下划线，"
                        "建议格式: 公司名-用途，如 'gaomeng-codebot', 'acme-support'"
                    ),
                },
                "name": {
                    "type": "string",
                    "description": "租户显示名称，如 '高梦科技 AI 助手'",
                },
                "platform": {
                    "type": "string",
                    "enum": ["feishu", "wecom", "wecom_kf", "qq"],
                    "description": (
                        "接入平台: feishu=飞书, wecom=企业微信内部应用, "
                        "wecom_kf=微信客服（面向外部微信用户）, qq=QQ机器人"
                    ),
                },
                "credentials_json": {
                    "type": "string",
                    "description": (
                        "平台凭证 JSON 字符串。不同平台需要不同字段:\n"
                        "飞书: {\"app_id\": \"...\", \"app_secret\": \"...\", "
                        "\"verification_token\": \"...\", \"encrypt_key\": \"...\"}\n"
                        "企微: {\"wecom_corpid\": \"...\", \"wecom_corpsecret\": \"...\", "
                        "\"wecom_agent_id\": 123, \"wecom_token\": \"...\", "
                        "\"wecom_encoding_aes_key\": \"...\"}\n"
                        "微信客服: {\"wecom_corpid\": \"...\", \"wecom_kf_secret\": \"...\", "
                        "\"wecom_kf_token\": \"...\", \"wecom_kf_encoding_aes_key\": \"...\", "
                        "\"wecom_kf_open_kfid\": \"wk...\"}"
                    ),
                },
                "llm_system_prompt": {
                    "type": "string",
                    "description": "自定义系统提示词（可选，定义 bot 人设和行为风格）",
                },
                "custom_persona": {
                    "type": "boolean",
                    "description": "是否完全自定义人设（跳过通用行为准则注入）",
                },
                "capability_modules": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "预装的能力模块列表（可选）。"
                        "模块名对应 app/knowledge/modules/{name}.md，"
                        "会在 system prompt 中注入领域专业知识。"
                        "例: [\"anti_drone_safety\"] 让 bot 具备反无人机业务知识。"
                        "例: [\"social_media_research\"] 让 bot 具备社媒调研能力。"
                        "创建 bot 前务必先调用 list_capability_modules 查看可用模块，"
                        "主动向客户推荐适合其行业的模块组合。"
                    ),
                },
            },
            "required": ["tenant_id", "name", "platform", "credentials_json"],
        },
    },
    {
        "name": "list_instances",
        "description": "列出所有已部署的租户 bot 实例及其运行状态（端口、容器状态等）",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_instance_status",
        "description": "查看某个租户实例的详细状态，包括容器状态、端口、启动时间、最近日志",
        "input_schema": {
            "type": "object",
            "properties": {
                "tenant_id": {
                    "type": "string",
                    "description": "要查看的租户 ID",
                },
            },
            "required": ["tenant_id"],
        },
    },
    {
        "name": "restart_instance",
        "description": "重启某个租户的 bot 实例（配置更新后需要重启生效）",
        "input_schema": {
            "type": "object",
            "properties": {
                "tenant_id": {
                    "type": "string",
                    "description": "要重启的租户 ID",
                },
            },
            "required": ["tenant_id"],
        },
    },
    {
        "name": "destroy_instance",
        "description": (
            "销毁某个租户的 bot 实例（停止容器、删除配置和数据）。"
            "此操作不可逆！请在执行前确认客户确实要停用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tenant_id": {
                    "type": "string",
                    "description": "要销毁的租户 ID",
                },
            },
            "required": ["tenant_id"],
        },
    },
    # ── 企微客服账号管理工具 ──
    {
        "name": "list_kf_accounts",
        "description": (
            "列出当前企业微信 corp 下所有客服账号（调用企微 API，不是查本地配置）。"
            "返回每个客服账号的 open_kfid、名称、头像等信息。"
            "用于查看已有客服账号或获取新账号的 open_kfid。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "add_kf_account",
        "description": (
            "在企业微信中创建一个新的客服账号。"
            "创建成功后返回 open_kfid，后续可用于 provision_tenant 部署 bot 实例，"
            "或用 get_kf_account_link 获取客服接入链接。"
            "注意：需要先在企微管理后台「微信客服」中启用「通过 API 管理」。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "客服名称（不超过 16 字符），如 '调研助手'、'售后客服'",
                },
                "media_id": {
                    "type": "string",
                    "description": "客服头像的 media_id（必填，通过企微素材上传接口获取）",
                },
            },
            "required": ["name", "media_id"],
        },
    },
    {
        "name": "delete_kf_account",
        "description": (
            "删除企微客服账号。此操作不可逆！"
            "删除前请确认该账号不再使用，且已停止对应的 bot 实例。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "open_kfid": {
                    "type": "string",
                    "description": "要删除的客服账号 ID（wk 开头）",
                },
            },
            "required": ["open_kfid"],
        },
    },
    {
        "name": "update_kf_account",
        "description": "修改企微客服账号的名称或头像。",
        "input_schema": {
            "type": "object",
            "properties": {
                "open_kfid": {
                    "type": "string",
                    "description": "要修改的客服账号 ID（wk 开头）",
                },
                "name": {
                    "type": "string",
                    "description": "新的客服名称（可选）",
                },
                "media_id": {
                    "type": "string",
                    "description": "新的头像 media_id（可选）",
                },
            },
            "required": ["open_kfid"],
        },
    },
    {
        "name": "get_kf_account_link",
        "description": (
            "获取客服账号的接入链接。用户点击此链接可直接向该客服发起咨询。"
            "链接可嵌入网页或生成二维码。"
            "可附带 scene 参数区分不同来源渠道。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "open_kfid": {
                    "type": "string",
                    "description": "客服账号 ID（wk 开头）",
                },
                "scene": {
                    "type": "string",
                    "description": (
                        "场景值（可选），用于区分用户来源渠道。"
                        "字母数字下划线连字符，不超过 32 字节。"
                        "如 'website'、'wechat_article'、'qrcode_offline'"
                    ),
                },
            },
            "required": ["open_kfid"],
        },
    },
]

TOOL_MAP = {
    "provision_tenant": _provision_tenant,
    "list_instances": _list_instances,
    "get_instance_status": _instance_status,
    "restart_instance": _restart_instance,
    "destroy_instance": _destroy_instance,
    "list_kf_accounts": _list_kf_accounts,
    "add_kf_account": _add_kf_account,
    "delete_kf_account": _delete_kf_account,
    "update_kf_account": _update_kf_account,
    "get_kf_account_link": _get_kf_account_link,
}
