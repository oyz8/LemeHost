# 在 Hugging Face 免费部署自动 LemeHost 续期 带自动开机功能
# ⭐ **觉得有用？给个 Star 支持一下！**
> 注册地址：https://lemehost.com/

## 📖 简介

自动为 LemeHost 免费服务器执行：
- ✅ **自动续期** — 倒计时 < 15分钟自动续期
- ✅ **自动开机** — 检测到服务器停机自动拉起
- ✅ **TG 通知** — 续期/开机/失败都会推送
- ✅ **多账号支持** — 每个账号独立运行
- ✅ **保活机制** — 防止 HuggingFace 休眠

---

## 🚀 部署步骤（HuggingFace Spaces）

### 第一步：创建 Space

1. 打开 [huggingface.co/new-space](https://huggingface.co/new-space)
2. 填写：
   - **Space name**：随便取，比如 `lemehost`
   - **SDK**：选择 **Gradio**
   - **Hardware**：选 **Free CPU Basic**
   - **Visibility**：选 **Public**
3. 点击 **Create Space**

### 第二步：配置环境变量

> ⚠️ **先配置环境变量，再上传文件**，否则启动时读不到配置

1. 进入 Space 页面
2. 点击右上角 **⚙️ Settings**
3. 找到 **Variables and secrets** 区域
4. 点击 **New secret** 添加以下变量：

| 变量名 | 值 | 必填 | 说明 |
|--------|-----|------|------|
| `LEME` | `邮箱-----密码` | ✅ 必填 | LemeHost 账号密码 注意：不是邮箱密码 |
| `TG_BOT_TOKEN` | Bot Token | 推荐 | Telegram 机器人 Token |
| `TG_CHAT_ID` | Chat ID | 推荐 | Telegram 聊天 ID |
| `TG_API` | 反代地址 | 推荐 | TG API 反代（HF 无法直连）[Cloudflare Workers 自建反代](./_worker.js) |
| `PROJECT_URL` | Space URL | 推荐 | 保活防休眠 |
| `CHECK_INTERVAL` | `300` | 可选 | 检查间隔秒数，默认 300 |
| `RENEW_THRESHOLD` | `900` | 可选 | 续期阈值秒数，默认 900 |

### 第三步：创建文件

在 Space 里点击 **Files** → **Add file** → **Create a new file**，创建以下 3 个文件：

#### `README.md`

```markdown
---
title: oyz8
emoji: 🎮
colorFrom: blue
colorTo: indigo
sdk: gradio
sdk_version: "5.15.0"
app_file: app.py
pinned: false
---
```

#### `requirements.txt`

```
gradio>=5.0.0
requests>=2.28.0
ddddocr>=1.4.0
websocket-client>=1.6.0
```

#### `app.py`

把完整的 [app.py](./app.py) 代码粘贴进去

### 第四步：等待启动

上传完文件后 Space 会自动构建并启动，大约需要 2-3 分钟。

---

## 📝 LEME 格式说明

### 单账号
```
admin@example.com-----123456
```

### 多账号（换行分隔）
```
admin@example.com-----123456
user2@example.com-----abcdef
user3@example.com-----password123
```

> ⚠️ 邮箱和密码之间用 **5个短横线** `-----` 分隔

---

## 📱 配置 Telegram 通知（可选）

### 1. 创建 Bot

1. 打开 Telegram，搜索 **@BotFather**
2. 发送 `/newbot`
3. 按提示输入 Bot 名称
4. 获得 **Bot Token**，格式如：`7594103635:AAEoQKB_xxxxx`

### 2. 获取 Chat ID

1. 打开 Telegram，搜索 **@userinfobot**
2. 发送任意消息
3. 获得你的 **Chat ID**，格式如：`123456789`

### 3. 填入环境变量

- `TG_BOT_TOKEN` = 你的 Bot Token
- `TG_CHAT_ID` = 你的 Chat ID

### 通知样式

```
✅ 续期成功

账号：admin@example.com
服务器: 10108103
🟢 已自动开机
到期: 2026年04月05日 20时35分 -> 2026年04月05日 21时54分

Leme Host Auto Renewal
```

---

## 🛡️ 配置保活（推荐）

防止 HuggingFace Space 因长时间无访问而休眠。

### 获取 Space URL

1. 打开你的 Space 页面
2. 点击右上角 **⋮** 菜单
3. 选择 **Embed this Space**
4. 复制 **Direct URL**，格式如：
   ```
   https://你的用户名-你的项目名.hf.space
   ```

### 填入环境变量

- `PROJECT_URL` = 你的 Space URL

---

## 🖥️ 监控面板

访问你的 Space URL 后加 `/oyz`，比如：

```
https://你的用户名-你的项目名.hf.space/oyz
```

面板显示：
- 🟢 运行状态
- 🖥️ 每台服务器的倒计时和到期时间
- 📋 实时日志
- 📊 续期/跳过/失败统计

---

## ⚙️ 环境变量详解

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LEME` | 无 | 账号密码，必填 |
| `TG_BOT_TOKEN` | 无 | TG Bot Token |
| `TG_CHAT_ID` | 无 | TG 聊天 ID |
| `TG_API` | `https://api.telegram.org` | TG API 地址（可自定义反代） |
| `PROJECT_URL` | 无 | 保活 URL |
| `CHECK_INTERVAL` | `300` | 每隔多少秒检查一次（5分钟） |
| `RENEW_THRESHOLD` | `900` | 倒计时低于多少秒才续期（15分钟） |

---

## ❓ 常见问题

### Q: 为什么收不到 Telegram 通知？

A: HuggingFace 网络限制无法直接访问 `api.telegram.org`，必须配置 `TG_API` 反代地址。可使用 [Cloudflare Workers 自建反代](./_worker.js)。

### Q: 验证码识别成功率低怎么办？

A: 这是正常的，ddddocr 对 LemeHost 验证码识别率约 10-20%。脚本会自动重试最多 30 次登录，30 次续期。通常都能成功。

### Q: 显示 "CF 拦截" 怎么办？

A: Cloudflare 偶尔会拦截请求。脚本会自动等待并重试，通常几分钟后会恢复。

### Q: 服务器开机成功但还是显示停机提示？

A: 正常现象。LemeHost 的 "was recently stopped" 提示不会自动消失，但脚本会通过 WebSocket 检查真实状态，不会重复开机。

### Q: 怎么添加更多账号？

A: 在 `LEME` 环境变量里换行添加：
```
账号1@mail.com-----密码1
账号2@mail.com-----密码2
```

### Q: Space 休眠了怎么办？

A: 配置 `PROJECT_URL` 环境变量启用保活。或者手动访问 Space 页面唤醒。

### Q: 续期页面要求验证码怎么办？

A: 脚本已自动支持续期验证码识别，最多重试 30 次。

---

## 📊 运行逻辑

```
每 5 分钟循环一次：

对每个账号：
  1. 检查登录状态，过期则重新登录
  2. 获取所有服务器
  3. 对每台服务器：
     a. 获取续期页面
     b. 检测是否停机 → WS 检查真实状态 → offline 则开机
     c. 检查倒计时
     d. 倒计时 > 15分钟 → 跳过
     e. 倒计时 ≤ 15分钟 → 续期（有验证码则识别）
     f. 发送 TG 通知
```

---

## ⚠️ 免责声明

本项目仅供学习研究使用。使用本脚本产生的任何后果由使用者自行承担。请遵守 LemeHost 的服务条款。
