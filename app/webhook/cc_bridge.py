"""Claude Code 桥接端点 —— 接收本地 CC 的回复并转发到聊天平台

路由：
  POST /api/cc/send   — CC 通过 channel-mcp-server 的 reply tool 调用
  POST /api/cc/react  — CC 发送表情回复（可选）

认证：
  Bearer token 校验（REMOTE_DEV_BRIDGE_TOKEN 环境变量）
  不配置则不校验（开发阶段）
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cc", tags=["cc-bridge"])

_BRIDGE_TOKEN = os.getenv("REMOTE_DEV_BRIDGE_TOKEN", "")


class SendRequest(BaseModel):
    platform: str = "feishu"
    chat_id: str
    text: str
    reply_to: str | None = None


class ReactRequest(BaseModel):
    platform: str = "feishu"
    chat_id: str
    message_id: str
    emoji: str


def _check_auth(request: Request) -> bool:
    """校验 Bearer token（不配置则放行）"""
    if not _BRIDGE_TOKEN:
        return True
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {_BRIDGE_TOKEN}"


@router.post("/send")
async def cc_send(body: SendRequest, request: Request):
    """接收 CC 的回复，转发到对应平台"""
    if not _check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if not body.chat_id or not body.text:
        return JSONResponse({"error": "chat_id and text required"}, status_code=400)

    logger.info("[cc-bridge] send: platform=%s chat=%s text=%s",
                body.platform, body.chat_id[:15], body.text[:80])

    try:
        if body.platform == "feishu":
            from app.services.feishu import feishu_client
            if body.reply_to:
                await feishu_client.reply_text(body.reply_to, body.text)
            else:
                await feishu_client.send_to_chat(body.chat_id, body.text)
        else:
            return JSONResponse(
                {"error": f"unsupported platform: {body.platform}"},
                status_code=400,
            )

        return JSONResponse({"ok": True})

    except Exception as e:
        logger.exception("[cc-bridge] send failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/react")
async def cc_react(body: ReactRequest, request: Request):
    """接收 CC 的表情回复"""
    if not _check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    logger.info("[cc-bridge] react: %s on %s", body.emoji, body.message_id[:15])

    try:
        if body.platform == "feishu":
            from app.services.feishu import feishu_client
            # 飞书添加 reaction 需要 message_id
            # FeishuClient 目前没有 add_reaction 方法，先记日志
            logger.info("[cc-bridge] reaction %s (not yet implemented)", body.emoji)
            return JSONResponse({"ok": True, "note": "reaction logged, not yet sent"})
        else:
            return JSONResponse({"error": f"unsupported platform"}, status_code=400)

    except Exception as e:
        logger.exception("[cc-bridge] react failed")
        return JSONResponse({"error": str(e)}, status_code=500)
