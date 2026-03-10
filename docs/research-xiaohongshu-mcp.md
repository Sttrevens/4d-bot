# 小红书 MCP 生态调研报告

> 调研日期：2026-03-04
> 背景：之前因小红书反爬太强，项目接了 TikHub 三方 API 做搜索/读取。现在 GitHub 上出现多个小红书 MCP server，可能实现发帖/运营自动化。

## 1. 头部项目：xpzouying/xiaohongshu-mcp

| 维度 | 详情 |
|---|---|
| **仓库** | https://github.com/xpzouying/xiaohongshu-mcp |
| **语言** | Go |
| **Stars** | ~10.1k（快速增长中） |
| **Forks** | ~1.6k |
| **最新版本** | v2026.03.01（活跃维护，每月发版） |
| **PRs** | 144 closed，社区贡献活跃 |
| **Open Issues** | ~90（包括封号风险讨论 #500） |
| **部署方式** | 二进制下载 / Docker Compose / 源码编译 / NPX |
| **平台支持** | macOS (M1/M2/M3 + Intel) / Windows x64 / Linux x64 |
| **License** | **无**（未设置 LICENSE 文件，默认保留所有权利，商用有法律风险） |
| **作者验证** | 声称稳定运行 1 年+无封号，用 Claude Code 自动化运营数周后开源 |

### 核心技术方案

**Playwright 浏览器自动化**（不是 API 逆向工程）：
- 首次运行自动下载 headless Chromium（~150MB）
- 模拟真人浏览器操作，绕过小红书反爬
- Cookie 持久化保持登录态（扫码登录一次，之后自动维持）
- 作者声称用 Claude Code 稳定自动化运营了数周后才开源

### MCP Tools 列表（完整 11 个）

| Tool | 功能 | 说明 |
|---|---|---|
| `publish_image_post` | 发布图文 | 标题(≤20字)/正文(≤1000字)/图片(URL或本地)/标签/定时发布(1h~14天)/原创声明/可见性(公开/仅自己/互关可见)/商品链接 |
| `publish_video_post` | 发布视频 | 标题/正文/本地视频文件(不支持URL)/标签，自动格式转换 |
| `search` | 搜索笔记 | 关键词搜索 |
| `get_recommendations` | 获取推荐 | 首页推荐流 |
| `get_post_details` | 查看帖子 | 帖子内容 + 互动数据 + 评论列表（需 feed_id + xsec_token） |
| `post_comment` | 发表评论 | 对帖子评论 |
| `reply_to_comment` | 回复评论 | 回复特定评论（需 comment_id） |
| `like/unlike` | 点赞/取消 | 切换点赞状态 |
| `favorite/unfavorite` | 收藏/取消 | 切换收藏状态 |
| `get_user_profile` | 用户主页 | 用户信息 + 已发布笔记列表 |
| `check_login` | 检查登录态 | 判断 Cookie 是否有效 |
| `delete_cookies` | 重置登录 | 清除 Cookie 重新登录 |

**不支持的操作：** 私信、关注/取关、编辑/删除已发布帖子、数据分析面板、多账号管理

## 2. 其他小红书 MCP 项目

| 项目 | 语言 | 技术方案 | 侧重 | 链接 |
|---|---|---|---|---|
| betars/xiaohongshu-mcp-python | Python | Playwright（Go 版完整重写） | 全功能 | https://glama.ai/mcp/servers/@betars/xiaohongshu-mcp-python |
| MilesCool/rednote-mcp | TypeScript | Playwright + 并行处理 | 搜索/提取（不发帖） | https://github.com/MilesCool/rednote-mcp |
| iFurySt/RedNote-MCP | TypeScript | Cookie 认证 | 开发者友好 | https://github.com/iFurySt/RedNote-MCP |
| cjpnice/xiaohongshu_mcp | Python | Selenium | 搜索/查看/评论 | https://github.com/cjpnice/xiaohongshu_mcp |
| chenningling/Redbook-Search-Comment-MCP | Python | AI 驱动评论 | 智能评论/互动 | https://github.com/chenningling/Redbook-Search-Comment-MCP2.0 |
| fancyboi999/xhs-auto-mcp | Python | Cookie 认证 | 全面自动化（含视频） | https://glama.ai/mcp/servers/@fancyboi999/xhs-auto-mcp |
| JS 逆向版 | JavaScript | JS 逆向（无 Playwright） | 轻量搜索/评论 | — |
| Java 版 | Java | Playwright-Java + SpringBoot3 | 企业级集成 | — |

### Python 版（betars）特点

从 Go 版完整重构，功能包括：
- 登录管理（扫码登录/状态检查/Cookie 管理）
- 内容浏览（推荐流/搜索/详情查看）
- 用户资料查看
- 社交互动（点赞/评论）
- 内容发布（图文/视频）

## 3. 与现有 TikHub 方案对比

### 现状（TikHub API + social_media_ops.py）

- **只读操作**：搜索笔记（`/api/v1/xiaohongshu/web/search_notes`）+ 搜索用户（`/api/v1/xiaohongshu/web_v2/search_users`）
- 第三方付费 API（$0.001/次），中国直连稳定
- **无法发帖/评论/互动**
- Fallback：API 失败时回退到 DuckDuckGo 搜索

### MCP 方案优势

| 能力 | TikHub | MCP |
|---|---|---|
| 搜索笔记 | ✅ | ✅ |
| 搜索用户 | ✅ | ✅ |
| 查看帖子详情 | ❌ | ✅ |
| **发布图文** | ❌ | ✅ |
| **发表评论** | ❌ | ✅ |
| **点赞互动** | ❌ | ✅ |
| 推荐流获取 | ❌ | ✅ |
| 定时发布 | ❌ | ✅ |
| 费用 | $0.001/次 | 免费 |

### MCP 方案风险

1. **封号风险** — Issue #500 直接讨论了这个问题。频繁操作可能触发风控
2. **资源开销** — 每个账号需运行一个 headless Chromium（~150MB 内存）
3. **登录维护** — Cookie 有有效期，失效后需重新扫码
4. **部署复杂度** — Go 二进制 + Chromium vs 纯 HTTP API 调用
5. **前端依赖** — 小红书前端改版会导致自动化失效，需跟进维护
6. **合规风险** — 所有发帖自动化都是非官方的（见下节）

## 4. 小红书官方 API 状况

- 小红书开放平台（school.xiaohongshu.com）存在，但**面向电商集成**（商品/订单/物流）
- **不提供内容发布 API** — 目前没有任何官方途径通过 API 发帖
- 所有发帖自动化方案都是**非官方**的，理论上违反 ToS
- 第三方数据服务（Meltwater 等）提供只读数据 API，无写入能力

## 5. 集成建议

### 已实施方案：Playwright + Gemini 视觉（自研 xhs_ops.py）

**决策：不用 TikHub API，不接外部 MCP，全部自研。**

| 场景 | 方案 | 优势 |
|---|---|---|
| 调研/数据采集 | xhs_search + xhs_get_note + xhs_get_user | 直读小红书真实页面，免费，比 TikHub 更准 |
| 内容发布/运营 | xhs_publish + xhs_confirm_publish | Playwright 驱动，参考 xiaohongshu-mcp 思路 |
| 互动/评论 | xhs_comment + xhs_like | 直接操作页面 |
| 登录管理 | xhs_login + xhs_check_login | QR 码扫码 + Cookie Redis 持久化 |

**比 xiaohongshu-mcp 的优势：**
- Gemini 视觉分析页面（不硬编码 DOM 选择器，抗改版）
- Python 原生（不依赖 Go 二进制）
- 与现有工具体系完全集成（同一 agent loop）
- 无第三方 LICENSE 风险

**实现文件：** `app/tools/xhs_ops.py`（10 个工具）
**集成：** `social_media_ops.py` 的 `search_social_media` 小红书部分优先走 Playwright

### 踩坑记录（实施过程中发现的问题）

#### 坑 1：纯 Vision 方案无法提取 URL → CSS selector 也失败 → __INITIAL_STATE__ 才行

**问题：** Gemini 视觉从截图提取搜索结果时，`href` 字段始终为空。小红书搜索结果卡片不显示 URL。

**第一次修复（失败）：** 用 CSS selector `a[href*="/explore/"]` 提取——生产日志 `extracted 0 links`。小红书搜索页是 React SPA，渲染后的 DOM 中 `<a>` 标签不一定有标准 href。

**第二次修复（成功）：** 阅读 xiaohongshu-mcp 源码发现它根本不用 CSS selector，而是读 `window.__INITIAL_STATE__.search.feeds._value`——React SSR 注入的内部状态对象，包含 `feed.id` + `xsecToken`。URL 构造：`https://www.xiaohongshu.com/explore/{id}?xsec_token={token}`

**最终方案——三层降级：**
1. `__INITIAL_STATE__`（最可靠，框架级结构）
2. CSS selector（兜底）
3. Vision only + 反幻觉警告（最后手段）

**关键反思：**
- xiaohongshu-mcp 的 README 写的是"Playwright 浏览器自动化"，但核心数据提取不是 DOM selector 而是 JS 状态注入——**看源码比看 README 重要**
- 我们第一版"看了 README 就动手"导致走了弯路（CSS selector），第二版"看了源码"才找到正确方案

#### 坑 2：LLM 对空字段的幻觉填充

不仅是 URL，任何返回空字段的工具结果都会触发 LLM 的"补全"行为。LLM 倾向于用看似合理但完全虚构的数据填充空字段，而不是告诉用户"该信息无法获取"。

**启示：** 工具设计时，宁可不返回字段，也不要返回空字符串——空字符串会被 LLM 解读为"该有数据但暂时没获取到，我来补"。

#### 坑 3：缺乏数据完整性自检

Bot 返回搜索结果后，没有任何机制验证"返回给用户的 URL 是否真的能打开"。如果 bot 能在返回结果前自动验证关键字段（至少检查非空），就能提前发现这个问题。

**对自治修复的启示：** 这类 bug 完全可以被自动发现——只要有"输出验证层"检查工具返回数据的关键字段完整性。

### 原方案（仅供参考）

#### 短期（已跳过）
~~在 `browser_ops.py` 基础上封装小红书发帖流程~~
→ 已直接实现为独立的 `xhs_ops.py`，有专属 BrowserContext 和 Cookie 管理

#### 中期（备选）
把 betars 的 Python 版 MCP 作为 sidecar 服务部署：
- OpenClaw 通过 MCP 协议（stdio 或 SSE）调用
- 工具定义标准化，升级维护独立
- 可同时对接多个 MCP server

#### 长期（MCP 网关）
统一通过 MCP 协议接入各平台：
- 小红书 MCP + 抖音 MCP + 微博 MCP + ...
- OpenClaw 作为 MCP client，平台运营能力按需插拔
- 符合 Anthropic MCP 标准，生态兼容性好

### 风险缓解措施

1. **频率控制** — 每天最多 ~50 篇（社区建议），每篇间隔 30+ 分钟，保守起见先 1-3 篇/天
2. **独立账号** — 用独立小号测试，不用客户主号。一机一卡一号
3. **内容审核** — LLM 生成内容后加人工审核环节（或 LLM 自检 + 敏感词过滤）
4. **状态监控** — 监控登录态失效（可能被风控的信号），笔记曝光量突降 = 可能被限流
5. **渐进上线** — 先做调研读取，验证稳定后再开放写入能力
6. **反检测** — 用 stealth 插件避免 WebDriver 默认指纹，用住宅 IP（非机房 IP）
7. **会话隔离** — MCP 运行期间不在另一浏览器登录同一账号（Cookie 冲突导致双向掉线）

### 小红书反自动化检测详情（深度调研补充）

小红书的反自动化能力**远超一般平台**：

**检测维度（200+）：**
- 操作间隔分析：0.5 秒内连续点赞 → 标记为机器行为
- 点击轨迹分析：线性滑动轨迹（非人类曲线）→ 标记
- 评论重复率检测：模板化评论 → 标记
- WebDriver 指纹检测：Selenium/Playwright 默认签名可被识别
- IP 关联分析：同 IP 多账号 → 立即标记为矩阵号

**近期封号潮（2025-2026）：**
- 大规模封号行动，波及 AI 生成内容检测率高的账号（即使已运营数月）
- 违规类型包括：引流、自动化工具、矩阵号操作
- 平台已要求**实名认证 + 人脸识别**，违规永久封禁无申诉
- 5+ 账号同设备 → 批量养号分类 → 集体永封

**社区经验教训：**
- 所有 MCP 项目 README 都标注"仅供学习研究"
- 生产环境用这些工具管理客户账号**风险极高**——丢失已有粉丝的成本远大于自动化节省的人力
- 小红书官方 ToS 明确禁止"使用非官方工具干扰社区秩序"

## 6. 结论

小红书 MCP 生态已经相当成熟（9k+ stars，多语言实现，活跃社区）。**核心价值是写入能力**——发帖/评论/互动是 TikHub 等只读 API 做不到的。

建议：
1. **保留 TikHub** 做调研数据采集（稳定可靠）
2. **试点接入 MCP** 做内容发布（用 Python 版 betars，与现有技术栈一致）
3. **先用 browser_ops 快速验证**发帖流程可行性，再决定是否正式接 MCP 协议
