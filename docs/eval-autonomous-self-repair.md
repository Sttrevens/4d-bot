# Bot 自治修复能力差距评估

> 评估日期：2026-03-05
> 背景：刚修复了 XHS 搜索结果 URL 为空的 bug。评估 bot 距离"自己发现并修复这类问题"还差多少。

## 0. 评估框架

我们用 XHS URL 空字段 bug 作为基准案例，分析 bot 自治修复需要的 5 个能力层：

```
┌─ L5: 自主修复 ─────── 定位根因 → 设计方案 → 改代码 → 测试 → 部署
├─ L4: 根因分析 ─────── 从症状推导出 "Vision 无法看到 DOM 属性"
├─ L3: 问题定位 ─────── "搜索结果 href 字段全为空" → 定位到 xhs_ops.py
├─ L2: 异常感知 ─────── 发现 "返回的数据有关键字段为空"
├─ L1: 数据采集 ─────── 用户反馈 / 日志 / 输出验证
└─ L0: 正常运行 ─────── 当前状态
```

## 1. 各层能力现状

### L1 数据采集 — ⚠️ 部分具备（40%）

| 信号源 | 现状 | 能否发现 XHS URL bug？ |
|---|---|---|
| 用户直接投诉 | ✅ 用户说"链接打不开" → 进入 agent loop | 能，但依赖用户主动反馈 |
| 工具报错（exception） | ✅ `record_error()` 捕获异常 → 触发 auto-fix | **不能** — 空字符串不是异常 |
| 工具返回数据质量 | ❌ **无检查** | **不能** — 没有验证 href 非空 |
| LLM 幻觉检测 | ❌ **无机制** | **不能** — LLM 伪造 URL 无人拦截 |
| 输出对比（regression） | ❌ 无基线数据 | **不能** — 不知道"以前有 URL 现在没有" |
| 端到端健康探针 | ❌ 无定时自检 | **不能** — 没有 cron 定期跑搜索验证 |

**关键缺口：bot 只能感知"崩溃"，不能感知"静默降级"（返回了数据但质量差）。**

### L2 异常感知 — ❌ 基本不具备（10%）

现有机制：
- `check_unfulfilled_deliverables()` — 检查用户要 PDF 但没生成 PDF ✅
- `_has_unmatched_reads()` — 检查读了数据但没写回 ✅
- 但这些都是**任务级**的（"是否完成了用户请求"），不是**数据级**的（"返回的数据是否完整/正确"）

**XHS URL bug 场景：** bot 完成了搜索请求，返回了结果 → deliverable check 通过。但数据里 href="" 这个质量问题无人检测。

### L3 问题定位 — ✅ 较好（70%）

如果 bot 知道"href 字段为空是个问题"，定位到代码位置是可以做到的：
- `self_search_code("href")` → 找到 `xhs_ops.py` 相关代码
- `self_read_file()` → 读代码理解逻辑
- auto-fix 的预加载机制已经会从 traceback 提取文件位置

**但前提是 L2 先发现了问题。**

### L4 根因分析 — ⚠️ 依赖 LLM 推理能力（50%）

当前 auto-fix 会将错误上下文 + 代码发给 LLM 分析。对于 XHS URL bug：
- LLM 需要理解"截图中看不到 URL"这个推理 → **可能做到**（Gemini Pro 有这个推理能力）
- LLM 需要知道 xiaohongshu-mcp 的 DOM 方案作为参考 → **做不到**（auto-fix 不搜索开源项目）
- LLM 需要决定"用 page.evaluate 提取 DOM 链接" → **可能做到**（如果 prompt 引导得好）

**关键差距：auto-fix 只看自己的代码和错误日志，不会主动搜索外部参考方案。**

### L5 自主修复 — ⚠️ 部分具备（60%）

如果 LLM 已经知道要怎么修，修复执行链是完整的：
- ✅ `self_edit_file("app/tools/xhs_ops.py", ...)` → 写入新代码
- ✅ `self_validate_file()` → 语法检查
- ✅ 自动 commit + push → CI/CD 部署
- ✅ `self_safe_deploy()` → 回滚保护
- ❌ **无法验证修复是否有效** — 没有自动化测试跑搜索并检查 href 非空

## 2. 具体差距清单

### 差距 A：输出验证层（最关键）

**现状：** 工具返回结果直接传给 LLM，无任何数据质量检查。
**需要：** 在 `_handle_tool_response()` 或工具返回后加验证层。

```python
# 概念设计
def _validate_tool_output(tool_name: str, result: dict) -> list[str]:
    """检查工具返回数据的关键字段完整性"""
    warnings = []

    # xhs_search 返回的结果应该有 URL
    if tool_name in ("xhs_search", "xhs_playwright_search"):
        if "results" in result:
            empty_hrefs = sum(1 for r in result["results"] if not r.get("href"))
            if empty_hrefs > 0:
                warnings.append(f"{empty_hrefs}/{len(result['results'])} results have empty href")

    # 通用：检查返回中是否有关键字段为空
    # ...
    return warnings
```

**实现复杂度：** 中。每个工具需要定义自己的"关键字段"schema。
**效果：** 能自动发现 XHS URL bug，并记录为异常触发 auto-fix。

### 差距 B：LLM 幻觉拦截

**现状：** LLM 收到空 URL 后伪造 URL，直接发给用户。
**需要：**
1. 工具设计层面：不返回空字符串（改为 null 或不返回该字段）
2. 输出层面：检测 LLM 回复中的 URL 是否在 `source_registry` 中注册过

**实现复杂度：** 低（设计规范）+ 中（URL 校验）。

### 差距 C：定时自检探针

**现状：** 无主动健康检查（只有 `/health` 端点检查服务是否活着）。
**需要：** 定时执行核心工具的冒烟测试。

```python
# 概念设计：每 6 小时跑一次
async def _health_probe_xhs_search():
    result = await xhs_playwright_search("美食推荐", "note", 3)
    if result and result["results"]:
        empty_hrefs = sum(1 for r in result["results"] if not r.get("href"))
        if empty_hrefs > len(result["results"]) * 0.5:
            record_error("health_probe", "xhs_search: >50% results have empty href")
```

**实现复杂度：** 中。需要选择哪些工具需要探针、用什么测试输入、多久跑一次。
**风险：** 自动搜索可能触发小红书反爬。

### 差距 D：外部参考搜索

**现状：** auto-fix 只看自己的代码和报错信息，不搜索 GitHub/Stack Overflow 等外部方案。
**需要：** 修复时允许 LLM 用 `web_search` 搜索类似问题的解决方案。

**实现复杂度：** 低。在 auto-fix 的 tool 列表中加入 `web_search`。
**风险：** 搜索结果可能误导 LLM（需要引导 prompt）。

### 差距 E：修复后自动验证

**现状：** 修复代码 → 部署。没有验证修复是否有效。
**需要：** 部署后自动重新执行触发 bug 的操作，验证是否修复。

**实现复杂度：** 高。需要：
1. 保存触发 bug 的上下文（哪个工具、什么参数）
2. 部署后重放该操作
3. 对比修复前后的输出

### 差距 F：allowlist 边界

**现状：** auto-fix 只能改 `app/tools/` 和 `app/knowledge/`。
**XHS URL bug 恰好在 app/tools/xhs_ops.py → ✅ 可修。**

但如果 bug 在：
- `app/services/base_agent.py`（stall detection 误杀）→ ❌ 不可修
- `app/router/intent.py`（路由错误）→ ❌ 不可修
- `requirements.txt`（缺少依赖）→ ❌ 不可修

**当前 allowlist 覆盖约 6% 的代码文件。** 大部分基础设施 bug 需要人工干预。

## 3. 综合评分

### XHS URL bug 自治修复可行性

| 步骤 | 所需能力 | 现状 | 差距 |
|---|---|---|---|
| 1. 发现 href 为空 | 输出验证层 | ❌ 无 | **差距 A** |
| 2. 判定为 bug（非预期） | 异常分类 | ⚠️ 需增强 | 差距 A |
| 3. 定位到 xhs_ops.py | 代码搜索 | ✅ 已有 | — |
| 4. 理解"Vision 看不到 URL" | LLM 推理 | ⚠️ 可能 | — |
| 5. 想到"用 DOM 提取" | LLM 设计 | ⚠️ 需参考 | **差距 D** |
| 6. 写出正确的 JS selector | LLM 编码 | ✅ 可以 | — |
| 7. 修改代码并部署 | self_edit + deploy | ✅ 已有 | — |
| 8. 验证修复有效 | 回归测试 | ❌ 无 | **差距 E** |

**总体评分：距离完全自治修复约 40-50%。**

核心瓶颈不是"修代码"（这个已经很强了），而是**"发现问题"**和**"验证修复"**。

## 4. 实施优先级建议

### P0 — 输出验证层（投入产出比最高）

为关键工具加输出完整性检查。不需要通用方案，先针对已知踩坑的工具：

| 工具 | 检查项 |
|---|---|
| `xhs_search` | results[].href 非空 |
| `xhs_playwright_search` | results[].href 非空 |
| `search_social_media` | results[].href 非空 |
| `export_file` | 返回的文件 URL 可访问 |
| `create_document` | 返回的文档 URL 可访问 |

**实现方式：** 在各工具的 handler 里加 `logger.warning()` + `record_error("data_quality", ...)` 即可。不需要新模块。

### P1 — LLM 输出 URL 校验

在最终回复发送前，提取回复中的所有 URL，与 `source_registry` 对比。未注册的 URL 标记为疑似幻觉。

### P2 — 定时自检探针

选 3-5 个核心工具，每 6 小时跑冒烟测试。发现异常自动记录。

### P3 — 修复后自动回归

auto-fix 部署后，重放触发 bug 的操作验证修复效果。

## 5. 结论

**bot 现在的自我修复能力像一个"有手但瞎了的医生"：**
- ✅ 手术能力强 — 代码修改、部署、回滚链路完整
- ✅ 知道自己的代码 — 能搜索、阅读、理解自己的代码
- ❌ 看不到病症 — 无法感知"静默降级"（数据质量下降但不报错）
- ❌ 不知道病在哪 — 没有输出验证告诉它"你的 XHS 搜索结果没 URL"
- ⚠️ 能诊断但缺参考 — LLM 推理能力足够，但不搜索外部方案（闭门造车）

**最短路径：** 加上 P0（输出验证层），bot 就能自动发现 XHS URL bug → 触发 auto-fix → LLM 分析 + 写修复 → 部署。整个链路就通了。

**预估工作量：**
- P0 输出验证：~2 小时（每个工具加几行检查代码）
- P1 URL 校验：~1 小时
- P2 定时探针：~4 小时（含测试）
- P3 回归验证：~8 小时（最复杂，需要保存/重放上下文）

P0+P1 做完后，bot 对类似 XHS URL bug 的自治修复能力约提升到 **70-80%**。
