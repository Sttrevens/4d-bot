# 12 Migration Plan

## Goal

Deliver a complete Hermes-backed Agent OS foundation behind our existing production bot shell, with per-tenant rollout, full observability, and reversible deployment.

## Architecture

The migration introduces `app/hermes_runtime/` as an adapter layer. The existing app remains the business owner. Hermes runs as a pinned sidecar runtime and calls our tools, skills, memory, MCP, and code execution only through bridges.

## Tech Stack

- Python 3.12 app runtime.
- Upstream Hermes pinned at commit `c6a992e3e3cb99d935da3d059093b5b1f839738c`.
- Redis for tenant/session/run state.
- Existing FastAPI, tenant config, tool registry, task run, metering, and dashboard systems.
- Pytest for unit, integration, replay, and parity tests.

## Files To Create

| File | Responsibility |
|---|---|
| `app/hermes_runtime/__init__.py` | Runtime package boundary |
| `app/hermes_runtime/types.py` | Request, response, event, artifact, error dataclasses |
| `app/hermes_runtime/selector.py` | Tenant runtime selection and rollout hashing |
| `app/hermes_runtime/client.py` | Sidecar client and timeout/fallback handling |
| `app/hermes_runtime/worker.py` | Sidecar process/service entrypoint |
| `app/hermes_runtime/tool_bridge.py` | Tool schema and execution bridge |
| `app/hermes_runtime/tool_policy.py` | Side-effect and permission classification |
| `app/hermes_runtime/skills_bridge.py` | Repo skill activation and file reads |
| `app/hermes_runtime/memory_bridge.py` | Hermes-compatible memory provider backed by our stores |
| `app/hermes_runtime/memory_scope.py` | Tenant/channel/user/session memory scope helpers |
| `app/hermes_runtime/checkpoint.py` | Checkpoint creation and side-effect ledger |
| `app/hermes_runtime/side_effects.py` | Side-effect classes and rollback availability |
| `app/hermes_runtime/provider_router.py` | Tenant model config to Hermes provider config |
| `app/hermes_runtime/credential_policy.py` | Tenant-scoped credential pool policy |
| `app/hermes_runtime/code_execution.py` | Execution backend adapter |
| `app/hermes_runtime/execution_policy.py` | Backend and command safety policy |
| `app/hermes_runtime/mcp_bridge.py` | MCP discovery and execution bridge |
| `app/hermes_runtime/mcp_registry.py` | MCP server registry and tenant filters |
| `app/hermes_runtime/events.py` | Runtime event schema and redaction |
| `app/hermes_runtime/observability.py` | Projection into task runs, traces, logs, and metering |

## Files To Modify

| File | Change |
|---|---|
| `app/tenant/config.py` | Add runtime, rollout, MCP, and execution config fields |
| `app/router/intent.py` | Select runtime after preflight checks and before provider call |
| `app/services/base_agent.py` | Share tool group expansion and skill activation helpers with bridges |
| `app/tools/skill_engine.py` | Include repo-skill support from `codex/agent-skill-repo-install` |
| `app/tools/skill_mgmt_ops.py` | Expose repo-skill management tools |
| `app/admin/routes.py` | Add runtime status and shadow comparison endpoints |
| `tenants.json` | Keep defaults legacy; optionally enable shadow for internal tenant |
| `.github/workflows/*.yml` | Add focused runtime test jobs if branch CI time becomes high |
| `requirements.txt` or lock file | Add pinned sidecar dependencies only after packaging strategy is chosen |

## Phase P0: Source And License Baseline

- [ ] Pin upstream Hermes source by commit and preserve MIT license notice.
- [ ] Decide package form: vendored source under `vendor/hermes-agent` or locked install artifact.
- [ ] Add `docs/hermes-runtime/` matrix to the branch.
- [ ] Record `PORT_TO_4D_BOT` in commit notes because this is generic runtime capability.

Exit: engineers know exactly which Hermes source is allowed in production.

## Phase P1: Runtime Contract And Selector

- [ ] Create `app/hermes_runtime/types.py` with `RuntimeRequest`, `RuntimeResponse`, `RuntimeEvent`, `RuntimeErrorInfo`.
- [ ] Create `app/hermes_runtime/selector.py`.
- [ ] Add tenant fields in `TenantConfig` with safe legacy defaults.
- [ ] Add tests in `tests/test_hermes_runtime_contract.py`.
- [ ] Wire `route_message` to select runtime while still calling legacy provider for all tenants.

Exit: runtime selection is testable and disabled by default.

## Phase P2: Sidecar Client Skeleton

- [ ] Create `HermesRuntimeClient` with timeout, health check, and structured error mapping.
- [ ] Create `worker.py` that can accept a serialized `RuntimeRequest` and return a `RuntimeResponse`.
- [ ] Add failure tests for unavailable sidecar and malformed response.
- [ ] Add shadow mode storage without delivering shadow output.

Exit: sidecar can be called in tests without executing real tools.

## Phase P3: Tool Bridge

- [ ] Build tenant-scoped tool schemas from existing `_get_tenant_tools`.
- [ ] Implement unknown-tool and denied-tool responses.
- [ ] Normalize `ToolResult` into runtime tool results.
- [ ] Add side-effect classification and sequential/concurrent execution policy.
- [ ] Test whitelist tenants and lazy groups.

Exit: Hermes can call read-only and safe write tools through our policy layer.

## Phase P4: Repo Skills And Skill Activation

- [ ] Merge or cherry-pick `codex/agent-skill-repo-install`.
- [ ] Ensure `extension` group includes repo-skill tools and legacy skill tools.
- [ ] Implement `SkillsBridge` activation cards.
- [ ] Add `guizang-ppt-skill` fixture and acceptance test.
- [ ] Verify large `SKILL.md` files are not injected directly.

Exit: `帮我做一份瑞士风 PPT` triggers the repo skill in Hermes runtime.

## Phase P5: Memory Bridge

- [ ] Implement scoped recall from profile, journal, and org memory.
- [ ] Implement background memory write candidates through existing quality classifier.
- [ ] Ensure subagents cannot write profile memory directly.
- [ ] Add admin-visible memory events.

Exit: Hermes recall is useful and canonical data remains in our Redis stores.

## Phase P6: Provider, Context, And Checkpoint

- [ ] Generate Hermes provider config from tenant LLM settings.
- [ ] Add tenant-scoped credential policy and exhaustion tracking.
- [ ] Wire context compression events into observability.
- [ ] Create checkpoint ledger for side-effecting tools.
- [ ] Add rollback and unknown side-effect state tests.

Exit: runtime can survive long tool loops, provider fallback, and safe mutations.

## Phase P7: Subagents, Code Execution, And MCP

- [ ] Enable bounded Hermes subagents for internal tenants.
- [ ] Add execution backend policy and deny skill script execution by default.
- [ ] Add MCP registry with disabled defaults.
- [ ] Expose MCP discovery only to allowlisted tenants.
- [ ] Add replay tests for subagent and MCP tool loading.

Exit: advanced Agent OS capabilities are available without global exposure.

## Phase P8: Production Rollout

- [ ] Run legacy vs Hermes shadow evaluation on internal tenants.
- [ ] Enable `hermes_runtime_shadow=true` for `pm-bot`.
- [ ] Compare quality, latency, tool calls, memory behavior, and cost.
- [ ] Enable `hermes_runtime_rollout_percent=10` for one internal tenant.
- [ ] Increase to 50 and 100 only after SLOs hold.
- [ ] Keep customer-facing tenants legacy until internal rollout passes.

Exit: at least one internal tenant uses Hermes runtime for user-visible turns.

## Verification Commands

Focused runtime suite:

```bash
uv run --python 3.12 --with-requirements requirements.txt --with pytest --with pytest-asyncio python -m pytest \
  tests/test_hermes_runtime_contract.py \
  tests/test_hermes_tool_bridge.py \
  tests/test_hermes_memory_bridge.py \
  tests/test_hermes_provider_routing.py \
  tests/test_hermes_checkpoint_policy.py \
  tests/test_hermes_code_execution.py \
  tests/test_hermes_mcp_bridge.py \
  tests/test_hermes_observability.py -q
```

Regression suite:

```bash
uv run --python 3.12 --with-requirements requirements.txt --with pytest --with pytest-asyncio python -m pytest tests/ -q
```

Replay suite:

```bash
uv run --python 3.12 --with-requirements requirements.txt --with pytest --with pytest-asyncio python -m pytest tests/test_scenario_replay.py tests/test_benchmark_runner.py -q
```

## Commit Cadence

1. Contract and selector.
2. Sidecar skeleton.
3. Tool bridge.
4. Repo skills bridge.
5. Memory bridge.
6. Provider/context/checkpoint.
7. Code execution and MCP.
8. Observability and admin endpoints.
9. Rollout config and runbook updates.

Each commit should state `PORT_TO_4D_BOT` or a specific defer reason.
