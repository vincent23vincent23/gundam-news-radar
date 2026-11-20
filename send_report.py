#!/usr/bin/env python3
"""
把日報／週報推到 Discord
==========================
  python3 send_report.py 日報/2026-11-20.json   ← 帶圖卡片版（推薦）
  python3 send_report.py 日報/2026-11-20.md     ← 純文字版（舊格式）

帶圖卡片版：
  - S 級：大圖卡片
  - A 級：右側小縮圖卡片
  - B 級：文字清單
  代表圖是程式自己去每篇文章抓 og:image，不花 AI 額度。
"""

import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from collect import webhook_url

COLORS = {"S": 0xE53935, "A": 0xFB8C00, "B": 0x9E9E9E}
BUCKET_ICON = {"新品": "🔥", "PB再販價格": "💰", "本週發售": "🔍", "動畫展覽": "📺", "快訊": "⚡"}
GENERIC_IMAGES = ["img_ogp", "/common/", "logo", "noimage"]   # 網站共用圖，抓到也不要


# ── Discord ─────────────────────────────────────────────

def post(payload):
    url = webhook_url()
    data = json.dumps(payload).encode()
    for _ in range(3):
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "gundam-weekly"})
        try:
            urllib.request.urlopen(req, timeout=20)
            time.sleep(1)  # 放慢速度，避免被 Discord 限流
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429:  # 太快了，照 Discord 說的秒數等一下
                time.sleep(float(json.loads(e.read() or b"{}").get("retry_after", 2)) + 0.5)
                continue
            print(f"Discord 錯誤 {e.code}：{e.read()[:200]}", file=sys.stderr)
            return False
        except Exception as e:
            print(f"Discord 錯誤：{e}", file=sys.stderr)
            return False
    return False


def post_text(text, limit=1900):
    """長文字自動切段送出。"""
    buf = ""
    for line in text.splitlines(keepends=True):
        if buf and len(buf) + len(line) > limit:
            post({"content": buf})
            buf = ""
        buf += line
    if buf.strip():
        post({"content": buf})


# ── 抓代表圖 ─────────────────────────────────────────────

def og_image(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=10).read(300_000).decode("utf-8", "replace")
    except Exception:
        return None
    m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html)
         or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', html))
    if not m:
        return None
    img = m.group(1).replace("&amp;", "&")
    return None if any(g in img for g in GENERIC_IMAGES) else img


# ── 卡片版 ───────────────────────────────────────────────

def send_json(path):
    r = json.loads(Path(path).read_text())
    items = r.get("items", [])
    cards = [i for i in items if i.get("level") in ("S", "A") and i.get("url")]
    with ThreadPoolExecutor(8) as pool:
        images = list(pool.map(og_image, [c["url"] for c in cards]))

    # 1. 標題
    head = f"# 🤖 {r.get('title', '')}\n**{r.get('summary', '')}**"
    if r.get("headlines"):
        head += "\n\n🎯 **頭條候選**\n" + "\n".join(f"{n}. {h}" for n, h in enumerate(r["headlines"], 1))
    post_text(head)

    # 2. S／A 卡片，依 bucket 分組；每則訊息最多 10 張卡、6000 字
    for bucket, icon in BUCKET_ICON.items():
        group = [(c, img) for c, img in zip(cards, images) if c.get("bucket") == bucket]
        if not group:
            continue
        embeds, size, header = [], 0, f"{icon} **{bucket}**"

        def flush():
            nonlocal embeds, size, header
            post({"content": header, "embeds": embeds} if header else {"embeds": embeds})
            embeds, size, header = [], 0, ""

        for c, img in group:
            e = {
                "title": f"[{c['level']}] {c['name']}"[:256],
                "url": c["url"],
                "description": "\n".join(x for x in (c.get("info"), c.get("note")) if x)[:1000],
                "color": COLORS[c["level"]],
            }
            if img:
                e["image" if c["level"] == "S" else "thumbnail"] = {"url": img}
            n = len(e["title"]) + len(e["description"])
            if embeds and (len(embeds) == 10 or size + n > 5500):
                flush()
            embeds.append(e)
            size += n
        flush()

    # 3. B 級快訊：文字清單
    b = [i for i in items if i.get("level") == "B"]
    if b:
        post_text("⚡ **B 級快訊**\n" + "\n".join(
            f"• {i['name']}｜{i.get('info', '')}" + (f"　<{i['url']}>" if i.get("url") else "") for i in b))

    # 4. 值得點開＋結尾
    tail = ""
    if r.get("links"):
        tail += "🔗 **值得點開**\n" + "\n".join(f"• {l['label']}　<{l['url']}>" for l in r["links"]) + "\n"
    if r.get("footer"):
        tail += f"-# {r['footer']}"
    if tail.strip():
        post_text(tail)
    print(f"已推送 {path}：卡片 {len(cards)} 張（有圖 {sum(1 for i in images if i)}）、快訊 {len(b)} 則")


def main():
    if len(sys.argv) < 2:
        sys.exit("用法：python3 send_report.py 日報/YYYY-MM-DD.json")
    if not webhook_url():
        sys.exit("沒有設定 Discord 網址（discord_webhook.txt），略過推送")
    path = sys.argv[1]
    if path.endswith(".json"):
        send_json(path)
    else:
        post_text(Path(path).read_text())
        print(f"已推送 {path}（純文字）")


if __name__ == "__main__":
    main()
