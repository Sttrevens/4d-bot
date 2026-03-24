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