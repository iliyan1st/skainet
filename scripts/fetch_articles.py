"""
SKAINET – RSS Fetcher & Translator
Fetches AI/tech news from RSS feeds, translates to Bulgarian with DeepL Free,
outputs articles.json for the frontend.
"""

import feedparser
import requests
import json
import os
import hashlib
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# ── RSS FEEDS ────────────────────────────────────────────────────────────────
FEEDS = [
    {"url": "https://huggingface.co/blog/feed.xml",                        "source": "HuggingFace"},
    {"url": "https://venturebeat.com/category/ai/feed/",                   "source": "VentureBeat"},
    {"url": "https://techcrunch.com/category/artificial-intelligence/feed/","source": "TechCrunch"},
    {"url": "https://www.artificialintelligence-news.com/feed/",           "source": "AI News"},
    {"url": "https://www.technologyreview.com/feed/",                      "source": "MIT Tech Review"},
    {"url": "https://blog.google/technology/ai/rss/",                      "source": "Google AI"},
]

# ── KEYWORDS to keep an article ───────────────────────────────────────────
KEYWORDS = [
    "model", "gpt", "claude", "gemini", "llm", "ai ", " ai", "neural",
    "machine learning", "deep learning", "chatgpt", "openai", "anthropic",
    "google deepmind", "mistral", "release", "launch", "benchmark",
    "multimodal", "agent", "automation", "robot", "chip", "nvidia",
    "language model", "generative", "transformer", "diffusion",
]

# ── CATEGORY DETECTION ───────────────────────────────────────────────────
CATEGORY_RULES = [
    (["robot", "drone", "chip", "nvidia", "hardware", "tesla", "self-driving", "autonomous", "quantum"], "ТЕХНОЛОГИИ", "tag-tech"),
    (["tool", "app", "productivity", "free", "plugin", "workflow", "software"], "ИНСТРУМЕНТИ", "tag-tools"),
    (["study", "analysis", "report", "research", "survey", "compare", "versus", "vs "], "АНАЛИЗИ", "tag-analysis"),
    (["startup", "funding", "europe", "regulation", "policy", "gdpr", "eu ai"], "AI В БЪЛГАРИЯ", "tag-bg"),
    (["tutorial", "guide", "course", "learn", "how to", "prompt"], "НАУЧИ СЕ", "tag-learn"),
]

# ── DEEPL FREE ────────────────────────────────────────────────────────────
DEEPL_URL = "https://api-free.deepl.com/v2/translate"

def deepl_translate(texts: list[str], api_key: str) -> list[str]:
    """Translate a batch of texts to Bulgarian using DeepL Free API."""
    if not texts:
        return []
    try:
        resp = requests.post(
            DEEPL_URL,
            headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
            json={"text": texts, "target_lang": "BG", "source_lang": "EN"},
            timeout=30,
        )
        resp.raise_for_status()
        return [t["text"] for t in resp.json()["translations"]]
    except Exception as e:
        print(f"  DeepL error: {e}")
        return texts  # return originals on failure

# ── HELPERS ────────────────────────────────────────────────────────────────
def clean_html(text: str) -> str:
    """Strip HTML tags and extra whitespace."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text

def is_relevant(title: str, summary: str) -> bool:
    combined = (title + " " + summary).lower()
    return any(kw in combined for kw in KEYWORDS)

def detect_category(title: str, summary: str):
    combined = (title + " " + summary).lower()
    for keywords, category, tag in CATEGORY_RULES:
        if any(kw in combined for kw in keywords):
            return category, tag
    return "AI НОВИНИ", "tag-ai"

def parse_date(entry) -> tuple[str, str]:
    """Return (iso_string, formatted_bg_date)."""
    BG_MONTHS = ["", "яну", "фев", "мар", "апр", "май", "юни",
                 "юли", "авг", "сеп", "окт", "ное", "дек"]
    try:
        dt = None
        for attr in ("published", "updated", "created"):
            raw = getattr(entry, attr, None)
            if raw:
                try:
                    dt = parsedate_to_datetime(raw)
                    break
                except Exception:
                    pass
        if dt is None:
            dt = datetime.now(timezone.utc)
        iso = dt.astimezone(timezone.utc).isoformat()
        bg = f"{dt.day} {BG_MONTHS[dt.month]} {dt.year}"
        return iso, bg
    except Exception:
        now = datetime.now(timezone.utc)
        return now.isoformat(), "днес"

def article_image(entry, title: str) -> str:
    """Try to get image from feed, fall back to deterministic picsum."""
    # media:content
    if hasattr(entry, "media_content") and entry.media_content:
        url = entry.media_content[0].get("url", "")
        if url.startswith("http"):
            return url
    # enclosures
    if hasattr(entry, "enclosures") and entry.enclosures:
        url = entry.enclosures[0].get("href", "")
        if url.startswith("http"):
            return url
    # og:image in summary
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', entry.get("summary", ""))
    if match:
        return match.group(1)
    # deterministic picsum fallback
    seed = hashlib.md5(title.encode()).hexdigest()[:10]
    return f"https://picsum.photos/seed/{seed}/600/340"

def make_id(title: str, source: str) -> str:
    raw = f"{source}-{title}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]

# ── MAIN ──────────────────────────────────────────────────────────────────
def main():
    deepl_key = os.environ.get("DEEPL_API_KEY", "")
    if not deepl_key:
        print("WARNING: DEEPL_API_KEY not set – articles will be in English.")

    collected = []

    for feed_cfg in FEEDS:
        print(f"Fetching {feed_cfg['source']} …")
        try:
            feed = feedparser.parse(feed_cfg["url"])
        except Exception as e:
            print(f"  Failed: {e}")
            continue

        count = 0
        for entry in feed.entries:
            if count >= 6:
                break

            title = clean_html(entry.get("title", ""))
            summary = clean_html(entry.get("summary", entry.get("description", "")))[:400]
            link = entry.get("link", "")

            if not title or not link:
                continue
            if not is_relevant(title, summary):
                continue

            iso_date, bg_date = parse_date(entry)
            category, tag = detect_category(title, summary)
            image = article_image(entry, title)
            art_id = make_id(title, feed_cfg["source"])

            collected.append({
                "id":            art_id,
                "title_en":      title,
                "excerpt_en":    summary,
                "title":         title,      # will be replaced after translation
                "excerpt":       summary,    # will be replaced after translation
                "url":           link,
                "source":        feed_cfg["source"],
                "category":      category,
                "tag":           tag,
                "date":          bg_date,
                "published_iso": iso_date,
                "image":         image,
            })
            count += 1

        print(f"  → {count} relevant articles")
        time.sleep(0.5)   # polite crawling

    # Sort by date descending
    collected.sort(key=lambda a: a["published_iso"], reverse=True)
    # Keep top 30
    collected = collected[:30]

    # ── TRANSLATE titles + excerpts in batch ──
    if deepl_key and collected:
        print(f"\nTranslating {len(collected)} articles with DeepL …")
        titles  = [a["title_en"]  for a in collected]
        excerpts = [a["excerpt_en"][:200] for a in collected]

        translated_titles  = deepl_translate(titles, deepl_key)
        time.sleep(1)
        translated_excerpts = deepl_translate(excerpts, deepl_key)

        for i, art in enumerate(collected):
            art["title"]   = translated_titles[i]
            art["excerpt"] = translated_excerpts[i]
        print("Translation done.")
    else:
        print("Skipping translation (no API key).")

    # ── Clean up internal fields ──
    for art in collected:
        del art["title_en"]
        del art["excerpt_en"]

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(collected),
        "articles": collected,
    }

    out_path = os.path.join(os.path.dirname(__file__), "..", "articles.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Saved {len(collected)} articles to articles.json")

if __name__ == "__main__":
    main()
