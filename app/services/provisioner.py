"""租户实例供应系统 — Per-Tenant Container Provisioning

Phase 1: 每个租户运行在独立 Docker 容器中
Phase 2: 通过 bot tool 暴露给管理员（Control Plane）

架构:
  Host
  ├── nginx (port 80/443) ─── 路由 ──────────────┐
  ├── bot-tenant-a    (port 8101) ← ─────────────┤
  ├── bot-tenant-b    (port 8102) ← ─────────────┤
  └── instances/
      ├── registry.json
      ├── tenant-a/
      │   ├── docker-compose.yml
      │   ├── tenants.json
      │   └── logs/
      └── tenant-b/
          └── ...

每个实例:
- 独立 Docker 容器（同一镜像，不同配置）
- 独立端口（8101-8199）
- 独立 tenants.json（只含本租户）
- 独立 workspace/logs volume
- Nginx 反代路由
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

INSTANCES_DIR = Path(os.getenv(
    "INSTANCES_DIR", str(_PROJECT_ROOT / "instances"),
))
REGISTRY_FILE = INSTANCES_DIR / "registry.json"
NGINX_CONF_DIR = Path(os.getenv(
    "NGINX_TENANT_DIR", "/etc/nginx/conf.d/tenants",
))

# ── Supported platforms ────────────────────────────────────────────
SUPPORTED_PLATFORMS = ("feishu", "wecom", "wecom_kf", "qq")

# ── Port range ─────────────────────────────────────────────────────
BASE_PORT = 8101
MAX_PORT = 8199

# ── Docker ─────────────────────────────────────────────────────────
IMAGE_NAME = os.getenv("BOT_IMAGE_NAME", "feishu-code-bot:latest")
CONTAINER_PREFIX = "bot-"


# =====================================================================
#  Instance Info
# =====================================================================

@dataclass
class InstanceInfo:
    tenant_id: str
    name: str
    platform: str
    port: int
    status: str = "created"      # created | running | stopped | error
    created_at: float = 0.0
    updated_at: float = 0.0
    container_name: str = ""

    def __post_init__(self):
        if not self.container_name:
            self.container_name = f"{CONTAINER_PREFIX}{self.tenant_id}"
        if not self.created_at:
            self.created_at = time.time()
        self.updated_at = time.time()


# =====================================================================
#  Registry
# =====================================================================

class InstanceRegistry:
    """管理已供应实例的注册表（JSON 文件持久化）"""

    def __init__(self):
        self._instances: dict[str, InstanceInfo] = {}
        self._load()

    def _load(self):
        if REGISTRY_FILE.exists():
            try:
                data = json.loads(REGISTRY_FILE.read_text())
                for tid, info in data.items():
                    self._instances[tid] = InstanceInfo(**info)
            except Exception as e:
                logger.warning("Failed to load instance registry: %s", e)

    def _save(self):
        INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
        data = {tid: asdict(inst) for tid, inst in self._instances.items()}
        REGISTRY_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def allocate_port(self) -> int:
        used = {inst.port for inst in self._instances.values()}
        for port in range(BASE_PORT, MAX_PORT + 1):
            if port not in used:
                return port
        raise RuntimeError(f"No available ports in range {BASE_PORT}-{MAX_PORT}")

    def register(self, info: InstanceInfo):
        self._instances[info.tenant_id] = info
        self._save()

    def get(self, tenant_id: str) -> InstanceInfo | None:
        return self._instances.get(tenant_id)

    def remove(self, tenant_id: str):
        self._instances.pop(tenant_id, None)
        self._save()

    def list_all(self) -> dict[str, InstanceInfo]:
        return dict(self._instances)


# Singleton（进程内共享）
_registry = InstanceRegistry()


# =====================================================================
#  Shell helpers
# =====================================================================

def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a shell command with logging."""
    logger.info("provision$ %s", " ".join(cmd))
    timeout = kwargs.pop("timeout", 120)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, **kwargs,
        )
    except FileNotFoundError:
        # docker/docker-compose 未安装
        logger.warning("Command not found: %s", cmd[0])
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: command not found")
    if result.returncode != 0:
        logger.error(
            "Command failed (rc=%d): %s\nstderr: %s",
            result.returncode, " ".join(cmd), result.stderr[:500],
        )
    return result


# =====================================================================
#  Image
# =====================================================================

def build_image(force: bool = False) -> bool:
    """构建共享 Docker 镜像（所有租户容器复用同一个镜像）"""
    if not force:
        result = _run(["docker", "image", "inspect", IMAGE_NAME])
        if result.returncode == 0:
            logger.info("Image %s exists, skipping build", IMAGE_NAME)
            return True

    logger.info("Building Docker image %s ...", IMAGE_NAME)
    result = _run(
        [
            "docker", "build",
            "--build-arg", "PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple",
            "-t", IMAGE_NAME,
            str(_PROJECT_ROOT),
        ],
        timeout=600,
    )
    return result.returncode == 0


# =====================================================================
#  Config generators
# =====================================================================

def _generate_tenant_json(tenant_configs: dict | list[dict]) -> str:
    """生成 per-container tenants.json。支持单租户 dict 或多租户 list。"""
    if isinstance(tenant_configs, dict):
        tenant_configs = [tenant_configs]
    return json.dumps({"tenants": tenant_configs}, indent=2, ensure_ascii=False)


def _generate_compose(info: InstanceInfo, env_file: str) -> str:
    return (
        f"# Auto-generated — tenant: {info.tenant_id}\n"
        f"# Do not edit manually\n\n"
        f"services:\n"
        f"  bot:\n"
        f"    image: {IMAGE_NAME}\n"
        f"    container_name: {info.container_name}\n"
        f"    ports:\n"
        f"      - \"{info.port}:8000\"\n"
        f"    env_file:\n"
        f"      - {env_file}\n"
        f"    volumes:\n"
        f"      - ./tenants.json:/app/tenants.json:ro\n"
        f"      - {info.tenant_id}-workspace:/tmp/workspace\n"
        f"      - ./logs:/app/logs\n"
        f"    extra_hosts:\n"
        f"      - \"host.docker.internal:host-gateway\"\n"
        f"    restart: unless-stopped\n"
        f"    mem_limit: 512m\n"
        f"    memswap_limit: 768m\n"
        f"    oom_kill_disable: false\n"
        f"    healthcheck:\n"
        f"      test: [\"CMD\", \"python\", \"-c\", \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')\"]\n"
        f"      interval: 30s\n"
        f"      timeout: 5s\n"
        f"      retries: 3\n"
        f"      start_period: 10s\n\n"
        f"volumes:\n"
        f"  {info.tenant_id}-workspace:\n"
    )


def _generate_nginx_conf(info: InstanceInfo) -> str:
    lines = [
        f"# Tenant: {info.tenant_id} ({info.name})",
        f"# Port: {info.port}",
        f"# Auto-generated — do not edit manually",
        "",
    ]
    # 飞书 + 企微 + 企微客服 + QQ webhook
    for prefix in ("feishu", "wecom", "wecom_kf", "qq"):
        lines.extend([
            f"location /webhook/{prefix}/{info.tenant_id} {{",
            f"    proxy_pass http://127.0.0.1:{info.port}/webhook/{prefix}/{info.tenant_id};",
            f"    proxy_set_header Host $host;",
            f"    proxy_set_header X-Real-IP $remote_addr;",
            f"    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            f"    proxy_read_timeout 600s;",
            f"    proxy_send_timeout 600s;",
            f"}}",
            "",
        ])
    # 飞书兼容路由: /webhook/{tenant_id}/event（无平台前缀）
    lines.extend([
        f"location /webhook/{info.tenant_id}/ {{",
        f"    proxy_pass http://127.0.0.1:{info.port}/webhook/{info.tenant_id}/;",
        f"    proxy_set_header Host $host;",
        f"    proxy_set_header X-Real-IP $remote_addr;",
        f"    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        f"    proxy_read_timeout 600s;",
        f"    proxy_send_timeout 600s;",
        f"}}",
        "",
    ])
    # OAuth callback — 飞书授权回调路由到该租户容器
    lines.extend([
        f"location /oauth/{info.tenant_id}/callback {{",
        f"    proxy_pass http://127.0.0.1:{info.port}/oauth/{info.tenant_id}/callback;",
        f"    proxy_set_header Host $host;",
        f"    proxy_set_header X-Real-IP $remote_addr;",
        f"    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        f"}}",
        "",
    ])
    # 多 channel 路由: /webhook/channel/{channel_id}/*
    lines.extend([
        f"location /webhook/channel/ {{",
        f"    proxy_pass http://127.0.0.1:{info.port}/webhook/channel/;",
        f"    proxy_set_header Host $host;",
        f"    proxy_set_header X-Real-IP $remote_addr;",
        f"    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        f"    proxy_read_timeout 600s;",
        f"    proxy_send_timeout 600s;",
        f"}}",
        "",
    ])
    # health endpoint
    lines.extend([
        f"location /health/{info.tenant_id} {{",
        f"    proxy_pass http://127.0.0.1:{info.port}/health;",
        f"}}",
        "",
    ])
    return "\n".join(lines)


# =====================================================================
#  Co-host helpers (同 corp 企微客服共用容器)
# =====================================================================

def _find_cohost_instance(
    wecom_corpid: str,
    wecom_kf_secret: str = "",
) -> InstanceInfo | None:
    """查找同 corpid + 同凭证的已有 wecom_kf 实例。

    只有 corpid 和 kf_secret 都匹配时才 co-host（说明用的是同一个自建应用）。
    如果 corpid 相同但 secret 不同，说明绑定了不同的自建应用，应独立部署。
    """
    if not wecom_corpid:
        return None
    for info in _registry.list_all().values():
        if info.platform != "wecom_kf":
            continue
        inst_tenants = INSTANCES_DIR / info.tenant_id / "tenants.json"
        if not inst_tenants.exists():
            continue
        try:
            data = json.loads(inst_tenants.read_text())
            tenants = data.get("tenants", [])
            for t in tenants:
                if (
                    t.get("wecom_corpid") == wecom_corpid
                    and t.get("wecom_kf_secret") == wecom_kf_secret
                ):
                    return info
        except Exception:
            continue
    return None


def _cohost_tenant(host_instance: InstanceInfo, new_tenant_config: dict) -> dict:
    """把新租户加入已有容器的 tenants.json 并重启。同步更新根 tenants.json。"""
    inst_dir = INSTANCES_DIR / host_instance.tenant_id
    tenants_file = inst_dir / "tenants.json"

    data = json.loads(tenants_file.read_text())
    tenants = data.get("tenants", [])

    # 去重
    existing_ids = {t["tenant_id"] for t in tenants}
    if new_tenant_config["tenant_id"] in existing_ids:
        return {
            "ok": False,
            "error": f"Tenant {new_tenant_config['tenant_id']} already in {host_instance.tenant_id} container",
        }

    tenants.append(new_tenant_config)
    tenants_file.write_text(
        json.dumps({"tenants": tenants}, indent=2, ensure_ascii=False)
    )

    # 同步到根 tenants.json（CI/CD source of truth）
    _sync_to_root_tenants(new_tenant_config)

    # 注册 kf dispatch
    kfid = new_tenant_config.get("wecom_kf_open_kfid", "")
    if kfid:
        _register_kf_dispatch(kfid, new_tenant_config["tenant_id"], host_instance.port)

    # 重启宿主容器使新配置生效
    _run([
        "docker", "compose",
        "-f", str(inst_dir / "docker-compose.yml"),
        "restart",
    ])

    logger.info(
        "Co-hosted %s with %s (port %d)",
        new_tenant_config["tenant_id"], host_instance.tenant_id, host_instance.port,
    )

    return {
        "ok": True,
        "tenant_id": new_tenant_config["tenant_id"],
        "co_hosted_with": host_instance.tenant_id,
        "port": host_instance.port,
        "container": host_instance.container_name,
        "webhook_path": f"/webhook/wecom_kf/{host_instance.tenant_id}",
        "status": "co-hosted (restarted)",
    }


def _sync_to_root_tenants(tenant_config: dict, remove: bool = False):
    """同步租户配置到根 tenants.json（CI/CD 部署时用）。"""
    root_file = _PROJECT_ROOT / "tenants.json"
    if not root_file.exists():
        return
    try:
        data = json.loads(root_file.read_text())
        tenants = data.get("tenants", [])
        tid = tenant_config.get("tenant_id", "")

        if remove:
            tenants = [t for t in tenants if t.get("tenant_id") != tid]
        else:
            # Update or append
            found = False
            for i, t in enumerate(tenants):
                if t.get("tenant_id") == tid:
                    tenants[i] = tenant_config
                    found = True
                    break
            if not found:
                tenants.append(tenant_config)

        data["tenants"] = tenants
        root_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        logger.info("Synced tenant %s to root tenants.json (remove=%s)", tid, remove)
    except Exception as e:
        logger.warning("Failed to sync to root tenants.json: %s", e)


# =====================================================================
#  Provision
# =====================================================================

def provision(
    tenant_id: str,
    name: str,
    platform: str,
    credentials: dict,
    *,
    llm_provider: str = "gemini",
    llm_api_key: str = "",
    llm_model: str = "gemini-3-flash-preview",
    llm_model_strong: str = "gemini-3.1-pro-preview",
    llm_system_prompt: str = "",
    custom_persona: bool = False,
    tools_enabled: list[str] | None = None,
    capability_modules: list[str] | None = None,
    env_file: str = "",
) -> dict:
    """创建新的租户实例。

    Args:
        tenant_id: 唯一标识（如 "customer-abc"）
        name: 显示名称
        platform: "feishu" | "wecom" | "wecom_kf"
        credentials: 平台凭证 dict
        llm_*: LLM 配置
        env_file: .env 路径（默认用项目根目录的 .env）

    Returns:
        {"ok": True, "port": ..., "webhook_path": ...} 或 {"ok": False, "error": ...}
    """
    # ── 校验 ──
    if not tenant_id or not tenant_id.replace("-", "").replace("_", "").isalnum():
        return {"ok": False, "error": "Invalid tenant_id: 只能用字母数字和 -/_"}

    if platform not in SUPPORTED_PLATFORMS:
        return {"ok": False, "error": f"Invalid platform: {platform}"}

    existing = _registry.get(tenant_id)
    if existing:
        return {
            "ok": False,
            "error": f"Tenant {tenant_id} already exists (port {existing.port})",
        }

    # ── 构建租户配置（提前构建，co-host 分支也需要）──
    tenant_config: dict = {
        "tenant_id": tenant_id,
        "name": name,
        "platform": platform,
        "llm_provider": llm_provider,
        "llm_api_key": llm_api_key or "${GEMINI_API_KEY}",
        "llm_model": llm_model,
        "llm_model_strong": llm_model_strong,
        "coding_model": "",
        "custom_persona": custom_persona,
        **credentials,
    }
    if llm_system_prompt:
        tenant_config["llm_system_prompt"] = llm_system_prompt
    if tools_enabled:
        tenant_config["tools_enabled"] = tools_enabled
    if capability_modules:
        tenant_config["capability_modules"] = capability_modules

    # ── 企微客服 co-host 检测 ──
    # 同 corpid + 同凭证的 KF 租户共用容器（共享回调 URL）。
    # 如果凭证不同（绑定了不同自建应用），走独立容器。
    if platform == "wecom_kf":
        cohost = _find_cohost_instance(
            credentials.get("wecom_corpid", ""),
            credentials.get("wecom_kf_secret", ""),
        )
        if cohost:
            return _cohost_tenant(cohost, tenant_config)

    # ── 分配端口 ──
    try:
        port = _registry.allocate_port()
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    # ── 创建实例目录 ──
    inst_dir = INSTANCES_DIR / tenant_id
    inst_dir.mkdir(parents=True, exist_ok=True)
    (inst_dir / "logs").mkdir(exist_ok=True)

    if not env_file:
        env_file = str(_PROJECT_ROOT / ".env")

    info = InstanceInfo(
        tenant_id=tenant_id,
        name=name,
        platform=platform,
        port=port,
    )

    # ── 写入配置文件 ──
    (inst_dir / "tenants.json").write_text(_generate_tenant_json(tenant_config))
    (inst_dir / "docker-compose.yml").write_text(_generate_compose(info, env_file))

    # ── 生成 nginx 配置 ──
    nginx_conf = _generate_nginx_conf(info)
    nginx_written = False
    try:
        NGINX_CONF_DIR.mkdir(parents=True, exist_ok=True)
        (NGINX_CONF_DIR / f"{tenant_id}.conf").write_text(nginx_conf)
        nginx_written = True
    except (PermissionError, OSError):
        # 无权限写 nginx 目录 → 保存到实例目录，让管理员手动拷贝
        (inst_dir / "nginx.conf").write_text(nginx_conf)
        logger.warning(
            "Cannot write to %s, saved to %s/nginx.conf instead",
            NGINX_CONF_DIR, inst_dir,
        )

    # ── 构建镜像 ──
    if not build_image():
        info.status = "error"
        _registry.register(info)
        return {"ok": False, "error": "Failed to build Docker image"}

    # ── 启动容器 ──
    result = _run([
        "docker", "compose",
        "-f", str(inst_dir / "docker-compose.yml"),
        "up", "-d",
    ])
    if result.returncode != 0:
        info.status = "error"
        _registry.register(info)
        return {"ok": False, "error": f"Container start failed: {result.stderr[:300]}"}

    info.status = "running"
    _registry.register(info)

    # ── 注册 kf 回调路由（同 corp 多客服账号分发）──
    if platform == "wecom_kf":
        kf_open_kfid = credentials.get("wecom_kf_open_kfid", "")
        if kf_open_kfid:
            _register_kf_dispatch(kf_open_kfid, tenant_id, port)

    # ── Reload nginx ──
    if nginx_written:
        reload = _run(["nginx", "-s", "reload"])
        if reload.returncode != 0:
            logger.warning("nginx reload failed: %s", reload.stderr[:200])

    # ── 健康检查 ──
    healthy = False
    for _ in range(10):
        time.sleep(2)
        try:
            import httpx
            resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=3)
            if resp.status_code == 200:
                healthy = True
                break
        except Exception:
            continue

    if not healthy:
        logger.warning("Health check failed for %s (port %d) after 20s", tenant_id, port)

    webhook_paths = {
        "feishu": f"/webhook/feishu/{tenant_id}/event",
        "wecom": f"/webhook/wecom/{tenant_id}",
        "wecom_kf": f"/webhook/wecom_kf/{tenant_id}",
        "qq": f"/webhook/qq/{tenant_id}",
    }

    return {
        "ok": True,
        "tenant_id": tenant_id,
        "port": port,
        "container": info.container_name,
        "healthy": healthy,
        "webhook_path": webhook_paths.get(platform, ""),
        "status": "running" if healthy else "starting",
    }


# =====================================================================
#  KF Dispatch (同 corp 多客服账号路由)
# =====================================================================

def _register_kf_dispatch(open_kfid: str, tenant_id: str, port: int):
    """注册 open_kfid → tenant 路由到 Redis（跨容器 kf 消息分发）。

    企微客服回调是 per-corp 的（一个 corp 只能配一个回调 URL），
    同 corp 下的多个客服账号共享同一回调。网关容器收到回调后，
    按 open_kfid 查此映射表转发到正确容器。
    """
    try:
        from app.services import redis_client as redis
        redis.execute(
            "SET", f"kf_dispatch:{open_kfid}",
            json.dumps({"tenant_id": tenant_id, "port": port}),
        )
        logger.info("Registered kf_dispatch: %s → %s:%d", open_kfid, tenant_id, port)
    except Exception as e:
        logger.warning("Failed to register kf_dispatch for %s: %s", open_kfid, e)


def _unregister_kf_dispatch(open_kfid: str):
    """移除 kf 回调路由"""
    try:
        from app.services import redis_client as redis
        redis.execute("DEL", f"kf_dispatch:{open_kfid}")
        logger.info("Unregistered kf_dispatch: %s", open_kfid)
    except Exception as e:
        logger.warning("Failed to unregister kf_dispatch for %s: %s", open_kfid, e)


# =====================================================================
#  Instance Management
# =====================================================================

_docker_available: bool | None = None  # None = not checked yet


def _auto_discover():
    """Auto-discover instances from Docker CLI or tenant registry + Redis.

    Strategy:
    1. Try Docker CLI (works on host, not inside containers)
    2. If Docker unavailable, build from tenant_registry (local tenants)
       + Redis admin:tenant:* (cross-container tenants)
    """
    global _docker_available

    # Try Docker once per process
    if _docker_available is None:
        _docker_available = _try_docker_discover()

    elif _docker_available:
        _try_docker_discover()  # Re-check for new containers

    # If Docker unavailable (inside container), build from tenant registry
    if not _docker_available:
        _build_from_tenant_registry()


def _try_docker_discover() -> bool:
    """Try to discover bot-* Docker containers. Returns True if Docker works."""
    try:
        result = _run([
            "docker", "ps", "-a",
            "--filter", "name=^bot-",
            "--format", "{{.Names}}\t{{.Ports}}",
        ])
        if result.returncode != 0:
            return False
        if not result.stdout.strip():
            return True  # Docker works, just no bot-* containers

        from app.tenant.registry import tenant_registry

        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split('\t')
            cname = parts[0].strip()
            if not cname.startswith("bot-"):
                continue
            tid = cname[4:]  # "bot-code-bot" → "code-bot"

            if _registry.get(tid):
                continue

            # Extract host port from "0.0.0.0:8101->8000/tcp"
            port = 0
            ports_str = parts[1].strip() if len(parts) > 1 else ""
            if ports_str:
                m = re.search(r':(\d+)->', ports_str)
                if m:
                    port = int(m.group(1))

            t = tenant_registry.get(tid)
            info = InstanceInfo(
                tenant_id=tid,
                name=t.name if t else tid,
                platform=t.platform if t else "unknown",
                port=port,
                container_name=cname,
            )
            _registry.register(info)
            logger.info("Auto-discovered from Docker: %s (port %d)", tid, port)
        return True
    except Exception:
        return False


def _build_from_tenant_registry():
    """Build instance entries from tenant_registry + Redis (when Docker unavailable).

    Groups wecom_kf tenants by shared corpid+kf_secret → first = primary instance.
    Also discovers cross-container tenants from Redis admin:tenant:* metadata.
    """
    from app.tenant.registry import tenant_registry

    # Collect all tenant metadata: local + Redis
    all_meta: dict[str, dict] = {}

    for tid, t in tenant_registry.all_tenants().items():
        all_meta[tid] = {
            "name": t.name,
            "platform": t.platform,
            "wecom_corpid": getattr(t, 'wecom_corpid', ''),
            "wecom_kf_secret": getattr(t, 'wecom_kf_secret', ''),
        }

    # Redis: other containers' tenants (admin:tenant:* metadata + tenant_cfg:* full config)
    try:
        from app.services import redis_client as redis_mod
        if redis_mod.available():
            # 1) Scan admin:tenant:* for basic metadata
            cursor = "0"
            remote_keys = []
            for _ in range(50):
                result = redis_mod.execute(
                    "SCAN", cursor, "MATCH", "admin:tenant:*", "COUNT", "50",
                )
                if not result or not isinstance(result, list) or len(result) < 2:
                    break
                cursor = str(result[0])
                keys = result[1] if isinstance(result[1], list) else []
                for key in keys:
                    tid = key.replace("admin:tenant:", "", 1) if isinstance(key, str) else ""
                    if tid and tid not in all_meta:
                        remote_keys.append((tid, key))
                if cursor == "0":
                    break

            if remote_keys:
                commands = [["GET", key] for _, key in remote_keys]
                responses = redis_mod.pipeline(commands)
                for (tid, _), data in zip(remote_keys, responses):
                    if data:
                        try:
                            meta = json.loads(data)
                            all_meta[tid] = {
                                "name": meta.get("name", tid),
                                "platform": meta.get("platform", "unknown"),
                                "wecom_corpid": "",
                                "wecom_kf_secret": "",
                            }
                        except (json.JSONDecodeError, TypeError):
                            pass

            # 2) Scan tenant_cfg:* for full config (has wecom credentials)
            #    Co-tenants added via dashboard are stored here with full config
            #    including wecom_corpid/kf_secret needed for co-host grouping.
            cursor = "0"
            cfg_keys: list[tuple[str, str]] = []
            for _ in range(50):
                result = redis_mod.execute(
                    "SCAN", cursor, "MATCH", "tenant_cfg:*", "COUNT", "50",
                )
                if not result or not isinstance(result, list) or len(result) < 2:
                    break
                cursor = str(result[0])
                keys = result[1] if isinstance(result[1], list) else []
                for key in keys:
                    tid = key.replace("tenant_cfg:", "", 1) if isinstance(key, str) else ""
                    if tid:
                        cfg_keys.append((tid, key))
                if cursor == "0":
                    break

            if cfg_keys:
                commands = [["GET", key] for _, key in cfg_keys]
                responses = redis_mod.pipeline(commands)
                for (tid, _), data in zip(cfg_keys, responses):
                    if data:
                        try:
                            cfg = json.loads(data)
                            corpid = cfg.get("wecom_corpid", "")
                            secret = cfg.get("wecom_kf_secret", "")
                            if tid not in all_meta:
                                # New tenant only in tenant_cfg
                                all_meta[tid] = {
                                    "name": cfg.get("name", tid),
                                    "platform": cfg.get("platform", "unknown"),
                                    "wecom_corpid": corpid,
                                    "wecom_kf_secret": secret,
                                }
                            elif corpid and secret:
                                # Enrich existing entry with credentials
                                all_meta[tid]["wecom_corpid"] = corpid
                                all_meta[tid]["wecom_kf_secret"] = secret
                        except (json.JSONDecodeError, TypeError):
                            pass
    except Exception:
        pass

    # Group wecom_kf tenants by (corpid, secret) → co-host under first tenant
    kf_groups: dict[tuple[str, str], list[str]] = {}
    kf_registered: dict[tuple[str, str], str] = {}  # credentials → already-registered tid
    standalone: list[str] = []

    for tid, meta in all_meta.items():
        is_kf = (meta["platform"] == "wecom_kf"
                 and meta["wecom_corpid"] and meta["wecom_kf_secret"])
        if _registry.get(tid):
            # Track credentials of already-registered KF instances
            if is_kf:
                key = (meta["wecom_corpid"], meta["wecom_kf_secret"])
                kf_registered.setdefault(key, tid)
            continue
        if is_kf:
            key = (meta["wecom_corpid"], meta["wecom_kf_secret"])
            kf_groups.setdefault(key, []).append(tid)
        else:
            standalone.append(tid)

    # Register standalone tenants (feishu/wecom/uncredentialed KF)
    for tid in standalone:
        meta = all_meta[tid]
        _registry.register(InstanceInfo(
            tenant_id=tid, name=meta["name"], platform=meta["platform"],
            port=0, container_name=f"bot-{tid}",
        ))
        logger.info("Registered instance from tenant registry: %s", tid)

    # Register primary of each KF group — skip if already covered by a registered instance
    for key, tids in kf_groups.items():
        if key in kf_registered:
            # These tenants will appear as co-tenants via _get_co_tenants_from_registry
            logger.info("KF tenants %s share credentials with registered instance %s, skipping",
                        tids, kf_registered[key])
            continue
        primary_tid = tids[0]
        meta = all_meta[primary_tid]
        _registry.register(InstanceInfo(
            tenant_id=primary_tid, name=meta["name"], platform=meta["platform"],
            port=0, container_name=f"bot-{primary_tid}",
        ))
        if len(tids) > 1:
            logger.info("Registered KF instance: %s (co-tenants: %s)",
                        primary_tid, tids[1:])


def _get_co_tenants_from_registry(primary_tid: str, platform: str) -> list[dict]:
    """Find co-tenants from local tenant_registry (fallback when instances dir missing)."""
    if platform != "wecom_kf":
        return []
    try:
        from app.tenant.registry import tenant_registry
        primary = tenant_registry.get(primary_tid)
        if not primary:
            return []
        corpid = getattr(primary, 'wecom_corpid', '')
        kf_secret = getattr(primary, 'wecom_kf_secret', '')
        if not corpid or not kf_secret:
            return []

        co_tenants = []
        for other_tid, other_t in tenant_registry.all_tenants().items():
            if (other_tid != primary_tid
                    and other_t.platform == "wecom_kf"
                    and getattr(other_t, 'wecom_corpid', '') == corpid
                    and getattr(other_t, 'wecom_kf_secret', '') == kf_secret):
                co_tenants.append({
                    "tenant_id": other_tid,
                    "name": other_t.name,
                    "wecom_kf_open_kfid": getattr(other_t, 'wecom_kf_open_kfid', ''),
                })
        return co_tenants
    except Exception:
        return []


def list_instances() -> list[dict]:
    """列出所有已供应实例（含实时容器状态 + co-hosted 租户）

    自动发现实例：Docker CLI（宿主机）或 tenant_registry + Redis（容器内）。
    """
    _auto_discover()

    result = []
    for tid, info in _registry.list_all().items():
        # Check container status (skip N docker calls if Docker unavailable)
        if _docker_available:
            check = _run(["docker", "inspect", "--format", "{{.State.Status}}", info.container_name])
            actual = check.stdout.strip() if check.returncode == 0 else "not_found"
        else:
            actual = "running"  # Assume running if we can't check

        entry = {
            "tenant_id": tid,
            "name": info.name,
            "platform": info.platform,
            "port": info.port,
            "status": actual,
            "container": info.container_name,
            "created_at": info.created_at,
            "co_tenants": [],
        }
        # Read co-hosted tenants from instance's tenants.json
        inst_tenants = INSTANCES_DIR / tid / "tenants.json"
        if inst_tenants.exists():
            try:
                data = json.loads(inst_tenants.read_text())
                tenants = data.get("tenants", [])
                for t in tenants:
                    if t.get("tenant_id") != tid:
                        entry["co_tenants"].append({
                            "tenant_id": t["tenant_id"],
                            "name": t.get("name", ""),
                            "wecom_kf_open_kfid": t.get("wecom_kf_open_kfid", ""),
                        })
            except Exception:
                pass
        else:
            # Fallback: detect co-tenants from local tenant registry
            entry["co_tenants"] = _get_co_tenants_from_registry(tid, info.platform)

        result.append(entry)

    _dedup_kf_instances(result)
    return result


def _dedup_kf_instances(instances: list[dict]):
    """Merge wecom_kf instances that share the same (corpid, kf_secret).

    Keeps the earliest-created as primary, moves others into co_tenants.
    Defensive post-processing — handles cases where _build_from_tenant_registry
    couldn't prevent duplicate registration (e.g. registry.json had them).
    """
    if not instances:
        return

    def _get_kf_creds(tid: str):
        """Look up (corpid, kf_secret) from tenant_registry or Redis."""
        try:
            from app.tenant.registry import tenant_registry
            t = tenant_registry.get(tid)
            if t:
                corpid = getattr(t, 'wecom_corpid', '')
                secret = getattr(t, 'wecom_kf_secret', '')
                if corpid and secret:
                    return (corpid, secret)
        except Exception:
            pass
        try:
            from app.services import redis_client as redis_mod
            if redis_mod.available():
                data = redis_mod.execute("GET", f"tenant_cfg:{tid}")
                if data:
                    cfg = json.loads(data)
                    corpid = cfg.get("wecom_corpid", "")
                    secret = cfg.get("wecom_kf_secret", "")
                    if corpid and secret:
                        return (corpid, secret)
        except Exception:
            pass
        return None

    # Collect credentials for each KF instance
    kf_creds: dict[int, tuple[str, str]] = {}
    for i, inst in enumerate(instances):
        if inst.get("platform") != "wecom_kf":
            continue
        cred = _get_kf_creds(inst["tenant_id"])
        if cred:
            kf_creds[i] = cred

    # Group by credentials
    groups: dict[tuple[str, str], list[int]] = {}
    for idx, cred in kf_creds.items():
        groups.setdefault(cred, []).append(idx)

    # Merge groups with 2+ instances
    remove_indices: set[int] = set()
    for _cred, indices in groups.items():
        if len(indices) < 2:
            continue
        # Keep earliest-created as primary
        indices.sort(key=lambda i: instances[i].get("created_at", 0) or 0)
        primary = instances[indices[0]]
        existing_ids = {primary["tenant_id"]}
        existing_ids.update(ct["tenant_id"] for ct in primary["co_tenants"])

        for merge_idx in indices[1:]:
            source = instances[merge_idx]
            # Add source instance itself as co-tenant
            if source["tenant_id"] not in existing_ids:
                t_info = {"tenant_id": source["tenant_id"],
                          "name": source.get("name", ""),
                          "wecom_kf_open_kfid": ""}
                # Try to get kfid
                try:
                    from app.tenant.registry import tenant_registry
                    t = tenant_registry.get(source["tenant_id"])
                    if t:
                        t_info["wecom_kf_open_kfid"] = getattr(t, 'wecom_kf_open_kfid', '')
                except Exception:
                    pass
                primary["co_tenants"].append(t_info)
                existing_ids.add(source["tenant_id"])
            # Move source's co-tenants to primary
            for ct in source.get("co_tenants", []):
                if ct["tenant_id"] not in existing_ids:
                    primary["co_tenants"].append(ct)
                    existing_ids.add(ct["tenant_id"])
            remove_indices.add(merge_idx)

    if remove_indices:
        logger.info("Merged %d duplicate KF instances by shared credentials", len(remove_indices))
        for idx in sorted(remove_indices, reverse=True):
            instances.pop(idx)


def list_co_tenants(instance_id: str) -> list[dict]:
    """列出某个实例下所有 co-hosted 租户的详细配置。"""
    info = _registry.get(instance_id)
    if not info:
        return []

    # Try instances/{tid}/tenants.json first (provisioned instances)
    inst_tenants = INSTANCES_DIR / instance_id / "tenants.json"
    if inst_tenants.exists():
        try:
            data = json.loads(inst_tenants.read_text())
            tenants = data.get("tenants", [])
            result = []
            for t in tenants:
                result.append({
                    "tenant_id": t.get("tenant_id", ""),
                    "name": t.get("name", ""),
                    "wecom_kf_open_kfid": t.get("wecom_kf_open_kfid", ""),
                    "trial_enabled": t.get("trial_enabled", False),
                    "trial_duration_hours": t.get("trial_duration_hours", 48),
                    "quota_user_tokens_6h": t.get("quota_user_tokens_6h", 0),
                    "is_primary": t.get("tenant_id") == instance_id,
                })
            return result
        except Exception:
            pass

    # Fallback: build from local tenant registry (auto-discovered instances)
    from app.tenant.registry import tenant_registry
    primary = tenant_registry.get(instance_id)
    if not primary:
        return [{"tenant_id": instance_id, "name": info.name,
                 "is_primary": True}]

    def _tenant_to_entry(tid, t, is_primary=False):
        return {
            "tenant_id": tid,
            "name": t.name,
            "wecom_kf_open_kfid": getattr(t, 'wecom_kf_open_kfid', ''),
            "trial_enabled": getattr(t, 'trial_enabled', False),
            "trial_duration_hours": getattr(t, 'trial_duration_hours', 48),
            "quota_user_tokens_6h": getattr(t, 'quota_user_tokens_6h', 0),
            "is_primary": is_primary,
        }

    result = [_tenant_to_entry(instance_id, primary, is_primary=True)]

    # Find co-tenants with same corpid + kf_secret
    if info.platform == "wecom_kf":
        corpid = getattr(primary, 'wecom_corpid', '')
        kf_secret = getattr(primary, 'wecom_kf_secret', '')
        if corpid and kf_secret:
            for other_tid, other_t in tenant_registry.all_tenants().items():
                if (other_tid != instance_id
                        and other_t.platform == "wecom_kf"
                        and getattr(other_t, 'wecom_corpid', '') == corpid
                        and getattr(other_t, 'wecom_kf_secret', '') == kf_secret):
                    result.append(_tenant_to_entry(other_tid, other_t))

    return result


def remove_co_tenant(instance_id: str, co_tenant_id: str) -> dict:
    """从实例的 tenants.json 中移除一个 co-hosted 租户。"""
    info = _registry.get(instance_id)
    if not info:
        return {"ok": False, "error": f"Instance {instance_id} not found"}
    if co_tenant_id == instance_id:
        return {"ok": False, "error": "Cannot remove primary tenant"}

    inst_dir = INSTANCES_DIR / instance_id
    tenants_file = inst_dir / "tenants.json"

    removed_kfid = ""

    if tenants_file.exists():
        # 文件存在：从 tenants.json 移除
        data = json.loads(tenants_file.read_text())
        tenants = data.get("tenants", [])
        original_len = len(tenants)

        new_tenants = []
        for t in data.get("tenants", []):
            if t.get("tenant_id") == co_tenant_id:
                removed_kfid = t.get("wecom_kf_open_kfid", "")
            else:
                new_tenants.append(t)

        if len(new_tenants) == original_len:
            return {"ok": False, "error": f"Tenant {co_tenant_id} not found in instance"}

        data["tenants"] = new_tenants
        tenants_file.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        # tenants.json 不存在（Redis-only 租户）：仍可通过 Redis 清理
        logger.warning("tenants.json not found for %s, removing via Redis only", instance_id)

    # Unregister kf dispatch
    if removed_kfid:
        _unregister_kf_dispatch(removed_kfid)

    # 清理 Redis 持久化的租户配置
    try:
        from app.services.redis_client import execute, available
        if available():
            execute("DEL", f"tenant_cfg:{co_tenant_id}")
            execute("DEL", f"admin:tenant:{co_tenant_id}")
            logger.info("Cleaned Redis keys for %s", co_tenant_id)
    except Exception:
        logger.debug("Redis cleanup for %s failed", co_tenant_id, exc_info=True)

    # Sync removal to root tenants.json
    _sync_to_root_tenants({"tenant_id": co_tenant_id}, remove=True)

    # Restart container (only if compose file exists)
    compose_file = inst_dir / "docker-compose.yml"
    if compose_file.exists():
        _run([
            "docker", "compose",
            "-f", str(compose_file),
            "restart",
        ])

    logger.info("Removed co-tenant %s from %s", co_tenant_id, instance_id)
    return {"ok": True, "tenant_id": co_tenant_id, "instance_id": instance_id}


def instance_status(tenant_id: str) -> dict:
    """查看实例详细状态"""
    info = _registry.get(tenant_id)
    if not info:
        return {"ok": False, "error": f"Instance {tenant_id} not found"}

    check = _run([
        "docker", "inspect", "--format",
        "{{.State.Status}}|{{.State.StartedAt}}",
        info.container_name,
    ])
    if check.returncode == 0:
        parts = check.stdout.strip().split("|")
        container_status = parts[0] if parts else "unknown"
        started_at = parts[1] if len(parts) > 1 else ""
    else:
        container_status = "not_found"
        started_at = ""

    logs_result = _run(["docker", "logs", "--tail", "20", info.container_name])
    recent_logs = logs_result.stdout[-1000:] if logs_result.returncode == 0 else ""

    return {
        "ok": True,
        "tenant_id": tenant_id,
        "name": info.name,
        "platform": info.platform,
        "port": info.port,
        "container": info.container_name,
        "container_status": container_status,
        "started_at": started_at,
        "recent_logs": recent_logs,
    }


def get_instance_logs(tenant_id: str, lines: int = 200, since: str = "",
                      grep: str = "", level: str = "") -> dict:
    """获取实例日志

    三层获取策略：
    1. 读本地日志文件（instances/{tid}/logs/bot.log）— 宿主机上或挂载了 instances 目录时可用
    2. HTTP 代理到目标容器的 /admin/api/logs — 跨容器获取，容器内无日志文件时使用
    3. docker logs CLI — 宿主机上直接运行时可用

    Args:
        lines: 返回最近 N 行（默认 200，最大 2000）
        since: Docker --since 参数（如 "1h", "30m", "2024-03-08T10:00:00"）
        grep: 日志内容过滤关键词
        level: 日志级别过滤（ERROR, WARNING, INFO 等）
    """
    info = _registry.get(tenant_id)
    if not info:
        return {"ok": False, "error": f"Instance {tenant_id} not found"}

    lines = min(max(lines, 10), 2000)
    log_lines = None
    log_source = "none"
    log_meta: dict = {}
    errors_tried = []

    # 方案1（优先）: HTTP 代理到目标容器的 /admin/api/logs
    # 每个容器都能可靠读自己的 /app/logs/bot.log，这是最可靠的跨容器方案
    http_result = _fetch_logs_via_http(info.port, lines, since, grep, level)
    if http_result is not None:
        logger.debug("get_instance_logs(%s): got logs via HTTP proxy (port %d)",
                     tenant_id, info.port)
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "container": info.container_name,
            "log_source": http_result.get("log_source", f"http:port-{info.port}"),
            "buffer_size": http_result.get("buffer_size", -1),
            "log_meta": http_result.get("log_meta", {}),
            **{k: v for k, v in http_result.items()
               if k not in ("log_meta", "log_source", "buffer_size")},
        }
    errors_tried.append("HTTP proxy failed (ADMIN_TOKEN missing or target unreachable)")

    # 方案2: 读本地日志文件（宿主机上或挂载了 instances volume 时可用）
    log_file = INSTANCES_DIR / tenant_id / "logs" / "bot.log"
    if log_file.exists():
        try:
            fd = os.open(str(log_file), os.O_RDONLY)
            try:
                raw_bytes = os.read(fd, 50 * 1024 * 1024)
            finally:
                os.close(fd)
            text = raw_bytes.decode("utf-8", errors="replace").strip()
            all_lines = text.split("\n")
            log_lines = all_lines[-lines:]
            log_source = str(log_file)
            st = log_file.stat()
            log_meta = {
                "file": str(log_file),
                "size_bytes": st.st_size,
                "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
            }
            logger.debug("get_instance_logs(%s): read %d lines from file %s",
                         tenant_id, len(log_lines), log_file)
        except Exception as e:
            logger.warning("Failed to read log file %s: %s", log_file, e)
            errors_tried.append(f"file read error: {e}")
    else:
        errors_tried.append(f"file not found: {log_file}")
        logger.debug("get_instance_logs(%s): log file not found at %s", tenant_id, log_file)

    # 方案3: docker logs CLI（宿主机上直接运行时可用）
    if log_lines is None:
        cmd = ["docker", "logs", "--tail", str(lines), "--timestamps"]
        if since:
            cmd.extend(["--since", since])
        cmd.append(info.container_name)
        try:
            result = _run(cmd, timeout=15)
            if result.returncode == 0:
                raw = (result.stdout or "") + (result.stderr or "")
                log_lines = raw.strip().split("\n") if raw.strip() else []
                log_source = f"docker-cli:{info.container_name}"
            else:
                errors_tried.append(f"docker logs exit {result.returncode}: {result.stderr[:100]}")
                logger.warning("docker logs failed for %s: %s",
                               info.container_name, result.stderr[:200])
        except Exception as e:
            errors_tried.append(f"docker logs error: {e}")
            logger.warning("docker logs error for %s: %s", info.container_name, e)

    if log_lines is None:
        return {
            "ok": False,
            "error": f"No log source available. Tried: {'; '.join(errors_tried)}",
        }

    # Filter by level (方案1和3需要本地过滤)
    if level:
        level_upper = level.upper()
        log_lines = [l for l in log_lines if level_upper in l]

    # Filter by keyword
    if grep:
        grep_lower = grep.lower()
        log_lines = [l for l in log_lines if grep_lower in l.lower()]

    # Separate errors for alerting
    error_lines = [l for l in log_lines if "ERROR" in l or "CRITICAL" in l
                   or "Traceback" in l or "Exception" in l]

    return {
        "ok": True,
        "tenant_id": tenant_id,
        "container": info.container_name,
        "log_source": log_source,
        "buffer_size": -1,  # file/docker fallback, no buffer
        "log_meta": log_meta,
        "errors_tried": errors_tried,
        "total_lines": len(log_lines),
        "logs": "\n".join(log_lines),
        "error_count": len(error_lines),
        "recent_errors": "\n".join(error_lines[-20:]) if error_lines else "",
    }


def _fetch_logs_via_http(port: int, lines: int, since: str = "",
                         grep: str = "", level: str = "") -> dict | None:
    """通过 HTTP 调用目标容器的 /admin/api/logs 获取日志。

    每个容器都部署了 LOG_BUFFER 内存缓冲区，通过 HTTP 代理获取的是实时日志。
    尝试 127.0.0.1（host 网络）和 host.docker.internal（bridge 网络）。

    返回 dict（含 total_lines/logs/error_count/recent_errors/buffer_size）或 None。
    远端已做 level/grep 过滤，调用方无需再过滤。
    """
    import httpx

    admin_token = os.getenv("ADMIN_TOKEN", "").strip()
    if not admin_token:
        logger.warning("_fetch_logs_via_http: ADMIN_TOKEN not set, cannot proxy")
        return None

    params = {"lines": lines, "_t": int(time.time())}  # cache-bust
    if since:
        params["since"] = since
    if grep:
        params["grep"] = grep
    if level:
        params["level"] = level
    headers = {"Authorization": f"Bearer {admin_token}"}

    # 尝试两个地址：host 网络用 127.0.0.1，bridge 网络用 host.docker.internal
    for host in ("127.0.0.1", "host.docker.internal"):
        url = f"http://{host}:{port}/admin/api/logs"
        try:
            with httpx.Client(timeout=httpx.Timeout(connect=3.0, read=10.0),
                              trust_env=False) as client:
                resp = client.get(url, params=params, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        return {
                            "total_lines": data.get("total_lines", 0),
                            "logs": data.get("logs", ""),
                            "error_count": data.get("error_count", 0),
                            "recent_errors": data.get("recent_errors", ""),
                            "log_source": data.get("log_source", f"http:{host}"),
                            "buffer_size": data.get("buffer_size", -1),
                            "log_meta": data.get("log_meta", {}),
                        }
                else:
                    logger.warning("HTTP log fetch %s returned %d: %s",
                                   url, resp.status_code, resp.text[:200])
        except Exception as e:
            logger.debug("HTTP log fetch %s failed: %s", url, e)
            continue

    return None


def restart_instance(tenant_id: str) -> dict:
    """重启实例"""
    info = _registry.get(tenant_id)
    if not info:
        return {"ok": False, "error": f"Instance {tenant_id} not found"}

    compose_file = INSTANCES_DIR / tenant_id / "docker-compose.yml"
    if compose_file.exists():
        result = _run(["docker", "compose", "-f", str(compose_file), "restart"])
    else:
        # Auto-discovered instance: direct docker restart
        result = _run(["docker", "restart", info.container_name])

    if result.returncode != 0:
        return {"ok": False, "error": f"Restart failed: {result.stderr[:300]}"}

    info.status = "running"
    info.updated_at = time.time()
    _registry.register(info)
    return {"ok": True, "tenant_id": tenant_id, "message": "Restarted successfully"}


def stop_instance(tenant_id: str) -> dict:
    """停止实例"""
    info = _registry.get(tenant_id)
    if not info:
        return {"ok": False, "error": f"Instance {tenant_id} not found"}

    compose_file = INSTANCES_DIR / tenant_id / "docker-compose.yml"
    if compose_file.exists():
        result = _run(["docker", "compose", "-f", str(compose_file), "down"])
    else:
        # Auto-discovered instance: direct docker stop
        result = _run(["docker", "stop", info.container_name])

    if result.returncode != 0:
        return {"ok": False, "error": f"Stop failed: {result.stderr[:300]}"}

    info.status = "stopped"
    info.updated_at = time.time()
    _registry.register(info)
    return {"ok": True, "tenant_id": tenant_id, "message": "Stopped successfully"}


def destroy_instance(tenant_id: str) -> dict:
    """销毁实例（停止容器 + 删除所有文件）"""
    info = _registry.get(tenant_id)
    if not info:
        return {"ok": False, "error": f"Instance {tenant_id} not found"}

    inst_dir = INSTANCES_DIR / tenant_id

    # 清理 kf 回调路由
    tenants_file = inst_dir / "tenants.json"
    if tenants_file.exists():
        try:
            tdata = json.loads(tenants_file.read_text())
            tenants_list = tdata.get("tenants", [tdata]) if isinstance(tdata, dict) else tdata
            if tenants_list:
                kfid = tenants_list[0].get("wecom_kf_open_kfid", "")
                if kfid:
                    _unregister_kf_dispatch(kfid)
        except Exception:
            pass

    # 停容器 + 删 volume
    _run([
        "docker", "compose",
        "-f", str(inst_dir / "docker-compose.yml"),
        "down", "-v",
    ])

    # 删 nginx 配置
    nginx_file = NGINX_CONF_DIR / f"{tenant_id}.conf"
    if nginx_file.exists():
        try:
            nginx_file.unlink()
            _run(["nginx", "-s", "reload"])
        except OSError:
            logger.warning("Failed to remove nginx config %s", nginx_file)

    # 删实例目录
    if inst_dir.exists():
        shutil.rmtree(inst_dir)

    _registry.remove(tenant_id)
    return {"ok": True, "tenant_id": tenant_id, "message": "Destroyed successfully"}


def update_instance_config(tenant_id: str, updates: dict) -> dict:
    """更新租户配置并重启"""
    info = _registry.get(tenant_id)
    if not info:
        return {"ok": False, "error": f"Instance {tenant_id} not found"}

    inst_dir = INSTANCES_DIR / tenant_id
    tenants_file = inst_dir / "tenants.json"

    if not tenants_file.exists():
        return {"ok": False, "error": f"tenants.json not found for {tenant_id}"}

    data = json.loads(tenants_file.read_text())
    # Handle both formats: {"tenants": [...]} and bare [...]
    if isinstance(data, dict) and "tenants" in data:
        current = data["tenants"][0] if data["tenants"] else {}
    elif isinstance(data, list) and data:
        current = data[0]
    else:
        current = {}

    current.update(updates)
    tenants_file.write_text(_generate_tenant_json(current))

    return restart_instance(tenant_id)
