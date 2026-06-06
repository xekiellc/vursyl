#!/usr/bin/env python3
"""
VURSYL feed pipeline
Generates:
  data/news.json         — positive news archive (append-only)
  data/media.json        — video/podcast archive (append-only)
  data/reality-check.json — auto-generated Reality Check entries

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
OUT_RC    = "data/reality-check.json"

MAX_NEWS_ITEMS  = 5000
MAX_MEDIA_ITEMS = 2000
MAX_RC_ITEMS    = 50
LOOKBACK_HOURS  = 48
MODEL_FILTER = "claude-haiku-4-5-20251001"
MODEL_RC     = "claude-sonnet-4-6"

CATEGORIES = ["ai", "quantum", "health", "robotics", "compute", "breakthroughs"]

RC_TAGS = {
  "energy":   ["energy","water","electricity","environment","carbon","emissions","power"],
  "jobs":     ["jobs","employment","unemployment","workers","labor","layoffs","replace"],
  "safety":   ["dangerous","risk","alignment","AGI","existential","regulation","ban","halt"],
  "health":   ["medical","hospital","diagnosis","FDA","clinical","patient","drug"],
  "quantum":  ["quantum","encryption","cryptography","qubits","supremacy"],
  "privacy":  ["privacy","data","surveillance","personal","tracking","GDPR"],
}

# ---------------- SOURCES ----------------

NEWSAPI_QUERIES = {
  "ai":       '"artificial intelligence" OR "large language model" OR LLM OR "AI model" OR "AI breakthrough"',
  "quantum":  '"quantum computing" OR "quantum computer" OR "quantum chip" OR "error correction" qubit',
  "health":   '"AI" AND (drug OR diagnosis OR cancer OR FDA OR clinical OR longevity OR CRISPR OR biotech)',
  "robotics": 'humanoid robot OR robotics OR SpaceX OR "space technology" OR autonomous',
  "compute":  'semiconductor OR "data center" OR GPU OR NVIDIA OR "chip breakthrough" OR supercomputer',
}

RC_QUERIES = [
  '"AI" AND (dangerous OR risk OR threat OR ban OR regulation OR harm OR replace OR jobs OR water OR energy)',
  '"artificial intelligence" AND (concern OR warning OR fear OR problem OR dangerous OR crisis)',
  '"data center" AND (water OR energy OR environment OR electricity OR bills)',
  '"quantum" AND (encryption OR security OR threat OR break OR hack)',
]

RSS_FEEDS = [
  ("OpenAI",            "https://openai.com/news/rss.xml"),
  ("Anthropic",         "https://www.anthropic.com/news/rss"),
  ("Google DeepMind",   "https://deepmind.google/blog/rss.xml"),
  ("NVIDIA",            "https://blogs.nvidia.com/feed/"),
  ("Microsoft Research","https://www.microsoft.com/en-us/research/feed/"),
  ("MIT News AI",       "https://news.mit.edu/topic/mitartificial-intelligence2-rss.xml"),
  ("IBM Research",      "https://research.ibm.com/blog/feed.rss"),
  ("NIH News",          "https://www.nih.gov/news-events/news-releases/feed.xml"),
]

YOUTUBE_CHANNELS = [
  "TwoMinutePapers","lexfridman","DwarkeshPatel","PeterDiamandis",
  "a16z","ycombinator","anthropic-ai","OpenAI","GoogleDeepMind",
  "NVIDIA","qiskit","TED",
]

# ---------------- HTTP HELPERS ----------------

def http_get(url, headers=None, timeout=30):
  req = urllib.request.Request(url, headers=headers or {"User-Agent":"VursylBot/1.0"})
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
  fmts = [
    "%Y-%m-%dT%H:%M:%SZ","%Y-%m-%dT%H:%M:%S%z",
    "%a, %d %b %Y %H:%M:%S %z","%a, %d %b %Y %H:%M:%S %Z",
    "%Y-%m-%dT%H:%M:%S.%fZ",
  ]
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
    d = datetime.fromisoformat(iso_str.replace("Z","+00:00"))
    return d > datetime.now(timezone.utc) - timedelta(hours=hours)
  except ValueError:
    return False

# ---------------- ARCHIVE MERGE ----------------

def load_existing(path):
  try:
    with open(path,"r") as f:
      data = json.load(f)
      return data.get("items",[])
  except (FileNotFoundError, json.JSONDecodeError):
    return []

def merge(existing, new_items, max_items):
  seen_urls, seen_titles, merged = set(), set(), []
  existing_by_url = {i["url"]:i for i in existing if i.get("url")}
  for item in new_items:
    url = item.get("url","")
    title_key = hashlib.md5(item.get("title","").lower().encode()).hexdigest()
    if url in seen_urls or title_key in seen_titles:
      continue
    if url in existing_by_url:
      if item.get("breakthrough"):
        existing_by_url[url]["breakthrough"] = True
      seen_urls.add(url); seen_titles.add(title_key)
      continue
    seen_urls.add(url); seen_titles.add(title_key)
    merged.append(item)
  for item in existing:
    url = item.get("url","")
    title_key = hashlib.md5(item.get("title","").lower().encode()).hexdigest()
    if url in seen_urls or title_key in seen_titles:
      if url not in seen_urls:
        merged.append(item)
      continue
    seen_urls.add(url); seen_titles.add(title_key)
    merged.append(item)
  hero_found = False
  for item in merged:
    if item.get("breakthrough"):
      if hero_found:
        item.pop("breakthrough",None)
      else:
        hero_found = True
  return merged[:max_items]

# ---------------- COLLECTORS ----------------

def collect_newsapi():
  items = []
  frm = (datetime.now(timezone.utc)-timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")
  for cat, q in NEWSAPI_QUERIES.items():
    url = ("https://newsapi.org/v2/everything?"+urllib.parse.urlencode({
      "q":q,"from":frm,"language":"en","sortBy":"publishedAt","pageSize":25
    })+f"&apiKey={NEWSAPI_KEY}")
    try:
      for a in http_json(url).get("articles",[]):
        if not a.get("title") or a["title"]=="[Removed]":
          continue
        items.append({
          "title":a["title"].strip(),"url":a["url"],
          "source":(a.get("source") or {}).get("name","News"),
          "category":cat,"published":iso(a.get("publishedAt","")),
        })
    except Exception as e:
      print(f"[newsapi:{cat}] {e}")
    time.sleep(1)
  return items

def collect_rc_candidates():
  """Collect potentially doom-framed headlines for Reality Check processing."""
  items = []
  frm = (datetime.now(timezone.utc)-timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%S")
  for q in RC_QUERIES:
    url = ("https://newsapi.org/v2/everything?"+urllib.parse.urlencode({
      "q":q,"from":frm,"language":"en","sortBy":"popularity","pageSize":15
    })+f"&apiKey={NEWSAPI_KEY}")
    try:
      for a in http_json(url).get("articles",[]):
        if not a.get("title") or a["title"]=="[Removed]":
          continue
        items.append({
          "title":a["title"].strip(),
          "url":a["url"],
          "source":(a.get("source") or {}).get("name","News"),
          "published":iso(a.get("publishedAt","")),
        })
    except Exception as e:
      print(f"[newsapi:rc] {e}")
    time.sleep(1)
  seen, deduped = set(), []
  for i in items:
    key = hashlib.md5(i["title"].lower().encode()).hexdigest()
    if key not in seen:
      seen.add(key); deduped.append(i)
  return deduped[:30]

def collect_rss():
  items = []
  for name, feed in RSS_FEEDS:
    try:
      root = ET.fromstring(http_get(feed))
      ns = {"atom":"http://www.w3.org/2005/Atom"}
      entries = root.findall(".//item") or root.findall(".//atom:entry",ns)
      for e in entries[:8]:
        title = (e.findtext("title") or e.findtext("atom:title",namespaces=ns) or "").strip()
        link  = e.findtext("link") or ""
        if not link:
          ln = e.find("atom:link",ns)
          link = ln.get("href") if ln is not None else ""
        pub = (e.findtext("pubDate") or e.findtext("atom:published",namespaces=ns)
               or e.findtext("atom:updated",namespaces=ns) or "")
        if title and link:
          items.append({"title":title,"url":link.strip(),"source":name,
                        "category":"ai","published":iso(pub)})
    except Exception as ex:
      print(f"[rss:{name}] {ex}")
  return [i for i in items if recent(i["published"],hours=96)]

def collect_youtube():
  items = []
  for handle in YOUTUBE_CHANNELS:
    try:
      ch = http_json("https://www.googleapis.com/youtube/v3/channels?"+urllib.parse.urlencode({
        "part":"contentDetails","forHandle":handle,"key":YOUTUBE_KEY}))
      chans = ch.get("items",[])
      if not chans:
        print(f"[yt:{handle}] handle not found")
        continue
      uploads = chans[0]["contentDetails"]["relatedPlaylists"]["uploads"]
      pl = http_json("https://www.googleapis.com/youtube/v3/playlistItems?"+urllib.parse.urlencode({
        "part":"snippet","playlistId":uploads,"maxResults":5,"key":YOUTUBE_KEY}))
      for v in pl.get("items",[]):
        s = v["snippet"]
        vid = s["resourceId"]["videoId"]
        pub = iso(s["publishedAt"])
        if not recent(pub,hours=120):
          continue
        items.append({
          "title":s["title"].strip(),
          "url":f"https://www.youtube.com/watch?v={vid}",
          "source":s["channelTitle"],
          "thumbnail":(s.get("thumbnails",{}).get("medium") or {}).get("url",""),
          "category":"ai","published":pub,
        })
    except Exception as e:
      print(f"[yt:{handle}] {e}")
    time.sleep(0.5)
  return items

# ---------------- CLAUDE FILTER ----------------

FILTER_PROMPT = """You are the editorial filter for VURSYL, a relentlessly positive news hub covering AI, quantum computing, and emerging technology. Vursyl publishes ONLY positive, constructive, optimistic content: breakthroughs, milestones, launches, approvals, records, discoveries, and wins.

REJECT any item that is: doom-framed, fear-based, about layoffs, lawsuits, bans, failures, scandals, warnings, risks, culture-war angles, or stock-drop news. Neutral technical explainers and factual announcements are ACCEPTED. When in doubt, REJECT.

For each ACCEPTED item assign exactly one category:
- ai (AI models, LLMs, research, agents, products)
- quantum (quantum computing, qubits, error correction)
- health (AI/tech in medicine, biotech, longevity, CRISPR, FDA)
- robotics (robots, humanoids, drones, space, autonomous)
- compute (chips, GPUs, data centers, infrastructure)
- breakthroughs (exceptional cross-cutting milestone — reserve for the truly remarkable)

Pick AT MOST ONE item as "hero": the single most exciting positive item in the batch.

Respond ONLY with JSON, no markdown fences:
{"accepted":[{"i":<index>,"category":"<cat>","hero":<true|false>}]}

Items:
"""

def claude_filter(items):
  if not items:
    return []
  listing = "\n".join(f'{n}. [{i["source"]}] {i["title"]}' for n,i in enumerate(items))
  try:
    resp = post_json("https://api.anthropic.com/v1/messages",{
      "model":MODEL_FILTER,"max_tokens":4000,
      "messages":[{"role":"user","content":FILTER_PROMPT+listing}],
    },{"Content-Type":"application/json","x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01"})
    text = "".join(b.get("text","") for b in resp.get("content",[]))
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    decisions = json.loads(text).get("accepted",[])
  except Exception as e:
    print(f"[claude:filter] {e} — failing closed")
    return []
  out = []
  for d in decisions:
    idx,cat = d.get("i"),d.get("category")
    if isinstance(idx,int) and 0<=idx<len(items) and cat in CATEGORIES:
      item = dict(items[idx])
      item["category"] = cat
      if d.get("hero"):
        item["breakthrough"] = True
      out.append(item)
  return out

# ---------------- REALITY CHECK GENERATOR ----------------

RC_SYSTEM = """You are the Reality Check editor for VURSYL — a pro-technology, pro-AI, evidence-first publication. Your job is to take viral negative or misleading claims about AI, quantum computing, data centers, and emerging technology, and produce clear, sourced, confident rebuttals.

VURSYL editorial standards:
- Steelman the concern first (acknowledge what is real about it)
- Then present what the data actually shows (be specific, cite real numbers where possible)
- End with the bigger picture (positive trajectory, context, or what the concern misses)
- Tone: confident, pro-tech, never dismissive, always evidence-based
- Never use weasel words. State positions clearly.
- Length: 3 short paragraphs maximum per section"""

RC_PROMPT = """Analyze these recent headlines for potential Reality Check entries. Identify the 2-3 most viral, misleading, or fear-based claims that Vursyl should rebut with evidence.

For each claim, produce a Reality Check entry in this exact JSON structure:
{{
  "claim": "The exact claim or fear as a person might say it",
  "verdict": "Busted" | "Misleading" | "Partial",
  "verdict_summary": "One sentence summary of what the data shows",
  "tag": "energy" | "jobs" | "safety" | "health" | "quantum" | "privacy",
  "icon": "⚡" | "💼" | "🤖" | "🏥" | "⚛️" | "🔒",
  "what_is_true": "One paragraph — steelman the concern, acknowledge the real kernel",
  "what_data_shows": "One paragraph — the actual evidence, specific numbers, named studies",
  "bigger_picture": "One paragraph — positive trajectory, context, what the claim misses",
  "data_points": [
    {{"n": "statistic or number", "l": "what it means"}},
    {{"n": "statistic or number", "l": "what it means"}},
    {{"n": "statistic or number", "l": "what it means"}}
  ],
  "tags": ["tag1", "tag2"]
}}

Headlines to analyze:
{headlines}

Respond ONLY with a JSON array of 2-3 entries. No markdown fences. No preamble."""

def generate_reality_checks(candidates):
  if not candidates:
    print("[rc] no candidates")
    return []
  headlines = "\n".join(f'{i+1}. [{c["source"]}] {c["title"]}' for i,c in enumerate(candidates[:20]))
  try:
    resp = post_json("https://api.anthropic.com/v1/messages",{
      "model":MODEL_RC,"max_tokens":4000,
      "system":RC_SYSTEM,
      "messages":[{"role":"user","content":RC_PROMPT.format(headlines=headlines)}],
    },{"Content-Type":"application/json","x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01"})
    text = "".join(b.get("text","") for b in resp.get("content",[]))
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    entries = json.loads(text)
    if not isinstance(entries,list):
      entries = [entries]
    now = datetime.now(timezone.utc).isoformat()
    for e in entries:
      e["generated"] = now
      e["auto"] = True
    print(f"[rc] generated {len(entries)} entries")
    return entries
  except Exception as ex:
    print(f"[claude:rc] {ex}")
    return []

def merge_rc(existing, new_entries):
  """Keep newest MAX_RC_ITEMS, dedupe by claim text."""
  seen = set()
  merged = []
  for e in new_entries + existing:
    key = hashlib.md5(e.get("claim","").lower().encode()).hexdigest()
    if key not in seen:
      seen.add(key)
      merged.append(e)
  return merged[:MAX_RC_ITEMS]

def load_rc(path):
  try:
    with open(path,"r") as f:
      data = json.load(f)
      return data.get("entries",[])
  except (FileNotFoundError, json.JSONDecodeError):
    return []

# ---------------- DEDUPE ----------------

def dedupe(items):
  seen, out = set(), []
  for i in items:
    key = hashlib.md5(i["title"].lower().encode()).hexdigest()
    if key not in seen:
      seen.add(key); out.append(i)
  return out

# ---------------- MAIN ----------------

def main():
  os.makedirs("data",exist_ok=True)

  existing_news  = load_existing(OUT_NEWS)
  existing_media = load_existing(OUT_MEDIA)
  existing_rc    = load_rc(OUT_RC)
  print(f"Archive: {len(existing_news)} news, {len(existing_media)} media, {len(existing_rc)} RC entries")

  print("Collecting articles…")
  raw_articles = dedupe(collect_newsapi()+collect_rss())
  print(f"  {len(raw_articles)} candidates")
  new_news = claude_filter(raw_articles)
  new_news.sort(key=lambda x:x["published"],reverse=True)
  print(f"  {len(new_news)} accepted")

  print("Collecting media…")
  raw_media = dedupe(collect_youtube())
  print(f"  {len(raw_media)} candidates")
  new_media = claude_filter(raw_media)
  for m in new_media:
    m.pop("breakthrough",None)
  new_media.sort(key=lambda x:x["published"],reverse=True)
  print(f"  {len(new_media)} accepted")

  print("Collecting Reality Check candidates…")
  rc_candidates = collect_rc_candidates()
  print(f"  {len(rc_candidates)} candidates")
  new_rc = generate_reality_checks(rc_candidates)

  all_news  = merge(existing_news,  new_news,  MAX_NEWS_ITEMS)
  all_media = merge(existing_media, new_media, MAX_MEDIA_ITEMS)
  all_rc    = merge_rc(existing_rc, new_rc)

  stamp = datetime.now(timezone.utc).isoformat()

  with open(OUT_NEWS,"w") as f:
    json.dump({"updated":stamp,"items":all_news},f,indent=1)
  with open(OUT_MEDIA,"w") as f:
    json.dump({"updated":stamp,"items":all_media},f,indent=1)
  with open(OUT_RC,"w") as f:
    json.dump({"updated":stamp,"entries":all_rc},f,indent=1)

  print(f"Done — {len(all_news)} news / {len(all_media)} media / {len(all_rc)} RC entries")
  print(f"  New this run: {len(new_news)} news / {len(new_media)} media / {len(new_rc)} RC entries")

if __name__ == "__main__":
  main()
