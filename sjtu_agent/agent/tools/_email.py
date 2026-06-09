"""Email tools — IMAP read/search + SMTP send via mail.sjtu.edu.cn."""

import imaplib
import os
import smtplib
import ssl
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate, make_msgid


_SJTU_IMAP_HOST = "mail.sjtu.edu.cn"
_SJTU_IMAP_PORT = 993
_SJTU_SMTP_HOST = "mail.sjtu.edu.cn"
_SJTU_SMTP_PORT = 465

# ── TOOLS schema entries ──────────────────────────────────────────────────────

TOOLS_ENTRIES = [
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


# ── helpers ───────────────────────────────────────────────────────────────────

def _get_email_creds() -> tuple[str, str]:
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
    username, password = _get_email_creds()
    if not username or not password:
        raise ValueError("未配置邮箱账号或密码（EMAIL_USERNAME / EMAIL_PASSWORD 或 JACCOUNT_USERNAME / JACCOUNT_PASSWORD）")
    ctx = ssl.create_default_context()
    m = imaplib.IMAP4_SSL(_SJTU_IMAP_HOST, _SJTU_IMAP_PORT, ssl_context=ctx)
    m.login(username, password)
    return m


def _parse_email_headers(raw_bytes: bytes) -> dict:
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


# ── tool implementations ──────────────────────────────────────────────────────

def tool_read_emails(
    folder: str = "INBOX",
    limit: int = 10,
    unread_only: bool = False,
    with_body: bool = False,
    uid: str = "",
) -> dict:
    try:
        m = _imap_connect()
    except ValueError as e:
        return {"error": str(e), "hint": "请先配置 EMAIL_USERNAME / EMAIL_PASSWORD 或 JACCOUNT_USERNAME / JACCOUNT_PASSWORD"}
    except Exception as e:
        return {"error": f"IMAP 登录失败：{e}"}

    try:
        _folder_map = {
            "收件箱": "INBOX", "已发送": "Sent", "发件箱": "Sent",
            "垃圾邮件": "Junk", "已删除": "Trash", "草稿": "Drafts", "草稿箱": "Drafts",
        }
        select_folder = _folder_map.get(folder, folder)

        if uid:
            typ, data = m.select(select_folder, readonly=True)
            if typ != "OK":
                typ, data = m.select(f'"{select_folder}"', readonly=True)
            typ2, raw = m.uid("FETCH", uid, "(RFC822)")
            m.close()
            m.logout()
            if typ2 != "OK" or not raw or not raw[0]:
                return {"error": f"未找到 UID={uid} 的邮件"}
            raw_bytes = raw[0][1]
            headers = _parse_email_headers(raw_bytes)
            body = _parse_email_body(raw_bytes)
            return {"uid": uid, **headers, "body": body}

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
            "folder": select_folder,
            "total_found": len(uid_list),
            "returned": len(emails),
            "emails": emails,
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

        try:
            typ, uids_data = m.uid(
                "SEARCH", "CHARSET", "UTF-8",
                search_in_upper, keyword.encode("utf-8"),
            )
        except imaplib.IMAP4.error:
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
            "keyword": keyword,
            "search_in": search_in_upper,
            "folder": select_folder,
            "total_found": len(uid_list),
            "returned": len(emails),
            "emails": emails,
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
    username, password = _get_email_creds()
    if not username or not password:
        return {"error": "未配置邮箱账号，请设置 EMAIL_USERNAME / EMAIL_PASSWORD 或 JACCOUNT_USERNAME / JACCOUNT_PASSWORD"}

    in_reply_to = ""
    references = ""
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
                references = in_reply_to
        except Exception:
            pass

    msg = MIMEMultipart()
    msg["From"] = username
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    if cc:
        msg["Cc"] = cc
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references

    msg.attach(MIMEText(body, "plain", "utf-8"))

    recipients = [r.strip() for r in to.split(",") if r.strip()]
    if cc:
        recipients += [r.strip() for r in cc.split(",") if r.strip()]

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(_SJTU_SMTP_HOST, _SJTU_SMTP_PORT, context=ctx, timeout=30) as smtp:
            smtp.login(username, password)
            refused = smtp.sendmail(username, recipients, msg.as_bytes())
    except smtplib.SMTPAuthenticationError:
        return {"error": "SMTP 登录失败：用户名或密码错误。请尝试在网页邮箱开启「客户端授权码」并将授权码设为 EMAIL_PASSWORD。"}
    except Exception as e:
        return {"error": f"发送失败：{e}"}

    appended_to_sent = False
    sent_error = ""
    try:
        m = _imap_connect()
        try:
            m.append("Sent", "\\Seen", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            appended_to_sent = True
        finally:
            try: m.logout()
            except Exception: pass
    except Exception as e:
        sent_error = str(e)

    return {
        "ok": True,
        "from": username,
        "to": to,
        "cc": cc,
        "subject": subject,
        "message_id": msg["Message-ID"],
        "refused": refused,
        "appended_to_sent": appended_to_sent,
        "sent_append_error": sent_error,
        "note": "SMTP queued accepted by mail.sjtu.edu.cn. If 对方没收到，请检查对方垃圾邮件箱。",
    }
