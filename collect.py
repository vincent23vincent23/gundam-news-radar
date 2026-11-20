#!/usr/bin/env python3
"""
鋼彈情報 巡邏程序（每 15 分鐘跑一次）
=====================================
做的事：
  1. 依序讀取 SOURCES 裡的 RSS
  2. 綜合模型站只留下含「鋼彈關鍵字」的文章，並丟掉 EXCLUDE 雜訊
  3. 跟 data/seen.json 比對，只留以前沒看過的
  4. 新情報「累加」到 raw/今天日期.md（台灣時間），隔天早上交給 AI 寫日報
  5. 符合 URGENT_RULES 的重大情報 → 立刻推播到 Discord

只用 Python 內建功能，不用安裝任何套件。
執行：python3 collect.py
Discord 網址：讀取環境變數 DISCORD_WEBHOOK_URL，或同資料夾的 discord_webhook.txt
            （兩個都沒有就只存檔、不推播）
"""

import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

# ── 設定區：要加減來源、關鍵字、重大規則，改這裡就好 ─────────────────

SOURCES = [
    # (名稱, RSS 網址, 是否需要用關鍵字過濾)
    ("早耳ガンプラ情報局", "https://hayamimi-gunpla.com/?xml", False),
    ("おきちゃんのガンプラ堂", "https://kaigoshinootakunaburogu.com/feed", False),
    ("THE GUNDAM BASE", "https://www.gundam-base.net/feed", False),
    ("HOBBY Watch", "https://hobby.watch.impress.co.jp/data/rss/1.0/hbw/feed.rdf", True),
    ("電撃ホビーウェブ", "https://hobby.dengeki.com/feed", True),
    ("fig速", "https://figsoku.net/feed", True),
    ("PTT Model", "https://www.ptt.cc/atom/Model.xml", True),
    ("PTT C_Chat", "https://www.ptt.cc/atom/C_Chat.xml", True),
]

# 綜合站只保留含這些字的文章（不分大小寫）
KEYWORDS = [
    "ガンダム", "ガンプラ", "GUNDAM", "ザク", "シャア", "ジオン",
    "METAL BUILD", "ROBOT魂", "MOBILE SUIT",
    "鋼彈", "鋼普拉", "高達", "薩克", "吉翁", "夏亞",
]
# 型號縮寫要「完整單字」才算（避免 jpg 被當成 PG）
GRADE_PATTERN = r"(?<![A-Za-z])(HG|HGUC|RG|MG|MGEX|MGSD|PG|EG)(?![A-Za-z])"

# 標題含這些字的直接丟掉（雜訊）。覺得漏掉重要的，就從這裡刪掉
EXCLUDE = ["在庫あり", "グミ", "ガンダムデカール", "アッセンブルカラー", "[徵求]", "[販售]", "[交換]", "[拍賣]", "[收購]"]

# 🚨 重大情報規則：標題「含 any 其中一個」且「不含 none 任何一個」→ 立刻推播
#    推太多就在 none 加字；漏掉就在 any 加字
URGENT_RULES = [
    {"label": "大型新品",
     "any": [r"(?<![A-Za-z])(MG|MGEX|PG|RG|PG UNLEASHED|METAL BUILD)(?![A-Za-z])"],
     "none": ["再販", "在庫", "デカール", "入荷", "予約受付中", "受注開始", "紹介", "COLLECTION", "レビュー", "[閒聊]", "[開箱]"]},
    {"label": "動畫／影像",
     "any": ["新作アニメ", "制作決定", "劇場版", "ティザー", "特報", "PV公開", "新プロジェクト", "新作發表"],
     "none": [],
     "need_gundam": True},
    {"label": "價格爭議",
     "any": ["価格改定", "値上げ", "漲價"],
     "none": ["参照", "まとめ"]},
]
MAX_PUSH_PER_RUN = 5   # 一次最多推幾則（避免第一次執行洗版）

LOOKBACK_DAYS = 2      # 只收最近幾天的文章（避免第一次執行塞爆）
SUMMARY_CHARS = 160    # 摘要最多幾個字（字越少，AI 越省額度）

# ── 以下是程式本體，一般不用改 ───────────────────────────────────

BASE = Path(__file__).resolve().parent
SEEN_FILE = BASE / "data" / "seen.json"
RAW_DIR = BASE / "raw"
TW = timezone(timedelta(hours=8))

NS = {
    "rss1": "http://purl.org/rss/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "atom": "http://www.w3.org/2005/Atom",
}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (gundam-weekly-rss)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def clean(text):
    """去掉 HTML 標籤、網址、多餘空白，並截短。"""
    text = unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:SUMMARY_CHARS] + ("…" if len(text) > SUMMARY_CHARS else "")


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)                              # RSS 2.0
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))  # ISO
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def text_of(el, *paths):
    for p in paths:
        found = el.find(p, NS)
        if found is not None and (found.text or found.get("href")):
            return found.text or found.get("href")
    return ""


def parse_feed(raw):
    """支援 RSS 2.0、RSS 1.0 (RDF)、Atom。回傳 [(標題, 連結, 日期, 摘要)]"""
    root = ET.fromstring(raw)
    items = []
    if root.tag == "rss":
        for it in root.iter("item"):
            items.append((text_of(it, "title"), text_of(it, "link"),
                          parse_date(text_of(it, "pubDate")), clean(text_of(it, "description"))))
    elif root.tag.endswith("RDF"):
        for it in root.findall("rss1:item", NS):
            items.append((text_of(it, "rss1:title"), text_of(it, "rss1:link"),
                          parse_date(text_of(it, "dc:date")), clean(text_of(it, "rss1:description"))))
    else:
        for it in root.findall("atom:entry", NS):
            link = it.find("atom:link", NS)
            items.append((text_of(it, "atom:title"), link.get("href") if link is not None else "",
                          parse_date(text_of(it, "atom:published", "atom:updated")),
                          clean(text_of(it, "atom:content", "atom:summary"))))
    return items


def is_gundam(text):
    return any(k.lower() in text.lower() for k in KEYWORDS) or re.search(GRADE_PATTERN, text)


def urgent_label(title, summary):
    """回傳命中的重大規則名稱；沒命中回傳 None。"""
    for rule in URGENT_RULES:
        if any(n in title for n in rule["none"]):
            continue
        if rule.get("need_gundam") and not is_gundam(f"{title} {summary}"):
            continue
        if any(re.search(p, title) for p in rule["any"]):
            return rule["label"]
    return None


def webhook_url():
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    f = BASE / "discord_webhook.txt"
    if not url and f.exists():
        url = f.read_text().strip()
    return url if url.startswith("https://") else ""


def discord(content):
    url = webhook_url()
    if not url:
        return False
    data = json.dumps({"content": content[:1990]}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "gundam-weekly"})
    try:
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception as e:
        print(f"Discord 推播失敗：{e}", file=sys.stderr)
        return False


def main():
    now = datetime.now(TW)
    cutoff = now - timedelta(days=LOOKBACK_DAYS)
    seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()

    new_lines, urgent, errors, titles = [], [], [], set()
    for name, url, need_filter in SOURCES:
        try:
            entries = parse_feed(fetch(url))
        except Exception as e:  # 單一網站壞掉不影響其他網站
            errors.append(f"{name}：{type(e).__name__}")
            continue
        for title, link, date, summary in entries:
            if not link or link in seen:
                continue
            if date and date < cutoff:
                continue
            seen.add(link)
            title = unescape(title.strip())
            key = re.sub(r"\[\d{2}/\d{2} [\d:]+\]$", "", title).strip()
            if key in titles or any(x in title for x in EXCLUDE):
                continue
            if need_filter and not is_gundam(f"{title} {summary}"):
                continue
            titles.add(key)
            label = urgent_label(title, summary)
            d = date.astimezone(TW).strftime("%m/%d %H:%M") if date else "?"
            mark = f"🚨{label} " if label else ""
            new_lines.append(f"- {mark}[{name}｜{d}] {title}\n  {link}\n  > {summary}")
            if label:
                urgent.append((label, name, title, link))

    # 新情報累加到今天的 raw 檔
    RAW_DIR.mkdir(exist_ok=True)
    raw = RAW_DIR / f"{now:%Y-%m-%d}.md"
    if new_lines:
        header = "" if raw.exists() else f"# 原始情報 {now:%Y-%m-%d}（台灣時間）\n"
        with raw.open("a") as f:
            f.write(header + f"\n## {now:%H:%M} 巡邏（新 {len(new_lines)} 則）\n" + "\n".join(new_lines) + "\n")

    # 重大情報推播
    pushed = 0
    for label, name, title, link in urgent[:MAX_PUSH_PER_RUN]:
        pushed += discord(f"🚨 **{label}**｜{name}\n{title}\n{link}")
    if len(urgent) > MAX_PUSH_PER_RUN:
        discord(f"……另外還有 {len(urgent) - MAX_PUSH_PER_RUN} 則重大情報，請看明天日報")

    SEEN_FILE.parent.mkdir(exist_ok=True)
    SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=0))

    print(f"{now:%H:%M} 新 {len(new_lines)} 則，重大 {len(urgent)} 則（已推播 {pushed}）→ {raw.name}")
    if errors:
        print("抓取失敗：" + "、".join(errors), file=sys.stderr)


if __name__ == "__main__":
    main()
