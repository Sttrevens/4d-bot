# Hermes Runtime Integration Matrix

## Decision

The selected direction is **Option A: Hermes as an Agent OS sidecar runtime**.

Our production bot keeps ownership of business identity, tenant routing, platform webhooks, tool allowlists, delivery, metering, trial, and deployment. Hermes is introduced behind an adapter as the agent capability substrate: agent loop, context engine, memory provider hooks, skill lifecycle, tool execution runtime, subagents, model/provider routing, MCP, terminal backends, and checkpoint-aware execution.

This is not a small proof. The target is a complete runtime foundation that can eventually run high-capability tenants through Hermes while preserving our current production contract.

## Upstream Baseline

Use the upstream Hermes repository as the reference implementation:

- Repository: `NousResearch/hermes-agent`
- Evaluated commit: `c6a992e3e3cb99d935da3d059093b5b1f839738c`
- Evaluated version: `0.14.0`
- Python requirement: `>=3.11`
- License: MIT, with license notice preserved if code is vendored or distributed.

Hermes source must be pinned. Runtime integration must not depend on a floating `main` branch in production.

## Document Matrix

| Doc | Purpose | Executable Output | Exit Criteria |
|---|---|---|---|
| [00-architecture.md](00-architecture.md) | Target architecture and ownership boundaries | Runtime sidecar topology, component map, control flow | An engineer can place new modules without guessing ownership |
| [01-capability-matrix.md](01-capability-matrix.md) | Gap analysis against Hermes | Adopt/bridge/keep/reject decisions for every major capability | Every Hermes capability has a disposition |
| [02-runtime-adapter-contract.md](02-runtime-adapter-contract.md) | Stable contract between our app and Hermes runtime | Request/response schema, error taxonomy, feature flags | Legacy and Hermes runtimes are swappable per tenant |
| [03-tenant-session-mapping.md](03-tenant-session-mapping.md) | Tenant, channel, user, and session identity mapping | Redis keys, session IDs, reset/resume behavior | No tenant or user state can cross boundaries |
| [04-tool-bridge.md](04-tool-bridge.md) | Tool exposure and execution bridge | Schema adapter, permission gate, result normalizer | Hermes can call our tools without bypassing tenant policy |
| [05-skills-bridge.md](05-skills-bridge.md) | Skill storage, activation, and repo-skill compatibility | Repo skill activation card and file-read bridge | `guizang-ppt-skill` can run through the Hermes runtime path |
| [06-memory-bridge.md](06-memory-bridge.md) | Memory provider integration | Canonical memory policy and provider adapter | Hermes recall improves the agent without corrupting our profile data |
| [07-checkpoint-rollback.md](07-checkpoint-rollback.md) | Checkpoint, approval, and rollback model | Mutation checkpoint policy and recovery flow | File/system side effects are auditable and reversible |
| [08-provider-routing.md](08-provider-routing.md) | Model and credential routing | Runtime provider strategy, fallback rules, metering alignment | Tenants can route to Hermes without losing quota controls |
| [09-code-execution.md](09-code-execution.md) | Code execution and terminal backend strategy | Sandbox and backend selection matrix | Code execution can expand safely beyond the current sandbox |
| [10-mcp-integration.md](10-mcp-integration.md) | MCP tool discovery and connector model | MCP registry, tenant allowlist, lifecycle | MCP tools can be enabled without prompt/tool explosion |
| [11-observability.md](11-observability.md) | Logs, traces, metrics, and debugging | Event schema, dashboard views, SLOs | Runtime behavior is debuggable from a single run ID |
| [12-migration-plan.md](12-migration-plan.md) | Full implementation plan | Phase-by-phase tasks with files and tests | A team can execute to complete production rollout |
| [13-test-benchmark-plan.md](13-test-benchmark-plan.md) | Test and benchmark program | Unit, integration, replay, parity, security suites | Runtime migration is measured against current bot behavior and Hermes parity |
| [14-deployment-runbook.md](14-deployment-runbook.md) | Production rollout and rollback | Config, deploy, monitor, rollback commands | Operators can safely enable, pause, or revert Hermes runtime |
| [15-port-to-4d-bot.md](15-port-to-4d-bot.md) | Sibling repo port policy | Port map and drift controls | Generic runtime work is intentionally synced to `../4d-bot` |

## Complete Target State

The complete state is reached when:

1. Tenants can choose `legacy` or `hermes_sidecar` runtime without changing webhook handlers.
2. Hermes runtime can execute our existing tools through the bridge and cannot access tools outside tenant allowlists.
3. Repo-style skills, including `op7418/guizang-ppt-skill`, activate through short cards and read their full files on demand.
4. Memory reads and writes are scoped by tenant, channel, user, and session, with our profile data remaining canonical unless explicitly migrated.
5. Subagents, code execution, MCP, provider routing, and checkpoints are available behind feature flags.
6. Scenario replay and benchmark suites prove equal or better completion quality before production cutover.
7. Deployment can roll forward and roll back per tenant without merging runtime state across customers.

## Implementation Principle

The sidecar is replaceable. Business logic never imports Hermes internals directly. All Hermes-specific code is contained under the runtime adapter and can be disabled per tenant with one config switch.
