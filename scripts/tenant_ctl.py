#!/usr/bin/env python3
"""租户实例管理 CLI — 在服务器上直接操作

兼容 Python 3.6+（不依赖 app.* 模块，直接操作文件和 Docker）

用法:
  python3 scripts/tenant_ctl.py list
  python3 scripts/tenant_ctl.py status <tenant_id>
  python3 scripts/tenant_ctl.py create --tenant-id mybot --name "My Bot" --platform wecom_kf --credentials '{"wecom_corpid": "..."}'
  python3 scripts/tenant_ctl.py restart <tenant_id>
  python3 scripts/tenant_ctl.py stop <tenant_id>
  python3 scripts/tenant_ctl.py destroy <tenant_id>
  python3 scripts/tenant_ctl.py build-image [--force]
  python3 scripts/tenant_ctl.py nginx-init
  python3 scripts/tenant_ctl.py regen-nginx
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

# ── Paths ──
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
INSTANCES_DIR = os.environ.get("INSTANCES_DIR", os.path.join(_PROJECT_ROOT, "instances"))
REGISTRY_FILE = os.path.join(INSTANCES_DIR, "registry.json")
NGINX_CONF_DIR = os.environ.get("NGINX_TENANT_DIR", "/etc/nginx/conf.d/tenants")
IMAGE_NAME = os.environ.get("BOT_IMAGE_NAME", "feishu-code-bot:latest")
CONTAINER_PREFIX = "bot-"
BASE_PORT = 8101
MAX_PORT = 8199


# ── Registry helpers ──

def _load_registry():
    if os.path.exists(REGISTRY_FILE):
        with open(REGISTRY_FILE, "r") as f:
            return json.load(f)
    return {}


def _save_registry(data):
    os.makedirs(INSTANCES_DIR, exist_ok=True)
    with open(REGISTRY_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _allocate_port(registry):
    used = set()
    for info in registry.values():
        used.add(info.get("port", 0))
    for port in range(BASE_PORT, MAX_PORT + 1):
        if port not in used:
            return port
    print("Error: no available ports in range %d-%d" % (BASE_PORT, MAX_PORT), file=sys.stderr)
    sys.exit(1)


def _run(cmd, timeout=120):
    print("$ " + " ".join(cmd))
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=timeout)
    if result.returncode != 0 and result.stderr:
        print("  stderr: " + result.stderr[:300], file=sys.stderr)
    return result


# ── Config generators ──

def _generate_tenant_json(config):
    return json.dumps({"tenants": [config]}, indent=2, ensure_ascii=False)


def _generate_compose(tenant_id, port, env_file):
    return (
        "# Auto-generated for tenant: {tid}\n"
        "# Do not edit manually\n\n"
        "services:\n"
        "  bot:\n"
        "    image: {img}\n"
        "    container_name: {prefix}{tid}\n"
        "    ports:\n"
        "      - \"{port}:8000\"\n"
        "    env_file:\n"
        "      - {env}\n"
        "    volumes:\n"
        "      - ./tenants.json:/app/tenants.json:ro\n"
        "      - {tid}-workspace:/tmp/workspace\n"
        "      - ./logs:/app/logs\n"
        "    restart: unless-stopped\n\n"
        "volumes:\n"
        "  {tid}-workspace:\n"
    ).format(tid=tenant_id, img=IMAGE_NAME, prefix=CONTAINER_PREFIX, port=port, env=env_file)


def _generate_nginx_conf(tenant_id, name, port):
    """生成租户 nginx 配置 — 与 provisioner.py 保持一致。

    路由:
      /webhook/feishu/{tenant_id}/*   → 飞书事件回调
      /webhook/{tenant_id}/*         → 飞书事件回调（兼容无平台前缀）
      /webhook/wecom/{tenant_id}     → 企微
      /webhook/wecom_kf/{tenant_id}  → 企微客服
      /oauth/{tenant_id}/callback    → OAuth 回调
      /health/{tenant_id}            → 健康检查
    """
    lines = [
        "# Tenant: %s (%s)" % (tenant_id, name),
        "# Port: %d" % port,
        "# Auto-generated — do not edit manually",
        "",
    ]
    proxy_block = "\n".join([
        "    proxy_set_header Host $host;",
        "    proxy_set_header X-Real-IP $remote_addr;",
        "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "    proxy_read_timeout 600s;",
        "    proxy_send_timeout 600s;",
    ])
    # 飞书 + 企微 + 企微客服 webhook（与 provisioner.py 一致）
    for prefix in ("feishu", "wecom", "wecom_kf"):
        lines.extend([
            "location /webhook/%s/%s {" % (prefix, tenant_id),
            "    proxy_pass http://127.0.0.1:%d/webhook/%s/%s;" % (port, prefix, tenant_id),
            proxy_block,
            "}",
            "",
        ])
    # 飞书兼容路由: /webhook/{tenant_id}/event（无平台前缀，FastAPI 也注册了这个）
    lines.extend([
        "location /webhook/%s/ {" % tenant_id,
        "    proxy_pass http://127.0.0.1:%d/webhook/%s/;" % (port, tenant_id),
        proxy_block,
        "}",
        "",
    ])
    # OAuth callback
    lines.extend([
        "location /oauth/%s/callback {" % tenant_id,
        "    proxy_pass http://127.0.0.1:%d/oauth/%s/callback;" % (port, tenant_id),
        "    proxy_set_header Host $host;",
        "    proxy_set_header X-Real-IP $remote_addr;",
        "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "}",
        "",
    ])
    # Health endpoint
    lines.extend([
        "location /health/%s {" % tenant_id,
        "    proxy_pass http://127.0.0.1:%d/health;" % port,
        "}",
        "",
    ])
    return "\n".join(lines)


# ── Commands ──

def cmd_list(args):
    registry = _load_registry()
    if not registry:
        print("No provisioned instances.")
        return

    fmt = "%-25s %-12s %-8s %-12s %-25s"
    print(fmt % ("TENANT", "PLATFORM", "PORT", "STATUS", "CONTAINER"))
    print("-" * 82)
    for tid, info in registry.items():
        container = info.get("container_name", CONTAINER_PREFIX + tid)
        check = _run(["docker", "inspect", "--format", "{{.State.Status}}", container])
        status = check.stdout.strip() if check.returncode == 0 else "not_found"
        print(fmt % (tid, info.get("platform", "?"), info.get("port", "?"), status, container))


def cmd_status(args):
    registry = _load_registry()
    info = registry.get(args.tenant_id)
    if not info:
        print("Error: instance %s not found" % args.tenant_id, file=sys.stderr)
        sys.exit(1)

    container = info.get("container_name", CONTAINER_PREFIX + args.tenant_id)
    check = _run(["docker", "inspect", "--format", "{{.State.Status}}|{{.State.StartedAt}}", container])
    if check.returncode == 0:
        parts = check.stdout.strip().split("|")
        print("container_status: " + parts[0])
        if len(parts) > 1:
            print("started_at: " + parts[1])
    else:
        print("container_status: not_found")

    for k, v in info.items():
        print("%s: %s" % (k, v))

    logs = _run(["docker", "logs", "--tail", "20", container])
    if logs.returncode == 0:
        print("\n--- Recent Logs ---")
        print(logs.stdout[-1000:] if logs.stdout else logs.stderr[-1000:])


def cmd_create(args):
    try:
        credentials = json.loads(args.credentials)
    except (json.JSONDecodeError, ValueError) as e:
        print("Error: invalid credentials JSON: %s" % e, file=sys.stderr)
        sys.exit(1)

    registry = _load_registry()
    tid = args.tenant_id

    if tid in registry:
        print("Error: tenant %s already exists (port %s)" % (tid, registry[tid].get("port")), file=sys.stderr)
        sys.exit(1)

    port = _allocate_port(registry)
    env_file = os.path.join(_PROJECT_ROOT, ".env")

    # Build tenant config
    tenant_config = {
        "tenant_id": tid,
        "name": args.name,
        "platform": args.platform,
        "llm_provider": "gemini",
        "llm_api_key": "${GEMINI_API_KEY}",
        "llm_model": "gemini-3-flash-preview",
        "llm_model_strong": "gemini-3.1-pro-preview",
    }
    tenant_config.update(credentials)
    if args.system_prompt:
        tenant_config["llm_system_prompt"] = args.system_prompt
    if args.custom_persona:
        tenant_config["custom_persona"] = True

    # Create instance directory
    inst_dir = os.path.join(INSTANCES_DIR, tid)
    os.makedirs(os.path.join(inst_dir, "logs"), exist_ok=True)

    # Write config files
    with open(os.path.join(inst_dir, "tenants.json"), "w") as f:
        f.write(_generate_tenant_json(tenant_config))
    with open(os.path.join(inst_dir, "docker-compose.yml"), "w") as f:
        f.write(_generate_compose(tid, port, env_file))

    # Nginx config
    nginx_conf = _generate_nginx_conf(tid, args.name, port)
    try:
        os.makedirs(NGINX_CONF_DIR, exist_ok=True)
        with open(os.path.join(NGINX_CONF_DIR, tid + ".conf"), "w") as f:
            f.write(nginx_conf)
    except (PermissionError, OSError):
        with open(os.path.join(inst_dir, "nginx.conf"), "w") as f:
            f.write(nginx_conf)
        print("Warning: cannot write to %s, saved to %s/nginx.conf" % (NGINX_CONF_DIR, inst_dir))

    # Build image
    check = _run(["docker", "image", "inspect", IMAGE_NAME])
    if check.returncode != 0:
        print("Building Docker image %s ..." % IMAGE_NAME)
        build = _run([
            "docker", "build",
            "--build-arg", "PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple",
            "-t", IMAGE_NAME, _PROJECT_ROOT,
        ], timeout=600)
        if build.returncode != 0:
            print("Error: image build failed", file=sys.stderr)
            sys.exit(1)

    # Start container
    compose_file = os.path.join(inst_dir, "docker-compose.yml")
    result = _run(["docker", "compose", "-f", compose_file, "up", "-d"])
    if result.returncode != 0:
        print("Error: container start failed", file=sys.stderr)
        sys.exit(1)

    # Register
    registry[tid] = {
        "tenant_id": tid,
        "name": args.name,
        "platform": args.platform,
        "port": port,
        "status": "running",
        "container_name": CONTAINER_PREFIX + tid,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    _save_registry(registry)

    # Reload nginx
    _run(["nginx", "-s", "reload"])

    # Health check
    print("Waiting for health check...")
    healthy = False
    for _ in range(10):
        time.sleep(2)
        try:
            check = _run(["curl", "-sf", "http://127.0.0.1:%d/health" % port])
            if check.returncode == 0:
                healthy = True
                break
        except Exception:
            pass

    webhook_paths = {
        "feishu": "/webhook/feishu/%s/event" % tid,
        "wecom": "/webhook/wecom/%s" % tid,
        "wecom_kf": "/webhook/wecom_kf/%s" % tid,
    }

    print("\nSuccess!")
    print("  Tenant:    %s" % tid)
    print("  Port:      %d" % port)
    print("  Container: %s%s" % (CONTAINER_PREFIX, tid))
    print("  Healthy:   %s" % healthy)
    print("  Webhook:   %s" % webhook_paths.get(args.platform, ""))
    print("\nNext: configure this webhook URL in your %s admin panel." % args.platform)


def cmd_restart(args):
    registry = _load_registry()
    if args.tenant_id not in registry:
        print("Error: instance %s not found" % args.tenant_id, file=sys.stderr)
        sys.exit(1)
    compose_file = os.path.join(INSTANCES_DIR, args.tenant_id, "docker-compose.yml")
    result = _run(["docker", "compose", "-f", compose_file, "restart"])
    if result.returncode == 0:
        print("Restarted %s" % args.tenant_id)
    else:
        sys.exit(1)


def cmd_stop(args):
    registry = _load_registry()
    if args.tenant_id not in registry:
        print("Error: instance %s not found" % args.tenant_id, file=sys.stderr)
        sys.exit(1)
    compose_file = os.path.join(INSTANCES_DIR, args.tenant_id, "docker-compose.yml")
    result = _run(["docker", "compose", "-f", compose_file, "down"])
    if result.returncode == 0:
        registry[args.tenant_id]["status"] = "stopped"
        _save_registry(registry)
        print("Stopped %s" % args.tenant_id)
    else:
        sys.exit(1)


def cmd_destroy(args):
    registry = _load_registry()
    if args.tenant_id not in registry:
        print("Error: instance %s not found" % args.tenant_id, file=sys.stderr)
        sys.exit(1)

    if not args.yes:
        try:
            confirm = input("Destroy tenant '%s'? This cannot be undone. [y/N] " % args.tenant_id)
        except EOFError:
            confirm = "n"
        if confirm.lower() != "y":
            print("Cancelled.")
            return

    inst_dir = os.path.join(INSTANCES_DIR, args.tenant_id)
    compose_file = os.path.join(inst_dir, "docker-compose.yml")
    _run(["docker", "compose", "-f", compose_file, "down", "-v"])

    nginx_file = os.path.join(NGINX_CONF_DIR, args.tenant_id + ".conf")
    if os.path.exists(nginx_file):
        os.remove(nginx_file)
        _run(["nginx", "-s", "reload"])

    if os.path.isdir(inst_dir):
        shutil.rmtree(inst_dir)

    del registry[args.tenant_id]
    _save_registry(registry)
    print("Destroyed %s" % args.tenant_id)


def cmd_build_image(args):
    if not args.force:
        check = _run(["docker", "image", "inspect", IMAGE_NAME])
        if check.returncode == 0:
            print("Image %s already exists. Use --force to rebuild." % IMAGE_NAME)
            return

    print("Building %s ..." % IMAGE_NAME)
    result = _run([
        "docker", "build",
        "--build-arg", "PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple",
        "-t", IMAGE_NAME, _PROJECT_ROOT,
    ], timeout=600)
    if result.returncode == 0:
        print("Image built successfully.")
    else:
        print("Image build failed.", file=sys.stderr)
        sys.exit(1)


def cmd_regen_nginx(args):
    """Regenerate nginx configs for all registered tenants."""
    registry = _load_registry()
    if not registry:
        print("No provisioned instances in registry.")
        return

    os.makedirs(NGINX_CONF_DIR, exist_ok=True)
    regenerated = []

    for tid, info in registry.items():
        name = info.get("name", tid)
        port = info.get("port")
        if not port:
            print("  SKIP %s: no port in registry" % tid)
            continue

        nginx_conf = _generate_nginx_conf(tid, name, port)
        conf_path = os.path.join(NGINX_CONF_DIR, tid + ".conf")
        try:
            with open(conf_path, "w") as f:
                f.write(nginx_conf)
            print("  Written: %s (port %d)" % (conf_path, port))
            regenerated.append(tid)
        except (PermissionError, OSError) as e:
            print("  ERROR %s: %s" % (conf_path, e), file=sys.stderr)

    if regenerated:
        test = _run(["nginx", "-t"])
        if test.returncode == 0:
            _run(["nginx", "-s", "reload"])
            print("\nnginx reloaded. Regenerated %d configs." % len(regenerated))
        else:
            print("\nERROR: nginx -t failed! Check configs manually.", file=sys.stderr)
    else:
        print("No configs regenerated.")


def cmd_nginx_init(args):
    template = os.path.join(_PROJECT_ROOT, "templates", "nginx-site.conf")
    if not os.path.exists(template):
        print("Error: template not found: %s" % template, file=sys.stderr)
        sys.exit(1)

    with open(template) as f:
        content = f.read()

    dest = "/etc/nginx/conf.d/bot-proxy.conf"
    try:
        os.makedirs(NGINX_CONF_DIR, exist_ok=True)
        with open(dest, "w") as f:
            f.write(content)
        print("Written: %s" % dest)
        print("Tenant configs dir: %s" % NGINX_CONF_DIR)
        print("Run: nginx -t && nginx -s reload")
    except PermissionError:
        print("Permission denied. Run with sudo.", file=sys.stderr)
        print("\nContent to write to %s:\n%s" % (dest, content))
        sys.exit(1)


def cmd_migrate(args):
    """Migrate all tenants from existing tenants.json to independent container instances."""
    source = args.source or os.path.join(_PROJECT_ROOT, "tenants.json")
    if not os.path.exists(source):
        print("Error: tenants.json not found: %s" % source, file=sys.stderr)
        sys.exit(1)

    with open(source, "r") as f:
        data = json.load(f)

    tenants = data.get("tenants", [])
    if not tenants:
        print("No tenants found in %s" % source)
        return

    registry = _load_registry()
    env_file = os.path.join(_PROJECT_ROOT, ".env")

    # Check image exists
    check = _run(["docker", "image", "inspect", IMAGE_NAME])
    if check.returncode != 0:
        print("Error: image %s not found. Run: python3 scripts/tenant_ctl.py build-image" % IMAGE_NAME,
              file=sys.stderr)
        sys.exit(1)

    migrated = []

    for tenant_config in tenants:
        tid = tenant_config.get("tenant_id")
        name = tenant_config.get("name", tid)
        platform = tenant_config.get("platform", "unknown")

        if not tid:
            print("Skipping tenant without tenant_id")
            continue

        if tid in registry:
            print("Skipping %s (already provisioned on port %s)" % (tid, registry[tid].get("port")))
            continue

        port = _allocate_port(registry)

        print("\n=== Migrating %s (%s) → port %d ===" % (tid, name, port))

        # Create instance directory
        inst_dir = os.path.join(INSTANCES_DIR, tid)
        os.makedirs(os.path.join(inst_dir, "logs"), exist_ok=True)

        # Write full tenant config (preserving all fields)
        with open(os.path.join(inst_dir, "tenants.json"), "w") as f:
            f.write(json.dumps({"tenants": [tenant_config]}, indent=2, ensure_ascii=False))

        # Write docker-compose
        with open(os.path.join(inst_dir, "docker-compose.yml"), "w") as f:
            f.write(_generate_compose(tid, port, env_file))

        # Nginx config
        nginx_conf = _generate_nginx_conf(tid, name, port)
        try:
            os.makedirs(NGINX_CONF_DIR, exist_ok=True)
            with open(os.path.join(NGINX_CONF_DIR, tid + ".conf"), "w") as f:
                f.write(nginx_conf)
        except (PermissionError, OSError):
            with open(os.path.join(inst_dir, "nginx.conf"), "w") as f:
                f.write(nginx_conf)
            print("  Warning: cannot write to %s, saved to %s/nginx.conf" % (NGINX_CONF_DIR, inst_dir))

        # Start container
        compose_file = os.path.join(inst_dir, "docker-compose.yml")
        result = _run(["docker", "compose", "-f", compose_file, "up", "-d"])
        if result.returncode != 0:
            print("  Error: failed to start %s" % tid, file=sys.stderr)
            continue

        # Register
        registry[tid] = {
            "tenant_id": tid,
            "name": name,
            "platform": platform,
            "port": port,
            "status": "running",
            "container_name": CONTAINER_PREFIX + tid,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        _save_registry(registry)
        migrated.append((tid, port))

    # Reload nginx once
    if migrated:
        _run(["nginx", "-s", "reload"])

    # Health checks
    print("\n=== Health Checks ===")
    for tid, port in migrated:
        healthy = False
        for _ in range(5):
            time.sleep(2)
            check = _run(["curl", "-sf", "http://127.0.0.1:%d/health" % port])
            if check.returncode == 0:
                healthy = True
                break
        print("  %s (:%d): %s" % (tid, port, "healthy" if healthy else "UNHEALTHY"))

    print("\nMigrated %d / %d tenants." % (len(migrated), len(tenants)))
    if migrated:
        print("\nNext steps:")
        print("  1. Verify each instance works (send test messages)")
        print("  2. Update DNS/Nginx to route traffic to new containers")
        print("  3. Stop the old single-process deployment")


def main():
    parser = argparse.ArgumentParser(description="Tenant Instance Management CLI")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List all instances")

    p = sub.add_parser("status", help="Show instance status")
    p.add_argument("tenant_id")

    p = sub.add_parser("create", help="Create a new instance")
    p.add_argument("--tenant-id", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--platform", required=True, choices=["feishu", "wecom", "wecom_kf"])
    p.add_argument("--credentials", required=True, help="JSON with platform credentials")
    p.add_argument("--system-prompt", default="")
    p.add_argument("--custom-persona", action="store_true")

    p = sub.add_parser("restart", help="Restart an instance")
    p.add_argument("tenant_id")

    p = sub.add_parser("stop", help="Stop an instance")
    p.add_argument("tenant_id")

    p = sub.add_parser("destroy", help="Destroy an instance")
    p.add_argument("tenant_id")
    p.add_argument("-y", "--yes", action="store_true")

    p = sub.add_parser("build-image", help="Build shared Docker image")
    p.add_argument("--force", action="store_true")

    sub.add_parser("nginx-init", help="Initialize nginx config")

    sub.add_parser("regen-nginx", help="Regenerate nginx configs for all registered tenants")

    p = sub.add_parser("migrate", help="Migrate all tenants from tenants.json to independent instances")
    p.add_argument("--source", default="", help="Path to tenants.json (default: project root)")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    handlers = {
        "list": cmd_list,
        "status": cmd_status,
        "create": cmd_create,
        "restart": cmd_restart,
        "stop": cmd_stop,
        "destroy": cmd_destroy,
        "build-image": cmd_build_image,
        "nginx-init": cmd_nginx_init,
        "regen-nginx": cmd_regen_nginx,
        "migrate": cmd_migrate,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
