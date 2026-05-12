"""
SKAINET – RSS Fetcher, Scraper & Translator
Fetches AI/tech news, extracts full article text, translates to Bulgarian,
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

try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False
    print("trafilatura not installed – falling back to RSS excerpts")

# ── RSS FEEDS ────────────────────────────────────────────────────────────────
FEEDS = [
    {"url": "https://huggingface.co/blog/feed.xml",                        "source": "HuggingFace"},
    {"url": "https://venturebeat.com/category/ai/feed/",                   "source": "VentureBeat"},
    {"url": "https://techcrunch.com/category/artificial-intelligence/feed/","source": "TechCrunch"},
    {"url": "https://www.artificialintelligence-news.com/feed/",           "source": "AI News"},
    {"url": "https://www.technologyreview.com/feed/",                      "source": "MIT Tech Review"},
    {"url": "https://blog.google/technology/ai/rss/",                      "source": "Google AI"},
]

# ── KEYWORDS ─────────────────────────────────────────────────────────────────
KEYWORDS = [
    "model", "gpt", "claude", "gemini", "llm", "ai ", " ai", "neural",
    "machine learning", "deep learning", "chatgpt", "openai", "anthropic",
    "google deepmind", "mistral", "release", "launch", "benchmark",
    "multimodal", "agent", "automation", "robot", "chip", "nvidia",
    "language model", "generative", "transformer", "diffusion",
]

# ── CATEGORY DETECTION ───────────────────────────────────────────────────────
CATEGORY_RULES = [
    (["robot", "drone", "chip", "nvidia", "hardware", "tesla", "self-driving", "autonomous", "quantum"], "ТЕХНОЛОГИИ", "tag-tech"),
    (["tool", "app", "productivity", "free", "plugin", "workflow", "software"], "ИНСТРУМЕНТИ", "tag-tools"),
    (["study", "analysis", "report", "research", "survey", "compare", "versus", "vs "], "АНАЛИЗИ", "tag-analysis"),
    (["startup", "funding", "europe", "regulation", "policy", "gdpr", "eu ai"], "AI В БЪЛГАРИЯ", "tag-bg"),
    (["tutorial", "guide", "course", "learn", "how to", "prompt"], "НАУЧИ СЕ", "tag-learn"),
]

# ── GOOGLE TRANSLATE (free, no API key) ──────────────────────────────────────
GTRANS_URL = "https://translate.googleapis.com/translate_a/single"

def google_translate(texts: list[str]) -> list[str]:
    """Translate a list of short texts to Bulgarian."""
    if not texts:
        return []
    results = []
    for text in texts:
        if not text or not text.strip():
            results.append(text)
            continue
        try:
            resp = requests.get(
                GTRANS_URL,
                params={"client": "gtx", "sl": "en", "tl": "bg", "dt": "t", "q": text},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            translated = "".join(part[0] for part in data[0] if part[0])
            results.append(translated)
            time.sleep(0.25)
        except Exception as e:
            print(f"  Translate error: {e}")
            results.append(text)
    return results

def translate_long_text(text: str, chunk_size: int = 1200) -> str:
    """Split long text into chunks, translate each, and rejoin."""
    if not text:
        return text
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks, current = [], ""
    for s in sentences:
        if len(current) + len(s) + 1 < chunk_size:
            current = (current + " " + s).strip()
        else:
            if current:
                chunks.append(current)
            current = s
    if current:
        chunks.append(current)
    translated = google_translate(chunks)
    return " ".join(translated)

def text_to_html(text: str) -> str:
    """Convert plain text paragraphs to HTML <p> tags."""
    paragraphs = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    return "".join(f"<p>{p}</p>" for p in paragraphs)

# ── HELPERS ──────────────────────────────────────────────────────────────────
def clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()

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
    if hasattr(entry, "media_content") and entry.media_content:
        url = entry.media_content[0].get("url", "")
        if url.startswith("http"):
            return url
    if hasattr(entry, "enclosures") and entry.enclosures:
        url = entry.enclosures[0].get("href", "")
        if url.startswith("http"):
            return url
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', entry.get("summary", ""))
    if match:
        return match.group(1)
    seed = hashlib.md5(title.encode()).hexdigest()[:10]
    return f"https://picsum.photos/seed/{seed}/600/340"

def make_id(title: str, source: str) -> str:
    return hashlib.md5(f"{source}-{title}".encode()).hexdigest()[:12]

def fetch_full_content(url: str) -> str | None:
    """Fetch and extract full article text using trafilatura."""
    if not HAS_TRAFILATURA:
        return None
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        return text
    except Exception as e:
        print(f"  Content fetch error: {e}")
        return None

# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
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
            if count >= 5:
                break

            title   = clean_html(entry.get("title", ""))
            summary = clean_html(entry.get("summary", entry.get("description", "")))[:400]
            link    = entry.get("link", "")

            if not title or not link:
                continue
            if not is_relevant(title, summary):
                continue

            iso_date, bg_date = parse_date(entry)
            category, tag = detect_category(title, summary)
            image  = article_image(entry, title)
            art_id = make_id(title, feed_cfg["source"])

            collected.append({
                "id":            art_id,
                "title_en":      title,
                "excerpt_en":    summary,
                "content_en":    None,   # filled below
                "title":         title,
                "excerpt":       summary,
                "content":       "",
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
        time.sleep(0.5)

    collected.sort(key=lambda a: a["published_iso"], reverse=True)
    collected = collected[:30]

    # ── FETCH FULL CONTENT ──
    print(f"\nFetching full article content …")
    for i, art in enumerate(collected):
        print(f"  [{i+1}/{len(collected)}] {art['source']}: {art['title_en'][:60]}")
        full = fetch_full_content(art["url"])
        if full and len(full) > 200:
            art["content_en"] = full[:8000]  # cap at 8k chars
        else:
            art["content_en"] = art["excerpt_en"]
        time.sleep(0.5)

    # ── TRANSLATE ──
    print(f"\nTranslating {len(collected)} articles …")
    titles   = [a["title_en"]   for a in collected]
    excerpts = [a["excerpt_en"][:200] for a in collected]

    translated_titles   = google_translate(titles)
    translated_excerpts = google_translate(excerpts)

    for i, art in enumerate(collected):
        art["title"]   = translated_titles[i]
        art["excerpt"] = translated_excerpts[i]

        print(f"  Translating content [{i+1}/{len(collected)}] …")
        translated_content = translate_long_text(art["content_en"])
        art["content"] = text_to_html(translated_content)

    print("Translation done.")

    # ── CLEAN UP ──
    for art in collected:
        del art["title_en"]
        del art["excerpt_en"]
        del art["content_en"]

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
