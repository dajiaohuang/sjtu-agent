#!/usr/bin/env python3
"""
setup_config.py — 自动从 Chrome 读取 cookie，生成 config.json

运行一次即可，之后直接用 ddl_checker.py。
需要 Chrome 处于关闭或后台状态（不需要完全退出，读取是只读操作）。
"""

import json
import sys
from sjtu_agent.paths import CONFIG_PATH

try:
    import browser_cookie3
except ImportError:
    print("[错误] 请先安装依赖：pip install browser-cookie3")
    sys.exit(1)

# ── 各平台需要的 cookie 名 ────────────────────────────────────────────────────

DOMAINS = {
    "aihaoke": {
        "domain": "sjtu.aihaoke.net",
        "keys": ["JSESSIONID", "think_sso_user_login", "SESSION", "PHPSESSID"],
    },
    # aihaoke 通过交大统一身份认证(SSO)登录，Playwright 需要带上 jaccount cookie 才能自动过 SSO
    "jaccount": {
        "domain": "jaccount.sjtu.edu.cn",
        "keys": ["JATrustCookie", "JAAuthCookie", "JSESSIONID"],
    },
    "phycai": {
        "domain": "www.phycai.sjtu.edu.cn",
        "keys": ["ASP.NET_SessionId", ".PhyEwsProj", "PhyEws_StuName", "PhyEws_StuType", ".ASPXAUTH"],
    },
    "icourse": {
        "domain": "icourse163.org",
        "keys": ["NTESSTUDYSI", "STUDY_LIVE_LOGIN_INFO", "ux", "p_h5_u", "videoStudyUrl"],
    },
}


def load_chrome_cookies(domain: str) -> dict[str, str]:
    """从 Chrome 本地数据库读取指定域名的所有 cookie。"""
    try:
        jar = browser_cookie3.chrome(domain_name=domain)
        return {c.name: c.value for c in jar}
    except Exception as e:
        print(f"  [警告] 读取 {domain} cookie 失败：{e}")
        return {}


def filter_cookies(all_cookies: dict, wanted_keys: list[str]) -> dict[str, str]:
    """只保留需要的 key，过滤掉空值。"""
    result = {}
    for key in wanted_keys:
        if key in all_cookies and all_cookies[key]:
            result[key] = all_cookies[key]
    # 如果指定 key 一个都没拿到，返回全部（让用户自己判断哪个有用）
    if not result and all_cookies:
        result = all_cookies
    return result


def build_config(existing: dict) -> dict:
    cfg = dict(existing)

    # 保留已有的 canvas_token（已在上一步生成）
    if not cfg.get("canvas_token") or cfg["canvas_token"].startswith("YOUR_"):
        cfg["canvas_token"] = "（请在 oc.sjtu.edu.cn → 账户设置 → 访问许可证 生成并填入）"

    cfg.setdefault("canvas_base_url", "https://oc.sjtu.edu.cn")

    for platform, info in DOMAINS.items():
        domain = info["domain"]
        print(f"[*] 读取 {domain} …")
        all_cookies = load_chrome_cookies(domain)
        if all_cookies:
            filtered = filter_cookies(all_cookies, info["keys"])
            cfg[f"{platform}_cookies"] = filtered
            print(f"    ✓ 获取到 {len(filtered)} 个 cookie：{list(filtered.keys())}")
        else:
            print(f"    ✗ 未找到 cookie（可能未登录或 Chrome 完全关闭）")
            cfg.setdefault(f"{platform}_cookies", {})

    return cfg


def main():
    # 读取现有 config（保留已有 token）
    existing = {}
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open(encoding="utf-8") as f:
            try:
                existing = json.load(f)
            except json.JSONDecodeError:
                pass

    print("正在从 Chrome 本地数据库读取 cookie …\n")
    cfg = build_config(existing)

    # 移除注释字段
    cfg.pop("_comment", None)

    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 已写入 {CONFIG_PATH}")
    print("\n当前配置：")
    # 打印时隐藏敏感值
    safe = {}
    for k, v in cfg.items():
        if isinstance(v, dict):
            safe[k] = {ck: cv[:6] + "…" if isinstance(cv, str) and len(cv) > 6 else cv
                       for ck, cv in v.items()}
        elif isinstance(v, str) and len(v) > 10 and "sjtu" not in v:
            safe[k] = v[:10] + "…"
        else:
            safe[k] = v
    print(json.dumps(safe, ensure_ascii=False, indent=2))
    print("\n现在可以运行：python ddl_checker.py")


if __name__ == "__main__":
    main()
