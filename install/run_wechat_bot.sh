#!/bin/bash
# run_wechat_bot.sh — 启动微信 ilink Bot
# 由 LaunchAgent com.sjtu.wechat-bot 调用

set -e
cd "$(dirname "$0")/.."

# 激活虚拟环境（若存在）
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

exec python3 scripts/wechat_bot.py
