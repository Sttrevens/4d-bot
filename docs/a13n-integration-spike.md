# Agent Foundation Harness integration spike

This is an opt-in runtime adapter, not a replacement for the bot's orchestration,
tool registry, memory service, or tenant policy. No tenant is enabled by this
change. RuntimeRequest and RuntimeResponse retain their existing fields.

## Reviewed current-main revisions

| Repository | Revision | Role |
| --- | --- | --- |
| Sttrevens/4d-bot | 74f59632089473418de104ddb085c2025dd96654 | Host and existing runtime contract, including merged PR #11 registry fix |
| Sttrevens/Project-TK | b218c05bd39622cd896f90ab9d401c605d1a90e0 | Workflow, skills, and subagent persona domain layer |
| converge-ai-labs/agent-foundation | 94183a4ae9def31d2fc4dbad316824eaffe735ca | Process-local Harness SDK |

The bot baseline was refreshed from e03059f to 74f5963 immediately before
publication. The adapter preserves that update's authoritative plugin-registry
groups and core export_file mapping; its group regressions remain in sanity CI.

The upstream source package currently reports `0.0.0`. Pin the source revision
and its `uv.lock`, rather than installing an unconstrained package version.
`packages/a13n-harness/pyproject.toml` requires Python 3.13 and OpenAI 3.x;
the bot uses Python 3.12 and OpenAI 1.x. The SDK therefore runs in an isolated
worker environment. The host never imports it; its only added dependency is
JSON Schema validation so it validates arguments independently before dispatch.

## Installation and explicit rollout

```bash
bash scripts/setup_a13n_worker.sh /srv/a13n/agent-foundation
```

Configure the operator-owned worker environment with model credentials. The
adapter passes an allowlist of model/TLS environment variables, not tenant
configuration or the host's complete environment. Do not use shared model
credentials for tenants that require independent credentials or billing.

```json
{
  "agent_runtime_provider": "a13n_harness",
  "agent_runtime_enabled": true,
  "agent_runtime_shadow": true,
  "agent_runtime_rollout_percent": 0,
  "agent_runtime_fallback_to_legacy": true,
  "agent_runtime_command": "/srv/a13n/agent-foundation/.venv/bin/python",
  "agent_runtime_args": ["/srv/4d-bot/scripts/a13n_worker.py"],
  "agent_runtime_model": "openai:gpt-4.1-mini",
  "agent_runtime_auto_execute": false,
  "a13n_tool_permissions": {
    "think": "allow",
    "send_message": "ask"
  }
}
```

The model string is an operator-selected Harness/Pydantic AI alias, not a
recommended model or a compatibility promise for all providers. The first
spike supports text and the current bot tool schemas. Media is rejected before
execution, allowing the existing legacy path to handle it.

Start with shadow traffic; shadow effects are denied and its state is isolated.
For visible traffic set shadow false and choose an explicit rollout percent.
Rollback is `agent_runtime_enabled=false` or `agent_runtime_provider="legacy"`.
The original selector, other providers, and ordinary legacy fallback remain.

## Contract and ownership

The host uses a bounded JSONL protocol over worker stdin/stdout. It sends the
current RuntimeRequest, visible tool schemas, derived permission modes, and an
accepted state candidate. The worker constructs a real Harness execution and
fresh bindings, then requests tool dispatch over that protocol. The host sends
normalized results from the existing tool bridge back to the worker.

| Concern | Owner and behavior |
| --- | --- |
| Run/Thread | Original request run_id plus SDK run_id; stable thread derived from tenant, channel, platform, history_key, sender identity, and shadow scope |
| State | Real HarnessState serialization; host atomically persists completed states and loads them for the next turn |
| Current authority | Rebuild toolset, permissions, and execution policy each run; state never grants authority |
| Tool execution | Existing build_runtime_toolset and execute_tool_call_async; empty visibility stays empty |
| Permissions | Native SDK allow/ask/deny/review plus host enforcement; tenant gates and confirmation requirements still apply |
| Ask/review | Suspend without dispatch; approval resumption is not implemented in this spike |
| Observability | Existing runtime event/summary storage; correlated IDs, decisions, statuses, and usage counts; no raw prompts, args, results, or state |
| Memory/skills | Existing base_agent system prompt composer and tools; no a13n memory store or duplicate registry |
| Project-TK | Catalog/skill/persona context decorates input.chat_context; runtime choice and actual child dispatch belong to its host |

`review` without a reviewer is permissive in the upstream SDK. This adapter
supplies a fail-closed reviewer and also blocks review dispatch in the host.
No approval is inferred from a prior state's tool calls or grants.

## Persistence and recovery limits

Set `A13N_STATE_DIR` to an operator-owned persistent volume; the default
`.runtime/a13n` is local to the bot working directory. State includes conversation
and tool-result data, so protect and retain it as conversation history. Files
are private and writes are atomic. A per-thread OS lock prevents concurrent
advancement on a single host. This spike targets Linux/POSIX and local disk,
not shared network filesystems, distributed leases, or Windows.

Completed-state continuation is supported. Approval continuation, interrupted
call replay, remote worker recovery, and exactly-once side effects are not.
The worker uses `tool_recovery="never"`. Before effectful dispatch the host
writes a journal marker; a crash or unknown outcome blocks automated replay.
The host must reconcile the external outcome before resolving that marker.
`fresh_start` must not erase unresolved effects. No automatic cleanup procedure
claims an uncertain write failed or succeeds.

RuntimeResponse.resume carries additive `fallback_safe` metadata. A paused gate
or uncertain/post-dispatch failure marks it false, so the router cannot retry
the same task through legacy. Failures before side-effect dispatch retain
normal fallback. Disabling the provider is a routing rollback, not an undo of
already executed tools.

## What is worth integrating

| Capability | Decision | Reason |
| --- | --- | --- |
| Run/State and current bindings | Integrate now | Portable continuation and clear correlation without changing public contracts |
| Tool permissions | Integrate now | Additional execution gate, using existing tenant and tool authority |
| Structured usage/events | Integrate now | Makes runtime comparison and suspension visible in current diagnostics |
| ToolProxy group discovery | Defer | Current plugin registry and lazy loading already own discovery; add only if measured tool-context savings justify it |
| Portable execution environments | Defer | Current bot sandbox/execution policy is the authority; environment migration needs a separate container integration |
| Memory, skills, tool registry | Keep current | Duplicating them introduces drift and conflicting authority |
| Service/Console | Do not import | Their workers, storage, and UI exceed this runtime spike |
| Durable replay/reconciliation | Host follow-up | Harness serialization does not implement accepted commits, worker leases, or external effect reconciliation |

## Concrete code boundaries

The worker uses upstream `packages/a13n-harness/a13n_harness/builder.py`
(`HarnessBuilder.build`), `execution.py` (`ExecutableAgent.stream`), `state.py`
(`HarnessState`), `context.py` (`RunBindings`), `tools/permissions.py`
(`ToolPermissionsCapability`), `tools/identity.py` (`source_tool_id`), and the
public events/usage contracts. Current upstream permissions are definition-time
capabilities, so the worker rebuilds the definition from each current host
request before continuing accepted state. No permissions are restored from
HarnessState. Instrumentation is opted out of background exporters; safe event
projection goes to the existing host diagnostics.

| Files | Change |
| --- | --- |
| app/agent_runtime/a13n_adapter.py, a13n_state.py | Host protocol, existing tool/prompt bridges, state and journal |
| scripts/a13n_worker.py, setup_a13n_worker.sh | Isolated real SDK worker and exact-source installation |
| app/agent_runtime/client.py, providers.py | Optional provider dispatch and accurate capability manifest |
| app/hermes_runtime/selector.py, types.py | Additional provider name; existing rollout and request/response fields retained |
| app/tenant/config.py | Optional tool permission overrides; defaults remain disabled |
| app/router/intent.py | Carry fresh-start and prevent unsafe fallback |
| requirements.txt, .gitignore | Host schema validator and private local state exclusion |
| tests/test_a13n_adapter.py, tests/fixtures/a13n/, tests/a13n_worker/ | Host replay/benchmark contracts and real SDK tests |
| scripts/run_tests.sh, .github/workflows/a13n-contract.yml | Host sanity and isolated SDK CI |
| Project-TK/scripts/runtime_domain_contract.py | Domain-only request mapper and response correlation |
| Project-TK/tests/ccgs_codex_migration/test_runtime_domain_contract.py, docs/runtime-domain-contract.md | Mapper verification and host integration boundary |

## Verification

The host tests use deterministic process transcripts but dispatch through the
actual existing tool bridge. A scenario fixture is checked with scenario_replay
and passed to benchmark.run_benchmark_suite through its injected replay_fn;
the default benchmark remains offline and gains no misleading live path.
Separate worker tests run the real pinned SDK with Pydantic AI FunctionModel,
without model API calls or external tool side effects.

```bash
python -m pytest -q tests/test_a13n_adapter.py tests/test_agent_runtime_providers.py tests/test_hermes_runtime_contract.py tests/test_scenario_replay.py tests/test_benchmark_runner.py
/srv/a13n/agent-foundation/.venv/bin/python -m pytest -q tests/a13n_worker
```

Worker test dependencies may be installed in the isolated environment; do not
add the SDK to the bot environment. `scripts/run_tests.sh sanity` includes the
host contract tests; the isolated SDK tests have their own CI job, which also
runs the real worker subprocess boundary from a separate Python 3.12 host.

```bash
uv pip install --python /srv/a13n/agent-foundation/.venv/bin/python 'pytest>=9,<10'
A13N_TEST_PYTHON=/srv/a13n/agent-foundation/.venv/bin/python python -m pytest -q tests/test_a13n_adapter.py
```

At the reviewed revisions, local validation produced:

| Check | Result |
| --- | --- |
| Adapter host contracts, including real SDK subprocess continuation | 49 passed |
| Isolated real SDK worker contracts | 16 passed |
| Bot sanity with the real subprocess boundary enabled | 245 passed |
| Bot full suite | 845 passed, 2 pre-existing memory failures, 8 missing-Chromium environment errors, 5 skipped |
| Project-TK mapper contracts | 13 passed |
| Project-TK required migration suite | 48 passed, 1 pre-existing PyYAML frontmatter trailing-newline failure |
| Project-TK staged commit validation; bot compile and whitespace checks | Passed |

The two bot memory failures were reproduced in an exact `e03059f` checkout.
No live model API call, deployment, or runtime rollout was performed.

## Port policy

`DEFER_PORT`: do not enable or copy this experimental runtime into
4dgames-feishu-code-bot until its operators validate isolated credentials,
persistent storage, and interrupted-effect reconciliation. A later intentional
port would involve app/agent_runtime/a13n_adapter.py (and its state helper),
scripts/a13n_worker.py, selector/provider dispatch/config, and the router's
fallback guard. The two bot repositories are not automatically synchronized.
