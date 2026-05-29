"""MATLAB discovery and batch runner for homework figure generation."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _find_matlab() -> str | None:
    """Locate MATLAB executable. Checks MATLAB_PATH, common paths, then PATH."""
    env_path = os.environ.get("MATLAB_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    if sys.platform == "win32":
        candidates = [
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                         "MATLAB", "*", "bin", "matlab.exe"),
        ]
        import glob as _glob
        for pattern in candidates:
            matches = sorted(_glob.glob(pattern), reverse=True)
            if matches:
                return matches[0]
        # fallback: common custom paths
        for drive in ["D:", "E:", "C:"]:
            for d in _glob.glob(f"{drive}\\MATLAB*"):
                exe = Path(d) / "bin" / "matlab.exe"
                if exe.exists():
                    return str(exe)
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/MATLAB_R2024a.app/bin/matlab",
            "/Applications/MATLAB_R2023b.app/bin/matlab",
            "/Applications/MATLAB_R2023a.app/bin/matlab",
        ]
        for c in candidates:
            if Path(c).exists():
                return c
    else:
        candidates = ["matlab"]

    for c in candidates:
        if Path(c).exists():
            return c

    found = shutil.which("matlab")
    if found:
        return found
    return None


MATLAB_BIN = _find_matlab()


def matlab_available() -> bool:
    """Check if MATLAB is installed and callable."""
    return MATLAB_BIN is not None and Path(MATLAB_BIN).exists()


def run_matlab(hw_dir: str | Path, script_name: str = "_figures.m",
               timeout: int = 180) -> str:
    """Run a MATLAB script in the homework directory, return combined stdout/stderr.

    Args:
        hw_dir: homework directory containing the .m script
        script_name: name of the .m file to run (default _figures.m)
        timeout: max wait in seconds (MATLAB startup takes ~5-10s)

    Returns:
        Combined stdout from MATLAB or error message.
    """
    if not matlab_available():
        return "[MATLAB] 未找到 MATLAB，请设置 MATLAB_PATH 环境变量"

    hw_dir = Path(hw_dir)
    m_file = hw_dir / script_name
    if not m_file.exists():
        return f"[MATLAB] 脚本不存在: {m_file}"

    # MATLAB -batch runs the command then exits
    # Need to cd to the directory first since latex files are there
    cmd = [
        MATLAB_BIN,
        "-batch",
        f"cd('{hw_dir}'); run('{script_name}'); exit",
    ]

    try:
        result = subprocess.run(
            cmd,
            cwd=str(hw_dir),
            capture_output=True, text=True,
            timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        output = result.stdout.strip()
        if result.returncode != 0 and not output:
            output = result.stderr.strip()
        return output or "[MATLAB] 运行完成（无输出）"
    except subprocess.TimeoutExpired:
        return f"[MATLAB] 超时（{timeout}s）"
    except Exception as e:
        return f"[MATLAB] 运行异常: {e}"
