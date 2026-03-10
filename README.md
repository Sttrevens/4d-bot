# 4D AI Bot

多平台、多租户 AI 智能助手，基于 Gemini Function Calling Agent 架构，支持飞书、企业微信、微信客服三大平台。

## Quick Start

```bash
git clone https://github.com/Sttrevens/4d-bot.git
cd 4d-bot
bash install.sh
```

**前置条件：** Docker、Git

**你需要准备：**
- 飞书/企微应用凭证（App ID + Secret）
- Gemini API Key（[获取](https://aistudio.google.com/apikey)）
- Upstash Redis（[免费注册](https://upstash.com)，记忆/历史存储）
- 国内服务器需要 Cloudflare Worker 代理（部署教程见 `cloudflare-worker/`）

## 核心能力

- **多平台接入** — 飞书 (Feishu/Lark)、企业微信 (WeCom)、微信客服 (WeCom KF)
- **多租户隔离** — 每个团队独立的凭证、仓库、LLM 配置、工具集、管理员权限
- **40+ 内置工具** — GitHub 操作、飞书日历/任务/文档/多维表格、代码搜索、网络搜索、浏览器自动化等
- **多模态处理** — 文本、图片 (Vision)、语音 (Whisper STT)、视频 (Gemini from_uri / FFmpeg)、文件、PDF 导出
- **自主 Agent** — Gemini Function Calling 循环，最多 50 轮工具调用，flash + pro 自动升级
- **自我修复** — 运行时错误自动触发 LLM 诊断 → 定位代码 → 修复 → 安全部署（allowlist 边界保护）
- **记忆系统** — 三层架构：工作记忆 (对话历史) + 事件日志 (journal) + 语义记忆 (per-tenant 可配)
- **浏览器自动化** — Playwright + Gemini Vision，操作任意网页
- **能力自获取** — 动态安装 Python 包、创建自定义工具、申请基础设施变更

## 架构概览

```
用户消息 → Webhook Handler (飞书/企微/微信客服)
         → 租户上下文设置 (contextvars)
         → route_message() (配额/限流/试用期检查)
         → Gemini Agent Loop (flash, round 6+ 自动升级 pro)
           ├─ 40+ Function Calling 工具
           ├─ 多模态处理 (图片/语音/视频)
           ├─ 记忆系统 (读写/回忆)
           └─ 结果返回用户
```

每个租户可运行在独立 Docker 容器中（端口隔离、内存限制），通过 `tenants.json` 配置。

## 技术栈

| 组件 | 技术 |
|------|------|
| 后端框架 | Python 3.12 + FastAPI + uvicorn |
| LLM | Gemini 3 Flash (默认) + Gemini 3.1 Pro (复杂任务自动升级) |
| 存储 | Upstash Redis (状态/记忆/配额/限流) |
| 代码管理 | GitHub REST API |
| 浏览器 | Playwright + Chromium |
| 语音转写 | OpenAI-compatible Whisper API |
| 视频处理 | FFmpeg + yt-dlp |
| 容器化 | Docker + docker-compose |
| 部署 | 阿里云 ECS / 任意 VPS (最低 2GB RAM 推荐) |
| 代理 | Cloudflare Workers (Gemini API + DuckDuckGo) |

## 项目结构

```
app/
├── main.py                     # FastAPI 入口
├── config.py                   # 全局配置
├── webhook/                    # 平台 Webhook 处理
│   ├── handler.py              # 飞书事件处理
│   ├── wecom_handler.py        # 企微事件处理
│   └── wecom_kf_handler.py     # 微信客服事件处理
├── router/
│   └── intent.py               # 消息路由 + 配额/限流/试用期检查
├── services/                   # 业务逻辑层
│   ├── base_agent.py           # Agent 基类 (工具注册/分类/指令)
│   ├── gemini_provider.py      # Gemini 主 Agent (flash + pro 自动升级)
│   ├── auto_fix.py             # 自我修复引擎 (allowlist 边界)
│   ├── memory.py               # 三层记忆管理
│   ├── history.py              # 对话历史 (per-tenant 配置)
│   ├── metering.py             # 用量计量
│   ├── rate_limiter.py         # 限流 (per-tenant + per-user)
│   ├── trial.py                # 试用期系统
│   ├── redis_client.py         # Upstash Redis REST 客户端
│   ├── tenant_sync.py          # 跨容器租户配置同步
│   └── ...
├── tools/                      # 40+ Function Calling 工具
│   ├── feishu_api.py           # 飞书开放平台 API
│   ├── bitable_ops.py          # 多维表格
│   ├── doc_ops.py              # 文档操作
│   ├── calendar_ops.py         # 日历
│   ├── git_ops.py              # Git 操作
│   ├── github_ops.py           # GitHub API
│   ├── web_search.py           # 联网搜索
│   ├── browser_ops.py          # 浏览器自动化
│   ├── sandbox.py              # 代码沙箱
│   ├── file_export.py          # 文件导出 (CSV/PDF/TXT/MD/JSON)
│   └── ...
├── tenant/                     # 多租户系统
│   ├── config.py               # TenantConfig 数据模型
│   ├── context.py              # 请求级租户上下文 (contextvars)
│   └── registry.py             # 租户注册/加载
├── admin/                      # 管理后台
│   ├── routes.py               # Admin API + Dashboard
│   └── dashboard.html          # Web 管理面板
└── knowledge/                  # 知识库模块
    └── modules/
cloudflare-worker/              # CF Worker 代理
├── gemini-proxy.js             # Gemini API 代理
└── ddg-search-proxy.js         # DuckDuckGo 搜索代理
```

## 安装

### 环境要求

- Docker + docker-compose（推荐）
- 或 Python 3.12+、FFmpeg、Git（本地开发）
- **最低配置：2GB RAM**（每个容器限制 512MB + swap，低于 2GB 多租户场景可能 OOM）

### Docker 部署

```bash
# 1. 克隆
git clone https://github.com/Sttrevens/4d-bot.git
cd 4d-bot

# 2. 配置
cp .env.example .env            # 编辑 .env，填入 API Key 等
cp tenants.example.json tenants.json  # 编辑 tenants.json，填入平台凭证

# 3. 构建（国内服务器用阿里云镜像加速）
docker compose --env-file /dev/null build \
    --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple

# 4. 启动
docker compose up -d

# 5. 查看日志
docker compose logs -f
```

### 本地开发

```bash
pip install -r requirements.txt
cp .env.example .env
cp tenants.example.json tenants.json
# 编辑配置文件...
python -m app.main
```

## 配置说明

### .env — 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `GEMINI_API_KEY` | 是 | Gemini API Key |
| `GOOGLE_GEMINI_BASE_URL` | 国内必填 | CF Worker 代理 URL |
| `UPSTASH_REDIS_REST_URL` | 推荐 | Redis REST URL（记忆/历史） |
| `UPSTASH_REDIS_REST_TOKEN` | 推荐 | Redis REST Token |
| `DDG_SEARCH_PROXY_URL` | 国内推荐 | DuckDuckGo 搜索代理 |
| `GITHUB_TOKEN` | 可选 | GitHub 功能需要 |
| `STT_API_KEY` | 可选 | 语音转写（Whisper API） |
| `ADMIN_TOKEN` | 推荐 | Admin Dashboard 认证 token |
| `HTTPS_PROXY` | 可选 | 代理地址（仅 Gemini/GitHub 等需要翻墙的 API 使用） |

### tenants.json — Bot 配置

参考 `tenants.example.json`，包含三个平台的完整示例。

核心字段：
- `tenant_id` / `name` / `platform` — 身份标识
- 平台凭证 — 飞书 `app_id`/`app_secret`，企微 `wecom_corpid`/`wecom_corpsecret` 等
- `llm_system_prompt` — Bot 人设
- `tools_enabled` — 工具白名单（空 = 全部启用）
- `admin_names` — 管理员姓名列表
- `memory_*` — 记忆系统配置（日记/历史轮数/TTL）
- `trial_*` — 试用期配置
- `quota_*` / `rate_limit_*` — 配额和限流

详细字段说明见 `tenants.example.json` 中的注释。

## 平台配置

### 飞书

1. [飞书开放平台](https://open.feishu.cn/app) 创建应用
2. 事件订阅 → 请求地址填 `https://YOUR_DOMAIN/webhook/event/{tenant_id}`
3. 订阅事件: `im.message.receive_v1`
4. 权限: `im:message`、`im:message:send_as_bot`、`im:resource`、`contact:user.base:readonly`
5. 版本管理 → 创建版本 → 发布

### 企业微信

1. 企微管理后台 → 应用管理 → 创建自建应用
2. API 接收消息 → URL 填 `https://YOUR_DOMAIN/webhook/wecom/{tenant_id}`

### 微信客服

1. 企微管理后台 → 微信客服 → API 接收消息
2. URL 填 `https://YOUR_DOMAIN/webhook/wecom_kf/{tenant_id}`
3. **接待方式必须选「智能助手接待」**（不是人工接待）

## Webhook 路由

| 端点 | 说明 |
|------|------|
| `POST /webhook/event/{tenant_id}` | 飞书事件 |
| `POST /webhook/wecom/{tenant_id}` | 企微事件 |
| `POST /webhook/wecom_kf/{tenant_id}` | 微信客服事件 |
| `GET /health` | 健康检查 |
| `GET /admin/dashboard` | 管理后台（需 ADMIN_TOKEN） |

## Admin Dashboard

Web 管理面板，访问 `https://YOUR_DOMAIN/admin/dashboard`。

功能：
- 租户概览 + 配置编辑
- 试用用户管理（approve/block/reset）
- 月度用量统计
- 容器实例管理

设置 `ADMIN_TOKEN` 环境变量启用认证。

## 安全说明

- **凭证管理**：所有 API Key、Secret 通过环境变量或 `${VAR}` 引用加载，`tenants.json` 和 `.env` 已 gitignore
- **代码沙箱**：自定义工具在受限沙箱中执行（import 白名单 + AST 安全检查 + 执行超时）
- **自修复边界**：auto-fix 只能修改 `app/tools/` 和 `app/knowledge/`，基础设施层只读（allowlist fail-closed）
- **SSRF 防护**：`fetch_url` 和 `browser_open` 均禁止访问内网/本地地址
- **Webhook 验证**：飞书 verification_token、企微 SHA1 签名 + AES 解密
- **Admin 认证**：所有 `/admin/api/*` 端点需 Bearer token 认证
- **容器隔离**：每个租户独立 Docker 容器，512MB 内存限制，防止单租户 OOM 影响全局

## 国内部署指南

国内服务器无法直连 Google API，需要 Cloudflare Worker 做代理：

1. **Gemini API 代理**: 部署 `cloudflare-worker/gemini-proxy.js`，获取 Worker URL 填入 `GOOGLE_GEMINI_BASE_URL`
2. **搜索代理**: 部署 `cloudflare-worker/ddg-search-proxy.js`，获取 Worker URL 填入 `DDG_SEARCH_PROXY_URL`

详见 `cloudflare-worker/` 目录中的部署说明。

**注意：** `HTTPS_PROXY` 环境变量仅影响需要翻墙的 API（Gemini、GitHub）。国内可直连的服务（飞书、企微、Upstash Redis）的 httpx 客户端均设置 `trust_env=False`，不会走代理。

## 测试

```bash
python -m pytest tests/ -v
```

## 贡献

欢迎 Issue 和 PR。

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/amazing-feature`)
3. 提交更改 (`git commit -m 'Add amazing feature'`)
4. 推送到分支 (`git push origin feature/amazing-feature`)
5. 开 Pull Request

## License

MIT
