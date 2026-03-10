#!/usr/bin/env bash
# Rebuild instances/ directory after accidental deletion (e.g. git clean -fd).
#
# This recreates the docker-compose.yml for each provisioned container.
# After running this, use sync_instance_configs.py to generate per-container tenants.json,
# then docker compose up -d to start.
#
# Usage: bash scripts/rebuild_instances.sh

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

# .env file path (relative to each instance dir)
ENV_FILE="$PROJECT_ROOT/.env"

# Instance definitions: tenant_id container_name port
INSTANCES=(
  "code-bot bot-code-bot 8101"
  "pm-bot bot-pm-bot 8102"
  "kf-steven-ai bot-kf-steven-ai 8103"
)

IMAGE_NAME="${BOT_IMAGE_NAME:-feishu-code-bot:latest}"

for entry in "${INSTANCES[@]}"; do
  read -r tid cname port <<< "$entry"
  dir="instances/$tid"

  echo ">>> creating $dir"
  mkdir -p "$dir/logs"

  cat > "$dir/docker-compose.yml" <<COMPOSE
# Auto-generated — tenant: $tid
# Do not edit manually

services:
  bot:
    image: $IMAGE_NAME
    container_name: $cname
    ports:
      - "$port:8000"
    env_file:
      - $ENV_FILE
    volumes:
      - ./tenants.json:/app/tenants.json:ro
      - ${tid}-workspace:/tmp/workspace
      - ./logs:/app/logs
    extra_hosts:
      - "host.docker.internal:host-gateway"
    restart: unless-stopped
    mem_limit: 512m
    memswap_limit: 768m
    oom_kill_disable: false
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s

volumes:
  ${tid}-workspace:
COMPOSE

  echo "  created $dir/docker-compose.yml (port $port, container $cname)"
done

echo ""
echo ">>> now run: python3 scripts/sync_instance_configs.py"
echo ">>> then:    for dir in instances/*/; do docker compose -f \"\$dir/docker-compose.yml\" up -d; done"
echo ""
echo "Done."
