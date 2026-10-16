"""
SKAINET – News Fetcher, Scraper & Translator
Fetches AI news (lab announcements, AI outlets, investments, Bulgaria), extracts the
full article text and image, translates English articles to Bulgarian, ranks them by
importance and outputs articles.json + sitemap.xml for the frontend.
"""

import feedparser
import requests
import json
import os
import hashlib
import re
import time
import calendar
from datetime import datetime, timezone, timedelta
from html import escape, unescape
from urllib.parse import urlsplit

try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False
    print("trafilatura not installed – falling back to RSS excerpts")

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SKAINET/1.0; +https://skainet-vert.vercel.app)"}

# ── SOURCES ──────────────────────────────────────────────────────────────────
# kind:     "rss" (default) | "sitemap" (site without RSS) | "techmeme" (curated top stories)
# official: the lab's own announcements – ranked higher on the homepage
# general:  not an AI-only feed – the AI term must be in the title, not just the summary
# lang:     "bg" sources are already Bulgarian – no translation needed
# bulgaria: every story from this source is about Bulgaria
FEEDS = [
    # Models – straight from the labs
    {"source": "OpenAI",          "url": "https://openai.com/news/rss.xml", "official": True},
    {"source": "Google DeepMind", "url": "https://deepmind.google/blog/rss.xml", "official": True},
    {"source": "Anthropic",       "url": "https://www.anthropic.com/sitemap.xml", "kind": "sitemap", "path": "/news/", "official": True},
    {"source": "NVIDIA",          "url": "https://blogs.nvidia.com/feed/", "official": True, "general": True},
    {"source": "Google AI",       "url": "https://blog.google/technology/ai/rss/", "official": True},
    {"source": "HuggingFace",     "url": "https://huggingface.co/blog/feed.xml"},
    # AI news
    {"source": "Techmeme",        "url": "https://www.techmeme.com/feed.xml", "kind": "techmeme", "general": True},
    {"source": "TechCrunch",      "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"source": "The Decoder",     "url": "https://the-decoder.com/feed/"},
    {"source": "The Verge",       "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"},
    {"source": "SiliconANGLE",    "url": "https://siliconangle.com/category/ai/feed/"},
    {"source": "Wired",           "url": "https://www.wired.com/feed/tag/ai/latest/rss"},
    {"source": "CNBC",            "url": "https://www.cnbc.com/id/19854910/device/rss/rss.html", "general": True},
    {"source": "Ars Technica",    "url": "https://feeds.arstechnica.com/arstechnica/technology-lab", "general": True},
    # Investments
    {"source": "Crunchbase News", "url": "https://news.crunchbase.com/feed/", "general": True},
    {"source": "TechCrunch",      "url": "https://techcrunch.com/category/venture/feed/", "general": True},
    {"source": "Tech.eu",         "url": "https://tech.eu/feed/", "general": True},
    {"source": "EU-Startups",     "url": "https://www.eu-startups.com/feed/", "general": True},
    # Bulgaria
    {"source": "Novinite",        "url": "https://www.novinite.com/services/news_rdf.php", "general": True, "bulgaria": True},
    {"source": "Kaldata",         "url": "https://www.kaldata.com/rss/", "lang": "bg"},
    {"source": "TechNews.bg",     "url": "https://technews.bg/feed", "lang": "bg"},
]

MAX_AGE_H        = 48    # ignore stories older than this
BG_MAX_AGE_H     = 168   # Bulgaria-related AI news is rarer – look back a week
MAX_PER_SOURCE   = 6     # new stories per source per run
MAX_NEW_PER_RUN  = 30    # translation budget per run (Google rate-limits GitHub runners)
MAX_ARTICLES     = 100   # stored in articles.json
SECTION_MIN      = 4     # always keep this many per homepage section, even if older
SECTION_CATEGORIES = ("МОДЕЛИ", "ИНВЕСТИЦИИ", "AI В БЪЛГАРИЯ")

# ── RELEVANCE ────────────────────────────────────────────────────────────────
# 1) AI gate – the story must clearly be about AI.
#    General feeds need a core term in the title; drones/chips alone don't make war or stock news AI news.
AI_CORE = re.compile(
    r"\b(ai|a\.i\.|artificial intelligence|machine learning|deep learning|neural|llms?|"
    r"(large )?language models?|foundation models?|frontier models?|generative|genai|chatbots?|"
    r"gpt[-\w]*|chatgpt|openai|anthropic|claude|gemini|deepmind|llama|mistral|grok|xai|deepseek|qwen|"
    r"nemotron|copilot|perplexity|hugging ?face|agentic|robots?|robotics|humanoids?|pretraining|fine-tun\w*)\b", re.I)
AI_EXTRA = re.compile(r"\b(drones?|robotaxis?|self-driving|nvidia|gpus?)\b", re.I)
AI_BG = re.compile(
    r"(изкуствен\w*\s+интелект|\bИИ\b|\bAI\b|\bчатбот\w*|ChatGPT|OpenAI|Anthropic|Gemini|Claude|Nvidia|"
    r"\bневронн\w*|машинно\s+обучение|\bгенеративн\w*|езиков\w*\s+модел\w*|\bробот\w*|\bдрон\w*)", re.I)
BULGARIA = re.compile(r"\b(bulgaria\w*|sofia|българи\w*|българск\w*|софия)\b", re.I)

# 2) Topic – what SKAINET covers: models & launches, world AI news, investments,
#    medicine, breakthroughs, robots & drones, hardware
TOPIC_KEYWORDS = [
    # launches & models
    "release", "launch", "announce", "unveil", "introduc", "debut", "new model", "open-weight",
    "update", "upgrade", "feature", "now available", "rolls out", "rolled out", "integrat",
    "model", "agent", "reasoning", "multimodal", "benchmark", "open source", "open-source",
    # world AI news
    "government", "world", "global", "china", "europe", "eu ", "u.s.", "country", "countries",
    "export", "military", "defense", "election", "ministry", "security", "safety", "ban ",
    # investments
    "raise", "funding", "funds", "series a", "series b", "series c", "series d", "seed round",
    "valuation", "valued at", "invest", "acqui", "ipo", "billion", "million", "unicorn", "startup",
    # medicine & science
    "medic", "health", "patient", "hospital", "clinical", "drug", "cancer", "diagnos", "disease",
    "doctor", "fda", "protein", "biotech", "vaccine", "surgery", "brain", "implant",
    "research", "study", "scientists", "discover", "breakthrough",
    # success stories
    "record", "milestone", "first", "achiev", "solves", "saves", "success", "beats", "outperforms",
    # robots, drones, hardware
    "robot", "humanoid", "drone", "autonomous", "self-driving", "chip", "gpu", "data center",
]

# 3) Exclude – lawsuits, layoffs, politics and stock-market noise
NEGATIVE_KEYWORDS = [
    "lawsuit", "sues", "sued", "court", "judge", "verdict", "testimony",
    "testifies", "on trial", "criminal trial", "legal battle", "settlement",
    "layoff", "laid off", "job cut", "employees fired",
    "gdpr", "compliance", "congress", "senate", "parliament", "legislation",
    "quarterly earnings", "earnings call", "stock price", "shares fell", "shares rose", "stock market",
    "stocks", "sell-off", "selloff", "wall street", "nasdaq", "s&p 500", "cramer", "credit default",
    "deal of the day", "discount", "% off", "best deals", "promo code",
    "porn", "horny", "nsfw", "onlyfans",
]

# ── CATEGORIES (first match wins; English + Bulgarian terms, matched on the title) ──
CATEGORIES = [
    ("ИНВЕСТИЦИИ", "tag-invest", re.compile(
        r"\b(rais(es|ed|ing)|secur(es|ed)|land(s|ed)|clos(es|ed))\s+([$€£]|\d|funds|money|capital|seed|series)|"
        r"\b(funding|funded|series [a-f]|seed round|pre-seed|valuation|valued at|venture|"
        r"invest(s|ed|ing|ment|ments)?|acqui(re|res|red|sition|sitions)|ipo|unicorn)\b|"
        r"\b(инвестиц\w*|финансиран\w*|придобиван\w*)|\bнабра\w*\s+[$€\d]", re.I)),
    ("AI В МЕДИЦИНАТА", "tag-med", re.compile(
        r"\b(medic\w*|health\w*|patients?|hospitals?|clinic\w*|drugs?|cancer|diagnos\w*|diseases?|"
        r"doctors?|fda|proteins?|biotech\w*|vaccin\w*|surg\w*|alzheimer\w*|tumou?rs?|therap\w*|implants?)\b|"
        r"\b(медицин\w*|пациент\w*|болниц\w*|лекар\w*|здравн\w*|здраве\w*|рак|диагно\w*|лечени\w*|"
        r"болест\w*|протеин\w*|ваксин\w*|имплант\w*|зрение\w*)\b", re.I)),
    # a named model, or "model" together with a launch verb – not every story that mentions a model
    ("МОДЕЛИ", "tag-models", re.compile(
        r"\b(gpt-?\d\w*|claude|gemini|gemma|llama|mistral|mixtral|grok|deepseek|qwen|kimi|glm-?\d\w*|phi-\d|"
        r"nemotron|sora|veo|imagen|midjourney|flux)\b|"
        r"^(?=.*\b(launch|releas|unveil|introduc|debut|open[- ]sourc|open[- ]weight|пуска|пусна|представ|обяви|разкри)\w*)"
        r"(?=.*\b(model|модел)\w*)", re.I)),
    ("РОБОТИ & ДРОНОВЕ", "tag-tech", re.compile(
        r"\b(robot\w*|humanoids?|drones?|boston dynamics|figure ai|optimus|quadruped\w*|"
        r"autonomous vehicles?|self-driving|robotaxis?|waymo)\b|\b(робот\w*|дрон\w*|безпилот\w*)", re.I)),
    ("AI ХАРДУЕР", "tag-tech", re.compile(
        r"\b(chips?|nvidia|blackwell|rubin|h100|h200|b200|gb200|gpus?|tpus?|semiconductors?|tsmc|asml|"
        r"data ?cent(er|re)s?|supercomputers?)\b|\b(чип\w*|процесор\w*|видеокарт\w*|суперкомпют\w*)", re.I)),
    ("ПРОБИВИ & УСПЕХИ", "tag-research", re.compile(
        r"\b(breakthroughs?|record[- ]breaking|new record|world record|milestones?|first[- ]ever|"
        r"world'?s first|solves?|solved|discover\w*|outperforms?|surpass\w*|nobel)\b|"
        r"\b(пробив\w*|рекорд\w*|открити\w*|успех\w*|реши|решава)\b|за първи път", re.I)),
    ("ГЕНЕРАТИВНО AI", "tag-tools", re.compile(
        r"\b(image generat\w*|video generat\w*|music generat\w*|text-to-\w+|dall-e|stable diffusion)\b", re.I)),
    ("ЗА РАЗРАБОТЧИЦИ", "tag-tools", re.compile(
        r"\b(open[- ]source|api|sdk|developers?|coding|code generation|github|hugging ?face|"
        r"fine-tun\w*|framework|cursor|vibe cod\w*)\b|\b(разработчи\w*|програмист\w*)", re.I)),
    ("АНАЛИЗИ", "tag-analysis", re.compile(
        r"\b(benchmarks?|versus|vs\.?|compar\w*|evaluations?|rankings?|leaderboards?|survey|analysis)\b|"
        r"\b(анализ\w*|сравнени\w*|проучван\w*)", re.I)),
    ("НАУЧИ СЕ", "tag-learn", re.compile(
        r"\b(tutorial|guide|how to|prompting|course)\b|\b(ръководство|как да)\b", re.I)),
]

# ── IMPORTANCE (homepage hero, side cards, "Важно днес") ─────────────────────
BIG_NAMES = re.compile(
    r"\b(openai|anthropic|google|deepmind|meta|nvidia|microsoft|apple|amazon|xai|mistral|deepseek|"
    r"chatgpt|gpt-?\d\w*|claude|gemini|llama|grok)\b", re.I)
BIG_MONEY = re.compile(r"[$€£]\s?\d+(\.\d+)?\s?(b|bn|billion)\b|\d+(\.\d+)?\s?(billion|млрд)", re.I)

def importance(a: dict) -> int:
    """Higher = shown more prominently. Recomputed for every article on every run."""
    text = f"{a.get('title_en', '')} {a['title']}"
    score = 0
    if a.get("official"):
        score += 3                      # the lab announced it itself
    if a.get("techmeme"):
        score += 3                      # Techmeme editors picked it as a top tech story
    if a["category"] in ("МОДЕЛИ", "AI В БЪЛГАРИЯ"):
        score += 2
    if a["category"] == "ИНВЕСТИЦИИ":
        score += 4 if BIG_MONEY.search(text) else 1   # billion-scale rounds lead; a €3M seed round doesn't
    if BIG_NAMES.search(text):
        score += 1
    return score

# ── GOOGLE TRANSLATE (free, no API key) ──────────────────────────────────────
GTRANS_URL = "https://translate.googleapis.com/translate_a/single"
RETRY_DELAYS = (2, 5, 15, 30)  # seconds to wait before each retry (429s from GitHub runner IPs)

class TranslationError(Exception):
    """Google Translate kept failing after all retries."""

def google_translate(texts: list[str]) -> list[str]:
    """Translate a list of short texts to Bulgarian.

    Raises TranslationError instead of returning the English original,
    so untranslated text never ends up in articles.json.
    """
    if not texts:
        return []
    results = []
    for text in texts:
        if not text or not text.strip():
            results.append(text)
            continue
        last_err = None
        for delay in (0, *RETRY_DELAYS):
            time.sleep(delay)
            try:
                resp = requests.get(
                    GTRANS_URL,
                    params={"client": "gtx", "sl": "en", "tl": "bg", "dt": "t", "q": text},
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
                results.append("".join(part[0] for part in data[0] if part[0]))
                break
            except Exception as e:
                last_err = e
                print(f"  Translate error: {str(e)[:100]}")
        else:
            raise TranslationError(last_err)
        time.sleep(0.25)
    return results


def md_block_to_html(orig_line: str, translated: str) -> str:
    """Convert one translated block to HTML using the original for type detection."""
    t = translated.strip()
    if orig_line.startswith("#"):
        kind = "h3" if orig_line.startswith("###") else "h2"
        t = t.lstrip("#").strip()
    elif orig_line.startswith("- ") or orig_line.startswith("* "):
        kind = "li"
        t = re.sub(r'^\s*[-*]\s*', '', t)
    elif orig_line.startswith("> "):
        kind = "blockquote"
        t = re.sub(r'^>\s*', '', t)
    else:
        kind = "p"
    if not t:
        return ""
    t = escape(t, quote=False)  # article text is plain text – never let it inject markup
    t = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', t)
    if kind == "p":
        t = re.sub(r'\*(.+?)\*', r'<em>\1</em>', t)
    return f"<{kind}>{t}</{kind}>"

def translate_and_format(text: str, translate: bool = True) -> str:
    """Translate article text (unless it is already Bulgarian) and return rich HTML preserving structure."""
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
        if not translate:
            translated_blocks.extend(batch)
            continue
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
BG_MONTHS = ["", "яну", "фев", "мар", "апр", "май", "юни", "юли", "авг", "сеп", "окт", "ное", "дек"]
BG_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s",
    "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sht",
    "ъ": "a", "ь": "y", "ю": "yu", "я": "ya",
})

def has_cyrillic(text: str) -> bool:
    return bool(re.search(r"[Ѐ-ӿ]", text or ""))

def clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()

def norm_url(url: str) -> str:
    """Same story, different tracking params → same key."""
    parts = urlsplit(url or "")
    return (parts.netloc.lower().removeprefix("www.") + parts.path.rstrip("/")).lower()

def is_relevant(title: str, summary: str, cfg: dict, about_bg: bool) -> bool:
    if cfg.get("lang") == "bg":
        return bool(AI_BG.search(title) or AI_BG.search(summary[:300]))
    if cfg.get("general"):
        if not AI_CORE.search(title):
            return False
    elif not (AI_CORE.search(f"{title} {summary}") or AI_EXTRA.search(f"{title} {summary}")):
        return False
    combined = f"{title} {summary}".lower()
    if any(kw in combined for kw in NEGATIVE_KEYWORDS):
        return False
    return about_bg or any(kw in combined for kw in TOPIC_KEYWORDS)  # any AI story about Bulgaria counts

def detect_category(a: dict) -> tuple[str, str]:
    title = f"{a.get('title_en', '')} {a['title']}"
    if a.get("bulgaria") or BULGARIA.search(f"{title} {a.get('excerpt', '')}"):
        return "AI В БЪЛГАРИЯ", "tag-bg"
    for name, tag, pattern in CATEGORIES:
        if pattern.search(title):
            return name, tag
    return "AI НОВИНИ", "tag-ai"

def entry_datetime(entry) -> datetime | None:
    """feedparser normalises RSS and Atom dates into UTC struct_time."""
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(attr)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), timezone.utc)
    return None

def bg_date(dt: datetime) -> str:
    return f"{dt.day} {BG_MONTHS[dt.month]} {dt.year}"

def entry_image(entry) -> str | None:
    for media in entry.get("media_content", []) + entry.get("media_thumbnail", []):
        if media.get("url", "").startswith("http"):
            return media["url"]
    for enc in entry.get("enclosures", []):
        if enc.get("href", "").startswith("http") and "image" in enc.get("type", "image"):
            return enc["href"]
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', entry.get("summary", ""))
    return match.group(1) if match else None

def placeholder_image(title: str) -> str:
    seed = hashlib.md5(title.encode()).hexdigest()[:10]
    return f"https://picsum.photos/seed/{seed}/600/340"

OG_IMAGE = [
    re.compile(r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)(?::src)?["\'][^>]*content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:image|twitter:image)', re.I),
]
def meta_content(html: str, prop: str) -> str | None:
    for pat in (rf'<meta[^>]+(?:property|name)=["\']{prop}["\'][^>]*content=["\']([^"\']*)',
                rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{prop}["\']'):
        m = re.search(pat, html, re.I)
        if m:
            return unescape(m.group(1)).strip()
    return None

def og_image(html: str) -> str | None:
    for pat in OG_IMAGE:
        m = pat.search(html or "")
        if m and m.group(1).startswith("http"):
            return unescape(m.group(1))
    return None

def make_id(title: str, source: str) -> str:
    return hashlib.md5(f"{source}-{title}".encode()).hexdigest()[:12]

def make_slug(title: str) -> str:
    """Create a URL-friendly slug from a title (Bulgarian titles are transliterated)."""
    slug = title.lower().translate(BG_TRANSLIT)
    slug = re.sub(r'[àáâãäå]', 'a', slug)
    slug = re.sub(r'[èéêë]', 'e', slug)
    slug = re.sub(r'[ìíîï]', 'i', slug)
    slug = re.sub(r'[òóôõö]', 'o', slug)
    slug = re.sub(r'[ùúûü]', 'u', slug)
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'\s+', '-', slug.strip())
    slug = re.sub(r'-+', '-', slug)
    return slug[:60].rstrip('-')

def fetch_page(url: str) -> tuple[str | None, str | None]:
    """Download an article page → (full text with markdown formatting, og:image)."""
    if not HAS_TRAFILATURA:
        return None, None
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None, None
        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            include_formatting=True,
        )
        return text, og_image(downloaded)
    except Exception as e:
        print(f"  Content fetch error: {e}")
        return None, None

# ── SOURCE READERS → items: {title, summary, link, published, image, content?} ──
def read_rss(cfg: dict) -> list[dict]:
    feed = feedparser.parse(cfg["url"])
    return [{
        "title":     clean_html(e.get("title", "")),
        "summary":   clean_html(e.get("summary", e.get("description", "")))[:400],
        "link":      e.get("link", ""),
        "published": entry_datetime(e),
        "image":     entry_image(e),
    } for e in feed.entries[:60]]

PUBLISHER_RE = re.compile(r'/\s*<a href="[^"]+">([^<]+)</a>:')
BYLINE_RE = re.compile(r"\s*\([^()]*/[^()]*\)\s*$")  # Techmeme headline suffix "(Author/Publisher)"

def strip_byline(title: str) -> str:
    return BYLINE_RE.sub("", title).strip()

def read_techmeme(cfg: dict) -> list[dict]:
    """Techmeme items point to the original story; the publisher becomes the source."""
    items = []
    for e in feedparser.parse(cfg["url"]).entries:
        summary = e.get("summary", "")
        links = [h for h in re.findall(r'href="([^"]+)"', summary) if "techmeme.com" not in h]
        publisher = PUBLISHER_RE.search(summary)
        items.append({
            "title":     strip_byline(clean_html(e.get("title", ""))),
            "summary":   strip_byline(clean_html(e.get("title", ""))),
            "link":      links[0] if links else e.get("link", ""),
            "published": entry_datetime(e),
            "image":     None,
            "source":    unescape(publisher.group(1)) if publisher else "Techmeme",
        })
    return items

def read_sitemap(cfg: dict, since: datetime) -> list[dict]:
    """For sites without RSS: recent URLs from the sitemap, then title/date/image from each page's meta tags."""
    xml = requests.get(cfg["url"], headers=HTTP_HEADERS, timeout=20).text
    recent = [loc for loc, lastmod in re.findall(r"<loc>([^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>", xml)
              if cfg["path"] in loc and lastmod >= since.isoformat()[:19]]
    items = []
    for url in recent[:MAX_PER_SOURCE]:
        try:
            html = requests.get(url, headers=HTTP_HEADERS, timeout=20).text
        except Exception as e:
            print(f"  Page fetch error: {e}")
            continue
        published = meta_content(html, "article:published_time")
        items.append({
            "title":     meta_content(html, "og:title") or "",
            "summary":   meta_content(html, "og:description") or "",
            "link":      url,
            "published": datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None,
            "image":     og_image(html),
            "content":   trafilatura.extract(html, include_formatting=True) if HAS_TRAFILATURA else None,
        })
    return items

# ── MAIN ─────────────────────────────────────────────────────────────────────
ROOT = os.path.join(os.path.dirname(__file__), "..")
OUT_PATH = os.path.join(ROOT, "articles.json")
PENDING_PATH = os.path.join(os.path.dirname(__file__), "pending_articles.json")
MAX_PENDING_DAYS = 7  # give up on an article that still can't be translated after this long
IMAGE_BACKFILL_PER_RUN = 20

def main():
    # Load existing articles first so we can skip already-seen ones
    existing = []
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, encoding="utf-8") as f:
                existing = json.load(f).get("articles", [])
        except Exception:
            pass

    # Articles whose translation failed in an earlier run — retried every run
    pending = []
    if os.path.exists(PENDING_PATH):
        with open(PENDING_PATH, encoding="utf-8") as f:
            pending = json.load(f)
    pending_cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_PENDING_DAYS)).isoformat()
    pending = [a for a in pending if a["published_iso"] >= pending_cutoff]

    # Older runs saved the English original when Google failed — send those back to the queue
    for a in [a for a in existing if not has_cyrillic(a["title"] + a["excerpt"] + a["content"])]:
        existing.remove(a)
        pending.append({**a, "title_en": a["title"], "excerpt_en": a["excerpt"], "content_en": None})
    if pending:
        print(f"{len(pending)} articles waiting for translation\n")

    known_ids  = {a["id"] for a in existing + pending}
    known_urls = {norm_url(a["url"]) for a in existing + pending}
    now = datetime.now(timezone.utc)
    techmeme_urls: set[str] = set()
    collected = []

    for cfg in FEEDS:
        print(f"Fetching {cfg['source']} …")
        kind = cfg.get("kind", "rss")
        try:
            if kind == "sitemap":
                items = read_sitemap(cfg, now - timedelta(hours=MAX_AGE_H))
            elif kind == "techmeme":
                items = read_techmeme(cfg)
            else:
                items = read_rss(cfg)
        except Exception as e:
            print(f"  Failed: {e}")
            continue

        count = 0
        for it in items:
            if kind == "techmeme":
                techmeme_urls.add(norm_url(it["link"]))
            title, summary, link = it["title"], it["summary"], it["link"]
            if not title or not link or norm_url(link) in known_urls:
                continue
            source = it.get("source", cfg["source"])
            art_id = make_id(title, source)
            if art_id in known_ids:
                continue  # already translated and stored — skip
            published = it["published"] or now
            about_bg = cfg.get("bulgaria") or bool(BULGARIA.search(f"{title} {summary}"))
            if now - published > timedelta(hours=BG_MAX_AGE_H if about_bg else MAX_AGE_H):
                continue
            if not is_relevant(title, summary, cfg, about_bg):
                continue

            art = {
                "id":            art_id,
                "slug":          make_slug(title),
                "title":         title,
                "excerpt":       summary,
                "content":       "",
                "url":           link,
                "source":        source,
                "date":          bg_date(published),
                "published_iso": published.isoformat(),
                "image":         it["image"],
                "lang":          cfg.get("lang", "en"),
                "official":      bool(cfg.get("official")),
                "bulgaria":      bool(cfg.get("bulgaria")),
                "title_en":      title if cfg.get("lang", "en") == "en" else "",
                "excerpt_en":    summary,
                "content_en":    it.get("content"),
            }
            art["category"], art["tag"] = detect_category(art)
            collected.append(art)
            known_ids.add(art_id)
            known_urls.add(norm_url(link))
            count += 1
            if count >= MAX_PER_SOURCE:
                break
        print(f"  → {count} new articles")
        time.sleep(0.5)

    # Most important new stories get the translation budget first; the rest wait for the next run
    for a in collected:
        a["techmeme"] = norm_url(a["url"]) in techmeme_urls
    collected.sort(key=lambda a: (importance(a), a["published_iso"]), reverse=True)
    collected = pending + collected[:MAX_NEW_PER_RUN]

    # ── FETCH FULL CONTENT + IMAGE (new articles + queued ones that don't have it yet) ──
    print(f"\nFetching full article content …")
    for i, art in enumerate(collected):
        if art.get("content_en") is not None and art.get("image"):
            continue
        print(f"  [{i+1}/{len(collected)}] {art['source']}: {art['title_en'] or art['title'][:60]}"[:90])
        full, image = fetch_page(art["url"])
        if art.get("content_en") is None:
            art["content_en"] = full[:8000] if full and len(full) > 200 else art["excerpt_en"]
        art["image"] = image or art.get("image") or placeholder_image(art["title"])  # og:image is full size; feed images are often thumbnails
        time.sleep(0.5)

    # ── TRANSLATE (one article at a time — a failure queues it instead of publishing English) ──
    print(f"\nTranslating {len(collected)} articles …")
    translated, still_pending = [], []
    for i, art in enumerate(collected):
        if still_pending and art.get("lang", "en") == "en":  # Google already gave up on us this run
            still_pending.append(art)
            continue
        print(f"  [{i+1}/{len(collected)}] {(art['title_en'] or art['title'])[:60]}")
        try:
            if art.get("lang") == "bg":
                art["excerpt"] = art["excerpt_en"][:200]
                art["content"] = translate_and_format(art["content_en"], translate=False)
            else:
                art["title"], art["excerpt"] = google_translate([art["title_en"], art["excerpt_en"][:200]])
                art["content"] = translate_and_format(art["content_en"])
        except TranslationError as e:
            print(f"  ✗ Translation failed, queueing remaining articles for the next run: {str(e)[:100]}")
            still_pending.append(art)
            continue
        for key in ("excerpt_en", "content_en"):
            del art[key]
        translated.append(art)

    print(f"Translation done: {len(translated)} translated, {len(still_pending)} queued.")

    with open(PENDING_PATH, "w", encoding="utf-8") as f:
        json.dump(still_pending, f, ensure_ascii=False, indent=2)

    # ── MERGE with existing articles.json ──
    merged = {a["id"]: a for a in existing}
    for a in translated:
        merged[a["id"]] = a  # new wins on same id

    # Real images for older articles that only got a random placeholder
    missing = [a for a in merged.values() if "picsum.photos" in a.get("image", "")][:IMAGE_BACKFILL_PER_RUN]
    if missing:
        print(f"\nLooking up images for {len(missing)} older articles …")
    for a in missing:
        _, image = fetch_page(a["url"])
        if image:
            a["image"] = image

    # Categories and importance follow the current rules for every article, old or new
    for a in merged.values():
        if norm_url(a["url"]) in techmeme_urls:
            a["techmeme"] = True
        a["title"] = strip_byline(a["title"])
        a["category"], a["tag"] = detect_category(a)
        a["importance"] = importance(a)

    by_date = sorted(merged.values(), key=lambda a: a["published_iso"], reverse=True)
    keep = by_date[:MAX_ARTICLES]
    kept_ids = {a["id"] for a in keep}
    for cat in SECTION_CATEGORIES:  # homepage sections never run empty just because a category is quieter
        for a in [a for a in by_date if a["category"] == cat][:SECTION_MIN]:
            if a["id"] not in kept_ids:
                keep.append(a)
                kept_ids.add(a["id"])
    all_articles = sorted(keep, key=lambda a: a["published_iso"], reverse=True)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(all_articles),
        "articles": all_articles,
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    counts = {}
    for a in all_articles:
        counts[a["category"]] = counts.get(a["category"], 0) + 1
    print(f"\n✓ Saved {len(all_articles)} articles to articles.json  {counts}")

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
    sitemap_path = os.path.join(ROOT, "sitemap.xml")
    with open(sitemap_path, "w", encoding="utf-8") as f:
        f.write(sitemap)
    print(f"✓ Generated sitemap.xml with {len(all_articles) + 1} URLs")

if __name__ == "__main__":
    main()
