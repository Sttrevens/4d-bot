"""Short-lived session facts and continuation helpers."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from app.tenant.context import get_current_tenant

logger = logging.getLogger(__name__)

_TTL_SECONDS = 6 * 3600
_MEM_CACHE: dict[str, tuple[float, dict]] = {}

_DEICTIC_VISUAL_RE = re.compile(
    r"(刚才(那顿|那张图|那张图片|那份|那盘|那些)|"
    r"上(一顿|张图|张图片)|"
    r"这张图|这张图片|这些|这个|那顿|那张图|刚刚发的图|刚才发的图|"
    r"我吃了这些|那一顿)",
    re.IGNORECASE,
)

_MEAL_RE = re.compile(
    r"(吃|饭|餐|热量|多少卡|卡路里|蛋白|碳水|脂肪|减脂|食堂|鸡蛋|烧鸟|刺身|"
    r"鸡胸|米饭|便当|外卖|早餐|午饭|晚饭)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ContinuationContext:
    note: str = ""
    reused_images: tuple[str, ...] = ()


def _key(sender_id: str) -> str:
    tenant = get_current_tenant()
    return f"session:facts:{tenant.tenant_id}:{sender_id}"


def _now() -> float:
    return time.time()


def _get_redis_client():
    try:
        from app.services import redis_client
        return redis_client
    except Exception:
        return None


def _load_payload(sender_id: str) -> dict | None:
    key = _key(sender_id)
    cached = _MEM_CACHE.get(key)
    now = _now()
    if cached and cached[0] > now:
        return cached[1]

    data = None
    redis = _get_redis_client()
    if redis and redis.available():
        raw = redis.execute("GET", key)
        if isinstance(raw, str) and raw:
            try:
                data = json.loads(raw)
            except Exception:
                logger.debug("session_facts: failed to decode %s", key, exc_info=True)
    if not isinstance(data, dict):
        return None

    expires_at = float(data.get("expires_at", 0) or 0)
    if expires_at and expires_at > now:
        _MEM_CACHE[key] = (expires_at, data)
        return data
    return None


def _store_payload(sender_id: str, payload: dict) -> None:
    key = _key(sender_id)
    expires_at = _now() + _TTL_SECONDS
    payload = dict(payload)
    payload["expires_at"] = expires_at
    _MEM_CACHE[key] = (expires_at, payload)
    redis = _get_redis_client()
    if redis and redis.available():
        try:
            redis.execute("SET", key, json.dumps(payload, ensure_ascii=False), "EX", str(_TTL_SECONDS))
        except Exception:
            logger.debug("session_facts: failed to persist %s", key, exc_info=True)


def infer_turn_objective(user_text: str, image_urls: list[str] | None = None) -> str:
    text = user_text or ""
    if image_urls and _MEAL_RE.search(text):
        return "meal_analysis"
    if image_urls:
        return "visual_analysis"
    if _MEAL_RE.search(text):
        return "meal_followup"
    return ""


def remember_visual_turn(
    *,
    sender_id: str,
    user_text: str,
    image_urls: list[str] | None,
    assistant_reply: str,
) -> None:
    if not image_urls:
        return
    payload = {
        "kind": "visual_turn",
        "objective": infer_turn_objective(user_text, image_urls),
        "user_text": (user_text or "")[:500],
        "assistant_reply": (assistant_reply or "")[:1200],
        "image_urls": [u for u in image_urls if isinstance(u, str) and u][:3],
        "ts": int(_now()),
    }
    _store_payload(sender_id, payload)


def should_reuse_recent_visual(user_text: str, image_urls: list[str] | None = None) -> bool:
    if image_urls:
        return False
    text = user_text or ""
    return bool(_DEICTIC_VISUAL_RE.search(text) or ("刚才" in text and _MEAL_RE.search(text)))


def build_continuation_context(
    *,
    sender_id: str,
    user_text: str,
    image_urls: list[str] | None = None,
) -> ContinuationContext:
    if not should_reuse_recent_visual(user_text, image_urls):
        return ContinuationContext()
    payload = _load_payload(sender_id)
    if not payload or payload.get("kind") != "visual_turn":
        return ContinuationContext()

    reused_images = tuple(payload.get("image_urls") or ())
    if not reused_images:
        return ContinuationContext()

    objective = payload.get("objective") or "visual_analysis"
    user_summary = payload.get("user_text", "")
    reply_summary = payload.get("assistant_reply", "")
    note = (
        "[短期会话事实]\n"
        f"- 你刚刚在处理同一轮{objective}，用户当时说：{user_summary}\n"
        f"- 你上一轮对这张图/这顿饭的回复是：{reply_summary}\n"
        "- 当前用户明显在追问“刚才那顿/这张图/这些”，请延续同一个对象回答。"
        "优先基于这轮图片继续分析，不要退回泛化估算，也不要漂移去生成无关文件。"
    )
    return ContinuationContext(note=note, reused_images=reused_images)
