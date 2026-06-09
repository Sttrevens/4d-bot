# 05 Skills Bridge

## Purpose

Bring Hermes-style skills into our multi-tenant Redis-backed skill system, including complete GitHub repo skills. The first acceptance sample is `op7418/guizang-ppt-skill`.

## Current State

Current `origin/main` supports:

- `install_skill_md`: installs a single `SKILL.md` file.
- `learn_skill`: stores generated skill instructions.
- `list_learned_skills`, `remove_skill`, `share_skill`.
- Dynamic skill trigger injection through `load_triggered_skills`.

Branch `codex/agent-skill-repo-install` adds repo-style install and must be merged or ported into this runtime work before Hermes skill activation is complete.

## Required Repo-Skill Tools

The Hermes runtime path depends on these tools being present:

- `install_agent_skill_from_github(url, branch?, triggers?)`
- `list_agent_skill_files(skill_name)`
- `read_agent_skill_file(skill_name, path, start_line?, max_lines?)`
- `export_agent_skill_template(skill_name, template_path, output_filename, replacements)`

These tools remain in our `extension` tool group and are controlled by tenant allowlists.

## Skill Activation Model

Repo-style skills must not inject large `SKILL.md` files directly into the runtime prompt. Activation returns a short card:

```xml
<skill-activation name="guizang-ppt-skill" type="repo">
  <summary>生成横向翻页网页 PPT，支持杂志风和瑞士国际主义风格。</summary>
  <how-to-use>
    Read files with read_agent_skill_file when detailed instructions, templates,
    or references are needed.
  </how-to-use>
  <available-files count="18">
    SKILL.md, assets/template.html, assets/template-swiss.html, references/layouts-swiss.md
  </available-files>
</skill-activation>
```

Hermes can then call `read_agent_skill_file` for the exact file it needs.

## Storage Contract

Use tenant-scoped Redis keys:

```text
agent_skills:{tenant_id}:_index                         -> SET skill names
agent_skills:{tenant_id}:{skill_name}:metadata           -> JSON metadata
agent_skills:{tenant_id}:{skill_name}:manifest           -> JSON file manifest
agent_skill_files:{tenant_id}:{skill_name}:{path}        -> STRING file content
```

File paths are normalized POSIX paths. Reject absolute paths, `..`, null bytes, and unsupported extensions.

## Safe File Policy

Install only text/template resources in v1:

- Allowed: `.md`, `.html`, `.css`, `.js`, `.mjs`, `.json`, `.txt`, `.svg`
- Rejected executable or risky files: `.py`, `.sh`, `.exe`, `.bat`, `.ps1`, binary media
- Saved but not executed: validation scripts with allowed text extensions such as `.mjs`

The runtime may read scripts as references but must not execute repo-provided scripts until the code execution sandbox policy explicitly allows it.

## Trigger Rules

Trigger priority:

1. Use `SKILL.md` frontmatter `triggers` when present.
2. Otherwise derive from name and description.
3. For PPT-like descriptions, add: `PPT`, `ppt`, `幻灯片`, `演示`, `slides`, `presentation`, `deck`, `瑞士风`, `杂志风`, `Swiss Style`.

Hermes and legacy runtime must call the same trigger function to avoid drift.

## `guizang-ppt-skill` Acceptance

After installing `https://github.com/op7418/guizang-ppt-skill`:

1. User says: `帮我做一份瑞士风 PPT`.
2. Runtime activates `guizang-ppt-skill` through a short activation card.
3. Runtime reads `SKILL.md` and `assets/template-swiss.html` on demand.
4. Runtime replaces exact template markers such as `<!-- SLIDES_HERE -->`.
5. Runtime exports a single HTML deck through `export_agent_skill_template`.
6. Existing file delivery sends the HTML artifact.

## Hermes Compatibility

Hermes filesystem skills and our Redis skills meet at `SkillsBridge`:

- Hermes asks for skill index by tenant.
- Bridge returns activation cards and file manifest.
- Hermes reads files through tool calls.
- Skill self-improvement suggestions are stored as proposed updates, not applied silently.
- Admin-approved updates can write new skill metadata or files.

## Tests

- Repo skill without root `SKILL.md` is rejected.
- Unsupported paths and extensions are rejected.
- Large archives, large files, and excessive file counts are rejected.
- PPT fallback triggers are generated when frontmatter triggers are missing.
- Activation injects a short card, not full skill contents.
- Hermes runtime can read line ranges from a repo skill file.
- Template export replaces exact strings and returns an HTML artifact.
- Legacy and Hermes runtime paths trigger the same skill for `瑞士风 PPT`.
