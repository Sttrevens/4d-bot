# 03 Tenant Session Mapping

## Purpose

Hermes has session-oriented runtime concepts. Our production app has tenant, channel, sender, identity, chat, and task-run concepts. The bridge must make these mappings explicit so memory, skills, tools, and checkpoints never cross tenant or user boundaries.

## Canonical Identity Layers

| Layer | Existing Source | Runtime Field | Rule |
|---|---|---|---|
| Tenant | `TenantConfig.tenant_id` | `tenant_id` | Required on every runtime request |
| Channel | `ChannelConfig.channel_id` or synthesized primary channel | `channel_id` | Required for multi-channel bots |
| Platform | channel platform or tenant platform | `platform` | Used for prompt and delivery policy |
| Sender | webhook sender id | `sender.sender_id` | Raw platform user id |
| Identity | `app.services.identity` mapping when available | `sender.identity_id` | Stable cross-channel identity |
| Chat | platform chat id | `conversation.chat_id` | Group/thread scope |
| History | `chat_history` key | `conversation.history_key` | Runtime receives pointer, not raw Redis keys |
| Task run | `app.services.task_run` | `run_id` | Correlates events, tools, delivery |

## Session ID Format

Hermes session IDs must be deterministic and scoped:

```text
hr:{tenant_id}:{channel_id}:{scope}:{identity_hash}:{chat_hash}
```

Where:

- `scope` is `dm`, `group`, `thread`, `cron`, or `subagent`.
- `identity_hash` is a short stable hash of `identity_id` when available, otherwise `sender_id`.
- `chat_hash` is a short stable hash of `chat_id` for group/thread chats, otherwise `self`.

Raw platform IDs are not embedded in Hermes session IDs. They are stored only in our existing tenant-scoped stores.

## Redis Keys

Use keys that preserve our current tenant isolation:

```text
{tenant_id}:runtime:hermes:session:{session_id}       -> HASH metadata
{tenant_id}:runtime:hermes:run:{run_id}               -> JSON run summary
{tenant_id}:runtime:hermes:events:{run_id}            -> LIST event stream
{tenant_id}:runtime:hermes:shadow:{run_id}            -> JSON shadow result
{tenant_id}:runtime:hermes:checkpoint:{checkpoint_id} -> JSON checkpoint metadata
```

Do not write unscoped keys such as `hermes:session:*`.

## Session Lifecycle

| Event | Behavior |
|---|---|
| First user turn | Create session metadata with tenant, channel, platform, identity hash |
| Normal turn | Reuse session id and update last active timestamp |
| Fresh start request | Clear current chat history and rotate Hermes session with `reset=true` |
| `/new` equivalent | Rotate Hermes session and preserve old run summaries |
| Group chat sender switch | Keep chat session but include sender identity in runtime request |
| Identity linking | Future turns use the stable identity id; old session remains readable for audit |
| Tenant runtime disabled | Stop creating new Hermes sessions; existing session metadata remains for audit |

## Memory Scope Mapping

| User Scenario | Runtime Session | Memory Scope |
|---|---|---|
| Feishu DM | `dm` | tenant + identity |
| Feishu group | `group` | tenant + chat + sender identity |
| WeCom KF customer | `dm` | tenant + external customer id |
| QQ user | `dm` | tenant + QQ sender id |
| Cron job | `cron` | tenant + automation id |
| Subagent | `subagent` | parent session plus child suffix |

Subagents inherit read access to the parent request context but do not write durable profile memory directly. Parent runtime observes subagent results and decides what to persist.

## Attachment Mapping

Runtime attachments must use our existing media processing path. Hermes receives sanitized attachment descriptors:

```json
{
  "attachment_id": "att_123",
  "kind": "image",
  "url": "https://...",
  "source_platform": "feishu",
  "expires_at": "2026-05-21T13:00:00Z"
}
```

If an attachment URL is temporary, the bridge must either download it into our file store or mark it as expiring so Hermes does not persist it as durable memory.

## Test Matrix

- DM sessions from two tenants with the same sender id produce different session ids.
- Two channels under one tenant produce different session ids.
- Group chat sessions include chat scope and do not overwrite DM memory.
- Fresh start rotates the Hermes session and clears only the correct history key.
- Subagent session ids include parent run id and cannot write user profile directly.
- Shadow runs write to `runtime:hermes:shadow` without changing delivered history.
