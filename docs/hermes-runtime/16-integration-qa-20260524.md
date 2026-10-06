# Hermes Runtime Integration QA - 2026-05-24

## Candidate

- Branch: `codex/hermes-integrated-qa-20260524`
- Base candidate: `origin/codex/hermes-admin-health-endpoint-20260524`
- Head commit under test: `6e2dd91 feat: expose Hermes runtime events admin API`
- Main baseline: `origin/main` at `3b1a0d8`
- Port policy: `PORT_TO_4D_BOT`

## Scope

This QA pass validates the integrated Hermes runtime foundation stack before PR review. The stack includes:

- async runtime tool bridge
- tenant-scoped skill context
- worker planned-tool loop
- memory bridge events
- provider credential fallback policy
- checkpoint ledger metadata
- code execution gate
- MCP discovery bridge
- sidecar app and health probe
- upstream OpenAI-compatible Hermes loop adapter
- runtime payload compatibility
- sidecar response parser hardening
- admin runtime event retrieval

Production tenant rollout is intentionally out of scope for this pass.

## Production Safety

`tenants.json` and `tenants.example.json` do not enable Hermes runtime for any tenant. The default `TenantConfig` remains:

- `agent_runtime = "legacy"`
- `hermes_runtime_enabled = false`
- `hermes_runtime_shadow = false`
- `hermes_runtime_rollout_percent = 0`
- `hermes_runtime_fallback_to_legacy = true`

The branch adds runtime code and docs only. It does not switch visible production traffic to Hermes.

## Verification

Commands run from:

`/Users/tianjiaowu/.config/superpowers/worktrees/4dgames-feishu-code-bot/hermes-integrated-qa-20260524`

```bash
scripts/run_tests.sh tests/test_hermes_runtime_contract.py tests/test_hermes_runtime_client.py tests/test_hermes_upstream_api.py tests/test_hermes_worker_upstream.py tests/test_hermes_sidecar_app.py tests/test_hermes_tool_bridge.py tests/test_hermes_memory_bridge.py tests/test_hermes_provider_routing.py tests/test_hermes_checkpoint_policy.py tests/test_hermes_code_execution.py tests/test_hermes_mcp_bridge.py tests/test_hermes_observability.py tests/test_hermes_admin_runtime.py tests/test_route_integration.py -q
```

Result: `61 passed`.

```bash
scripts/run_tests.sh tests/test_skill_engine.py tests/test_agent_skill_repo_install.py tests/test_tool_groups.py -q
```

Result: `96 passed, 2 warnings`.

```bash
scripts/run_tests.sh tests -q -rs
```

Result: `1110 passed, 7 skipped, 5 warnings`.

Skipped tests were environment-dependent local-agent Docker/desktop checks:

- Docker CLI/daemon/image unavailable
- desktop automation unavailable without `pyautogui`

Warnings were existing deprecation/syntax warnings and not introduced by this integration branch.

## Remaining Gaps

This branch should be treated as a runtime foundation, not final Hermes parity. Remaining parity work:

- prove the pinned upstream Hermes `AIAgent` loop in a running sidecar environment
- verify real upstream tool-call round trips, not only planned-tool bridge tests
- add rollback-capable checkpoint recovery, beyond metadata ledger records
- validate tenant-scoped MCP server lifecycle under real MCP servers
- validate code execution sandbox behavior with real runtime jobs
- run internal shadow traffic before visible tenant rollout

## Merge Recommendation

The integrated stack is ready for PR review as a dormant foundation branch. Merge is safe only if reviewers accept that production defaults stay legacy and that Hermes visible rollout remains blocked until the remaining gaps above have evidence.
