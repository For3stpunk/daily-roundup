#!/usr/bin/env python3
"""The Daily Roundup — collect the day's top articles per topic, then build the
static site (docs/) and its RSS feeds.

    python roundup.py            # collect today's issue and rebuild the site
    python roundup.py --rebuild  # rebuild the site from data/ without fetching
    python roundup.py --date 2026-11-14   # label the issue with a specific date

Config lives in sources.yml. Each issue is saved as data/YYYY-MM-DD.json.
"""
from __future__ import annotations

import argparse, concurrent.futures as cf, datetime as dt, email.utils, html, json, os, re, sys, time
from pathlib import Path
from urllib.parse import quote_plus, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser, requests, yaml

ROOT = Path(__file__).resolve().parent
DATA, DOCS = ROOT / "data", ROOT / "docs"
UA = "Mozilla/5.0 (compatible; DailyRoundupBot/1.0; +https://github.com/)"
NOW = dt.datetime.now(dt.timezone.utc)

# ---------------------------------------------------------------- helpers

def clean_text(s: str, limit: int = 280) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(re.sub(r"\s+", " ", s)).strip()
    if len(s) > limit:
        s = s[: limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return s

def canon_url(u: str) -> str:
    try:
        p = urlsplit(u)
        q = "&".join(x for x in p.query.split("&") if x and not x.startswith(("utm_", "ref=", "from=", "src=")))
        return urlunsplit((p.scheme, p.netloc.lower().removeprefix("www."), p.path.rstrip("/"), q, ""))
    except Exception:
        return u

def norm_title(t: str) -> str:
    t = re.sub(r"[‘’'\"“”]", "", t.lower())
    return re.sub(r"[^a-z0-9 ]+", " ", t).strip()

def tokens(t: str) -> set[str]:
    stop = {"the", "a", "an", "of", "and", "to", "in", "for", "on", "with", "is", "at", "by", "from", "new", "how"}
    return {w for w in norm_title(t).split() if len(w) > 2 and w not in stop}

def entry_time(e) -> dt.datetime | None:
    for k in ("published_parsed", "updated_parsed", "created_parsed"):
        v = e.get(k)
        if v:
            return dt.datetime(*v[:6], tzinfo=dt.timezone.utc)
    return None

def fetch(url: str) -> bytes:
    r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"}, timeout=25)
    r.raise_for_status()
    return r.content

# ---------------------------------------------------------------- collection

FEED_LINK = re.compile(r"<link\b[^>]*>", re.I)
COMMON_FEED_PATHS = ("feed/", "feed", "rss", "rss.xml", "atom.xml", "index.xml", "feeds/all")

def _parse(url: str):
    parsed = feedparser.parse(fetch(url))
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"not a feed ({str(parsed.get('bozo_exception',''))[:80]})")
    return parsed

def discover_feeds(src: dict) -> list[str]:
    """Candidate feed URLs when the configured one fails: <link rel=alternate>
    tags on the site's page (src['home'] or the feed URL's origin), then the
    usual WordPress/Ghost/Hugo feed paths."""
    p = urlsplit(src["url"])
    origin = f"{p.scheme}://{p.netloc}/"
    home = src.get("home") or origin
    cands = []
    try:
        page = fetch(home).decode("utf-8", "replace")
        for tag in FEED_LINK.findall(page):
            if re.search(r"rss|atom", tag, re.I) and re.search(r"alternate", tag, re.I) and not re.search(r"comment", tag, re.I):
                m = re.search(r'href=["\']([^"\']+)', tag)
                if m:
                    cands.append(requests.compat.urljoin(home, html.unescape(m.group(1))))
    except Exception:
        pass
    base = home if home.endswith("/") else home + "/"
    cands += [base + x for x in COMMON_FEED_PATHS] + [origin + x for x in COMMON_FEED_PATHS]
    seen, out = {src["url"]}, []
    for c in cands:
        if c not in seen:
            seen.add(c); out.append(c)
    return out[:12]

def collect_feed(src: dict, discover: bool = True) -> tuple[list[dict], str]:
    used, note = src["url"], ""
    try:
        parsed = _parse(used)
    except Exception as ex:
        first = f"{type(ex).__name__}: {str(ex)[:100]}"
        parsed = None
        if discover:
            for cand in discover_feeds(src):
                try:
                    parsed = _parse(cand)
                    if parsed.entries:
                        used, note = cand, f" (configured URL failed: {first}; found {cand})"
                        break
                    parsed = None
                except Exception:
                    continue
        if parsed is None:
            return [], f"error: {first}"
    out = []
    for e in parsed.entries:
        title, link = clean_text(e.get("title", ""), 220), e.get("link", "")
        if not title or not link:
            continue
        out.append({
            "title": title, "link": link, "source": src["name"],
            "published": (entry_time(e) or NOW).isoformat(), "dated": entry_time(e) is not None,
            "summary": clean_text(e.get("summary", "") or e.get("description", "")),
        })
    return out, f"ok: {len(out)} entries{note}"

def collect_google_news(query: str) -> tuple[list[dict], str]:
    url = f"https://news.google.com/rss/search?q={quote_plus(query + ' when:1d')}&hl=en-US&gl=US&ceid=US:en"
    items, status = collect_feed({"name": "Google News", "url": url}, discover=False)
    for it in items:
        # "Headline - Outlet" → headline + outlet as the source name
        m = re.match(r"^(.*)\s+-\s+([^-]{2,60})$", it["title"])
        if m:
            it["title"], it["source"] = m.group(1).strip(), m.group(2).strip()
        it["via"] = "Google News"
        it["summary"] = ""  # GN summaries just repeat the headline
    return items, status

def collect_hn(cfg: dict) -> tuple[list[dict], str]:
    try:
        r = requests.get("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=60", headers={"User-Agent": UA}, timeout=25)
        r.raise_for_status()
        hits = r.json().get("hits", [])
    except Exception as ex:
        return [], f"error: {type(ex).__name__}"
    out = []
    for h in hits:
        if (h.get("points") or 0) < cfg.get("min_points", 40):
            continue
        link = h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"
        out.append({
            "title": clean_text(h.get("title", ""), 220), "link": link,
            "source": f"Hacker News ({h.get('points', 0)} pts)", "published": h.get("created_at") or NOW.isoformat(),
            "dated": True, "summary": "", "points": h.get("points", 0),
            "discussion": f"https://news.ycombinator.com/item?id={h['objectID']}",
        })
    return out, f"ok: {len(out)} stories"

def gather(cfg: dict) -> tuple[dict[str, list[dict]], dict]:
    """Fetch every source once (feeds shared across topics are fetched once)."""
    jobs, status = {}, {}
    for t in cfg["topics"]:
        for f in t.get("feeds", []):
            jobs[("feed", f["url"])] = f
        for q in t.get("google_news", []):
            jobs[("gn", q)] = q
        if t.get("hn"):
            jobs[("hn", "front")] = t["hn"]
    results = {}
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        futs = {}
        for key, val in jobs.items():
            if key[0] == "feed":
                futs[ex.submit(collect_feed, val)] = key
            elif key[0] == "gn":
                futs[ex.submit(collect_google_news, val)] = key
            else:
                futs[ex.submit(collect_hn, val)] = key
        for fu in cf.as_completed(futs):
            key = futs[fu]
            items, st = fu.result()
            results[key] = items
            status[f"{key[0]}: {key[1]}"] = st
    return results, status

# ---------------------------------------------------------------- ranking

def build_topic(t: dict, results: dict, d: dict, window_h: int) -> list[dict]:
    cutoff = NOW - dt.timedelta(hours=window_h)
    pool = []
    def take(items, weight, cap, require=None, exclude=None, specific=False):
        for it in items:
            hay = (it["title"] + " " + it["link"]).lower()
            if require and not re.search(require, hay, re.I):
                continue
            if exclude and re.search(exclude, hay, re.I):
                continue
            pub = dt.datetime.fromisoformat(it["published"].replace("Z", "+00:00"))
            if pub < cutoff or pub > NOW + dt.timedelta(hours=6):
                continue
            pool.append({**it, "weight": weight, "cap": cap, "specific": specific, "pub": pub})
    for f in t.get("feeds", []):
        take(results.get(("feed", f["url"]), []), f.get("weight", 1.0), f.get("max", d["per_source_max"]),
             f.get("require"), f.get("exclude"), bool(f.get("require")))
    for q in t.get("google_news", []):
        take(results.get(("gn", q), []), d["google_news_weight"], 2)
    if t.get("hn"):
        take(results.get(("hn", "front"), []), t["hn"].get("weight", 1.2), t["hn"].get("max", 8))

    # merge duplicates (same URL, or near-identical headline) and count coverage
    merged: list[dict] = []
    for it in sorted(pool, key=lambda x: (-x["weight"], x["pub"])):
        tk, cu = tokens(it["title"]), canon_url(it["link"])
        for m in merged:
            if cu == m["curl"] or (tk and len(tk & m["tok"]) / max(1, len(tk | m["tok"])) >= 0.6):
                if it["source"] not in m["sources"]:
                    m["sources"].append(it["source"])
                if not m["summary"] and it["summary"]:
                    m["summary"] = it["summary"]
                m["specific"] = m["specific"] or it["specific"]
                break
        else:
            merged.append({**it, "curl": cu, "tok": tk, "sources": [it["source"]]})

    for m in merged:
        age_h = max(0.0, (NOW - m["pub"]).total_seconds() / 3600)
        recency = max(0.0, 1 - age_h / max(window_h, 24))
        pts = min(1.0, (m.get("points") or 0) / 500)
        m["score"] = round(m["weight"] + 0.5 * recency + 0.35 * (len(m["sources"]) - 1) + pts, 3)

    picked, per_src = [], {}
    for m in sorted(merged, key=lambda x: -x["score"]):
        key = m["source"].split(" (")[0]
        if per_src.get(key, 0) >= m["cap"]:
            continue
        per_src[key] = per_src.get(key, 0) + 1
        picked.append(m)
        if len(picked) >= d["per_topic"] * 2:  # extra candidates survive cross-topic dedupe
            break
    return picked

def make_issue(cfg: dict, date_label: str) -> dict:
    d = cfg["defaults"]
    results, status = gather(cfg)
    issue, seen = {"date": date_label, "generated": NOW.isoformat(), "topics": []}, {}
    for t in cfg["topics"]:
        picks = build_topic(t, results, d, d["window_hours"])
        if len(picks) < d["per_topic"]:
            picks = build_topic(t, results, d, d["fallback_window_hours"])
        issue["topics"].append({"slug": t["slug"], "title": t["title"], "items": picks})

    # an article appears under one topic only: the one whose source filter matched it specifically
    for ti, t in enumerate(issue["topics"]):
        for it in t["items"]:
            prev = seen.get(it["curl"])
            if prev is None or (it["specific"] and not prev[1]["specific"]):
                seen[it["curl"]] = (ti, it)
    for ti, t in enumerate(issue["topics"]):
        t["items"] = [
            {k: it[k] for k in ("title", "link", "source", "sources", "published", "summary", "score") if k in it}
            | ({"discussion": it["discussion"]} if it.get("discussion") else {})
            for it in t["items"] if seen[it["curl"]][0] == ti
        ][: cfg["defaults"]["per_topic"]]
    issue["status"] = dict(sorted(status.items()))
    return issue

# ---------------------------------------------------------------- output

def rfc822(iso: str) -> str:
    return email.utils.format_datetime(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")))

def esc(s) -> str:
    return html.escape(str(s or ""), quote=True)

def pretty_date(d: str) -> str:
    x = dt.date.fromisoformat(d)
    return x.strftime("%A, %B ") + str(x.day) + x.strftime(", %Y")

CSS = """
:root{--paper:#f6f5f1;--card:#fffefa;--ink:#16181d;--muted:#5f6470;--rule:#dcd9d0;--accent:#c2410c;--accent-soft:#fbe7dc;
--display:"Fraunces",Georgia,serif;--body:"Inter Tight","Helvetica Neue",Arial,sans-serif;--mono:"JetBrains Mono",ui-monospace,Menlo,monospace;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--paper:#121317;--card:#191b20;--ink:#ecebe6;--muted:#a0a3ab;--rule:#2c2e35;--accent:#fb923c;--accent-soft:#3a2418;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 var(--body)}
a{color:inherit}a:hover{color:var(--accent)}:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 64px}
.mast{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:end;gap:12px;border-bottom:3px double var(--ink);padding-bottom:14px}
.mast h1{font:600 clamp(2rem,5vw,3.4rem)/1 var(--display);margin:0;letter-spacing:-.01em}
.mast h1 a{text-decoration:none}.tag{color:var(--muted);margin:6px 0 0;max-width:60ch}
.meta{font:12px var(--mono);color:var(--muted);text-align:right}.meta a{margin-left:10px}
nav.topics{display:flex;flex-wrap:wrap;gap:6px;margin:16px 0 26px}
nav.topics a{font:12px var(--mono);text-decoration:none;border:1px solid var(--rule);background:var(--card);border-radius:99px;padding:4px 10px}
nav.topics a:hover{border-color:var(--accent)}nav.topics b{color:var(--accent);font-weight:500}
section{margin:0 0 40px}section h2{font:600 1.6rem/1.2 var(--display);margin:0 0 4px;display:flex;justify-content:space-between;align-items:baseline;gap:10px;border-bottom:1px solid var(--ink);padding-bottom:6px}
section h2 small{font:12px var(--mono);color:var(--muted);font-weight:400}section h2 small a{color:var(--muted)}
ol.items{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));column-gap:28px}
ol.items li{padding:12px 0;border-bottom:1px solid var(--rule);display:grid;grid-template-columns:28px 1fr;gap:8px}
.n{font:12px/1.9 var(--mono);color:var(--accent)}.t{font-weight:600;line-height:1.35;text-decoration:none}
.s{font:12px var(--mono);color:var(--muted);margin-top:3px}.s .multi{color:var(--accent)}
.sum{font-size:14px;color:var(--muted);margin:4px 0 0}
.archive li{padding:6px 0;border-bottom:1px solid var(--rule);list-style:none}.archive{padding:0}
footer{margin-top:40px;font:12px var(--mono);color:var(--muted)}
"""
FONTS = '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600&family=Inter+Tight:wght@400;600&family=JetBrains+Mono&display=swap" rel="stylesheet">'

def page(title: str, body: str, pub: dict, rel: str = "") -> str:
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>{FONTS}<link rel="stylesheet" href="{rel}style.css">
<link rel="alternate" type="application/rss+xml" title="{esc(pub['title'])} — all articles" href="{rel}feed.xml">
<link rel="alternate" type="application/rss+xml" title="{esc(pub['title'])} — daily issues" href="{rel}issues.xml"></head>
<body><div class="wrap">{body}<footer>Built automatically every morning from public RSS feeds, Google News and Hacker News. Headlines link to the original publishers.</footer></div></body></html>"""

def masthead(pub: dict, issue_date: str | None, rel: str = "") -> str:
    when = f"<div>{esc(pretty_date(issue_date))}</div>" if issue_date else ""
    return f"""<header class="mast"><div><h1><a href="{rel}index.html">{esc(pub['title'])}</a></h1><p class="tag">{esc(pub['tagline'])}</p></div>
<div class="meta">{when}<div><a href="{rel}archive.html">Archive</a><a href="{rel}feeds.html">RSS feeds</a></div></div></header>"""

def issue_html(issue: dict, pub: dict, rel: str = "") -> str:
    total = sum(len(t["items"]) for t in issue["topics"])
    nav = "".join(f'<a href="#{t["slug"]}">{esc(t["title"])} <b>{len(t["items"])}</b></a>' for t in issue["topics"])
    secs = []
    for t in issue["topics"]:
        lis = []
        for i, it in enumerate(t["items"], 1):
            srcs = it.get("sources") or [it["source"]]
            extra = f' <span class="multi">+{len(srcs)-1} more</span>' if len(srcs) > 1 else ""
            disc = f' · <a href="{esc(it["discussion"])}">discussion</a>' if it.get("discussion") else ""
            sm = f'<p class="sum">{esc(it["summary"])}</p>' if it.get("summary") else ""
            lis.append(f'<li><span class="n">{i:02d}</span><div><a class="t" href="{esc(it["link"])}">{esc(it["title"])}</a>'
                       f'<div class="s">{esc(srcs[0])}{extra}{disc}</div>{sm}</div></li>')
        secs.append(f'<section id="{t["slug"]}"><h2>{esc(t["title"])} <small>{len(t["items"])} · <a href="{rel}topics/{t["slug"]}.xml">RSS</a></small></h2>'
                    f'<ol class="items">{"".join(lis) or "<li>No articles found today.</li>"}</ol></section>')
    return masthead(pub, issue["date"], rel) + f'<nav class="topics" aria-label="Topics">{nav}</nav>' + "".join(secs) + \
        f'<p class="s">{total} articles across {len(issue["topics"])} topics.</p>'

def rss(pub: dict, title: str, link: str, desc: str, items: list[str], self_url: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>{esc(title)}</title><link>{esc(link)}</link><description>{esc(desc)}</description>
<language>{pub.get('language','en-us')}</language><lastBuildDate>{email.utils.format_datetime(NOW)}</lastBuildDate>
<atom:link href="{esc(self_url)}" rel="self" type="application/rss+xml"/><ttl>360</ttl>
{''.join(items)}</channel></rss>
"""

def article_item(it: dict, topic: dict, issue_date: str) -> str:
    srcs = it.get("sources") or [it["source"]]
    desc = esc(it.get("summary") or "") + (f" (Also covered by {esc(', '.join(srcs[1:]))}.)" if len(srcs) > 1 else "")
    return (f"<item><title>{esc(it['title'])}</title><link>{esc(it['link'])}</link>"
            f"<guid isPermaLink=\"false\">{esc(canon_url(it['link']))}</guid><pubDate>{rfc822(it['published'])}</pubDate>"
            f"<category>{esc(topic['title'])}</category><dc:creator>{esc(srcs[0])}</dc:creator>"
            f"<description>{desc}</description></item>")

def build_site(cfg: dict) -> None:
    pub = cfg["publication"]; base = pub["site_url"].rstrip("/")
    issues = [json.loads(p.read_text()) for p in sorted(DATA.glob("*.json"), reverse=True)]
    if not issues:
        sys.exit("No issues in data/ yet.")
    (DOCS / "issues").mkdir(parents=True, exist_ok=True); (DOCS / "topics").mkdir(exist_ok=True)
    (DOCS / "style.css").write_text(CSS); (DOCS / ".nojekyll").write_text("")
    latest = issues[0]
    (DOCS / "index.html").write_text(page(f"{pub['title']} — {pretty_date(latest['date'])}", issue_html(latest, pub), pub))
    for iss in issues[:120]:
        (DOCS / "issues" / f"{iss['date']}.html").write_text(page(f"{pub['title']} — {pretty_date(iss['date'])}", issue_html(iss, pub, "../"), pub, "../"))
    arch = "".join(f'<li><a href="issues/{i["date"]}.html">{esc(pretty_date(i["date"]))}</a> <span class="s">· {sum(len(t["items"]) for t in i["topics"])} articles</span></li>' for i in issues[:120])
    (DOCS / "archive.html").write_text(page(f"{pub['title']} — Archive", masthead(pub, None) + f'<h2>Archive</h2><ul class="archive">{arch}</ul>', pub))

    # all-articles feed: the latest 2 issues (Feedly dedupes on guid)
    items = [article_item(it, t, iss["date"]) for iss in issues[:2] for t in iss["topics"] for it in t["items"]]
    (DOCS / "feed.xml").write_text(rss(pub, pub["title"], base + "/", pub["tagline"], items, base + "/feed.xml"))
    # one feed per topic: last 7 issues
    for t in cfg["topics"]:
        its = [article_item(it, tt, iss["date"]) for iss in issues[:7] for tt in iss["topics"] if tt["slug"] == t["slug"] for it in tt["items"]]
        (DOCS / "topics" / f"{t['slug']}.xml").write_text(rss(pub, f"{pub['title']} — {t['title']}", f"{base}/#{t['slug']}", f"Top {t['title'].lower()} articles, daily.", its, f"{base}/topics/{t['slug']}.xml"))
    # issues feed: one item per day with the full digest
    iitems = []
    for iss in issues[:30]:
        body = issue_html(iss, pub, base + "/")
        when = dt.datetime.fromisoformat(iss["generated"]).isoformat()
        iitems.append(f"<item><title>{esc(pub['title'])} — {esc(pretty_date(iss['date']))}</title><link>{base}/issues/{iss['date']}.html</link>"
                      f"<guid isPermaLink=\"true\">{base}/issues/{iss['date']}.html</guid><pubDate>{rfc822(when)}</pubDate>"
                      f"<description>{sum(len(t['items']) for t in iss['topics'])} articles across {len(iss['topics'])} topics.</description>"
                      f"<content:encoded><![CDATA[{body.replace(']]>', ']]]]><![CDATA[>')}]]></content:encoded></item>")
    (DOCS / "issues.xml").write_text(rss(pub, f"{pub['title']} — Daily issues", base + "/", pub["tagline"], iitems, base + "/issues.xml"))
    rows = "".join(f'<li><b>{esc(t["title"])}</b> — <a href="topics/{t["slug"]}.xml">{base}/topics/{t["slug"]}.xml</a></li>' for t in cfg["topics"])
    feeds = (masthead(pub, None) + '<h2>RSS feeds</h2><p>Paste any of these into Feedly (Add Content → paste the URL).</p><ul class="archive">'
             f'<li><b>Everything</b> (one entry per article) — <a href="feed.xml">{base}/feed.xml</a></li>'
             f'<li><b>Daily issue</b> (one entry per day, full digest) — <a href="issues.xml">{base}/issues.xml</a></li>{rows}</ul>')
    (DOCS / "feeds.html").write_text(page(f"{pub['title']} — RSS feeds", feeds, pub))
    (DOCS / "status.json").write_text(json.dumps({"date": latest["date"], "generated": latest["generated"],
        "counts": {t["slug"]: len(t["items"]) for t in latest["topics"]}, "sources": latest.get("status", {})}, indent=1))

# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="rebuild docs/ from data/ without fetching")
    ap.add_argument("--date", help="issue date label (YYYY-MM-DD); default = today in the configured timezone")
    a = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "sources.yml").read_text())
    if not a.rebuild:
        tz = ZoneInfo(cfg["publication"].get("timezone", "UTC"))
        label = a.date or NOW.astimezone(tz).date().isoformat()
        DATA.mkdir(exist_ok=True)
        issue = make_issue(cfg, label)
        (DATA / f"{label}.json").write_text(json.dumps(issue, indent=1, ensure_ascii=False))
        print(f"{label}: " + ", ".join(f"{t['slug']} {len(t['items'])}" for t in issue["topics"]))
        bad = [k for k, v in issue["status"].items() if not v.startswith("ok")]
        if bad:
            print(f"{len(bad)} source(s) failed — see docs/status.json")
    build_site(cfg)

if __name__ == "__main__":
    main()
