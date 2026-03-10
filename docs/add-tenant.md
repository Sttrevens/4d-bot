# 添加新租户流程

## 前置条件

- 阿里云服务器已部署且 Docker 正在运行
- 安全组已开放 8000 端口
- 服务器公网 IP：`YOUR_SERVER_IP`

---

## 一、飞书 Bot 租户

### 1. 在飞书开放平台创建应用

1. 打开 https://open.feishu.cn/app → 创建企业自建应用
2. 记录 `App ID` 和 `App Secret`
3. 进入「事件订阅」页面，记录 `Verification Token`（Encrypt Key 可留空）
4. **暂时不要填写 Request URL**（等服务重启后再配）

### 2. 配置应用权限

在「权限管理」中开通以下权限：

| 权限 | 用途 |
|------|------|
| im:message | 接收和发送消息 |
| im:message.group_msg | 接收群聊消息 |
| im:resource | 下载图片/文件资源 |
| contact:user.base:readonly | 获取用户名（可选） |

### 3. 配置事件订阅

在「事件订阅」中添加：
- `im.message.receive_v1`（接收消息）

### 4. 编辑 tenants.json

SSH 到服务器，编辑 `tenants.json`，在 `tenants` 数组中新增一项：

```json
{
    "tenant_id": "your-bot-id",
    "name": "Bot 名称",
    "platform": "feishu",
    "app_id": "cli_xxxxxxxxxx",
    "app_secret": "xxxxxxxxxxxxxxxxxx",
    "verification_token": "xxxxxxxxxxxxxxxxxx",
    "encrypt_key": "",
    "oauth_redirect_uri": "",
    "github_token": "${GITHUB_TOKEN}",
    "github_repo_owner": "Sttrevens",
    "github_repo_name": "你的仓库名",
    "llm_api_key": "${KIMI_API_KEY}",
    "llm_base_url": "${KIMI_BASE_URL}",
    "llm_model": "${KIMI_MODEL}",
    "llm_system_prompt": "你的角色设定...",
    "stt_api_key": "",
    "stt_base_url": "",
    "stt_model": "",
    "admin_open_ids": [],
    "admin_names": ["管理员名字"],
    "tools_enabled": [],
    "self_iteration_enabled": false
}
```

**字段说明：**

| 字段 | 说明 |
|------|------|
| `tenant_id` | 唯一标识，用于 webhook URL 路径，建议用英文短横线格式 |
| `platform` | 飞书填 `"feishu"` |
| `app_id` / `app_secret` | 飞书开放平台获取 |
| `verification_token` | 飞书事件订阅页面获取 |
| `llm_api_key` 等 | 用 `${ENV_VAR}` 引用 .env 中的值，也可直接填值 |
| `github_repo_name` | 此 bot 操作的 GitHub 仓库 |
| `llm_system_prompt` | bot 角色设定 |
| `tools_enabled` | 空数组 = 全部工具可用；填具体名字 = 只启用指定工具 |
| `self_iteration_enabled` | 是否允许 bot 修改自身代码，客户租户建议 `false` |

### 5. 重启服务

```bash
cd /path/to/4dgames-feishu-code-bot
docker compose restart
```

查看日志确认加载成功：
```bash
docker compose logs --tail=50 bot
```

应该看到类似：
```
tenant registered: id=your-bot-id name=Bot 名称 platform=feishu
loaded 4 tenants from /app/tenants.json (default=code-bot)
```

### 6. 配置飞书 Webhook URL

回到飞书开放平台 → 事件订阅 → Request URL 填入：

```
http://YOUR_SERVER_IP:8000/webhook/your-bot-id/event
```

点击保存，飞书会发一个 challenge 验证请求。如果服务正常运行，验证会自动通过。

### 7. 发布应用

在飞书开放平台点「版本管理与发布」→「创建版本」→ 提交审核 → 审核通过后上线。

### 8. 测试

在飞书里找到这个 bot，发一条消息验证是否正常回复。

---

## 二、企微客服 Bot 租户

### 1. 在企业微信管理后台配置

1. 打开 https://work.weixin.qq.com → 应用管理 → 微信客服
2. 创建客服账号，记录 `open_kfid`（wkXXXXXX 格式）
3. 在「API」标签页获取：
   - `corpid`（企业 ID）
   - `kf_secret`（微信客服专用 secret）
4. 在「回调配置」页面：
   - `Token` 和 `EncodingAESKey` 自己生成或点随机生成
   - **URL 先不填**

### 2. 编辑 tenants.json

新增一项：

```json
{
    "tenant_id": "kf-your-id",
    "name": "客服 Bot 名称",
    "platform": "wecom_kf",
    "wecom_corpid": "wwXXXXXXXXXXXXXXXX",
    "wecom_kf_secret": "xxxxxxxxxxxxxxxxxx",
    "wecom_kf_token": "xxxxxxxxxx",
    "wecom_kf_encoding_aes_key": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "wecom_kf_open_kfid": "wkXXXXXXXXXXXXXXXX",
    "github_token": "",
    "github_repo_owner": "",
    "github_repo_name": "",
    "llm_api_key": "${KIMI_API_KEY}",
    "llm_base_url": "${KIMI_BASE_URL}",
    "llm_model": "${KIMI_MODEL}",
    "llm_system_prompt": "你的角色设定...",
    "stt_api_key": "",
    "stt_base_url": "",
    "stt_model": "",
    "admin_open_ids": [],
    "admin_names": [],
    "tools_enabled": [],
    "self_iteration_enabled": false
}
```

### 3. 重启服务

```bash
docker compose restart
```

### 4. 配置回调 URL

回到企微管理后台 → 微信客服 → 回调配置 → URL 填入：

```
http://YOUR_SERVER_IP:8000/webhook/wecom_kf/kf-your-id
```

保存时企微会发验证请求，自动通过即成功。

### 5. 测试

用微信扫客服二维码进入会话，发消息测试。

---

## 三、快速检查清单

添加完新租户后，用这个清单确认：

- [ ] `tenants.json` 中 `tenant_id` 唯一，没有重复
- [ ] `platform` 字段正确（`feishu` / `wecom_kf`）
- [ ] 凭证填对了（app_id、app_secret、verification_token 等）
- [ ] `docker compose restart` 已执行
- [ ] 日志中看到 `tenant registered: id=xxx`
- [ ] 平台侧的 webhook URL 已配置且验证通过
- [ ] 发消息测试 bot 有回复

---

## 四、常见问题

**Q: 改了 tenants.json 但 bot 没反应？**
A: 需要 `docker compose restart`，当前不支持热加载。

**Q: 飞书 webhook 验证失败？**
A: 检查 `verification_token` 是否和飞书开放平台一致。查日志里的 `token check` 行对比。

**Q: 多个飞书 bot 能共用同一个 LLM 配置吗？**
A: 可以，用 `${KIMI_API_KEY}` 等占位符引用 `.env` 中的全局配置。也可以给每个租户填不同的 API key。

**Q: `tools_enabled` 怎么配？**
A: 空数组 `[]` = 全部工具可用。如果只想开部分工具，填工具名列表，如 `["web_search", "github_ops"]`。

**Q: 怎么查看当前所有活跃的租户？**
A: 看启动日志，或者访问 `http://YOUR_SERVER_IP:8000/health` 确认服务运行中。
