# 06 Memory Bridge

## Purpose

Use Hermes memory lifecycle capabilities without corrupting our production memory model. Our Redis memory remains canonical for tenant-facing user profiles, journals, org recall, and admin dashboard inspection.

## Current Canonical Stores

| Store | Existing File | Role |
|---|---|---|
| Chat history | `app/services/history.py` | Short-term conversation context |
| Journal | `app/services/memory_store.py` | Durable episodic log |
| User profile | `app/services/memory.py` | Durable user model |
| Memory quality | `app/harness/memory_quality.py` | Scope and confidence classification |
| Recall planning | `app/harness/memory_recall.py` | Query and relevance strategy |
| Admin inspection | `app/admin/routes.py` | Memory visibility and rebuild operations |

## Hermes Concepts To Adopt

Hermes exposes a `MemoryProvider` lifecycle:

- `initialize`
- `system_prompt_block`
- `prefetch`
- `queue_prefetch`
- `sync_turn`
- `on_turn_start`
- `on_session_end`
- `on_pre_compress`
- `on_delegation`
- `get_tool_schemas`
- `handle_tool_call`

Implement our bridge as a Hermes-compatible memory provider backed by our Redis stores.

## New Files

- `app/hermes_runtime/memory_bridge.py`
- `app/hermes_runtime/memory_scope.py`
- `tests/test_hermes_memory_bridge.py`

## Canonical Policy

| Operation | Runtime Behavior | Canonical Owner |
|---|---|---|
| Read recent chat | Runtime reads through `chat_history` adapter | Our app |
| Recall journal | Runtime calls `recall` with scoped tenant/user query | Our app |
| Read profile | Runtime receives sanitized profile context | Our app |
| Write diary | Runtime queues through existing `write_diary` path | Our app |
| Update profile | Runtime writes through memory quality classifier | Our app |
| External provider memory | Runtime may add context-only provider after explicit config | Our app approves |
| Subagent memory | Parent observes subagent output and decides persistence | Our app |

Hermes must never write raw profile JSON directly.

## Memory Request Shape

```json
{
  "tenant_id": "kf-steven-ai",
  "session_id": "hr:kf-steven-ai:kf:dm:abc:self",
  "identity_id": "wecom_kf:external_userid",
  "sender_name": "客户",
  "query": "用户正在咨询 bot 开通和 PPT skill",
  "scopes": ["profile", "journal", "org"],
  "limit": 8
}
```

## Memory Response Shape

```json
{
  "profile_context": "用户画像摘要...",
  "journal_entries": [
    {
      "summary": "用户此前关注 repo-style skill 安装",
      "tags": ["skill", "runtime"],
      "source": "journal",
      "confidence": 0.86
    }
  ],
  "org_recall": [],
  "policy_notes": [
    "短期状态不能当成永久事实",
    "低置信候选只能作为待确认线索"
  ]
}
```

## Scope Rules

1. Tenant is always required.
2. User profile reads require `identity_id` or `sender_id`.
3. Group chat recall includes chat-level context plus sender profile.
4. Org recall is disabled unless `tenant.memory_org_recall_enabled` is true.
5. Short-term memory expires according to existing profile TTL rules.
6. Memory returned to Hermes is already sanitized by `sanitize_memory_text`.

## Background Writes

Hermes may produce memory write candidates after a turn. Bridge handling:

1. Convert candidate into existing diary input fields.
2. Run `classify_memory_candidate`.
3. Persist journal entry through `remember`.
4. Apply profile changes only through `_apply_decision_to_profile`.
5. Emit `runtime.memory.write` event with scope and confidence.

If classification returns review scope, store as low-confidence candidate and avoid injecting as fact in later turns.

## External Providers

Hermes supports external memory providers. Production policy:

- Default: disabled.
- Per-tenant allowlist required.
- External provider cannot receive raw platform secrets.
- External provider cannot become canonical profile source until an explicit migration plan is approved.
- External provider data must be visible in admin memory diagnostics before production rollout.

## Tests

- A Hermes memory read for tenant A cannot read tenant B journal keys.
- Short-term state is included only when unexpired.
- Review-scope memory writes become low-confidence candidates.
- Subagent memory writes do not directly update user profile.
- Org recall is omitted when tenant config disables it.
- External provider config is ignored unless tenant explicitly enables it.
- Memory bridge emits trace events with `run_id`, `tenant_id`, and `session_id`.
