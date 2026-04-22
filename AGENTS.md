# Agent Guide (Dual-Repo Context)

> First read this file, then read `CLAUDE.md`.

## 0) Current Repo Role

This repository (`4d-bot`) is the **open-source/public-facing sibling repo**.

Typical work that starts here:
- Open-source friendly improvements.
- Documentation/examples and generalized hardening.
- Features that should be reusable outside current production deployment specifics.

## 1) Relationship With The Other Bot Repo

Related repo: `../4dgames-feishu-code-bot`

- The two repos share code lineage and many modules.
- They are **not auto-synced**.
- Changes must be intentionally ported/cherry-picked when needed.

Working assumption for new agents:
- Live production incidents are usually fixed in `4dgames-feishu-code-bot` first.
- Generic fixes can then be ported here.

## 2) Sync Policy (How To Avoid Drift)

When you change behavior here, always decide one of:

1. `OSS_ONLY`
   - Keep change only in this repo (public/demo specific).
2. `PORT_TO_4DGAMES_FEISHU_CODE_BOT`
   - Port to production repo (if it affects real runtime behavior).
3. `DEFER_PORT`
   - Record reason why not ported now (dependency/risk/time).

At minimum, include in PR/commit notes:
- whether this must be ported;
- target files in the sibling repo if port is required.

## 3) Repo Selection Rules (Fast)

If task is about:
- Public/open-source maintainability -> may start here.
- Live tenant logs, operational regressions, channel incidents -> start in `4dgames-feishu-code-bot`.

If uncertain: start from production repo for correctness, then port back here.

## 4) Practical Checklist Before You Edit

1. Confirm you are in the intended repo (`pwd` + `git remote -v`).
2. Search same module path in sibling repo if change is generic.
3. Avoid assuming behavior parity across repos.
4. After fix, explicitly decide: `OSS_ONLY` vs `PORT_TO_4DGAMES_FEISHU_CODE_BOT`.

## 5) Non-Goals

- Do not claim both repos are identical.
- Do not silently diverge shared interfaces without documenting port impact.
