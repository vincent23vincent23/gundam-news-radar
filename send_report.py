#!/usr/bin/env python3
"""
把日報送到 Discord（Discord 一則訊息上限 2000 字，所以自動切段）
執行：python3 send_report.py 日報/2026-11-20.md
"""

import sys
import time
from pathlib import Path

from collect import discord, webhook_url


def chunks(text, limit=1900):
    """盡量在「段落標題」或換行處切開。"""
    parts, buf = [], ""
    for line in text.splitlines(keepends=True):
        if buf and (len(buf) + len(line) > limit or (line.startswith("## ") and len(buf) > limit * 0.6)):
            parts.append(buf)
            buf = ""
        buf += line
    if buf.strip():
        parts.append(buf)
    return parts


def main():
    if len(sys.argv) < 2:
        sys.exit("用法：python3 send_report.py 日報/YYYY-MM-DD.md")
    if not webhook_url():
        sys.exit("沒有設定 Discord 網址（discord_webhook.txt），略過推送")
    text = Path(sys.argv[1]).read_text()
    for part in chunks(text):
        discord(part)
        time.sleep(1)  # 避免太快被 Discord 擋
    print(f"已推送 {sys.argv[1]} 到 Discord")


if __name__ == "__main__":
    main()
