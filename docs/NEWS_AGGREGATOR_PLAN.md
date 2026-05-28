# SJTU Agent 智能信息聚合推送系统设计

**目标受众**：接手实现的 AI 助手（建议 Sonnet 4.6 在重构完成后实现）
**前置依赖**：`REFACTOR_PLAN.md` 阶段 1（ConfigStore）和阶段 2.1（拆分 agent.py）已完成
**预期工程量**：3-5 天（分 6 个子任务）

---

## 🎯 产品目标

每天 10:00 自动从多个交大相关信息源采集近 24 小时的新内容，结合用户画像智能排序，生成个性化日报推送到 Telegram / WeChat / 系统通知。**用户与 AI 聊得越多，推送越精准。**

### 用户体验示例

```
📰 SJTU 早报 · 2026-05-08 周五

💡 为你精选 5 条（基于你最近关心的「电路实验」「保研」「校园生活」）

────────

🔥 1. 教务处发布 2026 春季学期保研政策（推荐度 95%）
   你最近多次问保研相关问题，这条务必关注。
   📍 教务处官网 · 2小时前
   🔗 https://...

📚 2. 水源热帖：电路实验最后一次答疑安排（推荐度 88%）
   你正在做的电路实验作业，这条解释了几个常见问题。
   📍 水源 · 5小时前 · 32 楼
   🔗 https://...

🍽 3. 餐厅本周菜单更新（推荐度 60%）
   📍 后勤公众号 · 8小时前

... 还有 2 条 [展开全部]
```

---

## 📐 架构设计

### 目录结构

```
sjtu_agent/
└── news_aggregator/
    ├── __init__.py
    ├── sources/                 # 信息源爬虫
    │   ├── __init__.py
    │   ├── base.py              # BaseNewsSource 抽象类
    │   ├── jwc.py               # 教务处（jwc.sjtu.edu.cn）
    │   ├── shuiyuan.py          # 水源社区热帖
    │   ├── official.py          # 交大官网新闻（news.sjtu.edu.cn）
    │   ├── canvas.py            # Canvas 通告
    │   └── wechat_mp.py         # 微信公众号（依赖 wechat_bot ilink）
    ├── profile.py               # 用户画像分析
    ├── ranker.py                # LLM 智能排序
    ├── aggregator.py            # 主聚合流程
    ├── digest.py                # 日报生成器（Markdown）
    └── storage.py               # 已推送去重 + 历史归档

news_digest.py                   # 定时任务入口（launchd 调度）
```

### 核心数据流

```
┌──────────────────────────────────────────────────────────┐
│ 10:00 launchd 触发 news_digest.py                        │
└─────────────────────────┬────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────┐
│ 1. 并发采集（ThreadPoolExecutor）                        │
│    ├─ JwcSource.fetch_recent(hours=24)                  │
│    ├─ ShuiyuanSource.fetch_recent(hours=24)             │
│    ├─ OfficialSource.fetch_recent(hours=24)             │
│    ├─ CanvasSource.fetch_recent(hours=24)               │
│    └─ WechatMpSource.fetch_recent(hours=24)             │
│    → list[NewsItem]                                      │
└─────────────────────────┬────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────┐
│ 2. 去重（与 storage.last_pushed_ids 比对）              │
│ 3. 时间过滤（< 24 小时）                                 │
└─────────────────────────┬────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────┐
│ 4. UserProfile.load() 加载用户画像                       │
│    - 从对话历史提取关键词权重                            │
│    - 兴趣标签（保研/课业/校园生活/二手交易/...）        │
│    - 排斥标签（用户多次跳过的话题）                     │
└─────────────────────────┬────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────┐
│ 5. NewsRanker.rank(news_items, profile) → LLM 调用      │
│    输入：新闻列表（标题+摘要）+ 用户画像                │
│    输出：[(item, score, reason), ...]                    │
│    阈值过滤：score >= 0.5 才推送                         │
└─────────────────────────┬────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────┐
│ 6. DigestBuilder.build(ranked_items)                     │
│    生成 Markdown 日报（带分组、emoji、推荐理由）        │
└─────────────────────────┬────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────┐
│ 7. NotificationDispatcher.send(digest)                   │
│    - Telegram（默认）                                    │
│    - WeChat（如果配置）                                  │
│    - 邮件（可选）                                        │
└─────────────────────────┬────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────┐
│ 8. storage.mark_pushed(news_ids)                         │
│    - 写入 ~/.../sjtu-agent/news_history.json            │
│    - 7 天后自动清理                                      │
└──────────────────────────────────────────────────────────┘
```

---

## 🔧 核心模块详细设计

### 1. BaseNewsSource（信息源基类）

**文件**：`sjtu_agent/news_aggregator/sources/base.py`

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

@dataclass
class NewsItem:
    """统一的新闻数据结构。"""
    id: str                    # 全局唯一 ID（用于去重，建议 source_name + url 哈希）
    source: str                # 来源标识（jwc / shuiyuan / official / wechat_mp:xxx）
    title: str                 # 标题
    summary: str               # 摘要（200 字以内）
    url: str                   # 原文链接
    published_at: datetime     # 发布时间（必须带 CST 时区）
    author: str = ""           # 作者（公众号名 / 水源用户名 / 部门名）
    category: str = ""         # 分类（学术/通知/活动/二手/...）
    tags: list[str] = None     # 标签（用于初步分类）


class BaseNewsSource(ABC):
    """所有信息源的抽象基类。"""
    name: str                  # 子类必须设置
    enabled: bool = True       # 是否启用

    @abstractmethod
    def fetch_recent(self, hours: int = 24) -> list[NewsItem]:
        """获取最近 N 小时的新内容。失败时返回空列表，不抛异常。"""
        ...

    def _make_item_id(self, url: str) -> str:
        """生成唯一 ID。"""
        import hashlib
        return f"{self.name}:{hashlib.md5(url.encode()).hexdigest()[:12]}"
```

### 2. 各信息源实现

#### 2.1 教务处 (`jwc.py`)

**爬取方式**：直接 HTTP 请求（无需登录）
**入口**：`https://jwc.sjtu.edu.cn/xwtg/tztg.htm`（通知通告）
**实现要点**：
- 用 `requests` + `BeautifulSoup` 解析列表页
- 每条公告单独请求详情页提取摘要（前 200 字）
- 公告无明确分类时按标题关键词归类（含「考试」→ 考试通知；含「选课」→ 选课通知）

```python
class JwcSource(BaseNewsSource):
    name = "jwc"
    BASE_URL = "https://jwc.sjtu.edu.cn/xwtg/tztg.htm"

    def fetch_recent(self, hours: int = 24) -> list[NewsItem]:
        # 1. 抓列表页
        # 2. 解析每条 <li>，提取 title / url / date
        # 3. 时间过滤
        # 4. 并发抓取详情页提取摘要
        ...
```

#### 2.2 水源社区 (`shuiyuan.py`)

**爬取方式**：Discourse JSON API（需要 cookie）
**关键 API**：
- `GET /latest.json?order=created` — 最新帖子
- `GET /top.json?period=daily` — 24小时热帖
- `GET /t/{id}.json` — 帖子详情（含正文）

**实现要点**：
- 复用 `config.json` 中已有的 `shuiyuan_user_api_key` 或 `shuiyuan_cookies`
- 优先抓「热帖」（views_count > 100 或 like_count > 5）
- 排除用户在 `~/.../sjtu-agent/news_block_categories.json` 中屏蔽的板块

```python
class ShuiyuanSource(BaseNewsSource):
    name = "shuiyuan"
    BASE = "https://shuiyuan.sjtu.edu.cn"

    def fetch_recent(self, hours: int = 24) -> list[NewsItem]:
        # 1. 调 /top.json?period=daily 抓 24h 热帖
        # 2. 时间过滤 + 板块过滤
        # 3. 调 /t/{id}.json 抓正文摘要
        # 4. 转换为 NewsItem（author = topic.created_by.username）
        ...
```

#### 2.3 交大官网 (`official.py`)

**爬取方式**：RSS 或 HTML 抓取
**入口**：`https://news.sjtu.edu.cn/`（交大新闻网）
**实现要点**：
- 优先尝试 RSS（`/rss.xml`）；失败则降级到 HTML
- 包含「学术活动」「学校要闻」「人物」等分类
- 用 `feedparser` 解析 RSS

#### 2.4 Canvas 通告 (`canvas.py`)

**爬取方式**：Canvas API
**关键 API**：
- `GET /api/v1/users/self/upcoming_events` — 即将到来的事件
- `GET /api/v1/announcements` — 课程通告（按 `context_codes` 过滤）

**实现要点**：
- 复用 `config.json` 中的 `canvas_token`
- 只抓**通告**（announcement），不抓作业（已有 ddl_checker）
- 例：「张老师在《大学物理》发布了新通告：本周课程调整」

#### 2.5 微信公众号 (`wechat_mp.py`)

**这是最复杂的部分**。三种实现方案，按优先级：

**方案 A**：通过 `wechat_bot` 的 ilink 协议订阅公众号消息（推荐）
- wechat_bot 已经登录了用户的微信账号
- 监听"订阅号消息"事件
- 把过去 24 小时内的公众号推送转成 NewsItem
- 需要扩展 `wechat_bot.py` 增加事件订阅 hook

**方案 B**：通过 RSS 中转服务（备选）
- 用户将关注的公众号通过 [WeRSS](https://werss.app/) 或 [feeddd](https://feeddd.org) 转 RSS
- 在 `config.json` 配置 RSS URL 列表
- 用 feedparser 抓取

**方案 C**：完全跳过（MVP 阶段）
- 公众号采集复杂度高，第一版先不做
- 用 RSS 兜底重要公众号（教务处、学指委、共青团）

**推荐 MVP 走方案 C，后续迭代到 A**。

---

### 3. UserProfile 用户画像

**文件**：`sjtu_agent/news_aggregator/profile.py`

#### 3.1 数据存储

**存储路径**：`~/Library/Application Support/sjtu-agent/user_profile.json`

**字段结构**：
```json
{
  "version": 1,
  "updated_at": "2026-05-08T10:00:00+08:00",
  "interests": {
    "保研": 0.9,
    "电路实验": 0.85,
    "考研": 0.3,
    "二手交易": 0.2
  },
  "keywords": {
    "DDL": 18,
    "成绩": 12,
    "GPA": 8,
    "作业": 25,
    "实验": 15
  },
  "blocked_categories": ["健身", "恋爱"],
  "persona_summary": "在校大三学生，主修电类相关专业，关心保研政策与课业。最近在做电路实验。性格务实，偏好直接的信息推送。",
  "conversation_count": 142,
  "last_topics": [
    {"topic": "保研政策咨询", "timestamp": "2026-05-07T14:00:00+08:00"},
    {"topic": "电路实验作业", "timestamp": "2026-05-06T22:30:00+08:00"}
  ]
}
```

#### 3.2 画像更新策略

**更新触发**：
1. **每次对话结束后**（轻量更新）：增量更新 `keywords` 词频
2. **每天 23:00 重算**（深度更新）：
   - 读取最近 7 天的对话历史
   - 调用 LLM 生成 `persona_summary` 和 `interests` 权重
   - 衰减老数据：`interests[k] *= 0.95`（防止画像僵化）

**对话历史来源**：
- Telegram bot：从 `_sessions[chat_id]["messages"]` 序列化导出
- WeChat bot：同上
- Web UI：从 `_chat_history` 导出
- 统一存到 `~/.../sjtu-agent/conversation_log.jsonl`（每行一条对话轮次）

#### 3.3 关键代码

```python
class UserProfile:
    def __init__(self):
        self.path = USER_PROFILE_PATH
        self.data = read_json_safe(self.path, default=self._default())

    @staticmethod
    def _default() -> dict:
        return {
            "version": 1,
            "interests": {},
            "keywords": {},
            "blocked_categories": [],
            "persona_summary": "",
            "conversation_count": 0,
            "last_topics": [],
        }

    def record_conversation(self, user_text: str, agent_reply: str):
        """对话结束后调用：增量更新关键词词频。"""
        # 1. 用 jieba 分词提取名词
        # 2. 过滤停用词
        # 3. 更新 self.data["keywords"]
        # 4. atomic_write_json 保存
        ...

    def deep_update(self, llm_client):
        """每天 23:00 调用：用 LLM 重新生成画像。"""
        # 1. 读取最近 7 天的 conversation_log.jsonl
        # 2. 构造 prompt："以下是用户最近 7 天与 AI 助手的对话，请总结..."
        # 3. 调 LLM 输出 JSON：{interests, persona_summary}
        # 4. 衰减老 interests 权重
        # 5. 合并新结果
        ...

    def is_blocked(self, news_item) -> bool:
        """用户是否屏蔽了这个分类。"""
        return any(cat in news_item.tags for cat in self.data["blocked_categories"])
```

#### 3.4 LLM 画像分析 Prompt

```
你是用户画像分析助手。请分析以下用户与 AI 助手的对话历史，输出结构化画像。

## 对话历史（最近 7 天，共 N 条）
{conversations}

## 任务
输出 JSON：
{
  "persona_summary": "一段 100-150 字的用户画像描述（学习阶段、专业方向、性格、当前关注点）",
  "interests": {
    "标签名": 权重(0-1)
  },
  "recommended_categories": ["用户应该会关心的新闻分类"],
  "avoid_categories": ["用户明显不感兴趣的分类"]
}

## 注意
- interests 权重根据用户提及频次和情感倾向计算
- 标签要具体（如「电路实验」而不是「学习」）
- 最多 8 个标签
- 仅输出 JSON，无额外说明
```

---

### 4. NewsRanker 智能排序

**文件**：`sjtu_agent/news_aggregator/ranker.py`

#### 4.1 排序流程

```python
class NewsRanker:
    def rank(
        self,
        items: list[NewsItem],
        profile: UserProfile,
        top_k: int = 8,
    ) -> list[tuple[NewsItem, float, str]]:
        """
        返回 [(news_item, score, recommendation_reason), ...]
        score 范围 0-1，按降序排列
        """
        # 1. 关键词相关度初筛（无 LLM）
        scored = [(item, self._keyword_score(item, profile)) for item in items]
        scored.sort(key=lambda x: x[1], reverse=True)

        # 2. 取 top 30 进 LLM 精排（节省 token）
        candidates = scored[:30]

        # 3. LLM 批量打分 + 生成推荐理由
        ranked = self._llm_rank(candidates, profile)

        # 4. 阈值过滤 score >= 0.5
        return [r for r in ranked if r[1] >= 0.5][:top_k]
```

#### 4.2 关键词初筛公式

```python
def _keyword_score(self, item, profile) -> float:
    text = f"{item.title} {item.summary}"
    score = 0.0
    for keyword, weight in profile.data["keywords"].items():
        if keyword in text:
            # 词频归一化：log(1 + count) * interest_weight
            score += math.log(1 + weight / 10)
    # 兴趣标签加成
    for tag, weight in profile.data["interests"].items():
        if tag in text:
            score += weight * 0.5
    return min(score, 1.0)
```

#### 4.3 LLM 精排 Prompt

```
你是新闻推荐系统。基于用户画像，给以下新闻打分（0-1）。

## 用户画像
{persona_summary}

关注主题：{interests_top_5}
屏蔽分类：{blocked_categories}

## 候选新闻（共 N 条）
[1] {title} | {source} | {summary}
[2] ...

## 任务
对每条新闻输出 JSON 数组：
[
  {
    "id": 1,
    "score": 0.95,
    "reason": "用户最近多次问保研，这条是官方政策更新"
  },
  ...
]

## 评分标准
- 1.0：用户当前关注的核心问题，必须看
- 0.8：与用户兴趣高度相关
- 0.6：用户可能感兴趣
- 0.4：通用信息，可看可不看
- 0.2：与用户兴趣关联弱
- 0.0：用户明确表示不感兴趣

仅输出 JSON 数组。
```

---

### 5. DigestBuilder 日报生成

**文件**：`sjtu_agent/news_aggregator/digest.py`

#### 5.1 输出格式（Markdown）

```markdown
# 📰 SJTU 早报 · 2026-05-08 周五

💡 为你精选 **5 条**（基于「保研」「电路实验」「校园生活」）

---

## 🔥 重要（推荐度 ≥ 80%）

### 1. 教务处发布 2026 春季学期保研政策（推荐度 95%）
> 用户最近多次问保研相关问题，这条务必关注。

📍 教务处官网 · 2小时前
📝 摘要：本学期保研推荐工作启动，符合条件的同学请于 5月15日前提交申请...
🔗 [阅读原文](https://...)

### 2. 水源热帖：电路实验最后一次答疑安排（推荐度 88%）
> 你正在做的电路实验作业，这条解释了几个常见问题。

📍 水源 · 5小时前 · @张同学 · 32 楼
📝 摘要：本周五下午 2:00-4:00 在电院 4-301，李老师答疑...
🔗 [跳转水源](https://...)

---

## 📚 学习（推荐度 60-80%）

### 3. Canvas 通告：《数据结构》本周作业延期
📍 Canvas · 8小时前 · 王老师
🔗 [查看](https://...)

---

## 🍽 生活

### 4. 后勤公众号：餐厅本周菜单更新（推荐度 60%）
📍 后勤公众号 · 8小时前
🔗 ...

---

💬 _推送越用越准，多和我聊天我会更懂你。回复 /news_block <分类> 屏蔽某类。_
```

#### 5.2 实现

```python
class DigestBuilder:
    def build(
        self,
        ranked: list[tuple[NewsItem, float, str]],
        profile: UserProfile,
    ) -> str:
        if not ranked:
            return "📰 今天没有特别值得关注的新闻。"

        # 按 score 分层
        important = [r for r in ranked if r[1] >= 0.8]
        relevant = [r for r in ranked if 0.6 <= r[1] < 0.8]
        general = [r for r in ranked if r[1] < 0.6]

        # 渲染 markdown
        md = self._render_header(profile)
        if important: md += self._render_section("🔥 重要", important, show_reason=True)
        if relevant: md += self._render_section("📚 相关", relevant)
        if general: md += self._render_section("📌 其他", general)
        md += self._render_footer()
        return md
```

---

### 6. 主聚合器

**文件**：`sjtu_agent/news_aggregator/aggregator.py`

```python
class NewsAggregator:
    def __init__(self):
        self.sources: list[BaseNewsSource] = [
            JwcSource(),
            ShuiyuanSource(),
            OfficialSource(),
            CanvasSource(),
        ]
        self.profile = UserProfile()
        self.ranker = NewsRanker()
        self.builder = DigestBuilder()
        self.storage = NewsStorage()

    def run(self) -> str:
        """完整聚合流程，返回 Markdown 日报。"""
        # 1. 并发采集
        all_items = []
        with ThreadPoolExecutor(max_workers=len(self.sources)) as pool:
            futures = {pool.submit(s.fetch_recent, 24): s for s in self.sources}
            for fut in as_completed(futures):
                try:
                    all_items.extend(fut.result())
                except Exception as e:
                    logger.error(f"Source {futures[fut].name} failed: {e}")

        # 2. 去重
        all_items = self.storage.dedupe(all_items)

        # 3. 用户画像过滤
        all_items = [i for i in all_items if not self.profile.is_blocked(i)]

        # 4. LLM 排序
        ranked = self.ranker.rank(all_items, self.profile, top_k=8)

        # 5. 生成日报
        digest = self.builder.build(ranked, self.profile)

        # 6. 标记已推送
        self.storage.mark_pushed([item.id for item, _, _ in ranked])

        return digest
```

---

### 7. 定时任务入口

**文件**：`news_digest.py`（项目根目录）

```python
#!/usr/bin/env python3
"""
news_digest.py — 智能新闻日报定时任务

运行方式：
  python news_digest.py            # 立即生成并推送
  python news_digest.py --dry-run  # 只生成不推送（调试用）
  python news_digest.py --test     # 测试单个信息源

launchd 定时调度（每天 10:00）见 sjtu_agent/scheduler/launchd.py
"""

import sys
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from sjtu_agent.paths import ENV_PATH
load_dotenv(ENV_PATH)

from sjtu_agent.news_aggregator import NewsAggregator
from sjtu_agent.notifiers import NotificationDispatcher  # 假设阶段 3 已抽出


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test", help="只跑指定信息源（jwc/shuiyuan/official/canvas）")
    args = parser.parse_args()

    aggregator = NewsAggregator()
    digest = aggregator.run()

    print(digest)

    if not args.dry_run:
        dispatcher = NotificationDispatcher()
        dispatcher.send(
            title="📰 SJTU 早报",
            body=digest,
            priority="normal",
        )

if __name__ == "__main__":
    main()
```

### 8. launchd 定时配置

**改动**：`sjtu_agent/scheduler/launchd.py`

增加一个新的 LaunchAgent：

```python
NEWS_DIGEST_PLIST = {
    "label": "com.sjtu.news-digest",
    "program": [sys.executable, str(PROJECT_ROOT / "news_digest.py")],
    "start_calendar_interval": [{"Hour": 10, "Minute": 0}],
    "stdout_path": str(LOG_DIR / "news_digest.log"),
    "stderr_path": str(LOG_DIR / "news_digest.log"),
}
```

并在 `cli.py install-daemons` 中加入此项。

---

## 🚀 实施顺序（6 个子任务）

### 子任务 1：MVP 信息源（教务处 + 水源） — 1 天
- [ ] `sources/base.py` + `NewsItem` 数据结构
- [ ] `sources/jwc.py`（教务处）
- [ ] `sources/shuiyuan.py`（水源热帖）
- [ ] 单测：`tests/test_news_sources.py`
- [ ] 命令行验证：`python news_digest.py --test jwc`

### 子任务 2：用户画像（轻量版） — 1 天
- [ ] `profile.py` 基础结构
- [ ] 实现 `record_conversation`（增量关键词更新）
- [ ] 在 telegram_bot / wechat_bot / web 三个入口的对话结束处调用
- [ ] 写 `conversation_log.jsonl` 持久化
- [ ] 单测：模拟 100 轮对话验证关键词权重

### 子任务 3：智能排序 — 1 天
- [ ] `ranker.py` 关键词初筛
- [ ] LLM 精排 prompt 调试
- [ ] 测试：mock 20 条新闻 + 假画像，验证 score 合理性
- [ ] 边界处理：LLM 返回非 JSON 时降级到关键词分数

### 子任务 4：日报生成 + 推送 — 0.5 天
- [ ] `digest.py` Markdown 渲染
- [ ] `news_digest.py` 入口
- [ ] 复用 NotificationDispatcher（阶段 3 抽出后）
- [ ] 端到端测试：完整跑一遍并推送到 Telegram

### 子任务 5：补充信息源 — 0.5 天
- [ ] `sources/official.py`（交大新闻网 RSS）
- [ ] `sources/canvas.py`（Canvas 通告）
- [ ] 配置示例更新

### 子任务 6：高级功能 — 1 天
- [ ] 深度画像更新（每天 23:00 LLM 重算）
- [ ] 用户屏蔽命令：Telegram `/news_block <分类>` `/news_unblock`
- [ ] launchd 定时配置
- [ ] 文档：`docs/news_digest.md` 用户使用说明

---

## 🔌 集成点

### 与现有系统的集成

| 现有模块 | 改动 |
|---|---|
| `telegram_bot.py` | 在 `_streamed_turn` 结束后调 `profile.record_conversation()` |
| `wechat_bot.py` | 同上 |
| `sjtu_agent/web/server.py` | 同上 |
| `daily_report.py` | 可选：合并到 news_digest 中（统一为「早报」） |
| `care_check.py` | 不冲突，并行运行 |
| `sjtu_agent/cli.py` | 增加 `sjtu-agent news` 命令手动触发 |
| `sjtu_agent/scheduler/launchd.py` | 增加 `com.sjtu.news-digest` 定时任务 |
| `config.example.json` | 增加 `news_digest` 配置块 |

### 配置示例

```json
{
  "news_digest": {
    "enabled": true,
    "schedule_hour": 10,
    "schedule_minute": 0,
    "sources": {
      "jwc": {"enabled": true},
      "shuiyuan": {"enabled": true, "min_views": 100},
      "official": {"enabled": true},
      "canvas": {"enabled": true},
      "wechat_mp": {"enabled": false}
    },
    "top_k": 8,
    "score_threshold": 0.5,
    "channels": ["telegram"],
    "blocked_keywords": ["广告", "代写"]
  }
}
```

---

## ⚠️ 已知风险与对策

| 风险 | 对策 |
|---|---|
| LLM API 配额耗尽（每天精排 20-30 条） | 关键词初筛后只送 top 30 进 LLM；用 prompt caching 降本 |
| 信息源访问失败（教务处 503） | 单源失败不影响整体；记录失败次数，连续 3 天失败自动禁用 |
| 用户画像被错误更新（一次问偏门话题） | 衰减机制：`weights *= 0.95`/天；用户可手动 `/news_reset_profile` |
| 推送疲劳（每天太多无关内容） | `score_threshold` 默认 0.5；用户可调；推送前显示数量预估 |
| 隐私担忧（对话历史本地存储 7 天） | 默认本地不上传；提供 `/news_clear_history` 命令立即清除 |
| 微信公众号采集复杂 | MVP 不做；后续走 RSS 中转方案 |
| 时区错乱（爬虫返回 UTC） | 所有 NewsItem.published_at 必须强制转 CST |
| 重复推送 | NewsStorage 维护 7 天 ID 集合，跨日去重 |

---

## 📊 预期效果

| 指标 | MVP | 完整版 |
|---|---|---|
| 信息源数量 | 2（教务处+水源） | 5+ |
| 每日推送条数 | 5-8 | 5-8（精选） |
| 用户画像维度 | 关键词词频 | 关键词+兴趣+人设 |
| 推送精准度（用户主观评分） | 60% | 85% |
| 单次运行耗时 | 30s | 60s |
| LLM token 消耗 | ~3K | ~8K |

---

## 🔗 给 Sonnet 的执行建议

### 推荐执行顺序

1. **先完成 `REFACTOR_PLAN.md` 阶段 1（ConfigStore）和阶段 2.1（拆分 agent.py）**
2. **再完成阶段 3.1（Notifier 抽象）** — news_digest 直接复用
3. **再实施本文档的子任务 1-6**
4. 每完成一个子任务就 commit + 跑测试

### 验证清单

- [ ] 单源测试通过：`python news_digest.py --test jwc`
- [ ] 完整流程跑通：`python news_digest.py --dry-run`
- [ ] 推送成功：`python news_digest.py` 后 Telegram 收到日报
- [ ] 画像更新生效：跟 bot 聊 5 轮电路相关问题，第二天日报中电路新闻排前
- [ ] launchd 定时生效：`launchctl list | grep news-digest` 显示 PID
- [ ] 失败降级测试：断网情况下能否优雅退出

### 注意事项

1. **隐私优先**：用户画像和对话历史**绝不上传**，所有数据保留在本地
2. **LLM 成本控制**：关键词初筛是必须的，不要把所有新闻都送 LLM
3. **优雅降级**：单个信息源失败不影响整体，LLM 失败回退到关键词分数
4. **可调试性**：所有中间结果（采集到的新闻、画像快照、LLM 响应）都要落到 log 文件
5. **用户可控**：必须提供 `/news_block`、`/news_unblock`、`/news_reset_profile` 命令

---

## 🔗 相关资源

- **当前 commit**：`4731ed2` (2026-05-08)
- **GitHub 仓库**：https://github.com/kuan-er/sjtu-agent
- **依赖文档**：
  - `REFACTOR_PLAN.md` — 必须先完成阶段 1 和 2.1
  - `README.md` — 项目总览
- **关键外部 API**：
  - 教务处通知：`https://jwc.sjtu.edu.cn/xwtg/tztg.htm`
  - 水源 API：`https://shuiyuan.sjtu.edu.cn/top.json?period=daily`
  - 交大新闻：`https://news.sjtu.edu.cn/`
  - Canvas 通告：`/api/v1/announcements`

---

**祝实施顺利！这个特性做好后会让 SJTU Agent 从「工具」升级成「贴身助理」。**
