#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

export PYTHONPATH="${PYTHONPATH:-.}"
export ADMIN_TOKEN="${ADMIN_TOKEN:-dummy-admin-token}"
export BOT_LOG_FILE="${BOT_LOG_FILE:-/tmp/bot.log}"
export GITHUB_TOKEN="${GITHUB_TOKEN:-dummy-github-token}"
export GITHUB_REPO_OWNER="${GITHUB_REPO_OWNER:-sanity-owner}"
export GITHUB_REPO_NAME="${GITHUB_REPO_NAME:-sanity-repo}"
export SELF_REPO_OWNER="${SELF_REPO_OWNER:-sanity-owner}"
export SELF_REPO_NAME="${SELF_REPO_NAME:-sanity-repo}"
export KIMI_API_KEY="${KIMI_API_KEY:-dummy-kimi-key}"
export GEMINI_API_KEY="${GEMINI_API_KEY:-dummy-gemini-key}"
export GOOGLE_GEMINI_BASE_URL="${GOOGLE_GEMINI_BASE_URL:-https://example.com}"
export FEISHU_APP_ID="${FEISHU_APP_ID:-dummy-feishu-app-id}"
export FEISHU_APP_SECRET="${FEISHU_APP_SECRET:-dummy-feishu-app-secret}"
export FEISHU_VERIFICATION_TOKEN="${FEISHU_VERIFICATION_TOKEN:-dummy-verification-token}"
export FEISHU_ENCRYPT_KEY="${FEISHU_ENCRYPT_KEY:-dummy-encrypt-key}"
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-dummy-anthropic-key}"
export TENANTS_CONFIG_PATH="${TENANTS_CONFIG_PATH:-tenants.example.json}"

if command -v python >/dev/null 2>&1; then
  PYTEST=(python -m pytest)
  PYTHON=(python)
elif command -v python3.12 >/dev/null 2>&1; then
  PYTEST=(python3.12 -m pytest)
  PYTHON=(python3.12)
elif command -v uv >/dev/null 2>&1; then
  PYTEST=(uv run --python 3.12 --with-requirements requirements.txt --with pytest --with pytest-asyncio python -m pytest)
  PYTHON=(uv run --python 3.12 --with-requirements requirements.txt --with pytest --with pytest-asyncio python)
else
  PYTEST=(python3 -m pytest)
  PYTHON=(python3)
fi

mode="${1:-all}"
if [[ "$mode" == "sanity" ]]; then
  "${PYTHON[@]}" -m compileall -q app
  "${PYTHON[@]}" - <<'PY'
from app.harness import PLAN_ACTIVE_STATUSES, advance_next_step
from app.services import planner
from app.router.intent import route_message
from app.main import app

assert PLAN_ACTIVE_STATUSES
assert callable(advance_next_step)
assert callable(route_message)
assert app is not None
assert hasattr(planner, "create_plan")
print("Sanity import chain OK")
PY
  "${PYTEST[@]}" -q \
    tests/test_action_claims.py \
    tests/test_turn_mode.py \
    tests/test_grounding.py \
    tests/test_unmatched_reads.py \
    tests/test_session_facts.py \
    tests/test_tool_escalation.py \
    tests/test_empty_fallback.py \
    tests/test_plugin_registry.py \
    tests/test_tool_output_ledger.py \
    tests/test_scenario_replay.py \
    tests/test_benchmark_runner.py
elif [[ "$mode" == "all" ]]; then
  "${PYTEST[@]}" tests/ -v
else
  "${PYTEST[@]}" "$@"
fi
