#!/usr/bin/env python3
"""同步 per-container tenants.json — 从根 tenants.json 生成每个容器的配置。

核心逻辑：
- 普通租户（feishu/wecom）：1 个容器 = 1 个 tenant
- 企微客服（wecom_kf）：同 corpid + 同 kf_secret 的 KF 租户打包到一个容器
  （共用同一个自建应用 = 共用回调 URL，必须 co-host）
  不同 kf_secret = 绑定了不同自建应用，各自独立容器

用法：
  python3 scripts/sync_instance_configs.py [--dry-run]

CI/CD 调用：deploy.yml 在重启容器前执行此脚本。
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TENANTS_FILE = PROJECT_ROOT / "tenants.json"
INSTANCES_DIR = Path(sys.argv[1]) if len(sys.argv) > 2 else PROJECT_ROOT / "instances"

DRY_RUN = "--dry-run" in sys.argv


def main():
    if not TENANTS_FILE.exists():
        print("ERROR: %s not found" % TENANTS_FILE)
        sys.exit(1)

    if not INSTANCES_DIR.exists():
        print("WARN: %s not found, nothing to sync" % INSTANCES_DIR)
        return

    all_tenants = json.loads(TENANTS_FILE.read_text(encoding="utf-8"))["tenants"]

    # 按 (corpid, kf_secret) 分组 wecom_kf 租户
    # 同 corpid + 同 secret = 同一个自建应用 = 共用回调 = 必须 co-host
    kf_groups = {}  # (corpid, kf_secret) -> [tenant_config, ...]
    for t in all_tenants:
        if t.get("platform") == "wecom_kf":
            key = (t.get("wecom_corpid", ""), t.get("wecom_kf_secret", ""))
            if key[0]:
                kf_groups.setdefault(key, []).append(t)

    synced = 0
    for inst_dir in sorted(INSTANCES_DIR.iterdir()):
        if not inst_dir.is_dir():
            continue
        if not (inst_dir / "docker-compose.yml").exists():
            continue

        tid = inst_dir.name
        primary = next((t for t in all_tenants if t["tenant_id"] == tid), None)
        if not primary:
            print("  SKIP %s: not found in root tenants.json" % tid)
            continue

        # 决定这个容器装哪些租户
        if primary.get("platform") == "wecom_kf":
            key = (primary.get("wecom_corpid", ""), primary.get("wecom_kf_secret", ""))
            container_tenants = kf_groups.get(key, [primary])
        else:
            container_tenants = [primary]

        tenant_ids = [t["tenant_id"] for t in container_tenants]
        config_json = json.dumps(
            {"tenants": container_tenants},
            indent=2,
            ensure_ascii=False,
        )

        target = inst_dir / "tenants.json"
        if DRY_RUN:
            print("  DRY-RUN %s: would write %d tenant(s) %s" % (tid, len(container_tenants), tenant_ids))
        else:
            target.write_text(config_json, encoding="utf-8")
            print("  SYNCED %s: %d tenant(s) %s" % (tid, len(container_tenants), tenant_ids))

        synced += 1

    print("\nDone: synced %d instance(s)" % synced)


if __name__ == "__main__":
    main()
