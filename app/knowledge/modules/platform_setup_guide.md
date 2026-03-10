# 平台凭证获取 & Webhook 配置指南

用 guide_human 生成引导清单时，参考以下各平台的精确操作路径。

## 飞书（Feishu）

### 获取凭证
1. 打开 open.feishu.cn → 登录 → 点「创建企业自建应用」
2. 填写应用名称和描述 → 创建
3. 左侧菜单「凭证与基础信息」→ 复制 App ID 和 App Secret
4. 左侧菜单「事件与回调」→「加密策略」→ 复制 Verification Token 和 Encrypt Key
5. 需要的凭证：app_id, app_secret, verification_token, encrypt_key

### 配置 Webhook（开通后做）
1. open.feishu.cn → 进入应用 → 左侧「事件与回调」
2. 「请求地址配置」填入 webhook URL（开通后会提供）
3. 左侧「权限管理」→ 搜索并开通以下权限：
   - im:message（接收和发送消息）
   - im:message:send_as_bot（以应用身份发消息）
   - contact:user.id:readonly（查询用户 ID）
4. 左侧「应用发布」→ 创建版本 → 提交审核 → 管理员在飞书管理后台审批
5. 审批通过后，在飞书客户端搜索应用名即可找到 bot

### 注意事项
- 企业自建应用无需上架应用商店，只需管理员审批
- 权限变更后需要重新提交版本审核
- 群聊中使用需要先把 bot 拉入群

## 企业微信（WeCom）

### 获取凭证
1. 打开 work.weixin.qq.com → 管理后台登录
2. 上方菜单「我的企业」→ 复制 Corp ID
3. 左侧「应用管理」→「自建」→「创建应用」
4. 填写应用信息 → 创建完成后进入应用详情
5. 复制 AgentId 和 Secret（Corp Secret）
6. 下方「接收消息」→ 设置 API 接收 → 系统自动生成 Token 和 EncodingAESKey → 复制
7. 需要的凭证：wecom_corpid, wecom_corpsecret, wecom_agent_id, wecom_token, wecom_encoding_aes_key

### 配置 Webhook（开通后做）
1. 应用详情 →「接收消息」→ 设置 API 接收
2. URL 填入 webhook URL（开通后会提供）
3. Token 和 EncodingAESKey 用之前复制的值

## 微信客服（WeChat KF）

### 获取凭证
1. 打开 work.weixin.qq.com → 管理后台登录
2. 上方「我的企业」→ 复制 Corp ID
3. 左侧「应用管理」→ 找到「微信客服」应用 → 进入
4. 「API」tab →「获取 secret」→ 复制 KF Secret
5. 同页面下方「接收消息」→ 设置 → 系统生成 Token 和 EncodingAESKey → 复制
6. 「客服账号」tab → 找到要接入的客服账号 → 复制 Open KfID（格式 wk 开头）
7. 需要的凭证：wecom_corpid, wecom_kf_secret, wecom_kf_token, wecom_kf_encoding_aes_key, wecom_kf_open_kfid

### 配置 Webhook（开通后做）
1. 「微信客服」应用 →「API」→「接收消息」→ 设置
2. URL 填入 webhook URL
3. 确保「接待方式」设置为「智能助手接待」（不是人工接待，否则 bot 无法回复）

### 注意事项
- 「智能助手接待」是必须的，否则 state=3 卡死 bot
- 同一个自建应用的 secret/token/encoding_aes_key 是一组
- 不同客服账号可以分配给不同自建应用

## QQ 机器人（QQ Bot）

### 获取凭证
1. 打开 q.qq.com → QQ 开放平台 → 登录
2. 「应用管理」→「创建机器人」→ 填写信息
3. 创建完成后进入机器人详情 → 复制 AppID 和 AppSecret
4. 「开发」→「开发设置」→ 生成 Token（用于 webhook 签名验证）→ 复制
5. 需要的凭证：qq_app_id, qq_app_secret, qq_token

### 配置 Webhook（开通后做）
1. 机器人详情 →「开发」→「回调配置」
2. 填入 webhook 回调地址（开通后提供，格式 `https://域名/webhook/qq/{tenant_id}`）
3. 回调地址要求：已备案域名 + HTTPS + 端口 443/8443/80/8080
4. 「添加事件」→ 全选以下事件类型：
   - 单聊事件（C2C_MESSAGE_CREATE — 单聊消息）
   - 群事件（GROUP_AT_MESSAGE_CREATE — 群 @消息）
5. 「IP 白名单」→ 添加服务器公网 IP
6. 保存后平台会发一次验证请求（Ed25519 签名），系统自动处理

### 注意事项
- QQ 强制要求 HTTPS 443 端口，确保 nginx SSL 配置正确
- 群里 bot 只能收到 @它的消息（GROUP_AT_MESSAGE_CREATE）
- 2025-04 起主动推送能力已停用，只能被动回复（5 分钟内，群聊最多 5 条）
- 单聊被动回复窗口 60 分钟
- user_openid 是 per-app 的，不同机器人应用看到的用户 ID 不同
