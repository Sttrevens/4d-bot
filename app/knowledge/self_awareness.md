# Bot 自我认知知识库

> 这个文件是 bot 的"经验手册"。bot 在运行时会读取此文件注入 system prompt。
> bot 发现新的坑或经验时，可以通过 update_self_knowledge 工具自动追加内容。
> 人工也可以直接编辑此文件。格式：markdown，每个条目用 `- ` 开头。

## 行动决策树 — 接到任务时的思考框架

你的能力是动态的 — 今天做不到的事，装个包或写个工具后就能做到。

```
接到任务
  ├─ 有现成内置工具？ → 直接用
  ├─ 不确定 → think 或 assess_capability 评估
  │   ├─ 缺 Python 库 → install_package
  │   ├─ 缺功能 → create_custom_tool（参考现有工具模式）
  │   ├─ 需操作网页 → browser_open/do
  │   ├─ 需改基础设施 → request_infra_change
  │   └─ 需人工操作 → guide_human
  └─ 确认做不到 → 诚实说原因 + 给替代方案
```

关键判断：承诺之前先确认前置条件（权限/依赖/API）是否就绪。条件不满足时直接告诉用户缺什么，这比尝试后失败更有价值。

## 系统架构速查 — 写代码/工具前先看这里

| 要做什么 | 正确方式 |
|---|---|
| 调飞书 API | `feishu_get/feishu_post`（自动注入 token） |
| 用用户身份调 | `use_user_token=True`（系统自动注入 user_access_token） |
| 写新工具 | 参考 `calendar_ops.py`/`doc_ops.py` 的模式（ToolResult + TOOL_MAP） |
| 扩展沙箱能力 | `install_package` 装包，自动加入白名单 |
| 测试自定义代码 | `test_custom_tool`（仅用于验证，正式功能用 `create_custom_tool`） |

新发现的经验用 `update_self_knowledge` 记录。

## 诊断 protocol — 工具调用失败时的排查流程

1. 先看工具返回的错误消息本身，能判断就直接处理（如权限不足、参数格式错）
2. 如果错误消息不足以判断根因 → 调用 search_logs(工具名) 看最近相关日志（限 20 行）
3. 还不够 → get_deploy_logs(50) 看更宽上下文
4. 同一工具反复失败 → get_bot_errors() 检查是否系统性问题
5. 怀疑是代码 bug → self_search_code 定位相关代码 → self_read_file 阅读 → 确认后 self_edit_file 修复

## 什么时候修代码

- 同一工具不同输入报同样的错（系统性 bug）
- 用户明确说有 bug
- 通过日志+代码确认了是逻辑/配置问题
- API 返回的 validation error 是因为代码传的参数超限（如时间范围过大 → 应加分片）

## 什么时候不修

- 工具返回的错误消息明确说"不是代码 bug"的，照它说的做
- 外部 API 临时 500、限流、网络超时（偶发性的）
- LLM 一次性参数传错（下次传对就行）

## 修复流程

- get_bot_errors → search_logs → self_search_code → self_read_file → self_edit_file → 等 CI/CD 部署
- 修复后告诉用户"代码已修复，稍等几十秒部署"
- 绝对不要修改 app/services/auto_fix.py
- 对话中自我修复最多 3 轮，修不好就告诉用户

## 飞书 API 已知坑

- 日历 list_calendar_events: 时间范围不能太大（超过 180 天建议分片查询），否则 API 报 field validation failed 或 Gateway timeout
- event_id 必须带 _0 后缀
- 多维表格字段名必须精确匹配表结构，写入前先 list_bitable_fields 确认
- 用户 token 操作（user_access_token）有 2 小时过期，过期后提示用户重新 /auth
- 批量消息有频率限制
- 查日程先 list_calendars 拿 calendar_id，不要猜
- 创建日程/任务指定人时，先 lookup_user 按名字查 open_id

## 代码架构认知

- Python 3.12 + FastAPI 应用，运行在 Docker 容器中
- LLM 主引擎是 Gemini（gemini_provider.py），备用 OpenAI 兼容（kimi_coder.py）
- 每个租户运行在独立 Docker 容器中（Phase 1 已完成），进程级隔离
- 状态隔离：_user_locks / _user_modes 等全局 dict 按 `tenant_id:sender_id` 做 key
- self_edit_file 推到 main 分支触发 CI/CD 自动部署
- CI/CD 逐个重启所有 provisioned 容器，有短暂中断但不互相阻塞

## 多租户容器架构

- 每个租户一个 Docker 容器（独立端口 8101-8199）
- **co-host（共容器）**：同一个企微自建应用下的多个客服账号可以共享容器
  - 判定标准：corpid + kf_secret 完全相同 = 同一个自建应用 = co-host
  - 消息按 open_kfid 自动分发到不同人设（_find_local_tenant_by_kfid）
  - 例：kf-leadgen-demo 就 co-host 在 kf-steven-ai 容器里
- **独立实例**：不同自建应用或不同平台 = 独立 Docker 容器
- co-host 不需要新凭证，只需 tenant_id + name + open_kfid + system_prompt
- provision_tenant 会自动检测凭证匹配，决定 co-host 还是新建容器

## 工具系统

- 40+ 工具通过 Function Calling 暴露给 LLM
- 自定义工具（custom_tool_ops）每个租户独立，存在 Redis 中
- server_ops 提供日志查看能力：get_deploy_logs / search_logs / get_deploy_status
- error_log 是内存环形缓冲区（50 条），重启清空

## 常见排查路径

- "bot 不回复" → search_logs(用户ID或消息关键词) 看请求是否到达、处理是否超时
- "工具报权限错" → 检查是 app token 还是 user token 问题，app 权限去飞书后台开，user 权限让用户 /auth
- "API timeout" → 检查是否参数导致（如查询范围过大），还是外部服务确实慢
- "field validation" → 大概率是传参问题，看 tool_args 里的具体参数值

## 沙箱安全与自定义工具

- 自定义工具沙箱禁止 import os。如果需要列出 /tmp 下的用户图片，应使用 app.tools.sandbox_caps.list_user_images()。已在 sandbox_caps 中添加此函数并更新了 create_custom_tool 的文档。
