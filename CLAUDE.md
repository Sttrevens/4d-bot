# Project: 4DGames Feishu Code Bot

## Overview
Python 3.12 + FastAPI 多租户 AI Bot 平台，支持飞书/企业微信/微信客服三平台接入。
核心 LLM: Gemini (Function Calling Agent)，默认 gemini-3-flash-preview，复杂任务自动升级 gemini-3.1-pro-preview。
40+ 工具，支持多模态（图片/语音/视频/文件）。
部署环境: 阿里云 ECS + Docker，GitHub Actions CI/CD。

## Architecture

```
app/
├── main.py              # FastAPI 入口
├── config.py            # 全局配置
├── channels/            # 平台接入层（飞书 channel）
├── router/              # 路由（意图分类 + LLM provider 路由）
├── webhook/             # 各平台 webhook handler + 加解密
├── tenant/              # 多租户系统
│   ├── config.py        # 租户配置模型
│   ├── context.py       # contextvars 租户上下文
│   └── registry.py      # 租户注册表
├── services/            # 核心服务
│   ├── kimi.py          # OpenAI 兼容 API 辅助（意图分类等）
│   ├── gemini_provider.py # Gemini 主 agent（flash + pro 自动升级）
│   ├── anthropic_coder.py # Anthropic 编码
│   ├── auto_fix.py      # 自我修复引擎（运行时错误→LLM诊断→自动修→部署→回滚，allowlist 边界限制只改 tools/+knowledge/）
│   ├── memory.py        # 三层记忆（工作记忆+事件日志+语义记忆）
│   ├── memory_store.py  # Upstash Redis 持久化
│   ├── history.py       # 对话历史
│   ├── feishu.py        # 飞书 API 封装
│   ├── wecom.py         # 企微消息
│   ├── wecom_kf.py      # 企微客服（用户识别+历史持久化）
│   ├── wecom_crypto.py  # 企微加解密
│   ├── media_processor.py # 多模态处理
│   ├── planner.py       # 任务规划
│   ├── scheduler.py     # 定时任务
│   ├── redis_client.py  # Redis 客户端
│   └── tenant_sync.py   # 跨容器租户配置同步（Redis 持久化 + 消息队列）
├── tools/               # 40+ Function Calling 工具
│   ├── feishu_api.py    # 飞书开放平台 API
│   ├── bitable_ops.py   # 多维表格
│   ├── doc_ops.py       # 文档操作
│   ├── calendar_ops.py  # 日历
│   ├── git_ops.py       # Git 操作
│   ├── github_ops.py    # GitHub API
│   ├── self_ops.py      # 自改代码（核心自我修复工具）
│   ├── sandbox.py       # 代码沙箱执行
│   ├── web_search.py    # 联网搜索
│   ├── server_ops.py    # 服务器操作
│   ├── task_ops.py      # 任务管理
│   ├── memory_ops.py    # 记忆操作
│   ├── provision_ops.py # 租户实例管理（Phase 2 Control Plane）
│   ├── env_ops.py       # 动态包管理（pip install + 沙箱白名单）
│   ├── browser_ops.py   # 浏览器自动化（Playwright + Gemini 视觉）
│   └── capability_ops.py # 能力获取元工具（评估/基础设施变更/人工引导）
├── services/
│   └── provisioner.py   # 核心供应逻辑（Docker 容器 + Nginx 路由）
├── scripts/
│   └── tenant_ctl.py    # CLI 实例管理工具
├── templates/
│   └── nginx-site.conf  # Nginx 反代主站模板
└── cloudflare-worker/   # CF Worker 代理
    ├── gemini-proxy.js  # Gemini API 代理（绕墙）
    └── ddg-search-proxy.js # DuckDuckGo 搜索代理
```

## 文件导出能力（File Export）

`export_file` 工具支持格式: **CSV, TXT, Markdown, JSON, PDF, HTML, XLSX**

### XLSX（Excel）导出
- 依赖 `openpyxl`（requirements.txt）
- 推荐传入 JSON 数组: `[{"列A": "值1", "列B": "值2"}, ...]`
- 也支持 CSV 文本（自动解析）
- 自动生成: 表头样式（蓝底白字）、斑马纹、冻结首行、自适应列宽
- 生成失败时自动降级为 CSV
- 适用场景: 报价单、设备清单、巡检报告、数据导出 — 客户零门槛打开

## 能力模组系统（Capability Modules）

### 模组注册表
- 位置: `app/knowledge/modules/registry.json`
- 包含每个模组的 name、label（中文名）、description、category、recommended_tools、platforms
- Dashboard 和 `list_capability_modules` 工具都读取此文件

### Dashboard 模组选择器
- 创建新 bot 实例时，表单包含 **Capability Modules** 区域
- 从 `/admin/api/module-registry` 加载注册表，展示 checkbox 列表
- 选中的模组自动传入 provision API 的 `capability_modules` 参数

### Steven AI 开通流程改进
- `provision_tenant` 工具描述中引导 bot 先调用 `list_capability_modules` 查看可用模组
- 模组列表展示 category 分类标签和中文名

## 工具调用质量反馈（Tool Call Quality）

### ToolResult.retry_hint
- `ToolResult` 新增 `retry_hint: str` 字段
- 工具调用失败时，`retry_hint` 给 LLM 结构化的重试建议
- `__str__()` 自动在失败结果后附加 `💡 建议: {hint}`
- 已在 `file_export`、`provision_ops` 等高频误调用场景添加 retry_hint

### 自定义工具 Schema 验证
- `create_custom_tool` 新增 `_validate_input_schema()` 验证
- 检查: 顶层 type 必须是 "object"、properties 格式、参数 type 合法性、required 一致性、description 必填
- 验证失败时返回 `invalid_param` + `retry_hint` 指导正确格式

## ⚠️ Adding New Tools — MANDATORY Checklist

**每次新增工具必须完成以下所有步骤，否则工具对部分租户不可用。**

这个 checklist 是因为多次踩坑总结的（search_social_media、plan tools 等都因遗漏白名单导致生产环境不可用）。

### Step 1: 创建工具文件
- 在 `app/tools/` 创建/修改工具文件
- 导出 `TOOL_DEFINITIONS`（schema 列表）和 `TOOL_MAP`（name→handler 映射）

### Step 2: 注册到 base_agent.py（3 处）
```python
# 1) Import（Lines ~27-135）
from app.tools.your_tool import (
    TOOL_DEFINITIONS as YOUR_TOOLS,
    TOOL_MAP as YOUR_TOOL_MAP,
)

# 2) 加入 ALL_TOOL_MAP（Lines ~170-189）
ALL_TOOL_MAP = {
    ...
    **YOUR_TOOL_MAP,
}

# 3) 加入 _ALL_TOOL_DEFS（Lines ~216-234）
_ALL_TOOL_DEFS = (
    ...
    + YOUR_TOOLS
)
```

### Step 3: 更新工具分类常量（按需）
在 `base_agent.py` 中检查是否需要加入以下分类：

| 常量 | 含义 | 何时需要 |
|---|---|---|
| `_SELF_ITERATION_TOOLS` | 自我修复工具 | 工具涉及代码修改/服务器操作 |
| `_INSTANCE_MGMT_TOOLS` | 实例管理工具 | 工具涉及租户容器管理 |
| `_FEISHU_ONLY_TOOLS` | 飞书专属工具 | 工具依赖飞书 API（日历/文档/任务等） |
| `_WECOM_ONLY_TOOLS` | 企微专属工具 | 工具仅企微平台需要 |
| `_RESEARCH_TOOLS` | 调研类工具 | 工具会被反复调用搜索（提高 stall 阈值到 7） |
| `_CUSTOM_TOOL_META_NAMES` | 需注入 tenant_id | 工具需要知道当前租户（自定义工具/技能） |

### Step 4: ⚠️ 更新 tenants.json 白名单（最容易遗漏！）
**所有使用 `tools_enabled` 白名单的租户都必须手动添加新工具名！**

```
tools_enabled: []      → 全部启用，无需操作
tools_enabled: [...]   → 必须手动添加新工具名，否则该租户看不到这个工具
```

当前使用白名单的租户：
- `kf-steven-ai` — 企微客服 AI 分身（工具最多，约 50+）
- `kf-leadgen-demo` — 社媒调研获客 bot（精简工具集）
- 未来新增的白名单租户

**检查方法：** `grep -c '"tools_enabled"' tenants.json` — 找到所有有白名单的租户，逐个确认。

### Step 5: 验证
```bash
python -m pytest tests/ -v  # 确保测试通过
# 在本地启动后检查工具是否出现在 LLM 的工具列表中
```

## Multi-Tenant System
- `tenants.json` 配置多个独立 bot 实例（凭证/仓库/LLM/工具集隔离）
- `contextvars` 实现请求级租户上下文切换
- **Phase 1 已部署：** 3 个 bot 各自运行在独立 Docker 容器中（code-bot:8101, pm-bot:8102, kf-steven-ai:8103），进程级隔离已实现

## Known Architecture Issues (SaaS 化阻碍)

### 核心问题: ~~单进程 + 共享代码仓库服务多租户~~ Phase 1 容器隔离已解决大部分
1. **~~代码共享~~** — ~~self_write_file 写同一份代码，租户间会冲突~~ 各容器有独立 workspace，self_write_file 互不影响 ✅
2. **~~部署全局~~** — ~~代码直推 main 触发全局重部署，所有租户中断~~ CI/CD 自动逐个重启容器（deploy.yml:86-95），仍有短暂中断但不互相阻塞 ✅
3. **~~无计算隔离~~** — ~~bash/sandbox 在同一 OS，租户A能看到租户B的数据~~ Docker 容器进程隔离 ✅（注：仍共享宿主机网络 host mode）
4. **状态混串** — ~~`_user_locks`/`_user_modes` 等全局 dict 只按 sender_id 做 key，无 tenant 前缀~~ 三个 handler（feishu/wecom/wecom_kf）均已加 `_tuk()` 函数做 `tenant_id:sender_id` 隔离 ✅
5. **auto-fix 门禁** — ~~`self_iteration_enabled` 靠 bool flag + contextvars，async create_task 边界可能丢失 context~~ 已加 allowlist 边界（只能写 `app/tools/` 和 `app/knowledge/`），基础设施层硬拦截 ✅
6. **并发控制** — ~~`_fix_in_progress` 是普通 bool 不是 asyncio.Lock，存在 TOCTOU 竞争~~ 已改为 `asyncio.Lock` ✅

**Phase 1 后残留问题：**
- 共享宿主机网络（host mode）— 端口需手动分配不冲突
- 共享 Docker 镜像 — 代码更新是全局的，无法 per-tenant 版本控制
- 原始容器 `4dgames-feishu-code-bot-bot-1` 可能仍在运行（遗留，应清理）
- 无健康监控/告警 — 容器挂了无人知道
- 无日志持久化 — 容器重建日志丢失

### SaaS 化路线图
- **Phase 1 (已完成)**: Per-tenant instance — **已部署生产环境** ✅ 3 个 bot 各自运行在独立 Docker 容器（code-bot:8101, pm-bot:8102, kf-steven-ai:8103）
- **Phase 1.5 (当前)**: 运维加固 — 健康监控/告警、日志持久化、清理遗留容器、容器自动重启策略
- **Phase 2 (中期)**: Control Plane — ~~管理面板统一监控/更新/回滚所有客户实例~~ **bot tool 已完成** ✅，需部署 Admin Dashboard UI
- **Phase 3 (远期)**: 共享引擎 + 插件沙箱 — 核心不可改，租户自定义逻辑在隔离沙箱跑

### Provisioning System (Phase 1 + 2) — 实施状态

**Phase 1 — 已部署生产环境 ✅**

当前运行状态（`docker ps`）：
| 容器名 | 租户 | 端口映射 | 说明 |
|---|---|---|---|
| `bot-code-bot` | code-bot (高梦) | 8101→8000 | 飞书 Code Bot |
| `bot-pm-bot` | pm-bot (耀西) | 8102→8000 | 飞书 PM Bot |
| `bot-kf-steven-ai` | kf-steven-ai | 8103→8000 | 企微客服 AI 分身 |
| `4dgames-feishu-code-bot-bot-1` | (遗留) | host network | 原始容器，应清理 |

**代码实现：**
- `app/services/provisioner.py` — 核心供应逻辑（创建/管理/销毁租户容器）
- `app/tools/provision_ops.py` — 5 个 bot tool（provision_tenant, list_instances, get_instance_status, restart_instance, destroy_instance）
- `app/tenant/config.py` — 新增 `instance_management_enabled` 权限字段
- `app/services/kimi_coder.py` — 工具注册 + `_INSTANCE_MGMT_TOOLS` 权限过滤
- `scripts/tenant_ctl.py` — CLI 管理工具（服务器上直接操作）
- `templates/nginx-site.conf` — Nginx 主站反代模板

**架构设计：**
- 所有容器复用同一 Docker 镜像（`feishu-code-bot:latest`）
- 每个租户容器有独立端口（8101-8199）、tenants.json、workspace/logs volume
- Nginx 按路径路由到对应容器（`/webhook/{platform}/{tenant_id}` + `/oauth/{tenant_id}/callback` → `127.0.0.1:{port}`）
- 实例注册表：`instances/registry.json`（gitignored，只存在于服务器）
- 每个实例目录：`instances/{tenant_id}/`（docker-compose.yml + tenants.json + logs/）
- CI/CD（deploy.yml）推送到 main 时自动重建镜像 + 逐个重启所有 provisioned 容器

**给 bot 启用实例管理：**
在 tenants.json 对应租户配置中添加 `"instance_management_enabled": true`，
该租户的 bot 就能使用 provision_tenant 等 5 个工具为客户自助开通实例。
（当前 kf-steven-ai 已启用）

### Auto-Fix 边界策略（allowlist）

**设计哲学：auto-fix 只能修改「应用层」，「基础设施层」必须人工迭代。**

采用 allowlist（白名单）而非 blocklist，fail-closed：新文件默认受保护。

| 层 | 路径 | auto-fix 权限 |
|---|---|---|
| 应用层 | `app/tools/` | ✅ 可读可写 |
| 应用层 | `app/knowledge/` | ✅ 可读可写 |
| 基础设施层 | `app/services/`、`app/main.py`、`app/config.py` | 🔒 只读 |
| 基础设施层 | `app/tenant/`、`app/channels/`、`app/router/`、`app/webhook/` | 🔒 只读 |
| 基础设施层 | `scripts/`、`templates/`、`.github/`、`Dockerfile` | 🔒 只读 |

**实现位置：** `auto_fix.py` → `_ALLOWED_WRITE_PATHS` + `_execute_tool()` 硬拦截

**当 bug 在基础设施层时：** auto-fix 会诊断问题并在修复报告中描述根因 + 建议方案，通知管理员人工处理。

### 自我修复部署流程

**直推 main**（不再使用 staging 分支）：
1. `self_edit_file` / `self_write_file` → 直接 commit 到 main
2. 推送到 main 后 GitHub Actions CI/CD 自动部署
3. `self_safe_deploy` → 记录回滚点 + 保存任务上下文到 Redis
4. 重启后 `_recover_missed_messages` → 检测 `bot:pending_resume` → 自动重新投递原始用户任务

**重启后任务继续（pending_resume 机制）：**
- `self_safe_deploy` 时保存 in-flight 请求到 Redis `bot:pending_resume`
- 重启后自动读取，以"系统消息：你刚修改了代码…继续完成用户任务"方式重新投递
- 与 `bot:in_flight` 互斥：已通过 resume 恢复的消息不会再通知用户"请重发"

### 消息路由（Message Routing）

**当前策略：全部走 Gemini。** K2.5 在 30+ 工具场景下 function calling 不可靠，已全面切换 Gemini。

```
消息进入 route_message()
    ↓
tenant.coding_model 非空？（当前所有租户均为空 → 走 Gemini）
    ├─ 是 → 有图片/视频？
    │       ├─ 有 → Gemini flash+pro（多模态主 provider）
    │       └─ 无 → coding_model 指定的模型
    └─ 否 → 直接走主 provider（Gemini）← 当前所有租户走这条路
```

**如需启用备选模型路由：** 在 tenants.json 中设置（默认为空 = 不启用）：
```json
{
  "coding_model": "kimi-k2.5",
  "coding_api_key": "",
  "coding_base_url": ""
}
```
- `coding_model` 默认值为 `""`（空 = 全走 Gemini），不再默认 K2.5
- 多模态消息（含图片/视频/音频）始终走 Gemini

**实现位置：** `router/intent.py`（路由层）+ `kimi_coder.py`（model_override 参数）

### 计量/计费系统（Metering）

**目的：** 记录每个租户的 LLM 用量，支持月度配额限制和用量查询。

**实现：**
- `app/services/metering.py` — 核心计量模块
  - `record_usage(UsageRecord)` — 记录单次 LLM 调用（token 数、工具调用、耗时等）
  - `check_quota(tenant_id)` — 月度配额检查（API 调用次数 + token 用量）
  - `get_usage_summary(tenant_id, month)` — 月度用量摘要
  - `get_daily_breakdown(tenant_id, month)` — 每日用量明细
- `app/router/intent.py` — 在 `route_message()` 入口做前置配额检查 + 试用期检查 + 后置用量记录
- `app/admin/routes.py` — 管理接口 `GET /admin/api/usage[/{tenant_id}]`（需 ADMIN_TOKEN 认证）

**Redis 数据结构：**
```
meter:{tenant_id}:{YYYY-MM}              → HASH {input_tokens, output_tokens, api_calls, tool_calls, rounds}
meter:{tenant_id}:{YYYY-MM}:daily:{DD}   → HASH {input_tokens, output_tokens, api_calls, tool_calls}
meter:{tenant_id}:user:{sender_id}:{YYYY-MM} → HASH {api_calls, input_tokens}
```

**配额配置：** 在 tenants.json 中设置：
```json
{
  "quota_monthly_api_calls": 1000,     // 0 = 无限制
  "quota_monthly_tokens": 5000000,     // 0 = 无限制
  "rate_limit_rpm": 120,               // 0 = 默认 60/分钟
  "rate_limit_user_rpm": 20            // 0 = 默认 10/分钟
}
```

**设计原则：** fail-open — Redis 不可用时放行，不阻塞业务。

### 限流系统（Rate Limiting）

**实现：** `app/services/rate_limiter.py` — Redis sorted set 滑动窗口
- 三层限流：全局并发（已有 semaphore）→ per-tenant RPM → per-user RPM
- 集成在 `router/intent.py` 的 `route_message()` 入口
- 超限时直接返回友好提示，不调用 LLM

### 试用期系统（Trial）

**目的：** 新用户首次消息自动进入试用期，到期后需管理员手动审批才能继续使用。

**实现：**
- `app/services/trial.py` — 核心试用期逻辑
  - `check_trial(tenant_id, user_id, duration_hours)` — 试用期状态检查（自动注册新用户）
  - `check_user_token_quota(tenant_id, user_id, limit)` — 每用户 6 小时 token 限额
  - `record_user_tokens(tenant_id, user_id, tokens)` — LLM 调用后记录 token 用量
  - `approve_user` / `block_user` / `reset_user` — 管理操作
  - `list_trial_users(tenant_id)` — 列出所有试用用户（SCAN 遍历）
- `app/router/intent.py` — 在 `route_message()` 中 rate_limit 之后做 trial + 6h 配额检查

**用户状态流转：**
```
新用户首次消息 → trial（试用中）
                    ↓ duration_hours 过期
                 expired（已过期，被拦截）
                    ↓ 管理员操作
              approved（已审批）或 blocked（已封禁）
```

**Redis 数据结构：**
```
trial:{tenant_id}:user:{user_id} → HASH {
    first_seen, status, message_count, last_active,
    approved_at, approved_by, notes
}
quota:6h:{tenant_id}:{user_id} → SORTED SET (滑动窗口，TTL 6h)
```

**租户配置：** 在 tenants.json 中设置：
```json
{
  "trial_enabled": true,           // 启用试用期
  "trial_duration_hours": 48,      // 试用时长（小时）
  "quota_user_tokens_6h": 500000   // 每用户每 6 小时最大 token 数，0=不限
}
```

### 部署配额系统（Deploy Quota）

**目的：** 每个用户有 N 次免费部署 bot 的机会（默认 1 次），部署成功才消耗配额。用完后引导付费。超管不受限。

**实现：**
- `app/services/deploy_quota.py` — 核心配额逻辑
  - `check_deploy_quota(tenant_id, user_id, free_deploys)` — 检查剩余额度
  - `consume_deploy_quota(tenant_id, user_id, deployed_tid, free_deploys)` — 部署成功后扣减
  - `set_user_quota` / `reset_user_quota` / `get_user_quota` — 管理员操作
  - `list_all_quotas(tenant_id)` — 列出所有用户配额
- `app/tools/customer_ops.py` — `request_provision` 入口处做前置配额检查
- `app/services/provision_approval.py` — `approve_request` 成功后自动消耗配额
- `app/services/base_agent.py` — `_build_deploy_quota_context()` 注入到 system prompt

**Redis 数据结构：**
```
deploy_quota:{tenant_id}:{user_id} → HASH {
    total, used, deploys (JSON list), first_request, last_deploy, notes
}
```

**业务规则：**
- 每用户默认 1 次免费部署，仅部署成功才消耗
- 申请被拒绝不扣额度
- 超管跳过配额检查
- 配额用完后 Steven AI 引导用户了解付费方案
- 管理员可通过 admin API 或 dashboard 手动调整配额

**触发场景：** 用户主动说「想部署 bot」、Steven AI 主动推荐、管理员指令，三种都合法。
Steven AI 需收集：客户名称、目标平台、bot 用途/类型、联系方式。

**租户配置：** 在 tenants.json 中设置：
```json
{
  "deploy_free_quota": 1    // 每用户免费部署次数，0=无限制
}
```

**Admin API：**
```
GET  /admin/api/deploy-quotas/{tid}              — 列出用户配额
GET  /admin/api/deploy-quotas/{tid}/{uid}        — 查看配额详情
POST /admin/api/deploy-quotas/{tid}/{uid}/set    — 设置配额（body: {total, notes}）
POST /admin/api/deploy-quotas/{tid}/{uid}/reset  — 重置配额
```

### Admin Dashboard

**目的：** Web 管理面板，管理租户、用户试用状态、用量统计。

**实现：**
- `app/admin/routes.py` — FastAPI 路由（API + 页面）
- `app/admin/dashboard.html` — 单页应用（Tailwind CDN + vanilla JS，无构建步骤）
- 认证：Bearer token（`ADMIN_TOKEN` 环境变量）

**访问：** `https://your-domain/admin/dashboard`

**功能：**
- Overview：所有租户概览（平台、月度用量、试用/配额配置）+ **编辑租户配置**
- Users：试用用户管理（查看状态、approve/block/reset、备注）
- Usage：月度用量统计（按租户汇总 + 每日柱状图）
- Instances：容器实例管理（创建/重启/停止/销毁）+ Co-tenant CRUD

**租户编辑：** Dashboard 可编辑所有已部署 bot 的配置（name、system prompt、试用期、配额、限流、记忆参数等）。
- 本地租户：直接更新 registry + tenants.json + Redis 同步
- 跨容器租户：通过 Redis `tenant_cfg:*` 读写完整配置
- 部分可见租户（仅有 `admin:tenant:*` 元数据）：编辑受限，前端显示警告

**可编辑字段（`_EDITABLE_FIELDS`）：** name, llm_system_prompt, custom_persona, trial_*, quota_*, rate_limit_*, deploy_free_quota, memory_*, admin_names, tools_enabled, capability_modules, self_iteration_enabled, instance_management_enabled

**API 端点：**
```
GET  /admin/dashboard                          — 管理面板页面
GET  /admin/api/tenants                        — 租户列表（跨容器）
GET  /admin/api/tenants/{tid}/config           — 租户可编辑配置（3 级 fallback）
PUT  /admin/api/tenants/{tid}/config           — 更新租户配置（跨容器）
GET  /admin/api/usage[/{tenant_id}]            — 用量统计
GET  /admin/api/trial/{tenant_id}/users        — 试用用户列表
GET  /admin/api/trial/{tenant_id}/user/{uid}   — 用户详情
POST /admin/api/trial/{tid}/user/{uid}/approve — 审批
POST /admin/api/trial/{tid}/user/{uid}/block   — 封禁
POST /admin/api/trial/{tid}/user/{uid}/reset   — 重置
POST /admin/api/trial/{tid}/user/{uid}/notes   — 设置备注
```

**跨容器架构：** 每个容器只加载自己的租户到 `tenant_registry`，但 admin dashboard 需要看到所有租户。

```
Redis key: admin:tenant:{tenant_id} → JSON {tenant_id, name, platform, trial_enabled, ...}（元数据，TTL 24h）
Redis key: tenant_cfg:{tenant_id}   → JSON 完整租户配置（dashboard 添加的租户，无 TTL）
Redis key: tenant_sync:queue        → LIST 实时通知消息（LTRIM 保留最近 100 条）
```

**配置读取 3 级 fallback（GET /api/tenants/{tid}/config）：**
1. 本地 `tenant_registry`（内存，最全） → `_source: "local"`
2. Redis `tenant_cfg:{tid}`（dashboard 添加/编辑过的完整配置） → `_source: "redis_cfg"`
3. Redis `admin:tenant:{tid}`（仅元数据，字段不全） → `_source: "redis_meta_partial"`

**部署配置：** 在 `.env` 中设置 `ADMIN_TOKEN=your-secure-token`，Nginx 配置中 `/admin/` 路由到任意容器端口（认证由应用层 Bearer token 控制）。

### 跨容器租户同步（tenant_sync）

**目的：** Dashboard 添加/编辑的租户配置需要跨容器同步，且重启不丢失。

**实现：** `app/services/tenant_sync.py`
- `publish_tenant_update(action, config)` — 持久化到 Redis `tenant_cfg:*` + 发消息到队列
- `load_persisted_tenants()` — 容器启动时从 Redis 加载所有 `tenant_cfg:*`
- `start_sync_listener()` — 后台轮询队列（5 秒间隔），实时 hot-load 新租户
- `_process_message()` — 处理队列消息，"add" 直接注册，"update" 合并到已有 registry

**设计要点：**
- **持久化 vs 队列分离**：`tenant_cfg:*` 永久保存（重启加载），`tenant_sync:queue` 只做实时通知（LTRIM 100 条）
- **不为 tenants.json 原生租户创建 tenant_cfg**：`update` 操作只更新已存在的 `tenant_cfg` 条目，避免泄露 `${VAR}` 解析后的密钥
- **队列消息脱敏**：update 消息只发可编辑字段，不发凭证
- **update 合并语义**：`_process_message("update")` 合并到 registry 已有条目，保留凭证等原有字段

**启动流程（main.py）：**
```python
# 1) 加载 tenants.json
# 2) load_persisted_tenants() — 从 Redis 加载 dashboard 添加的租户
# 3) start_sync_listener() — 启动后台轮询
```

### 测试系统

**首次引入测试（64 个测试用例）：**
- `tests/test_metering.py` — 计量系统（13 个：UsageRecord / record_usage / check_quota / get_usage_summary）
- `tests/test_rate_limiter.py` — 限流器（8 个：租户/用户级限流 / fail-open）
- `tests/test_tenant_isolation.py` — 租户隔离（10 个：_tuk / 模式隔离 / 配额字段）
- `tests/test_auto_fix_boundary.py` — 自修复边界（17 个：allowlist / 读写权限 / 各路径拦截）
- `tests/test_tenant_config.py` — 租户配置注册表（12 个：字段完整性 / 加载 / 查找 / 环境变量解析）
- `tests/test_route_integration.py` — 路由集成（3 个：配额拒绝 / 限流拒绝 / 正常放行）
- `tests/test_deploy_quota.py` — 部署配额（16 个：check/consume/set/reset/get + 集成测试）

**运行：** `python -m pytest tests/ -v`

### HTTPS 支持

- `templates/nginx-site-ssl.conf` — 带 SSL 的 nginx 配置模板（TLS 1.2+, Mozilla Intermediate）
- `scripts/setup_ssl.sh` — 一键 SSL 配置脚本（certbot + Let's Encrypt 自动续期）
- 用法：`sudo bash scripts/setup_ssl.sh your-domain.com admin@example.com`

### 微信客服 Session State 管理

**背景：** 企微客服 API 有三种 service_state：
- `0` — 未接入（初始态）
- `1` — 智能助手接待（bot 可自由回复）
- `3` — 人工接待（后台分配，API 无法直接转出到 1）

**踩过的坑：**
1. **state=3 卡死** — 如果企微后台配置了「人工接待」，新用户进来直接 state=3，API 只能 3→2（待接入池），不能 3→1。必须后台改成「智能助手接待」
2. **state 转接路径** — API 限制：0→1 ✅、3→2 ✅、3→1 ❌。enter_session 时自动尝试转 state=1，失败时检测 state=3 并打日志提醒管理员
3. **welcome_code 20 秒过期** — `send_msg_on_event` 必须在 20 秒内调用，过期则静默失败
4. **`get_service_state`** — 新增的诊断 API，用于检测当前 state 并输出有用的错误信息

**实现位置：** `app/webhook/wecom_kf_handler.py:_handle_event()` + `app/services/wecom_kf.py`

### 回复设计原则

**核心原则：除了错误提示（超时/异常），所有回复都应由 LLM 生成，禁止硬编码人机回复。**

- 欢迎语：enter_session 时通过 `_process_and_reply` 让 LLM 自然打招呼，不用写死文本
- 错误降级：超时、异常等极端情况才用硬编码友好提示（"处理超时了，请简化一下消息再试~"）
- 老用户回来（无 welcome_code）：不发任何东西，等用户主动说话

### 社媒数据 API（TikHub 集成）

**目的：** `search_social_media` 工具通过第三方 API 获取精确的社媒数据（粉丝数、点赞数、播放量等），替代 DuckDuckGo 间接搜索。

**当前支持：**
- **TikHub API** — `api.tikhub.dev`（中国直连，$0.001/次）
  - 抖音用户搜索：`/api/v1/douyin/web/fetch_user_search_result_v2`
  - 抖音视频搜索：`/api/v1/douyin/web/fetch_video_search_result`
  - 小红书笔记搜索：`/api/v1/xiaohongshu/web/search_notes`
  - 小红书用户搜索：`/api/v1/xiaohongshu/web_v2/search_users`（fallback 到笔记搜索提取作者）

**配置：** 在 tenants.json 中设置：
```json
{
  "social_media_api_provider": "tikhub",
  "social_media_api_key": "Bearer token from TikHub",
  "social_media_api_secret": ""
}
```

**fallback 逻辑：** API key 未配置或 API 调用失败 → 自动回退到 `web_search`（DuckDuckGo）。

**实现位置：** `app/tools/social_media_ops.py`（`_tikhub_get()` + `_search_tikhub()` + 4 个平台端点函数）

### Per-Tenant 记忆配置

**目的：** 不同类型的 bot 需要不同深度的记忆。AI 分身需要长期记忆，调研工具不需要。

**配置字段（tenants.json）：**
```json
{
  "memory_diary_enabled": true,    // 每次交互后 LLM 提炼摘要写日记（false=节省 LLM 调用）
  "memory_journal_max": 800,       // 日志条数达此阈值触发压缩（0=不压缩）
  "memory_chat_rounds": 5,         // 对话历史保留轮数（1轮=user+assistant各1条）
  "memory_chat_ttl": 3600,         // 对话历史 Redis TTL（秒）
  "memory_context_enabled": true   // 是否在 system prompt 注入记忆上下文
}
```

**推荐配置：**
| Bot 类型 | diary | journal_max | chat_rounds | chat_ttl | context |
|---|---|---|---|---|---|
| AI 分身（kf-steven-ai） | true | 800 | 10 | 7200 | true |
| 调研工具（kf-leadgen-demo） | false | 0 | 3 | 1800 | false |
| 技术助手（code-bot） | true | 800 | 5 | 3600 | true |
| PM Bot（pm-bot） | true | 800 | 5 | 3600 | true |

**记忆系统提示注入：** 当 bot 的工具列表包含 `save_memory` 时，system prompt 自动注入 `_MEMORY_USAGE_HINT`，引导 LLM 在适当时机主动保存/回忆记忆。

**实现位置：** `tenant/config.py`（配置字段）+ `services/history.py`（chat rounds/TTL）+ `services/memory.py`（journal 压缩阈值）+ `services/base_agent.py`（diary/context/hint 注入）

### 飞书聊天记录自动回填（Cache Miss Backfill）

**目的：** 当 Redis 对话历史 TTL 过期后，用户再发消息时 bot 不再"失忆"——自动从飞书 API 拉取最近 N 轮聊天记录回填缓存。

**触发条件：** `_ensure_loaded()` 发现 Redis 为空（TTL 过期或从未缓存） → 检查租户平台是否为 feishu → 调用飞书 `/im/v1/messages` API 拉取最近消息。

**三级加载：**
1. 内存缓存（`_store`）→ 最快，进程内有效
2. Redis 持久化 → 跨请求有效，TTL 过期自动清理
3. **飞书 API 回填（新增）** → Redis 也为空时，从平台拉真实聊天记录

**行为：**
- 仅对飞书平台租户生效（`platform == "feishu"`）
- 企微客服没有历史消息 API，无法回填，仍依赖 memory journal 兜底
- 拉取数量 = `memory_chat_rounds * 2`（与配置的轮数一致）
- 回填后写入 Redis 缓存，后续请求不会重复拉取
- 正确还原 role（bot 发的 = assistant，其他人发的 = user）
- 群聊消息自动带发送者名字前缀（`[姓名]: 内容`）
- fail-open：API 调用失败不影响正常对话，只是没有历史上下文

**限制：**
- 私聊需要已有 p2p chat_id 映射（用户之前和 bot 聊过，registry 里有记录）
- 飞书 API 需要 bot 有 `im:message` 读取权限
- 图片/文件/视频等非文本消息只恢复为占位符（`[图片]`、`[文件]`等）

**实现位置：** `app/services/history.py` — `_backfill_from_feishu()` + `_ensure_loaded()` 三级加载

### Gemini thinking_config

**问题：** Gemini 模型内部推理过程（thinking）会泄露到最终回复中，用户能看到模型的思考步骤。

**修复：** 在 `gemini_provider.py` 调用时配置 `thinking_config`，确保推理过程不出现在 response 中。

### 飞书群消息 open_id 自动学习

**问题：** 飞书 bot 在群里被 @，但不知道自己的 open_id，导致无法识别 @mention 并回复。

**修复：** bot 从自己发出的回复消息中提取 `sender.sender_id.open_id`，自动学习并缓存自己的 open_id。无需手动配置。

**实现位置：** `app/webhook/feishu_handler.py`

## Pitfalls & Lessons Learned

### Bridge 网络下跨容器 HTTP 不通（严重）

**问题：** Provisioned 容器使用 Docker bridge 网络（端口隔离、安全），但 dashboard 添加 co-tenant 后通过 HTTP `127.0.0.1:{port}` 同步到目标容器。Bridge 模式下 `127.0.0.1` 是容器自己的 loopback，不是宿主机，导致：
- Dashboard 添加 co-tenant 显示 `hot_loaded: No`（连接失败）
- `kf_dispatch` 转发消息到其他容器也全部失败（co-tenant 消息无响应）
- Dashboard 添加的租户重启后丢失（tenants.json 是 `:ro` 挂载，无法写入）

**根因：** bridge 模式 vs host 模式的根本区别。主 docker-compose 用 host 模式（容器内 `127.0.0.1` = 宿主机），但 provisioned 容器用 bridge（`127.0.0.1` = 容器自己）。

**修复：** 所有跨容器通信改为 Redis 消息队列（Upstash REST API）：
1. `tenant_sync.py` — 发布 `tenant_cfg:{tid}` 持久化 + `tenant_sync:queue` 实时通知
2. `wecom_kf_handler.py:_try_hot_load_tenant()` — 从 Redis `tenant_cfg:*` 扫描匹配 open_kfid
3. `main.py` startup — `load_persisted_tenants()` 从 Redis 加载，`start_sync_listener()` 后台轮询
4. `admin/routes.py` — co-tenant add/remove/edit 全部改用 Redis sync

**不改回 host 模式的原因：** bridge 模式是有意选择（端口隔离 + 安全），改回 host 会丧失这些好处。

**教训：** 跨容器通信不能依赖 `127.0.0.1`。Docker bridge 网络下必须用外部通道（Redis/MQ/service discovery）。在选择容器网络模式时，要考虑所有通信路径（不只是入站 webhook）。

### Dashboard 编辑泄露 `${VAR}` 解析值

**问题：** `asdict(t)` 返回 TenantConfig 的所有字段，其中 `${GEMINI_API_KEY}` 等环境变量引用已被解析为真实值。如果将 `asdict()` 结果直接写回 tenants.json 或 Redis，会：
- 把 `${VAR}` 替换为明文密钥（tenants.json 提交到 git = 密钥泄露）
- 为 tenants.json 原生租户创建不必要的 `tenant_cfg:*` Redis 条目

**修复：**
1. `_update_local_tenants_json()` 只写用户实际修改的字段（`updates`），不写 `full_config`
2. `publish_tenant_update("update")` 只更新已存在的 `tenant_cfg:` 条目，不为新租户创建
3. 队列消息中 update 操作剥离凭证字段，只发安全的可编辑字段
4. `_process_message("update")` 合并到已有 registry 条目，保留原有凭证

**实现位置：** `admin/routes.py` + `services/tenant_sync.py`

**教训：** 永远不要把 `asdict()` / `model_dump()` 的完整输出直接写回持久化存储。对含 `${VAR}` 引用的配置系统，写入时必须区分"用户修改的字段"和"系统解析后的字段"。

### 多租户容器化后 OAuth 回调断裂（严重）

**问题：** Phase 1 容器化后，每个租户容器跑在独立端口（8101/8102/8103），但 Nginx 的 `/oauth/callback` 仍然路由到 `:8000`（旧主进程）。飞书 OAuth 授权用户侧显示成功，但 `exchange_code` 从未被调用 → token 永远不更新 → 邮件等需要 user_access_token 的功能全部不可用。

**症状：** 用户反复 `/auth` 授权成功，但 API 始终报 `99991679`（scope 不足）。服务器无错误日志（请求根本没到容器），排查极其困难。

**根因：** `provisioner.py:_generate_nginx_conf` 只生成了 webhook 路由，漏了 OAuth callback 路由。

**修复：**
1. `main.py` — 新增 `/oauth/{tenant_id}/callback` 路由（per-tenant）
2. `provisioner.py:_generate_nginx_conf` — 为每个租户生成 OAuth callback 的 nginx 路由
3. `oauth_store.py:build_auth_url` — 自动把 `/oauth/callback` 改写为 `/oauth/{tenant_id}/callback`
4. `oauth_store.py:exchange_code` — redirect_uri 同步改写（飞书要求 auth 和 exchange 的 redirect_uri 完全一致）
5. 旧的 `/oauth/callback → :8000` 保留为 fallback（向后兼容未迁移租户）

**教训：** 基础设施变更（端口、路由、容器化）必须端到端验证所有入口流量路径，不能只验证主流程（webhook）。OAuth 回调、管理接口等辅助路径一样会断。

**部署后需要做的：**
1. 重新生成所有租户的 nginx conf（`provision` 系统或手动）
2. `sudo nginx -t && sudo nginx -s reload`
3. 每个租户 `/auth` 重新授权一次（让新的 per-tenant callback URL 生效）
4. 飞书开发者后台的「重定向 URL」更新为 `/oauth/{tenant_id}/callback` 格式

### Docker build 代理泄漏

**问题：** 项目 `.env` 里有 `HTTPS_PROXY=http://host.docker.internal:...`，docker compose build 默认加载 `.env`，导致构建时代理指向不存在的地址，`pip install` 全部失败。

**修复链：**
1. `docker compose build --build-arg` 覆盖 → 无效（`.env` 优先级更高）
2. Docker client config `~/.docker/config.json` 代理 → 无效（build-time 不看这个）
3. **最终方案：** `docker compose --env-file /dev/null build` — 构建时完全不加载 `.env`

**教训：** 在有代理环境的机器上做 Docker 构建，务必隔离 `.env`。

### ⛔⛔⛔ Redis 绝对不能继承全局 HTTPS_PROXY（已造成生产事故）⛔⛔⛔

**严重程度：P0 — 此 bug 会导致 bot 全功能瘫痪（记忆/OAuth/试用期/计量/租户同步全挂），且表面看容器还在跑，极难排查。**

**事故经过（2026-03-07）：** xray 代理进程挂掉 → `redis_client.py` 继承了 `HTTPS_PROXY` 环境变量 → 所有 Redis 请求尝试走代理 → `Connection refused` → bot 看起来正常运行但所有依赖 Redis 的功能全部静默失败。

**根因：** `_get_proxy()` 函数读了 `HTTPS_PROXY`/`HTTP_PROXY` 全局环境变量，而 Upstash Redis 在国内可直连，根本不需要代理。

**修复规则（永久生效）：**
- `app/services/redis_client.py` 的 `_get_proxy()` 只允许读 `REDIS_PROXY` 环境变量
- **绝对不要**在 `_get_proxy()` 中读取 `HTTPS_PROXY`、`HTTP_PROXY`、`https_proxy`、`http_proxy`
- **绝对不要**给 Redis 的 httpx.Client 传入全局代理
- 如果你在重构 redis_client.py，请确保 httpx 不会自动继承环境变量代理（httpx 默认不读环境变量代理，但 `proxy=` 参数会覆盖）

**验证方法：** `grep -n "HTTPS_PROXY\|HTTP_PROXY\|https_proxy\|http_proxy" app/services/redis_client.py` — 如果有匹配行（除了注释/警告），说明又被改坏了。

**教训：** 代理配置要按服务粒度控制，不要用全局环境变量一刀切。国内可直连的服务（Upstash Redis）走代理只会增加故障面。

### ⛔⛔⛔ xray 挂掉导致系统雪崩 — 必须硬重启（已造成多次生产事故）⛔⛔⛔

**严重程度：P0 — xray 进程挂掉后几秒内系统完全不可用（SSH 连不上、VNC login 无响应），只能阿里云控制台硬重启。已发生 3+ 次。**

**事故链条：**
```
xray 挂掉（原因：OOM / crash / 未知）
  → .env 的 HTTPS_PROXY=http://host.docker.internal:10809 指向死端口
  → 3 个容器里所有走代理的 httpx 请求 hang（每个等 10-120 秒超时）
  → 新请求不断进来（用户消息、Gemini 多轮调用、定时任务、tenant_sync 轮询）
  → 几十上百个 TCP 连接堆积在 SYN_SENT / ESTABLISHED 等待超时
  → 宿主机 conntrack 表 / 文件描述符 / PID 耗尽
  → 内核无法 fork 新进程 → SSH 超时、VNC login 无响应
  → 只能阿里云控制台硬重启
```

**根因分析：**
1. **httpx 默认 `trust_env=True`** — 继承 `HTTPS_PROXY` 环境变量，所有 HTTP 请求走死掉的代理
2. **大量客户端无 timeout 或 timeout 过长** — wecom.py 6 个 AsyncClient 完全没有 timeout（无限 hang）；Gemini 总超时 120 秒且无 connect timeout
3. **Gemini 是最大流量源** — 每个用户消息触发多轮 LLM 调用（tool calling loop），每轮一个新连接
4. **xray watchdog（cron 1分钟）太慢** — 系统几秒内就崩了，1 分钟检测周期来不及

**修复（三层防御）：**

| 层 | 措施 | 效果 |
|---|---|---|
| 1 | systemd xray-watchdog 每 5 秒检测端口 | xray 挂了 ≤5s 自动拉起 |
| 2 | 中国 API 客户端全部 `trust_env=False` | 飞书/企微/TikHub/OAuth 不走代理，xray 挂了完全无影响 |
| 3 | 所有 httpx 客户端补齐 timeout + Gemini connect=5s | 走代理的请求 5 秒快速失败，不堆积 |

**修改的文件（15 个）：**
- `feishu.py` — 9 个 AsyncClient 全部加 `trust_env=False` + 补 4 个缺失 timeout
- `wecom.py` — 8 个 AsyncClient 全部加 `trust_env=False` + 补 6 个缺失 timeout
- `wecom_kf.py` — 17 个 AsyncClient 全部加 `trust_env=False`
- `feishu_api.py` — 6 个 Client 全部加 `trust_env=False`
- `oauth_store.py` — 4 个 Client 全部加 `trust_env=False`
- `file_export.py` — 4 个 Client 全部加 `trust_env=False`
- `social_media_ops.py` — TikHub 2 个 Client 加 `trust_env=False`
- `xhs_ops.py` — 企微 API 4 个 Client 加 `trust_env=False`
- `gemini_provider.py` — Gemini Client 加 `httpx.Timeout(connect=5.0, read=120.0)`
- `github_api.py` / `repo_search.py` — connect timeout 从 30s 降到 5s
- `super_admin.py` / `auto_fix.py` / `admin/routes.py` / `wecom_kf_handler.py` — 补 `trust_env=False`

**永久规则：**
- **新增 httpx 客户端时**：如果目标是中国可直连的 API（飞书/企微/TikHub/Upstash），**必须** `trust_env=False`
- **需要代理的客户端**（Gemini/GitHub/CF Worker）：**必须**设置短 connect timeout（≤5s），通过 `httpx.Timeout(connect=5.0, read=N)` 实现
- **绝对不要**创建无 timeout 的 httpx 客户端 — `httpx.AsyncClient()` 裸调用会无限 hang
- **验证方法：** `grep -rn "httpx\.\(Client\|AsyncClient\)(" app/ | grep -v trust_env | grep -v _GH_TIMEOUT` — 检查是否有遗漏

**xray watchdog 部署：**
```bash
# /etc/systemd/system/xray-watchdog.service
[Service]
ExecStart=/bin/bash -c 'while true; do if ! ss -tln | grep -q :10809; then systemctl restart xray; fi; sleep 5; done'
Restart=always
```

**教训：**
1. 代理是单点故障 — 所有 HTTP 流量都依赖一个 xray 进程，它一挂全部完蛋
2. `trust_env=True` 是 httpx 的危险默认值 — 在有 `HTTPS_PROXY` 的环境里，每个 httpx 客户端都会继承
3. 无 timeout 的 HTTP 客户端是定时炸弹 — 正常时没问题，网络异常时无限 hang
4. connect timeout 和 read timeout 要分开设置 — Gemini 读取需要 120s，但连接只需要 5s
5. 雪崩速度极快（秒级）— 1 分钟 cron 检测根本来不及，必须用 systemd 5 秒级检测

### Docker bridge 网络 + extra_hosts（provisioned 容器）

**背景：** Provisioned 容器使用 Docker bridge 网络（端口隔离、安全），但容器内需要访问宿主机的 xray 代理（用于 Gemini API 等）。Linux bridge 模式下 `host.docker.internal` 不会自动解析。

**方案：** `provisioner.py:_generate_compose()` 生成的 compose 文件包含 `extra_hosts: ["host.docker.internal:host-gateway"]`，Docker 自动将 `host.docker.internal` 映射到宿主机 IP。
- `.env` 中 `HTTPS_PROXY=http://host.docker.internal:10809` 在容器内可正常解析
- 多租户容器用不同端口映射（8101-8199 → 8000）

**⚠️ 绝对不要 `git clean -fd` 删除 `instances/` 目录！** 见下方「`git clean` 误删 instances/ 导致容器配置丢失」。

### YouTube 视频分析优化（from_uri 直传）

**背景：** 原方案是 yt-dlp 下载视频 → 上传到 Gemini File API → 分析。YouTube 视频需要代理+cookies，下载慢且容易失败。

**优化：** Gemini API 原生支持 YouTube URL，通过 `Part(file_data=FileData(file_uri=youtube_url))` 直传，Gemini 服务端自己获取视频。

**实现：**
- `sandbox_caps.py` — `is_youtube_url()` + `gemini_analyze_youtube_url()` + `_async_gemini_analyze_youtube()`
- `video_url_ops.py` — `analyze_video_url()` 检测 YouTube URL 自动走 from_uri 路径，非 YouTube 走 yt-dlp
- 元信息（标题/时长）仍尝试 yt-dlp 获取，失败不影响分析
- **限制：** 仅支持公开视频（不支持未列出/私享）

### yt-dlp 需要 Node.js

**问题：** YouTube 签名解密需要 JS runtime，`yt-dlp` 默认用 `PhantomJS`（已废弃）。

**修复：** Dockerfile 装 `nodejs`，yt-dlp 自动识别并使用。同时加了 `--cookies` 支持解决年龄限制视频。

### scheduler 租户上下文丢失

**问题：** 定时任务（APScheduler）在独立协程中执行，`contextvars` 租户上下文丢失 → API 调用拿不到 Bearer token → 静默失败。

**修复：** scheduler 触发时显式设置租户上下文（`set_current_tenant()`），确保定时任务也能正确识别租户。

### 定时提醒 per-user Redis 隔离（defense-in-depth）

**问题：** 提醒系统原来用 `reminders:{tenant_id}` 作为 Redis key，所有用户的提醒存在同一个 Sorted Set 中。用户隔离仅靠 app 层的 `user_id` 过滤——如果过滤逻辑有 bug，用户 A 可能看到用户 B 的提醒。

**修复：** Redis key 从 `reminders:{tenant_id}` 改为 `reminders:{tenant_id}:{user_id}`，每个用户独立 Sorted Set。增加辅助索引 `reminder_users:{tenant_id}`（SET）记录有活跃提醒的 user_id，供 scheduler 遍历。

**Redis 数据结构（新）：**
```
reminders:{tenant_id}:{user_id}     → SORTED SET（score=触发时间戳，member=JSON）
reminder_users:{tenant_id}          → SET（有活跃提醒的 user_id 集合）
reminder_log:{tenant_id}            → LIST（执行日志，保留最近 100 条）
```

**向后兼容：** `migrate_legacy_key(tenant_id)` 在 scheduler 每次循环时自动检测旧 key，将数据迁移到 per-user key 后删除旧 key。安全幂等。

**实现位置：** `app/tools/reminder_ops.py`（key 函数 + CRUD + 迁移）+ `app/services/scheduler.py`（调用迁移 + 遍历用户 key）

### 多租户容器化后 OAuth 回调失效（严重）

**问题：** 迁移到多租户容器架构（Phase 1）后，每个租户容器使用独立端口（8101-8199），但 Nginx 反向代理配置未及时更新。飞书 OAuth 授权完成后，回调请求 `POST /oauth/callback` 被路由到旧端口或无法到达容器，导致：
- 用户授权成功（飞书后台显示授权详情）
- 但服务器收不到回调，`/oauth/callback` 端点无日志
- 用户 token 无法保存，邮件等功能无法使用

**根因：**
```
# 容器端口映射（docker ps）
0.0.0.0:8102->8000/tcp   # pm-bot 容器映射到 8102

# 但 Nginx 配置仍指向旧的 8000 端口
proxy_pass http://127.0.0.1:8000/oauth/callback;  # ❌ 错误
```

**排查难点：**
- 飞书授权页面显示成功，用户感知不到问题
- 服务器没有任何错误日志（请求根本没到达）
- 容易误判为 scope 配置问题，反复调整 mail scope 无效

**修复：**
```nginx
location /oauth/callback {
    # 根据 tenant_id 路由到对应容器端口
    # pm-bot 用 8102
    proxy_pass http://127.0.0.1:8102/oauth/callback;
}
```

**教训：**
1. 多租户容器化时，Nginx 配置必须与容器端口同步更新
2. 所有涉及外部回调的功能（OAuth、Webhook）都要验证端到端连通性
3. 添加 `/oauth/callback` 访问日志，便于快速诊断

### 飞书文档权限管理

**背景：** Bot 创建的飞书文档，owner 是 bot 自己。用户虽然被授予 full_access 但无法迁入知识库（需要 owner 权限）。飞书没有「权限申请」webhook 事件，bot 收不到用户的权限申请通知。

**方案：**
1. `create_document()` 创建文档后自动 transfer owner 给请求用户
2. 新增 `transfer_feishu_doc_owner` 工具 — 随时转让文档所有权（支持按姓名指定用户）
3. 新增 `grant_feishu_doc_permission_to_user` 工具 — 按姓名给其他用户开权限
4. 新增 `set_feishu_doc_sharing` 工具 — 设置链接分享权限（组织内可读/可编辑/互联网可读等）

**飞书 API 限制：** 没有 `drive.file.permission_member_applied` 事件。用户点「申请权限」后飞书只在 UI 通知 owner，不推送 webhook。所以 bot 不能监听权限申请事件，只能用「创建时自动转让 + 对话中手动操作」的方式解决。

**实现位置：** `app/tools/doc_ops.py`
- `_transfer_doc_owner()` — POST `/drive/v1/permissions/{token}/members/transfer_owner`
- `_set_doc_link_share()` — PATCH `/drive/v1/permissions/{token}/public`
- `grant_doc_permission_to_user()` — 按姓名查 open_id → 调 `_grant_doc_permission()`

### Capability Acquisition Layer（能力获取层 — 元能力）

**设计哲学：** bot 不止能用已有工具干活，还能自主获取新能力来完成从未见过的任务。

**三堵墙与解决方案：**

| 墙 | 问题 | 解决工具 | 实现文件 |
|---|---|---|---|
| 依赖墙 | 沙箱不能 import 新包 | `install_package` — pip install + 动态白名单 | `app/tools/env_ops.py` |
| 交互墙 | 无法操作没有 API 的网站 | `browser_*` — Playwright + Gemini 视觉 | `app/tools/browser_ops.py` |
| 边界墙 | auto-fix 只能改 tools/ | `request_infra_change` — 申请→审批→执行 | `app/tools/capability_ops.py` |

**核心流程（bot 遇到新任务时）：**
```
assess_capability(任务) → 识别能力 gap
  ├─ 缺 Python 包 → install_package 安装
  ├─ 缺特定功能 → create_custom_tool 创建
  ├─ 需要浏览器 → browser_open / browser_do 操作
  ├─ 需要改基础设施 → request_infra_change 申请审批
  └─ 需要人工操作 → guide_human 引导用户
```

**动态白名单（sandbox.py 改造）：**
- `install_package` 安装包后，模块名写入 Redis `sandbox:dynamic_modules:{tenant_id}`
- `sandbox.py` 的 `_is_module_allowed()` 同时检查静态白名单 + Redis 动态白名单
- 动态白名单带 60 秒缓存，不会频繁查 Redis
- fail-open：Redis 不可用时只检查静态白名单

**浏览器自动化（vision-language-action 循环）：**
```
browser_open(url) → Playwright 截图 → Gemini 视觉分析 → 返回页面描述
  → LLM 决定下一步 → browser_do(action) → 截图 → 分析 → 循环
```
- 需要先 `install_package('playwright')` + 服务器运行 `playwright install --with-deps chromium`
- 每个租户最多 1 个浏览器会话，5 分钟不活动自动关闭
- SSRF 防护：禁止访问内网 IP / localhost / file://

**安全边界：**
- `install_package`: 包名字符集校验 + 危险包黑名单（paramiko/scapy/docker 等）
- `browser_ops`: URL 过滤（阻止 SSRF）+ 会话隔离 + 超时清理
- `request_infra_change`: 只生成方案存 Redis，不自动执行，管理员审批
- `guide_human`: 纯信息输出，不执行任何操作

### K2.5 Function Calling 不可靠（大工具集场景）

**问题：** Kimi K2.5 在工具数量超过 ~30 个时，function calling 严重不可靠：
- 从 71 个工具缩减到 37 个后仍无法正确选择 `list_kf_accounts`，改调了功能完全不同的 `list_custom_tools`
- 甚至用 `create_custom_tool` 从零写了一个重复工具，而不是调用已有工具
- 系统 prompt 中的引导（"行动前决策"）对 K2.5 基本无效——K2.5 更看 tool description 而非 system prompt

**根因：** K2.5 的 function calling 实现对大工具集支持差。GPT-4o / Claude / Gemini 均可处理 100+ 工具。

**修复：**
1. **所有租户**均清空 `coding_model`，全部走 Gemini function calling
2. `TenantConfig.coding_model` 默认值从 `"kimi-k2.5"` 改为 `""`，新租户自动走 Gemini
3. K2.5 路由机制保留但不启用，未来如需使用需显式在 tenants.json 配置

**教训：** 选择 LLM 做 function calling 时必须评估工具数量上限。K2.5 适合 <20 工具的简单场景。旧的危险默认值（`coding_model="kimi-k2.5"`）导致新租户自动走 K2.5，排查困难。

### tool summary 泄露到用户回复

**问题：** `_set_tool_summary` 将完整的工具调用记录（含参数 JSON）存入 chat history 的 assistant 消息。K2.5 从历史中学到了 `[本轮调用了: tool_name({"param": "value"})]` 的模式，在回复中原样输出，导致用户看到工具调用细节。

**修复：** `_set_tool_summary` 只记录工具名（不含参数），用 `<tools_used>` XML 标签包裹，避免 LLM 将其当作回复模板。

**实现位置：** `app/services/kimi_coder.py:_set_tool_summary()`

### 宿主机 Python 版本过低

**问题：** `scripts/sync_instance_configs.py` 在 CI/CD 的 SSH step 中直接运行在宿主机上（`python3 scripts/sync_instance_configs.py`），不是在 Docker 容器内。宿主机的 Python < 3.7，`from __future__ import annotations` 直接 SyntaxError，导致部署失败。

**修复：** 去掉 `from __future__ import annotations` 和 3.9+ 的类型注解语法（`dict[str, list]` → 无类型注解）。

**教训：** deploy.yml 里跑的脚本必须兼容宿主机的 Python 版本，不要假设和 Docker 容器一样是 3.12。如果需要高版本特性，用 `docker run` 在容器内跑脚本。

### 企微客服回调是 per-自建应用，不是 per-corp

**问题：** 最初以为企微客服回调 URL 是 corp 级别的（一个 corp 只有一个回调），同 corp 下所有客服账号必须共用容器。实际上：
- 企微客服的 API 权限绑定到**自建应用**
- 不同客服账号可以分配给不同的自建应用
- 每个自建应用有独立的 `secret` / `token` / `encoding_aes_key` / 回调 URL
- 只有共用同一个自建应用的客服账号才必须共用回调

**修复：** co-host 判定条件从"同 corpid"改为"同 corpid + 同 kf_secret"。凭证相同 = 同一个自建应用 = 必须 co-host；凭证不同 = 不同自建应用 = 可以独立容器。

**实现位置：** `provisioner.py:_find_cohost_instance()` + `scripts/sync_instance_configs.py`

### 企微客服 co-host 本地路由

**问题：** 同容器 co-host 两个 KF 租户时，`kf_dispatch` 原来只支持 HTTP 跨容器转发（查 Redis 路由表）。CI/CD 的 `sync_instance_configs.py` 不注册 Redis 路由，导致 co-host 的消息被丢弃（"no route for open_kfid"）。

**修复：** handler 收到不匹配的 `open_kfid` 时，先查本容器 `tenant_registry` 有没有匹配的租户：
- 有 → 直接 `set_current_tenant()` 切换上下文，本地处理（零延迟）
- 没有 → 再走 Redis 路由表 + HTTP 跨容器转发

**实现位置：** `app/webhook/wecom_kf_handler.py:_find_local_tenant_by_kfid()` + `wecom_kf_callback()`

### coding_model 危险默认值

**问题：** `TenantConfig.coding_model` 原默认值为 `"kimi-k2.5"`，导致任何**未显式设置** `coding_model: ""` 的新租户自动走 K2.5 路由。加上 K2.5 在大工具集下不可靠，新租户上线就出问题，且排查困难（日志只显示 `text-only → routing to kimi-k2.5`，看起来像是故意配置）。

**修复：** 默认值改为 `""`（空 = 全走 Gemini）。需要 K2.5 的租户必须显式配置。

**教训：** Pydantic model 的默认值要 fail-safe。路由类配置的默认值应该是「不启用」，而不是「启用某个特定模型」。

### export_file 无 PDF 支持

**问题：** `export_file` 工具只支持 CSV/TXT/MD/JSON。用户要求 PDF 报告时，LLM 只能通过 `create_custom_tool` 临时写 PDF 生成器，代码质量差（硬编码字体路径、不处理字体缺失），产出空 PDF 或乱码。

**修复：** `file_export.py` 新增 `_generate_pdf()`，基于 fpdf2 + NotoSansSC TTF 字体，支持 Markdown 风格内容自动渲染（标题/表格/链接/列表/引用/分隔线）。PDF 生成失败时自动降级为 .md 文件。

**实现位置：** `app/tools/file_export.py` + `Dockerfile` + `requirements.txt`

### PDF 中文乱码（CID-keyed CFF vs TTF）

**问题：** PDF 导出中文一直显示乱码，尝试了 5+ 种方案都失败（TTC 直接加载、collection_font_number、fontTools 提取子字体、构建时提取等）。

**根因：** `fonts-noto-cjk` 安装的 `NotoSansCJK*.ttc` 使用 CID-keyed CFF（OpenType）格式，fpdf2 对此格式兼容性差。无论是直接加载 TTC、还是用 fontTools 提取子字体为独立 OTF，都会产生乱码。

**修复：** 改用 NotoSansSC（Google Fonts 版），纯 TTF（TrueType 轮廓）格式，fpdf2 完美支持。
1. Dockerfile：从 jsDelivr CDN 下载 `NotoSansSC[wght].ttf`（中国有 CDN 节点可直连），移除 `fonts-noto-cjk`
2. `file_export.py`：字体搜索路径改为 TTF 路径，移除所有 TTC/OTF 提取代码，新增运行时 CDN 下载兜底
3. 旧容器无需重建：运行时自动从 jsDelivr CDN 下载到 `/tmp/NotoSansSC.ttf`

**教训：** fpdf2 的字体兼容性取决于字体格式（TrueType vs CFF），不只是文件本身。CID-keyed CFF 虽能被 `add_font()` 加载，但无法正确渲染 CJK 字符。

### export_file PDF 上传失败（bytearray bug）

**问题：** fpdf2 的 `pdf.output()` 返回 `bytearray`，但 httpx 的 `files=` 参数需要 `bytes` 或文件对象。上传时报 `AttributeError: 'bytearray' object has no attribute 'read'`。

**修复：**
1. `_generate_pdf()` 返回 `bytes(pdf.output())` 确保类型正确
2. `_upload_media_sync()` 用 `io.BytesIO()` 包装文件字节，兼容 bytes/bytearray
3. 抑制 `fontTools.subset` 的海量 DEBUG 日志（每次 PDF 生成 50+ 行）

**教训：** httpx 的 `files` 参数对类型敏感。fpdf2 返回 bytearray 而不是 bytes 是容易踩的坑。

### LLM 自定义工具滥用（tool addiction）

**问题：** Gemini 对 `create_custom_tool` 上瘾，遇到任何失败就写新工具而不是分析错误：
- export_file 报错 → 不分析 bytearray 错误，直接创建 3 个不同的 PDF 工具
- 用户要查 log → 不用已有的 `search_logs` 工具，创建 2 个自定义 log 工具
- 12 轮 agent loop，大部分在写废工具

**根因：** system prompt 把 `create_custom_tool` 描述为「核心能力」，LLM 把它当万能钥匙。

**修复：** system prompt 加 guardrails：
1. 明确标注「按需扩展，非首选」
2. 要求先分析错误信息，不要直接写新工具
3. 同一对话最多 1-2 个自定义工具
4. 禁止为已有工具创建重复的自定义工具

**实现位置：** `app/services/kimi_coder.py:_INSTRUCTIONS`

### tools_enabled 白名单遗漏（反复踩坑）

**问题：** 新增工具只在 `app/tools/` + `base_agent.py` 注册，忘了更新 `tenants.json` 中使用 `tools_enabled` 白名单的租户。结果：
- `search_social_media` / `get_platform_search_url` — kf-steven-ai 和 kf-leadgen-demo 都用不了
- `create_plan` / `activate_plan` 等 6 个 plan 工具 — kf-steven-ai 白名单里没有
- `tools_enabled: []`（空 = 全启用）的租户不受影响，所以开发者在 code-bot/pm-bot 上测试正常，以为没问题

**根因：** `tools_enabled` 白名单是 opt-in 模式——非空时只启用列出的工具。新工具默认不在白名单里 = 对白名单租户不可见。开发时容易在 `tools_enabled: []` 的租户上测试，看到工具可用就以为没问题。

**修复：**
1. 补齐了 kf-steven-ai 和 kf-leadgen-demo 的白名单
2. 在 CLAUDE.md 顶部新增「Adding New Tools — MANDATORY Checklist」，Step 4 专门提醒白名单更新

**教训：** 任何涉及 opt-in 白名单的系统，新增功能必须同步更新所有白名单实例。建议未来加 CI 检查：`_ALL_TOOL_DEFS` 中的工具名 ⊆ 每个非空 `tools_enabled` 或有明确的 exclude 理由。

### Agent stall detection 误报（browser 调研场景）

**问题：** bot 在做 YouTube 深度调研时，连续调用 `browser_open` 5 次（访问不同频道页），触发了 stall detection（`_detect_stall()`），导致 agent loop 被提前终止。

**症状：** 用户让 bot 调研 YouTube 频道，bot 浏览了几个页面后突然停止，回复说"调研已完成"但实际只完成了一半。日志显示 `stall detected at round 9`。

**根因：** `_RESEARCH_TOOLS` 白名单没有包含 `browser_open`/`browser_do`/`browser_read`/`search_social_media`。非 research 工具的 stall 阈值是 5（最近 10 次调用中重复 ≥5），research 工具是 7。浏览器操作天然需要反复调用（打开页面→操作→读取→下一个页面），5 次很容易触发。

**修复：** 将 `browser_open`、`browser_do`、`browser_read`、`search_social_media` 加入 `_RESEARCH_TOOLS`。

**实现位置：** `app/services/base_agent.py:_RESEARCH_TOOLS`

**教训：** 新增工具时要评估该工具的调用模式——如果会被 LLM 在同一任务中反复调用（搜索、浏览、社媒调研等），必须加入 `_RESEARCH_TOOLS`，否则 stall detection 会误杀。

### task_watchdog ImportError（企微客服重试失败）

**问题：** `task_watchdog.py` 中 `from app.services.wecom_kf import WecomKfClient` 报 ImportError，导致企微客服平台的未完成任务无法自动重试。

**根因：** `wecom_kf.py` 导出的是模块级 singleton `wecom_kf_client`（小写），不是类名 `WecomKfClient`。且正确用法是直接用 singleton，不需要传 tenant 参数实例化。

**修复：** `from app.services.wecom_kf import wecom_kf_client`，直接使用 `kf_client = wecom_kf_client`。

**实现位置：** `app/services/task_watchdog.py:_retry_wecom_kf()`

### _final_call 幻觉交付物（stall 截断后 LLM 声称已生成文件）

**问题：** agent loop 被 stall detection 强制终止后，`_final_call()` 让 LLM "总结已完成的工作"。LLM 把**计划要做但还没做**的事也描述为"已完成"，声称已生成 PDF/PPT 文件但实际上从未调用 `export_file`。用户收到"已生成报告"的回复，但附件根本不存在。

**根因：** `_final_call` 的 prompt 太模糊（"总结你做了什么"），LLM 无法区分"已完成"和"计划完成"。

**修复：** 重写 `_final_call` prompt，明确要求：
1. 只总结**实际完成**的步骤和**已获取到**的信息
2. 如果还没来得及生成文件，必须如实告知用户"文件还没生成"
3. 不要声称已经生成了实际上没有创建的文件
4. 告诉用户可以说"继续"让 bot 接着做

**实现位置：** `app/services/gemini_provider.py:_final_call()`

**教训：** LLM 在总结时倾向于过度承诺。任何"总结/收尾"prompt 都必须明确要求区分"已做"和"未做"，否则 LLM 会把意图当成事实。

### 小红书 Playwright 搜索结果无 URL（vision-only 盲区）

**问题：** `xhs_search` 和 `xhs_playwright_search` 用 Gemini 视觉分析截图提取搜索结果，但截图中**看不到 URL**——小红书搜索结果卡片只显示标题/作者/点赞数，不显示链接地址。结果所有搜索结果的 `href` 字段为空字符串。

**症状：** 用户让 bot 搜索小红书博主，bot 返回了正确的用户名和粉丝数，但没有任何可点击的链接。LLM 于是**幻觉伪造 URL**（如 `xiaohongshu.com/user/profile/xxx`），用户点击 404。

**根因：** 纯 vision 方案的设计假设是"截图包含所有信息"，但 URL 是不可见元素（存在于 DOM 的 `<a href>` 属性中，不渲染在页面上）。xiaohongshu-mcp 项目不用 CSS selector 也不用截图——它读的是 `window.__INITIAL_STATE__.search.feeds`（React SSR 注入的内部状态）。

**修复历程（三次迭代）：**

| 版本 | 方案 | 结果 |
|---|---|---|
| v1（原始） | 纯 Vision 截图 | ❌ href 全为空，LLM 幻觉伪造 URL |
| v2（首次修复） | CSS selector `a[href*="/explore/"]` | ❌ 生产日志 `extracted 0 links`——SPA 渲染的 DOM 不匹配 |
| v3（当前） | `window.__INITIAL_STATE__` + CSS fallback + Vision fallback | ✅ 直接读框架内部数据 |

**v3 最终方案——三层降级：**
1. `_extract_search_feeds_from_state()` — 读 `__INITIAL_STATE__.search.feeds._value`，获取 `feed.id` + `xsecToken`，构造 `https://www.xiaohongshu.com/explore/{id}?xsec_token={token}`
2. `_extract_search_links_from_dom()` — CSS selector 兜底（`a[href*="/explore/"]` 等）
3. Vision only — 最后手段，加**反幻觉警告**（"不要编造链接"）

**反幻觉措施：**
- 搜索结果中明确标注"不要自己编造或猜测小红书链接和账号 ID"
- 0 链接时输出 ⚠️ 警告，建议用户自行在小红书 App 搜索
- `xhs_playwright_search` 加了输出验证 `logger.warning`（empty href 比例检测）

**实现位置：** `app/tools/xhs_ops.py` — `_extract_search_feeds_from_state()` + `_extract_search_links_from_dom()` + `_handle_xhs_search()` + `xhs_playwright_search()`

**教训：**
1. 纯 vision 方案有盲区——任何不可见的 DOM 属性（href, data-*, id 等）都无法从截图获取
2. CSS selector 对 SPA 也不可靠——React/Vue 渲染的 DOM 结构与模板不一致
3. 读框架内部状态（`__INITIAL_STATE__`）是最可靠的数据源——它是框架级结构，极少变动
4. 当工具返回空字段时，LLM 会幻觉填充看起来合理但完全错误的值（尤其是 URL）
5. **必须在工具输出中加反幻觉指令**——告诉 LLM "不要编造"比指望它自己不编造有效得多
6. **生产日志是最好的验证**——`extracted 0 links` 一行日志立刻暴露 CSS selector 方案失败
7. **看开源项目的源码而不只是 README**——xiaohongshu-mcp 的 README 说"Playwright 浏览器自动化"，实际核心是 `__INITIAL_STATE__` 而不是 DOM selector

### 记忆系统 one-size-fits-all（per-tenant 配置缺失）

**问题：** 记忆系统（ChatHistory + memory.py + build_memory_context）对所有租户使用相同配置：5 轮对话历史、1 小时 TTL、800 条日志压缩阈值。但不同类型的 bot 需要不同深度的记忆：
- kf-steven-ai（AI 分身）：需要深度记忆，记住用户偏好和历史交互
- kf-leadgen-demo（调研工具）：短期任务，不需要跨会话记忆
- code-bot（技术助手）：需要记住项目上下文，但不需要个人化记忆

另外，`save_memory`/`recall_memory` 工具虽然注册了，但 LLM 不知道什么时候该用——system prompt 中没有任何提示。`tools_enabled: []`（全启用）的租户有这些工具但不会主动使用。

**修复：**
1. `TenantConfig` 新增 5 个 per-tenant 记忆配置字段：
   - `memory_diary_enabled` — 是否写日记（每次交互后 LLM 提炼摘要）
   - `memory_journal_max` — 日志压缩阈值
   - `memory_chat_rounds` — 对话历史保留轮数
   - `memory_chat_ttl` — 对话历史 Redis TTL
   - `memory_context_enabled` — 是否在 system prompt 注入记忆上下文
2. `history.py` 改为从 tenant config 读取 chat_rounds / chat_ttl
3. `memory.py` 改为从 tenant config 读取 journal_max 压缩阈值
4. `base_agent.py` 新增 `_MEMORY_USAGE_HINT`，当 bot 有 `save_memory` 工具时自动注入提示到 system prompt
5. `_trigger_memory()` 检查 `memory_diary_enabled` 再决定是否写日记
6. `build_prompt()` 检查 `memory_context_enabled` 再决定是否注入记忆上下文

**实现位置：** `tenant/config.py` + `services/history.py` + `services/memory.py` + `services/base_agent.py`

### 小红书发帖失败（creator 子域名 + selector 过时 + 幻觉错误信息）

**问题：** bot 调用 `xhs_publish` 发帖时，Playwright 报 `Timeout 30000ms exceeded: waiting for locator("[placeholder*="标题"]").first`。LLM 看到 timeout 错误后**幻觉编造了"IP 存在风险"的故事**，实际跟 IP 风控无关。

**三层根因：**

| 层 | 问题 | 正确做法 |
|---|---|---|
| URL 错误 | 代码导航到 `www.xiaohongshu.com/publish/publish`（不存在） | 发帖在 `creator.xiaohongshu.com/publish/publish`（创作者平台，独立子域名） |
| Selector 过时 | `[placeholder*="标题"]` 匹配标准 input | 创作者平台用 `contenteditable` div，需用 `#title-input` / `.ql-editor` 等 |
| Cookie 不通 | `xhs_login` 在 www 登录的 cookie 存储时保留了 `www.xiaohongshu.com` domain | `web_session` 的实际 domain 是 `.xiaohongshu.com`（带前导点），覆盖所有子域名。保存 cookie 时统一 domain 为 `.xiaohongshu.com` |

**为什么 MCP 项目能发帖：** xpzouying/xiaohongshu-mcp (Go, 10.3k⭐) 通过 CDP 连接浏览器，login 在 `www.xiaohongshu.com/explore`，publish 在 `creator.xiaohongshu.com/publish/publish?source=official`。**Cookie 是跨子域名共享的**——`web_session` cookie 的 domain 就是 `.xiaohongshu.com`，不需要分别登录。MCP 项目用 `Network.getCookies` 导出所有 cookie（包含正确的 domain），`Network.setCookies` 恢复后自然覆盖 creator。

**修复（四次迭代）：**

| 版本 | 方案 | 结果 |
|---|---|---|
| v1（原始） | `www.xiaohongshu.com/publish/publish` + `[placeholder*="标题"]` | ❌ 页面不存在，selector timeout，LLM 幻觉"IP 风控" |
| v2（首次修复） | `creator.xiaohongshu.com/publish/publish` + 多策略 selector + cookie 注入 | ❌ cookie domain 未统一，creator 仍要求登录 |
| v3 | v2 + creator 内联 QR 登录（CSS selector + JS 文字匹配切换 QR tab） | ❌ creator 登录页 QR 入口是图标不是文字，selector 找不到 |
| v4（当前） | cookie domain 统一 `.xiaohongshu.com` + Gemini Vision 定位 QR 图标 + www QR fallback | ✅ |

**v4 方案（多层防御）：**

1. **Cookie domain 统一**：`_save_cookies_to_redis()` 保存时将所有 `xiaohongshu.com` 子域名 cookie 的 domain 统一为 `.xiaohongshu.com`。这是 MCP 项目验证过的方案——`web_session` 本身就是 parent domain cookie。如果 cookie domain 正确，导航到 creator 时浏览器自动携带，无需 QR 登录。

2. **Creator QR 切换（多策略）**：如果 cookie 仍不被 creator 接受（可能 Playwright context 行为与 CDP 不同）：
   - 策略1: CSS selector 匹配文字/class（`text=扫码登录`等）
   - 策略2: JS 遍历 DOM 找 QR 相关图标（class/src/alt 含 qr/scan/扫码）+ 登录框右上角小元素
   - 策略3: **Gemini Vision** 截图 → 用视觉分析定位 QR 按钮坐标 → `page.mouse.click(x, y)`

3. **www QR fallback**：所有 creator QR 策略失败 → 回退到 `www.xiaohongshu.com/explore` 做 QR 登录（已验证100%可靠），扫码后获取新 cookie → 统一 domain → 导航回 creator

**MCP CSS Selectors（xpzouying 10k⭐ 验证）：**
| 元素 | Selector |
|---|---|
| 标题输入 | `div.d-input input` |
| 内容编辑器 | `div.ql-editor`（Quill） |
| 图片上传 | `.upload-input` / `input[type='file']` |
| 上传完成检测 | `.img-preview-area .pr` |
| 发布按钮 | `.publish-page-publish-btn button.bg-red` |
| 话题标签 | 输入 `#` 后 `#creator-editor-topic-container .item` |
| Tab 切换 | `div.creator-tab` 文字匹配 |

**实现位置：** `app/tools/xhs_ops.py:_handle_xhs_publish()` + `_save_cookies_to_redis()`

**教训：**
1. **Cookie domain 是关键**——`web_session` 的原始 domain 是 `.xiaohongshu.com`，覆盖所有子域名。之前误以为 cookie 不通，实际是保存/恢复时 domain 丢失了前导点
2. **MCP 项目的核心不是 CDP，是 cookie domain 正确**——CDP 的 `Network.getCookies` 保留了完整 domain 信息，我们的 Playwright `context.cookies()` 也有，但 Redis 持久化时没统一
3. 创作者平台登录页的 QR 入口是**图标**不是文字——CSS/文字 selector 失败是必然的，需要 Vision 定位
4. Playwright selector timeout ≠ 风控/网络问题——**LLM 会对错误信息进行二次幻觉**
5. **多层防御**比单一方案可靠：cookie 统一（最优）→ Vision 定位（次优）→ www fallback（保底）

### `git clean` 误删 instances/ 导致容器配置丢失（严重）

**问题：** 服务器上有大量未提交的本地改动（历史遗留），执行 `sudo git clean -fd` 清理时，把 `instances/` 目录整个删了。`instances/` 包含每个租户容器的 `docker-compose.yml`、`tenants.json`、`registry.json` 和日志，全部在 `.gitignore` 里，不受版本控制。

**症状：**
- `python3 scripts/sync_instance_configs.py` 输出 `synced 0 instance(s)`（目录不存在）
- 手动重建 compose 文件时漏了 `extra_hosts: ["host.docker.internal:host-gateway"]`
- 容器启动后 Redis（Upstash REST API）连不上：`httpx.ConnectError: [Errno -2] Name or service not known`
- 根因：`.env` 的 `HTTPS_PROXY=http://host.docker.internal:...` 在 bridge 模式下无法解析

**连锁影响（CI/CD 静默失效）：**
- `instances/` 删除后，deploy.yml 的重启逻辑走 `else` 分支，只打一行 `WARN: no instances/ directory found`
- **CI/CD 显示部署成功**，但实际上没有重启任何容器 — 代码更新了但容器跑的还是旧镜像
- 表面看 Docker 日志一直在跑（因为容器本身没被停止），但新代码从未部署
- 极难排查：workflow 绿了，容器也在跑，只是没重启

**恢复步骤：**
1. `bash scripts/rebuild_instances.sh` — 自动重建所有 docker-compose.yml
2. `python3 scripts/sync_instance_configs.py` — 生成 per-container tenants.json
3. `for dir in instances/*/; do docker compose -f "$dir/docker-compose.yml" up -d; done`

**⚠️ env_file 路径必须是绝对路径且匹配服务器实际部署路径！**
- 项目在 `/opt/4dgames-feishu-code-bot/`，env_file 必须指向 `/opt/4dgames-feishu-code-bot/.env`
- `rebuild_instances.sh` 用 `PROJECT_ROOT` 自动检测，手动创建时容易写错路径（如 `/home/admin/...`）
- 路径错误时 `docker compose up -d` 报 `env file not found`，容器无法启动

**预防措施：**
- **绝对不要在服务器项目目录执行 `git clean -fd`** — 会删除所有 gitignored 的运行时文件
- 如果必须清理，用 `git checkout .`（只还原 tracked 文件的修改）而不是 `git clean`
- 或者用 `git clean -fd -e instances/ -e .env` 排除关键目录
- deploy.yml 已加保护：`instances/` 不存在时自动运行 `rebuild_instances.sh` 重建
- `rebuild_instances.sh` 自动检测 `PROJECT_ROOT`，env_file 路径不会硬编码错

**教训：** `.gitignore` 里的运行时目录（instances/、logs/）是**不可恢复的**——没有版本控制、没有备份。`git clean` 对 gitignored 文件是毁灭性的。服务器上的 git 操作要格外小心。CI/CD 的"成功"状态不等于"部署生效"——需要验证容器是否真的重启了。

### ⛔⛔⛔ 容器无 mem_limit 导致整机 hang 死（已造成多次生产事故）⛔⛔⛔

**严重程度：P0 — 2-3 个用户同时跑重度任务 → 内存耗尽 → 整机 hang（SSH 连不上）→ 只能阿里云控制台硬重启。已发生 3+ 次。**

**事故链条：**
```
多个用户同时发重度任务（Gemini 10-20 轮 tool calling, 每轮累积上下文）
  → 3 个容器同时高负载，内存持续增长
  → 无 mem_limit → 容器可以无限吃内存
  → 1.8GB 物理内存耗尽 → 疯狂 swap thrashing（磁盘 IO 100%）
  → 内核无法 fork 新进程 → SSH 超时、所有服务无响应
  → 只能阿里云控制台硬重启
```

**根因：** 生产环境的 `instances/{tid}/docker-compose.yml` 是早期手动创建或用旧版 provisioner 生成的，**没有 `mem_limit`**。后来 `provisioner.py:_generate_compose()` 和 `rebuild_instances.sh` 都加了 `mem_limit: 512m`，但没人重新生成已部署的 compose 文件。代码更新了，配置没同步。

**表现 vs xray 雪崩的区别：**
- xray 雪崩：xray 挂了 → 代理连接 hang → 连接堆积 → 资源耗尽
- mem_limit 缺失：xray 正常跑 → 纯内存不够 → swap thrashing → hang
- 两者症状一样（SSH 连不上），但 xray 日志里能看到 watchdog 重启，这次看不到

**修复（2026-03-09）：**
```bash
# 给所有容器加内存限制
for tid in kf-steven-ai code-bot pm-bot; do
  sed -i '/restart: unless-stopped/a\    mem_limit: 512m\n    memswap_limit: 768m' instances/$tid/docker-compose.yml
done
# 重启容器让限制生效
for tid in kf-steven-ai code-bot pm-bot; do
  docker compose -f instances/$tid/docker-compose.yml up -d
done
```

**效果：** 单个容器最多用 512MB 内存 + 256MB swap = 768MB。超过 → Docker OOM kill 该容器 → `restart: unless-stopped` 几秒自动拉起。整机不 hang，最多某个 bot 暂时重启。

**永久规则：**
- **所有容器必须有 `mem_limit` + `memswap_limit`** — 裸跑 = 定时炸弹
- **代码更新 compose 模板后，必须同步更新已部署的 compose 文件** — `provisioner.py` / `rebuild_instances.sh` 改了不等于生产生效
- **deploy.yml / CI/CD 更新代码后应验证运行时配置** — `docker inspect` 检查容器实际限制
- **验证方法：** `docker stats --no-stream` — MemLimit 列如果显示和物理内存一样大，说明没有限制

**教训：** 代码里的默认值 ≠ 生产环境的实际配置。模板更新后必须重新生成/部署配置文件。这和 `coding_model` 默认值的坑一样——代码改了默认值，但已部署的实例用的还是旧值。任何涉及"模板 → 生成 → 部署"的流程，更新模板后必须触发重新部署。

### 飞书 API `Bearer ` 空 token 导致 httpx 异常

**问题：** 日志中反复出现 `httpcore.LocalProtocolError: Illegal header value b'Bearer '`，大量 traceback 刷屏。

**根因：** `_get_token()` 在飞书凭证缺失或 token 获取失败时返回空字符串 `""`，`_headers()` 直接拼接为 `"Authorization: Bearer "`（末尾有空格无 token），httpx 拒绝发送此 header 并抛异常。触发场景：启动时 `precache_bot_open_ids()` 遍历租户，某租户的 `app_id`/`app_secret` 为空。

**修复：** `_headers()` 检测到 token 为空时返回空 headers + `tok_type="none"`。所有 `feishu_get/post/patch/put/delete` 在 `tok_type=="none"` 时直接返回错误字符串，不发 HTTP 请求。

**实现位置：** `app/tools/feishu_api.py` — `_headers()` + 所有 `feishu_*` 函数

**教训：** 拼接 `Authorization: Bearer {token}` 前必须检查 token 非空。httpx 对 header 值有严格校验，空 token 不会被静默忽略而是直接抛异常。

### `<tools_used>` 标签泄露到用户回复

**问题：** LLM 从 chat history 中学到了 `<tools_used>` 标签模式（由 `_enrich_reply()` 注入到历史记录中），在自己的回复中模仿输出，导致用户看到内部工具调用信息。

**修复：** 在 `_strip_hallucinated_code_blocks()` 中新增 `_INTERNAL_TAGS` 正则，回复发给用户前自动清除 `<tools_used>...</tools_used>` 标签。

**实现位置：** `app/services/base_agent.py` — `_INTERNAL_TAGS` + `_strip_hallucinated_code_blocks()`

### LLM 承诺执行但未调工具就退出（agent loop 提前终止）

**问题：** LLM 回复"我先试下"、"我来处理"等承诺性文本，但在同一个 round 里没有调用任何工具就输出了最终回复。agent loop 看到 `reply_text` 非空直接 `return reply`，用户等半天没有后续。

**症状：** 日志显示 `reply to user: ...我先试下`，之后无更多 round 日志。不是 hang，是 loop 正常退出。

**修复（两层）：**
1. **Exit gate**（已有）：`llm_exit_review()` 在 loop 返回文本前用小模型判断回复是否包含未执行的承诺。判定为"承诺未执行"时 nudge LLM 继续调用工具。
2. **空响应 nudge**（新增）：Gemini 返回空 content parts 时，不再直接 `_final_call` 退出，而是 nudge 模型继续（最多 3 次，保留工具能力）。只有连续 3 次空响应才 fallback 到 `_final_call`。

**根因分析：** exit gate 只能拦截"有文本但没调工具"的情况。还有一种更隐蔽的路径：Gemini 返回空 content parts（无文本、无工具调用）→ 代码直接调 `_final_call`（text-only，禁用工具）→ LLM 在无工具的环境下生成承诺性文本 → `return reply` 退出 → exit gate 从未运行。这条路径下 LLM 说"我马上开始修复"但工具已被禁用，永远不可能执行。

**日志特征：** `empty content parts but N tools were called, using _final_call` → 紧接着 `reply to user: ...我马上开始/我这就处理`。

**实现位置：** `app/services/gemini_provider.py` — exit gate 检查 + 空响应 nudge（`_empty_content_retries`）

### `is_retry` 变量作用域错误（xhs_search 搜索崩溃）

**问题：** `is_retry` 在 `_handle_xhs_search()` 定义（从 args 读取），但在 `_xhs_search_impl()` 中使用——两个不同的函数作用域。当搜索遇到登录墙时 `NameError: name 'is_retry' is not defined` 直接崩溃。

**症状：** kf-leadgen-demo 客户搜索 `AI 商业重构 中小企业` 时，先被登录墙拦截，然后 `is_retry` 引用未定义变量 → 整个搜索报错。LLM 只能 fallback 到 `web_search`。

**根因：** `_xhs_search_impl()` 被提取为独立函数（为了 `asyncio.wait_for` 超时控制），但 `is_retry` 没有作为参数传递。

**修复：** 给 `_xhs_search_impl()` 添加 `is_retry` kwarg，调用处传入。

**教训：** 重构函数（提取为子函数）时，必须检查所有使用的变量是否在新作用域中可访问。Python 不会在定义时报错，只在运行时该分支被触发时才 NameError。

### `__INITIAL_STATE__` Vue 响应式对象解包失败

**问题：** 小红书的 `__INITIAL_STATE__.search.feeds` 存在（key 在 Object.keys 中可见），但 `feeds._rawValue || feeds._value || feeds.value || feeds` 解包后不是数组 → 返回 null → fallback 到 CSS selector（也提取 0 链接）→ 搜索结果为空。

**可能原因：** Vue/Pinia 版本升级后响应式代理对象的内部属性变了（比如从 `_value` 变成 `__v_raw`），或数据嵌套在 `searchFeedsWrapper` 等新 key 下。

**修复：**
1. 增加 `__v_raw` 解包路径
2. 增加 `searchFeedsWrapper`、`searchFeeds`、`noteFeeds` 等备选 key 探测
3. 返回结构化 debug 信息（`{_hit, _data, _debug, _keys}`）而非简单 null，日志中能看到所有可用 keys 和匹配结果

**实现位置：** `app/tools/xhs_ops.py:_extract_search_feeds_from_state()` + `_extract_search_users_from_state()`

**教训：** 依赖框架内部状态（`__INITIAL_STATE__`）比 CSS selector 可靠，但仍需防御性编程——响应式代理对象的内部属性会随框架版本更新而变化。保留充足的 debug 日志是快速定位的关键。

### 小红书创作者平台默认手机号登录（QR 码不在默认 tab）

**问题：** `xhs_publish` 导航到 `creator.xiaohongshu.com` 登录页后，直接查找 QR 码元素（`.qrcode-img` 等），但创作者平台**默认显示手机号登录**，QR 码在旁边的 tab 上。结果：找不到 QR → fallback 到整页截图发给用户 → 用户看到的是手机号输入界面而不是 QR 码。

**区别：** `xhs_login` 在 `www.xiaohongshu.com` 操作（登录弹窗默认 QR），但 `xhs_publish` 到 `creator.xiaohongshu.com`（创作者平台，默认手机号）。两者的登录 UI 不同。

**修复：** 在查找 QR 元素前，先尝试点击"扫码登录" tab 切换：
1. Playwright locator 文本匹配（`text=扫码登录`、`text=二维码登录`）
2. CSS class 匹配（`[class*="qrcode-tab"]`、`[class*="switch-type"]`）
3. JS `textContent` 遍历点击兜底

**实现位置：** `app/tools/xhs_ops.py:_handle_xhs_publish()` 中 `creator_needs_login` 分支

### 创作者平台登录误判（SPA 渲染慢导致 false positive）

**问题：** 用户 cookie 有效（已登录 www），cookie domain 统一为 `.xiaohongshu.com` 后 creator 也能认。但导航到 `creator.xiaohongshu.com/publish/publish` 后只等 4 秒，SPA 还没渲染完 → `input[type="file"]` 和 `[contenteditable="true"]` 都不存在 → 误判为"需要登录" → 点击了一个无关的 "corner-icon" 元素 → 找不到 QR（因为根本不在登录页） → 裁剪截图发给用户（用户看到的是已登录的发布页面）→ 等待 90 秒超时。

**三个修复点：**

1. **更长的 SPA 等待**：`domcontentloaded` 后再 `wait_for_load_state("networkidle", timeout=8000)` 替代固定 4 秒。
2. **更多发布页 selector**：除了 `input[type="file"]` 和 `contenteditable`，新增 `.upload-input`、`.ql-editor`、`div.d-input input`、`.creator-tab`、`.publish-page` 等 MCP 验证过的 selector，三轮检查（每轮间隔 3 秒）。
3. **登录误判逃逸检查**：当所有 QR 策略都找不到 QR 码时，重新导航到发布页再检查一遍——如果发现发布页元素说明实际已登录，直接跳过 QR 等待流程。同时收紧登录检测条件：不仅要"没有发布元素"，还要"确实有登录表单元素"（`input[type="password"]`、`input[placeholder*="手机号"]` 等）。

**实现位置：** `app/tools/xhs_ops.py:_handle_xhs_publish()` — 登录检测 + 逃逸检查

### 小红书发帖 MCP 项目方案调研

**调研了 5 个 MCP 项目的发帖实现：**

| 项目 | 语言 | 图片来源 | 发帖方法 |
|---|---|---|---|
| xpzouying/xiaohongshu-mcp (10.3k⭐) | Go | 用户提供（URL/本地路径） | Rod 浏览器自动化 |
| betars/xiaohongshu-mcp-python | Python | 用户提供（本地路径） | Playwright |
| Gikiman/Autoxhs | Python | DALL-E 3 生成 或 用户上传 | Playwright |
| iFurySt/RedNote-MCP | TypeScript | 无发帖功能 | — |
| YYH211/xiaohongshu | Python | 委托给 xiaohongshu-mcp | 委托 |

**关键发现：**
1. **所有项目都用浏览器自动化**，没有私有 REST API
2. **图片上传用 `set_input_files()`**，selector 为 `.upload-input` 或 `input[type="file"]`
3. **XHS 要求至少 1 张图片**，只有 Autoxhs 自动生成图片（DALL-E 3）
4. **上传完成检测**：计数 `.img-preview-area .pr` 元素 = 预期图片数，60 秒超时
5. **标题 selector**: `div.d-input input`
6. **正文 selector**: `div.ql-editor`（Quill 编辑器）
7. **发布按钮**: `div.submit div.d-button-content` 或 `.publish-page-publish-btn button.bg-red`

**已应用到代码的改进：**
- 上传 selector 优先 `.upload-input`，fallback `input[type="file"]`
- 标题 selector 优先 `div.d-input input`
- 正文 selector 优先 `.ql-editor`
- 新增图片上传完成检测（`.img-preview-area .pr` 计数）

**图片自动生成（已实现）：** `xhs_publish` 不传 `images` 参数时自动生成：
1. **文字卡片**：HTML+CSS 渲染 → Playwright 截图（8 种配色方案，长文自动分页）
2. **Gemini 配图**：`gemini-2.5-flash-image` + `response_modalities=["IMAGE"]` 生成封面图
3. 组合：封面图（第 1 张）+ 文字卡片（后续页），小红书展示效果好
4. 支持 `image_prompt` 参数自定义配图提示词

**实现位置：** `app/tools/xhs_ops.py` — `_generate_text_card_image()` + `_generate_gemini_image()` + `_auto_generate_images()`

### 小红书 redCaptcha 旋转验证码

**问题：** 小红书使用自研的 redCaptcha **旋转验证码**（不是普通滑块），原代码将滑块拖到底是完全错误的。

**旋转验证码机制：**
- 一张圆形图片被随机旋转了某个角度
- 下方有水平滑块，拖动滑块旋转图片
- 需要将图片旋转到正确方向
- `drag_distance = (angle / 360) * track_width`
- DOM selector: 滑块 `div.red-captcha-slider`，图片 `div#red-captcha-rotate > img`

**修复：**
1. 检测 redCaptcha 专用 selector（`div.red-captcha-slider`, `#red-captcha-rotate`）
2. 提取旋转图片 → 发给 Gemini Vision 判断旋转角度
3. 按 `(角度/360) × 轨道宽度` 计算精确拖动距离
4. 人类模拟拖动（ease-out + y轴抖动，25-40步）
5. Fallback：整页截图 → Gemini 同时判断角度+滑块位置+轨道宽度

**参考：** SpiderAPI redCaptcha 文档、8yteDance/RotateCaptcha 项目、CSDN 旋转验证码分析

**实现位置：** `app/tools/xhs_ops.py` — `_detect_captcha()` + `_try_solve_slider_captcha()`

### 创作者平台 SMS 登录静默失败（三层问题）

**问题：** `_handle_sms_login()` 输入手机号+验证码+点登录，但页面纹丝不动——不跳转、不报错、不出 CAPTCHA。两次尝试都失败，URL 始终停留在 `creator.xiaohongshu.com/login?redirectReason=401`。

**三层根因及修复：**

| 层 | 问题 | 修复 |
|---|---|---|
| React 事件 | Playwright `fill()` 可能不触发 React onChange，内部 state 为空，点登录等于提交空表单 | 改用 `type(delay=30)` + JS `dispatchEvent(input/change)` + `nativeInputValueSetter` 确保 React state 同步 |
| 协议复选框 | 创作者平台可能有「同意用户协议」checkbox 未勾选，登录按钮点了但表单未提交 | 登录前自动 JS 扫描并勾选所有未选中的 agree/protocol checkbox |
| 验证码重用 | 第二次尝试用了第一次的旧验证码（用户回复了相同的号码），已消费的 code 无法再用 | 新增 `xhs:last_code:` Redis key 跟踪上次使用的验证码，检测到重复则提醒用户发新码 |

**额外改进：**
- 登录前打印所有 input 值（确认 React state 是否同步）
- 登录按钮选择器更精确（`[class*="login-btn"]` 优先于宽泛的 `text=登录`，避免点到导航栏文字）
- 等待时间从 2s 增加到 3s（让网络请求完成）

**实现位置：** `app/tools/xhs_ops.py:_handle_sms_login()`

### QR fallback 发送无意义截图（SMS 登录页截图 ≠ 二维码）

**问题：** SMS 登录失败后走 QR fallback，但 creator 页面的 QR tab 切换全部失败（CSS/JS/Vision 都没找到真正的 QR 切换按钮）。代码仍然截了一张全页图 → 裁剪中间 60% → 发给用户。用户收到的是 SMS 登录页面截图（不是二维码），完全没用。

**修复：**
- 删除"裁剪中心区域截图"的 fallback——如果 creator 上没找到 QR 元素，**不发截图**
- 直接跳到 `www.xiaohongshu.com` 做 QR 登录（已验证 100% 可靠、默认显示 QR）
- 只有在确认提取到了 QR 码（DOM 中有 `data:image` 或 canvas）才发给用户
- www 也失败时返回明确错误提示，不发无意义图片

**教训：**
1. 给用户发截图前必须验证截图内容是否有用——发一张 SMS 登录表单截图当"二维码"是 UX 灾难
2. QR 切换按钮可能是图标而不是文字——CSS 和文字匹配都不可靠
3. www.xiaohongshu.com 的 QR 登录比 creator 的更可靠（默认显示 QR，不需要切换 tab）

**实现位置：** `app/tools/xhs_ops.py:_handle_xhs_publish()` — QR 提取+发送部分

### 发帖流程重构为 MCP 方案（fresh context + CDP cookies）

**问题：** 原 `_handle_xhs_publish()` 在 creator.xiaohongshu.com 上尝试 SMS 登录、3 策略 QR tab 切换、Gemini Vision 定位 QR 按钮等复杂流程，全部失败。核心原因是 Playwright `context.add_cookies()` 和 CDP `Network.setCookies` 行为不同。

**MCP 方案（xpzouying/xiaohongshu-mcp 验证）：**
1. **Fresh context**：每次发帖创建独立的 `browser.new_context()`，不复用 www 的 context
2. **CDP cookie 注入**：通过 `page.context.new_cdp_session(page)` → `cdp.send("Network.setCookies")` 设置 cookie，而非 `context.add_cookies()`
3. **Cookie domain 统一**：所有 xiaohongshu.com cookie 的 domain 统一为 `.xiaohongshu.com`（带前导点），确保 creator 子域名能收到
4. **无 creator 登录**：不在 creator 上做任何登录操作。cookie 来自 www 的 `xhs_login`，通过 CDP 注入到 fresh context 后直接访问 creator
5. **Publish URL**：使用 `creator.xiaohongshu.com/publish/publish?source=official`（带 `?source=official`）

**重构前 vs 重构后：**

| 项目 | 重构前 | 重构后 |
|---|---|---|
| Context | 复用 session.context | 新建 creator_context |
| Cookie 设置 | `context.add_cookies()` | CDP `Network.setCookies` |
| Creator 登录检测 | ~400 行（SPA 等待 + 登录表单检测） | ~30 行（简单 URL 检查） |
| SMS 登录 | 完整实现（React 事件 + 验证码） | 删除（不需要） |
| QR tab 切换 | 3 策略（CSS/JS/Vision） | 删除（不需要） |
| QR fallback | creator QR → 裁剪截图 | 直接去 www QR |
| 代码量 | ~400 行登录流程 | ~80 行 |

**Session 管理：**
- `creator_context` 和 `creator_page` 存储在 `_XhsSession` 上
- `xhs_confirm_publish` 优先使用 `session.creator_page`
- 发布完成或出错后自动 `creator_context.close()`
- `_cleanup_xhs_session` 也会清理 creator_context

**实现位置：** `app/tools/xhs_ops.py:_handle_xhs_publish()` + `_handle_xhs_confirm_publish()` + `_XhsSession`

### Dashboard 日志永远静态 — uvicorn propagate + bridge 网络双重陷阱（5 次迭代才修复）

**严重程度：** 非功能性 bug，但极其难排查，5 次迭代才找到根因。

**症状：** Dashboard 的 Logs 页面永远显示相同内容，auto-refresh 有效（时间戳在变）但日志内容不更新。

**5 次迭代历程：**

| 次数 | 方案 | 结果 | 失败原因 |
|---|---|---|---|
| 1 | `RotatingFileHandler` + 每次 `flush()` | ❌ 静态 | uvicorn access/error logger 设置 `propagate=False`，日志不到 root handler |
| 2 | `flush()` + `os.fsync()` + `O_RDONLY` 读 | ❌ 静态 | 同上，问题不在文件系统缓存 |
| 3 | startup 时把 file handler 挂到 uvicorn loggers | ❌ 未生效 | `uvicorn.run()` 内部调 `dictConfig()` 会重置 logger handlers |
| 4 | `uvicorn.run(log_config=...)` 自定义 dictConfig | ⚠️ 文件有写入但 dashboard 仍显示"静态" | 跨容器读文件有延迟/不一致 |
| 5 | 内存 `LOG_BUFFER` (deque) + 本地 tenant 直读 | ✅ 实时 | 彻底绕过文件系统 |

**三层根因：**

1. **uvicorn `propagate=False`**：uvicorn 的 `uvicorn.access` 和 `uvicorn.error` logger 默认 `propagate=False`。即使 root logger 配了 file handler，uvicorn 的日志（包括 health check 等高频日志）也不会到 root → 不会写入文件。必须通过 `uvicorn.run(log_config=...)` 的 `dictConfig` 来配置 uvicorn 自己的 handlers。

2. **Docker bridge 网络下 HTTP 自代理失败**：Dashboard 运行在 code-bot 容器（bridge 模式）。查看 code-bot 自己的日志时走 `get_instance_logs("code-bot")` → HTTP 代理到 `127.0.0.1:8101`。但 bridge 模式下 `127.0.0.1` 是容器内部 loopback，端口 8101 不存在（uvicorn 监听 8000，Docker 在宿主机做 8101→8000 映射）。`host.docker.internal:8101` 理论上可行但实测也失败 → 全部 fallback 到读文件。

3. **文件读取天然有延迟**：即使 uvicorn 写了文件，跨容器读 `instances/{tid}/logs/bot.log`（bind mount）仍有文件系统缓存/延迟问题，导致 dashboard 显示的内容看起来"不更新"。

**最终方案 — 内存环形缓冲区：**
```python
# app/main.py
LOG_BUFFER: deque = deque(maxlen=3000)  # 内存环形缓冲区

class _BufferLogHandler(logging.Handler):
    def emit(self, record):
        LOG_BUFFER.append(self.format(record))

# startup 时挂到 root + uvicorn loggers
# uvicorn.run(log_config=_UVICORN_LOG_CONFIG)  # 文件写入也保留
```

```python
# app/admin/routes.py — api_instance_logs()
# 关键：如果 tenant 在当前容器运行，直接读 LOG_BUFFER，不走 HTTP 代理
if tenant_registry.get(tenant_id) is not None:
    return await api_self_logs(...)  # 读内存，零延迟
# 否则走 provisioner 三层获取（HTTP 代理 → 文件 → docker logs）
```

**实现位置：**
- `app/main.py` — `LOG_BUFFER` + `_BufferLogHandler` + `_UVICORN_LOG_CONFIG`
- `app/admin/routes.py` — `api_self_logs()`（读 LOG_BUFFER）+ `api_instance_logs()`（本地 tenant 直读）
- `app/services/provisioner.py` — `_fetch_logs_via_http()`（跨容器 HTTP 代理 + 诊断日志）
- `app/admin/dashboard.html` — stats 显示 `log_source` / `buffer_size` / 实时时间

**教训：**
1. **uvicorn 日志不走 root logger** — `propagate=False` 是 uvicorn 的默认行为，不能指望 root handler 收到 uvicorn 日志。必须用 `log_config` 参数配置
2. **Bridge 网络下容器不能 HTTP 自代理** — `127.0.0.1:{外部端口}` 在容器内不通。即使有 `host.docker.internal`，也不能假设一定能用。最可靠的方式是检测 tenant 是否在本进程中，直接读内存
3. **文件系统不适合实时日志展示** — 写入延迟、缓存、bind mount 一致性等问题太多。内存 deque 是最简单可靠的方案
4. **诊断信息要暴露到前端** — 5 次迭代中前 4 次都在盲猜，加了 `log_source` / `buffer_size` / `errors_tried` 后立刻定位到"HTTP 代理失败 → 读文件"的问题
5. **同一问题可能有多层根因** — uvicorn propagate 是第一层，bridge 网络是第二层，文件延迟是第三层。只修一层看起来没效果，容易误判为"修不好"

### ⛔ Agent 强制截断是反模式（stall detection + budget timeout 全部移除）

**严重程度：P1 — 多次导致 bot 任务做一半就停，用户体验极差。**

**事故场景（2026-03-08）：** 用户让 pm-bot 从 Google Sheets 读取 GDC 活动列表并创建飞书日历。bot 创建了 5 个日历事件后被 stall detection 杀掉（`create_calendar_event` 连续调用 5 次触发阈值）。bot 在 `_final_call` 中回复"马上就好"然后沉默。用户连发 3 次"继续"才勉强完成部分任务。

**连锁问题：**
1. **stall detection 杀正常操作** — 批量创建日历事件就是需要连续调 `create_calendar_event` N 次，这不是空转
2. **budget timeout 截断复杂任务** — 90 秒对"读表格→筛选→创建 20+ 日历事件"的任务远远不够
3. **`_final_call` 幻觉** — 被截断后 LLM 说"马上就好"或"正在处理"，但实际 agent loop 已经退出了
4. **"继续"循环** — 用户被迫反复说"继续"，每次 bot 重新读数据+重新开始，效率极低
5. **硬编码进度消息不应景** — "我去搜一下相关信息"出现在创建日历事件的场景中，完全不匹配

**之前的错误设计（K2.5 时代遗留）：**

| 机制 | 问题 |
|---|---|
| `_detect_stall` | 同一工具调用 5 次就杀。批量操作（创建日历/写文档/搜索）天然需要重复调用 |
| Budget timeout | normal=90s, research=300s。关键词分类经常误判（"继续"→"quick"→20s 预算！） |
| `_final_call` 强制总结 | 被截断后 LLM 幻觉"已完成"或"马上就好"，承诺不兑现 |
| 硬编码进度消息 | LLM 生成失败时 fallback 到固定模板，场景完全不匹配 |

**修复（全面移除强制截断，信任模型）：**

| 改动 | 说明 |
|---|---|
| 移除 `_detect_stall` | 完全删除。从 gemini_provider.py、kimi_coder.py、sub-agent runner 三处移除 |
| 移除 budget timeout | 完全删除 `_TASK_BUDGET`、`get_task_budget`、`get_task_stall_multiplier`。不再有时间限制 |
| 保留 `_MAX_ROUNDS=50` | 纯安全网，50 轮足够完成任何合理任务 |
| 进度消息改 LLM-only | `_generate_progress_hint` 失败时返回 None（不发消息），不再 fallback 到硬编码模板 |
| 进度消息放宽频率 | 首条从 15s+2轮 改为 30s+3轮，后续从 40s 改为 60s |

**设计原则（新）：**
- **信任模型**：Gemini 3 flash/pro 有自然的任务完成感知，不需要外部强制截断
- **成本控制靠配额**：per-tenant token 配额 + per-user 6h 限额已经覆盖成本风险
- **Claude Code 参考**：CC 没有 stall detection，让模型自己决定何时完成，用户可随时中断
- **批量操作是正常的**：创建 20 个日历事件就是要调 20 次 `create_calendar_event`，这不是 bug

**实现位置：** `base_agent.py`（常量+函数删除）+ `gemini_provider.py`（loop 简化）+ `kimi_coder.py`（stall 删除）

### 工具返回值截断导致 LLM 幻觉 URL（`_MAX_TOOL_RESULT_LEN`）

**问题：** Google Sheets CSV 导出 ~12000 字符，被 `_MAX_TOOL_RESULT_LEN=8000` 截断。后半部分的活动 URL 丢失，LLM 在创建日历事件时**编造了看起来合理但完全错误的 Eventbrite URL**。

**症状：** 日历事件中 Pocket Gamer 活动链接为 `eventbrite.co.uk/e/pocket-gamer-mixmob-gdc-party-tickets-818648173547`（假的），实际应为 `eventbrite.co.uk/e/1978098881002/`。用户点链接 404。

**根因：** `_MAX_TOOL_RESULT_LEN` 截断 + LLM 擅长编造看起来合理的 URL（正确的域名+合理的路径格式+随机 ticket ID）。

**修复：**
1. `_MAX_TOOL_RESULT_LEN` 从 8000 提升到 16000 — 数据完整性比省 token 重要
2. 反幻觉指令更新：明确要求"所有链接必须原样复制自工具返回的数据"，不再只说"搜索结果"

**教训：**
1. 工具返回值截断是 LLM 幻觉的直接诱因 — 数据不完整时 LLM 会用"合理推测"填补空白
2. URL 幻觉特别危险 — 格式正确但内容错误的 URL 用户无法一眼识别
3. 宁可多用 token 也不要截断数据 — 错误的输出比没输出更糟

**实现位置：** `base_agent.py:_MAX_TOOL_RESULT_LEN` + `base_agent.py:_INSTRUCTIONS`

### 硬编码进度消息不应景（LLM fallback 方案失败）

**问题：** `_generate_progress_hint` 用 gemini-2.0-flash 生成人味进度消息，但 3 秒超时内经常失败（代理延迟），fallback 到 `_build_progress_hint` 的硬编码模板。硬编码消息基于工具名简单匹配类别，场景经常不匹配：
- bot 在创建日历事件 → 发了"我去搜一下相关信息"（因为之前调过 web_search）
- bot 在处理表格数据 → 发了"还在搜，找到一些了"

**修复：**
1. LLM 超时从 3s 提升到 5s（减少 fallback 频率）
2. **彻底删除硬编码 fallback** — `_generate_progress_hint` 失败时返回 None，不发消息
3. 进度消息发送条件放宽（30s+3轮 / 60s间隔），减少不必要的消息

**设计原则：** 宁可不发进度消息，也不发与场景不匹配的硬编码消息。不匹配的消息比沉默更让用户困惑。

**实现位置：** `base_agent.py:_generate_progress_hint` + `gemini_provider.py` 进度发送逻辑

### URL 溯源验证器（结构性防 URL 幻觉）

**问题：** Prompt 约束（"绝对禁止编造 URL"）是软限制，LLM 从根本上不擅长精确复制字符串。即使反幻觉指令写得再清楚（甚至举了 eventbrite 的例子），LLM 仍然会编造看起来合理但完全错误的 URL。Google Sheets 日历事件、小红书搜索结果等场景反复出问题。

**根因：** 这不是 prompt 写得不够好的问题，而是 LLM 架构的固有限制 —— 概率模型不等于精确复制。方向错了：加更多 prompt 约束不会根本解决。

**修复（结构性防护）：** URL 溯源验证器 —— 在代码层面拦截幻觉 URL，不依赖 LLM 自律。

**机制：**
1. **收集阶段**：每次工具返回结果后，从 `result_str` 中提取所有 URL，加入 `_seen_urls` 集合。用户消息和对话历史中的 URL 也加入
2. **验证阶段**：LLM 生成写操作（`update_calendar_event`、`create_calendar_event`、`send_mail` 等）的 tool call 时，提取参数中的所有 URL，检查是否在 `_seen_urls` 中
3. **拦截**：未见过的 URL → 不执行工具，返回错误让 LLM 用真实 URL 重试

**白名单（不检查的工具）：** `fetch_url`、`browser_open`、`web_search`、`think`、`test_custom_tool` 等（参数中的 URL 是用户提供的，不是 LLM 生成的）

**写操作白名单（必须检查的工具）：** `update_calendar_event`、`create_calendar_event`、`create_document`、`send_mail`、`add_bitable_record` 等

**三级判定：**
1. **精确匹配**（规范化后）→ 放行
2. **前缀/反向前缀匹配**（LLM 加/删 query param）→ 放行
3. **域名匹配**（同域名但完整 URL 没见过）→ **软警告**（⚠️，可能是从 ID 构造的合法 URL）
4. **完全未见**（域名也没有）→ **硬拦截**（⛔，大概率幻觉）

**防误杀机制：**
- **URL 规范化**：去尾部斜杠、大小写统一、URL decode、去 utm_*/fbclid 等追踪参数、去 fragment
- **域名降级**：同域名不同路径只警告不硬拦截（防止从 ID 构造 URL 的场景被误杀，如 `xiaohongshu.com/explore/{note_id}`）
- **死循环保护**：同一 URL 被拦截 1 次后记入 `_blocked_urls`，第二次直接放行（防止 LLM 反复重试同一个幻觉 URL 浪费 rounds）
- **历史 URL 种子**：从 `history`（对话历史）中提取所有 URL 加入 `_seen_urls`，减少跨轮对话的误杀
- `_seen_urls` 为空时不检查（对话刚开始，没有参考数据）

**覆盖范围：** 主 agent loop + sub-agent loop 均已集成

**实现位置：** `base_agent.py`（`extract_urls` + `check_url_provenance` + `_normalize_url`）+ `gemini_provider.py`（主 agent + sub-agent loop 中集成）

**测试：** `tests/test_url_provenance.py`（32 个测试用例：提取、规范化、精确匹配、域名降级、死循环保护、前缀匹配）

**教训：**
1. Prompt 约束是软限制 —— 对"不要做 X"类规则，LLM 遵从率永远到不了 100%
2. 结构性防护 > prompt 约束 —— 代码层拦截比指令更可靠
3. 数据溯源是正确方向 —— URL 幻觉的本质是"数据凭空出现"，验证数据来源是最自然的防护
4. 结构性防护也需要防误杀 —— 硬拦截会导致死循环和误伤合法 URL，必须分级（硬拦截/软警告/放行）+ 死循环保护

### 日记系统不保存数据源 URL（跨对话失忆）

**问题：** 用户发 Google Sheet URL → bot 用 `fetch_url` 成功读取 → 下一轮对话用户说"照着我给你发的 google sheet" → bot 从 50 条聊天记录里猜 URL → 选了错误的 Sheet → 404 → 放弃。

**根因链：**
1. `_extract_outcome(fetch_url, result)` 只看工具返回内容（CSV 数据），不记录**调用参数**中的来源 URL
2. `_trigger_memory` 有 `action_outcomes` 参数但**没传给 `write_diary`**
3. `write_diary` → `_llm_diary_entry` 只看 `user_text` + `reply` + 工具名列表，完全没有工具返回结果的 URL
4. 日记写"读取了 GDC 活动数据"但没保存 Sheet URL → 下次 `recall_memory` 找不到

**修复（三处）：**
1. `_extract_outcome` 对 `fetch_url` 特殊处理：记录来源 URL（`func_args["url"]`）而非返回内容中的 URL
2. `_trigger_memory` 把 `action_outcomes` 传给 `write_diary`
3. `write_diary` → `_llm_diary_entry` 把 outcomes 拼进 LLM prompt（"工具执行结果"段），让日记 LLM 能看到完整的 URL/ID 等关键数据

**效果：** 日记从 `{"s":"读取了GDC活动数据"}` 变成 `{"s":"从 https://docs.google.com/spreadsheets/d/19kK0V.../export?format=csv 读取了GDC活动列表"}`。下次 `recall_memory("google sheet")` 就能精确召回。

**实现位置：** `base_agent.py:_extract_outcome()` + `base_agent.py:_trigger_memory()` + `memory.py:write_diary()` + `memory.py:_llm_diary_entry()`

### ⛔ Google Sheets CSV 截断导致 LLM 整页编造数据（严重）

**严重程度：P0 — 模型看不到用户需要的数据，会编造活动名、日期、地点、链接，全部是假的。用户无法分辨真假。**

**事故经过（2026-03-08）：** 用户让 bot 从 GDC Party 表格中挑选 3 月 12 日之后的活动创建日历。bot 创建了 5 个活动，活动名/日期/链接全部是编造的。

**根因：**
- 该 Google Sheet 有 732 行 / 204,110 字符
- `_try_google_doc_export` 截断到 16,000 字符 = 只有前 62 行可见
- **"March 12" 在 CSV 中第一次出现在 position 18,179** — 完全超出截断窗口
- 模型看不到任何 March 12+ 的数据，但不会说"我看不到"，而是从上下文/记忆中编造

**错误排查路径（教训）：**
1. 最初以为是 `=HYPERLINK()` 公式导致 URL 丢失 → 写了 HTML 导出提取方案
2. 发现 Google Sheets `format=html` 返回 400 → 方案根本不工作
3. 实际测试后发现：URL 是纯文本不是 HYPERLINK 公式，CSV 里本来就有；问题是整个 CSV 太大被截断了

**如果一开始就跑这三个测试（<30 秒），就能立刻定位根因：**
```python
# 1. CSV 有多大？
resp = await client.get(export_url)
print(len(resp.text))  # → 204110（远超 16000 截断）

# 2. 用户要的数据在哪？
print(resp.text.find("March 12"))  # → 18179（超出截断点）

# 3. format=html 能用吗？
html_resp = await client.get(html_url)
print(html_resp.status_code)  # → 400（不支持）
```

**修复：** `fetch_url` 新增 `offset` 参数，支持分页读取大文档。
- 截断消息明确告诉模型如何翻页：`要读取下一页，请调用 fetch_url 并设置 offset=15000`
- 反幻觉警告：`绝对不要猜测或编造你没看到的内容`
- 每页 15000 字符，模型需要多次调用才能读完大表格

**实现位置：** `app/tools/web_search.py`（offset 参数）+ `app/tools/browser_ops.py`（分页逻辑）

**教训：**
1. **遇到数据问题，第一步是用实际数据跑一遍，不是看日志猜** — 对着日志推理推出的因果链是错的
2. **Google Sheets 不支持 `format=html` 导出** — 只支持 csv/tsv/xlsx/pdf/ods
3. **204K CSV 截断到 16K = 丢失 92% 数据** — 模型不会说"我看不到"，会编造看起来合理但完全错误的数据
4. **gviz/tq API** — `docs.google.com/spreadsheets/d/{id}/gviz/tq?tqx=out:csv` 支持 SQL 查询过滤，但对表格结构有要求
5. **永远不要假设 LLM 会承认数据不足** — 截断警告必须在工具返回中用强制指令告诉模型

### action-claim 检测器误杀完成总结（false positive）

**问题：** 模型执行了 20+ 工具调用（删除旧日程 + 创建新日程），写完成总结时说"接下来我会把群聊邀请进去"，被 promise 模式（空 frozenset）匹配为"空承诺" → nudge 打回 → 浪费 3 轮。

**根因：** promise 模式 `(我[去就来]|接下来我).{0,10}(做|创建|加)` 不区分"刚开始什么都没做就承诺"和"做了大量工作后在总结中提到下一步"。

**修复：** 如果 `tool_names_called` 已有 ≥5 次调用，跳过 promise 模式（模型显然在做事，不是空承诺）。

**实现位置：** `app/services/base_agent.py:detect_action_claims()`

### 进度消息 / exit gate 使用不存在的模型（gemini-2.0-flash）

**问题：** `_generate_progress_hint` 和 `llm_exit_review` 用 `gemini-2.0-flash` 模型，但 CF Worker 代理可能不路由这个模型。结果：
- 进度消息 5 分钟一条都没发（LLM 生成全部超时返回 None）
- exit gate 每次超时 → 之前 fail-closed 导致误 nudge（已改为 fail-open 但仍浪费时间）
- `logger.debug` 在生产不输出，失败完全不可见

**修复：**
1. 模型改为 `gemini-3-flash-preview`（与主 agent 一致，确认代理可用）
2. `logger.debug` → `logger.info`（失败可见）

**实现位置：** `app/services/base_agent.py`

**永久规则：** 辅助 LLM 调用（进度消息、exit gate、意图分类）必须使用与主 agent 相同的模型或确认代理支持的模型。不要随便用 `gemini-2.0-flash` 等未验证的模型名。

### 日历时间解析不支持 IANA 时区名

**问题：** LLM 传 `2026-03-12 16:00 America/Los_Angeles`，`_parse_time` 只匹配固定的 `%Y-%m-%d %H:%M` 格式，尾部的时区名导致 strptime 全部失败 → 日历事件创建报错。

**修复：** `_parse_time` 用正则检测尾部 IANA 时区名（如 `America/Los_Angeles`），提取为 `ZoneInfo`，strip 后正常解析。

**实现位置：** `app/tools/calendar_ops.py:_parse_time()`

### 大型 Google Sheets 逐页翻阅导致 URL 丢失和数据编造

**问题：** 204K 字符的 Google Sheet CSV 按 15K 每页分页，模型需要翻 7 页才能读完。但模型实际上跳页阅读（offset 0→40000→100000→140000→180000），只读了 41% 的数据。到创建日历事件时，早期页面的 URL 已远离上下文窗口，模型无法精确回忆 → 编造看起来合理但完全错误的 URL。

**根因：** 逐页翻阅大型 CSV 是错误的方法。7 次 API 调用读数据 + 跳页遗漏 + 上下文衰减 = URL 必然丢失。

**修复：** 添加 `query` 参数支持 Google Visualization Query（gviz/tq），在服务端过滤行再返回。
- `fetch_url(url="...", query="select * where B contains 'March 12'")` → 只返回匹配行
- 结果通常几 K 字符，一页搞定，URL 完整保留在上下文中
- 第一页截断时自动提示模型用 query 参数而非继续翻页
- gviz/tq 失败时自动回退到全量 CSV 导出

**实现位置：** `app/tools/browser_ops.py:_try_google_doc_export(query=)` + `app/tools/web_search.py`（fetch_url 新增 query 参数）

**教训：**
1. 大数据 + 分页 + LLM = 数据丢失是必然的，不是偶然的
2. 让服务端过滤远比让 LLM 翻页可靠 — SQL 查询的精确性远超 LLM 的记忆力
3. 不要逐页翻 200K 文档找 10 行数据 — 这就像用肉眼翻电话簿找人

### 日历事件时区错误（活动在旧金山但时间按上海）

**问题：** 用户说"三番时间的活动"，模型传 `start_time='2026-03-11 17:00'` 但不带时区。`_parse_time` 默认用 `_get_user_tz()`（Asia/Shanghai）。结果：17:00 PST 的活动被创建为 17:00 CST（早了 16 小时）。

**根因：** `create_calendar_event` 工具没有 `timezone` 参数。虽然 `_parse_time` 已支持 IANA 时区名后缀，但模型没有动力（也不知道需要）在时间字符串后追加时区名。

**修复：**
1. `create_calendar_event` 新增 `timezone` 参数（IANA 格式，如 `America/Los_Angeles`）
2. 工具 description 明确提示：如果活动不在用户所在时区，必须设置 timezone
3. `create_event()` 接收 timezone 后追加到时间字符串，由 `_parse_time` 解析
4. 支持 IANA 时区名和城市名映射（`_CITY_TZ_MAP`）

**实现位置：** `app/tools/calendar_ops.py` — create_calendar_event tool definition + `create_event()` function

### 进度消息 3.5 分钟内零条发送（静默失败）

**问题：** agent loop 跑了 12 轮 / 3.5 分钟，用户收到零条进度消息。条件（30s+3轮）在 round 5 就满足了，但没有任何日志表明 `_generate_progress_hint` 被调用或失败。

**可能根因：**
1. `_generate_progress_hint` 生成的中文文本 > 50 字符（原始限制），被静默丢弃（无日志）
2. CF Worker 代理对并发请求有限制，主 LLM 调用刚返回就发进度 hint 请求，可能被限流
3. 进度 hint 5 秒超时内 CF Worker 未响应

**修复：**
1. 进度 hint 入口处添加显式日志（`progress hint: attempting`）
2. 返回 None 时添加日志（`progress hint: LLM returned None`）
3. 文本长度上限从 50 放宽到 80，超长时智能截断到第一个逗号/顿号而非直接丢弃
4. 发送成功时记录实际文本（`progress hint: sent '...'`）

**实现位置：** `app/services/gemini_provider.py`（进度检查入口日志）+ `app/services/base_agent.py:_generate_progress_hint()`（长度放宽+截断+日志）

**教训：** 静默返回 None 是调试噩梦。任何可能失败的路径都要有日志，特别是涉及 LLM 调用的辅助功能。

### Bot 幻觉完成：声称"搞定了"但没调 write_file（严重）

**问题：** 用户让 bot 改代码（加个布尔开关控制 UI 动画），bot 读了文件 13 轮，回复"搞定了"，但日志中没有任何 `write_file` 或 `git_commit` 调用。exit gate 在 round 9 检测到问题并 nudge，但 bot 只是又去读了文件就退出了。后续交互中 bot 又新建了分支 `feat/push-task-ui-toggle-v2` 而不是复用已有的 `fix/taskui-not-moving`。

**三层根因：**

| 层 | 问题 | 修复 |
|---|---|---|
| Action claim 检测盲区 | `_ACTION_CLAIM_PATTERNS` 没有匹配"搞定了/改完了/做完了"等完成声称 | 新增模式：`(搞定\|改完\|做完\|写完\|弄完)了` → 需要 write 类工具 |
| Read-without-write 检测漏洞 | `read_file` 从 `_READ_WRITE_PAIRS` 移除（怕误判），但代码修改场景确实需要检测 | 新增上下文感知检测：用户消息含修改意图关键词（改/修复/加个/fix）且只调了 read_file 没调 write_file → nudge |
| 分支管理无规范 | bot 不检查已有分支就随意创建新分支 | system prompt 新增分支管理规范：先 `git_list_branches` 检查已有分支，有则复用，无则新建 |

**附带问题：**
- Intent classification 反复返回 `'Here is'` 而非 JSON — CF Worker 代理可能不传递 `response_mime_type`。修复：JSON 提取 fallback（regex `\{[^}]+\}`）
- Empty content parts 浪费 2 轮 — 已有 nudge 机制，但本次效果不佳

**实现位置：**
- `base_agent.py:_ACTION_CLAIM_PATTERNS` — 新增"搞定了"模式
- `base_agent.py:_has_unmatched_reads()` — 新增 `user_text` 参数 + `_CODE_MODIFY_INTENT` 正则
- `base_agent.py:_FULL_ACCESS_ADDENDUM` — 新增分支管理规范
- `gemini_provider.py:_classify_intent_llm()` — 新增 regex JSON 提取 fallback

**教训：**
1. "搞定了"是中文里最常见的完成声称之一，action claim 检测必须覆盖
2. `read_file` 不能一刀切移除——需要根据用户意图区分"读来看看"和"读来改"
3. 分支管理需要显式指令，模型不会自动检查已有分支
4. `response_mime_type="application/json"` 经过代理后可能不生效，必须有 JSON 提取 fallback

### _classify_intent_llm fallback 改变工具加载行为（语音消息罢工）

**严重程度：P1 — 所有语音消息处理失败，用户反复收到"处理超时了"。**

**事故经过（2026-03-24）：** 客户反馈智能体"罢工"，发语音消息全部超时无响应。文本消息正常。

**根因链：**
1. CF Worker 代理不传递 `response_mime_type="application/json"` → Gemini 对意图分类请求返回 `"Here is the"` 而非 JSON
2. `_classify_intent_llm` JSON 解析失败 → 走 fallback
3. **关键变化（05654fa, 2026-03-18）：** fallback 从 `return None` 改为 `return _classify_intent_keywords(user_text)`
4. 对语音消息 `"[语音消息] 请听取并理解这段语音"` 的影响：
   - **改之前：** `None` → `_llm_groups=None` → `_get_tenant_tools` 走 `elif user_text:` 分支 → 关键词不匹配 → **加载全部工具** → 语音正常处理
   - **改之后：** `{"groups":["core"]}` → `_llm_groups={"core"}` → `_get_tenant_tools` 走 `elif suggested_groups:` 分支 → **只加载 core 工具组** → agent 缺少必要工具 → 处理失败/超时

**修复（两层）：**
1. 语音消息（检测 `[语音消息]`/`[音频]`）直接跳过 LLM 意图分类，返回 `{type: "normal", groups: ["core", "research"]}`
2. 分类 prompt 尾部追加 `"Reply ONLY with valid JSON"` 防御 CF Worker 不传 `response_mime_type`

**实现位置：** `app/services/gemini_provider.py:_classify_intent_llm()`

**教训：**
1. **改 fallback 行为时必须检查下游影响** — `None` 和 `{"groups":["core"]}` 虽然都是 fallback，但对工具加载的影响完全不同（全量 vs 只加 core）
2. **意图分类器不能处理非文本输入** — 语音消息的文本只是指令，不代表用户真正的意图，应直接跳过分类
3. **CF Worker 代理是意图分类的单点故障** — `response_mime_type` 不被传递会导致每次分类都 fallback

## Pending Tasks

### Phase 1.5 — 运维加固（优先级最高）
- [ ] 清理遗留容器 — `4dgames-feishu-code-bot-bot-1` 已被 provisioned 容器替代，应停止并移除
- [ ] 健康监控/告警 — 3 个容器目前只有 /health，无主动告警（容器挂了无人知道）
- [ ] 日志持久化 — Docker 日志无持久化，容器重建即丢
- [ ] 容器自动重启 — 确保 restart policy（restart: unless-stopped）

### Phase 2 — 产品化
- [x] Admin Dashboard — 管理面板 UI ✅（`/admin/dashboard`，Tailwind + vanilla JS）
- [x] 试用期系统 — 新用户自动 trial，到期 admin 手动 approve/block ✅
- [x] 每用户 6h 限额 — 滑动窗口限流，防止单用户刷爆 ✅
- [ ] 自助注册流程 — 客户自助开通 bot 实例
- [ ] 龙虾化（OpenClaw 品牌人设）— system prompt、回复风格调整

### GTC Agent 研究借鉴（2026-03-17 调研）

**来源：NVIDIA GTC 2025 + GTC 2026 agent 相关发布和演讲**

- [ ] Sub-Agent 分解 — 把 40+ 工具的单一 agent loop 拆成专业子 agent（文档 agent、调研 agent、日历 agent 等），由编排 agent 协调分发。NVIDIA 推荐的 scale 模式，能解决工具太多导致选择不准、上下文膨胀的问题。参考 NeMo Agent Toolkit 的 composability 设计（agent/tool/workflow 统一为 function call）
- [ ] Hybrid Model 路由增强（AI-Q 模式）— 当前 flash→pro 按 round 6 被动升级。改为主动拆分：贵模型（Pro）做编排决策和工具选择，便宜模型（Flash）做数据检索和子任务执行，成本降 50%+。参考 NVIDIA AI-Q Blueprint 的 frontier+research 双模型架构
- [ ] Data Flywheel 工具路由蒸馏 — 收集生产环境 tool calling 日志（metering 系统已有数据）→ 蒸馏大模型到小模型做工具路由 → fine-tune → 部署。NVIDIA 验证 1B 蒸馏模型达到 70B 模型 98% 的工具选择准确率。比截断 tool result 省 token 更根本。参考 NVIDIA Data Flywheel Blueprint（GitHub: NVIDIA-AI-Blueprints/data-flywheel）
- [ ] Per-Tool 可观测性（OpenTelemetry）— 当前 metering 只到 per-tenant 粒度。需要加 per-tool 粒度的 token 用量、延迟、成功率、调用频次监控。NeMo Agent Toolkit 用 OpenTelemetry 导出到 Phoenix/Langfuse/Weave。能精准定位 40 个工具里哪个最烧 token、哪个最慢、哪个失败率最高
- [ ] Auto-fix 策略引擎升级（OpenShell 模式）— 当前 allowlist 是全局硬编码（`_ALLOWED_WRITE_PATHS`）。NVIDIA OpenShell 用 YAML 策略文件 per-tenant 配置可访问的文件/网络/服务，支持运行时动态更新。可以做成 per-tenant 安全策略，不同 bot 有不同的 auto-fix 权限边界
- [ ] Privacy Router — NVIDIA OpenShell 的隐私路由：敏感数据走本地模型，非敏感走云端 frontier 模型。可用于处理用户隐私数据时自动切换到本地部署的 Nemotron/Llama 模型，非敏感对话继续用 Gemini
- [ ] Nemotron 开源模型评估 — NVIDIA 发布了 Nemotron 3 Super 120B（Hybrid Mamba-Transformer MoE），专为 agentic reasoning 优化。Mamba 线性注意力处理长 context 的 agent loop 比纯 Transformer 成本低。评估是否可以替代部分 Gemini 调用（特别是工具路由和子任务执行）

### 功能迭代
- [ ] Capability Acquisition Layer 部署验证 — env_ops / browser_ops / capability_ops 已合入代码，需在生产环境测试
- [ ] Playwright 部署 — 服务器上运行 `playwright install --with-deps chromium`
- [ ] 自我修复适配阿里云 — 原 Railway API → Docker restart 方式
- [ ] tenants.json 热加载 — 改配置要重启 → file watcher / API 热加载
- [ ] Gemini API 调用调试 — Cloudflare Worker 代理层问题排查
- [ ] Vertex AI 迁移 — 去掉 CF Worker 代理，直连 GCP。需要：创建 GCP 项目 → 启用 Vertex AI API → 选区域（asia-southeast1 新加坡推荐）→ 创建 Service Account（Vertex AI User 角色）→ 下载 JSON Key。代码改动：`genai.Client(vertexai=True, project=..., location=...)` + `_use_file_api=True`（File API 可用，大媒体不再走 inline_data）

### 已完成
- [x] ~~Phase 1 容器隔离~~ — 3 个 bot 各自独立 Docker 容器（8101/8102/8103）✅
- [x] ~~Capability Acquisition Layer 代码~~ — env_ops + browser_ops + capability_ops + sandbox 动态白名单 ✅
- [x] ~~CI/CD 容器级部署~~ — deploy.yml 自动逐个重启 provisioned 容器 ✅
- [x] ~~HTTPS/域名~~ — SSL 模板 + certbot 脚本已完成，需服务器上执行 `setup_ssl.sh`
- [x] ~~微信客服 session state 管理~~ — state=3 卡死修复 + 状态转接 + welcome_code 处理
- [x] ~~欢迎语 LLM 化~~ — 去掉硬编码回复，enter_session 走 LLM 生成
- [x] ~~Gemini thinking 泄露~~ — thinking_config 修复
- [x] ~~飞书群消息~~ — bot open_id 自动学习，群 @mention 正常回复
- [x] ~~Docker 构建代理隔离~~ — `--env-file /dev/null` 方案
- [x] ~~Redis 代理隔离~~ — 不继承全局 HTTPS_PROXY
- [x] ~~Docker host network~~ — 解决容器访问宿主机代理
- [x] ~~YouTube 视频直传~~ — from_uri 直传 Gemini，绕过 yt-dlp 下载
- [x] ~~yt-dlp Node.js~~ — Dockerfile 加 nodejs + cookies
- [x] ~~scheduler 租户上下文~~ — 定时任务显式设置 tenant context
- [x] ~~企微客服账号管理 API~~ — list/add/delete/update/get_link 5 个 tool ✅
- [x] ~~kf-steven-ai 去 K2.5~~ — function calling 全走 Gemini，K2.5 大工具集不可靠 ✅
- [x] ~~kf-leadgen-demo 新租户~~ — 社媒调研获客专家 bot，co-host 在 kf-steven-ai 容器 ✅
- [x] ~~CI/CD per-container config 同步~~ — `sync_instance_configs.py` 自动从根 tenants.json 生成 ✅
- [x] ~~co-host 本地路由~~ — `_find_local_tenant_by_kfid()` 避免 Redis 跨容器转发 ✅
- [x] ~~tool summary 泄露修复~~ — 只记工具名，`<tools_used>` XML 标签包裹 ✅
- [x] ~~assess_capability 降级~~ — 不再作为默认第一步，优先直接使用 tools 列表 ✅
- [x] ~~全面 Gemini~~ — 所有租户 coding_model 清空，默认值改为 ""，K2.5 不再是默认路由 ✅
- [x] ~~PDF 导出~~ — export_file 工具新增 PDF 格式支持（fpdf2 + Markdown 渲染 + CJK 字体 + bytearray→bytes 修复）✅
- [x] ~~自定义工具滥用约束~~ — system prompt 加 guardrails，限制创建频率和重复 ✅
- [x] ~~试用期系统~~ — trial.py + per-user 6h 限额 + route_message 集成 ✅
- [x] ~~Admin Dashboard~~ — `/admin/dashboard` 单页管理面板（租户概览 + 用户管理 + 用量统计 + 租户编辑）✅
- [x] ~~跨容器租户同步~~ — Redis 持久化 `tenant_cfg:*` + 消息队列实时 hot-load，替代失败的 HTTP 跨容器方案 ✅
- [x] ~~Dashboard 租户编辑~~ — 3 级 config fallback（local → tenant_cfg → admin:tenant），${VAR} 保护，跨容器编辑支持 ✅
- [x] ~~TikHub API 集成~~ — search_social_media 工具支持抖音+小红书精确数据（用户搜索/视频搜索/笔记搜索）✅
- [x] ~~Per-tenant 记忆配置~~ — 5 个配置字段（diary/journal_max/chat_rounds/chat_ttl/context），history.py + memory.py + base_agent.py 联动 ✅
- [x] ~~工具白名单补齐~~ — kf-steven-ai / kf-leadgen-demo 的 tools_enabled 补上 search_social_media + plan tools ✅
- [x] ~~Stall detection 修复~~ — browser_open/do/read + search_social_media 加入 _RESEARCH_TOOLS（阈值 7）✅
- [x] ~~task_watchdog 修复~~ — WecomKfClient import → wecom_kf_client singleton ✅
- [x] ~~_final_call 幻觉修复~~ — 重写 prompt 要求区分"已完成"和"未完成"的交付物 ✅
- [x] ~~记忆系统提示注入~~ — _MEMORY_USAGE_HINT 自动注入 system prompt，引导 LLM 主动使用记忆工具 ✅
- [x] ~~部署配额系统~~ — deploy_quota.py + per-user 免费部署次数（默认 1 次）+ 成功才消耗 + 超管跳过 + admin API + system prompt 引导 ✅
- [x] ~~XHS 搜索 URL 修复~~ — Playwright DOM 提取真实链接（`_extract_search_links_from_dom`），混合 Vision+DOM 方案，消除 href 空字段 + LLM 幻觉 URL ✅
- [x] ~~Agent 强制截断全面移除~~ — stall detection + budget timeout + 硬编码进度消息全部删除，`_MAX_ROUNDS` 提升到 50，进度消息改 LLM-only 无硬编码 fallback ✅
- [x] ~~工具返回值截断修复~~ — `_MAX_TOOL_RESULT_LEN` 从 8000 提升到 16000，防止数据截断导致 URL 幻觉 ✅
- [x] ~~反幻觉指令增强~~ — URL 必须原样复制自工具返回数据，不限于搜索结果 ✅
- [x] ~~URL 溯源验证器~~ — 结构性防护替代 prompt 约束，写操作中的 URL 必须来自工具返回数据，代码层拦截幻觉 URL ✅
- [x] ~~日记系统保存数据源 URL~~ — `_extract_outcome` 记录 `fetch_url` 来源 URL，`action_outcomes` 传入 `write_diary`，跨对话可回忆 Google Sheet 等数据源 ✅
- [x] ~~fetch_url 大文档分页~~ — offset 参数支持分页读取，Google Sheets 204K CSV 不再截断丢数据 ✅
- [x] ~~action-claim 误杀修复~~ — ≥5 次工具调用时跳过 promise 检测 ✅
- [x] ~~进度消息模型修复~~ — gemini-2.0-flash → gemini-3-flash-preview，确保代理可用 ✅
- [x] ~~日历时区解析~~ — `_parse_time` 支持 IANA 时区名（America/Los_Angeles 等）✅
- [x] ~~Google Sheets 服务端查询~~ — fetch_url 新增 query 参数（gviz/tq SQL-like 过滤），大型表格不再逐页翻阅 ✅
- [x] ~~日历时区参数~~ — create_calendar_event 新增 timezone 参数，跨时区活动不再默认上海时间 ✅
- [x] ~~进度消息静默失败修复~~ — 添加进度 hint 全链路日志 + 文本长度放宽到 80 + 智能截断 ✅
- [x] ~~Exit gate 幻觉完成修复~~ — 新增"搞定了"完成声称检测 + 上下文感知 read-without-write + 分支管理规范 + intent JSON fallback ✅

## Tech Stack
- Runtime: Python 3.12, FastAPI, uvicorn
- LLM: Gemini 3 Flash (primary) + Gemini 3.1 Pro (complex tasks, auto-escalate at round 6)
- LLM Coding: 当前全走 Gemini；可选配置 coding_model 路由纯文本到其他模型（如 K2.5），但 30+ 工具场景不推荐
- LLM Legacy: kimi.py/kimi_coder.py 文件名为历史遗留（原 Kimi/Moonshot），现为通用 OpenAI 兼容 client
- Storage: Upstash Redis (记忆/历史), GitHub (代码)
- Deploy: 阿里云 ECS, Docker, GitHub Actions CI/CD
- Proxy: Cloudflare Workers (Gemini API + DuckDuckGo search)
- Platforms: 飞书/企业微信/微信客服

## Development Notes
- 中国大陆服务器，访问外网服务需要代理（Cloudflare Worker 做 API 代理）
- Gemini API 通过 `gemini-proxy.js` CF Worker 代理访问
- DuckDuckGo 搜索通过 `ddg-search-proxy.js` CF Worker 代理
- tenants.json 含敏感凭证，不要提交到公开仓库
- Docker 用 host network mode（`network_mode: host`），容器直接用宿主机端口
- `.env` 有代理配置，Docker build 时必须 `--env-file /dev/null` 隔离
- ⛔ Redis（Upstash）国内可直连，**严禁继承 HTTPS_PROXY**！`redis_client.py` 的 `_get_proxy()` 只能读 `REDIS_PROXY`。已造成过生产事故，详见 Pitfalls 章节
- ⛔ **新增 httpx 客户端必须遵守**：中国可直连 API（飞书/企微/TikHub/Upstash）→ `trust_env=False`；需要代理的 API（Gemini/GitHub）→ `httpx.Timeout(connect=5.0)`；**绝对不要**裸调 `httpx.AsyncClient()` 无 timeout。详见 Pitfalls「xray 挂掉导致系统雪崩」
- 所有用户可见的回复都应由 LLM 生成，不要硬编码文本（错误提示除外）
- ⛔ **绝对不要加回 stall detection 或 budget timeout**！Agent loop 已移除所有强制截断机制，只保留 `_MAX_ROUNDS=50` 安全网。模型有完全的自主权决定何时完成任务。成本控制靠 per-tenant token 配额，不靠截断 agent。详见 Pitfalls「Agent 强制截断是反模式」
- ⛔ **工具返回值不要随意截断**！`_MAX_TOOL_RESULT_LEN=16000`。截断会导致 URL 等关键数据丢失 → LLM 幻觉编造。宁可多用 token 也不要丢数据
- 企微客服后台必须配置「智能助手接待」，否则 state=3 卡死 bot 无法回复
- **宿主机 Python 版本很低（< 3.7）**，deploy.yml 里直接跑的脚本不能用 `from __future__ import annotations` 或 3.9+ 类型注解。需要高版本特性就用 `docker run` 在容器内跑
- K2.5 function calling 在 30+ 工具时不可靠，**所有租户已全面切换 Gemini**。`coding_model` 默认值已改为空字符串
- `export_file` 工具支持 PDF 导出（fpdf2 + NotoSansSC TTF 字体），Dockerfile 从 jsDelivr CDN 下载 TTF（不用 NotoSansCJK TTC/OTF，那是 CID-keyed CFF 格式，fpdf2 会乱码）
- LLM 有「写工具上瘾」倾向：system prompt 已加 guardrails 限制 create_custom_tool 滥用
- **新增工具必看顶部 checklist！** `tools_enabled` 白名单、`_RESEARCH_TOOLS`、平台过滤常量都需要同步更新。详见「⚠️ Adding New Tools — MANDATORY Checklist」章节
- 社媒数据 API（TikHub）中国可直连 `api.tikhub.dev`，Bearer token 认证。配置 `social_media_api_provider: "tikhub"` 启用
- 记忆系统支持 per-tenant 配置（5 个字段），不同 bot 可有不同记忆深度。详见「Per-Tenant 记忆配置」章节

### NanoClaw 启发的新架构（2026-03-25）

#### 插件注册表（Plugin Registry）
- **位置：** `app/plugins/registry.py`
- **理念：** 借鉴 NanoClaw "skills over features" —— 工具模块自描述元数据，按需加载
- **用法：** `plugin_registry.discover()` 扫描 `app/tools/`，`get_tools_for_tenant()` 按租户配置过滤
- **新工具零改动核心代码：** 在 `app/tools/` 放文件 + 在 `_DEFAULT_MANIFESTS` 声明组/平台/权限即可
- **TenantConfig 新字段：** `plugin_groups_enabled`（指定启用的工具组）、`plugin_lazy_loading`（按需加载）

#### 容器级沙箱（Container Sandbox）
- **位置：** `app/services/container_sandbox.py`
- **理念：** 借鉴 NanoClaw OS 级隔离 —— 代码在独立 Docker 容器执行
- **安全限制：** 64MB 内存、0.5 CPU、只读文件系统、网络隔离、32 进程上限
- **降级：** Docker 不可用时自动回退到现有 sandbox.py（进程级隔离）
- **TenantConfig 新字段：** `container_sandbox_enabled`

#### Per-Channel 记忆隔离
- **位置：** `app/services/memory_store.py`（所有函数新增 `channel_id` 参数）
- **理念：** 借鉴 NanoClaw per-group context —— 每个群聊有独立的记忆空间
- **Redis key 结构：** `{tenant_id}:ch:{channel_id}:mem:{key}`
- **向后兼容：** `channel_id` 为空时行为与之前完全一致
- **TenantConfig 新字段：** `memory_channel_isolation`

#### Cron Agent 定时任务
- **位置：** `app/services/cron_agent.py` + `app/tools/cron_agent_ops.py`
- **理念：** 借鉴 NanoClaw Scheduled Tasks —— 定时运行完整 Agent（不只是提醒）
- **5 个工具：** create_cron_agent, list_cron_agents, delete_cron_agent, toggle_cron_agent, get_cron_agent_log
- **Cron 表达式：** 标准 5 字段（分 时 日 月 星期），支持 `*/N`、`N-M`、`N,M`
- **TenantConfig 新字段：** `cron_agent_enabled`
- **Redis 存储：** `cron_agents:{tenant_id}` + `cron_log:{tenant_id}`

#### 新增 TenantConfig 字段汇总
```json
{
  "plugin_groups_enabled": [],          // 启用的工具组，空=全部
  "plugin_lazy_loading": true,          // 按需加载工具
  "container_sandbox_enabled": false,   // Docker 容器级沙箱
  "memory_channel_isolation": false,    // 频道级记忆隔离
  "cron_agent_enabled": false           // 定时 Agent 任务
}
```
