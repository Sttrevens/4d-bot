# 13 Test And Benchmark Plan

## Purpose

The Hermes migration must be proven by tests and replay, not by impression. This plan measures correctness, safety, tenant isolation, capability parity, and production readiness.

## Test Pyramid

| Layer | Scope | Command |
|---|---|---|
| Unit | Selector, types, policies, schema conversion | `pytest tests/test_hermes_* -q` |
| Integration | Runtime client, tool bridge, skill bridge, memory bridge | focused runtime suite |
| Replay | Existing scenario replay against legacy and Hermes | `pytest tests/test_scenario_replay.py -q` |
| Parity | Legacy answer vs Hermes answer quality and side effects | benchmark runner |
| Security | Tenant isolation, secret redaction, policy bypass attempts | security focused tests |
| Production smoke | Shadow run on internal tenant | admin runtime dashboard and logs |

## Required Unit Tests

Create:

- `tests/test_hermes_runtime_contract.py`
- `tests/test_hermes_tool_bridge.py`
- `tests/test_hermes_memory_bridge.py`
- `tests/test_hermes_provider_routing.py`
- `tests/test_hermes_checkpoint_policy.py`
- `tests/test_hermes_code_execution.py`
- `tests/test_hermes_mcp_bridge.py`
- `tests/test_hermes_observability.py`

Key assertions:

- Runtime selector respects tenant config and rollout hash.
- Runtime request never includes tools outside allowlist.
- Tool results normalize errors and retry hints.
- Repo skill activation injects card rather than full file.
- Memory bridge cannot cross tenant boundaries.
- Provider config redacts raw secrets.
- Destructive commands request confirmation.
- MCP tools are hidden unless enabled per tenant.
- Events include run id and are redacted.

## Skill Acceptance Fixture

Add minimal fixture:

```text
tests/fixtures/skills/guizang-ppt-skill/
  SKILL.md
  assets/template-swiss.html
  references/layouts-swiss.md
```

Scenario:

1. Install fixture archive.
2. Send `帮我做一份瑞士风 PPT`.
3. Assert `guizang-ppt-skill` activation.
4. Assert runtime reads `assets/template-swiss.html`.
5. Assert export replaces `<!-- SLIDES_HERE -->`.
6. Assert returned artifact is HTML.

## Replay Scenarios

Add scenario files:

```text
tests/fixtures/scenarios/hermes_skill_ppt.json
tests/fixtures/scenarios/hermes_memory_recall.json
tests/fixtures/scenarios/hermes_tool_denial.json
tests/fixtures/scenarios/hermes_subagent_research.json
tests/fixtures/scenarios/hermes_mcp_lazy_load.json
```

Each scenario should run in both legacy and Hermes modes when applicable.

## Benchmark Metrics

| Metric | Compare |
|---|---|
| Completion success | Legacy vs Hermes |
| Tool-call correctness | Expected tools vs actual |
| Side-effect safety | Denied or confirmed as expected |
| Latency | End-to-end and tool-only |
| Cost | API calls and token usage |
| Context size | Prompt tokens and compression count |
| Memory usefulness | Recalled facts used correctly |
| User-visible quality | Human review score or rubric |

## Regression Gates

Before enabling user-visible Hermes runtime:

- Runtime focused tests pass.
- Existing tool group tests pass.
- Existing skill engine tests pass.
- Scenario replay has no severe regression.
- Shadow-mode side effects are blocked.
- Secret redaction tests pass.
- Tenant isolation tests pass.

## Security Cases

Add adversarial tests:

- Hermes calls a tool not in `allowed_tool_names`.
- Hermes attempts to load `admin` group through lazy expansion.
- Hermes asks to read another tenant skill file.
- Hermes tries to execute a repo skill script.
- Hermes requests a destructive terminal command without approval.
- Hermes attempts to persist low-confidence memory as durable identity fact.
- Hermes returns raw API key-like text in an event payload.

## Production Shadow Review

For each shadow run, store:

- Legacy final answer.
- Hermes final answer.
- Tool calls from both runtimes.
- Runtime events.
- Usage and latency.
- Human or rubric score.

Promotion requires at least 50 representative internal shadow runs with no critical policy failures.
