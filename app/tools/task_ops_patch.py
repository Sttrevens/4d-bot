# 这是给 kimi_coder.py 的补丁说明

# 1. 修改 _TOOL_INSTRUCTIONS 中的找任务规则（第280-285行附近）：
# 原文：
# - 重要：找任务的推荐顺序：① 先 list_feishu_tasklists 获取清单列表 → ② 用 list_tasklist_tasks(tasklist_guid, keyword) 从清单里搜（能看到所有任务） → ③ 如果任务是子任务，用 list_feishu_subtasks(parent_id) 查看
# - list_feishu_tasks 只能看到个人相关的任务（分配给自己/自己创建的），很多任务不在这里，优先用 list_tasklist_tasks

# 改成：
# - 重要：找任务时必须优先去任务清单里找！正确顺序：① 先 list_feishu_tasklists 获取清单列表 → ② 用 list_tasklist_tasks(tasklist_guid, keyword) 从清单里搜（能看到所有任务，不限于个人名下） → ③ 如果任务是子任务，用 list_feishu_subtasks(parent_id) 查看。list_feishu_tasks 只能看到个人名下的任务（分配给自己/自己创建的），很多任务不在这里，不要一上来就用它！

# 2. 同时需要更新 TOOL_DEFINITIONS 中的两个工具的 description：

# list_feishu_tasks 的 description 应该强调限制：
# "description": "查询个人任务列表（自动翻页获取全部）。支持按关键词过滤任务标题，强烈建议用 keyword 缩小范围。注意：此工具只能看到分配给你自己或你创建的任务，看不到清单里的其他任务！要找团队任务请先用 list_feishu_tasklists + list_tasklist_tasks"

# list_tasklist_tasks 的 description 应该强调优先使用：
# "description": "列出某个任务清单内的所有任务（按清单维度查看，能看到清单里的所有任务，不限于个人任务列表）。需要清单 ID（先用 list_feishu_tasklists 获取）。推荐：找任务时优先用这个工具从清单里找，比 list_feishu_tasks（个人列表）更全，能看到团队所有任务"
