"""sjtu_agent/homework_agent.py — Canvas 作业自动获取、分析与回传。

核心流程：
  1. 拉取 DDL → 过滤 Canvas + N 天内到期
  2. 下载作业文件到 ASSIGNMENTS_DIR / 课程 / 作业 /
  3. 读取各类文件（PDF/DOCX/HTML/MD/TXT）
  4. 调 LLM 生成：摘要 + 题目分析 + 参考答案
  5. 结果通过飞书推送回用户
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from sjtu_agent.paths import ASSIGNMENTS_DIR

import agent


def _get_feishu_config() -> dict | None:
    """读取飞书配置用于推送。"""
    from sjtu_agent.paths import CONFIG_PATH
    if not CONFIG_PATH.exists():
        return None
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if cfg.get("feishu_app_id") and cfg.get("feishu_open_id"):
            return cfg
    except Exception:
        pass
    return None


def _read_file(file_path: Path) -> str:
    """读取单个文件，返回文本内容。根据扩展名选择解析方式。"""
    ext = file_path.suffix.lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(file_path))
            text = ""
            for page in reader.pages[:10]:  # 最多读 10 页
                t = page.extract_text()
                if t:
                    text += t + "\n"
            return text.strip() or "[PDF 内容为空]"

        elif ext in (".docx", ".doc"):
            from docx import Document
            doc = Document(str(file_path))
            paras = [p.text for p in doc.paragraphs if p.text.strip()]
            return "\n".join(paras) or "[DOCX 内容为空]"

        elif ext in (".html", ".htm"):
            from html.parser import HTMLParser
            class _Stripper(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.text = []
                def handle_data(self, d):
                    self.text.append(d)
            s = _Stripper()
            s.feed(file_path.read_text(encoding="utf-8", errors="replace"))
            return "".join(s.text).strip() or "[HTML 内容为空]"

        elif ext in (".md", ".txt", ".tex", ".py", ".json", ".yaml", ".yml"):
            return file_path.read_text(encoding="utf-8", errors="replace").strip()

        else:
            return f"[不支持的文件格式: {ext}]"
    except Exception as e:
        return f"[读取失败: {e}]"


def _latex_to_unicode(text: str) -> str:
    """简单 LaTeX → Unicode 转换。"""
    replacements = {
        r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
        r"\epsilon": "ε", r"\theta": "θ", r"\lambda": "λ", r"\mu": "μ",
        r"\pi": "π", r"\sigma": "σ", r"\phi": "φ", r"\omega": "ω",
        r"\times": "×", r"\div": "÷", r"\pm": "±", r"\cdot": "·",
        r"\sum": "∑", r"\prod": "∏", r"\int": "∫", r"\infty": "∞",
        r"\leq": "≤", r"\geq": "≥", r"\neq": "≠", r"\approx": "≈",
        r"\sqrt": "√", r"\frac": "/", r"\partial": "∂", r"\nabla": "∇",
        r"\rightarrow": "→", r"\Rightarrow": "⇒", r"\leftarrow": "←",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text


def _extract_code_blocks(md_text: str) -> list[tuple[str, str]]:
    """从 Markdown 提取代码块，返回 [(language, code), ...]."""
    blocks = []
    pattern = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)
    for m in pattern.finditer(md_text):
        lang = m.group(1) or ""
        code = m.group(2).strip()
        blocks.append((lang, code))
    return blocks


def _generate_pdf(title: str, md_text: str, output_path: Path) -> None:
    """从 Markdown 文本生成 PDF（中文支持）。"""
    try:
        from fpdf import FPDF
        pdf = FPDF()
        pdf.add_page()
        font_path = "C:/Windows/Fonts/msyh.ttc"
        try:
            pdf.add_font("CJK", "", font_path, uni=True)
            pdf.add_font("CJK", "B", font_path, uni=True)
        except Exception:
            pass
        pdf.set_font("CJK", "", 11)
        pdf.set_font_size(14)
        pdf.multi_cell(0, 10, title)
        pdf.ln(5)
        pdf.set_font_size(10)
        clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", md_text)
        clean = re.sub(r"\*([^*]+)\*", r"\1", clean)
        clean = re.sub(r"`([^`]+)`", r"\1", clean)
        clean = re.sub(r"```.*?```", "[代码块]", clean, flags=re.DOTALL)
        for line in clean.split("\n"):
            if pdf.get_y() > 270:
                pdf.add_page()
            pdf.multi_cell(0, 5, line or " ")
        pdf.output(str(output_path))
        print(f"[homework] PDF 已保存: {output_path}")
    except Exception as e:
        print(f"[homework] PDF 生成失败: {e}")


def _generate_html(title: str, md_text: str, output_path: Path) -> None:
    """生成内嵌 MathJax 的 HTML，正确渲染 LaTeX 公式。"""
    # 转移 HTML 特殊字符
    escaped = md_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # 将 markdown 加粗转为 <b>
    import re as _re
    escaped = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = _re.sub(r"\*(.+?)\*", r"<i>\1</i>", escaped)
    # $$...$$ 公式块保持原样（MathJax 直接渲染）
    escaped = escaped.replace(chr(10), "<br>\n")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>{title}</title>
<script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
<script>window.MathJax = {{ tex: {{ inlineMath: [['$','$'], ['\\\\(','\\\\)']], displayMath: [['$$','$$'], ['\\\\[','\\\\]']] }} }};</script>
<style>body{{font-family:"Microsoft YaHei","SimHei",sans-serif;max-width:900px;margin:40px auto;line-height:1.8;font-size:15px;color:#222;padding:0 20px}}
b{{color:#1a1a2e}} i{{color:#555}} table{{border-collapse:collapse;margin:10px 0}} td,th{{border:1px solid #ccc;padding:4px 10px}}
</style></head><body>
<h2>{title}</h2>
<p>{escaped}</p>
</body></html>"""
    try:
        output_path.write_text(html, encoding="utf-8")
        print(f"[homework] HTML 已保存: {output_path}")
    except Exception as e:
        print(f"[homework] HTML 生成失败: {e}")


def generate_solution_files(title: str, solution: str, output_dir: Path) -> list[str]:
    """生成所有格式文件（.md / .py / .pdf / .docx）。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    # MD
    (output_dir / "_解答.md").write_text(solution, encoding="utf-8")
    saved.append(str(output_dir / "_解答.md"))
    # Code files
    for i, (lang, code) in enumerate(_extract_code_blocks(solution)):
        ext = lang if lang in ("py", "java", "cpp", "c", "js", "ts", "go") else "txt"
        p = output_dir / f"_code_{i+1}.{ext}"
        p.write_text(code, encoding="utf-8")
        saved.append(str(p))
    # PDF
    pdf_path = output_dir / "_解答.pdf"
    _generate_pdf(title, solution, pdf_path)
    if pdf_path.exists():
        saved.append(str(pdf_path))
    # HTML（MathJax 渲染公式）
    html_path = output_dir / "_解答.html"
    _generate_html(title, solution, html_path)
    if html_path.exists():
        saved.append(str(html_path))
    return saved


def read_assignment_content(assignment_dir: Path) -> str:
    """读取一个作业目录下所有文件，返回合并文本。"""
    if not assignment_dir.exists():
        return f"[目录不存在: {assignment_dir}]"
    parts = []
    for f in sorted(assignment_dir.iterdir()):
        if f.is_file() and not f.name.startswith("."):
            content = _read_file(f)
            if content:
                parts.append(f"[{f.name}]\n{content}")
    return "\n\n".join(parts) if parts else "[无可读文件]"


def _call_llm(prompt: str, llm_client=None, model: str = "") -> str:
    """调用 LLM 并返回结果。"""
    if llm_client is None:
        agent_cfg = agent.load_agent_config()
        if not agent_cfg.get("api_key"):
            return "[LLM 未配置]"
        llm_client = agent._make_client(agent_cfg)
        model = agent_cfg.get("model", "deepseek-chat")

    try:
        if agent._is_anthropic_model(model):
            resp = llm_client.messages.create(
                model=model, max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text or "[空响应]"
        else:
            resp = llm_client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}],
                max_tokens=4096,
            )
            text = resp.choices[0].message.content or ""
            think_re = re.compile(r"<think>.*?</think>", re.DOTALL)
            return think_re.sub("", text).strip() or "[空响应]"
    except Exception as e:
        return f"[分析失败: {e}]"


def solve_homework(course: str, assignment_name: str, content: str,
                   brief: bool = False) -> str:
    """让 LLM 实际解题并返回完整解答。brief=True 仅返回摘要。"""
    content = _latex_to_unicode(content)

    if brief:
        prompt = f"""课程：{course}，作业：{assignment_name}
{content[:6000]}

请用 1-2 句话概括这份作业的要求。"""
    else:
        prompt = f"""你是一位学霸，请完整解答以下作业。要能被直接提交。

课程：{course}
作业名称：{assignment_name}

{content[:8000]}

要求：
- 逐题解答，标清题号
- 编程题给出完整可运行代码（含必要的 import 和注释）
- 数学题给出分步推导和最终答案
- 论述题给出结构化论点
- 如题目信息不完整请标注推断依据
- 用中文回答"""
    return _call_llm(prompt)


# 向后兼容
analyze_homework = solve_homework


def _fetch_pending(include_past: bool = False) -> list[dict]:
    """获取 Canvas 作业。include_past=True 时包含已过期的历史作业。"""
    import ddl_checker as dc
    cfg = dc.load_config()
    ddls = dc.fetch_canvas(cfg, include_past=include_past)
    if include_past:
        # 只返回已过期但已提交的作业（历史作业）
        from datetime import datetime, timezone, timedelta
        CST = timezone(timedelta(hours=8))
        now = datetime.now(CST)
        past = [d for d in ddls if d.get("due") and hasattr(d["due"], "timestamp") and d["due"] < now]
        print(f"[homework] Canvas 共 {len(ddls)} 个作业，{len(past)} 个历史")
        return past
    pending = [d for d in ddls if not d.get("submitted")]
    print(f"[homework] Canvas 共 {len(ddls)} 个作业，{len(pending)} 个未提交")
    return pending


def _filter_by_due(pending: list[dict], due_within_days: int) -> list[dict]:
    """按截止天数过滤。due_within_days=0 表示不限制。"""
    if due_within_days <= 0:
        return pending
    import ddl_checker as dc
    from datetime import timedelta
    now_time = dc.NOW
    window = timedelta(days=due_within_days)
    filtered = []
    for d in pending:
        due = d.get("due")
        if due and hasattr(due, 'timestamp'):
            remaining = due - now_time
            if remaining <= window and remaining.total_seconds() > 0:
                filtered.append(d)
    return filtered


import shutil

# Claude Code CLI 路径（常驻进程 PATH 可能不包含 npm global 目录）
_CLAUDE_CANDIDATES = [
    shutil.which("claude"),
    shutil.which("claude.cmd"),
    r"D:\develop\node_global\claude.cmd",
    r"D:\develop\node_global\claude",
]
_CLAUDE_BIN = next((p for p in _CLAUDE_CANDIDATES if p and Path(p).exists()), "")


def _claude_code_solve(hw_dir: Path, course: str, aname: str, content: str,
                        brief: bool = False) -> str:
    """使用本地 Claude Code CLI 解题。不可用时回退到 _call_llm。"""
    if not _CLAUDE_BIN or not Path(_CLAUDE_BIN).exists():
        print("[homework] Claude Code 不可用，回退到 API 调用")
        return solve_homework(course, aname, content, brief=brief)

    import subprocess

    # 读取用户信息作为上下文
    user_ctx = ""
    try:
        from sjtu_agent.paths import CONFIG_PATH, ENV_PATH
        cfg_data = {}
        if CONFIG_PATH.exists():
            cfg_data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        jaccount = cfg_data.get("jaccount_username", "") or ""
        if jaccount:
            user_ctx = f"\n用户信息：jAccount 用户名 {jaccount}"
    except Exception:
        pass

    prompt = f"""你是上海交通大学的学霸。在当前工作目录中完成作业并生成文件。

课程：{course}
作业名称：{aname}{user_ctx}

**必须遵守的规则**：
- 禁止提出任何问题！禁止要求确认！禁止等待回复！
- 所有文件操作直接执行（--dangerously-skip-permissions 已开启）
- 个人信息缺失时用 [待填写] 占位，不要停下来问
- 无论是否完美，都要完成并输出结果
- 最后输出 200 字摘要，以 "SUMMARY:" 开头

工作流程：
1. 读取目录中的 description.html 和所有附件
2. 逐题解答（编程题给代码，数学题分步推导）
3. 将解答写入 _解答.md，代码文件单独保存
4. 输出 SUMMARY
- 将解答保存为 _解答.md
- 代码单独保存为 .py 等文件
- 如果是 LaTeX 公式，在解答中正确排版"""

    if brief:
        prompt += "\n注意：只要摘要，不要完整解答。"

    try:
        result = subprocess.run(
            [_CLAUDE_BIN, "-p", prompt, "--add-dir", str(hw_dir)],
            cwd=str(hw_dir), capture_output=True, text=True, timeout=300,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and not output:
            print(f"[homework] Claude Code 失败 ({result.returncode}), 回退 API")
            print(f"  stderr: {result.stderr[:200]}")
            return solve_homework(course, aname, content, brief=brief)

        # 提取 SUMMARY 部分作为飞书回复
        summary_marker = "SUMMARY:"
        if summary_marker in output:
            idx = output.index(summary_marker)
            summary = output[idx + len(summary_marker):].strip()[:500]
            return summary + f"\n\n完整解答已保存到 {hw_dir}"
        # 没有 SUMMARY 标记，返回最后 500 字
        return output[-500:] + f"\n\n完整解答已保存到 {hw_dir}"
    except subprocess.TimeoutExpired:
        print("[homework] Claude Code 超时，回退 API")
        return solve_homework(course, aname, content, brief=brief)
    except Exception as e:
        print(f"[homework] Claude Code 异常: {e}, 回退 API")
        return solve_homework(course, aname, content, brief=brief)


def _download_and_analyze_one(d: dict, idx: int, brief: bool = False) -> str:
    """下载并解答单个作业。brief=True 仅返回摘要。"""
    course = d.get("course", "未知课程")
    aname = d.get("name", "未知作业")
    due = d.get("due")
    due_str = due.strftime("%m月%d日 %H:%M") if due and hasattr(due, 'strftime') else str(due or "?")
    from datetime import datetime, timezone, timedelta
    CST = timezone(timedelta(hours=8))
    days_left = (due - datetime.now(CST)).days if due else "?"
    remaining = f"{days_left} 天" if isinstance(days_left, int) else "?"

    safe_course = re.sub(r'[\\/*?:"<>|]', '_', course)
    safe_name = re.sub(r'[\\/*?:"<>|]', '_', aname)
    hw_dir = ASSIGNMENTS_DIR / safe_course / safe_name

    # 下载
    try:
        import ddl_checker as dc
        cfg = dc.load_config()
        dc.download_assignments(
            cfg,  # 必需的第一个参数
            course_filter=course, assignment_filter=aname,
            output_dir=str(ASSIGNMENTS_DIR), due_within_days=3650,
            include_past=True,  # 允许下载历史作业
        )
    except Exception as e:
        print(f"[homework] 下载失败 {course}/{aname}: {e}")

    content = read_assignment_content(hw_dir)
    if "[无可读文件]" in content:
        return (
            f"[{idx}] {course} — {aname}\n"
            f"截止：{due_str}（{remaining}）\n"
            f"{content}"
        )

    print(f"[homework] 解题: {course} - {aname}")
    solution = _claude_code_solve(hw_dir, course, aname, content, brief=brief)

    # 生成解答文件
    title = f"{course} — {aname}"
    try:
        files = generate_solution_files(title, solution, hw_dir)
        print(f"[homework] 已生成 {len(files)} 个文件: {files}")
    except Exception as e:
        print(f"[homework] 文件生成失败: {e}")

    # 飞书回复：显示前 600 字，提示完整文件路径
    preview = solution[:600]
    if len(solution) > 600:
        preview += f"\n\n…（完整解答共 {len(solution)} 字，已保存到电脑）\n{safe_course}/{safe_name}/"

    return (
        f"[{idx}] {course} — {aname}\n"
        f"截止：{due_str}（{remaining}）\n\n"
        f"{preview}"
    )


def _format_list(pending: list[dict]) -> str:
    """格式化作业列表。"""
    if not pending:
        return "[homework] 暂无 Canvas 作业"
    lines = [f"共 {len(pending)} 个作业："]
    from datetime import datetime, timezone, timedelta
    CST = timezone(timedelta(hours=8))
    for i, d in enumerate(pending):
        course = d.get("course", "未知课程")
        aname = d.get("name", "未知作业")
        due = d.get("due")
        due_str = due.strftime("%m/%d") if due and hasattr(due, 'strftime') else str(due or "?")
        days = (due - datetime.now(CST)).days if due else "?"
        lines.append(f"  [{i}] {course} — {aname}（{due_str}，{days} 天）")
    lines.append("\n/hw do <序号> 下载分析")
    return "\n".join(lines)


def run_homework_check(due_within_days: int = 0, specific_idx: int | None = None,
                       list_only: bool = False, brief: bool = False,
                       include_past: bool = False) -> str:
    """主入口：列出或分析 Canvas 作业。include_past=True 时包含历史作业。"""
    pending = _fetch_pending(include_past=include_past)
    if due_within_days > 0:
        pending = _filter_by_due(pending, due_within_days)
        print(f"[homework] 过滤后 {len(pending)} 个 {due_within_days} 天内到期")

    if not pending:
        label = f"{due_within_days} 天内" if due_within_days > 0 else ""
        return f"[homework] 暂无{label}未提交的 Canvas 作业"

    # 仅列出
    if list_only:
        return _format_list(pending)

    # 分析指定作业
    if specific_idx is not None:
        if 0 <= specific_idx < len(pending):
            return _download_and_analyze_one(pending[specific_idx], specific_idx, brief=brief)
        return f"[homework] 无效序号：{specific_idx}，共 {len(pending)} 个（0~{len(pending)-1}）"

    # 默认：列出
    return _format_list(pending)


def run_homework_check_and_push(due_within_days: int = 3,
                                 specific_idx: int | None = None) -> None:
    """运行作业检查并通过飞书推送结果。"""
    result = run_homework_check(due_within_days, specific_idx)
    cfg = _get_feishu_config()
    if not cfg:
        print("[homework] 飞书未配置，仅打印：\n" + result)
        return

    import requests
    try:
        r = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": cfg["feishu_app_id"], "app_secret": cfg["feishu_app_secret"]},
            timeout=10,
        )
        if r.status_code != 200 or r.json().get("code") != 0:
            print(f"[homework] 飞书 token 获取失败")
            return
        token = r.json()["tenant_access_token"]

        chunks = [result[i:i + 3800] for i in range(0, len(result), 3800)]
        for chunk in chunks:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                params={"receive_id_type": "open_id"},
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": cfg["feishu_open_id"],
                    "msg_type": "text",
                    "content": json.dumps({"text": chunk}, ensure_ascii=False),
                },
                timeout=15,
            )
            if resp.status_code != 200 or resp.json().get("code") != 0:
                print(f"[homework] 推送失败: {resp.text[:100]}")
                return
        print("[homework] 飞书推送完成")
    except Exception as e:
        print(f"[homework] 推送异常: {e}")
