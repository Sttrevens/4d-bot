# Hermes Latest Stack QA - 2026-05-25

## Candidate

- Branch: `codex/hermes-latest-stack-qa-20260525`
- Base candidate: `origin/codex/hermes-autofix-boundary-20260525`
- Main baseline: `origin/main`
- Supersedes: draft PR `#49` as the preferred review candidate because it includes the post-#49 upstream tool, skill, memory, provider, checkpoint, approval, and auto-fix boundary slices.
- Port policy: `PORT_TO_4D_BOT`

## Added Since PR #49

The latest stack includes all of the dormant foundation work from PR `#49`, plus:

- upstream OpenAI tool-call round trip
- repo skill activation injection into upstream context
- upstream memory prefetch and diary sync
- tenant upstream model routing
- provider fallback retry
- tenant-scoped provider credential candidates
- upstream checkpoint events
- infrastructure-tool approval gating
- protected-path denial for Hermes-triggered auto-fix writes

## QA Fixes In This Branch

This branch fixes integration issues found by the deep QA automation:

- `tests/test_hermes_upstream_api.py` now restores `tenant_registry` after each test so provider credential state cannot leak into later skill or memory tests.
- Empty `RuntimePolicy.allowed_tool_names` now means "use the tenant-visible toolset", matching existing tenant semantics where `tools_enabled=[]` means all allowed tools.
- `RuntimePolicy.shadow_mode` blocks side-effecting tools in both synchronous and asynchronous tool execution paths before handler execution or checkpoint ledger creation.

## Production Safety

No visible Hermes rollout is enabled by this branch.

- `TenantConfig.agent_runtime` default remains `legacy`.
- `TenantConfig.hermes_runtime_enabled` default remains `false`.
- `TenantConfig.hermes_runtime_shadow` default remains `false`.
- `TenantConfig.hermes_runtime_rollout_percent` default remains `0`.
- `TenantConfig.hermes_runtime_fallback_to_legacy` default remains `true`.
- `tenants.json` is unchanged.

## Verification

Commands run from:

`/Users/tianjiaowu/.config/superpowers/worktrees/4dgames-feishu-code-bot/hermes-latest-stack-qa-20260525`

```bash
scripts/run_tests.sh tests/test_hermes_upstream_api.py::test_upstream_adapter_empty_policy_tools_uses_tenant_visible_toolset tests/test_hermes_upstream_api.py::test_upstream_adapter_injects_repo_skill_activation_card tests/test_hermes_upstream_api.py::test_upstream_adapter_prefetches_and_syncs_memory tests/test_hermes_tool_bridge.py::test_execute_tool_call_blocks_side_effects_in_shadow_mode_before_handler tests/test_hermes_tool_bridge.py::test_execute_tool_calls_blocks_async_side_effects_in_shadow_mode_before_handler -q
```

Result: `5 passed, 2 warnings`.

```bash
scripts/run_tests.sh tests/test_hermes_upstream_api.py tests/test_hermes_tool_bridge.py tests/test_hermes_checkpoint_policy.py tests/test_hermes_provider_routing.py -q
```

Result: `34 passed, 9 warnings`.

```bash
scripts/run_tests.sh tests/test_hermes_runtime_contract.py tests/test_hermes_runtime_client.py tests/test_hermes_upstream_api.py tests/test_hermes_worker_upstream.py tests/test_hermes_sidecar_app.py tests/test_hermes_tool_bridge.py tests/test_hermes_memory_bridge.py tests/test_hermes_provider_routing.py tests/test_hermes_checkpoint_policy.py tests/test_hermes_code_execution.py tests/test_hermes_mcp_bridge.py tests/test_hermes_observability.py tests/test_hermes_admin_runtime.py tests/test_route_integration.py -q
```

Result: `76 passed, 9 warnings`.

```bash
scripts/run_tests.sh tests/test_skill_engine.py tests/test_agent_skill_repo_install.py tests/test_tool_groups.py tests/test_scenario_replay.py tests/test_benchmark_runner.py -q
```

Result: `103 passed, 1 warning`.

```bash
scripts/run_tests.sh tests -q -rs
```

Result: `1127 passed, 7 skipped, 5 warnings`.

Skipped tests were environment-dependent local-agent Docker/desktop checks:

- Docker CLI/daemon/image unavailable
- desktop automation unavailable without `pyautogui`

Warnings were existing FastAPI deprecations, a regex SyntaxWarning, and OpenAI test-client cleanup warnings. They do not block this runtime integration branch.

## Remaining Non-Blocking Gaps

This remains a dormant runtime foundation, not a final Hermes parity claim. Remaining work before visible tenant rollout:

- run a real pinned upstream Hermes sidecar, not only an OpenAI-compatible mock endpoint
- validate real MCP server lifecycle and permissions
- validate code execution sandbox jobs under tenant policy
- add rollback-capable checkpoint recovery, beyond event/ledger metadata
- run internal shadow traffic and review telemetry before enabling any visible tenant rollout
