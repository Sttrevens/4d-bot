#!/usr/bin/env bash
# Isolate the 0.x SDK from the bot's Python 3.12 / OpenAI 1.x dependencies.
set -euo pipefail

source_sha=94183a4ae9def31d2fc4dbad316824eaffe735ca
target_dir="${1:?Usage: bash scripts/setup_a13n_worker.sh /absolute/path/to/agent-foundation}"
if [[ "$target_dir" != /* ]]; then
  echo "Use an absolute destination path." >&2
  exit 2
fi
if [[ ! -d "$target_dir" ]]; then
  git clone https://github.com/converge-ai-labs/agent-foundation.git "$target_dir"
  git -C "$target_dir" checkout --detach "$source_sha"
fi
if [[ "$(git -C "$target_dir" rev-parse HEAD)" != "$source_sha" ]]; then
  echo "Destination is not the reviewed upstream revision; refusing to alter it." >&2
  exit 2
fi
if [[ -n "$(git -C "$target_dir" status --porcelain --untracked-files=no)" ]]; then
  echo "Destination has tracked modifications; refusing installation." >&2
  exit 2
fi
uv python install 3.13
(
  cd "$target_dir"
  uv sync --locked --package a13n-harness --no-default-groups --python 3.13
)
"$target_dir/.venv/bin/python" -c 'from a13n_harness import HarnessBuilder, HarnessState; print("Pinned Harness worker environment ready")'
