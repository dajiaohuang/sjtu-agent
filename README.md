# SJTU Agent

面向上海交通大学学生的校园助手，提供终端对话、Telegram / 飞书 / 微信 Bot、提醒守护进程和 MCP Server。

English summary: A deployable Shanghai Jiao Tong University campus assistant with terminal chat, Telegram / Feishu (Lark) / WeChat bots, reminder daemon, and MCP server.

👉 **[项目展示页](https://kuan-er.github.io/sjtu-agent)**

如果这个项目对你有帮助，欢迎点一个 ⭐ Star，这对我很有意义！

## 安装

macOS / Linux:

```bash
git clone https://github.com/kuan-er/sjtu-agent.git && cd sjtu-agent && bash install.sh
```

Windows PowerShell:

```powershell
git clone https://github.com/kuan-er/sjtu-agent.git; cd sjtu-agent; powershell -ExecutionPolicy Bypass -File .\install.ps1
```

安装脚本会自动完成：创建 `.venv`、安装依赖、安装 Playwright Chromium，然后直接启动 `sjtu-agent setup`。

setup 向导会先保存大模型 API 配置，然后依次引导保存校园平台凭据、自动创建 Canvas Token、从 Chrome 导入 Cookie，最后在 macOS 上一并安装 launchd 后台服务并**自动打开 Web UI**（`http://127.0.0.1:7860`）进行配置。在 setup 过程中可以直接用自然语言回答，也可以输入快捷命令：`status`、`help`、`skip`、`quit`、`open canvas`、`auto canvas`。

## 安装进阶选项

如果只想安装但不立刻进入 setup，或者想跳过 Chromium：

```bash
# macOS / Linux
bash install.sh --no-setup
bash install.sh --skip-playwright
```

```powershell
# Windows
.\install.ps1 -NoSetup
.\install.ps1 -SkipPlaywright
```

手动安装方式：

```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -U pip
pip install -e .
sjtu-agent setup
```

## 配置致远一号 API（推荐）

[致远一号](https://zhiyuan.sjtu.edu.cn) 是上海交通大学官方提供的大模型服务平台，支持 OpenAI 兼容接口，交大师生可免费申请使用。

**接入方式：** 安装完成后直接运行 `sjtu-agent setup`，setup 向导会在第一步引导你输入 API Key 并自动保存，无需手动编辑任何文件。

如需手动配置，在运行时数据目录的 `.env` 文件中填入：

```bash
ZHIYUAN_API_KEY=你的致远一号APIKey
```

Base URL 默认为 `https://models.sjtu.edu.cn/api/v1`，模型默认 `deepseek-chat`（即交大部署的 DeepSeek V3.2），无需额外修改。

可用模型列表：

| 调用名 | 说明 |
|--------|------|
| `deepseek-chat` | DeepSeek V3.2（**默认**）|
| `deepseek-reasoner` | DeepSeek V3.2（推理模式）|
| `glm-5` | GLM 5.0 |
| `minimax` / `minimax-m2.5` | MiniMax M2.5 |
| `qwen3coder` | Qwen3-Coder-30B |
| `qwen3vl` | Qwen3-VL-32B |

**如何申请致远一号 API Key：**

前往 [https://zhiyuan.sjtu.edu.cn](https://zhiyuan.sjtu.edu.cn)，使用 jAccount 登录后在「API 管理」中创建 Key。

如果使用 DeepSeek 官方或其他 OpenAI 兼容接口，在 Web 配置页选择「自定义」，填入对应的 API Key、Base URL 和模型名即可。Base URL 只填服务根地址，不要填到 `/chat/completions`。

---

## 常用命令

```bash
sjtu-agent                # 启动主对话
sjtu-agent setup          # 运行首次配置向导
sjtu-agent doctor         # 查看当前配置状态和运行时路径
sjtu-agent setup-config   # 从浏览器读取 Cookie 并生成 config.json
sjtu-agent login --aihaoke
sjtu-agent ddl --canvas-only
sjtu-agent daily-report --test
sjtu-agent telegram-bot --test
sjtu-agent remind-check --list
sjtu-agent mcp --http --port 8765
sjtu-agent install-daemons
```

也可以直接以模块方式运行：

```bash
python -m sjtu_agent
```

几个常用的 setup 变体：

```bash
sjtu-agent setup
sjtu-agent setup --yes --skip-cookie-import --skip-launchd
sjtu-agent setup --yes --write-daemons-only --output-dir /tmp/sjtu-agent-launchd
```

## macOS 后台服务

在 macOS 上，可以直接用一条命令安装内置 launchd 服务：

```bash
sjtu-agent install-daemons
```

默认会把 LaunchAgent plist 写入 `~/Library/LaunchAgents`，并自动加载到当前用户会话。安装完成后会**自动在浏览器中打开 Web UI**（`http://127.0.0.1:7860`），可以在里面完成所有配置，包括 API Key、平台账号、Telegram Bot Token 等。

- `web`：Web 配置界面，随系统启动，由 launchd 保活
- `daily-report`：每天 `22:00` 运行一次
- `remind-check`：每 `60` 秒运行一次
- `telegram-bot`：登录后启动，并由 launchd 保活

常见变体：

```bash
sjtu-agent install-daemons --write-only
sjtu-agent install-daemons --services daily-report remind-check
sjtu-agent install-daemons --daily-report-time 21:30 --remind-interval 120
```

这些后台服务会使用当前选定的 Python 解释器，以运行时数据目录为工作目录，并把日志写到 `~/Library/Application Support/sjtu-agent/logs`。

## 在飞书中使用 Bot

> 每位用户需要**自建一个飞书应用**作为 bot 的"外壳"（飞书不支持公共多租户 bot，必须挂在你自己的组织/账号下）。整个过程约 5 分钟。

### 一、在飞书开放平台创建应用

1. 打开 https://open.feishu.cn ，扫码登录（用手机飞书扫）
2. 右上角进入 **「开发者后台」** → **「创建企业自建应用」**
   - 应用名称：随便填，比如「SJTU Agent」
   - 描述、图标随意
3. 创建完成后，进入这个应用，记下左上角的 **App ID**（形如 `cli_xxxxxxxxxxxx`）和 **App Secret**

### 二、开启机器人能力 + 权限

在你新建的应用里：

1. **「添加应用能力」** → 找到 **机器人** → 启用
2. **「权限管理」** → 搜索并申请以下 3 个权限（点"申请权限"，无需审批立即生效）：
   - `im:message`
   - `im:message.p2p_msg:readonly`
   - `im:message:send_as_bot`

### 三、订阅消息事件（用长连接）

1. **「事件与回调」** → **「事件配置」**
2. 接收方式切换到 **「使用长连接接收事件」**（**不要**选回调 URL）
3. **「添加事件」** → 搜索 `im.message.receive_v1`（接收消息 v2.0）→ 添加

### 四、发布应用（关键步骤）

> 没发布的话，飞书里搜不到你的 bot。

1. **「版本管理与发布」** → **「创建版本」**
   - 版本号填 `1.0.0`
   - 可用范围：选「全部成员」或「指定成员（包含你自己）」
2. 点 **「申请发布」**
3. 个人开发者会进入"待管理员审批"状态：
   - 如果你的飞书账号是个人版（用手机号注册的"个人组织"），你**自己就是管理员**——打开手机飞书 → 工作台 → 飞书管理后台 → 应用审核 → 通过即可
   - 如果是学校/公司的组织，找管理员审批

### 五、在 SJTU-Agent 里填写配置

1. 终端运行 `sjtu-agent web` 打开 WebUI
2. 找到 **「🪶 飞书 Bot（Lark）」** 卡片，填入：
   - **App ID**：第一步记下的 `cli_xxx`
   - **App Secret**
   - **允许的用户 open_id**：先**留空**，下一步再回来填
3. 点 **「保存」**
4. 点 **「🚀 启动飞书 Bot」**（macOS 会把它注册成后台服务，开机自启 + 崩溃自动重启）

### 六、找到 bot 并锁定为只有你能用

1. 打开飞书（手机/电脑都行）
2. **顶部搜索框搜你的应用名字** → 点头像 → 直接发消息（不用加好友）
3. 第一次发完消息，bot 会回你 agent 的输出。同时**终端里**会打印一行：
   ```
   [feishu] ℹ 白名单为空，已允许所有人；建议把此 open_id 加入白名单：ou_xxxxxxxxxxxx
   ```
4. 复制那个 `ou_xxx`，回到 WebUI 飞书卡片的「允许的用户 open_id」里粘贴 → 保存 → 再点一次「🚀 启动飞书 Bot」重启即可

至此 bot 只会响应你本人，其他人发消息会收到拒绝提示。

### 常见问题

| 现象 | 原因 / 解决 |
|---|---|
| WebUI 里点启动报错 | macOS 才支持一键启动；其他系统手动 `sjtu-agent feishu-bot` |
| 在飞书搜不到自己的 bot | 版本没发布 / 审批没通过 / 可见范围没包含你自己 |
| bot 在线但不回消息 | 「事件与回调」没切到**长连接**，或没订阅 `im.message.receive_v1` |
| 回 "permission denied" / 报权限错 | 权限管理里漏申请了 `im:message:send_as_bot` |
| 想验证 App ID/Secret 对不对 | 终端跑 `sjtu-agent feishu-bot -- --test`（注意中间的 `--`） |
| 想查自己的 open_id | 终端跑 `sjtu-agent feishu-bot -- --whoami`，然后随便发条消息 |

## 配置说明

最重要的运行时文件有三个：

- `config.json`：平台 Token、Cookie、Telegram 配置
- `.env`：jAccount 和 MOOC 账号密码，以及致远一号 API Key
- `agent_config.json`：大模型提供方、Base URL 和模型名（若已在 `.env` 填写 `ZHIYUAN_API_KEY` 则无需此文件）

对于 Canvas，如果 Playwright 和 jAccount 凭据已经就绪，`sjtu-agent setup` 会优先尝试自动创建并保存 Token；如果自动流程失败，再回退到打开 `https://oc.sjtu.edu.cn/profile/settings` 并让你手动确认一次。

## 运行时数据

安装后的命令默认把运行时文件写到用户数据目录，而不是仓库根目录。

- macOS: `~/Library/Application Support/sjtu-agent`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/sjtu-agent`
- Windows: `%APPDATA%/sjtu-agent`

首次导入包时，如果仓库根目录里已经存在这些旧文件，会自动迁移过去：

- `.env`
- `config.json`
- `agent_config.json`
- `reminders.json`
- `remind_state.json`
- `mysjtu_catalog.json`
- `.schedule_cache.json`

## 发布说明

这个仓库已经具备可安装、可分发的包结构和稳定入口；同时，为了保持现有行为稳定，核心平台适配逻辑仍然保留在顶层模块中。
