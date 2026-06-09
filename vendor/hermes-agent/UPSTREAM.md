# Hermes Agent Upstream Pin

This repository integrates Hermes Agent as a replaceable Agent OS runtime
behind `app/hermes_runtime/`.

Production must use a pinned upstream source, not a floating branch:

- Repository: `https://github.com/NousResearch/hermes-agent`
- Commit: `c6a992e3e3cb99d935da3d059093b5b1f839738c`
- Version observed in `pyproject.toml`: `0.14.0`
- Python requirement observed upstream: `>=3.11`
- License: MIT

The current implementation does not vendor the entire upstream source tree.
It pins the intended source and exposes a dormant adapter boundary. If a later
phase vendors or packages upstream Hermes code, keep the MIT license notice in
this directory and update `HERMES_RUNTIME_SOURCE_SHA`.
