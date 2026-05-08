#!/usr/bin/env python3
"""
news_digest.py — 智能新闻日报定时任务

运行方式：
  python news_digest.py              # 立即生成并推送
  python news_digest.py --dry-run    # 只生成不推送（调试用）
  python news_digest.py --test jwc   # 只测试单个信息源
  python news_digest.py --no-llm     # 跳过 LLM 排序（纯关键词）

launchd 定时调度（每天 10:00）：
  sjtu-agent install-daemons 后自动配置
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from sjtu_agent.paths import ENV_PATH
load_dotenv(ENV_PATH)


def main():
    parser = argparse.ArgumentParser(description="SJTU 智能新闻日报")
    parser.add_argument("--dry-run", action="store_true", help="只生成不推送")
    parser.add_argument("--test", metavar="SOURCE", help="只测试指定信息源（jwc/shuiyuan/official/canvas）")
    parser.add_argument("--no-llm", action="store_true", help="跳过 LLM 排序")
    parser.add_argument("--hours", type=int, default=24, help="采集最近 N 小时（默认 24）")
    parser.add_argument("--top-k", type=int, default=8, help="最多推送 N 条（默认 8）")
    args = parser.parse_args()

    # 单源测试模式
    if args.test:
        _test_source(args.test)
        return

    # 初始化 LLM 客户端
    llm_client = None
    model = ""
    if not args.no_llm:
        try:
            from sjtu_agent.agent.chat_loop import load_agent_config
            from sjtu_agent.agent.runner import _make_client
            cfg = load_agent_config()
            if cfg.get("api_key") and cfg.get("model"):
                llm_client = _make_client(cfg)
                model = cfg["model"]
                print(f"[news] LLM 已就绪：{model}", flush=True)
        except Exception as e:
            print(f"[news] LLM 初始化失败，降级到关键词排序：{e}", flush=True)

    from sjtu_agent.news_aggregator import NewsAggregator
    aggregator = NewsAggregator(llm_client=llm_client, model=model)
    md_digest, html_digest = aggregator.run(hours=args.hours, top_k=args.top_k)

    print("\n" + "=" * 60)
    print(md_digest)
    print("=" * 60 + "\n")

    if not args.dry_run:
        ok = aggregator.send_via_telegram(html_digest)
        if ok:
            print("[news] ✅ Telegram 推送成功", flush=True)
        else:
            print("[news] ⚠ Telegram 推送失败或未配置", flush=True)

        ok_wx = aggregator.send_via_wechat(md_digest)
        if ok_wx:
            print("[news] ✅ 微信推送成功", flush=True)
        else:
            print("[news] ⚠ 微信推送失败或未配置", flush=True)
    else:
        print("[news] --dry-run 模式，跳过推送", flush=True)


def _test_source(source_name: str):
    """测试单个信息源。"""
    source_map = {
        "jwc":      lambda: __import__("sjtu_agent.news_aggregator.sources.jwc", fromlist=["JwcSource"]).JwcSource(),
        "shuiyuan": lambda: __import__("sjtu_agent.news_aggregator.sources.shuiyuan", fromlist=["ShuiyuanSource"]).ShuiyuanSource(),
        "official": lambda: __import__("sjtu_agent.news_aggregator.sources.official", fromlist=["OfficialSource"]).OfficialSource(),
        "canvas":   lambda: __import__("sjtu_agent.news_aggregator.sources.canvas", fromlist=["CanvasSource"]).CanvasSource(),
    }
    if source_name not in source_map:
        print(f"未知信息源：{source_name}，可选：{list(source_map.keys())}")
        return

    print(f"[test] 测试信息源：{source_name}", flush=True)
    source = source_map[source_name]()
    items = source.fetch_recent(hours=48)  # 测试时用 48 小时
    print(f"[test] 采集到 {len(items)} 条", flush=True)
    for item in items[:5]:
        print(f"  - [{item.source}] {item.title[:60]}")
        print(f"    {item.url}")
        print(f"    发布：{item.published_at.strftime('%Y-%m-%d %H:%M')}，{item.age_hours():.1f}小时前")
        if item.summary:
            print(f"    摘要：{item.summary[:80]}")
        print()


if __name__ == "__main__":
    main()
