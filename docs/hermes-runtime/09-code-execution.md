# 09 Code Execution

## Purpose

Hermes has multiple terminal and execution backends. Our production bot has sandbox tools, browser automation, Lark CLI, self-edit tools, and tenant containers. The integration should expand execution power without weakening production isolation.

## Backend Strategy

| Backend | Use Case | Production Default |
|---|---|---|
| No execution | Chat, research, planning | Default for customer-facing tenants |
| Existing sandbox | Small Python/JS snippets and safe transforms | Enabled where current tools allow |
| Tenant Docker container | Code-bot and internal PM workflows | Preferred for code mutation tenants |
| SSH backend | Server operations | Admin-only |
| Browser backend | Web inspection and login-dependent flows | Existing `browser_*` policy |
| Lark CLI | Feishu APIs not covered by native tools | Existing `lark_cli_*` policy |
| Skill script execution | Future sandboxed validation | Disabled until explicit policy exists |

## New Files

- `app/hermes_runtime/code_execution.py`
- `app/hermes_runtime/execution_policy.py`
- `tests/test_hermes_code_execution.py`

## Execution Policy Envelope

```json
{
  "backend": "docker",
  "cwd": "/workspace",
  "network": "restricted",
  "timeout_seconds": 120,
  "max_output_chars": 20000,
  "allow_file_write": true,
  "allow_package_install": false,
  "allowed_paths": ["app/tools", "app/knowledge"],
  "requires_checkpoint": true
}
```

The bridge chooses this envelope from tenant config, tool type, and admin status.

## Path Policy

Execution tools must inherit the existing auto-fix path boundary:

- Read operations can inspect broader repo context when the tenant owns that repo.
- Write operations require allowlisted paths.
- Infrastructure paths require human engineering work.
- Generated artifacts can be written only to artifact/export directories.

## Terminal Approval

Commands classified as destructive require confirmation:

- `rm`, `mv` over existing files, `chmod -R`, `chown -R`
- `git reset --hard`, `git checkout --`, force pushes
- `docker stop`, `docker rm`, deployment commands
- package installs in production containers
- scripts from installed repo skills

The approval request must include command, working directory, expected side effect, and rollback availability.

## Skill Scripts

Repo skills may include scripts such as `validate-swiss-deck.mjs`. v1 policy:

- Store readable scripts when extension is safe.
- Allow the model to inspect script contents.
- Do not execute skill scripts automatically.
- Add script execution only after sandbox policy can prove package, filesystem, and network boundaries.

## Tests

- Customer-facing tenant defaults to no terminal execution.
- Internal code tenant can use Docker backend when allowlisted.
- Destructive commands return `needs_confirmation`.
- Writes outside allowed paths are denied.
- Skill script execution is denied even when script file exists.
- Execution result is truncated for model context but full output remains in run artifact/log storage.
