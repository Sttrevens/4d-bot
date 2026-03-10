# Bot 行业模板

为客户设计 bot 时参考以下模板。每个模板包含推荐的 system_prompt 风格、工具集和配额配置。

## 市场营销 Bot
**适用：** 品牌推广、竞品分析、社媒监控、内容创作
**人设风格：** 专业但不生硬，熟悉各社媒平台，数据驱动
**推荐工具：** web_search, search_social_media, get_platform_search_url, browser_open, browser_do, browser_read, browser_close, export_file, save_memory, recall_memory, think, create_plan, activate_plan, update_plan_step, list_plans, get_plan_detail
**记忆配置：** diary=true, journal_max=800, chat_rounds=5, chat_ttl=3600, context=true
**配额：** trial_duration_hours=48, quota_user_tokens_6h=500000

## 客服 Bot
**适用：** 售前咨询、售后支持、FAQ 回答、工单处理
**人设风格：** 亲切专业、耐心、积极解决问题
**推荐工具：** web_search, save_memory, recall_memory, think, export_file
**记忆配置：** diary=true, journal_max=800, chat_rounds=10, chat_ttl=7200, context=true
**配额：** trial_duration_hours=24, quota_user_tokens_6h=300000
**备注：** 客服 bot 需要更长对话记忆（chat_rounds=10），因为客户问题可能跨多轮

## 项目管理 Bot
**适用：** 任务跟踪、进度汇报、团队协作、日程管理
**人设风格：** 简洁高效、关注 deadline 和优先级
**推荐工具：** （飞书平台）list_calendars, list_calendar_events, create_calendar_event, create_feishu_task, list_feishu_tasks, create_document, read_document, lookup_user, send_feishu_message, web_search, save_memory, recall_memory, think, export_file, create_plan, activate_plan, update_plan_step, list_plans, get_plan_detail, set_reminder, list_reminders
**记忆配置：** diary=true, journal_max=800, chat_rounds=5, chat_ttl=3600, context=true

## 调研分析 Bot
**适用：** 行业调研、数据收集、报告生成、竞品分析
**人设风格：** 严谨、数据驱动、善于结构化信息
**推荐工具：** web_search, search_social_media, get_platform_search_url, browser_open, browser_do, browser_read, browser_close, export_file, think, create_plan, activate_plan, update_plan_step, list_plans, get_plan_detail
**记忆配置：** diary=false, journal_max=0, chat_rounds=3, chat_ttl=1800, context=false
**配额：** quota_user_tokens_6h=800000（调研任务 token 消耗大）
**备注：** 调研 bot 不需要长期记忆，每次任务独立

## 通用助手 Bot
**适用：** 日常问答、知识查询、简单任务处理
**人设风格：** 友好自然、有帮助、不过度专业
**推荐工具：** web_search, save_memory, recall_memory, think, export_file
**记忆配置：** diary=true, journal_max=800, chat_rounds=5, chat_ttl=3600, context=true
**配额：** trial_duration_hours=48, quota_user_tokens_6h=500000
