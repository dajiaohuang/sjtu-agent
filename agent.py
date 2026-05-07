#!/usr/bin/env python3
"""
SJTU DDL Agent — 自配置交互式聊天助手

特性：
  - 首次运行自动引导配置（无需手动编辑任何文件）
  - 支持任意 OpenAI 兼容 API（DeepSeek、学校集群等）
  - Agent 自动完成 DDL 查询 / 物理实验查询 / 账号配置 / 自动登录

使用方式：
  python3 agent.py
"""

import json
import os
import re
import sys
import threading
import itertools
import time
from pathlib import Path
from dotenv import load_dotenv

# ── 路径设置 ──────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from sjtu_agent.paths import (
    AGENT_CONFIG_PATH,
    CARE_STATE_PATH,
    CONFIG_PATH,
    ENV_PATH,
    MYSJTU_CATALOG_PATH,
    REMINDERS_PATH,
    USER_PROFILE_PATH,
)
from sjtu_agent.terminal_ui import print_markdown_message, print_rule

load_dotenv(ENV_PATH)

import ddl_checker as dc
from openai import OpenAI
from anthropic import Anthropic

# ══════════════════════════════════════════════════════════════════════════════
# 进度指示器
# ══════════════════════════════════════════════════════════════════════════════

def _ansi_supported() -> bool:
    """
    检测当前终端是否值得开启 \r 覆盖式 Spinner 动画。

    Windows 上即使 ANSI 转义序列可用（Windows Terminal / VS Code 终端），
    Spinner 线程的 \\r 写入仍会与 login.py / Playwright 的 print() 产生
    竞争，导致输出闪烁和乱码。因此 Windows 一律禁用动画，降级为单行静态文字。
    """
    if sys.platform == "win32":
        return False
    return True

_ANSI_OK: bool | None = None  # lazy-init

class Spinner:
    """在终端同一行显示动态转圈动画，stop() 后清除该行。
    在不支持 ANSI 的终端（Windows cmd）自动退化为静态文本行。
    每次 start 前先打印一个空行，避免 \\r 覆盖上一行用户输入。
    """
    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, msg: str = ""):
        self._msg   = msg
        self._stop  = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False

    def _ansi(self) -> bool:
        global _ANSI_OK
        if _ANSI_OK is None:
            _ANSI_OK = _ansi_supported()
        return _ANSI_OK

    def start(self, msg: str = "") -> "Spinner":
        if msg:
            self._msg = msg
        if self._started:
            # 已在运行，只更新消息
            return self
        self._stop.clear()
        self._started = True
        if self._ansi():
            # 先换行，确保 \r 不回到用户输入行
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            # Windows 无 ANSI：只打印一行文字
            print(f"… {self._msg}")
        return self

    def update(self, msg: str) -> None:
        self._msg = msg

    def stop(self, final: str = "") -> None:
        self._stop.set()
        self._started = False
        if self._thread:
            self._thread.join()
            self._thread = None
        if self._ansi():
            # 清除整行（包括开头的换行占位）
            sys.stdout.write("\r\033[K")
        if final:
            sys.stdout.write(final + "\n")
        sys.stdout.flush()

    def _spin(self) -> None:
        for frame in itertools.cycle(self._FRAMES):
            if self._stop.is_set():
                break
            sys.stdout.write(f"\r{frame} {self._msg}")
            sys.stdout.flush()
            time.sleep(0.08)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()


# ══════════════════════════════════════════════════════════════════════════════
# 工具定义
# ══════════════════════════════════════════════════════════════════════════════

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_ddls",
            "description": "获取所有平台（Canvas / AI 好课（aihaoke） / 中国大学MOOC）未完成 DDL，按截止时间升序。",
            "parameters": {
                "type": "object",
                "properties": {
                    "skip_canvas":  {"type": "boolean"},
                    "skip_aihaoke": {"type": "boolean"},
                    "skip_icourse": {"type": "boolean"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_next_lab",
            "description": "获取下一次物理实验课（phycai 实验室预约）安排，包括名称、时间、地点。注意：这是实验课预约，不是作业。用户说'实验安排'、'物理实验课'、'下次实验'时调用，不要因为'物理作业'触发此工具。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_all",
            "description": "一次性获取所有平台 DDL 和下一次物理实验安排。",
            "parameters": {
                "type": "object",
                "properties": {
                    "skip_canvas":  {"type": "boolean"},
                    "skip_aihaoke": {"type": "boolean"},
                    "skip_icourse": {"type": "boolean"},
                    "skip_phycai":  {"type": "boolean"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_setup",
            "description": "检查当前环境配置状态：各平台凭证是否存在、Cookie 是否存在。启动时必须调用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "setup_shuiyuan",
            "description": "交互式授权水源社区 Discourse User API Key。会自动打开浏览器，用户在页面点击授权后自动完成，无需手动操作。用户说'配置水源'/'授权水源'/'设置水源'时调用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "setup_canvas",
            "description": "配置 Canvas API Token。优先在具备 jAccount 凭据和 Playwright 时尝试自动创建并保存 token；如果自动流程失败，再回退到手动引导。用户说'配置Canvas'/'设置Canvas'/'Canvas token 不会弄'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "open_browser": {
                        "type": "boolean",
                        "description": "是否尝试打开 Canvas 设置页，默认 true"
                    },
                    "auto_create": {
                        "type": "boolean",
                        "description": "是否尝试通过 Playwright 自动创建并保存 Canvas token，默认 false"
                    },
                    "token_purpose": {
                        "type": "string",
                        "description": "自动创建 token 时填写的用途，默认 SJTU Agent"
                    }
                },
                "required": []
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_credentials",
            "description": "将用户提供的账号凭证保存到本地 .env 和 config.json，仅传入已提供的字段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "jaccount_username": {"type": "string", "description": "交大 jAccount 用户名（用于 AI 好课（aihaoke）和物理实验）"},
                    "jaccount_password": {"type": "string", "description": "交大 jAccount 密码"},
                    "canvas_token":      {"type": "string", "description": "Canvas API Token"},
                    "mooc_username":     {"type": "string", "description": "中国大学MOOC 手机号"},
                    "mooc_password":     {"type": "string", "description": "中国大学MOOC 密码"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "login_platform",
            "description": "为指定平台执行 Playwright 自动登录，刷新 Cookie。保存凭证后调用此工具验证。",
            "parameters": {
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "enum": ["aihaoke", "phycai", "icourse"],
                    },
                },
                "required": ["platform"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_assignment_files",
            "description": (
                "列出本地 assignments/ 目录下已下载的作业文件。"
                "用户问「有哪些作业」「下载了什么」「列出作业文件」时调用。"
                "返回课程-作业-文件的树状结构。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "course_filter": {
                        "type": "string",
                        "description": "只列出名称包含此字符串的课程，留空则列出全部",
                    },
                    "assignments_dir": {
                        "type": "string",
                        "description": "作业目录，默认 ./assignments",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_assignment_file",
            "description": (
                "读取本地作业文件的文字内容（支持 PDF 和 HTML）。"
                "用户问「第一题是什么」「这道题怎么做」「帮我看看作业内容」时，"
                "先用 list_assignment_files 找到文件路径，再调用此工具读取内容，然后回答。"
                "注意：PDF 中的数学公式可能无法完整提取，需结合上下文理解。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "文件的完整路径（从 list_assignment_files 结果获取）",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "最多返回的字符数，默认 8000，超长文档可分段读取",
                    },
                    "start_page": {
                        "type": "integer",
                        "description": "PDF 从第几页开始读（1-indexed），默认 1",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_assignments",
            "description": (
                "下载近期作业材料（Canvas 题目说明/附件 + AI 好课（aihaoke）作业页面截图/附件），"
                "保存到本地 assignments/ 目录。返回每个作业的保存路径。"
                "用户说「下载作业」「帮我把题目下载下来」时调用。"
                "默认只下载近期作业；如果上下文里已经明确提到某门课或某个作业，必须传过滤条件，避免扫全平台。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skip_canvas":  {"type": "boolean", "description": "跳过 Canvas，默认 false"},
                    "skip_aihaoke": {"type": "boolean", "description": "跳过 AI 好课（aihaoke），默认 false"},
                    "course_filter": {
                        "type": "string",
                        "description": "只下载名称包含此字符串的课程。上下文已明确课程时必须填写",
                    },
                    "assignment_filter": {
                        "type": "string",
                        "description": "只下载名称包含此字符串的作业。上下文已明确作业名时必须填写",
                    },
                    "due_within_days": {
                        "type": "integer",
                        "description": "只下载未来多少天内截止的作业，默认 7。若用户明确要求全部长期作业，可设更大值",
                    },
                    "output_dir": {
                        "type": "string",
                        "description": "保存目录，默认 ./assignments",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_canvas_assignments",
            "description": (
                "列出 Canvas 上允许文件提交（online_upload）的作业，含课程ID、作业ID。"
                "用户想提交作业但没有提供 course_id/assignment_id 时，先调此工具让用户确认目标作业。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "course_filter": {
                        "type": "string",
                        "description": "只列出名称包含此字符串的课程，留空则列全部",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_canvas_assignment",
            "description": (
                "将本地文件上传并提交到 Canvas 指定作业。"
                "必须先知道 course_id 和 assignment_id（可先调 list_canvas_assignments 获取）。"
                "用户把 PDF/文件拖入终端后得到路径，说'帮我提交这个文件'时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "本地文件的绝对路径，如 /Users/xxx/hw1.pdf",
                    },
                    "course_id": {
                        "type": "integer",
                        "description": "Canvas 课程 ID（从 list_canvas_assignments 获取）",
                    },
                    "assignment_id": {
                        "type": "integer",
                        "description": "Canvas 作业 ID（从 list_canvas_assignments 获取）",
                    },
                    "comment": {
                        "type": "string",
                        "description": "可选：提交时附加的文字备注",
                    },
                },
                "required": ["file_path", "course_id", "assignment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_campus",
            "description": (
                "搜索交大校园相关网站的内容。"
                "支持：jwc（教务处通知公告）、shuiyuan（水源社区论坛帖子）、dyweb（传承·交大课程资料）。"
                "重要：若用户明确指定了某个网站（如'水源'、'教务处'、'传承'），"
                "必须在 sites 中只填该网站，不得多填其他网站。"
                "只有用户未指定网站时才搜全部。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，如「期末考试」「选课」「转专业」",
                    },
                    "sites": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["jwc", "shuiyuan", "dyweb"]},
                        "description": (
                            "要搜索的网站。"
                            "用户说'水源/水源社区/bbs'→必须只填[\"shuiyuan\"]；"
                            "用户说'教务处/jwc'→必须只填[\"jwc\"]；"
                            "用户说'传承/dyweb'→必须只填[\"dyweb\"]；"
                            "用户未指定平台→不传此参数，搜全部。"
                            "绝对不能在用户只要水源时多加jwc或dyweb。"
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "每个网站最多返回几条结果，默认 6",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_schedule",
            "description": (
                "查询课表。"
                "用户问「今天有什么课」「明天几点上课」「本周课表」「下周有没有课」等时调用。"
                "query_type='day' 查某天课程，query_type='week' 查某周课表。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query_type": {
                        "type": "string",
                        "enum": ["day", "week"],
                        "description": "day=查某天，week=查某周",
                    },
                    "date": {
                        "type": "string",
                        "description": (
                            "query_type=day 时使用。"
                            "'今天'/'明天'/'后天'/'昨天' 或 'YYYY-MM-DD'，留空=今天"
                        ),
                    },
                    "week_offset": {
                        "type": "integer",
                        "description": (
                            "query_type=week 时使用。"
                            "0=本周（默认），1=下周，-1=上周"
                        ),
                    },
                    "set_semester_start": {
                        "type": "string",
                        "description": (
                            "如果用户告知学期起始日期，传入 YYYY-MM-DD（必须是周一）。"
                            "仅在用户明确说出起始日期时才传。"
                        ),
                    },
                    "refresh": {
                        "type": "boolean",
                        "description": "true=强制忽略缓存重新拉取课表。仅在用户明确说「刷新课表」「更新课表」时才传 true。",
                    },
                },
                "required": ["query_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browse_mysjtu",
            "description": (
                "在浏览器中自动操作 my.sjtu.edu.cn 完成查询或业务办理。"
                "适用于：查成绩、查绩点、查奖学金、查培养方案、办理注册手续、预约校车班车、办理各类申请等。"
                "不适用于：课表（用 get_schedule）、DDL（用 get_ddls）、搜索（用 search_campus）。"
                "遇到需要点击、填表、导航的情况也可以用，通过 action 参数传入操作指令。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "要完成的任务，用自然语言描述，例如「查看本学期所有课程成绩」「预约明天去徐汇的班车」",
                    },
                    "start_url": {
                        "type": "string",
                        "description": "起始 URL，默认 https://my.sjtu.edu.cn，可指定具体子页面加快速度",
                    },
                    "action": {
                        "type": "string",
                        "description": (
                            "可选的具体操作指令（在上一次 browse_mysjtu 返回页面内容后用）。"
                            "格式：'click:文本' 点击包含该文本的链接/按钮；"
                            "'goto:URL' 直接跳转；"
                            "'search:关键词' 在搜索框输入并搜索。"
                            "留空则只读取当前/起始页面内容。"
                        ),
                    },
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "refresh_mysjtu_catalog",
            "description": (
                "爬取 my.sjtu.edu.cn 所有分类和服务，建立本地缓存供后续快速查找。"
                "首次使用 browse_mysjtu 前可先调用一次，以后每隔数周刷新一次即可。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_grades",
            "description": (
                "直接从教学信息服务网 (i.sjtu.edu.cn) 查询学生成绩和绩点，自动完成 jAccount SSO。"
                "用户说「查成绩」「上学期成绩」「查绩点」「GPA」「看看我的成绩」「本学年成绩」等时调用。"
                "比 browse_mysjtu 更快更准，直接返回结构化的成绩列表和加权绩点。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "year": {
                        "type": "string",
                        "description": (
                            "学年起始年份，如 '2025' 表示 2025-2026 学年。"
                            "不传或空字符串=查全部学年。"
                            "'上学年'/'去年'→当前年份减1；"
                            "'本学年'/'今年'→当前年份（如 2025）。"
                        ),
                    },
                    "semester": {
                        "type": "string",
                        "enum": ["", "1", "2", "3"],
                        "description": (
                            "'1'=第1学期(秋季/上学期)，'2'=第2学期(春季/下学期)，"
                            "'3'=第3学期(夏季)，''=全部学期。"
                            "用户说'上学期'→通常是'1'（秋季学期）；'下学期'→'2'；不指定→''。"
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_reminder",
            "description": (
                "添加一条提醒事项到本地列表。"
                "用户说「帮我记一下」「提醒我」「记得要...」「把 XXX 加到提醒」时调用。"
                "start 是提醒开始时间（或事项截止时间），end 是可选的结束时间。"
                "若用户未提供具体时间，从上下文推断或询问。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "提醒标题，简洁描述事项"},
                    "start": {"type": "string", "description": "开始时间，格式 'YYYY-MM-DD HH:MM'"},
                    "end":   {"type": "string", "description": "结束时间（可选），格式 'YYYY-MM-DD HH:MM'"},
                    "note":  {"type": "string", "description": "备注说明（可选）"},
                },
                "required": ["title", "start"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_reminders",
            "description": (
                "查看所有提醒事项（分为未过期/已过期）。"
                "用户说「我有什么提醒」「提醒事项」「记了什么」时调用。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_reminder",
            "description": "删除指定 id 的提醒事项。",
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {"type": "integer", "description": "要删除的提醒 id"},
                },
                "required": ["reminder_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_user_profile",
            "description": (
                "将本轮对话中观察到的用户信息更新到本地用户画像文件。"
                "每当你从对话中了解到用户的新信息（姓名/学号/专业/课程偏好/作息/情绪状态/"
                "近期压力/兴趣爱好/特殊事件等），就调用此工具记录。"
                "不要等用户主动说「更新画像」，而是每轮对话结束前自动判断是否有新信息需要记录。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "updates": {
                        "type": "object",
                        "description": (
                            "要更新的字段（只传有新信息的字段，不要覆盖未提及的字段）。\n"
                            "常用字段示例：\n"
                            "  name: str — 姓名或昵称\n"
                            "  major: str — 专业\n"
                            "  grade: str — 年级（如 大二）\n"
                            "  courses: list[str] — 正在上的课程\n"
                            "  stress_level: str — 近期压力（low/medium/high/overwhelmed）\n"
                            "  mood: str — 情绪（happy/normal/tired/anxious/sad）\n"
                            "  recent_events: list[str] — 近期重要事件（考试/答辩/面试/生日等）\n"
                            "  hobbies: list[str] — 兴趣爱好\n"
                            "  sleep_pattern: str — 作息（如 late_night/normal/early）\n"
                            "  last_active: str — 最后活跃时间（ISO 格式，自动填当前时间）\n"
                            "  care_notes: list[str] — 需要定期关怀提示（如 '明天考物理'）\n"
                            "  custom: dict — 其他自定义字段"
                        ),
                        "additionalProperties": True,
                    },
                    "reason": {
                        "type": "string",
                        "description": "简述为什么更新这些字段（供调试参考）",
                    },
                },
                "required": ["updates"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_profile",
            "description": (
                "读取当前用户画像，了解用户的基本信息、情绪状态、近期事件等。"
                "在准备给用户发送关怀消息或个性化回复前先调用，确保不重复关怀。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "setup_telegram",
            "description": (
                "配置 Telegram Bot：将 telegram_token 和可选的 allowed_ids 保存到 config.json，"
                "然后可以用 sjtu-agent telegram-bot 启动 Bot。"
                "用户说「接入Telegram」「配置Telegram」「怎么用Telegram」「Telegram bot」时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "telegram_token": {
                        "type": "string",
                        "description": "BotFather 给出的 Bot Token，格式如 1234567890:ABCdefGHI…",
                    },
                    "allowed_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "允许使用 Bot 的 Telegram user_id 列表（整数）。留空则 Bot 启动后会显示任意用户的 chat_id，可先留空再补填。",
                    },
                },
                "required": ["telegram_token"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "setup_wechat",
            "description": (
                "配置微信 ilink Bot：打印登录二维码，让用户扫码完成微信接入，"
                "bot_token 自动保存到 config.json。"
                "用户说「接入微信」「配置微信」「微信 bot」「微信推送」时调用。"
                "注意：扫码登录必须在终端完成，此工具会打印二维码并等待用户扫码确认。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_python",
            "description": (
                "在当前项目环境中动态执行 Python 代码片段，用于完成没有现成工具的任务。"
                "当你想做某件事但没有对应工具时（例如：标记邮件已读、批量操作、数据处理、"
                "调用任意 API、读写文件等），先尝试写代码解决，实在做不到再报错。"
                "代码可以 import 任何已安装的包（imaplib/smtplib/requests/json/os 等）。"
                "代码中 print() 的输出会作为结果返回。"
                "注意：代码运行在受信任的本地环境，可以直接访问 os.environ、CONFIG_PATH 等。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "要执行的 Python 代码。"
                            "可通过 import agent, ddl_checker as dc 引入项目模块。"
                            "结果用 print() 输出，或直接 raise 异常报错。\n"
                            "示例：将所有未读邮件设为已读：\n"
                            "  import imaplib, ssl, os\n"
                            "  ctx = ssl.create_default_context()\n"
                            "  m = imaplib.IMAP4_SSL('mail.sjtu.edu.cn', 993, ctx)\n"
                            "  user = os.environ['JACCOUNT_USERNAME'] + '@sjtu.edu.cn'\n"
                            "  m.login(user, os.environ['JACCOUNT_PASSWORD'])\n"
                            "  m.select('INBOX')\n"
                            "  m.uid('STORE', '1:*', '+FLAGS', '\\\\Seen')\n"
                            "  print('OK')\n"
                            "  m.logout()"
                        ),
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时秒数，默认 60",
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_emails",
            "description": (
                "通过 IMAP 读取交大邮箱（mail.sjtu.edu.cn）邮件列表。"
                "用户说「看看邮件」「有没有新邮件」「读一下邮件」「查收件箱」时调用。"
                "默认读收件箱最新 10 封；也可以指定 uid 精确读取某一封的正文。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "description": "文件夹：INBOX（收件箱，默认）/ Sent（已发送）/ Drafts / Trash / 中文别名（收件箱/已发送/垃圾邮件）",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回几封，默认 10",
                    },
                    "unread_only": {
                        "type": "boolean",
                        "description": "只看未读邮件，默认 false",
                    },
                    "with_body": {
                        "type": "boolean",
                        "description": "同时返回邮件正文，默认 false（仅列表时不返回正文，节省 token）",
                    },
                    "uid": {
                        "type": "string",
                        "description": "指定读取某一封邮件的完整正文（uid 从 read_emails 列表结果中获取）",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_emails",
            "description": (
                "按关键词搜索交大邮箱中的邮件（标题/发件人/全文）。"
                "用户说「找一下关于 XXX 的邮件」「搜索来自 XXX 的邮件」时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词",
                    },
                    "folder": {
                        "type": "string",
                        "description": "搜索的文件夹，默认 INBOX",
                    },
                    "search_in": {
                        "type": "string",
                        "enum": ["SUBJECT", "FROM", "TEXT", "TO"],
                        "description": "搜索范围：SUBJECT（主题，默认）/ FROM（发件人）/ TEXT（全文）/ TO（收件人）",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回几封，默认 10",
                    },
                    "with_body": {
                        "type": "boolean",
                        "description": "同时返回搜索结果的正文，默认 false",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": (
                "用交大邮箱发送邮件（SMTP SSL）。"
                "用户说「发一封邮件给 XXX」「回复这封邮件」「帮我发邮件」时调用。"
                "发送前先向用户确认收件人、主题和正文内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "收件人邮箱地址，多个用逗号分隔",
                    },
                    "subject": {
                        "type": "string",
                        "description": "邮件主题",
                    },
                    "body": {
                        "type": "string",
                        "description": "邮件正文（纯文本）",
                    },
                    "cc": {
                        "type": "string",
                        "description": "抄送地址（可选，逗号分隔）",
                    },
                    "reply_to_uid": {
                        "type": "string",
                        "description": "回复某封邮件时传入原邮件 uid（自动补 In-Reply-To 头）",
                    },
                    "folder": {
                        "type": "string",
                        "description": "reply_to_uid 所在文件夹，默认 INBOX",
                    },
                },
                "required": ["to", "subject", "body"],
            },
        },
    },
]

# ══════════════════════════════════════════════════════════════════════════════
# my.sjtu.edu.cn 服务目录缓存
# ══════════════════════════════════════════════════════════════════════════════

_CANVAS_DEFAULT_BASE_URL = "https://oc.sjtu.edu.cn"
_CANVAS_SETUP_REASON = (
    "Canvas Access Token 只会在用户点击“新增访问令牌”后显示一次，"
    "当前 Agent 只会在具备 jAccount 凭据和 Playwright 时尝试自动创建；"
    "如果自动流程不可靠或失败，就会回退到手动引导。"
)
_CANVAS_SETUP_STEPS = [
    "打开 Canvas 并完成 jAccount 登录。",
    "进入「账户 / Account」->「设置 / Settings」。",
    "在页面下方找到「已批准的集成 / 访问许可证」。",
    "点击「+ 新增访问令牌 / New Access Token」。",
    "用途建议填写 SJTU Agent；过期时间可按需设置。",
    "复制弹出的 token（只显示一次），原样发给我。",
    "我会调用 save_credentials 把 token 保存到本地 config.json。",
]

# 常见别名映射（中文俗称 → 服务名关键词）
_MYSJTU_ALIASES: dict[str, list[str]] = {
    "班车": ["学生预约乘车", "Shuttle Bus"],
    "校车": ["学生预约乘车", "Shuttle Bus"],
    "乘车": ["学生预约乘车"],
    "预约乘车": ["学生预约乘车"],
    "洗澡": ["学生洗浴"],
    "洗浴": ["学生洗浴"],
    "电费": ["宿舍电费"],
    "报修": ["自助报修"],
    "宿舍报修": ["自助报修"],
    "网络报修": ["学生宿舍网络报修"],
    "开网": ["学生宿舍开网申请"],
    "心理": ["心理咨询"],
    "就业": ["就业服务"],
    "实习": ["就业服务"],
    "发票": ["我的发票"],
    "报销": ["智能报销"],
    "缴费": ["在线缴费"],
    "学费": ["学费情况", "在线缴费"],
    "宿舍": ["住在交大"],
    "失物": ["失物招领"],
    "地图": ["电子地图"],
    "热线": ["54741234热线平台"],
    "投诉": ["54741234热线平台"],
    "天文台": ["光启天文台预约"],
    "进校": ["学生亲友进校备案"],
    "亲友": ["学生亲友进校备案"],
    "电动车": ["两轮电动自行车实名登记"],
    "体育场": ["Sports Venue Booking"],
    "场馆": ["Sports Venue Booking"],
    "会议室": ["会议室预约平台"],
    "助学贷款": ["助学贷款信息登记"],
    "绿色通道": ["绿色通道"],
    "减免": ["学费减免申请"],
    "档案": ["人事档案状态查询"],
    "成绩": ["本科生电子成绩单"],
    "成绩单": ["本科生电子成绩单", "第二课堂成绩单"],
    "接种": ["预防接种"],
    "疫苗": ["预防接种"],
    "宾馆": ["交大宾馆预订"],
    "酒店": ["交大宾馆预订"],
    "等级考试": ["等级考试"],
    "四六级": ["等级考试"],
    "IP申请": ["IP申请"],
    "预约羽毛球场": ["场馆预约"],
    "场馆": ["场馆预约"],
    "体育馆": ["场馆预约"],
    "预约场地": ["场馆预约"],
    "电子成绩单": ["本科生电子成绩单"],
    "学业成绩": ["本科生电子成绩单"],
    "课表": ["学在交大"],
    "课程表": ["学在交大"],
    "我的课表": ["学在交大"],
    "培养方案": ["学在交大"],
    "选课": ["学在交大", "学生选课特殊申请"],
    "图书馆座位": ["交圕座位预约"],
    "图书馆空间": ["交圕空间预约"],
    "图书馆会议室": ["交圕会议室预约"],
    "借书": ["当前借阅", "历史借阅", "图书馆权限（门禁/借书）开通申请"],
    "借阅": ["当前借阅", "历史借阅"],
    "开门时间": ["开放时间"],
    "开放时间": ["开放时间"],
    "教务": ["学在交大"],
    "教务服务": ["学在交大"],
    "在线缴费": ["在线缴费"],
    "交学费": ["在线缴费"],
}

_MYSJTU_STOPWORDS = [
    "帮我", "一下", "看看", "查看", "看一下", "看", "查一下", "查", "去", "我要", "我想",
    "想", "服务", "业务", "办理", "申请", "入口", "页面", "系统", "功能", "使用", "打开",
]

_MYSJTU_CATEGORY_ALIASES: dict[str, list[str]] = {
    "图书馆": ["图书馆"],
    "教务": ["教务处", "学在交大", "教学服务"],
    "教务服务": ["教务处", "学在交大", "教学服务"],
    "学习": ["学在交大", "教学服务"],
    "缴费": ["财务", "后勤"],
    "报修": ["信息服务", "后勤", "图书馆"],
    "宿舍": ["生活服务", "信息服务"],
    "体育": ["智慧体育"],
    "场馆": ["智慧体育"],
    "校园卡": ["生活服务", "信息服务", "财务"],
}

_MYSJTU_SEARCH_ONLY_HINTS = {
    "图书馆", "教务", "教务服务", "校园卡", "信息服务", "生活服务", "财务", "后勤",
}


def _canvas_settings_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/profile/settings"


def _canvas_openid_connect_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/login/openid_connect"


def _canvas_auto_setup_state() -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception as exc:
        return False, f"Playwright 不可用：{exc}"

    username = os.environ.get("JACCOUNT_USERNAME", "").strip()
    password = os.environ.get("JACCOUNT_PASSWORD", "").strip()
    if not username or not password:
        return False, "缺少 jAccount 用户名或密码"

    return True, "ready"


def _canvas_click_first(page, selectors: list[str], timeout: int = 5000) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = locator.count()
            for idx in range(count):
                candidate = locator.nth(idx)
                try:
                    candidate.wait_for(state="visible", timeout=timeout)
                    candidate.click()
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _canvas_fill_first(page, selectors: list[str], value: str, timeout: int = 5000) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = locator.count()
            for idx in range(count):
                candidate = locator.nth(idx)
                try:
                    candidate.wait_for(state="visible", timeout=timeout)
                    candidate.fill(value)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _extract_canvas_token(page) -> str:
    try:
        inputs = page.locator("input, textarea")
        for idx in range(inputs.count()):
            try:
                node = inputs.nth(idx)
                if not node.is_visible():
                    continue
                value = node.input_value().strip()
            except Exception:
                continue
            if re.fullmatch(r"[A-Za-z0-9_\-]{20,}", value):
                return value
    except Exception:
        pass

    for selector in ["code", "pre", ".ic-Form-control", ".ui-dialog-content", ".ReactModal__Content", "body"]:
        try:
            nodes = page.locator(selector)
        except Exception:
            continue
        for idx in range(nodes.count()):
            try:
                node = nodes.nth(idx)
                if selector != "body" and not node.is_visible():
                    continue
                text = node.inner_text(timeout=1500).strip()
            except Exception:
                continue
            match = re.search(r"([A-Za-z0-9_\-]{20,})", text)
            if match:
                return match.group(1)
    return ""


def _auto_create_canvas_token(base_url: str, token_purpose: str = "SJTU Agent") -> dict:
    ready, reason = _canvas_auto_setup_state()
    if not ready:
        return {"success": False, "error": reason}

    username = os.environ.get("JACCOUNT_USERNAME", "").strip()
    password = os.environ.get("JACCOUNT_PASSWORD", "").strip()
    settings_url = _canvas_settings_url(base_url)
    openid_connect_url = _canvas_openid_connect_url(base_url)

    try:
        from playwright.sync_api import sync_playwright
        import login as login_module
    except Exception as exc:
        return {"success": False, "error": f"自动创建前置依赖不可用：{exc}"}

    try:
        print("[Canvas] 正在启动浏览器并尝试自动创建 token…", flush=True)
        with sync_playwright() as playwright:
            # 始终使用无头模式：有界面模式在 Windows 终端/CI 环境中容易卡死
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()

            print("[Canvas] 正在打开 Canvas 设置页…", flush=True)
            page.goto(settings_url, wait_until="domcontentloaded", timeout=30_000)

            if "/login/canvas" in page.url or page.url.rstrip("/").endswith("/login"):
                print("[Canvas] 检测到 Canvas 登录页，正在跳转到 jAccount 单点登录…", flush=True)
                page.goto(openid_connect_url, wait_until="domcontentloaded", timeout=30_000)

            if "jaccount.sjtu.edu.cn" in page.url:
                print("[Canvas] 检测到 jAccount 登录页，正在尝试登录…", flush=True)
                if not login_module._fill_jaccount(page, username, password):
                    browser.close()
                    return {"success": False, "error": "jAccount 登录失败，无法自动创建 Canvas token"}

            print("[Canvas] 已进入 Canvas，正在定位 token 设置…", flush=True)
            # 用 load 代替 networkidle，避免在复杂页面无限等待
            try:
                page.goto(settings_url, wait_until="load", timeout=30_000)
            except Exception:
                page.goto(settings_url, wait_until="domcontentloaded", timeout=30_000)
            # 额外等待 JS 渲染完成
            page.wait_for_timeout(2000)

            if not _canvas_click_first(
                page,
                [
                    "text=New Access Token",
                    "text=创建新访问许可证",
                    "text=新增访问令牌",
                    "text=+ New Access Token",
                    "text=+ 创建新访问许可证",
                    "text=+ 新增访问令牌",
                    "button:has-text('New Access Token')",
                    "button:has-text('创建新访问许可证')",
                    "button:has-text('新增访问令牌')",
                    "a:has-text('New Access Token')",
                    "a:has-text('创建新访问许可证')",
                    "a:has-text('新增访问令牌')",
                ],
            ):
                browser.close()
                return {"success": False, "error": "没有在 Canvas 设置页找到创建访问令牌的入口"}

            print("[Canvas] 已打开新建 token 对话框，正在填写用途…", flush=True)
            # 等待对话框出现
            page.wait_for_timeout(800)
            _canvas_fill_first(
                page,
                [
                    "input[name='purpose']",
                    "input[id*='purpose']",
                    "input[placeholder*='Purpose']",
                    "input[placeholder*='用途']",
                    ".ui-dialog input[type='text']",
                    ".ReactModal__Content input[type='text']",
                    "dialog input[type='text']",
                ],
                token_purpose,
            )

            if not _canvas_click_first(
                page,
                [
                    "button:has-text('Generate Token')",
                    "button:has-text('生成令牌')",
                    "button:has-text('生成')",
                    "button:has-text('Submit')",
                    "button:has-text('确定')",
                    "a:has-text('生成令牌')",
                    ".ReactModal__Content button.btn-primary",
                    ".ui-dialog button.btn-primary",
                    ".ui-dialog button[type='submit']",
                    ".ReactModal__Content button[type='submit']",
                ],
            ):
                browser.close()
                return {"success": False, "error": "没有找到生成 token 的确认按钮"}

            print("[Canvas] 正在等待 token 出现…", flush=True)
            # 等待 token 显示区域出现（最多 8 秒）
            _token_appeared = False
            for _sel in [
                "input[value]",
                ".ic-Form-control",
                ".ui-dialog-content",
                ".ReactModal__Content",
                "code",
                "pre",
            ]:
                try:
                    page.wait_for_selector(_sel, timeout=8_000)
                    _token_appeared = True
                    break
                except Exception:
                    continue
            if not _token_appeared:
                page.wait_for_timeout(3000)
            token = _extract_canvas_token(page)
            browser.close()
    except Exception as exc:
        return {"success": False, "error": f"自动创建 Canvas token 失败：{exc}"}

    if not token:
        return {"success": False, "error": "Canvas 已触发生成流程，但没有成功读取到 token"}

    print("[Canvas] 已读取到 token，正在保存到本地配置…", flush=True)
    tool_save_credentials(canvas_token=token)
    return {
        "success": True,
        "auto_created": True,
        "settings_url": settings_url,
        "token_saved": True,
        "token_purpose": token_purpose,
    }


def _normalize_mysjtu_task(task: str) -> str:
    text = (task or "").lower().strip()
    text = re.sub(r"[\s\u3000]+", "", text)
    text = re.sub(r"[，。！？、,.!?:：;；/\\\-_+=()（）\[\]【】<>《》\"'`~@#$%^&*]+", "", text)
    for word in sorted(_MYSJTU_STOPWORDS, key=len, reverse=True):
        text = text.replace(word, "")
    return text


def _mysjtu_grams(text: str) -> set[str]:
    if not text:
        return set()
    if len(text) == 1:
        return {text}
    return {text[i:i+2] for i in range(len(text) - 1)}


def _mysjtu_category_matches(task_norm: str, category: str) -> bool:
    category_norm = _normalize_mysjtu_task(category)
    if category_norm and category_norm in task_norm:
        return True
    for hint, categories in _MYSJTU_CATEGORY_ALIASES.items():
        if hint in task_norm and category in categories:
            return True
    return False


def _mysjtu_search_keyword(task: str) -> str:
    task_norm = _normalize_mysjtu_task(task)
    if not task_norm:
        return (task or "").strip()[:10]
    for hint in sorted(_MYSJTU_CATEGORY_ALIASES, key=len, reverse=True):
        if hint in task_norm:
            return hint
    return task_norm[:10]


def _extract_libseat_context(current_url: str, text: str) -> dict | None:
    """为图书馆座位预约系统补充解释，避免把首页统计误判为当前可预约状态。"""
    if "libseat.sjtu.edu.cn" not in current_url and "图书馆座位预约系统" not in text:
        return None

    is_homepage = "#/ic/home" in current_url or current_url.rstrip("/") == "https://libseat.sjtu.edu.cn"
    library_counts = []
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^(.*?图书馆.*?|.*?阅览室.*?)\((\d+)/(\d+)\)$", line)
        if m:
            library_counts.append({
                "name": m.group(1).strip(),
                "display_count": f"{m.group(2)}/{m.group(3)}",
            })

    warning = None
    if is_homepage:
        warning = (
            "当前页面是图书馆座位系统首页统计页。首页里的“空闲/总数”和馆区汇总数字不等于“此刻一定可以预约”，"
            "闭馆、未到开放时段或需要进入具体日期/时段时，首页仍可能显示这些统计。"
            "只有进入具体日期/时段的选座页面并看到可选座位后，才能确认当前可预约。"
        )

    return {
        "site": "libseat",
        "is_homepage": is_homepage,
        "booking_status": "unverified" if is_homepage else "unknown",
        "warning": warning,
        "library_counts": library_counts[:8],
    }


def _load_mysjtu_catalog() -> list[dict]:
    """加载本地服务目录缓存，不存在则返回空列表。"""
    if not MYSJTU_CATALOG_PATH.exists():
        return []
    try:
        data = json.loads(MYSJTU_CATALOG_PATH.read_text(encoding="utf-8"))
        return data.get("services", [])
    except Exception:
        return []


def _find_mysjtu_service(task: str, catalog: list[dict]) -> dict | None:
    """
    在服务目录中根据任务描述找最匹配的服务。
    匹配策略：别名优先 → 服务名子串 → 分类子串。
    返回 {'name', 'url', 'category'} 或 None。
    """
    if not catalog:
        return None

    task_raw = (task or "").strip()
    task_norm = _normalize_mysjtu_task(task_raw)
    generic_search_only = task_norm in _MYSJTU_SEARCH_ONLY_HINTS

    # 1. 别名匹配
    for alias, names in sorted(_MYSJTU_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True):
        if alias in task_raw or alias in task_norm:
            for target in names:
                for svc in catalog:
                    if target in svc["name"]:
                        return svc

    if generic_search_only:
        return None

    # 2. 服务名子串匹配（任务包含服务名的关键字）
    best: dict | None = None
    best_score = 0.0
    task_grams = _mysjtu_grams(task_norm)
    for svc in catalog:
        name = svc.get("name", "")
        category = svc.get("category", "")
        name_norm = _normalize_mysjtu_task(name)
        if not name_norm:
            continue

        score = 0.0
        if task_norm == name_norm:
            score += 2.0
        elif task_norm and task_norm in name_norm:
            score += 1.1

        name_grams = _mysjtu_grams(name_norm)
        if task_grams and name_grams:
            overlap = len(task_grams & name_grams)
            if overlap >= 2:
                score += overlap / len(name_grams)
            elif overlap == 1:
                score += 0.1

        if _mysjtu_category_matches(task_norm, category):
            score += 0.35

        if score > best_score:
            best_score = score
            best = svc

    if best_score >= 0.55:
        return best

    return None


def tool_refresh_mysjtu_catalog() -> dict:
    """爬取 my.sjtu.edu.cn 所有分类的服务，建立本地缓存。直接从 Vue 组件数据提取 URL，无需点击。"""
    try:
        from playwright.sync_api import sync_playwright as _spw
    except ImportError:
        return {"error": "未安装 playwright"}

    cfg = dc.load_config()
    jaccount_cookies = cfg.get("jaccount_cookies", {})
    if not jaccount_cookies:
        return {"error": "未配置 jAccount cookie，请先配置 jAccount"}

    catalog: list[dict] = []
    seen: set[str] = set()

    _JS_EXTRACT = """() => {
        const appEls = document.querySelectorAll('.app.cursor-pointer');
        const results = [];
        for (const el of appEls) {
            const vk = Object.keys(el).find(k => k.startsWith('__vue'));
            if (!vk) continue;
            const comp = el[vk];
            const app = comp && comp._props && comp._props.app;
            if (app && app.name && app.uri) {
                results.push({name: app.name, url: app.uri, id: app.id || ''});
            }
        }
        return results;
    }"""

    with _spw() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        ctx.add_cookies([
            {"name": k, "value": v, "domain": ".sjtu.edu.cn", "path": "/"}
            for k, v in jaccount_cookies.items()
        ])

        page = ctx.new_page()
        page.goto("https://my.sjtu.edu.cn", wait_until="domcontentloaded", timeout=20_000)
        page.wait_for_timeout(2000)

        all_cats = page.locator(".type-item-text").all_text_contents()

        for cat in all_cats:
            cat = cat.strip()
            if not cat:
                continue
            try:
                page.locator(".type-item-text", has_text=cat).first.click()
                page.wait_for_timeout(500)

                apps = page.evaluate(_JS_EXTRACT)
                for app in apps:
                    name = app.get("name", "").strip()
                    if name and name not in seen:
                        seen.add(name)
                        catalog.append({
                            "name": name,
                            "url": app["url"],
                            "id": app.get("id", ""),
                            "category": cat,
                        })
            except Exception:
                continue

        browser.close()

    import datetime
    MYSJTU_CATALOG_PATH.write_text(
        json.dumps({
            "updated": datetime.date.today().isoformat(),
            "services": catalog,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "ok": True,
        "count": len(catalog),
        "message": f"已缓存 {len(catalog)} 个服务，保存于 {MYSJTU_CATALOG_PATH.name}",
    }

# ══════════════════════════════════════════════════════════════════════════════
# 工具实现
# ══════════════════════════════════════════════════════════════════════════════

def _serialize_ddl(item: dict, now=None) -> dict:
    import datetime as _dt
    if now is None:
        now = _dt.datetime.now(dc.CST)
    total_seconds = (item["due"] - now).total_seconds()
    hours_left    = int(total_seconds / 3600)
    return {
        "platform":   item["platform"],
        "course":     item["course"],
        "name":       item["name"],
        "due":        item["due"].strftime("%Y-%m-%d %H:%M"),   # 已转为 CST，无需带 tz
        "hours_left": hours_left,                               # 负数=已过期
        "expired":    total_seconds < 0,
        "submitted":  item.get("submitted", False),
    }


def _serialize_lab(lab: dict | None) -> dict | None:
    if not lab:
        return None
    dt = lab["dt"]
    return {
        "name":     lab["name"],
        "datetime": dt.isoformat(),
        "weekday":  dc.WEEKDAY_ZH[dt.weekday()],
        "time_str": lab["time_str"],
        "room":     lab["room"],
    }


def tool_check_setup() -> dict:
    env_user  = os.environ.get("JACCOUNT_USERNAME", "")
    env_pass  = os.environ.get("JACCOUNT_PASSWORD", "")
    mooc_user = os.environ.get("MOOC_USERNAME", "")
    mooc_pass = os.environ.get("MOOC_PASSWORD", "")
    agent_cfg = load_agent_config()
    canvas_auto_ready, canvas_auto_reason = _canvas_auto_setup_state()

    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass

    def has_cookies(key: str) -> bool:
        return bool(cfg.get(key))

    return {
        "agent": {
            "configured": bool(agent_cfg.get("api_key") and agent_cfg.get("model")),
            "base_url": agent_cfg.get("base_url") or None,
            "model": agent_cfg.get("model") or None,
        },
        "jaccount": {
            "has_credentials": bool(env_user and env_pass),
            "username": env_user or None,
        },
        "canvas": {
            "has_token": bool(cfg.get("canvas_token") and not cfg.get("canvas_token", "").startswith("YOUR_")),
            "settings_url": _canvas_settings_url(cfg.get("canvas_base_url", _CANVAS_DEFAULT_BASE_URL)),
            "setup_tool": "setup_canvas",
            "can_auto_fetch": canvas_auto_ready,
            "auto_fetch_reason": canvas_auto_reason,
        },
        "aihaoke": {
            "has_credentials": bool(env_user and env_pass),
            "has_cookies": has_cookies("aihaoke_cookies"),
        },
        "phycai": {
            "has_credentials": bool(env_user and env_pass),
            "has_cookies": has_cookies("phycai_cookies"),
        },
        "icourse": {
            "has_credentials": bool(mooc_user and mooc_pass),
            "mooc_username": mooc_user or None,
            "has_cookies": has_cookies("icourse_cookies"),
        },
        "shuiyuan": {
            "has_api_key": bool(cfg.get("shuiyuan_user_api_key")),
            "has_cookies": bool(cfg.get("shuiyuan_cookies")),
        },
        "config_file_exists": CONFIG_PATH.exists(),
    }


def tool_setup_canvas(open_browser: bool = True, auto_create: bool = False, token_purpose: str = "SJTU Agent") -> dict:
    """提供 Canvas Token 生成引导，并在条件允许时尝试自动创建 token。"""
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass

    base_url = cfg.get("canvas_base_url", _CANVAS_DEFAULT_BASE_URL).rstrip("/")
    settings_url = _canvas_settings_url(base_url)
    token = cfg.get("canvas_token", "").strip()
    has_existing_token = bool(token and not token.startswith("YOUR_"))
    token_valid = None
    can_auto_fetch, auto_fetch_reason = _canvas_auto_setup_state()

    if has_existing_token:
        try:
            import requests as _req
            resp = _req.get(
                f"{base_url}/api/v1/users/self/profile",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            token_valid = resp.status_code == 200
        except Exception:
            token_valid = None

    if auto_create:
        auto_result = _auto_create_canvas_token(base_url, token_purpose=token_purpose)
        auto_result.setdefault("settings_url", settings_url)
        auto_result.setdefault("can_auto_fetch", can_auto_fetch)
        auto_result.setdefault("auto_fetch_reason", auto_fetch_reason)
        auto_result.setdefault("has_existing_token", has_existing_token)
        auto_result.setdefault("existing_token_valid", token_valid)
        if auto_result.get("success"):
            auto_result.setdefault("next_action", "Canvas token 已经自动保存到本地 config.json。")
            return auto_result
        auto_result.setdefault("reason", _CANVAS_SETUP_REASON)
        auto_result.setdefault("steps", _CANVAS_SETUP_STEPS)
        auto_result.setdefault("next_action", "自动流程失败后，你仍然可以手动生成 token 并粘贴给我保存。")
        return auto_result

    opened_browser = False
    if open_browser:
        try:
            import webbrowser
            opened_browser = bool(webbrowser.open(settings_url))
        except Exception:
            opened_browser = False

    return {
        "success": True,
        "can_auto_fetch": can_auto_fetch,
        "auto_fetch_reason": auto_fetch_reason,
        "reason": _CANVAS_SETUP_REASON,
        "base_url": base_url,
        "settings_url": settings_url,
        "opened_browser": opened_browser,
        "has_existing_token": has_existing_token,
        "existing_token_valid": token_valid,
        "steps": _CANVAS_SETUP_STEPS,
        "next_action": "生成后把 token 原样发给我，我会调用 save_credentials 保存。",
    }


def tool_setup_shuiyuan() -> dict:
    """用 Playwright 登录水源社区，保存 session cookie（User API Key 方案已废弃）。"""
    username = os.environ.get("JACCOUNT_USERNAME", "").strip()
    password = os.environ.get("JACCOUNT_PASSWORD", "").strip()
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass

    if not username and not cfg.get("jaccount_cookies"):
        return {
            "error": "需要先配置 jAccount 凭据（save_credentials）",
            "next_action": "请先用 save_credentials 保存 jAccount 用户名和密码，再重试「配置水源」。",
        }

    return _setup_shuiyuan_session(cfg, username, password)


def _setup_shuiyuan_session(cfg: dict, username: str, password: str) -> dict:
    """降级方案：Playwright 登录水源，保存 session cookie。"""
    manual_note = (
        "水源社区没有固定的 API 设置页面；不要去偏好设置里找 API。"
        "如果 User API Key 授权不可用，session cookie 就是当前的降级方案。"
    )

    def _shuiyuan_session_error(message: str) -> dict:
        return {
            "error": message,
            "manual_note": manual_note,
            "next_action": (
                "如果自动登录失败，可以稍后重新说“配置水源”再试一次。"
                "当前项目对水源的可用凭据不一定是 API Key，也可能是 session cookie。"
            ),
        }

    try:
        from playwright.sync_api import sync_playwright as _sync_pw
    except ImportError:
        return _shuiyuan_session_error("未安装 playwright")

    try:
        import login as login_module
    except Exception as e:
        return _shuiyuan_session_error(f"加载登录模块失败：{e}")

    jaccount_cookies = cfg.get("jaccount_cookies", {})

    new_session: dict = {}
    with _sync_pw() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        if jaccount_cookies:
            ctx.add_cookies([
                {"name": k, "value": v, "domain": "jaccount.sjtu.edu.cn", "path": "/"}
                for k, v in jaccount_cookies.items()
            ])
        page = ctx.new_page()
        try:
            page.goto("https://shuiyuan.sjtu.edu.cn/", wait_until="networkidle", timeout=20_000)
        except Exception:
            pass
        if "jaccount" in page.url:
            if not username or not password:
                browser.close()
                return {"error": "需要 jAccount 凭据，请先用 save_credentials 配置"}
            try:
                if not login_module._fill_jaccount(page, username, password):
                    browser.close()
                    return _shuiyuan_session_error("jAccount 登录失败，请检查账号密码")
                try:
                    page.wait_for_url("**/shuiyuan.sjtu.edu.cn/**", timeout=15_000)
                except Exception:
                    pass
                new_ja = {c["name"]: c["value"] for c in ctx.cookies()
                          if "jaccount" in c.get("domain", "")}
                if new_ja:
                    cfg["jaccount_cookies"] = new_ja
            except Exception as e:
                browser.close()
                return _shuiyuan_session_error(f"jAccount 登录失败：{e}")
        new_session = {c["name"]: c["value"] for c in ctx.cookies()
                       if "shuiyuan.sjtu.edu.cn" in c.get("domain", "")}
        browser.close()

    if not new_session:
        return _shuiyuan_session_error("未能获取水源社区 session，请检查账号")

    cfg["shuiyuan_cookies"] = new_session
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    return {"success": True, "message": f"水源社区 session 登录成功（需定期更新）"}


def tool_save_credentials(
    jaccount_username: str = "",
    jaccount_password: str = "",
    canvas_token: str = "",
    mooc_username: str = "",
    mooc_password: str = "",
) -> dict:
    updated = []

    env_lines: list = []
    if ENV_PATH.exists():
        env_lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    def set_env(key, value):
        nonlocal env_lines
        line = f"{key}={value}"
        for i, l in enumerate(env_lines):
            if l.startswith(f"{key}="):
                env_lines[i] = line
                return
        env_lines.append(line)

    if jaccount_username:
        set_env("JACCOUNT_USERNAME", jaccount_username)
        os.environ["JACCOUNT_USERNAME"] = jaccount_username
        updated.append("jAccount 用户名")
    if jaccount_password:
        set_env("JACCOUNT_PASSWORD", jaccount_password)
        os.environ["JACCOUNT_PASSWORD"] = jaccount_password
        updated.append("jAccount 密码")
    if mooc_username:
        set_env("MOOC_USERNAME", mooc_username)
        os.environ["MOOC_USERNAME"] = mooc_username
        updated.append("MOOC 用户名")
    if mooc_password:
        set_env("MOOC_PASSWORD", mooc_password)
        os.environ["MOOC_PASSWORD"] = mooc_password
        updated.append("MOOC 密码")

    if any([jaccount_username, jaccount_password, mooc_username, mooc_password]):
        ENV_PATH.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass

    cfg.setdefault("canvas_base_url", "https://oc.sjtu.edu.cn")
    cfg.setdefault("aihaoke_cookies", {})
    cfg.setdefault("phycai_cookies", {})
    cfg.setdefault("icourse_cookies", {})

    if canvas_token:
        cfg["canvas_token"] = canvas_token
        updated.append("Canvas Token")

    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    return {"saved": updated, "success": True}


def tool_login_platform(platform: str) -> dict:
    if not CONFIG_PATH.exists():
        return {"success": False, "error": "config.json 不存在，请先保存凭证"}
    cfg = json.loads(CONFIG_PATH.read_text())
    try:
        if platform == "aihaoke":
            print("  [Playwright 自动登录 aihaoke，浏览器窗口会短暂出现…]", flush=True)
            ok, error = dc.refresh_aihaoke_cookies(cfg)
            if not ok:
                return {"success": False, "error": error}
            result = dc.fetch_aihaoke(cfg)
            return {"success": True, "platform": "aihaoke", "ddl_count": len(result)}
        elif platform == "phycai":
            print("  [Playwright 自动登录物理实验平台…]", flush=True)
            result = dc.fetch_phycai(cfg)
            return {"success": True, "platform": "phycai", "lab": _serialize_lab(result)}
        elif platform == "icourse":
            print("  [Playwright 自动登录中国大学MOOC…]", flush=True)
            result = dc.fetch_icourse(cfg)
            return {"success": True, "platform": "icourse", "ddl_count": len(result)}
        else:
            return {"success": False, "error": f"未知平台: {platform}"}
    except Exception as e:
        return {"success": False, "error": str(e)}



# ── DDL 持久磁盘缓存 ─────────────────────────────────────────────────────────
# 缓存文件存放在 DATA_DIR/.ddl_cache.json，进程重启后依然有效。
# TTL = 15 分钟（900 秒）；若缓存命中则直接返回，避免每次都发起网络请求。

from sjtu_agent.paths import DDL_CACHE_PATH as _DDL_CACHE_PATH

_DDL_CACHE_TTL = 900  # 秒（15 分钟）


def _ddl_cache_load() -> dict:
    """从磁盘读取缓存，返回 {cache_key: {"ts": float, "data": list}} 字典。"""
    try:
        if _DDL_CACHE_PATH.exists():
            import json as _json
            return _json.loads(_DDL_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _ddl_cache_save(store: dict) -> None:
    """将缓存字典写入磁盘。"""
    try:
        import json as _json
        _DDL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DDL_CACHE_PATH.write_text(
            _json.dumps(store, ensure_ascii=False, default=str), encoding="utf-8"
        )
    except Exception:
        pass


def _ddl_cache_get(cache_key: str) -> list | None:
    """从磁盘缓存读取指定 key，若 TTL 未超期则返回数据，否则返回 None。"""
    import time as _t
    import datetime as _dt
    store = _ddl_cache_load()
    entry = store.get(cache_key)
    if not entry:
        return None
    if _t.time() - entry.get("ts", 0) > _DDL_CACHE_TTL:
        return None
    # 反序列化 due 字段（JSON 存为字符串）
    raw_list = entry.get("data", [])
    result = []
    for item in raw_list:
        item = dict(item)
        if isinstance(item.get("due"), str):
            try:
                item["due"] = _dt.datetime.fromisoformat(item["due"])
            except Exception:
                pass
        if isinstance(item.get("dt"), str):
            try:
                item["dt"] = _dt.datetime.fromisoformat(item["dt"])
            except Exception:
                pass
        result.append(item)
    return result


def _ddl_cache_set(cache_key: str, data: list) -> None:
    """将 data 写入磁盘缓存（datetime 自动序列化为 ISO 格式字符串）。"""
    import time as _t
    import datetime as _dt

    def _serialize(obj):
        if isinstance(obj, _dt.datetime):
            return obj.isoformat()
        return str(obj)

    store = _ddl_cache_load()
    import json as _json
    serializable = _json.loads(_json.dumps(data, default=_serialize))
    store[cache_key] = {"ts": _t.time(), "data": serializable}
    _ddl_cache_save(store)


def _fetch_ddls_parallel(cfg: dict, skip_canvas=False, skip_aihaoke=False, skip_icourse=False) -> list:
    """并行拉取各平台 DDL，返回合并列表（未排序）。
    优先使用 15 分钟内的磁盘缓存，缓存命中时无需发起任何网络请求。
    """
    import concurrent.futures as _cf

    cache_key = f"{skip_canvas},{skip_aihaoke},{skip_icourse}"
    cached = _ddl_cache_get(cache_key)
    if cached is not None:
        return cached

    tasks = []
    if not skip_canvas:   tasks.append(("canvas",  lambda: dc.fetch_canvas(cfg)))
    if not skip_aihaoke:  tasks.append(("aihaoke", lambda: dc.fetch_aihaoke(cfg)))
    if not skip_icourse:  tasks.append(("icourse", lambda: dc.fetch_icourse(cfg)))

    all_ddl: list = []
    if not tasks:
        return all_ddl

    with _cf.ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = {pool.submit(fn): name for name, fn in tasks}
        for fut in _cf.as_completed(futures):
            try:
                all_ddl.extend(fut.result())
            except Exception as e:
                print(f"[DDL] {futures[fut]} 拉取失败：{e}")

    _ddl_cache_set(cache_key, all_ddl)
    return all_ddl


def _prefetch_ddls_background() -> None:
    """在独立子进程中静默预热 DDL 缓存，不阻塞主进程，不向终端输出任何内容。
    子进程的 stdout/stderr 统一重定向到 devnull，完全不干扰主进程终端。
    """
    import subprocess as _sp
    import sys as _sys
    import os as _os

    cached = _ddl_cache_get("False,False,False")
    if cached is not None:
        return  # 缓存仍有效，无需预热

    # 用 -c 片段在子进程里静默执行拉取
    _script = (
        "import sys, os; sys.path.insert(0, os.path.dirname(sys.argv[0]) or '.'); "
        "import agent as _a, ddl_checker as _dc; "
        "_a._fetch_ddls_parallel(_dc.load_config())"
    )
    try:
        _sp.Popen(
            [_sys.executable, "-c", _script],
            stdout=_sp.DEVNULL,
            stderr=_sp.DEVNULL,
            cwd=str(Path(__file__).resolve().parent),
        )
    except Exception:
        pass  # 预热失败不影响主进程


def _check_for_updates() -> None:
    """
    在后台线程中检查 git 远程是否有新提交。
    若检测到更新，启动完成后打印一行提示，引导用户运行 sjtu-agent update。
    非 git 仓库 / 无网络时静默失败，不影响任何功能。
    """
    import shutil as _shutil
    import subprocess as _sub

    git = _shutil.which("git")
    if not git:
        return

    project_root = str(Path(__file__).resolve().parent)
    try:
        # 检查是否在 git 仓库内
        r = _sub.run(
            [git, "rev-parse", "--is-inside-work-tree"],
            cwd=project_root, capture_output=True, timeout=5,
        )
        if r.returncode != 0:
            return

        # 静默 fetch（只更新远端引用，不改变本地分支）
        _sub.run(
            [git, "fetch", "--quiet", "--no-tags", "origin"],
            cwd=project_root, capture_output=True, timeout=15,
        )

        # 比较本地 HEAD 与 origin/HEAD（或 origin/main）
        local_hash = _sub.run(
            [git, "rev-parse", "HEAD"],
            cwd=project_root, capture_output=True, timeout=5,
        ).stdout.decode().strip()

        # 尝试 @{u}（跟踪分支），失败则 origin/main
        r2 = _sub.run(
            [git, "rev-parse", "@{u}"],
            cwd=project_root, capture_output=True, timeout=5,
        )
        if r2.returncode == 0:
            remote_hash = r2.stdout.decode().strip()
        else:
            r3 = _sub.run(
                [git, "rev-parse", "origin/main"],
                cwd=project_root, capture_output=True, timeout=5,
            )
            if r3.returncode != 0:
                return
            remote_hash = r3.stdout.decode().strip()

        if local_hash and remote_hash and local_hash != remote_hash:
            # 统计落后几个提交
            r4 = _sub.run(
                [git, "rev-list", "--count", f"{local_hash}..{remote_hash}"],
                cwd=project_root, capture_output=True, timeout=5,
            )
            behind = r4.stdout.decode().strip() if r4.returncode == 0 else "?"
            # 存入模块级变量，启动完成后打印
            _UPDATE_AVAILABLE["behind"] = behind
    except Exception:
        pass  # 网络不通或其他异常，静默忽略


# 用于在主线程启动完成后读取后台更新检查结果
_UPDATE_AVAILABLE: dict = {}


def tool_get_ddls(skip_canvas=False, skip_aihaoke=False, skip_icourse=False):
    import datetime as _dt
    cfg = dc.load_config()
    now = _dt.datetime.now(dc.CST)
    all_ddl = _fetch_ddls_parallel(cfg, skip_canvas, skip_aihaoke, skip_icourse)
    all_ddl.sort(key=lambda x: x["due"])
    warnings = []
    if not skip_canvas and not (cfg.get("canvas_token") and not cfg.get("canvas_token", "").startswith("YOUR_")):
        warnings.append("Canvas 未配置 token；请先调用 setup_canvas 获取引导，生成后再用 save_credentials 保存。")
    return {
        "current_time": now.strftime("%Y-%m-%d %H:%M"),
        "ddls": [_serialize_ddl(x, now) for x in all_ddl if not x.get("submitted")],
        "warnings": warnings,
    }


def tool_get_next_lab():
    return _serialize_lab(dc.fetch_phycai(dc.load_config()))


def tool_get_all(skip_canvas=False, skip_aihaoke=False, skip_icourse=False, skip_phycai=False):
    import concurrent.futures as _cf
    cfg = dc.load_config()

    # DDL 和物理实验同时拉取
    with _cf.ThreadPoolExecutor(max_workers=2) as pool:
        ddl_fut = pool.submit(_fetch_ddls_parallel, cfg, skip_canvas, skip_aihaoke, skip_icourse)
        lab_fut = pool.submit(dc.fetch_phycai, cfg) if not skip_phycai else None
        all_ddl = ddl_fut.result()
        lab = lab_fut.result() if lab_fut else None

    import datetime as _dt
    now = _dt.datetime.now(dc.CST)
    all_ddl.sort(key=lambda x: x["due"])
    warnings = []
    if not skip_canvas and not (cfg.get("canvas_token") and not cfg.get("canvas_token", "").startswith("YOUR_")):
        warnings.append("Canvas 未配置 token；请先调用 setup_canvas 获取引导，生成后再用 save_credentials 保存。")
    return {
        "current_time": now.strftime("%Y-%m-%d %H:%M"),
        "ddls": [_serialize_ddl(x, now) for x in all_ddl if not x.get("submitted")],
        "lab":  _serialize_lab(lab),
        "warnings": warnings,
    }



def tool_search_campus(
    query: str,
    sites: list | None = None,
    max_results: int = 6,
) -> dict:
    cfg = dc.load_config()
    return dc.search_campus(cfg, query, sites=sites, max_results=max_results)


def tool_get_schedule(
    query_type: str = "day",
    date: str = "",
    week_offset: int = 0,
    set_semester_start: str = "",
    refresh: bool = False,
) -> dict:
    cfg = dc.load_config()
    if set_semester_start:
        result = dc.set_semester_start(cfg, set_semester_start)
        if "error" in result:
            return result
        cfg = dc.load_config()
    if query_type == "week":
        return dc.get_schedule_for_week(cfg, week_offset=week_offset, refresh=refresh)
    else:
        return dc.get_schedule_for_date(cfg, date_str=date, refresh=refresh)


def tool_browse_mysjtu(task: str, start_url: str = "https://my.sjtu.edu.cn", action: str = "") -> dict:
    """
    用 Playwright 打开 my.sjtu.edu.cn，执行可选操作，返回页面文字内容。
    先查本地服务目录缓存，命中则直接跳转目标 URL，无需多级导航。
    """
    try:
        from playwright.sync_api import sync_playwright as _spw
    except ImportError:
        return {"error": "未安装 playwright"}

    cfg = dc.load_config()
    jaccount_cookies = cfg.get("jaccount_cookies", {})

    # ── 缓存命中：根据任务描述直接跳转对应服务 URL ────────────────────────
    catalog = _load_mysjtu_catalog()
    _auto_search_keyword = None
    if catalog and not action and start_url == "https://my.sjtu.edu.cn":
        matched = _find_mysjtu_service(task, catalog)
        if matched:
            start_url = matched["url"]
            # 在返回值里告知命中了哪个服务
            _matched_service = f"{matched['name']}（{matched['category']}）"
        else:
            _matched_service = None
            _auto_search_keyword = _mysjtu_search_keyword(task)
    else:
        _matched_service = None

    with _spw() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        # 注入 jAccount cookie，直接跳过登录
        if jaccount_cookies:
            ctx.add_cookies([
                {"name": k, "value": v, "domain": ".sjtu.edu.cn", "path": "/"}
                for k, v in jaccount_cookies.items()
            ] + [
                {"name": k, "value": v, "domain": "jaccount.sjtu.edu.cn", "path": "/"}
                for k, v in jaccount_cookies.items()
            ])

        page = ctx.new_page()

        try:
            page.goto(start_url, wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_timeout(1500)
        except Exception as e:
            browser.close()
            return {"error": f"页面加载失败：{e}"}

        effective_action = action
        if _auto_search_keyword and not effective_action and start_url == "https://my.sjtu.edu.cn":
            effective_action = f"search:{_auto_search_keyword}"

        # 执行操作指令
        if effective_action:
            try:
                if effective_action.startswith("click:"):
                    text = effective_action[6:].strip()
                    # 优先精确匹配链接/按钮，再模糊匹配
                    for sel in [f"a:has-text('{text}')", f"button:has-text('{text}')",
                                f"[class*='menu']:has-text('{text}')", f"*:has-text('{text}')"]:
                        loc = page.locator(sel).first
                        if loc.count() and loc.is_visible(timeout=1000):
                            loc.click()
                            page.wait_for_load_state("domcontentloaded", timeout=10_000)
                            page.wait_for_timeout(1000)
                            break
                elif effective_action.startswith("goto:"):
                    url = effective_action[5:].strip()
                    page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                    page.wait_for_timeout(1000)
                elif effective_action.startswith("search:"):
                    kw = effective_action[7:].strip()
                    for sel in ["input[type='search']", "input[placeholder*='搜索']",
                                "input[placeholder*='search']", ".search-input input", "input.el-input__inner"]:
                        loc = page.locator(sel).first
                        if loc.count() and loc.is_visible(timeout=500):
                            loc.fill(kw)
                            loc.press("Enter")
                            page.wait_for_load_state("domcontentloaded", timeout=10_000)
                            page.wait_for_timeout(1000)
                            break
            except Exception as e:
                pass  # 操作失败，继续返回当前页内容

        current_url = page.url

        # 提取页面文字内容
        text = page.evaluate("""
        () => {
            // 移除 script/style
            document.querySelectorAll('script,style,noscript').forEach(e => e.remove());
            // 提取主要内容区
            const main = document.querySelector('main, #main, .main, [class*="content"], [class*="container"]');
            const src = main || document.body;
            return (src.innerText || src.textContent || '').replace(/\\n{3,}/g, '\\n\\n').trim();
        }
        """)

        # 提取页面中的链接（帮助 agent 决定下一步点哪里）
        links = page.evaluate("""
        () => {
            return Array.from(document.querySelectorAll('a[href]'))
                .filter(a => a.innerText.trim() && !a.href.startsWith('javascript'))
                .slice(0, 30)
                .map(a => ({text: a.innerText.trim().slice(0, 40), href: a.href}));
        }
        """)

        browser.close()

    libseat_context = _extract_libseat_context(current_url, text)
    if libseat_context and libseat_context.get("warning"):
        text = f"[系统提示] {libseat_context['warning']}\n\n{text}"

    # 检测是否被重定向到登录页
    is_login_page = "jaccount.sjtu.edu.cn" in current_url or (
        "login" in current_url.lower() and "sjtu.edu.cn" in current_url
    )

    return {
        "url": current_url,
        "logged_in": not is_login_page,
        "matched_service": _matched_service,
        "auto_search_keyword": _auto_search_keyword,
        "libseat_context": libseat_context,
        "content": text[:6000],
        "truncated": len(text) > 6000,
        "links": links,
        "task": task,
    }


def tool_query_grades(year: str = "", semester: str = "") -> dict:
    """
    直接从教学信息服务网 (i.sjtu.edu.cn) 查询成绩，自动完成 jAccount SSO。
    year: 学年起始年，如 "2025" 表示 2025-2026 学年，空=全部
    semester: "1"=第1学期(秋), "2"=第2学期(春), "3"=第3学期(夏), ""=全部
    """
    try:
        from playwright.sync_api import sync_playwright as _spw
    except ImportError:
        return {"error": "未安装 playwright，请运行 pip install playwright"}

    cfg = dc.load_config()
    jaccount_cookies = cfg.get("jaccount_cookies", {})
    if not jaccount_cookies:
        return {"error": "未配置 jAccount Cookie，请先配置 jAccount 登录"}

    _XQM_MAP = {"1": "3", "2": "12", "3": "16", "": ""}
    xqm = _XQM_MAP.get(str(semester), "")

    try:
        import time as _time
        with _spw() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            )
            ctx.add_cookies([
                {"name": k, "value": v, "domain": ".sjtu.edu.cn", "path": "/"}
                for k, v in jaccount_cookies.items()
            ])
            page = ctx.new_page()

            # 1. SSO 登录（自动跳转）
            page.goto("https://i.sjtu.edu.cn/jaccountlogin", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)
            if "jaccount" in page.url:
                browser.close()
                return {"error": "jAccount Cookie 已过期，请重新配置 jAccount 登录"}

            # 2. 访问成绩查询页面，获取隐藏字段（含用户身份信息）
            page.goto(
                "https://i.sjtu.edu.cn/cjcx/cjcx_cxDgXscj.html?gnmkdm=N305005",
                wait_until="networkidle", timeout=15000
            )
            page.wait_for_timeout(500)

            form_data = page.evaluate("""() => {
                const inputs = document.querySelectorAll('input[type=hidden]');
                const data = {};
                for (const i of inputs) { data[i.name] = i.value; }
                return data;
            }""")

            # 3. 直接调用 jqGrid 数据接口
            resp = ctx.request.post(
                "https://i.sjtu.edu.cn/cjcx/cjcx_cxXsgrcj.html?doType=query&gnmkdm=N305005",
                form={
                    **form_data,
                    "xnm": year,
                    "xqm": xqm,
                    "kcbjdm": "",
                    "page": "1",
                    "rows": "500",
                    "sidx": "xnm",
                    "sord": "desc",
                    "_search": "false",
                    "nd": str(int(_time.time() * 1000)),
                    "zd_fzdm": "N305005-xs",
                },
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": "https://i.sjtu.edu.cn/cjcx/cjcx_cxDgXscj.html?gnmkdm=N305005",
                }
            )
            data = resp.json()
            items = data.get("items", [])
            browser.close()
    except Exception as e:
        return {"error": str(e)}

    if not items:
        return {
            "count": 0,
            "year_filter": year or "全部",
            "semester_filter": semester or "全部",
            "grades": [],
            "message": "未找到成绩数据，该学期可能还未录入",
        }

    grades = []
    total_credits = 0.0
    weighted_sum = 0.0

    for item in items:
        xf_str = item.get("xf", "")
        jd_str = item.get("jd", "")
        try:
            xf = float(xf_str) if xf_str else 0.0
            jd = float(jd_str) if jd_str else None
        except ValueError:
            xf = 0.0
            jd = None

        grades.append({
            "year":        f"{item.get('xnm', '')}学年",
            "semester":    f"第{item.get('xqmmc', '')}学期",
            "course_id":   item.get("kch", ""),
            "course_name": item.get("kcmc", ""),
            "score":       item.get("cj", ""),
            "gpa":         jd_str,
            "credits":     xf_str,
            "type":        item.get("kcbj", "").strip(),
            "exam_type":   item.get("khfsmc", ""),
        })

        if jd is not None and xf > 0:
            total_credits += xf
            weighted_sum += jd * xf

    avg_gpa = weighted_sum / total_credits if total_credits > 0 else None

    return {
        "count": len(grades),
        "year_filter": year or "全部",
        "semester_filter": semester or "全部",
        "weighted_gpa": round(avg_gpa, 4) if avg_gpa is not None else None,
        "total_credits": total_credits,
        "grades": grades,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 提醒事项
# ══════════════════════════════════════════════════════════════════════════════

def _load_reminders() -> list[dict]:
    if not REMINDERS_PATH.exists():
        return []
    try:
        return json.loads(REMINDERS_PATH.read_text(encoding="utf-8")).get("reminders", [])
    except Exception:
        return []


def _save_reminders(reminders: list[dict]) -> None:
    REMINDERS_PATH.write_text(
        json.dumps({"reminders": reminders}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def tool_add_reminder(
    title: str,
    start: str,
    end: str = "",
    note: str = "",
) -> dict:
    """
    添加一条提醒事项。
    start/end: ISO 8601 或 'YYYY-MM-DD HH:MM'（默认上海时区）。
    """
    import datetime as _dt
    def _parse(s: str) -> _dt.datetime | None:
        if not s:
            return None
        s = s.strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = _dt.datetime.strptime(s, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=dc.CST)
                return dt
            except ValueError:
                continue
        return None

    start_dt = _parse(start)
    if start_dt is None:
        return {"error": f"无法解析时间：{start!r}，请使用 'YYYY-MM-DD HH:MM' 格式"}

    reminders = _load_reminders()
    new_id = max((r["id"] for r in reminders), default=0) + 1
    entry = {
        "id":    new_id,
        "title": title.strip(),
        "start": start_dt.isoformat(),
        "end":   _parse(end).isoformat() if end else "",
        "note":  note.strip(),
    }
    reminders.append(entry)
    _save_reminders(reminders)
    return {"ok": True, "id": new_id, "reminder": entry}


def tool_list_reminders() -> dict:
    """列出所有提醒事项，标注是否已过期。"""
    import datetime as _dt
    now = _dt.datetime.now(dc.CST)
    reminders = _load_reminders()
    items = []
    for r in reminders:
        end_str = r.get("end", "")
        expired = False
        if end_str:
            try:
                end_dt = _dt.datetime.fromisoformat(end_str)
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=dc.CST)
                expired = end_dt < now
            except Exception:
                pass
        items.append({**r, "expired": expired})
    active   = [i for i in items if not i["expired"]]
    inactive = [i for i in items if i["expired"]]
    return {
        "current_time": now.strftime("%Y-%m-%d %H:%M"),
        "active_count": len(active),
        "active":   active,
        "expired":  inactive,
    }


def tool_remove_reminder(reminder_id: int) -> dict:
    """删除指定 id 的提醒事项。"""
    reminders = _load_reminders()
    new_list = [r for r in reminders if r["id"] != reminder_id]
    if len(new_list) == len(reminders):
        return {"error": f"未找到 id={reminder_id} 的提醒事项"}
    _save_reminders(new_list)
    return {"ok": True, "removed_id": reminder_id}


# ══════════════════════════════════════════════════════════════════════════════
# 交大邮箱（IMAP / SMTP）
# ══════════════════════════════════════════════════════════════════════════════

_SJTU_IMAP_HOST = "mail.sjtu.edu.cn"
_SJTU_IMAP_PORT = 993
_SJTU_SMTP_HOST = "mail.sjtu.edu.cn"
_SJTU_SMTP_PORT = 465


def _get_email_creds() -> tuple[str, str]:
    """从环境变量读取邮箱账号和密码。
    账号格式：学号@sjtu.edu.cn，密码同 jAccount 密码（或独立邮箱密码）。
    优先用 EMAIL_USERNAME / EMAIL_PASSWORD，回退到 JACCOUNT_USERNAME / JACCOUNT_PASSWORD。
    """
    username = os.environ.get("EMAIL_USERNAME", "").strip()
    password = os.environ.get("EMAIL_PASSWORD", "").strip()
    if not username:
        username = os.environ.get("JACCOUNT_USERNAME", "").strip()
        if username and "@" not in username:
            username = username + "@sjtu.edu.cn"
    if not password:
        password = os.environ.get("JACCOUNT_PASSWORD", "").strip()
    return username, password


def _imap_connect():
    """建立 IMAP4_SSL 连接并登录，返回 imaplib.IMAP4_SSL 对象。"""
    import imaplib, ssl
    username, password = _get_email_creds()
    if not username or not password:
        raise ValueError("未配置邮箱账号或密码（EMAIL_USERNAME / EMAIL_PASSWORD 或 JACCOUNT_USERNAME / JACCOUNT_PASSWORD）")
    ctx = ssl.create_default_context()
    m = imaplib.IMAP4_SSL(_SJTU_IMAP_HOST, _SJTU_IMAP_PORT, ssl_context=ctx)
    m.login(username, password)
    return m


def _parse_email_headers(raw_bytes: bytes) -> dict:
    """从 RFC 822 头字节解析 Subject / From / To / Date。"""
    import email as _email
    import email.header as _hdr
    msg = _email.message_from_bytes(raw_bytes)

    def _decode(value: str | None) -> str:
        if not value:
            return ""
        parts = _hdr.decode_header(value)
        result = []
        for text, charset in parts:
            if isinstance(text, bytes):
                try:
                    result.append(text.decode(charset or "utf-8", errors="replace"))
                except LookupError:
                    result.append(text.decode("utf-8", errors="replace"))
            else:
                result.append(text)
        return "".join(result)

    return {
        "subject": _decode(msg.get("Subject")),
        "from":    _decode(msg.get("From")),
        "to":      _decode(msg.get("To")),
        "date":    _decode(msg.get("Date")),
        "message_id": (msg.get("Message-ID") or "").strip(),
    }


def _parse_email_body(raw_bytes: bytes, max_chars: int = 3000) -> str:
    """提取邮件纯文本正文（text/plain 优先，其次 text/html 去标签）。"""
    import email as _email
    import re as _re
    msg = _email.message_from_bytes(raw_bytes)

    def _walk(part) -> str:
        ct = part.get_content_type()
        if ct == "text/plain":
            try:
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
            except Exception:
                return ""
        if ct == "text/html":
            try:
                charset = part.get_content_charset() or "utf-8"
                html = part.get_payload(decode=True).decode(charset, errors="replace")
                text = _re.sub(r"<[^>]+>", "", html)
                text = _re.sub(r"&nbsp;", " ", text)
                text = _re.sub(r"&lt;", "<", text)
                text = _re.sub(r"&gt;", ">", text)
                text = _re.sub(r"&amp;", "&", text)
                text = _re.sub(r"\s{3,}", "\n\n", text)
                return text.strip()
            except Exception:
                return ""
        return ""

    # 优先找 text/plain
    plain = ""
    html_fallback = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            ct = part.get_content_type()
            if ct == "text/plain" and not plain:
                plain = _walk(part)
            elif ct == "text/html" and not html_fallback:
                html_fallback = _walk(part)
    else:
        text = _walk(msg)
        if msg.get_content_type() == "text/html":
            html_fallback = text
        else:
            plain = text

    body = plain or html_fallback
    if len(body) > max_chars:
        body = body[:max_chars] + "\n…（正文已截断）"
    return body.strip()


def tool_read_emails(
    folder: str = "INBOX",
    limit: int = 10,
    unread_only: bool = False,
    with_body: bool = False,
    uid: str = "",
) -> dict:
    """
    通过 IMAP 读取交大邮箱邮件。
    folder: 文件夹名（INBOX / Sent / Drafts / Trash，中文如 已发送 也可）
    limit: 最多返回几封，默认 10
    unread_only: 只看未读邮件
    with_body: 同时返回邮件正文
    uid: 指定读取某一封邮件（uid 从列表里取）
    """
    import imaplib

    try:
        m = _imap_connect()
    except ValueError as e:
        return {"error": str(e), "hint": "请先配置 EMAIL_USERNAME / EMAIL_PASSWORD 或 JACCOUNT_USERNAME / JACCOUNT_PASSWORD"}
    except Exception as e:
        return {"error": f"IMAP 登录失败：{e}"}

    try:
        # 处理中文文件夹别名
        _folder_map = {
            "收件箱": "INBOX",
            "已发送": "Sent",
            "发件箱": "Sent",
            "垃圾邮件": "Junk",
            "已删除": "Trash",
            "草稿": "Drafts",
            "草稿箱": "Drafts",
        }
        select_folder = _folder_map.get(folder, folder)

        # 只读取单封（by uid）
        if uid:
            typ, data = m.select(select_folder, readonly=True)
            if typ != "OK":
                # 尝试加引号（含空格或特殊字符的文件夹名需要）
                typ, data = m.select(f'"{select_folder}"', readonly=True)
            typ2, raw = m.uid("FETCH", uid, "(RFC822)")
            m.close()
            m.logout()
            if typ2 != "OK" or not raw or not raw[0]:
                return {"error": f"未找到 UID={uid} 的邮件"}
            raw_bytes = raw[0][1]
            headers = _parse_email_headers(raw_bytes)
            body    = _parse_email_body(raw_bytes)
            return {"uid": uid, **headers, "body": body}

        # 批量读取
        typ, data = m.select(select_folder, readonly=True)
        if typ != "OK":
            typ, data = m.select(f'"{select_folder}"', readonly=True)
        if typ != "OK":
            m.logout()
            return {"error": f"无法打开文件夹 '{select_folder}'，请检查文件夹名称"}

        search_criteria = "UNSEEN" if unread_only else "ALL"
        typ, uids_data = m.uid("SEARCH", search_criteria)
        if typ != "OK":
            m.close(); m.logout()
            return {"error": "搜索失败"}

        uid_list = uids_data[0].decode().split() if uids_data[0] else []
        uid_list = uid_list[-limit:]   # 取最新的 limit 封

        emails = []
        for _uid in reversed(uid_list):
            fetch_spec = "(RFC822)" if with_body else "(RFC822.HEADER)"
            typ2, raw = m.uid("FETCH", _uid, fetch_spec)
            if typ2 != "OK" or not raw or not raw[0]:
                continue
            raw_bytes = raw[0][1]
            entry = {"uid": _uid, **_parse_email_headers(raw_bytes)}
            if with_body:
                entry["body"] = _parse_email_body(raw_bytes)
            emails.append(entry)

        m.close()
        m.logout()
        return {
            "folder":       select_folder,
            "total_found":  len(uid_list),
            "returned":     len(emails),
            "emails":       emails,
        }
    except Exception as e:
        try: m.logout()
        except Exception: pass
        return {"error": str(e)}


def tool_search_emails(
    keyword: str,
    folder: str = "INBOX",
    search_in: str = "SUBJECT",
    limit: int = 10,
    with_body: bool = False,
) -> dict:
    """
    搜索交大邮箱中的邮件。
    keyword: 搜索关键词
    folder: 文件夹（默认 INBOX）
    search_in: SUBJECT（主题）/ FROM（发件人）/ TEXT（全文）/ TO（收件人）
    limit: 最多返回结果数
    with_body: 是否同时返回正文
    """
    import imaplib

    try:
        m = _imap_connect()
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"IMAP 登录失败：{e}"}

    try:
        _folder_map = {
            "收件箱": "INBOX", "已发送": "Sent", "垃圾邮件": "Junk",
            "已删除": "Trash", "草稿": "Drafts",
        }
        select_folder = _folder_map.get(folder, folder)
        typ, _ = m.select(select_folder, readonly=True)
        if typ != "OK":
            typ, _ = m.select(f'"{select_folder}"', readonly=True)
        if typ != "OK":
            m.logout()
            return {"error": f"无法打开文件夹 '{select_folder}'"}

        search_in_upper = search_in.upper()
        if search_in_upper not in ("SUBJECT", "FROM", "TEXT", "TO", "BODY"):
            search_in_upper = "SUBJECT"

        # IMAP SEARCH 字符串（UTF-8 CHARSET）
        try:
            typ, uids_data = m.uid(
                "SEARCH", "CHARSET", "UTF-8",
                search_in_upper, keyword.encode("utf-8"),
            )
        except imaplib.IMAP4.error:
            # 部分服务器不支持 UTF-8 charset，降级到 ASCII
            safe_kw = keyword.encode("ascii", errors="ignore").decode()
            typ, uids_data = m.uid("SEARCH", search_in_upper, f'"{safe_kw}"')

        uid_list = uids_data[0].decode().split() if (uids_data and uids_data[0]) else []
        uid_list = uid_list[-limit:]

        emails = []
        for _uid in reversed(uid_list):
            fetch_spec = "(RFC822)" if with_body else "(RFC822.HEADER)"
            typ2, raw = m.uid("FETCH", _uid, fetch_spec)
            if typ2 != "OK" or not raw or not raw[0]:
                continue
            raw_bytes = raw[0][1]
            entry = {"uid": _uid, **_parse_email_headers(raw_bytes)}
            if with_body:
                entry["body"] = _parse_email_body(raw_bytes)
            emails.append(entry)

        m.close()
        m.logout()
        return {
            "keyword":      keyword,
            "search_in":    search_in_upper,
            "folder":       select_folder,
            "total_found":  len(uid_list),
            "returned":     len(emails),
            "emails":       emails,
        }
    except Exception as e:
        try: m.logout()
        except Exception: pass
        return {"error": str(e)}


def tool_send_email(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    reply_to_uid: str = "",
    folder: str = "INBOX",
) -> dict:
    """
    用交大邮箱发送邮件（SMTP SSL port 465）。
    to: 收件人（单个或逗号分隔多个）
    subject: 邮件主题
    body: 邮件正文（纯文本）
    cc: 抄送（可选，逗号分隔）
    reply_to_uid: 如果是回复某封邮件，传入原邮件的 IMAP uid（自动补 In-Reply-To）
    folder: reply_to_uid 所在文件夹（默认 INBOX）
    """
    import smtplib, ssl
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.utils import formatdate, make_msgid

    username, password = _get_email_creds()
    if not username or not password:
        return {"error": "未配置邮箱账号，请设置 EMAIL_USERNAME / EMAIL_PASSWORD 或 JACCOUNT_USERNAME / JACCOUNT_PASSWORD"}

    # 若是回复，先取原邮件的 Message-ID
    in_reply_to = ""
    references  = ""
    if reply_to_uid:
        try:
            m = _imap_connect()
            _folder_map = {"收件箱": "INBOX", "已发送": "Sent"}
            sf = _folder_map.get(folder, folder)
            m.select(sf, readonly=True)
            typ, raw = m.uid("FETCH", reply_to_uid, "(RFC822.HEADER)")
            m.close(); m.logout()
            if typ == "OK" and raw and raw[0]:
                headers = _parse_email_headers(raw[0][1])
                in_reply_to = headers.get("message_id", "")
                references  = in_reply_to
        except Exception:
            pass

    msg = MIMEMultipart()
    msg["From"]    = username
    msg["To"]      = to
    msg["Date"]    = formatdate(localtime=True)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    if cc:
        msg["Cc"] = cc
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"]  = references

    msg.attach(MIMEText(body, "plain", "utf-8"))

    recipients = [r.strip() for r in to.split(",") if r.strip()]
    if cc:
        recipients += [r.strip() for r in cc.split(",") if r.strip()]

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(_SJTU_SMTP_HOST, _SJTU_SMTP_PORT, context=ctx, timeout=30) as smtp:
            smtp.login(username, password)
            smtp.sendmail(username, recipients, msg.as_bytes())
        return {
            "ok": True,
            "from": username,
            "to": to,
            "cc": cc,
            "subject": subject,
            "message_id": msg["Message-ID"],
        }
    except smtplib.SMTPAuthenticationError:
        return {"error": "SMTP 登录失败：用户名或密码错误。请尝试在网页邮箱开启「客户端授权码」并将授权码设为 EMAIL_PASSWORD。"}
    except Exception as e:
        return {"error": f"发送失败：{e}"}


def tool_get_user_profile() -> dict:
    """读取本地用户画像文件，返回画像数据。"""
    import datetime as _dt
    if not USER_PROFILE_PATH.exists():
        return {"exists": False, "profile": {}}
    try:
        profile = json.loads(USER_PROFILE_PATH.read_text(encoding="utf-8"))
        return {"exists": True, "profile": profile}
    except Exception as e:
        return {"exists": False, "error": str(e), "profile": {}}


def tool_update_user_profile(updates: dict, reason: str = "") -> dict:
    """将 updates 合并到本地用户画像文件（深度合并，不覆盖未提及字段）。"""
    import datetime as _dt

    profile: dict = {}
    if USER_PROFILE_PATH.exists():
        try:
            profile = json.loads(USER_PROFILE_PATH.read_text(encoding="utf-8"))
        except Exception:
            profile = {}

    def deep_merge(base: dict, patch: dict) -> dict:
        for k, v in patch.items():
            if k in base and isinstance(base[k], list) and isinstance(v, list):
                # list 字段：合并去重
                existing = base[k]
                for item in v:
                    if item not in existing:
                        existing.append(item)
            elif k in base and isinstance(base[k], dict) and isinstance(v, dict):
                deep_merge(base[k], v)
            else:
                base[k] = v
        return base

    profile = deep_merge(profile, updates)
    profile["last_updated"] = _dt.datetime.now().isoformat()

    USER_PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    USER_PROFILE_PATH.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"ok": True, "updated_keys": list(updates.keys()), "reason": reason}


def tool_setup_wechat() -> dict:
    """
    启动微信 ilink Bot 扫码登录流程，在终端打印二维码，等待用户扫码。
    扫码成功后 bot_token 自动保存到 config.json。
    """
    try:
        import subprocess as _sp
        import sys as _sys
        result = _sp.run(
            [_sys.executable, str(ROOT / "wechat_bot.py"), "--login"],
            cwd=str(ROOT),
            timeout=300,  # 5 分钟内完成扫码
        )
        if result.returncode == 0:
            cfg = {}
            if CONFIG_PATH.exists():
                cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            has_token = bool(cfg.get("wechat_bot_token"))
            _msg = (
                "微信 Bot 登录成功！token 已保存到 config.json。\n"
                "现在请在微信里找到你刚才登录的 AI Bot（在微信搜索 AI小助手 或你绑定的 bot 名称），\n"
                "给它发一条任意消息（如 你好），系统就会记录 context_token，之后可以主动推送消息。\n\n"
                "启动微信 Bot 后台服务：\n"
                "  python3 wechat_bot.py        # 前台运行\n"
                "  sjtu-agent wechat-bot         # 通过 CLI 启动（如已安装）"
            ) if has_token else "扫码完成但未能读取 token，请检查 config.json"
            _steps = [
                "在微信里找到你的 AI Bot（搜索 AI小助手）",
                "给 Bot 发一条消息（如 你好），系统自动记录 context_token",
                "运行 python3 wechat_bot.py 启动 Bot（或用 sjtu-agent wechat-bot）",
            ] if has_token else []
            return {
                "success": has_token,
                "saved": has_token,
                "message": _msg,
                "next_steps": _steps,
            }
        else:
            return {
                "success": False,
                "error": f"登录进程退出码 {result.returncode}",
                "hint": "请在终端直接运行 python3 wechat_bot.py --login 查看详细错误",
            }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "hint": "请在终端直接运行 python3 wechat_bot.py --login 完成扫码登录",
        }


def tool_setup_telegram(telegram_token: str, allowed_ids: list | None = None) -> dict:
    """
    将 Telegram Bot Token 和可选的白名单 user_id 保存到 config.json。
    保存后用户可执行 sjtu-agent telegram-bot 启动 Bot。
    """
    cfg: dict = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    cfg["telegram_token"] = telegram_token.strip()
    if allowed_ids is not None:
        cfg["telegram_allowed_ids"] = [int(i) for i in allowed_ids]

    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    # 验证 token 是否有效（可选，无网络或防火墙限制时跳过）
    token_valid: bool | None = None
    bot_info: dict = {}
    try:
        import requests as _req
        resp = _req.get(
            f"https://api.telegram.org/bot{telegram_token.strip()}/getMe",
            timeout=10,
        )
        if resp.status_code == 200:
            token_valid = True
            bot_info = resp.json().get("result", {})
        else:
            token_valid = False
    except Exception:
        token_valid = None  # 网络不通，跳过验证

    result: dict = {
        "saved": True,
        "token_valid": token_valid,
        "bot_username": bot_info.get("username", ""),
        "bot_name": bot_info.get("first_name", ""),
        "allowed_ids_set": allowed_ids or [],
        "next_steps": [
            "运行 `sjtu-agent telegram-bot` 启动 Bot（长轮询模式）。",
            "在 Telegram 中发送 /id 给 Bot，可以获得自己的 user_id，然后把它添加到白名单。",
            "如果还没有 Bot Token，先在 Telegram 里找 @BotFather，发 /newbot 创建。",
        ],
    }
    if not allowed_ids:
        result["tip"] = (
            "当前白名单为空，Bot 启动后会对所有发消息的用户返回其 chat_id，"
            "方便你确认自己的 user_id 后再来用 setup_telegram 补填白名单。"
        )
    return result


def tool_execute_python(code: str, timeout: int = 60) -> dict:
    """
    在当前进程中安全地执行动态 Python 代码片段。
    stdout/stderr 捕获后作为结果返回，不会污染终端。
    """
    import subprocess as _sp
    import sys as _sys

    # 注入基础 import 路径
    preamble = (
        "import sys, os\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from pathlib import Path\n"
        "from dotenv import load_dotenv\n"
        f"load_dotenv({str(ENV_PATH)!r})\n"
        "import ddl_checker as dc\n"
    )
    full_code = preamble + "\n" + code

    try:
        result = _sp.run(
            [_sys.executable, "-c", full_code],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(ROOT),
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if result.returncode != 0:
            return {
                "ok": False,
                "returncode": result.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "error": stderr or f"进程退出码 {result.returncode}",
            }
        return {
            "ok": True,
            "returncode": 0,
            "stdout": stdout,
            "stderr": stderr,
        }
    except _sp.TimeoutExpired:
        return {"ok": False, "error": f"代码执行超时（{timeout}秒）"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_list_assignment_files(
    course_filter: str = "",
    assignments_dir: str = "./assignments",
) -> dict:
    base = Path(assignments_dir)
    if not base.exists():
        return {"error": f"目录不存在: {base.resolve()}，请先执行 download_assignments"}
    tree = []
    for course_dir in sorted(base.iterdir()):
        if not course_dir.is_dir():
            continue
        if course_filter and course_filter not in course_dir.name:
            continue
        assignments = []
        for asgn_dir in sorted(course_dir.iterdir()):
            if not asgn_dir.is_dir():
                continue
            files = [
                {"name": f.name, "path": str(f.resolve()), "size_kb": round(f.stat().st_size / 1024, 1)}
                for f in sorted(asgn_dir.iterdir())
                if f.is_file() and f.suffix.lower() in {".pdf", ".html", ".png", ".jpg", ".docx", ".zip"}
            ]
            if files:
                assignments.append({"assignment": asgn_dir.name, "files": files})
        if assignments:
            tree.append({"course": course_dir.name, "assignments": assignments})
    return {"tree": tree, "base_dir": str(base.resolve())}


def tool_read_assignment_file(
    file_path: str,
    max_chars: int = 8000,
    start_page: int = 1,
) -> dict:
    path = Path(file_path)
    if not path.exists():
        # 尝试相对于脚本目录解析（防止 LLM 传入相对路径）
        path = ROOT / file_path
    if not path.exists():
        return {"error": f"文件不存在: {file_path}，请用 list_assignment_files 确认正确路径"}
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            import pypdf
            reader = pypdf.PdfReader(str(path))
            total_pages = len(reader.pages)
            parts = []
            chars = 0
            for i, page in enumerate(reader.pages[start_page - 1:], start=start_page):
                text = page.extract_text() or ""
                if chars + len(text) > max_chars:
                    text = text[: max_chars - chars]
                    parts.append(text)
                    chars = max_chars
                    break
                parts.append(text)
                chars += len(text)
            content = "\n\n--- 第 {} 页 ---\n".join([""] * len(parts)).strip()
            # 保留页码标记
            labeled = []
            for idx, (pg_num, txt) in enumerate(
                zip(range(start_page, start_page + len(parts)), parts)
            ):
                labeled.append(f"【第 {pg_num} 页】\n{txt.strip()}")
            content = "\n\n".join(labeled)
            return {
                "file": path.name,
                "total_pages": total_pages,
                "pages_read": f"{start_page}-{start_page + len(parts) - 1}",
                "truncated": chars >= max_chars,
                "content": content,
            }
        elif suffix in {".html", ".htm"}:
            from html.parser import HTMLParser
            class _Strip(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.parts = []
                def handle_data(self, data):
                    self.parts.append(data)
            p = _Strip()
            p.feed(path.read_text(encoding="utf-8", errors="replace"))
            text = " ".join(p.parts)
            # 压缩空白
            import re
            text = re.sub(r"\s{3,}", "\n\n", text).strip()
            truncated = len(text) > max_chars
            return {
                "file": path.name,
                "truncated": truncated,
                "content": text[:max_chars],
            }
        else:
            return {"error": f"暂不支持 {suffix} 格式，目前支持 PDF 和 HTML"}
    except Exception as e:
        return {"error": str(e)}


def tool_download_assignments(
    skip_canvas: bool = False,
    skip_aihaoke: bool = False,
    course_filter: str = "",
    assignment_filter: str = "",
    due_within_days: int = 7,
    output_dir: str = "./assignments",
) -> dict:
    cfg = dc.load_config()

    # 自动跳过 aihaoke：仅有 locale cookie 说明未登录（不值得尝试，避免无效的 Playwright 登录）
    _aihaoke_cookies = cfg.get("aihaoke_cookies", {})
    _meaningful_aihaoke = {k: v for k, v in _aihaoke_cookies.items() if k != "locale"}
    if not _meaningful_aihaoke and not skip_aihaoke:
        skip_aihaoke = True

    results = dc.download_assignments(
        cfg,
        output_dir=output_dir,
        skip_canvas=skip_canvas,
        skip_aihaoke=skip_aihaoke,
        course_filter=course_filter,
        assignment_filter=assignment_filter,
        due_within_days=due_within_days,
    )
    # 统计摘要
    total_files = sum(len(r.get("files", [])) for r in results)
    return {
        "downloaded": len(results),
        "total_files": total_files,
        "output_dir": str(Path(output_dir).resolve()),
        "filters": {
            "course_filter": course_filter,
            "assignment_filter": assignment_filter,
            "due_within_days": due_within_days,
            "skip_canvas": skip_canvas,
            "skip_aihaoke": skip_aihaoke,
        },
        "items": [
            {
                "platform": r["platform"],
                "course":   r["course"],
                "name":     r["name"],
                "due":      r.get("due"),
                "files":    r.get("files", []),
                "output_dir": r.get("output_dir", ""),
            }
            for r in results
            if "error" not in r
        ],
        "errors": [r["error"] for r in results if "error" in r],
    }


def tool_list_canvas_assignments(course_filter: str = "") -> dict:
    """列出 Canvas 上允许文件提交（online_upload）的作业，返回含 course_id / assignment_id。"""
    import requests as _req
    cfg   = dc.load_config()
    base  = cfg.get("canvas_base_url", _CANVAS_DEFAULT_BASE_URL).rstrip("/")
    token = cfg.get("canvas_token", "").strip()
    if not token:
        return {
            "error": "未配置 Canvas Token。",
            "settings_url": _canvas_settings_url(base),
            "next_action": "请先调用 setup_canvas 获取一步步引导，生成 token 后再用 save_credentials 保存。",
        }
    headers = {"Authorization": f"Bearer {token}"}

    # 获取在读课程
    resp = _req.get(
        f"{base}/api/v1/courses",
        params={"enrollment_type": "student", "enrollment_state": "active", "per_page": 50},
        headers=headers, timeout=30,
    )
    if resp.status_code != 200:
        return {
            "error": f"获取课程列表失败 ({resp.status_code})，请检查 Canvas Token 是否有效。",
            "settings_url": _canvas_settings_url(base),
            "next_action": "如 token 已失效，请重新调用 setup_canvas 按提示生成新 token。",
        }
    courses = [c for c in resp.json() if isinstance(c, dict) and c.get("name")]
    if course_filter:
        courses = [c for c in courses if course_filter in c.get("name", "")]

    result = []
    for course in courses[:15]:
        cid   = course["id"]
        cname = course.get("name", "未知课程")
        resp2 = _req.get(
            f"{base}/api/v1/courses/{cid}/assignments",
            params={"per_page": 50, "order_by": "due_at"},
            headers=headers, timeout=30,
        )
        if resp2.status_code != 200:
            continue
        for a in resp2.json():
            if not isinstance(a, dict):
                continue
            if "online_upload" not in a.get("submission_types", []):
                continue
            result.append({
                "course_id":       cid,
                "course_name":     cname,
                "assignment_id":   a["id"],
                "assignment_name": a.get("name", ""),
                "due_at":          a.get("due_at", ""),
                "points_possible": a.get("points_possible"),
            })

    return {"count": len(result), "assignments": result}


def tool_submit_canvas_assignment(
    file_path: str,
    course_id: int,
    assignment_id: int,
    comment: str = "",
) -> dict:
    """
    将本地文件上传并提交到 Canvas 指定作业（three-step Canvas file upload）。
    file_path: 文件的绝对路径（用户拖入终端后得到的路径）。
    """
    import mimetypes
    import requests as _req
    from pathlib import Path as _P

    fp = _P(file_path.strip().strip("'\""))
    if not fp.exists():
        return {"error": f"文件不存在: {fp}"}
    if not fp.is_file():
        return {"error": f"路径不是文件: {fp}"}

    cfg   = dc.load_config()
    base  = cfg.get("canvas_base_url", _CANVAS_DEFAULT_BASE_URL).rstrip("/")
    token = cfg.get("canvas_token", "").strip()
    if not token:
        return {
            "error": "未配置 Canvas Token。",
            "settings_url": _canvas_settings_url(base),
            "next_action": "请先调用 setup_canvas 获取一步步引导，生成 token 后再用 save_credentials 保存。",
        }
    headers = {"Authorization": f"Bearer {token}"}

    mime      = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
    file_size = fp.stat().st_size

    # ── Step 1: 申请上传许可 ─────────────────────────────────────────────
    r1 = _req.post(
        f"{base}/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/self/files",
        headers=headers,
        json={"name": fp.name, "size": file_size, "content_type": mime},
        timeout=30,
    )
    if r1.status_code not in (200, 201):
        return {"error": f"申请上传许可失败 ({r1.status_code}): {r1.text[:300]}"}
    upload_info   = r1.json()
    upload_url    = upload_info["upload_url"]
    upload_params = upload_info.get("upload_params", {})

    # ── Step 2: 上传文件 ──────────────────────────────────────────────────
    with open(fp, "rb") as fobj:
        r2 = _req.post(
            upload_url,
            data=upload_params,
            files={"file": (fp.name, fobj, mime)},
            timeout=180,
            allow_redirects=True,
        )

    if r2.status_code in (200, 201):
        file_data = r2.json()
    elif r2.status_code in (301, 302, 303):
        confirm_url = r2.headers.get("Location", "")
        r3 = _req.get(confirm_url, headers=headers, timeout=30)
        file_data = r3.json()
    else:
        return {"error": f"文件上传失败 ({r2.status_code}): {r2.text[:300]}"}

    file_id = file_data.get("id")
    if not file_id:
        return {"error": f"上传完成但未获取到文件 ID，响应: {str(file_data)[:200]}"}

    # ── Step 3: 提交作业 ──────────────────────────────────────────────────
    payload: dict = {
        "submission": {
            "submission_type": "online_upload",
            "file_ids": [file_id],
        }
    }
    if comment:
        payload["comment"] = {"text_comment": comment}

    r_sub = _req.post(
        f"{base}/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions",
        headers=headers,
        json=payload,
        timeout=30,
    )
    if r_sub.status_code not in (200, 201):
        return {"error": f"提交失败 ({r_sub.status_code}): {r_sub.text[:300]}"}

    sub = r_sub.json()
    return {
        "ok":             True,
        "file_name":      fp.name,
        "file_id":        file_id,
        "submission_id":  sub.get("id"),
        "submitted_at":   sub.get("submitted_at"),
        "workflow_state": sub.get("workflow_state"),
    }


def run_tool(name: str, args: dict) -> str:
    try:
        if   name == "check_setup":         r = tool_check_setup()
        elif name == "save_credentials":    r = tool_save_credentials(**args)
        elif name == "setup_canvas":        r = tool_setup_canvas(**args)
        elif name == "login_platform":      r = tool_login_platform(args["platform"])
        elif name == "get_ddls":            r = tool_get_ddls(**args)
        elif name == "get_next_lab":        r = tool_get_next_lab()
        elif name == "get_all":             r = tool_get_all(**args)
        elif name == "download_assignments":r = tool_download_assignments(**args)
        elif name == "list_assignment_files": r = tool_list_assignment_files(**args)
        elif name == "read_assignment_file":  r = tool_read_assignment_file(**args)
        elif name == "search_campus":         r = tool_search_campus(**args)
        elif name == "get_schedule":          r = tool_get_schedule(**args)
        elif name == "setup_shuiyuan":        r = tool_setup_shuiyuan()
        elif name == "browse_mysjtu":         r = tool_browse_mysjtu(**args)
        elif name == "refresh_mysjtu_catalog": r = tool_refresh_mysjtu_catalog()
        elif name == "query_grades":            r = tool_query_grades(**args)
        elif name == "add_reminder":            r = tool_add_reminder(**args)
        elif name == "list_reminders":          r = tool_list_reminders()
        elif name == "remove_reminder":         r = tool_remove_reminder(**args)
        elif name == "list_canvas_assignments":  r = tool_list_canvas_assignments(**args)
        elif name == "submit_canvas_assignment": r = tool_submit_canvas_assignment(**args)
        elif name == "read_emails":              r = tool_read_emails(**args)
        elif name == "search_emails":            r = tool_search_emails(**args)
        elif name == "send_email":               r = tool_send_email(**args)
        elif name == "execute_python":           r = tool_execute_python(**args)
        elif name == "update_user_profile":      r = tool_update_user_profile(**args)
        elif name == "get_user_profile":         r = tool_get_user_profile()
        elif name == "setup_telegram":           r = tool_setup_telegram(**args)
        elif name == "setup_wechat":             r = tool_setup_wechat()
        else:                               r = {"error": f"未知工具: {name}"}
    except Exception as e:
        r = {"error": str(e)}
    return json.dumps(r, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════════════
# Agent LLM 配置
# ══════════════════════════════════════════════════════════════════════════════

# 致远一号 API（交大官方 OpenAI 兼容接口）的环境变量名
_ZHIYUAN_BASE_URL_ENV = "ZHIYUAN_BASE_URL"
_ZHIYUAN_API_KEY_ENV  = "ZHIYUAN_API_KEY"
_ZHIYUAN_DEFAULT_BASE = "https://models.sjtu.edu.cn/api/v1"
_ZHIYUAN_DEFAULT_MODEL = "deepseek-chat"


def load_agent_config() -> dict:
    """加载 Agent LLM 配置，优先级：致远一号环境变量 > agent_config.json > 空配置。"""
    # 1. 优先：致远一号环境变量
    zhiyuan_base = os.environ.get(_ZHIYUAN_BASE_URL_ENV, "").strip()
    zhiyuan_key  = os.environ.get(_ZHIYUAN_API_KEY_ENV, "").strip()
    if zhiyuan_key:
        return {
            "base_url": zhiyuan_base or _ZHIYUAN_DEFAULT_BASE,
            "api_key":  zhiyuan_key,
            "model":    _ZHIYUAN_DEFAULT_MODEL,
            "_source":  "zhiyuan_env",
        }
    # 2. fallback：agent_config.json（原有 Claude / 其他 OpenAI 配置）
    if AGENT_CONFIG_PATH.exists():
        return json.loads(AGENT_CONFIG_PATH.read_text())
    return {}


def _test_llm_connection_simple(base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    """测试 LLM API 连接是否正常。返回 (ok, error_msg)。"""
    _url = base_url.strip().rstrip("/")
    if _url and not _url.startswith(("http://", "https://")):
        return False, f"Base URL 格式不正确（缺少 http:// 或 https://）：{_url!r}"
    if not api_key.strip():
        return False, "API Key 为空"
    try:
        client = OpenAI(api_key=api_key.strip(), base_url=_url or None)
        client.chat.completions.create(
            model=model.strip(),
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
            timeout=15,
        )
        return True, ""
    except Exception as e:
        err = str(e)
        if "Connection error" in err or "UnsupportedProtocol" in err or "missing an 'http" in err:
            return False, f"无法连接到 API（{_url or 'openai 官方'}），请检查 Base URL"
        if "401" in err or "Unauthorized" in err or "Invalid API key" in err.lower():
            return False, "API Key 无效或已失效"
        if "timeout" in err.lower() or "timed out" in err.lower():
            return False, "连接超时（15s），请检查网络或 Base URL"
        return False, f"连接失败：{err[:120]}"


def setup_agent_config() -> dict:
    print("\n=== SJTU DDL Agent 首次配置 ===")
    print("请填写用于驱动 Agent 的大模型 API 信息")
    print("（支持 DeepSeek、学校超算集群等任意 OpenAI 兼容接口）")
    print("输入 quit / skip 可跳过配置直接进入 Agent（功能受限）\n")

    def _prompt(msg: str) -> str:
        """带退出检测的 input，Ctrl+C / EOF / quit / skip 均触发跳出。"""
        try:
            val = input(msg).strip()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit(0)
        if val.lower() in ("quit", "exit", "skip", "q"):
            raise SystemExit(0)
        return val

    while True:
        try:
            base_url = _prompt("API Base URL（如 https://api.openai.com/v1，回车使用 OpenAI 官方）: ")
            api_key  = _prompt("API Key: ")
            model    = _prompt("模型名称（如 deepseek-chat，回车默认 deepseek-chat）: ") or "deepseek-chat"
        except SystemExit:
            print("\n已跳过 API 配置。部分依赖 LLM 的功能将不可用。")
            print("你可以后续运行 sjtu-agent setup 补充配置，或使用 /model 命令修改。\n")
            # 返回一个"空"配置，让 chat_loop 仍可启动（工具调用不受影响）
            return {"base_url": "", "api_key": "", "model": "deepseek-chat"}

        resolved_url = base_url or "https://api.openai.com/v1"
        print("正在测试 API 连接，请稍候…", end="", flush=True)
        ok, err_msg = _test_llm_connection_simple(resolved_url, api_key, model)
        if ok:
            print(" ✅ 连接成功")
            break
        print(f"\n⚠️  连接测试失败：{err_msg}")
        print("请重新输入（直接回车可重用上次输入的值；输入 quit 可跳过配置）\n")

    cfg = {
        "base_url": resolved_url,
        "api_key":  api_key,
        "model":    model,
    }
    AGENT_CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    print("\nAgent 配置已保存。\n")
    return cfg


# ══════════════════════════════════════════════════════════════════════════════
# 主聊天循环
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """你是 SJTU 全能助手，帮助上海交通大学学生处理学业、校园生活的各类事务。

## 核心原则：永远先尝试，不主动说不行
用户提出任何请求时，**先调用工具尝试，绝不因为"规则里没有"就拒绝**。
- 遇到不确定怎么处理的事情 → 先用 browse_mysjtu 或 search_campus 探索
- 工具失败或结果不理想 → 告知遇到的具体问题，并提出替代方案
- 只有在所有工具都明确无法完成时，才说明原因并请求用户协助

## 工具选择策略（遇到不确定时按此顺序判断）
1. 属于作业/DDL 范畴 → get_ddls / download_assignments
2. 查成绩/绩点/GPA（i.sjtu.edu.cn 教学信息服务网） → **query_grades**（专用工具，自动 SSO，最快最准）
3. 属于交大门户（校车/选课/事务办理/注册/缴费/预约/申请等）→ browse_mysjtu
4. 属于信息查询/公告/资料 → search_campus
5. 实在不确定 → 先 browse_mysjtu(start_url="https://my.sjtu.edu.cn") 看看首页有没有入口

## 启动行为
对话开始时立刻调用 check_setup，然后：
- 若配置不完整：告知用户缺少哪些配置，主动引导一步步完成设置。
- 若配置完整：告知用户一切就绪，等待指令。

## 配置引导顺序（每次只问一项，等回答后再继续）
1. 交大 jAccount 用户名和密码（用于 AI 好课（aihaoke）和物理实验自动登录）
   **注意：jAccount 用户名不是学号，是你登录 my.sjtu.edu.cn、邮箱时使用的英文用户名（通常是拼音缩写，如 zhangsan）**
2. Canvas Token（需要用户在 Canvas 页面手动生成；若用户不会，调用 setup_canvas 打开设置页并逐步引导）
3. 中国大学MOOC 手机号和密码

收到凭证后：先调用 save_credentials 保存，再调用 login_platform 自动登录验证。
告知用户：凭证仅保存在本地文件，不会上传任何服务器。
用户不想配置某平台时直接跳过。

## 查询行为
- DDL / 作业 / 截止日期 → get_ddls
- 物理实验课安排 / 下次实验课 / 实验预约 → get_next_lab（「物理作业」不属于此类！）
- "所有" / "全部" / "全查" → get_all
- 回复用中文，日期友好展示（如"还有 3 天，5月6日 23:59 截止"）
- 无待完成任务时明确告知

## 下载行为
- 用户说「下载作业」「把题目下载下来」「帮我保存作业材料」「下载物理作业」「下载临近作业」→ download_assignments
- 「物理作业」= Canvas 上的作业题目文件，用 download_assignments（course_filter="物理"）
- 「物理实验课安排」才对应 get_next_lab，不要混淆
- 如果当前上下文里已经明确提到某门课或某个作业，调用 download_assignments 时必须传 course_filter 和/或 assignment_filter，不要空参数全量扫平台
- 用户是在你刚刚提示的某个即将截止作业后接着说「帮我下载作业」，默认理解为下载那个作业本身，而不是全部平台作业
- 用户没有明确说「全部下载」「都下载」时，保持 due_within_days 默认值，只下载近期作业
- 下载完成后告知保存目录和各作业的文件数量
- 可通过 course_filter / assignment_filter 参数只下载指定课程或指定作业

## 搜索行为
- 用户说「水源」「水源社区」「bbs」→ search_campus(query=..., sites=["shuiyuan"])
- 用户说「教务处」「jwc」→ search_campus(query=..., sites=["jwc"])
- 用户说「传承」「dyweb」「传承交大」→ search_campus(query=..., sites=["dyweb"])
- 用户未指定平台 → 不传 sites 参数，搜全部三个
- 搜索无结果时直接告知用户未找到相关内容
- 展示传承结果时显示：课程名、院系、资料名称（类型）、课程链接

## 阅读作业内容
- 用户问「第几题是什么」「帮我看看物理作业」→ 先调 list_assignment_files 找到文件，再调 read_assignment_file 读取，然后回答
- 若 truncated=true，可继续读下一段（用 start_page）

## 课表
- 用户问「今天有什么课」「明天几点上课」→ get_schedule(query_type="day", date="今天/明天/后天")
- 用户问「本周/下周课表」→ get_schedule(query_type="week", week_offset=0/1)
- 单天：显示时间段、课程名、教室、教师；周课表：按天分组
- 若提示"未配置 semester_start"，询问用户第一周周一日期，调用 get_schedule(..., set_semester_start="YYYY-MM-DD") 保存

## 成绩与绩点查询（i.sjtu.edu.cn 教学信息服务网）
**专用工具：query_grades**（比 browse_mysjtu 快且准，优先使用）
- 用户说「查成绩」「上学期成绩」「这学期成绩」「绩点多少」「GPA 是多少」→ 调用 query_grades
- 默认查全部（不传参数），也可指定学年/学期：
  - 「上学期」通常是第1学期（秋季），传 semester="1"；「下学期」→ semester="2"
  - 「本学年」→ year="2025"（当前学年起始年）；「去年」→ year="2024"
- 返回结构化成绩列表（课程名、成绩、绩点、学分）和加权平均绩点
- 展示时：以表格形式显示课程名、成绩、绩点、学分，最后汇总加权绩点和总学分
- Cookie 过期时告知用户需要重新配置 jAccount

## my.sjtu.edu.cn 业务（交我办、门户、校内系统）
browse_mysjtu 的使用场景：成绩、绩点、奖学金、培养方案、注册、缴费、选课、校车/班车预约、物资申请、场地预约、宿舍维修、各类行政事务……凡是交大门户能办的事，都可以用。

**图书馆座位预约特别规则：**
- 如果 browse_mysjtu 返回的是 libseat.sjtu.edu.cn 首页统计页，绝对不要把首页里的“空闲/总数”直接解释成“现在就能预约”。
- 只有进入具体日期/时段的选座页面并看到可选座位，或者页面明确写出当前可预约时段，才能说“可以预约”。
- 如果当前只拿到首页统计，必须明确告诉用户“当前可预约性还没确认”，再询问想去的馆区和时间段，或继续导航确认。

**服务目录缓存（重要）：**
- 若本地已有 mysjtu_catalog.json 缓存，browse_mysjtu 会自动匹配服务并直接跳转，无需多步导航
- 首次使用前建议先调 refresh_mysjtu_catalog 建立缓存（约需 2-3 分钟）
- 缓存不存在时也能正常使用，只是需要多轮导航

**多步导航方法（必须掌握）：**
1. 调 browse_mysjtu(task=任务描述) 获取首页内容和链接列表
2. 从 links 列表中找到最相关的链接，用 action="click:链接文字" 进入
3. 重复直到找到目标，最多 6 步
4. 没有找到入口时，尝试 action="search:关键词" 在页面内搜索
5. 把最终结果简洁地告知用户

**注意：**
- browse_mysjtu 返回 content（页面文字）和 links（链接列表）
- 如果页面返回登录提示（content 含"登录"或 url 含"jaccount"），告知用户 jAccount 会话已过期，需要重新配置
- 不要因为"不确定能不能办"就不调用，先试试

## Canvas 作业提交
- Canvas 相关能力依赖 canvas_token；若缺失或失效，优先调用 setup_canvas，不要只说“去配置 token”。
- 用户把文件拖入终端后会得到路径（如 `/Users/xxx/hw1.pdf`），说「帮我提交这个文件」「帮我交作业」→ submit_canvas_assignment
- **提交流程（必须两步走）：**
  1. 先调 list_canvas_assignments（可传 course_filter 缩小范围）列出可提交的作业
  2. 把列表展示给用户，请用户确认目标作业（课程名+作业名），再调 submit_canvas_assignment
  3. 切勿跳过确认步骤，以免提交到错误的作业
- 提交成功后显示：文件名、提交时间、作业名、课程名
- 文件路径含空格时原样传入，勿修改

## 提醒事项管理
- 用户说「帮我记一下」「提醒我XXX」「把XXX加到提醒」→ add_reminder（从上下文提取时间）
- 用户说「我有什么提醒」「有什么要做的」「提醒列表」→ list_reminders
- 展示时：未过期的用✅标注，已过期的用🔴标注，同时显示距离开始/结束的剩余时间
- 用户说「删除/取消提醒 XXX」→ remove_reminder
- 当从搜索或查询结果中发现有明确截止时间的重要事项（如报名、缴费、选课窗口），主动问用户「需要加入提醒列表吗？」

## 其他
- 用户说"重新配置"/"更新账号"时引导修改凭证
- 用户说"配置Canvas"/"设置Canvas"/"Canvas token 不会弄" → 调用 setup_canvas
- 用户说"配置水源"/"授权水源" → 调用 setup_shuiyuan
- 查询失败时主动提议重新登录（login_platform）
- 遇到任何没有提到的交大相关需求 → 先思考哪个工具最接近，直接尝试，不要说"我的功能有限"或"我只能帮你做XXX"。

## Telegram Bot 配置
用户说「接入Telegram」「配置Telegram」「怎么把你接入Telegram」「Telegram bot 怎么用」时：
1. 如果用户还没有 Bot Token：先引导去 Telegram 找 @BotFather，发 /newbot，按提示创建，拿到 Token
2. 用户提供 Token 后：调用 setup_telegram(telegram_token=...) 保存配置并验证 Token 有效性
3. 配置成功后告知用户：
   - 运行 `sjtu-agent telegram-bot` 启动 Bot（长轮询，适合本地/服务器常驻）
   - 在 Telegram 中给 Bot 发 /id，获取自己的 user_id
   - 如果想限制 Bot 只响应自己，再次调用 setup_telegram 补填 allowed_ids
4. Bot 功能与终端版本完全相同：可以查 DDL、看课表、查成绩、搜索校园内容等

## 微信 Bot 配置（ilink 协议）
用户说「接入微信」「配置微信」「微信 bot」「把你接入微信」「微信推送」时：
1. 调用 setup_wechat()，**这会在终端直接打印二维码并等待扫码**，整个过程在终端完成，无需用户手动操作
2. 扫码成功后 bot_token 自动保存到 config.json，告知用户：
   - 在微信里找到你刚才登录的 AI Bot（搜索"AI小助手"）
   - 给 Bot 发一条消息（如「你好」），系统自动记录 context_token
   - 运行 `python3 wechat_bot.py` 启动 Bot 后台服务（或 `sjtu-agent wechat-bot`）
3. Bot 功能与终端版本完全相同：查 DDL、看课表、查成绩、搜索校园内容、接收日报推送等"""

_TOOL_LABELS = {
    "list_canvas_assignments":  "正在列出 Canvas 作业",
    "submit_canvas_assignment": "正在上传并提交作业",
    "get_ddls":               "正在获取作业 DDL",
    "get_next_lab":           "正在查询物理实验安排",
    "get_all":                "正在获取全部信息",
    "save_credentials":       "正在保存凭证",
    "login_platform":         "正在自动登录",
    "download_assignments":   "正在下载作业材料",
    "list_assignment_files":  "正在列出作业文件",
    "read_assignment_file":   "正在读取作业内容",
    "search_campus":          "正在搜索校园内容",
    "get_schedule":           "正在查询课表",
    "browse_mysjtu":          "正在浏览 my.sjtu.edu.cn",
    "setup_canvas":          "正在引导配置 Canvas",
    "setup_shuiyuan":          "正在授权水源社区",
    "refresh_mysjtu_catalog": "正在爬取 my.sjtu.edu.cn 服务目录",
    "query_grades":           "正在查询教学信息服务网成绩",
    "add_reminder":           "正在添加提醒事项",
    "list_reminders":         "正在读取提醒列表",
    "remove_reminder":        "正在删除提醒事项",
    "check_setup":            "正在检查配置",
    "read_emails":            "正在读取交大邮箱…",
    "search_emails":          "正在搜索邮件…",
    "send_email":             "正在发送邮件…",
    "execute_python":         "正在执行代码…",
    "setup_telegram":         "正在配置 Telegram Bot…",
    "setup_wechat":           "正在启动微信扫码登录…",
}


def _is_anthropic_model(model: str) -> bool:
    return model.startswith("claude")


def _make_client(cfg: dict):
    """根据模型名自动选择 OpenAI 或 Anthropic SDK。"""
    if _is_anthropic_model(cfg.get("model", "")):
        # openclaudecode.cn 等代理服务会拦截 Anthropic SDK 默认 UA，需覆盖为 Claude CLI 风格
        ua = cfg.get("user_agent", "claude-cli/1.0.57")
        return Anthropic(
            api_key=cfg["api_key"],
            base_url=cfg.get("base_url") or None,
            default_headers={"user-agent": ua},
        )
    return OpenAI(api_key=cfg["api_key"], base_url=cfg.get("base_url") or None)


def _anthropic_tools() -> list:
    """将 OpenAI 工具格式转换为 Anthropic 格式。"""
    result = []
    for t in TOOLS:
        fn = t["function"]
        result.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn["parameters"],
        })
    return result


def _stream_with_think_tags(stream, spinner: "Spinner") -> tuple[str, str, dict]:
    """
    消费 OpenAI 兼容的流式响应，处理两种思考格式：
      1. delta.reasoning_content 字段（DeepSeek-R1 原生）
      2. <think>...</think> XML 标签混在 content 中（minimax / 部分模型）

    思考内容实时以暗体灰字流式输出（先停 Spinner，避免并发写屏乱码）。
    正文内容全部缓冲，流结束后由调用方统一用 print_markdown_message 渲染。

    关键：在开始写思考文字前先停 Spinner，思考结束后重启 Spinner 等待正文。
    这样消除了 Spinner 的 \\r 和 write() 并发竞争导致的闪烁。

    返回：(full_content_no_think, full_reasoning, tool_calls_map)
    """
    full_content   = ""   # 包含 <think> 的原始正文（用于存入 messages）
    full_reasoning = ""   # 思考内容（展示并收集）
    tool_calls_map: dict[int, dict] = {}

    TAG_OPEN  = "<think>"
    TAG_CLOSE = "</think>"
    in_think = False   # 当前是否在 <think> 块内
    thinking_started = False  # 是否已打印过思考前缀

    def _start_thinking():
        nonlocal thinking_started
        if thinking_started:
            return
        spinner.stop()  # ← 关键：先停 Spinner，再输出文字，避免 \r 覆盖
        if spinner._ansi():
            sys.stdout.write("\033[2m💭 ")  # 暗体灰字前缀（ANSI 支持时）
        else:
            sys.stdout.write("💭 思考中：")   # Windows 纯文本前缀
        sys.stdout.flush()
        thinking_started = True

    def _end_thinking():
        nonlocal thinking_started, in_think
        if thinking_started:
            if spinner._ansi():
                sys.stdout.write("\033[0m\n")  # 重置颜色，换行
            else:
                sys.stdout.write("\n")         # Windows：直接换行
            sys.stdout.flush()
            thinking_started = False
        in_think = False
        # 注意：不在这里重启 Spinner；由调用方在流结束后统一 stop/render


    for chunk in stream:
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta is None:
            continue

        # ── reasoning_content 字段（DeepSeek-R1 / Qwen 原生）────────────
        rc = getattr(delta, "reasoning_content", None) or ""
        if rc:
            _start_thinking()
            sys.stdout.write(rc)
            sys.stdout.flush()
            full_reasoning += rc

        # ── content 字段 ────────────────────────────────────────────────
        text_chunk = delta.content or ""
        if text_chunk:
            full_content += text_chunk

            # 处理 <think> 标签
            if TAG_OPEN in text_chunk and not in_think:
                in_think = True
                # 取 <think> 之后的内容
                after = text_chunk[text_chunk.index(TAG_OPEN) + len(TAG_OPEN):]
                if after:
                    _start_thinking()
                    sys.stdout.write(after)
                    sys.stdout.flush()
                    full_reasoning += after
            elif TAG_CLOSE in text_chunk and in_think:
                # 取 </think> 之前的内容
                before = text_chunk[:text_chunk.index(TAG_CLOSE)]
                if before:
                    _start_thinking()
                    sys.stdout.write(before)
                    sys.stdout.flush()
                    full_reasoning += before
                _end_thinking()
            elif in_think:
                # 在思考块内部
                _start_thinking()
                sys.stdout.write(text_chunk)
                sys.stdout.flush()
                full_reasoning += text_chunk
            else:
                # 普通正文：若之前有 reasoning_content 思考，先结束思考显示
                if thinking_started:
                    _end_thinking()

        # ── 工具调用 ─────────────────────────────────────────────────────
        if delta.tool_calls:
            for tc_delta in delta.tool_calls:
                idx = tc_delta.index
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {"id": "", "name": "", "arguments": ""}
                entry = tool_calls_map[idx]
                if tc_delta.id:
                    entry["id"] += tc_delta.id
                if tc_delta.function:
                    if tc_delta.function.name:
                        entry["name"] += tc_delta.function.name
                    if tc_delta.function.arguments:
                        entry["arguments"] += tc_delta.function.arguments

    # 流结束时若还在思考状态，收尾
    if thinking_started:
        _end_thinking()

    # 从 full_content 中剥离 <think>...</think> 块，得到纯正文
    clean_content = re.sub(r"<think>.*?</think>", "", full_content, flags=re.DOTALL).strip()
    return clean_content, full_reasoning, tool_calls_map


def _run_one_turn_openai(client: OpenAI, model: str, messages: list) -> None:
    """流式输出版本：
    - 思考过程（reasoning_content 或 <think> 标签）实时灰色显示
    - 正文内容流式缓冲，结束后用 print_markdown_message 统一渲染 markdown
    """
    spinner = Spinner()

    while True:
        # ── 流式请求 ────────────────────────────────────────────────────────
        spinner.start("等待响应…")
        try:
            stream = client.chat.completions.create(
                model=model, messages=messages, tools=TOOLS, tool_choice="auto",
                timeout=180, stream=True,
            )
        except Exception as e:
            spinner.stop()
            err = str(e).lower()
            if "timeout" in err or "timed out" in err or "read" in err:
                import time as _time
                print(f"\r[提示] 网络超时，5 秒后重试…（{e}）")
                _time.sleep(5)
                continue
            raise
        try:
            clean_content, _reasoning, tool_calls_map = _stream_with_think_tags(stream, spinner)
        except Exception as e:
            spinner.stop()
            raise
        spinner.stop()  # 无思考内容时 _stream_with_think_tags 不会停 spinner，在此兜底

        # ── 渲染正文（markdown）──────────────────────────────────────────
        if clean_content:
            print_markdown_message("Agent", clean_content)

        # ── 纯文本回复（无工具调用）─────────────────────────────────────
        if not tool_calls_map:
            messages.append({"role": "assistant", "content": clean_content})
            return

        # ── 有工具调用：构建 assistant 消息并执行 ───────────────────────
        from openai.types.chat import ChatCompletionMessageToolCall
        from openai.types.chat.chat_completion_message_tool_call import Function
        from openai.types.chat import ChatCompletionMessage

        tool_call_objs = []
        for idx in sorted(tool_calls_map):
            e = tool_calls_map[idx]
            tool_call_objs.append(
                ChatCompletionMessageToolCall(
                    id=e["id"],
                    type="function",
                    function=Function(name=e["name"], arguments=e["arguments"]),
                )
            )

        assistant_msg = ChatCompletionMessage(
            role="assistant",
            content=clean_content or None,
            tool_calls=tool_call_objs,
        )
        messages.append(assistant_msg)

        for tc in tool_call_objs:
            fn_name = tc.function.name
            fn_args = json.loads(tc.function.arguments or "{}")
            if fn_name not in ("check_setup",):
                spinner.start(_TOOL_LABELS.get(fn_name, fn_name) + "…")
            result = run_tool(fn_name, fn_args)
            if fn_name not in ("check_setup",):
                spinner.stop()
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})


def _run_one_turn_anthropic(client: Anthropic, model: str, messages: list) -> None:
    """流式调用 Anthropic Messages API（SSE），实时显示 thinking block 和正文。"""
    import httpx as _httpx
    import json as _json
    spinner = Spinner()
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    tools  = _anthropic_tools()

    api_key  = client.api_key
    base_url = str(client.base_url).rstrip("/")
    ua       = (client.default_headers or {}).get("user-agent", "claude-cli/1.0.57")
    endpoint = f"{base_url}/v1/messages"
    req_headers = {
        "x-api-key":          api_key,
        "anthropic-version":  "2023-06-01",
        "content-type":       "application/json",
        "user-agent":         ua,
    }

    while True:
        api_msgs = [m for m in messages if m["role"] != "system"]
        spinner.start("等待响应…")

        # ── SSE 流式请求 ────────────────────────────────────────────────────
        content_blocks: list[dict] = []     # 最终 assistant 消息内容
        tool_inputs: dict[int, str] = {}    # block_index -> accumulated JSON str
        in_thinking = False
        in_text     = False
        full_text   = ""
        error_payload: dict | None = None

        try:
            with _httpx.stream(
                "POST", endpoint,
                headers=req_headers,
                json={"model": model, "system": system, "messages": api_msgs,
                      "tools": tools, "max_tokens": 4096, "stream": True},
                timeout=180,
            ) as resp:
                spinner.stop()

                if resp.status_code not in (200,):
                    body = resp.read().decode()
                    try:
                        error_payload = _json.loads(body)
                    except Exception:
                        error_payload = {"raw": body}
                else:
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            ev = _json.loads(data_str)
                        except Exception:
                            continue

                        ev_type = ev.get("type", "")

                        # 新 block 开始
                        if ev_type == "content_block_start":
                            block = ev.get("content_block", {})
                            btype = block.get("type", "")
                            bidx  = ev.get("index", len(content_blocks))
                            if btype == "thinking":
                                in_thinking = True
                                spinner.start("思考中…")  # Spinner 替代，隐藏思维链内容
                                content_blocks.append({"type": "thinking", "thinking": ""})
                            elif btype == "text":
                                # 文字 block 开始：停止思考 Spinner，换一个等待 Spinner
                                if in_thinking:
                                    spinner.stop()
                                    in_thinking = False
                                in_text = True
                                spinner.start("处理中…")
                                content_blocks.append({"type": "text", "text": ""})
                            elif btype == "tool_use":
                                content_blocks.append({
                                    "type": "tool_use",
                                    "id": block.get("id", ""),
                                    "name": block.get("name", ""),
                                    "input": {},
                                })
                                tool_inputs[bidx] = ""

                        # delta
                        elif ev_type == "content_block_delta":
                            delta = ev.get("delta", {})
                            dtype = delta.get("type", "")
                            bidx  = ev.get("index", 0)

                            if dtype == "thinking_delta":
                                chunk = delta.get("thinking", "")
                                # 只累积，不输出到终端（用 Spinner 代替，避免 ANSI 光标计算闪烁）
                                if content_blocks and content_blocks[-1].get("type") == "thinking":
                                    content_blocks[-1]["thinking"] += chunk

                            elif dtype == "text_delta":
                                chunk = delta.get("text", "")
                                # 只缓冲，不实时输出（等 block 结束后统一 markdown 渲染）
                                full_text += chunk
                                if content_blocks and content_blocks[-1].get("type") == "text":
                                    content_blocks[-1]["text"] += chunk

                            elif dtype == "input_json_delta":
                                tool_inputs[bidx] = tool_inputs.get(bidx, "") + delta.get("partial_json", "")

                        # block 结束
                        elif ev_type == "content_block_stop":
                            bidx = ev.get("index", 0)
                            # 把累积的 input JSON 解析回 dict
                            if bidx in tool_inputs and bidx < len(content_blocks):
                                blk = content_blocks[bidx]
                                if blk.get("type") == "tool_use":
                                    try:
                                        blk["input"] = _json.loads(tool_inputs[bidx] or "{}")
                                    except Exception:
                                        blk["input"] = {}

                        elif ev_type == "message_stop":
                            break

                        elif ev_type == "error":
                            error_payload = ev.get("error", ev)
                            break

        except (
            _httpx.ReadTimeout, _httpx.ConnectTimeout,
            _httpx.TimeoutException, _httpx.ConnectError,
            _httpx.RemoteProtocolError, _httpx.NetworkError,
        ) as e:
            spinner.stop()
            if in_thinking or in_text:
                sys.stdout.write("\033[0m\n")
                sys.stdout.flush()
            import time as _time
            print(f"\r[提示] 网络连接失败，5 秒后重试…（{type(e).__name__}: {e}）")
            _time.sleep(5)
            continue
        except Exception as e:
            spinner.stop()
            if in_thinking or in_text:
                sys.stdout.write("\033[0m\n")
                sys.stdout.flush()
            # 对于非预期异常，打印错误但不退出聊天循环
            print(f"\r[错误] 请求失败：{type(e).__name__}: {e}")
            return  # 返回到 chat_loop，让用户重新输入
        finally:
            spinner.stop()

        # ── 收尾渲染 ──────────────────────────────────────────────────────
        if in_thinking:
            spinner.stop()
            in_thinking = False
        if in_text and full_text:
            spinner.stop()  # 停止"处理中…" spinner，再渲染正文
            print_markdown_message("Agent", full_text)
        elif in_text:
            spinner.stop()

        # ── 错误处理 ──────────────────────────────────────────────────────
        if error_payload:
            import time as _time
            msg = (error_payload.get("message") or str(error_payload))[:200]
            if "overload" in msg.lower() or "过载" in msg:
                print(f"\r[提示] 模型过载，10 秒后重试…")
                _time.sleep(10)
                continue
            if error_payload.get("type") == "invalid_request_error" and "500" in str(error_payload):
                import time as _time
                _time.sleep(5)
                continue
            raise RuntimeError(f"Anthropic API 错误: {msg}")

        # ── 判断是否有工具调用 ────────────────────────────────────────────
        has_tool_use = any(b.get("type") == "tool_use" for b in content_blocks)
        messages.append({"role": "assistant", "content": content_blocks})

        if not has_tool_use:
            return

        # ── 执行工具 ──────────────────────────────────────────────────────
        tool_results = []
        for b in content_blocks:
            if b.get("type") != "tool_use":
                continue
            fn_name = b["name"]
            fn_args = b["input"] if isinstance(b["input"], dict) else {}
            if fn_name not in ("check_setup",):
                spinner.start(_TOOL_LABELS.get(fn_name, fn_name) + "…")
            result = run_tool(fn_name, fn_args)
            if fn_name not in ("check_setup",):
                spinner.stop()
            tool_results.append({"type": "tool_result", "tool_use_id": b["id"], "content": result})
        messages.append({"role": "user", "content": tool_results})


def _run_one_turn(client, model: str, messages: list) -> None:
    if _is_anthropic_model(model):
        _run_one_turn_anthropic(client, model, messages)
    else:
        _run_one_turn_openai(client, model, messages)


def chat_loop(client, model: str):
    import datetime as _dt
    _now = _dt.datetime.now()
    _year = _now.year
    _month = _now.month
    # 判断当前学期：9-1月=第1学期(秋), 2-6月=第2学期(春), 7-8月=第3学期(夏)
    if _month >= 9:
        _cur_xnm = _year       # 如 2025（即2025-2026学年）
        _cur_xqm = "1"
        _prev_xnm = _year - 1  # 上学期 = 上一学年第2学期
        _prev_xqm = "2"
    elif _month <= 6:
        _cur_xnm = _year - 1   # 如 2025（即2025-2026学年）
        _cur_xqm = "2"
        _prev_xnm = _year - 1  # 上学期 = 同一学年第1学期
        _prev_xqm = "1"
    else:  # 7-8月
        _cur_xnm = _year - 1
        _cur_xqm = "3"
        _prev_xnm = _year - 1
        _prev_xqm = "2"

    _date_ctx = (
        f"\n\n## 当前时间（自动注入，每次对话刷新）\n"
        f"现在：{_now.strftime('%Y年%m月%d日 %H:%M')}，星期{'一二三四五六日'[_now.weekday()]}。\n"
        f"当前学期：{_cur_xnm}-{_cur_xnm+1}学年第{_cur_xqm}学期。\n"
        f"「上学期」= {_prev_xnm}-{_prev_xnm+1}学年第{_prev_xqm}学期"
        f"（query_grades: year='{_prev_xnm}', semester='{_prev_xqm}'）。\n"
        f"「本学期」= {_cur_xnm}-{_cur_xnm+1}学年第{_cur_xqm}学期"
        f"（query_grades: year='{_cur_xnm}', semester='{_cur_xqm}'）。\n"
        f"「本学年」= {_cur_xnm}学年"
        f"（query_grades: year='{_cur_xnm}', semester=''）。"
    )
    messages = [{"role": "system", "content": SYSTEM_PROMPT + _date_ctx}]
    model_box  = [model]   # 用列表包裹使内部可修改
    client_box = [client]  # 同理，切换模型时可替换 client

    # ── 启动时后台预热 DDL 缓存 + 检查更新（完全不阻塞主线程）──────────────────
    _prefetch_ddls_background()
    _update_thread = threading.Thread(target=_check_for_updates, daemon=True)
    _update_thread.start()
    # 不在这里 join()，避免 git fetch 慢网络时卡住启动

    # ── 启动检查：直接调本地函数，无需 LLM roundtrip ─────────────────────────

    print("正在检查配置状态…", flush=True)
    setup = tool_check_setup()
    all_ok = (
        setup["jaccount"]["has_credentials"]
        and setup["canvas"]["has_token"]
        and setup["aihaoke"]["has_cookies"]
        and setup["phycai"]["has_cookies"]
        and setup["icourse"]["has_cookies"]
    )
    if all_ok:
        uname = setup["jaccount"].get("username") or ""
        print(f"✅ 所有平台已就绪（{uname}）\n")
        print("输入问题继续对话，输入 quit 退出。\n")
    else:
        # 有未完成配置，让 LLM 引导
        setup_json = json.dumps(setup, ensure_ascii=False)
        messages.append({
            "role": "user",
            "content": f"配置检查结果：{setup_json}\n请根据结果告知我缺少哪些配置，并引导我完成设置。",
        })
        _run_one_turn(client_box[0], model_box[0], messages)
        print("输入问题继续对话，输入 quit 退出。\n")

    # ── 启动时检查即将到期的提醒事项（30分钟内）────────────────────────────
    import datetime as _dt2
    _now2 = _dt2.datetime.now(dc.CST)
    _soon = _now2 + _dt2.timedelta(minutes=30)
    _due_reminders = []
    for _r in _load_reminders():
        for _key in ("start", "end"):
            _ts = _r.get(_key, "")
            if not _ts:
                continue
            try:
                _rdt = _dt2.datetime.fromisoformat(_ts)
                if _rdt.tzinfo is None:
                    _rdt = _rdt.replace(tzinfo=dc.CST)
                if _now2 <= _rdt <= _soon:
                    _due_reminders.append(
                        f"  ⏰ 【{_r['title']}】{'开始' if _key=='start' else '结束'}"
                        f" 于 {_rdt.strftime('%H:%M')}"
                        + (f"（{_r['note']}）" if _r.get("note") else "")
                    )
            except Exception:
                pass
    if _due_reminders:
        print_rule("即将到期的提醒事项（30分钟内）")
        for _line in _due_reminders:
            print(_line)
        print()

    # ── 在第一次等待用户输入前，最多等 2 秒看更新检查结果 ─────────────────
    _update_thread.join(timeout=2)
    if _UPDATE_AVAILABLE.get("behind"):
        behind = _UPDATE_AVAILABLE["behind"]
        print(f"💡 有 {behind} 个新提交可用，运行 sjtu-agent update 即可一键更新。\n")

    while True:
        try:
            user_input = input(f"你[{model_box[0]}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "退出", "q"):
            print("再见！")
            break

        # 断言命令
        if user_input.startswith("/model"):
            print_rule("切换模型配置")
            cur = load_agent_config()
            new_base  = input(f"API Base URL（当前: {cur.get('base_url','')}，回车不变）: ").strip()
            new_key   = input(f"API Key（当前: {'*'*8 if cur.get('api_key') else '未设置'}，回车不变）: ").strip()
            new_model = input(f"模型名称（当前: {cur.get('model','')}，回车不变）: ").strip()
            updated = {
                "base_url": new_base  or cur.get("base_url", "https://api.openai.com/v1"),
                "api_key":  new_key   or cur.get("api_key", ""),
                "model":    new_model or cur.get("model", "deepseek-chat"),
            }
            AGENT_CONFIG_PATH.write_text(json.dumps(updated, indent=2, ensure_ascii=False))
            client_box[0] = _make_client(updated)
            model_box[0] = updated["model"]
            # 切换协议时重置对话，避免消息格式冲突
            messages.clear()
            messages.append({"role": "system", "content": SYSTEM_PROMPT})
            proto = "Anthropic" if _is_anthropic_model(updated["model"]) else "OpenAI"
            print(f"  已切换到: {updated['model']}  [协议: {proto}]（已保存，对话已重置）\n")
            continue

        messages.append({"role": "user", "content": user_input})
        try:
            _run_one_turn(client_box[0], model_box[0], messages)
        except KeyboardInterrupt:
            print("\n[已中断当前请求，可继续输入]")
            # 移除未完成的 user 消息，保持历史干净
            if messages and messages[-1].get("role") == "user":
                messages.pop()
        except Exception as e:
            print(f"\r[错误] 本轮请求失败（{type(e).__name__}: {e}），请重新输入。")
            # 移除未完成的 user 消息
            if messages and messages[-1].get("role") == "user":
                messages.pop()


def main():
    cfg = load_agent_config()
    if not cfg or not cfg.get("api_key"):
        cfg = setup_agent_config()
    client = _make_client(cfg)
    chat_loop(client, cfg["model"])


if __name__ == "__main__":
    main()
