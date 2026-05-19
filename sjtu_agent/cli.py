from __future__ import annotations

import argparse
import json
import runpy
import sys
from pathlib import Path

from sjtu_agent import __version__
from sjtu_agent.paths import describe_runtime_paths
from sjtu_agent.scheduler import available_service_names, current_platform_name, install_daemons
from sjtu_agent.setup_wizard import register_setup_parser
from sjtu_agent.terminal_ui import print_json


def _run_module(module_name: str, script_args: list[str] | None = None) -> int:
    old_argv = sys.argv[:]
    sys.argv = [module_name, *(script_args or [])]
    try:
        runpy.run_module(module_name, run_name="__main__")
        return 0
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 0
    finally:
        sys.argv = old_argv


def _cmd_doctor(_: argparse.Namespace) -> int:
    import agent

    payload = {
        "version": __version__,
        "paths": describe_runtime_paths(),
        "setup": agent.tool_check_setup(),
    }
    print_json(payload)
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    """拉取最新代码并重装包，可选更新 Playwright Chromium。"""
    import subprocess
    import shutil
    from sjtu_agent.paths import PROJECT_ROOT

    pip = Path(sys.executable).parent / "pip"
    if not pip.exists():
        pip = Path(sys.executable).parent / "pip3"

    print(f"[sjtu-agent update] 当前版本：{__version__}")
    print(f"[sjtu-agent update] 项目目录：{PROJECT_ROOT}")

    # ── 1. git pull ────────────────────────────────────────────────────────
    if not args.skip_git:
        git = shutil.which("git")
        if not git:
            print("[sjtu-agent update] ⚠️  未找到 git，跳过代码更新")
        else:
            print("[sjtu-agent update] 正在 git pull…")
            result = subprocess.run(
                [git, "pull", "--ff-only"],
                cwd=str(PROJECT_ROOT),
                capture_output=False,
            )
            if result.returncode != 0:
                print("[sjtu-agent update] ⚠️  git pull 失败，请手动解决冲突后重试")
                return 1

    # ── 2. pip install -e . ────────────────────────────────────────────────
    print("[sjtu-agent update] 正在重新安装依赖…")
    pip_cmd = [sys.executable, "-m", "pip", "install", "-e", str(PROJECT_ROOT), "--quiet"]
    if args.upgrade_deps:
        pip_cmd.append("--upgrade")
    result = subprocess.run(pip_cmd)
    if result.returncode != 0:
        print("[sjtu-agent update] ⚠️  依赖安装失败")
        return 1

    # ── 3. 可选：更新 Playwright Chromium ─────────────────────────────────
    if args.update_playwright:
        print("[sjtu-agent update] 正在更新 Playwright Chromium…")
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])

    # ── 3b. Windows 上写兜底 .pth 确保 editable install 路径不丢失 ────
    if sys.platform == "win32":
        try:
            import site as _site
            sp_dirs = _site.getsitepackages()
            if sp_dirs:
                pth_path = Path(sp_dirs[0]) / "sjtu_agent_editable_path.pth"
                pth_path.write_text(str(PROJECT_ROOT) + "\n", encoding="utf-8")
                print(f"[sjtu-agent update] 已刷新 .pth 文件：{pth_path}")
        except Exception as _e:
            print(f"[sjtu-agent update] （写 .pth 失败，非致命：{_e}）")

    # ── 4. 打印新版本 ────────────────────────────────────────────────────
    # 重新导入以获取更新后的版本号
    try:
        import importlib
        import sjtu_agent as _pkg
        importlib.reload(_pkg)
        new_version = _pkg.__version__
    except Exception:
        new_version = "（重新打开终端后生效）"
    print(f"[sjtu-agent update] ✅ 更新完成！新版本：{new_version}")
    return 0


def _cmd_chat(args: argparse.Namespace) -> int:
    return _run_module("agent", args.script_args)


def _cmd_setup_config(args: argparse.Namespace) -> int:
    return _run_module("setup_config", args.script_args)


def _cmd_login(args: argparse.Namespace) -> int:
    return _run_module("login", args.script_args)


def _cmd_ddl(args: argparse.Namespace) -> int:
    return _run_module("ddl_checker", args.script_args)


def _cmd_daily_report(args: argparse.Namespace) -> int:
    return _run_module("daily_report", args.script_args)


def _cmd_telegram_bot(args: argparse.Namespace) -> int:
    return _run_module("telegram_bot", args.script_args)


def _cmd_feishu_bot(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parent.parent
    script = root / "feishu_bot.py"
    old_argv = sys.argv[:]
    sys.argv = [str(script), *(args.script_args or [])]
    try:
        runpy.run_path(str(script), run_name="__main__")
        return 0
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 0
    finally:
        sys.argv = old_argv


def _cmd_remind_check(args: argparse.Namespace) -> int:
    return _run_module("remind_check", args.script_args)


def _cmd_news_digest(args: argparse.Namespace) -> int:
    """运行智能新闻日报（采集 + 排序 + 推送）。"""
    root = Path(__file__).resolve().parent.parent
    script = root / "news_digest.py"
    old_argv = sys.argv[:]
    sys.argv = [str(script), *(args.script_args or [])]
    try:
        import runpy
        runpy.run_path(str(script), run_name="__main__")
    finally:
        sys.argv = old_argv
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    return _run_module("mcp_server", args.script_args)


def _cmd_web(args: argparse.Namespace) -> int:
    from sjtu_agent.web.server import start
    start(port=args.port, open_browser=not args.no_browser)
    return 0


def _cmd_wechat_bot(args: argparse.Namespace) -> int:
    # wechat_bot.py 位于项目根目录，用 run_path 直接执行脚本文件
    root = Path(__file__).resolve().parent.parent
    script = root / "wechat_bot.py"
    old_argv = sys.argv[:]
    sys.argv = [str(script), *(args.script_args or [])]
    try:
        runpy.run_path(str(script), run_name="__main__")
        return 0
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 0
    finally:
        sys.argv = old_argv


def _parse_hhmm(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("time must be in HH:MM format") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise argparse.ArgumentTypeError("time must be in HH:MM format")
    return hour, minute


def _cmd_install_daemons(args: argparse.Namespace) -> int:
    try:
        # 构建平台专属参数（macOS 支持自定义 output_dir 和 telegram_throttle）
        platform_kwargs: dict = {}
        if hasattr(args, "output_dir") and args.output_dir:
            platform_kwargs["output_dir"] = Path(args.output_dir)
        if hasattr(args, "telegram_throttle"):
            platform_kwargs["telegram_throttle"] = args.telegram_throttle

        payload = install_daemons(
            service_names=tuple(args.services) if args.services else None,
            python_executable=Path(args.python_executable),
            daily_report_time=args.daily_report_time,
            remind_interval=args.remind_interval,
            load=not args.write_only,
            **platform_kwargs,
        )
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print_json(payload)
    if not args.write_only:
        import time
        import webbrowser
        import urllib.request
        url = "http://127.0.0.1:7860"
        # Poll until web service is up (max 15s) instead of a fixed sleep
        for _ in range(15):
            try:
                urllib.request.urlopen(url + "/api/status", timeout=1)
                break
            except Exception:
                time.sleep(1)
        webbrowser.open(url)
    return 0


def _add_passthrough_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    help_text: str,
    handler,
) -> None:
    parser = subparsers.add_parser(name, help=help_text)
    parser.add_argument("script_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    parser.set_defaults(func=handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sjtu-agent", description="Deployable CLI for SJTU Agent.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    register_setup_parser(subparsers, _parse_hhmm)

    _add_passthrough_parser(subparsers, "chat", "start interactive chat mode", _cmd_chat)
    _add_passthrough_parser(subparsers, "setup-config", "build config.json from browser cookies", _cmd_setup_config)
    _add_passthrough_parser(subparsers, "login", "refresh platform cookies with Playwright", _cmd_login)
    _add_passthrough_parser(subparsers, "ddl", "run the DDL checker report", _cmd_ddl)
    _add_passthrough_parser(subparsers, "daily-report", "generate or send the daily report", _cmd_daily_report)
    _add_passthrough_parser(subparsers, "telegram-bot", "start the Telegram bot", _cmd_telegram_bot)
    _add_passthrough_parser(subparsers, "feishu-bot", "start the Feishu (Lark) bot (long connection)", _cmd_feishu_bot)
    _add_passthrough_parser(subparsers, "remind-check", "run the reminder daemon once", _cmd_remind_check)
    _add_passthrough_parser(subparsers, "news-digest", "run the smart news digest (collect + rank + push)", _cmd_news_digest)
    _add_passthrough_parser(subparsers, "mcp", "start the MCP server", _cmd_mcp)
    _add_passthrough_parser(subparsers, "wechat-bot", "start the WeChat ilink bot (long-polling)", _cmd_wechat_bot)

    web_parser = subparsers.add_parser("web", help="open the local web configuration UI in your browser")
    web_parser.add_argument(
        "--port",
        type=int,
        default=7860,
        help="port to listen on (default: 7860)",
    )
    web_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="start the server without opening the browser automatically",
    )
    web_parser.set_defaults(func=_cmd_web)

    _platform_name = current_platform_name()
    install_daemons_parser = subparsers.add_parser(
        "install-daemons",
        help=f"install background services for the current platform ({_platform_name})",
    )
    install_daemons_parser.add_argument(
        "--output-dir",
        default=None,
        help="(macOS/Linux) directory where service files will be written (default: platform standard path)",
    )
    install_daemons_parser.add_argument(
        "--write-only",
        action="store_true",
        help="only write service files; do not load/register them",
    )
    install_daemons_parser.add_argument(
        "--python-executable",
        default=sys.executable,
        help="python executable that background services should use",
    )
    install_daemons_parser.add_argument(
        "--services",
        nargs="+",
        choices=available_service_names(),
        help="subset of services to install",
    )
    install_daemons_parser.add_argument(
        "--daily-report-time",
        type=_parse_hhmm,
        default=(22, 0),
        help="daily report schedule in HH:MM, default 22:00",
    )
    install_daemons_parser.add_argument(
        "--remind-interval",
        type=int,
        default=60,
        help="reminder daemon interval in seconds, default 60",
    )
    install_daemons_parser.add_argument(
        "--telegram-throttle",
        type=int,
        default=10,
        help="(macOS) launchd throttle interval for telegram bot restarts, default 10",
    )
    install_daemons_parser.set_defaults(func=_cmd_install_daemons)

    doctor = subparsers.add_parser("doctor", help="print runtime paths and setup status")
    doctor.set_defaults(func=_cmd_doctor)

    update_parser = subparsers.add_parser(
        "update",
        help="pull latest code from git and reinstall the package",
    )
    update_parser.add_argument(
        "--skip-git",
        action="store_true",
        help="skip git pull (only reinstall dependencies)",
    )
    update_parser.add_argument(
        "--upgrade-deps",
        action="store_true",
        help="also upgrade all Python dependencies to their latest versions",
    )
    update_parser.add_argument(
        "--update-playwright",
        action="store_true",
        help="also update Playwright Chromium browser",
    )
    update_parser.set_defaults(func=_cmd_update)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, unknown = parser.parse_known_args(argv)
    if not getattr(args, "command", None):
        return _cmd_chat(argparse.Namespace(script_args=[]))
    if unknown:
        if hasattr(args, "script_args"):
            args.script_args = list(getattr(args, "script_args", [])) + unknown
        else:
            parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
