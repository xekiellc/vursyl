#!/usr/bin/env python3
"""
VURSYL feed pipeline
Generates data/news.json and data/media.json
Sources: NewsAPI + lab/institution RSS feeds + YouTube Data API
Filter:  Claude Haiku — positive/constructive content only, categorized

Env vars required:
  NEWSAPI_KEY        - newsapi.org key
  ANTHROPIC_API_KEY  - Claude API key
  YOUTUBE_API_KEY    - YouTube Data API v3 key

Run: python scripts/update_feed.py
"""

import os, json, time, hashlib, urllib.request, urllib.parse, xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

NEWSAPI_KEY   = os.environ["NEWSAPI_KEY"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
YOUTUBE_KEY   = os.environ["YOUTUBE_API_KEY"]

OUT_NEWS  = "data/news.json"
OUT_MEDIA = "data/media.json"
MAX_NEWS_ITEMS  = 60
MAX_MEDIA_ITEMS = 18
LOOKBACK_HOURS  = 48
MODEL = "claude-haiku-4-5-20251001"

CATEGORIES = ["ai", "quantum", "health", "robotics", "compute", "breakthroughs"]

# ---------------- SOURCES ----------------

NEWSAPI_QUERIES = {
    "ai":       '"artificial intelligence" OR "large language model" OR LLM OR "AI model" OR "AI breakthrough"',
    "quantum":  '"quantum computing" OR "quantum computer" OR "quantum chip" OR "error correction" qubit',
    "health":   '"AI" AND (drug OR diagnosis OR cancer OR FDA OR clinical OR longevity OR CRISPR OR biotech)',
    "robotics": 'humanoid robot OR robotics OR SpaceX OR "space technology" OR autonomous',
    "compute":  'semiconductor OR "data center" OR GPU OR NVIDIA OR "chip breakthrough" OR supercomputer',
}

RSS_FEEDS = [
    ("OpenAI",            "https://openai.com/news/rss.xml"),
    ("Anthropic",         "https://www.anthropic.com/rss.xml"),
    ("Google DeepMind",   "https://deepmind.google/blog/rss.xml"),
    ("NVIDIA",            "https://blogs.nvidia.com/feed/"),
    ("Microsoft Research","https://www.microsoft.com/en-us/research/feed/"),
    ("MIT News AI",       "https://news.mit.edu/topic/mitartificial-intelligence2-rss.xml"),
    ("IBM Research",      "https://research.ibm.com/blog/rss.xml"),
    ("NIH News",          "https://www.nih.gov/news-events/news-releases/feed"),
]

# YouTube channels by handle — resolved to upload playlists at runtime
YOUTUBE_CHANNELS = [
    "TwoMinutePapers",
    "lexfridman",
    "DwarkeshPatel",
    "PeterDiamandis",
    "a16z",
    "ycombinator",
    "anthropic-ai",
    "OpenAI",
    "GoogleDeepMind",
    "NVIDIA",
    "qiskit",
    "TED",
]

# ---------------- HTTP HELPERS ----------------

def http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "VursylBot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")

def http_json(url, headers=None):
    return json.loads(http_get(url, headers))

def post_json(url, payload, headers):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))

def iso(dt_str):
    """Normalize various date formats to ISO 8601 UTC."""
    fmts = ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z",
            "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
            "%Y-%m-%dT%H:%M:%S.%fZ"]
    for f in fmts:
        try:
            d = datetime.strptime(dt_str.strip(), f)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc).isoformat()
        except (ValueError, AttributeError):
            continue
    return datetime.now(timezone.utc).isoformat()

def recent(iso_str, hours=LOOKBACK_HOURS):
    try:
        d = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return d > datetime.now(timezone.utc) - timedelta(hours=hours)
    except ValueError:
        return False

# ---------------- COLLECTORS ----------------

def collect_newsapi():
    items, frm = [], (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")
    for cat, q in NEWSAPI_QUERIES.items():
        url = ("https://newsapi.org/v2/everything?" + urllib.parse.urlencode({
            "q": q, "from": frm, "language": "en", "sortBy": "publishedAt", "pageSize": 25
        }) + f"&apiKey={NEWSAPI_KEY}")
        try:
            for a in http_json(url).get("articles", []):
                if not a.get("title") or a["title"] == "[Removed]":
                    continue
                items.append({
                    "title": a["title"].strip(),
                    "url": a["url"],
                    "source": (a.get("source") or {}).get("name", "News"),
                    "category": cat,
                    "published": iso(a.get("publishedAt", "")),
                })
        except Exception as e:
            print(f"[newsapi:{cat}] {e}")
        time.sleep(1)
    return items

def collect_rss():
    items = []
    for name, feed in RSS_FEEDS:
        try:
            root = ET.fromstring(http_get(feed))
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            entries = root.findall(".//item") or root.findall(".//atom:entry", ns)
            for e in entries[:8]:
                title = (e.findtext("title") or e.findtext("atom:title", namespaces=ns) or "").strip()
                link  = e.findtext("link") or ""
                if not link:
                    ln = e.find("atom:link", ns)
                    link = ln.get("href") if ln is not None else ""
                pub = (e.findtext("pubDate") or e.findtext("atom:published", namespaces=ns)
                       or e.findtext("atom:updated", namespaces=ns) or "")
                if title and link:
                    items.append({"title": title, "url": link.strip(), "source": name,
                                  "category": "ai", "published": iso(pub)})
        except Exception as ex:
            print(f"[rss:{name}] {ex}")
    return [i for i in items if recent(i["published"], hours=96)]  # labs post less often

def collect_youtube():
    items = []
    for handle in YOUTUBE_CHANNELS:
        try:
            ch = http_json("https://www.googleapis.com/youtube/v3/channels?" + urllib.parse.urlencode({
                "part": "contentDetails", "forHandle": handle, "key": YOUTUBE_KEY}))
            chans = ch.get("items", [])
            if not chans:
                print(f"[yt:{handle}] handle not found — check spelling")
                continue
            uploads = chans[0]["contentDetails"]["relatedPlaylists"]["uploads"]
            pl = http_json("https://www.googleapis.com/youtube/v3/playlistItems?" + urllib.parse.urlencode({
                "part": "snippet", "playlistId": uploads, "maxResults": 5, "key": YOUTUBE_KEY}))
            for v in pl.get("items", []):
                s = v["snippet"]
                vid = s["resourceId"]["videoId"]
                pub = iso(s["publishedAt"])
                if not recent(pub, hours=120):  # 5-day window for video
                    continue
                items.append({
                    "title": s["title"].strip(),
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "source": s["channelTitle"],
                    "thumbnail": (s.get("thumbnails", {}).get("medium") or {}).get("url", ""),
                    "category": "ai",
                    "published": pub,
                })
        except Exception as e:
            print(f"[yt:{handle}] {e}")
        time.sleep(0.5)
    return items

# ---------------- CLAUDE FILTER ----------------

FILTER_PROMPT = """You are the editorial filter for VURSYL, a relentlessly positive news hub covering AI, quantum computing, and emerging technology. Vursyl publishes ONLY positive, constructive, optimistic content: breakthroughs, milestones, launches, approvals, records, discoveries, and wins.

REJECT any item that is: doom-framed, fear-based, about layoffs, lawsuits, bans, failures, scandals, warnings, risks, "AI hitting a wall" takes, culture-war angles, or stock-drop news. Neutral technical explainers and factual announcements are ACCEPTED. When in doubt about tone, REJECT.

For each ACCEPTED item, assign exactly one category:
- ai (AI models, LLMs, research, agents, AI products)
- quantum (quantum computing, qubits, error correction)
- health (AI/tech in medicine, biotech, longevity, CRISPR, FDA approvals)
- robotics (robots, humanoids, drones, space, autonomous systems)
- compute (chips, GPUs, data centers, infrastructure, energy for compute)
- breakthroughs (exceptional cross-cutting milestone — reserve for the truly remarkable)

Also pick AT MOST ONE item overall as "hero": the single most exciting, positive, consequential item in the batch.

Respond ONLY with JSON, no markdown fences:
{"accepted":[{"i":<index>,"category":"<cat>","hero":<true|false>}]}

Items:
"""

def claude_filter(items):
    if not items:
        return []
    listing = "\n".join(f'{n}. [{i["source"]}] {i["title"]}' for n, i in enumerate(items))
    try:
        resp = post_json("https://api.anthropic.com/v1/messages", {
            "model": MODEL, "max_tokens": 4000,
            "messages": [{"role": "user", "content": FILTER_PROMPT + listing}],
        }, {"Content-Type": "application/json", "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01"})
        text = "".join(b.get("text", "") for b in resp.get("content", []))
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        decisions = json.loads(text).get("accepted", [])
    except Exception as e:
        print(f"[claude] filter failed: {e} — failing closed (no unfiltered publishing)")
        return []
    out = []
    for d in decisions:
        idx, cat = d.get("i"), d.get("category")
        if isinstance(idx, int) and 0 <= idx < len(items) and cat in CATEGORIES:
            item = dict(items[idx])
            item["category"] = cat
            if d.get("hero"):
                item["breakthrough"] = True
            out.append(item)
    return out

# ---------------- MAIN ----------------

def dedupe(items):
    seen, out = set(), []
    for i in items:
        key = hashlib.md5(i["title"].lower().encode()).hexdigest()
        if key not in seen:
            seen.add(key)
            out.append(i)
    return out

def main():
    os.makedirs("data", exist_ok=True)

    print("Collecting articles…")
    articles = dedupe(collect_newsapi() + collect_rss())
    print(f"  {len(articles)} candidates")
    news = claude_filter(articles)
    news.sort(key=lambda x: x["published"], reverse=True)
    heroes = [n for n in news if n.get("breakthrough")]
    for extra in heroes[1:]:
        extra.pop("breakthrough", None)
    news = news[:MAX_NEWS_ITEMS]
    print(f"  {len(news)} accepted")

    print("Collecting media…")
    videos = dedupe(collect_youtube())
    print(f"  {len(videos)} candidates")
    media = claude_filter(videos)
    for m in media:
        m.pop("breakthrough", None)
    media.sort(key=lambda x: x["published"], reverse=True)
    media = media[:MAX_MEDIA_ITEMS]
    print(f"  {len(media)} accepted")

    stamp = datetime.now(timezone.utc).isoformat()
    with open(OUT_NEWS, "w") as f:
        json.dump({"updated": stamp, "items": news}, f, indent=1)
    with open(OUT_MEDIA, "w") as f:
        json.dump({"updated": stamp, "items": media}, f, indent=1)
    print("Done — data/news.json + data/media.json written.")

if __name__ == "__main__":
    main()
