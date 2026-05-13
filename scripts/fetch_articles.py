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
    {"url": "https://huggingface.co/blog/feed.xml",                         "source": "HuggingFace"},
    {"url": "https://venturebeat.com/category/ai/feed/",                    "source": "VentureBeat"},
    {"url": "https://techcrunch.com/category/artificial-intelligence/feed/","source": "TechCrunch"},
    {"url": "https://www.artificialintelligence-news.com/feed/",            "source": "AI News"},
    {"url": "https://www.technologyreview.com/feed/",                       "source": "MIT Tech Review"},
    {"url": "https://blog.google/technology/ai/rss/",                       "source": "Google AI"},
    {"url": "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml", "source": "The Verge"},
    {"url": "https://feeds.arstechnica.com/arstechnica/technology-lab",     "source": "Ars Technica"},
]

# ── POSITIVE KEYWORDS — must match at least one ───────────────────────────────
# Focus: new features/releases of AI models/platforms, robots, drones
KEYWORDS = [
    # Model & platform launches / updates
    "releases", "release", "launches", "launch", "announces", "announced",
    "unveils", "unveiled", "introduces", "introduced", "debuts", "new model",
    "new version", "update", "upgrade", "feature", "capability", "now can",
    # Specific AI models
    "gpt-", "gpt4", "gpt5", "claude", "gemini", "llama", "mistral", "grok",
    "dall-e", "midjourney", "stable diffusion", "sora", "veo", "flux",
    "deepseek", "qwen", "phi-", "copilot", "perplexity",
    # AI capabilities & tech
    "multimodal", "vision", "reasoning", "agent", "benchmark", "llm",
    "language model", "generative ai", "transformer", "diffusion model",
    "voice mode", "image generation", "video generation", "code generation",
    "fine-tuning", "open source", "open-source", "api", "inference",
    "context window", "tokens", "training", "chatgpt",
    # Robots & drones
    "robot", "humanoid", "drone", "autonomous vehicle", "self-driving",
    "boston dynamics", "figure robot", "tesla optimus", "1x robot",
    "agility robotics", "spot robot", "quadruped",
    # AI hardware
    "nvidia blackwell", "h100", "b200", "gb200", "tpu", "ai chip",
    "neural processing", "inference chip",
    # Research & discoveries
    "breakthrough", "researchers", "new study", "paper", "arxiv",
    "scientists", "discovery", "achieve", "achieves", "outperforms",
    "state of the art", "state-of-the-art", "sota",
    # Comparisons & evaluations
    "vs ", "versus", "compared to", "better than", "beats", "ranking",
    "leaderboard", "evaluation", "performance", "score",
    # New tools & features
    "now available", "just released", "new tool", "new feature",
    "rolls out", "rolled out", "integrates", "adds ", "plugin",
    "extension", "assistant", "copilot",
]

# ── NEGATIVE KEYWORDS — exclude if title+summary contain these ────────────────
# Filters out business, legal, financial, political news
NEGATIVE_KEYWORDS = [
    "lawsuit", "sues", "sued", "court", "judge", "verdict", "testimony",
    "testifies", "trial", "legal battle", "settlement",
    "ipo", "acquisition", "acquires", "acquired", "merger", "buys ",
    "layoff", "laid off", "job cut", "employees fired",
    "gdpr", "compliance", "congress", "senate", "parliament", "legislation",
    "quarterly earnings", "revenue", "profit ", "stock price", "shares fell",
    "venture capital", "series a", "series b", "series c", "valuation at",
    "secondary platform", "investors against",
]

# ── CATEGORY DETECTION ───────────────────────────────────────────────────────
CATEGORY_RULES = [
    (["robot", "humanoid", "drone", "boston dynamics", "figure", "optimus",
      "agility", "quadruped", "autonomous vehicle", "self-driving"], "РОБОТИ & ДРОНОВЕ", "tag-tech"),
    (["chip", "nvidia", "blackwell", "h100", "gpu", "tpu", "hardware",
      "inference chip", "neural processing"], "AI ХАРДУЕР", "tag-tech"),
    (["benchmark", "compare", "versus", "vs ", "test", "evaluation",
      "ranks", "beats", "outperforms"], "АНАЛИЗИ", "tag-analysis"),
    (["open source", "open-source", "api", "fine-tuning", "developer",
      "code generation", "github", "hugging face"], "ЗА РАЗРАБОТЧИЦИ", "tag-tools"),
    (["tutorial", "guide", "how to", "prompt", "learn", "course"], "НАУЧИ СЕ", "tag-learn"),
    (["image generation", "video generation", "dall-e", "midjourney",
      "stable diffusion", "sora", "veo", "flux", "music generation"], "ГЕНЕРАТИВНО AI", "tag-tools"),
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


def md_block_to_html(orig_line: str, translated: str) -> str:
    """Convert one translated block to HTML using the original for type detection."""
    t = translated.strip()
    if not t:
        return ""
    if orig_line.startswith("### "):
        return f"<h3>{re.sub(r'^#+\\s*', '', t)}</h3>"
    if orig_line.startswith("## ") or orig_line.startswith("# "):
        return f"<h2>{re.sub(r'^#+\\s*', '', t)}</h2>"
    if orig_line.startswith("- ") or orig_line.startswith("* "):
        t = re.sub(r'^\s*[-*]\s*', '', t)
        t = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', t)
        return f"<li>{t}</li>"
    if orig_line.startswith("> "):
        t = re.sub(r'^>\s*', '', t)
        return f"<blockquote>{t}</blockquote>"
    t = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', t)
    t = re.sub(r'\*(.+?)\*', r'<em>\1</em>', t)
    return f"<p>{t}</p>"

def translate_and_format(text: str) -> str:
    """Translate article text and return rich HTML preserving structure."""
    if not text:
        return ""
    blocks = [b for b in re.split(r'\n{2,}', text) if b.strip()]
    if not blocks:
        return ""

    # Group consecutive blocks into batches ≤1000 chars for translation
    batches, current, current_len = [], [], 0
    for b in blocks:
        if current_len + len(b) > 1000 and current:
            batches.append(current)
            current, current_len = [b], len(b)
        else:
            current.append(b)
            current_len += len(b)
    if current:
        batches.append(current)

    translated_blocks = []
    for batch in batches:
        combined = "\n\n".join(batch)
        result = google_translate([combined])[0]
        parts = re.split(r'\n{2,}', result)
        # Pad/trim to match original count
        while len(parts) < len(batch):
            parts.append("")
        translated_blocks.extend(parts[:len(batch)])

    html_parts = []
    in_list = False
    for orig, trans in zip(blocks, translated_blocks):
        tag = md_block_to_html(orig.strip(), trans)
        is_li = tag.startswith("<li>")
        if is_li and not in_list:
            html_parts.append("<ul>")
            in_list = True
        elif not is_li and in_list:
            html_parts.append("</ul>")
            in_list = False
        if tag:
            html_parts.append(tag)
    if in_list:
        html_parts.append("</ul>")

    return "".join(html_parts)

# ── HELPERS ──────────────────────────────────────────────────────────────────
def clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()

def is_relevant(title: str, summary: str) -> bool:
    combined = (title + " " + summary).lower()
    if not any(kw in combined for kw in KEYWORDS):
        return False
    if any(kw in combined for kw in NEGATIVE_KEYWORDS):
        return False
    return True

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

def make_slug(title: str) -> str:
    """Create a URL-friendly slug from a title."""
    slug = title.lower()
    slug = re.sub(r'[àáâãäå]', 'a', slug)
    slug = re.sub(r'[èéêë]', 'e', slug)
    slug = re.sub(r'[ìíîï]', 'i', slug)
    slug = re.sub(r'[òóôõö]', 'o', slug)
    slug = re.sub(r'[ùúûü]', 'u', slug)
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'\s+', '-', slug.strip())
    slug = re.sub(r'-+', '-', slug)
    return slug[:60].rstrip('-')

def fetch_full_content(url: str) -> str | None:
    """Fetch and extract full article text using trafilatura with formatting."""
    if not HAS_TRAFILATURA:
        return None
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            include_formatting=True,
        )
        return text
    except Exception as e:
        print(f"  Content fetch error: {e}")
        return None

# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    # Load existing articles first so we can skip already-seen ones
    out_path = os.path.join(os.path.dirname(__file__), "..", "articles.json")
    existing = []
    if os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as f:
                existing = json.load(f).get("articles", [])
        except Exception:
            pass
    from datetime import timedelta
    existing_ids = {a["id"] for a in existing}
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=48)

    collected = []

    for feed_cfg in FEEDS:
        print(f"Fetching {feed_cfg['source']} …")
        try:
            feed = feedparser.parse(feed_cfg["url"])
        except Exception as e:
            print(f"  Failed: {e}")
            continue

        count = 0
        for entry in feed.entries[:25]:  # scan up to 25 entries to find new ones
            title   = clean_html(entry.get("title", ""))
            summary = clean_html(entry.get("summary", entry.get("description", "")))[:400]
            link    = entry.get("link", "")

            if not title or not link:
                continue
            if not is_relevant(title, summary):
                continue

            art_id = make_id(title, feed_cfg["source"])
            if art_id in existing_ids:
                continue  # already translated and stored — skip

            iso_date, bg_date = parse_date(entry)
            # Skip articles older than 48 hours
            try:
                pub_dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00")).replace(tzinfo=None)
                if pub_dt < cutoff:
                    continue
            except Exception:
                pass
            category, tag = detect_category(title, summary)
            image  = article_image(entry, title)

            collected.append({
                "id":            art_id,
                "slug":          make_slug(title),
                "title_en":      title,
                "excerpt_en":    summary,
                "content_en":    None,
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
            if count >= 10:  # max 10 new articles per source per run
                break

        print(f"  → {count} new articles")
        time.sleep(0.5)

    collected.sort(key=lambda a: a["published_iso"], reverse=True)
    collected = collected[:30]

    # ── FETCH FULL CONTENT (only for newly fetched articles) ──
    print(f"\nFetching full article content …")
    for i, art in enumerate(collected):
        print(f"  [{i+1}/{len(collected)}] {art['source']}: {art['title_en'][:60]}")
        full = fetch_full_content(art["url"])
        if full and len(full) > 200:
            art["content_en"] = full[:8000]
        else:
            art["content_en"] = art["excerpt_en"]
        time.sleep(0.5)

    # ── TRANSLATE ──
    print(f"\nTranslating {len(collected)} articles …")
    titles   = [a["title_en"]          for a in collected]
    excerpts = [a["excerpt_en"][:200]  for a in collected]

    translated_titles   = google_translate(titles)
    translated_excerpts = google_translate(excerpts)

    for i, art in enumerate(collected):
        art["title"]   = translated_titles[i]
        art["excerpt"] = translated_excerpts[i]
        print(f"  Translating content [{i+1}/{len(collected)}] …")
        art["content"] = translate_and_format(art["content_en"])

    print("Translation done.")

    # ── CLEAN UP internal fields ──
    for art in collected:
        del art["title_en"]
        del art["excerpt_en"]
        del art["content_en"]

    # ── MERGE with existing articles.json (after cleanup) ──
    merged = {a["id"]: a for a in existing}
    for a in collected:
        merged[a["id"]] = a  # new wins on same id

    all_articles = sorted(merged.values(), key=lambda a: a["published_iso"], reverse=True)[:60]

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(all_articles),
        "articles": all_articles,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Saved {len(all_articles)} articles to articles.json")

    # ── GENERATE SITEMAP ──
    base_url = "https://skainet-vert.vercel.app"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url_entries = [f"""  <url>
    <loc>{base_url}/</loc>
    <lastmod>{today}</lastmod>
    <changefreq>hourly</changefreq>
    <priority>1.0</priority>
  </url>"""]
    for art in all_articles:
        slug = art.get('slug') or art['id']
        url_entries.append(f"""  <url>
    <loc>{base_url}/article/{slug}</loc>
    <lastmod>{art['published_iso'][:10]}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.8</priority>
  </url>""")
    sitemap = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    sitemap += "\n".join(url_entries)
    sitemap += "\n</urlset>\n"
    sitemap_path = os.path.join(os.path.dirname(__file__), "..", "sitemap.xml")
    with open(sitemap_path, "w", encoding="utf-8") as f:
        f.write(sitemap)
    print(f"✓ Generated sitemap.xml with {len(all_articles) + 1} URLs")

if __name__ == "__main__":
    main()
