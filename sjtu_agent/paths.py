from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

try:
    from platformdirs import user_data_dir
except ImportError:
    def user_data_dir(app_name: str, app_author: str | None = None) -> str:
        if os.name == "nt":
            base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        elif sys.platform == "darwin":
            base = str(Path.home() / "Library" / "Application Support")
        else:
            base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        if app_author:
            return str(Path(base) / app_author / app_name)
        return str(Path(base) / app_name)

APP_NAME = "sjtu-agent"
APP_AUTHOR = "sjtu"

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
DATA_DIR = Path(os.environ.get("SJTU_AGENT_HOME", user_data_dir(APP_NAME)))
LOG_DIR = DATA_DIR / "logs"

ENV_PATH = DATA_DIR / ".env"
CONFIG_PATH = DATA_DIR / "config.json"
AGENT_CONFIG_PATH = DATA_DIR / "agent_config.json"
REMINDERS_PATH = DATA_DIR / "reminders.json"
REMIND_STATE_PATH = DATA_DIR / "remind_state.json"
MYSJTU_CATALOG_PATH = DATA_DIR / "mysjtu_catalog.json"
SCHEDULE_CACHE_PATH = DATA_DIR / ".schedule_cache.json"

DAILY_REPORT_LOG_PATH = LOG_DIR / "daily_report.log"
REMIND_CHECK_LOG_PATH = LOG_DIR / "remind_check.log"
DDL_CACHE_PATH        = DATA_DIR / ".ddl_cache.json"
USER_PROFILE_PATH     = DATA_DIR / "user_profile.json"
CARE_STATE_PATH       = DATA_DIR / "care_state.json"


def atomic_write_json(path: Path, data, *, indent: int = 2) -> None:
    """原子写入 JSON：先写临时文件，再 os.replace 替换。

    崩溃/磁盘满/被 SIGKILL 时绝不会留下半截写入的状态文件，避免下次启动时
    误读成空对象触发"重发整批已发送提醒"事故。

    使用：sjtu_agent.paths.atomic_write_json(REMIND_STATE_PATH, state)
    """
    import json as _json
    import os as _os
    import tempfile as _tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = _tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            try:
                _os.fsync(f.fileno())  # 确保数据真的落盘
            except OSError:
                pass  # 某些文件系统/平台不支持 fsync
        _os.replace(tmp_path, path)
    except Exception:
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_json_safe(path: Path, default=None):
    """安全读取 JSON：文件不存在/损坏返回 default。

    与 atomic_write_json 配套使用，避免每个调用方都写 try/except。
    """
    import json as _json

    if default is None:
        default = {}
    if not path.exists():
        return default
    try:
        return _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError):
        return default


def _copy_if_missing(source: Path, target: Path) -> None:
    if target.exists() or not source.exists() or not source.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def ensure_runtime_layout() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    legacy_runtime_files = {
        ".env": PROJECT_ROOT / ".env",
        "config.json": PROJECT_ROOT / "config.json",
        "agent_config.json": PROJECT_ROOT / "agent_config.json",
        "reminders.json": PROJECT_ROOT / "reminders.json",
        "remind_state.json": PROJECT_ROOT / "remind_state.json",
        "mysjtu_catalog.json": PROJECT_ROOT / "mysjtu_catalog.json",
        ".schedule_cache.json": PROJECT_ROOT / ".schedule_cache.json",
    }

    old_fallback_dir = Path.home() / "Library" / "Application Support" / "sjtu" / APP_NAME
    for name in list(legacy_runtime_files):
        legacy_runtime_files.setdefault(f"old::{name}", old_fallback_dir / name)

    for name, source in legacy_runtime_files.items():
        target_name = name.split("::", 1)[-1]
        _copy_if_missing(source, DATA_DIR / target_name)


def describe_runtime_paths() -> dict[str, str]:
    return {
        "project_root": str(PROJECT_ROOT),
        "data_dir": str(DATA_DIR),
        "log_dir": str(LOG_DIR),
        "config_path": str(CONFIG_PATH),
        "env_path": str(ENV_PATH),
        "agent_config_path": str(AGENT_CONFIG_PATH),
        "reminders_path": str(REMINDERS_PATH),
        "mysjtu_catalog_path": str(MYSJTU_CATALOG_PATH),
        "schedule_cache_path": str(SCHEDULE_CACHE_PATH),
    }


ensure_runtime_layout()
