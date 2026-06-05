"""
Job Alert Bot + Live Portal - RBCET CSE/AI Placement Cell  (v2)

Each run it:
  1. Searches Google News for the queries in queries.txt (categorised).
  2. Posts ONLY NEW items to the Telegram channel (push layer).
  3. Updates jobs.json and regenerates docs/index.html - a segmented,
     searchable portal served free by GitHub Pages (browse layer).

Env vars (GitHub repo Secrets):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID      Optional: DRY_RUN=1
"""

import hashlib
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

SEEN_FILE = "seen.json"
JOBS_FILE = "jobs.json"
PORTAL_FILE = os.path.join("docs", "index.html")
QUERIES_FILE = "queries.txt"
TELEGRAM_LINK = "https://t.me/rbcet_placements"  # edit to your channel link

MAX_AGE_DAYS = 15        # ignore news older than this when fetching
KEEP_DAYS = 45          # how long items stay on the portal
MAX_JOBS = 600
MAX_ITEMS_PER_QUERY = 8
MAX_SEEN = 8000
IST = timezone(timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ---- Relevance filter (editable) ----
# A title must contain at least one INCLUDE word and no EXCLUDE word.
INCLUDE_WORDS = ["off campus", "off-campus", "drive", "hiring", "registration",
                 "register", "recruitment", "recruit", "apply", "application",
                 "vacanc", "opening", "walk-in", "walkin", "internship",
                 "intern", "job", "career", "nqt", "hackathon", "freshers"]
EXCLUDE_WORDS = ["layoff", "laid off", "jobless", "shut down", "shutdown",
                 "shuts", "scam", "fraud", "arrested", "strike", "protest",
                 "report", "trends", "summit", "felicitated", "convocation",
                 "fest", "lose job", "loses job", "salary delay"]


def is_relevant(title):
    t = title.lower()
    if any(w in t for w in EXCLUDE_WORDS):
        return False
    return any(w in t for w in INCLUDE_WORDS)


def norm_title(title):
    return re.sub(r"[^a-z0-9]", "", title.lower())[:70]

CATEGORY_ORDER = ["Hiring Challenges", "Big Tech & GCC", "Services",
                  "Women-only", "General"]
CATEGORY_COLOR = {"Services": "#2e6e4e", "Hiring Challenges": "#b3541e",
                  "Big Tech & GCC": "#1f4e79", "Women-only": "#8a3a64",
                  "General": "#5a5247"}


def load_queries():
    """Return list of (category, query)."""
    out = []
    with open(QUERIES_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "::" in line:
                cat, q = line.split("::", 1)
                out.append((cat.strip() or "General", q.strip()))
            else:
                out.append(("General", line))
    return out


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def parse_rss(xml_bytes):
    items = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return items
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = item.findtext("pubDate")
        source = (item.findtext("source") or "").strip()
        published = None
        if pub:
            try:
                published = datetime.strptime(pub.strip(),
                                              "%a, %d %b %Y %H:%M:%S %Z")
                published = published.replace(tzinfo=timezone.utc)
            except ValueError:
                published = None
        if title and link:
            items.append({"title": title, "link": link,
                          "published": published, "source": source})
    return items


def is_fresh(item):
    if item["published"] is None:
        return True
    return datetime.now(timezone.utc) - item["published"] <= timedelta(days=MAX_AGE_DAYS)


def link_id(link):
    return hashlib.sha1(link.encode("utf-8")).hexdigest()


def clean_title(t):
    return re.sub(r"\s+", " ", t)[:220]


def telegram_send(text, token, chat_id, dry_run=False):
    if dry_run:
        print("---- DRY RUN MESSAGE ----")
        print(text)
        return True
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception as e:
        print(f"Telegram send failed: {e}", file=sys.stderr)
        return False


def chunk_messages(header, lines, limit=3500):
    current = header
    for line in lines:
        if len(current) + len(line) + 2 > limit:
            yield current
            current = header + " (contd.)\n" + line
        else:
            current += "\n" + line
    if current.strip():
        yield current


def age_label(iso, now):
    then = datetime.fromisoformat(iso)
    mins = int((now - then).total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 1)} min ago"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs} hr ago"
    return f"{hrs // 24} d ago"


def render_portal(jobs, now):
    new_cut = now - timedelta(hours=24)
    total = len(jobs)
    new24 = sum(1 for j in jobs
                if datetime.fromisoformat(j["first_seen"]) >= new_cut)
    updated = now.strftime("%d %b %Y, %I:%M %p IST")

    cats = [c for c in CATEGORY_ORDER
            if any(j["category"] == c for j in jobs)]
    chips = ['<button class="chip active" data-f="all">All</button>',
             '<button class="chip" data-f="new">New 24h</button>']
    chips += [f'<button class="chip" data-f="{html.escape(c)}">{html.escape(c)}</button>'
              for c in cats]

    sections = []
    for cat in cats:
        items = sorted([j for j in jobs if j["category"] == cat],
                       key=lambda j: j["first_seen"], reverse=True)
        cards = []
        for j in items:
            is_new = datetime.fromisoformat(j["first_seen"]) >= new_cut
            badge = '<span class="new">NEW</span>' if is_new else ""
            src = f'<span>{html.escape(j["source"])}</span> · ' if j["source"] else ""
            cards.append(
                f'<article class="card" data-cat="{html.escape(cat)}" '
                f'data-new="{1 if is_new else 0}">'
                f'<a href="{html.escape(j["link"])}" target="_blank" rel="noopener">'
                f'{html.escape(j["title"])}</a>{badge}'
                f'<div class="meta">{src}<span>spotted {age_label(j["first_seen"], now)}</span></div>'
                f'</article>')
        color = CATEGORY_COLOR.get(cat, "#5a5247")
        sections.append(
            f'<section class="cat" data-cat="{html.escape(cat)}" style="--cc:{color}">'
            f'<h2>{html.escape(cat)} <small>{len(items)}</small></h2>'
            + "".join(cards) + "</section>")

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RBCET Placement Board</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,700;9..144,900&family=IBM+Plex+Sans:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{{--paper:#f7f1e5;--ink:#1b2433;--accent:#d96f1e;--muted:#6f6757;--card:#fffdf7}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--paper);color:var(--ink);
 font:16px/1.5 "IBM Plex Sans",sans-serif;
 background-image:radial-gradient(#00000010 1px,transparent 1px);
 background-size:22px 22px}}
header{{border-bottom:3px double var(--ink);padding:26px 16px 14px;
 max-width:880px;margin:0 auto}}
h1{{font-family:Fraunces,serif;font-weight:900;font-size:clamp(30px,6vw,52px);
 margin:0;letter-spacing:-.02em}}
h1 em{{color:var(--accent);font-style:normal}}
.sub{{color:var(--muted);font-size:14px;margin-top:6px}}
.sub b{{color:var(--ink)}}
.bar{{position:sticky;top:0;background:var(--paper);z-index:5;
 border-bottom:1px solid #00000022;padding:10px 16px;max-width:880px;margin:0 auto}}
.chips{{display:flex;gap:8px;overflow-x:auto;padding-bottom:6px}}
.chip{{font:600 13px "IBM Plex Sans",sans-serif;border:1.5px solid var(--ink);
 background:transparent;color:var(--ink);padding:6px 12px;border-radius:999px;
 cursor:pointer;white-space:nowrap}}
.chip.active{{background:var(--ink);color:var(--paper)}}
#q{{width:100%;margin-top:8px;padding:9px 12px;border:1.5px solid var(--ink);
 border-radius:8px;background:var(--card);font:inherit}}
main{{max-width:880px;margin:0 auto;padding:8px 16px 40px}}
.cat h2{{font-family:Fraunces,serif;font-weight:700;font-size:22px;
 border-left:6px solid var(--cc);padding-left:10px;margin:26px 0 10px}}
.cat h2 small{{color:var(--muted);font:600 13px "IBM Plex Sans",sans-serif}}
.card{{background:var(--card);border:1px solid #00000018;border-left:4px solid var(--cc);
 border-radius:8px;padding:12px 14px;margin:8px 0;box-shadow:2px 2px 0 #00000010}}
.card a{{color:var(--ink);font-weight:600;text-decoration:none}}
.card a:hover{{text-decoration:underline;text-decoration-color:var(--accent)}}
.meta{{color:var(--muted);font-size:13px;margin-top:4px}}
.new{{background:var(--accent);color:#fff;font:700 11px "IBM Plex Sans",sans-serif;
 padding:2px 7px;border-radius:4px;margin-left:8px;vertical-align:2px}}
.hide{{display:none}}
footer{{max-width:880px;margin:0 auto;padding:18px 16px 50px;color:var(--muted);
 font-size:13px;border-top:3px double var(--ink)}}
footer a{{color:var(--accent)}}
</style></head><body>
<header>
 <h1>RBCET <em>Placement</em> Board</h1>
 <div class="sub">Dept. of CSE/AI · auto-refreshed 4&times; daily ·
  last update <b>{updated}</b> · <b>{total}</b> postings ·
  <b>{new24}</b> new in 24h</div>
</header>
<div class="bar">
 <div class="chips">{''.join(chips)}</div>
 <input id="q" type="search" placeholder="Search company or keyword&hellip;">
</div>
<main>{''.join(sections)}</main>
<footer>Always apply on the company's official career page (these links are
announcements, not application forms). Get instant push alerts on
<a href="{TELEGRAM_LINK}">Telegram</a>. Sources: public news feeds; drive dates
must be verified on official portals.</footer>
<script>
var f="all";
function apply(){{var q=document.getElementById("q").value.toLowerCase();
 document.querySelectorAll(".card").forEach(function(c){{
  var ok=(f==="all")||(f==="new"&&c.dataset.new==="1")||(c.dataset.cat===f);
  if(ok&&q)ok=c.textContent.toLowerCase().indexOf(q)>-1;
  c.classList.toggle("hide",!ok);}});
 document.querySelectorAll(".cat").forEach(function(s){{
  s.classList.toggle("hide",
   s.querySelectorAll(".card:not(.hide)").length===0);}});}}
document.querySelectorAll(".chip").forEach(function(ch){{
 ch.onclick=function(){{document.querySelectorAll(".chip")
  .forEach(function(x){{x.classList.remove("active")}});
  ch.classList.add("active");f=ch.dataset.f;apply();}}}});
document.getElementById("q").oninput=apply;
</script></body></html>"""


def main():
    dry_run = os.environ.get("DRY_RUN") == "1"
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not dry_run and (not token or not chat_id):
        print("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(IST)
    seen = load_json(SEEN_FILE, [])
    seen_set = set(seen)
    jobs = [j for j in load_json(JOBS_FILE, []) if is_relevant(j["title"])]
    titles_seen = {norm_title(j["title"]) for j in jobs}
    new_lines = []

    for category, query in load_queries():
        url = ("https://news.google.com/rss/search?q="
               + urllib.parse.quote(query) + "&hl=en-IN&gl=IN&ceid=IN:en")
        try:
            xml_bytes = fetch(url)
        except Exception as e:
            print(f"Fetch failed for '{query}': {e}", file=sys.stderr)
            continue
        count = 0
        for item in parse_rss(xml_bytes):
            if count >= MAX_ITEMS_PER_QUERY:
                break
            if not is_fresh(item):
                continue
            lid = link_id(item["link"])
            if lid in seen_set:
                continue
            seen_set.add(lid)
            seen.append(lid)
            title = clean_title(item["title"])
            nt = norm_title(title)
            if not is_relevant(title) or nt in titles_seen:
                continue
            titles_seen.add(nt)
            jobs.append({"id": lid, "title": title, "link": item["link"],
                         "source": item["source"], "category": category,
                         "first_seen": now.isoformat()})
            src = f" ({item['source']})" if item["source"] else ""
            new_lines.append(f"[{category}] {title}{src}\n{item['link']}")
            count += 1

    # prune portal data
    cutoff = now - timedelta(days=KEEP_DAYS)
    jobs = [j for j in jobs
            if datetime.fromisoformat(j["first_seen"]) >= cutoff][-MAX_JOBS:]

    os.makedirs("docs", exist_ok=True)
    with open(PORTAL_FILE, "w", encoding="utf-8") as fh:
        fh.write(render_portal(jobs, now))
    save_json(JOBS_FILE, jobs)

    if not new_lines:
        save_json(SEEN_FILE, seen[-MAX_SEEN:])
        print("No new postings; portal refreshed.")
        return

    header = f"NEW PLACEMENT ALERTS - {now.strftime('%d %b %Y, %I:%M %p')} IST"
    ok = True
    for msg in chunk_messages(header, new_lines):
        ok = telegram_send(msg, token, chat_id, dry_run) and ok
    if ok:
        save_json(SEEN_FILE, seen[-MAX_SEEN:])
        print(f"Sent {len(new_lines)} new item(s); portal refreshed.")
    else:
        print("Some sends failed; will retry next run.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
