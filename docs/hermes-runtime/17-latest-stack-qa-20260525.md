# Hermes Latest Stack QA - 2026-05-25

## Candidate

- OSS branch: `codex/hermes-safety-visibility-port-20260525`
- OSS base branch: `origin/codex/hermes-provider-routing-port-20260525-r2`
- Source repo: `../4dgames-feishu-code-bot`
- Source branch tip: `origin/codex/hermes-admin-rollout-visibility-20260525`
- Source commits ported: `73d5a04`, `31cc087`, `da3486e`, `aae1928`, `188dcb6`, `aae8259`
- Port policy: `PORT_TO_4D_BOT` because these are generic Agent OS/runtime capabilities, not tenant/deploy/wechat/openclaw behavior.

## Ported Runtime Slices

This OSS branch stacks on the prior runtime/provider parity port and adds:

- upstream checkpoint events from side-effect ledgers
- infrastructure-tool approval pauses before handler execution
- protected-path denial for Hermes-triggered auto-fix writes
- empty runtime tool policy semantics that use the tenant-visible toolset
- async MCP runtime toolset bridging through the Hermes tool loop
- tenant-scoped admin reads for stored Hermes shadow responses
- latest-stack QA coverage for the above behavior

## OSS Safety

No visible Hermes rollout is enabled by this port.

- `TenantConfig.agent_runtime` default remains `legacy`.
- `TenantConfig.hermes_runtime_enabled` default remains `false`.
- `TenantConfig.hermes_runtime_shadow` default remains `false`.
- `TenantConfig.hermes_runtime_rollout_percent` default remains `0`.
- `TenantConfig.hermes_runtime_fallback_to_legacy` default remains `true`.
- `tenants.json` is unchanged.

## Verification

Commands run from:

`/private/tmp/4d-bot-hermes-runtime-foundation-port-20260524`

```bash
PYTHONPYCACHEPREFIX=/private/tmp/4d-bot-pycache PYTHONPATH=/private/tmp/codex-uv-cache/archive-v0/Bn_cTSTsl1Ss_jAjB8CSc/lib/python3.12/site-packages /private/tmp/codex-uv-cache/archive-v0/Bn_cTSTsl1Ss_jAjB8CSc/bin/python3.12 -m pytest tests/test_hermes_upstream_api.py::test_upstream_adapter_empty_policy_tools_uses_tenant_visible_toolset tests/test_hermes_upstream_api.py::test_upstream_adapter_emits_checkpoint_event_for_side_effect_tool tests/test_hermes_upstream_api.py::test_upstream_adapter_pauses_for_infrastructure_confirmation_before_execution tests/test_hermes_upstream_api.py::test_upstream_adapter_blocks_autofix_protected_path_before_tool_execution tests/test_hermes_checkpoint_policy.py::test_async_tool_bridge_requires_confirmation_before_infrastructure_mutation tests/test_hermes_checkpoint_policy.py::test_async_tool_bridge_blocks_autofix_protected_path_before_checkpoint tests/test_hermes_mcp_bridge.py::test_mcp_runtime_toolset_executes_async_handler_through_tool_bridge tests/test_hermes_admin_runtime.py::test_admin_hermes_shadow_response_reads_tenant_scoped_result tests/test_hermes_observability.py::test_load_shadow_response_reads_tenant_scoped_result -q
```

Result: `9 passed, 1 warning`.

```bash
PYTHONPYCACHEPREFIX=/private/tmp/4d-bot-pycache PYTHONPATH=/private/tmp/codex-uv-cache/archive-v0/Bn_cTSTsl1Ss_jAjB8CSc/lib/python3.12/site-packages /private/tmp/codex-uv-cache/archive-v0/Bn_cTSTsl1Ss_jAjB8CSc/bin/python3.12 -m pytest tests/test_hermes*.py tests/test_route_integration.py -q
```

Result: `81 passed, 1 warning`.

Warning: existing `app/tools/capability_ops.py` invalid escape `SyntaxWarning`.

## Deferred

- `2c88fef` (`feat: gate Hermes execution tools`) is local-only in the production checkout and was not ported in this run. Port it after it is pushed or otherwise confirmed branch-ready/stable.
- No production-specific tenant/deploy/wechat/openclaw behavior was ported.

## Remaining Non-Blocking Gaps

This remains a dormant runtime foundation, not a final Hermes rollout claim. Remaining work before visible tenant rollout:

- run a real pinned upstream Hermes sidecar, not only an OpenAI-compatible mock endpoint
- validate real MCP server lifecycle and permissions
- validate code execution sandbox jobs under tenant policy
- add rollback-capable checkpoint recovery beyond event/ledger metadata
- review shadow telemetry before enabling visible runtime traffic
