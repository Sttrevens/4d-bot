# Local Agent Runtime Providers

`4d-bot` can route a tenant to a local agent runtime instead of the built-in legacy provider. This is intended for open-source users who want to bring their own local coding agent, such as Codex CLI, Claude CLI, a Hermes sidecar, or a custom command.

The default remains safe:

- `agent_runtime_provider` is empty.
- `agent_runtime_enabled` is `false`.
- `agent_runtime_sandbox` is `read-only`.
- Write-capable sandboxes are ignored unless `agent_runtime_auto_execute` is explicitly `true`.
- `agent_runtime_fallback_to_legacy` is `true`.

## Providers

| Provider | Command | Best For | Default Safety |
|---|---|---|---|
| `legacy` | none | Existing in-process Gemini/OpenAI-compatible bot path | Always default |
| `hermes_sidecar` | HTTP sidecar or local worker | Hermes-compatible runtime contract and tool bridge | Disabled unless opted in |
| `codex_cli` | `codex exec --json` | Codex local/CLI workflows with JSONL events | `read-only` sandbox |
| `claude_cli` | `claude -p --output-format stream-json` | Claude local/CLI workflows and session-capable automation | `permission_mode=plan` |
| `custom_command` | operator supplied | Any local agent that can print a final answer to stdout | Disabled unless opted in |

The admin API exposes the same capability matrix:

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  https://YOUR_DOMAIN/admin/api/runtime/providers
```

## Codex CLI

Minimal read-only configuration:

```json
{
  "agent_runtime_provider": "codex_cli",
  "agent_runtime_enabled": true,
  "agent_runtime_rollout_percent": 100,
  "agent_runtime_command": "codex",
  "agent_runtime_workspace": "/srv/workspaces/my-repo",
  "agent_runtime_sandbox": "read-only",
  "agent_runtime_auto_execute": false
}
```

Write-capable configuration:

```json
{
  "agent_runtime_provider": "codex_cli",
  "agent_runtime_enabled": true,
  "agent_runtime_rollout_percent": 100,
  "agent_runtime_command": "codex",
  "agent_runtime_workspace": "/srv/workspaces/my-repo",
  "agent_runtime_sandbox": "workspace-write",
  "agent_runtime_auto_execute": true
}
```

Use write-capable modes only inside an isolated workspace or container. `danger-full-access` is supported by configuration but should be reserved for hardened local environments.

## Claude CLI

Minimal planning configuration:

```json
{
  "agent_runtime_provider": "claude_cli",
  "agent_runtime_enabled": true,
  "agent_runtime_rollout_percent": 100,
  "agent_runtime_command": "claude",
  "agent_runtime_workspace": "/srv/workspaces/my-repo",
  "agent_runtime_permission_mode": "plan",
  "agent_runtime_auto_execute": false
}
```

More permissive Claude modes should be enabled only after the operator has reviewed the local workspace, credentials, and command execution policy.

## Custom Command

`custom_command` runs an operator-supplied command and appends the user prompt as the final argument. The command must print the final user-facing answer to stdout.

```json
{
  "agent_runtime_provider": "custom_command",
  "agent_runtime_enabled": true,
  "agent_runtime_rollout_percent": 100,
  "agent_runtime_command": "/usr/local/bin/my-agent --json",
  "agent_runtime_workspace": "/srv/workspaces/my-repo"
}
```

For richer integrations, prefer implementing the shared `RuntimeRequest` / `RuntimeResponse` contract and running the agent as a sidecar.

## Field Reference

| Field | Default | Meaning |
|---|---:|---|
| `agent_runtime_provider` | `""` | `hermes_sidecar`, `codex_cli`, `claude_cli`, or `custom_command` |
| `agent_runtime_enabled` | `false` | Required opt-in for local agent providers |
| `agent_runtime_rollout_percent` | `0` | Visible traffic percentage from `0` to `100` |
| `agent_runtime_shadow` | `false` | Run runtime in the background without delivering its answer |
| `agent_runtime_fallback_to_legacy` | `true` | Fall back to the built-in provider on runtime failure |
| `agent_runtime_timeout_seconds` | `180` | Runtime subprocess/sidecar budget |
| `agent_runtime_command` | `""` | CLI binary or command path |
| `agent_runtime_args` | `[]` | Extra CLI args inserted before the prompt |
| `agent_runtime_workspace` | `""` | Working directory for local CLI runtimes |
| `agent_runtime_sandbox` | `read-only` | Codex sandbox mode |
| `agent_runtime_permission_mode` | `plan` | Claude permission mode |
| `agent_runtime_auto_execute` | `false` | Required before write-capable sandbox modes are honored |
| `agent_runtime_model` | `""` | Optional model override passed to provider CLIs |

## Port Policy

This is an `OSS_ONLY` capability unless production explicitly chooses to expose local agent runtimes to live tenants. Do not copy tenant-specific workspaces, credentials, local CLI auth files, or allowlists between repos.
