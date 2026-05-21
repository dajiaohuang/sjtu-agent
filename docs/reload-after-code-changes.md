# 修改代码后如何生效

当你修改了项目代码（例如修复 bug、调整提示词或更新配置逻辑）后，**必须执行以下步骤才能让修改真正生效**。Python 模块一旦加载到内存就不会自动热更新，缓存数据也可能导致旧结果继续出现。

---

## 1. 确认安装方式

如果你之前运行过 `pip install .`（没有 `-e`），site-packages 中会复制一份旧代码，后续修改 repo 中的源码不会生效。**必须先卸载再改为 editable 模式：**

```bash
pip uninstall sjtu-agent -y
pip install -e .
```

如果已经是 `pip install -e .`，则跳过此步。

---

## 2. 停止所有运行中的进程

### macOS（launchd）

如果你通过 `sjtu-agent install-daemons` 安装了后台服务：

```bash
# 查看当前服务状态
launchctl list | grep sjtu

# 逐个卸载
launchctl unload ~/Library/LaunchAgents/com.sjtu.daily-report.plist
launchctl unload ~/Library/LaunchAgents/com.sjtu.news-digest.plist
launchctl unload ~/Library/LaunchAgents/com.sjtu.remind.plist
launchctl unload ~/Library/LaunchAgents/com.sjtu.telegram-bot.plist
launchctl unload ~/Library/LaunchAgents/com.sjtu.web.plist
launchctl unload ~/Library/LaunchAgents/com.sjtu.wechat-bot.plist

# 强制终止任何残留的 Python 进程
pkill -9 -f "sjtu-agent|wechat_bot|telegram_bot|feishu_bot|remind_check|daily_report|news_digest"
```

### Linux（systemd）

```bash
# 查看状态
systemctl --user list-units | grep sjtu

# 停止所有服务
systemctl --user stop 'sjtu-agent-*'

# 强制终止残留进程
pkill -9 -f "sjtu-agent|wechat_bot|telegram_bot|feishu_bot|remind_check|daily_report|news_digest"
```

### 前台手动运行

如果你是在终端里直接运行 `python wechat_bot.py` 或 `sjtu-agent telegram-bot`：

- 按 `Ctrl+C` 停止当前进程
- 或者另开一个终端执行 `pkill -9 -f wechat_bot`

---

## 3. 清除运行时缓存

DDL、课表等数据有磁盘缓存，旧缓存可能包含错误的时间或格式：

```bash
# macOS
rm -f ~/Library/Application\ Support/sjtu-agent/.ddl_cache.json
rm -f ~/Library/Application\ Support/sjtu-agent/.schedule_cache.json

# Linux
rm -f ~/.local/share/sjtu-agent/.ddl_cache.json
rm -f ~/.local/share/sjtu-agent/.schedule_cache.json
```

---

## 4. 重新启动服务

### 方式 A：重新安装守护进程（推荐用于长期运行）

```bash
sjtu-agent install-daemons
```

这会重新生成 plist/.service 文件并加载最新代码。

### 方式 B：前台手动启动（适合调试）

```bash
sjtu-agent wechat-bot
sjtu-agent telegram-bot
sjtu-agent feishu-bot
```

---

## 5. 验证是否生效

以 DDL 时区修复为例，重启后可以：

1. 检查进程加载的代码路径：
   ```bash
   ps aux | grep wechat_bot
   ```

2. 查看缓存文件是否已重新生成且时间正确：
   ```bash
   cat ~/Library/Application\ Support/sjtu-agent/.ddl_cache.json | python3 -m json.tool
   ```

3. 直接给 Bot 发消息测试功能。

---

## 常见踩坑

| 现象 | 原因 | 解决 |
|------|------|------|
| 修改了代码但行为没变 | 进程未重启，内存中仍是旧模块 | 彻底 kill 并重新启动 |
| `launchctl list` 看到状态 `-9` | 之前用 `kill -9` 强杀，launchd 仍持有旧 plist | 先 `launchctl unload` 再重新 `install-daemons` |
| 删除了 `.ddl_cache.json` 但旧数据还在 | 进程在删除前已把旧数据读入内存 | 先停进程，再删缓存，最后重启 |
| `pip install .` 后改代码不生效 | site-packages 中是旧副本，editable 链接未建立 | `pip uninstall sjtu-agent -y && pip install -e .` |
