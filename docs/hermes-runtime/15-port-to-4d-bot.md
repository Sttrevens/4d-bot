# 15 Port To 4D Bot

## Decision

This work is `PORT_TO_4D_BOT`.

Reason: Hermes runtime integration is generic Agent OS capability, not production-only incident handling. The sibling repo `../4d-bot` should receive the same architecture and core adapter once the production-first implementation is stable.

## Port Scope

| Area | Port? | Notes |
|---|---|---|
| `docs/hermes-runtime/` | Yes | Port the full design matrix |
| `app/hermes_runtime/*` | Yes | Core runtime adapter is generic |
| `app/tenant/config.py` runtime fields | Yes | Keep defaults legacy |
| `app/router/intent.py` selector integration | Yes | Adjust to sibling repo routing shape |
| ToolBridge | Yes | Map to sibling repo tool registry names |
| SkillsBridge | Yes | Port repo-style skill install first if absent |
| MemoryBridge | Yes | Adjust memory store module paths |
| MCP bridge | Yes | Keep disabled by default |
| Deployment runbook | Partial | Rewrite production-specific CI/CD sections |
| Tenant allowlists | Partial | Do not copy production tenants blindly |
| Admin dashboard endpoints | Conditional | Port if sibling repo has matching admin surface |

## Drift Controls

1. Every implementation PR in this repo must include a `PORT_TO_4D_BOT` note.
2. Commit messages should name target files in sibling repo.
3. If a change cannot be ported immediately, mark `DEFER_PORT` with a reason.
4. Do not claim parity until tests pass in both repos.
5. Keep source pin and runtime contract identical unless sibling repo has a documented incompatibility.

## Port Order

1. Documentation matrix.
2. Tenant config fields with legacy defaults.
3. Runtime types and selector.
4. Sidecar client skeleton.
5. ToolBridge adjusted to sibling tool registry.
6. Repo-skill install support.
7. SkillsBridge and `guizang-ppt-skill` fixture.
8. MemoryBridge.
9. Provider routing and observability.
10. Code execution, MCP, and rollout runbook.

## Required Checks In `../4d-bot`

Before porting code, inspect:

```bash
pwd
git remote -v
rg -n "ALL_TOOL_MAP|_ALL_TOOL_DEFS|route_message|TenantConfig|skill_engine|skill_mgmt" app tests
rg --files docs
```

Then choose exact sibling paths. The two repos share lineage but are not auto-synced.

## Port Acceptance

- Sibling repo has the same runtime contract docs.
- Sibling tests cover selector, tool bridge, skill bridge, and tenant isolation.
- Default runtime remains legacy.
- No production tenant secrets or tenant-specific allowlists are copied from this repo.
- Commit notes explicitly say which production commit or branch was ported.
