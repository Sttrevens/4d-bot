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

_TOPIC_FOLLOWUP_RE = re.compile(
    r"(按刚才那个|按刚才那个来|接着刚才|继续刚才|刚才那个|刚刚那个|上一个|上一条"
    r"|来个速通|速通版|简版|精简版|一小时的|一小时版|压缩版|短版|浓缩版"
    r"|就按这个|按这个来|那这个呢|那个呢|那这个怎么办|那这个|那个怎么办)",
    re.IGNORECASE,
)

_TOPIC_NOISE_RE = re.compile(
    r"^(宝宝|在吗|你好|哈喽|hello|hi|嗯|哦|好的|ok|收到|妈妈|宝贝|亲爱的)$",
    re.IGNORECASE,
)

_CONSTRAINT_HINT_RE = re.compile(
    r"(coser|cos|博主|主播|影评|电影|旅行|美食|游戏|穿搭|摄影|舞蹈|汉服|二次元|"
    r"老师|学生|女生|男生|女的|男的|杭州|上海|北京|广州|深圳|模特|配音|画师|"
    r"不是那个|不是做|不是拍|不是电影号|不是影评|是个|应该是|好像是|做|搞|偏|主打)",
    re.IGNORECASE,
)

_CONSTRAINT_FOLLOWUP_RE = re.compile(
    r"(继续|接着|搜|查|找|进展|结果|怎么样了|搜的怎么样了|继续搜|继续查|继续找)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ContinuationContext:
    note: str = ""
    reused_images: tuple[str, ...] = ()


def _key(sender_id: str) -> str:
    tenant = get_current_tenant()
    return f"session:facts:{tenant.tenant_id}:{sender_id}"


def _topic_key(sender_id: str) -> str:
    tenant = get_current_tenant()
    return f"session:topic:{tenant.tenant_id}:{sender_id}"


def _constraint_key(sender_id: str) -> str:
    tenant = get_current_tenant()
    return f"session:constraints:{tenant.tenant_id}:{sender_id}"


def _now() -> float:
    return time.time()


def _get_redis_client():
    try:
        from app.services import redis_client
        return redis_client
    except Exception:
        return None


def _load_payload(sender_id: str, *, key_builder=_key) -> dict | None:
    key = key_builder(sender_id)
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


def _store_payload(sender_id: str, payload: dict, *, key_builder=_key) -> None:
    key = key_builder(sender_id)
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


def _normalize_topic_text(text: str) -> str:
    compact = re.sub(r"\s+", " ", (text or "")).strip(" ，。！？?!.")
    return compact[:120]


def _normalize_constraint_text(text: str) -> str:
    compact = re.sub(r"\s+", " ", (text or "")).strip(" ，。！？?!.")
    return compact[:40]


def should_remember_recent_topic(user_text: str, assistant_reply: str, image_urls: list[str] | None = None) -> bool:
    text = _normalize_topic_text(user_text)
    if image_urls:
        return False
    if not text or len(text) < 6:
        return False
    if _TOPIC_NOISE_RE.search(text):
        return False
    if _TOPIC_FOLLOWUP_RE.search(text):
        return False
    return bool(assistant_reply and assistant_reply.strip())


def remember_recent_topic(
    *,
    sender_id: str,
    user_text: str,
    assistant_reply: str,
    image_urls: list[str] | None = None,
) -> None:
    if not should_remember_recent_topic(user_text, assistant_reply, image_urls):
        return
    payload = {
        "kind": "recent_topic",
        "topic_text": _normalize_topic_text(user_text),
        "assistant_reply": (assistant_reply or "")[:1200],
        "ts": int(_now()),
    }
    _store_payload(sender_id, payload, key_builder=_topic_key)


def should_remember_active_constraints(user_text: str, image_urls: list[str] | None = None) -> bool:
    if image_urls:
        return False
    text = _normalize_constraint_text(user_text)
    if not text or len(text) < 2 or len(text) > 40:
        return False
    if _TOPIC_NOISE_RE.search(text):
        return False
    if "？" in text or "?" in text:
        return False
    return bool(_CONSTRAINT_HINT_RE.search(text))


def remember_active_constraints(
    *,
    sender_id: str,
    user_text: str,
    image_urls: list[str] | None = None,
) -> None:
    if not should_remember_active_constraints(user_text, image_urls):
        return
    text = _normalize_constraint_text(user_text)
    payload = _load_payload(sender_id, key_builder=_constraint_key)
    constraints: list[str] = []
    if payload and payload.get("kind") == "active_constraints":
        constraints = [str(c) for c in payload.get("constraints") or [] if isinstance(c, str)]
    if text in constraints:
        constraints.remove(text)
    constraints.append(text)
    payload = {
        "kind": "active_constraints",
        "constraints": constraints[-6:],
        "ts": int(_now()),
    }
    _store_payload(sender_id, payload, key_builder=_constraint_key)


def should_reuse_recent_visual(user_text: str, image_urls: list[str] | None = None) -> bool:
    if image_urls:
        return False
    text = user_text or ""
    return bool(_DEICTIC_VISUAL_RE.search(text) or ("刚才" in text and _MEAL_RE.search(text)))


def should_reuse_recent_topic(user_text: str, image_urls: list[str] | None = None) -> bool:
    if image_urls:
        return False
    text = _normalize_topic_text(user_text)
    if not text:
        return False
    if _TOPIC_FOLLOWUP_RE.search(text):
        return True
    if len(text) <= 24 and ("那个" in text or "这个" in text or "刚才" in text):
        return True
    return False


def should_reuse_active_constraints(user_text: str, image_urls: list[str] | None = None) -> bool:
    if image_urls:
        return False
    text = _normalize_topic_text(user_text)
    if not text:
        return False
    if _TOPIC_FOLLOWUP_RE.search(text):
        return True
    if _CONSTRAINT_FOLLOWUP_RE.search(text):
        return True
    if len(text) <= 24 and ("那个" in text or "这个" in text or "刚才" in text):
        return True
    return False


def build_continuation_context(
    *,
    sender_id: str,
    user_text: str,
    image_urls: list[str] | None = None,
) -> ContinuationContext:
    payload = None
    if should_reuse_recent_visual(user_text, image_urls):
        payload = _load_payload(sender_id)
        if payload and payload.get("kind") == "visual_turn":
            reused_images = tuple(payload.get("image_urls") or ())
            if reused_images:
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

    note_parts: list[str] = []

    if should_reuse_recent_topic(user_text, image_urls):
        topic_payload = _load_payload(sender_id, key_builder=_topic_key)
        if topic_payload and topic_payload.get("kind") == "recent_topic":
            topic_text = topic_payload.get("topic_text", "")
            reply_summary = topic_payload.get("assistant_reply", "")
            if topic_text:
                note_parts.append(
                    "[最近会话话题]\n"
                    f"- 你们上一段主要在聊：{topic_text}\n"
                    f"- 你上一轮对这个话题的回答是：{reply_summary}\n"
                    "- 当前用户这句明显是在承接上文，请默认继续这个最近话题。"
                    "先给出这个话题的速通版/简版/下一步，不要先跳去旧计划、长期记忆或无关业务。"
                )

    if should_reuse_active_constraints(user_text, image_urls):
        constraint_payload = _load_payload(sender_id, key_builder=_constraint_key)
        if constraint_payload and constraint_payload.get("kind") == "active_constraints":
            constraints = [str(c) for c in constraint_payload.get("constraints") or [] if isinstance(c, str)]
            if constraints:
                joined = "；".join(constraints[-4:])
                note_parts.append(
                    "[当前任务约束]\n"
                    f"- 用户已经补充过这些条件：{joined}\n"
                    "- 当前这句是在继续上一件事时，默认把这些条件一起带上。"
                    "不要重新问用户已经说过的限制条件，搜索、筛选和总结时都要优先使用这些约束。"
                )

    if not note_parts:
        return ContinuationContext()
    return ContinuationContext(note="\n\n".join(note_parts), reused_images=())
