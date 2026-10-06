# 01 Capability Matrix

## Summary

Hermes is stronger as an Agent OS. Our bot is stronger as a production business shell for Chinese team workflows, Feishu/WeCom tenants, customer provisioning, and deployment. Option A combines these strengths by adopting Hermes runtime capabilities through bridges while keeping business ownership local.

## Matrix

| Capability | Current Bot | Hermes Agent | Decision | Complete State |
|---|---|---|---|---|
| Platform ingress | Feishu, WeCom, WeCom KF, QQ with tenant routing | Gateway supports many chat platforms | Keep ours | Existing handlers remain the production ingress |
| Agent loop | Custom Gemini loop with harness nudges, exit governor, progress hints | Mature loop with interruption, concurrent tools, memory/skill nudges | Adopt behind adapter | Hermes loop can run a tenant without changing handlers |
| Tool registry | `ALL_TOOL_MAP`, `_ALL_TOOL_DEFS`, tenant allowlists, lazy groups | Toolsets and tool executor with guardrails | Bridge | Hermes sees a generated toolset based on our allowlist |
| Tool execution | Sequential and policy-mediated; some retry hints | Concurrent execution, interrupt propagation, result storage budget | Adopt execution semantics, keep policy | Bridge supports concurrent safe tools and ordered results |
| Prompt/harness | Strong production guardrails for grounding, delivery, object binding | Runtime-oriented system prompt, context, skills | Merge via adapter | Our business contracts become runtime policy blocks |
| Context compaction | Harness compaction and provider-specific compression | Pluggable `ContextEngine` | Adopt | A context engine can be selected per runtime profile |
| Memory | Redis journal, profile, quality classification, org recall | Pluggable memory providers, lifecycle hooks, external providers | Bridge | Our Redis profile remains canonical; Hermes provider hooks read/write through adapter |
| Skills | Redis `SKILL.md` parser, repo install branch pending | Agentskills-compatible skills and self-improvement loop | Bridge and extend | Repo skills activate with cards and file reads; Hermes skill loop uses our storage |
| Repo-style skill install | Implemented on branch `codex/agent-skill-repo-install` | Native filesystem skill model | Port into runtime bridge | `guizang-ppt-skill` works from Redis-backed repo files |
| Subagents | Existing `should_delegate_to_sub_agent` and internal sub-agent path | Isolated subagents and parallel workstreams | Adopt | Runtime can spawn bounded subagents with tenant-scoped tools |
| Code execution | Sandbox, self tools, browser, lark-cli, limited auto-fix | Multiple terminal backends and checkpoint-aware mutations | Adopt selectively | Backend selected by tenant and guarded by our policy |
| Checkpoints | Auto-fix deploy rollback and some harness ledgers | Checkpoint manager before mutations | Bridge | File and terminal mutations produce checkpoints before execution |
| Provider routing | Gemini default, strong model escalation, metering | Provider registry, credential pool, model switching | Adopt behind quota controls | Runtime provider config is generated from tenant config and metered locally |
| MCP | Limited local CLI/tool integrations | MCP support as first-class extension layer | Adopt | MCP servers are tenant-allowlisted and lazy-loaded |
| Cron/automation | Reminders, scheduler, task run progress | Built-in cron scheduler | Bridge later | Our reminders remain canonical; Hermes cron can execute agent runs behind same delivery |
| Observability | Logs, latency trace, task_run, metering, dashboard APIs | Runtime events and gateway status | Merge | Every Hermes run has our `run_id` and event projection |
| Scenario replay | Harness benchmark and scenario replay tests | Trajectory generation/compression | Merge | Replay suite measures legacy vs Hermes behavior |
| Deployment | Dockerized per-tenant containers, GitHub Actions main deploy | Runs on many backends | Keep ours | Hermes sidecar is deployed in our container topology |

## Adopt Criteria

Adopt a Hermes capability when all conditions hold:

1. It is runtime substrate, not tenant business logic.
2. It can be placed behind `app/hermes_runtime/*`.
3. It can be enabled per tenant.
4. It preserves our audit, quota, and delivery semantics.
5. It can be tested with scenario replay.

## Keep Criteria

Keep our implementation when any condition holds:

1. It contains production tenant policy.
2. It depends on Feishu/WeCom/QQ credentials or platform-specific delivery.
3. It encodes billing, trial, customer provisioning, or admin dashboard behavior.
4. It is already the canonical data source for user, tenant, or customer state.

## Reject Criteria

Reject or postpone a Hermes capability when:

1. It requires exposing raw tenant secrets to upstream runtime code.
2. It bypasses our tenant allowlists or action authorization.
3. It executes unreviewed scripts from installed skills.
4. It requires changing production webhook URLs.

## Priority Order

1. Runtime adapter contract.
2. Tool bridge.
3. Skill bridge.
4. Memory bridge.
5. Context engine and provider routing.
6. Subagents and code execution.
7. MCP and cron.
8. Production rollout and port to `../4d-bot`.
