# YKST / 树洞原生支持

SJTU Agent 内置 YKST/TreeHole gRPC-Web 客户端，不需要安装额外 MCP 服务即可读取树洞主题、回帖、身份列表，并在二次确认后执行回帖、切换身份、点赞/点踩和收藏等操作。

## 登录

```bash
sjtu-agent ykst-login
```

命令会通过 Chrome DevTools Protocol 自动完成整个 OAuth 流程：打开 jAccount 登录页 → 等待登录完成 → 自动捕获回调 URL → 保存 session token。无需手动复制粘贴。

如果 Chrome 不可用，会回退到手动流程，打印登录 URL 并提示手动传入回调 URL：

```bash
sjtu-agent ykst-login --callback-url "https://web.treehole.space/auth/jaccount?code=..."
```

另有 `--code` 参数可单独传入 OAuth code，以及 `--no-browser` 跳过自动打开浏览器。

## 在对话中使用

对 Agent 说「配置树洞」「登录树洞」即可触发同样流程。Agent 会优先尝试自动捕获，失败时回退到返回登录 URL 并引导用户手动粘贴回调 URL。

## 可用工具（14 个）

| 工具 | 说明 |
|------|------|
| `setup_ykst` | 启动 OAuth 登录（自动捕获回调 URL） |
| `ykst_login_with_callback` | 手动传入回调 URL 或 code 完成登录 |
| `ykst_save_session_token` | 手动保存已知 session token |
| `ykst_auth_status` | 查看当前登录状态和 token 来源 |
| `ykst_get_profile` | 获取账号资料 |
| `ykst_list_identities` | 列出所有身份 |
| `ykst_get_identity` | 按 id / code / active 查找身份 |
| `ykst_set_active_identity` | 切换活跃身份（需确认） |
| `ykst_search_threads` | 关键词搜索主题 |
| `ykst_get_thread` | 读取单个主题 |
| `ykst_get_post` | 读取单个回帖 |
| `ykst_get_thread_posts` | 读取主题回帖列表（分页） |
| `ykst_reply_thread` | 回复主题（需确认） |
| `ykst_rate_thread` / `ykst_rate_post` | 点赞/点踩（需确认） |
| `ykst_favorite_thread` | 收藏/取消收藏（需确认） |

所有写操作（回帖、评分、收藏、切换身份）默认只返回操作草稿，需要用户再次确认后才执行。

## 配置

登录态保存到本机 `config.json` 的 `ykst_treehole_token` 字段。也可通过环境变量覆盖：

- `TREEHOLE_SESSION` / `TREEHOLE_TOKEN` — session token
- `TREEHOLE_RPC_HOST` — RPC 代理地址（默认 `https://proxy.treehole.qaq.ac.cn`）

## 自动捕获登录

自动捕获依赖 Chrome 或 Chromium 浏览器。查找逻辑：

1. `CHROME_PATH` 环境变量
2. 平台默认路径（Windows / macOS / Linux）
3. PATH 中的 `google-chrome`、`chromium` 等可执行文件

可通过以下环境变量微调行为：

- `CHROME_PATH` — Chrome 可执行文件路径
- `TREEHOLE_LOGIN_DEBUG_PORT` — 指定 DevTools 调试端口（留空则自动分配）
- `TREEHOLE_LOGIN_CHROME_PROFILE` — 隔离 Chrome profile 目录（默认系统临时目录）

## 技术实现

客户端从零实现 gRPC-Web 协议（protobuf 序列化/反序列化、gRPC-Web 帧编码、HTTP 传输），不依赖 protobuf 编译产物或 Node.js 运行时。RPC 端点通过 `https://proxy.treehole.qaq.ac.cn` 反向代理访问 YKST 后端服务。

自动捕获方案参考 [ykst_mcp](https://github.com/dajiaohuang/ykst_mcp) 的 `login-watch-browser-url.js`，通过 Chrome DevTools Protocol 轮询 `http://127.0.0.1:{port}/json/list`，实时监听标签页 URL 变化，发现包含 `?code=` 的回调 URL 后自动提取并完成登录。
