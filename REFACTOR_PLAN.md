# SJTU Agent 重构执行手册

**目标受众**：接手重构的 AI 助手（Sonnet 4.6）  
**当前版本**：commit `325d873` (2026-05-08)  
**重构优先级**：按 ROI 排序，优先做收益高/风险低的改造

---

## 📋 项目现状概览

### 核心文件与规模

```
agent.py                4236 行  # 单文件巨石：SYSTEM_PROMPT + TOOLS + 98 个函数
ddl_checker.py          2349 行  # 4 个平台爬虫 + 缓存逻辑
telegram_bot.py         1073 行  # Telegram 入口 + 流式 runner
wechat_bot.py            537 行  # WeChat ilink 入口
sjtu_agent/setup_wizard.py 1155 行  # 交互式配置向导
sjtu_agent/web/server.py   772 行  # Web UI 后端
sjtu_agent/web/static/index.html 1355 行  # Web UI 前端
remind_check.py          378 行  # 提醒事项守护进程
care_check.py            237 行  # 主动关怀守护进程
daily_report.py          308 行  # 每日学习日报
shuiyuan_watcher.py      151 行  # 水源社区监控
```

### 架构痛点（按严重程度）

| 问题 | 影响 | 优先级 |
|---|---|---|
| **agent.py 4200 行单文件** | IDE 卡顿、合并冲突、认知负荷高、测试困难 | 🔴 P1 |
| **telegram_bot / wechat_bot 代码重复 65%** | `_capture_turn`、`_init_messages`、`_build_date_ctx` 等逻辑重复 | 🟡 P2 |
| **配置访问散落 30+ 处** | `json.loads(CONFIG_PATH.read_text())` 到处都是，无缓存、无类型检查 | 🟡 P2 |
| **ddl_checker 4 个平台爬虫重复模式** | playwright 启动、cookie 注入、异常处理重复 600+ 行 | 🟠 P3 |
| **推送渠道逻辑散落** | Telegram / WeChat / 系统通知 / 邮件推送分散在 5+ 文件 | 🟠 P3 |
| **无测试覆盖** | 重构风险高、bug 易引入、配置初始化常出错 | 🟠 P3 |
| **日志不统一** | print + logging 混用，无结构化日志、无性能指标 | 🟢 P4 |

---

## 🎯 重构计划（分 4 个阶段）

### 阶段 1：基础设施（必做，后续依赖）

#### 1.1 ConfigStore 单例 ⭐⭐⭐⭐

**目标**：30+ 处 `json.loads(CONFIG_PATH.read_text())` 收敛成一个类，带缓存、类型化、热重载。

**实施步骤**：

1. 创建 `sjtu_agent/config.py`：
   ```python
   class ConfigStore:
       _instance = None
       _cache = {}
       _mtime = {}
       
       @classmethod
       def get_instance(cls) -> "ConfigStore": ...
       
       def get_canvas_token(self) -> str | None: ...
       def get_telegram_token(self) -> str: ...
       def get_telegram_allowed_ids(self) -> list[int]: ...
       def get_jaccount_credentials(self) -> tuple[str, str] | None: ...
       def get_aihaoke_cookies(self) -> dict: ...
       # ... 其他类型化访问接口
       
       def reload_if_changed(self): ...  # 检查文件 mtime，变化时重新加载
   ```

2. 全局替换：
   - `json.loads(CONFIG_PATH.read_text())` → `ConfigStore.get_instance().get_xxx()`
   - 涉及文件：agent.py、ddl_checker.py、telegram_bot.py、wechat_bot.py、remind_check.py、care_check.py、daily_report.py、shuiyuan_watcher.py

3. 验证：
   - 写单测：`tests/test_config.py`
   - 测试热重载：修改 config.json → 调用 `reload_if_changed()` → 验证新值生效

**预期收益**：
- 减少文件 IO（缓存）
- 便于单测（mock ConfigStore）
- 减少初次跑出错（类型检查 + 缺失字段提示）
- 便于后续配置迁移（如支持环境变量覆盖）

**工程量**：小（半天）

---

#### 1.2 测试基线 ⭐⭐⭐

**目标**：`pytest` + 覆盖关键路径（`run_tool` 各分支、`atomic_write_json`、`_capture_turn`）。

**实施步骤**：

1. 创建 `tests/` 目录，添加 `pytest.ini` / `conftest.py`
2. 优先覆盖：
   - `tests/test_config.py`：ConfigStore 读写、热重载、缺失字段
   - `tests/test_paths.py`：`atomic_write_json` / `read_json_safe` 崩溃场景
   - `tests/test_agent_tools.py`：`run_tool("check_setup", {})` 等无副作用工具
   - `tests/test_bot_runner.py`：`_capture_turn` / `_streamed_turn` 的 mock LLM 场景

3. CI 集成（可选）：
   - `.github/workflows/test.yml`：每次 push 跑测试
   - 覆盖率报告：`pytest --cov=sjtu_agent --cov-report=html`

**预期收益**：
- 降低重构风险
- 提高代码质量
- 便于新人上手

**工程量**：大（2-3 天）

---

### 阶段 2：核心模块拆分（高收益）

#### 2.1 拆分 agent.py ⭐⭐⭐⭐

**目标**：4200 行 → 4 个文件，每个 500-1000 行。

**新结构**：
```
sjtu_agent/agent/
├── __init__.py          # 统一导出接口
├── prompts.py           # SYSTEM_PROMPT、_build_date_ctx、_TOOL_LABELS
├── tools.py             # TOOLS 定义 + 所有 tool_xxx 函数（按功能分组）
├── runner.py            # _run_one_turn_openai / _run_one_turn_anthropic / _make_client
└── chat_loop.py         # chat_loop、main、setup_agent_config
```

**实施步骤**：

1. **先拆 prompts.py**（最独立）：
   - 移动 `SYSTEM_PROMPT`、`_build_date_ctx`、`_TOOL_LABELS`
   - 在 `agent/__init__.py` 中 `from .prompts import SYSTEM_PROMPT, _TOOL_LABELS`

2. **再拆 tools.py**：
   - 移动 `TOOLS` 列表定义
   - 移动所有 `tool_xxx` 函数（98 个）
   - 移动 `run_tool` 分发函数
   - 按功能分组注释：
     ```python
     # ── 配置与设置 ──
     def tool_check_setup(): ...
     def tool_save_credentials(): ...
     
     # ── DDL 与作业 ──
     def tool_get_ddls(): ...
     def tool_download_assignments(): ...
     
     # ── 校园服务 ──
     def tool_browse_mysjtu(): ...
     def tool_search_campus(): ...
     
     # ── 成绩与课表 ──
     def tool_query_grades(): ...
     def tool_get_schedule(): ...
     
     # ── 提醒与邮件 ──
     def tool_add_reminder(): ...
     def tool_read_emails(): ...
     
     # ── 其他 ──
     def tool_execute_python(): ...
     ```

3. **拆 runner.py**：
   - 移动 `_is_anthropic_model`、`_make_client`、`_anthropic_tools`
   - 移动 `_run_one_turn_openai`、`_run_one_turn_anthropic`、`_run_one_turn`
   - 移动 `_stream_with_think_tags`（Anthropic 思考标签处理）

4. **拆 chat_loop.py**：
   - 移动 `chat_loop`、`main`
   - 移动 `load_agent_config`、`setup_agent_config`、`_test_llm_connection_simple`

5. **更新所有导入**：
   - 根目录 `agent.py` 改为：
     ```python
     # agent.py — 向后兼容的入口文件
     from sjtu_agent.agent import *
     
     if __name__ == "__main__":
         main()
     ```
   - 或直接删除根目录 `agent.py`，让所有入口改用 `from sjtu_agent.agent import ...`

6. **验证**：
   - 跑所有入口：`python -m sjtu_agent`、`python telegram_bot.py`、`python wechat_bot.py`
   - 跑测试：`pytest tests/`

**预期收益**：
- 降低认知负荷（每个文件 500-1000 行）
- 便于单元测试（工具函数独立）
- 减少合并冲突
- 加快 IDE 响应

**工程量**：中（1 天）

---

#### 2.2 抽 BotRunner 基类 ⭐⭐⭐⭐

**目标**：telegram_bot.py + wechat_bot.py 重复约 65% 代码收敛。

**新结构**：
```
sjtu_agent/bots/
├── __init__.py
├── base.py              # BaseBotRunner 基类
├── telegram.py          # TelegramBotRunner(BaseBotRunner)
└── wechat.py            # WeChatBotRunner(BaseBotRunner)
```

**BaseBotRunner 接口**：
```python
class BaseBotRunner:
    def __init__(self, config: dict):
        self.config = config
        self.sessions: dict[int, dict] = {}
        self.locks: dict[int, threading.Lock] = {}
    
    def _get_session(self, chat_id: int) -> dict:
        """初始化会话：messages / model_box / client_box"""
        ...
    
    def _build_date_ctx(self) -> str:
        """生成当前时间上下文（学年/学期/星期）"""
        ...
    
    def _init_messages(self, sess: dict) -> None:
        """注入 SYSTEM_PROMPT + 日期上下文"""
        ...
    
    def _capture_turn(self, sess: dict, user_text: str) -> str:
        """运行一轮对话，捕获 stdout，返回回复文本"""
        ...
    
    def _streamed_turn(self, sess: dict, user_text: str, on_progress) -> str:
        """流式运行，通过 on_progress 回调报告进度"""
        ...
    
    # 子类实现：
    def send_message(self, chat_id: int, text: str, **kwargs): ...
    def edit_message(self, chat_id: int, message_id: int, text: str, **kwargs): ...
    def delete_message(self, chat_id: int, message_id: int): ...
    def send_typing_action(self, chat_id: int): ...
```

**实施步骤**：

1. 创建 `sjtu_agent/bots/base.py`，实现 `BaseBotRunner`
2. 改造 `telegram_bot.py`：
   - 删除 `_get_session`、`_build_date_ctx`、`_init_messages`、`_capture_turn`、`_streamed_turn`
   - 创建 `TelegramBotRunner(BaseBotRunner)`，实现平台特定方法
   - `handle_text` 改用 `runner.handle_message(msg)`
3. 改造 `wechat_bot.py`：同理
4. 验证：两个 bot 都能正常跑

**预期收益**：
- 减少 400+ 行重复代码
- 统一时间上下文管理
- 便于添加新 bot（Discord / Slack）

**工程量**：中（1 天）

---

### 阶段 3：可选优化（按需）

#### 3.1 Notifier 抽象 ⭐⭐⭐

**目标**：Telegram / WeChat / 系统通知 / 邮件推送统一接口。

**新结构**：
```
sjtu_agent/notifiers/
├── __init__.py
├── base.py              # Notifier 抽象类
├── telegram.py          # TelegramNotifier(Notifier)
├── wechat.py            # WeChatNotifier(Notifier)
├── system.py            # SystemNotifier(Notifier) — plyer / osascript
├── email.py             # EmailNotifier(Notifier)
└── dispatcher.py        # NotificationDispatcher — 根据配置路由
```

**接口**：
```python
class Notifier(ABC):
    @abstractmethod
    def send(self, title: str, body: str, priority: str = "normal") -> bool:
        """发送通知，返回是否成功"""
        ...

class NotificationDispatcher:
    def __init__(self, config: dict):
        self.notifiers: list[Notifier] = []
        # 根据 config 初始化 TelegramNotifier / WeChatNotifier / SystemNotifier
    
    def send(self, title: str, body: str, priority: str = "normal"):
        """向所有已配置渠道发送"""
        for n in self.notifiers:
            try:
                n.send(title, body, priority)
            except Exception as e:
                logging.error(f"Notifier {n} failed: {e}")
```

**实施步骤**：

1. 创建 `sjtu_agent/notifiers/` 子包
2. 实现各 Notifier
3. 改造 `remind_check.py`、`care_check.py`、`daily_report.py`：
   - 删除各自的 Telegram / 系统通知代码
   - 改用 `NotificationDispatcher.send(title, body)`

**预期收益**：
- 便于添加新渠道（钉钉 / 飞书）
- 统一错误处理
- 便于测试（mock Notifier）

**工程量**：中（1 天）

---

#### 3.2 BasePlatform 抽象（ddl_checker）⭐⭐

**目标**：4 个平台爬虫（Canvas / aihaoke / phycai / icourse）抽出共同模式。

**注意**：4 个平台差异较大（Canvas 用 API、aihaoke 用 Playwright、icourse 用 requests），强行抽象可能增加复杂度。**建议先观察，如果重复模式明确再抽**。

**可选方案**：
```python
class BasePlatform(ABC):
    @abstractmethod
    def fetch_ddls(self, cfg: dict) -> list[dict]: ...
    
    def _init_browser(self): ...  # Playwright 平台复用
    def _inject_cookies(self, cookies: dict): ...
```

**工程量**：大（2 天）  
**优先级**：低（当前 ddl_checker 已经优化到 ~1s，收益不大）

---

### 阶段 4：质量提升（长期）

#### 4.1 统一日志 ⭐⭐

**目标**：print + logging 混用 → 统一 loguru / structlog，支持 JSON 输出。

**实施步骤**：

1. 创建 `sjtu_agent/logging.py`：
   ```python
   from loguru import logger
   
   def setup_logging(level: str = "INFO", json_output: bool = False):
       logger.remove()
       if json_output:
           logger.add(sys.stderr, serialize=True, level=level)
       else:
           logger.add(sys.stderr, format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}", level=level)
   ```

2. 全局替换：
   - `print(...)` → `logger.info(...)`
   - `print(f"[WARN] ...")` → `logger.warning(...)`

3. 关键路径添加性能日志：
   ```python
   with logger.contextualize(tool=fn_name):
       t0 = time.monotonic()
       result = agent.run_tool(fn_name, fn_args)
       elapsed = time.monotonic() - t0
       logger.info(f"Tool {fn_name} completed", elapsed_ms=int(elapsed * 1000))
   ```

**预期收益**：
- 便于问题诊断
- 性能优化（看哪个工具慢）
- 生产监控

**工程量**：中（1 天）

---

## 🚀 执行建议（给 Sonnet）

### 推荐顺序

1. **先做阶段 1**（ConfigStore + 测试基线）— 后续所有改动都依赖它们
2. **再做阶段 2.1**（拆 agent.py）— 最高收益，降低后续改动的认知负荷
3. **再做阶段 2.2**（抽 BotRunner）— 减少重复，便于后续维护
4. **阶段 3 按需**（Notifier / BasePlatform）— 如果时间充裕再做
5. **阶段 4 长期**（日志）— 可以边重构边改

### 每个阶段的验证清单

**ConfigStore**：
- [ ] 所有入口（agent / telegram_bot / wechat_bot / remind_check / care_check / daily_report / shuiyuan_watcher）都能正常启动
- [ ] 单测覆盖：读取、缺失字段、热重载

**拆分 agent.py**：
- [ ] `python -m sjtu_agent` 正常启动
- [ ] `python telegram_bot.py` 正常启动
- [ ] `python wechat_bot.py` 正常启动
- [ ] 所有工具调用正常（`run_tool("get_ddls", {})` 等）

**抽 BotRunner**：
- [ ] Telegram bot 正常对话
- [ ] WeChat bot 正常对话
- [ ] 流式进度显示正常

### 注意事项

1. **每次改动后立即跑测试**：`pytest tests/` + 手动测试主要入口
2. **小步提交**：每完成一个子任务就 commit，便于回滚
3. **保持向后兼容**：根目录 `agent.py` 可以保留作为兼容层，避免破坏现有脚本
4. **不要一次性改太多**：阶段 1 + 阶段 2.1 就已经是很大的改动了，先做完这两个再考虑后续

---

## 📊 预期成果

重构完成后：

| 指标 | 改前 | 改后 |
|---|---|---|
| agent.py 行数 | 4236 | ~800（chat_loop.py） |
| 代码重复（telegram/wechat） | 65% | <10% |
| 配置读取 IO | 每次调用都读文件 | 缓存 + 热重载 |
| 测试覆盖率 | 0% | >60%（关键路径） |
| 新人上手时间 | 2-3 天 | 1 天 |
| IDE 响应速度 | 卡顿 | 流畅 |

---

## 🔗 相关资源

- **当前 commit**：`325d873` (2026-05-08)
- **GitHub 仓库**：https://github.com/kuan-er/sjtu-agent
- **关键文件**：
  - `agent.py` — 主 agent 逻辑（待拆分）
  - `ddl_checker.py` — DDL 爬虫
  - `telegram_bot.py` / `wechat_bot.py` — 两个 bot 入口
  - `sjtu_agent/paths.py` — 路径管理 + atomic IO
  - `sjtu_agent/web/server.py` — Web UI 后端

---

**祝重构顺利！有任何疑问随时问用户。**
