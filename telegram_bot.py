#!/usr/bin/env python3
"""
telegram_bot.py — 将 agent.py 接入 Telegram Bot

用法:
  python3 telegram_bot.py          # 正常运行（长轮询）
  python3 telegram_bot.py --test   # 只测试 token 连通性，不启动

配置（config.json）:
  telegram_token        : BotFather 给的 token
  telegram_allowed_ids  : 允许使用的 Telegram user_id 列表（整数）
                          留空列表时，bot 会对任何人回复其 chat_id，方便首次配置

命令:
  /start /help  — 帮助
  /id           — 显示自己的 chat_id（添加到白名单用）
  /reset        — 清空本次对话历史
  /reminders    — 查看提醒事项列表
"""

import io
import json
import re
import sys
import threading
import datetime as _dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from sjtu_agent.paths import CONFIG_PATH

import telebot
import agent
import ddl_checker as dc

# ── 配置加载 ──────────────────────────────────────────────────────────────────

def _load_cfg() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

cfg         = _load_cfg()
BOT_TOKEN   = cfg.get("telegram_token", "")
ALLOWED_IDS = set(int(x) for x in cfg.get("telegram_allowed_ids", []))

if not BOT_TOKEN:
    print("❌ config.json 中未设置 telegram_token，请先运行 setup_config.py")
    sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN)

# ── 会话状态（每个 chat_id 独立） ─────────────────────────────────────────────

_sessions: dict[int, dict] = {}
_locks:    dict[int, threading.Lock] = {}


def _get_session(chat_id: int) -> dict:
    if chat_id not in _sessions:
        agent_cfg = agent.load_agent_config()
        _sessions[chat_id] = {
            "messages":   [],
            "model_box":  [agent_cfg["model"]],
            "client_box": [agent._make_client(agent_cfg)],
        }
        _locks[chat_id] = threading.Lock()
    return _sessions[chat_id]


def _build_date_ctx() -> str:
    """生成包含当前精确时间的日期上下文（每次调用都是最新时间）。"""
    now   = _dt.datetime.now()
    year  = now.year
    month = now.month
    if month >= 9:
        cur_xnm, cur_xqm   = year,     "1"
        prev_xnm, prev_xqm = year - 1, "2"
    elif month <= 6:
        cur_xnm, cur_xqm   = year - 1, "2"
        prev_xnm, prev_xqm = year - 1, "1"
    else:
        cur_xnm, cur_xqm   = year - 1, "3"
        prev_xnm, prev_xqm = year - 1, "2"
    return (
        f"\n\n## 当前时间（每轮自动刷新）\n"
        f"现在：{now.strftime('%Y年%m月%d日 %H:%M')}，星期{'一二三四五六日'[now.weekday()]}。\n"
        f"当前学期：{cur_xnm}-{cur_xnm+1}学年第{cur_xqm}学期。\n"
        f"「上学期」={prev_xnm}-{prev_xnm+1}学年第{prev_xqm}学期"
        f"（query_grades: year='{prev_xnm}', semester='{prev_xqm}'）。\n"
        f"「本学期」={cur_xnm}-{cur_xnm+1}学年第{cur_xqm}学期"
        f"（query_grades: year='{cur_xnm}', semester='{cur_xqm}'）。"
    )


def _init_messages(sess: dict) -> None:
    """首次对话时注入 system prompt；后续每轮由 _capture_turn 刷新时间。"""
    if sess["messages"]:
        return
    sess["messages"].append({"role": "system", "content": agent.SYSTEM_PROMPT + _build_date_ctx()})


# ── 输出捕获 ──────────────────────────────────────────────────────────────────

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mKABCDEFGHJKST]')


def _capture_turn(sess: dict, user_text: str) -> str:
    """
    运行一轮对话，捕获 stdout，提取并返回 Agent 回复文本。
    Spinner 的 \r 控制序列和 ANSI 颜色代码会被过滤掉。
    """
    _init_messages(sess)
    # 每轮刷新 system prompt 中的时间，避免长会话里时间过期
    if sess["messages"] and sess["messages"][0]["role"] == "system":
        sess["messages"][0]["content"] = agent.SYSTEM_PROMPT + _build_date_ctx()
    sess["messages"].append({"role": "user", "content": user_text})

    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        agent._run_one_turn(
            sess["client_box"][0],
            sess["model_box"][0],
            sess["messages"],
        )
    finally:
        sys.stdout = old_stdout

    raw = buf.getvalue()
    # 去除 ANSI 转义码
    clean = _ANSI_RE.sub("", raw)
    # 找到最后一个 "Agent: " 标记（spinner 输出在它之前）
    marker = "Agent: "
    idx = clean.rfind(marker)
    if idx == -1:
        # 没有找到，可能全是工具调用后无文本回复
        # 尝试从消息历史里取最后一条 assistant 消息
        for m in reversed(sess["messages"]):
            if m.get("role") == "assistant":
                content = m.get("content", "")
                if isinstance(content, str):
                    return content.strip() or "(已完成)"
                elif isinstance(content, list):
                    texts = [b.get("text", "") for b in content if b.get("type") == "text"]
                    return "\n".join(texts).strip() or "(已完成)"
        return "(已完成)"

    return clean[idx + len(marker):].strip()


# ── Markdown → Telegram HTML 转换 ────────────────────────────────────────────

def _md_to_tg_html(text: str) -> str:
    """
    将 agent 输出的 Markdown 文本转换为 Telegram 支持的 HTML 格式。
    Telegram HTML 只支持：<b> <i> <u> <s> <code> <pre> <a>
    """
    import html as _html
    import re as _re

    # 1. 先转义 HTML 特殊字符（避免内容中的 < > & 被解析为标签）
    #    但要跳过已有的 HTML 标签（如日报里的 <b>）
    #    检测是否已经是 HTML：包含明确的 <b> <i> 等标签
    if _re.search(r'<(b|i|u|code|pre|a)\b[^>]*>', text):
        # 已经是 HTML 格式，直接返回
        return text

    # 转义纯文本中的 HTML 字符
    def _escape_non_tag(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    lines = text.split("\n")
    result = []

    for line in lines:
        # 水平分隔线
        if _re.match(r"^\s*[-*_]{3,}\s*$", line):
            result.append("")
            continue

        # ### 标题 → <b>
        m = _re.match(r"^#{1,6}\s+(.*)", line)
        if m:
            content = _escape_non_tag(m.group(1).strip())
            # 移除标题内的 ** 标记
            content = _re.sub(r"\*\*(.*?)\*\*", r"\1", content)
            result.append(f"<b>{content}</b>")
            continue

        # 普通行：转义后处理内联样式
        line = _escape_non_tag(line)

        # **bold** → <b>bold</b>
        line = _re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", line)

        # *italic* 或 _italic_ → <i>italic</i>（避免误伤正常下划线）
        line = _re.sub(r"\*([^*\n]+?)\*", r"<i>\1</i>", line)

        # `code` → <code>code</code>
        line = _re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", line)

        # [text](url) → <a href="url">text</a>
        line = _re.sub(r"\[([^\]]+?)\]\((https?://[^\)]+?)\)", r'<a href="\2">\1</a>', line)

        result.append(line)

    return "\n".join(result)


# ── 消息发送工具 ──────────────────────────────────────────────────────────────

def _send_chunks(chat_id: int, text: str, max_len: int = 4000, parse_mode: str = "") -> None:
    """自动分割超长消息（Telegram 限制 4096 字节）。"""
    while text:
        bot.send_message(chat_id, text[:max_len], parse_mode=parse_mode or None)
        text = text[max_len:]


def _keep_typing(chat_id: int, stop_event: threading.Event) -> None:
    """在处理期间持续发送 typing 动作（每 4 秒一次）。"""
    while not stop_event.wait(4):
        try:
            bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass


# ── 权限检查 ──────────────────────────────────────────────────────────────────

def _is_authorized(chat_id: int, message) -> bool:
    if not ALLOWED_IDS:
        bot.reply_to(
            message,
            f"⚠️ 白名单未配置。\n\n"
            f"你的 chat_id 是：<code>{chat_id}</code>\n\n"
            f"请将此 ID 添加到 config.json 的 <code>telegram_allowed_ids</code> 列表中，"
            f"然后重启 bot。",
            parse_mode="HTML",
        )
        return False
    if chat_id not in ALLOWED_IDS:
        bot.reply_to(message, f"⛔ 未授权访问（chat_id: {chat_id}）")
        return False
    return True


# ── 命令处理 ──────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start", "help"])
def cmd_start(msg):
    chat_id = msg.chat.id
    if not _is_authorized(chat_id, msg):
        return
    bot.reply_to(msg, (
        "🤖 <b>SJTU Agent</b>\n\n"
        "直接发消息开始对话。\n\n"
        "<b>命令：</b>\n"
        "/report — 立即生成今日学习日报\n"
        "/reset — 重置对话历史\n"
        "/reminders — 查看提醒事项\n"
        "/id — 显示你的 chat_id"
    ), parse_mode="HTML")


@bot.message_handler(commands=["report"])
def cmd_report(msg):
    chat_id = msg.chat.id
    if not _is_authorized(chat_id, msg):
        return
    bot.send_chat_action(chat_id, "typing")
    stop_typing = threading.Event()
    typing_thread = threading.Thread(
        target=_keep_typing, args=(chat_id, stop_typing), daemon=True
    )
    typing_thread.start()
    def run_report():
        try:
            import daily_report
            report = daily_report.build_report()
            _send_chunks(chat_id, report, parse_mode="HTML")
        except Exception as e:
            bot.send_message(chat_id, f"❌ 生成日报出错：{e}")
        finally:
            stop_typing.set()
    threading.Thread(target=run_report, daemon=True).start()


@bot.message_handler(commands=["id"])
def cmd_id(msg):
    bot.reply_to(msg, f"你的 chat_id：<code>{msg.chat.id}</code>", parse_mode="HTML")


@bot.message_handler(commands=["reset"])
def cmd_reset(msg):
    if not _is_authorized(msg.chat.id, msg):
        return
    _sessions.pop(msg.chat.id, None)
    bot.reply_to(msg, "✅ 对话历史已清空。")


@bot.message_handler(commands=["reminders"])
def cmd_reminders(msg):
    if not _is_authorized(msg.chat.id, msg):
        return
    result = agent.tool_list_reminders()
    active  = result.get("active", [])
    expired = result.get("expired", [])
    lines   = [f"🕐 {result['current_time']}\n"]
    if active:
        lines.append(f"📌 <b>待提醒（{len(active)}条）</b>")
        for r in active:
            end  = f" → {r['end'][:16]}" if r.get("end") else ""
            note = f"\n   <i>{r['note']}</i>" if r.get("note") else ""
            lines.append(f"• [{r['id']}] {r['title']}  {r['start'][:16]}{end}{note}")
    else:
        lines.append("📌 暂无待提醒事项")
    if expired:
        lines.append(f"\n✅ <b>已过期（{len(expired)}条）</b>")
        for r in expired:
            lines.append(f"• [{r['id']}] {r['title']}")
    bot.reply_to(msg, "\n".join(lines), parse_mode="HTML")


# ── 普通消息处理 ──────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: True)
def handle_text(msg):
    chat_id = msg.chat.id
    if not _is_authorized(chat_id, msg):
        return
    if not msg.text:
        bot.reply_to(msg, "暂不支持非文字消息，请直接发文字。")
        return

    sess = _get_session(chat_id)
    lock = _locks[chat_id]

    if lock.locked():
        bot.reply_to(msg, "⏳ 上一条消息还在处理中，请稍候…")
        return

    bot.send_chat_action(chat_id, "typing")

    def run():
        stop_typing = threading.Event()
        typing_thread = threading.Thread(
            target=_keep_typing, args=(chat_id, stop_typing), daemon=True
        )
        typing_thread.start()
        try:
            with lock:
                reply = _capture_turn(sess, msg.text.strip())
            html_reply = _md_to_tg_html(reply)
            _send_chunks(chat_id, html_reply, parse_mode="HTML")
        except Exception as e:
            bot.send_message(chat_id, f"❌ 出错了：{e}")
        finally:
            stop_typing.set()

    threading.Thread(target=run, daemon=True).start()


# ── 公共函数（供 remind_check.py 调用） ──────────────────────────────────────

def send_reminder_via_telegram(title: str, subtitle: str, body: str) -> None:
    """
    向 telegram_allowed_ids 中所有用户推送提醒通知。
    由 remind_check.py 在找到匹配提醒时调用。
    """
    allowed = set(int(x) for x in _load_cfg().get("telegram_allowed_ids", []))
    if not allowed:
        return
    text = f"🔔 <b>{title}</b>\n<i>{subtitle}</i>"
    if body:
        text += f"\n{body}"
    _bot = telebot.TeleBot(BOT_TOKEN)
    for uid in allowed:
        try:
            _bot.send_message(uid, text, parse_mode="HTML")
        except Exception as e:
            print(f"[WARN] Telegram 推送失败 uid={uid}: {e}")


# ── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--test" in sys.argv:
        me = bot.get_me()
        print(f"✅ Bot 连通正常：@{me.username}（{me.first_name}）")
        print(f"   白名单：{ALLOWED_IDS or '(未设置，任何人发消息都会看到提示)'}")
        sys.exit(0)

    me = bot.get_me()
    print(f"✅ @{me.username} 已启动")
    print(f"   白名单：{ALLOWED_IDS or '(未设置)'}")

    # 向 Telegram 服务器注册命令列表，用户输入 / 时会弹出自动补全菜单
    from telebot.types import BotCommand
    bot.set_my_commands([
        BotCommand("report",    "📊 立即生成今日学习日报"),
        BotCommand("reminders", "📌 查看提醒事项列表"),
        BotCommand("reset",     "🔄 重置对话历史"),
        BotCommand("id",        "🔑 显示我的 chat_id"),
        BotCommand("help",      "❓ 帮助与命令列表"),
    ])
    print("   已注册 5 条命令（/report /reminders /reset /id /help）")
    print(f"   等待消息… （Ctrl+C 停止）")
    bot.infinity_polling(timeout=30, long_polling_timeout=30)
