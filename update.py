#!/usr/bin/env python3
"""Refresh the catalogue: discover feeds from the broadcasters' own indexes,
check every feed, update feeds.json/missing.json and render site/."""
import collections
import concurrent.futures as cf
import datetime as dt
import email.utils
import gzip
import html
import http.client
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import quoteattr

ROOT = Path(__file__).parent
SITE = ROOT / "site"
REPO_URL = "https://github.com/kmein/oerr-podcasts"
UA = f"Mozilla/5.0 (compatible; oerr-podcasts; +{REPO_URL})"
NOW = dt.datetime.now(dt.timezone.utc)
ACTIVE = NOW - dt.timedelta(days=365)
# a feed has to fail this many runs in a row before it is dropped, so that one
# bad night at a broadcaster does not empty its section
DROP_AFTER = 4
# deutschlandradio answers 403 after ~80 quick requests
SLOW_HOSTS = ("deutschlandfunk", "hoerspielundfeature", "kakadu.de")

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
ATOM = "{http://www.w3.org/2005/Atom}"
AUDIO_EXT = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac", ".mp2")
VIDEO_EXT = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".m3u8")

ARD_ORGS = {"Deutschlandradio": "Deutschlandradio", "ZDF": "ZDF", "phoenix": "ARD/ZDF", "KiKA": "ARD/ZDF", "funk": "ARD/ZDF"}
ORF_STATIONS = {
    "oe1": "Ö1", "oe3": "Ö3", "fm4": "FM4", "tv": "ORF TV", "orf": "ORF",
    "wie": "Radio Wien", "noe": "Radio NÖ", "bgl": "Radio Burgenland", "stm": "Radio Steiermark",
    "ktn": "Radio Kärnten", "ooe": "Radio Oberösterreich", "sbg": "Radio Salzburg",
    "tir": "Radio Tirol", "vbg": "Radio Vorarlberg",
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, data=None, headers=None, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept-Encoding": "gzip", **(headers or {})})
            with urllib.request.urlopen(req, timeout=30) as r:
                b = r.read()
                return gzip.decompress(b) if r.headers.get("Content-Encoding") == "gzip" else b
        except urllib.error.HTTPError as e:
            if e.code not in (403, 429, 500, 502, 503, 504) or i == tries - 1:
                raise
        except (OSError, http.client.HTTPException):
            if i == tries - 1:
                raise
        time.sleep(30 * (i + 1))


def get_json(url, **kw):
    return json.loads(get(url, **kw))


def key(url):
    return re.sub(r"^https?://(www\d?\.)?", "", url.strip().lower()).rstrip("/")


# discovery: only sources that list feed urls in a machine-readable way.
# everything else enters feeds.json by hand and is kept alive by the check.

def discover_ard():
    sets, off = [], 0
    while True:
        q = "{ programSets(first: 500, offset: %d) { nodes { coreId title lastItemAdded feedUrl feedUrl2 publicationService { title organizationName } } } }" % off
        nodes = get_json("https://api.ardaudiothek.de/graphql", data=json.dumps({"query": q}).encode(), headers={"Content-Type": "application/json"})["data"]["programSets"]["nodes"]
        sets += nodes
        off += 500
        if len(nodes) < 500:
            break
    cands = []
    for s in sets:
        ps = s["publicationService"] or {}
        url = next((u for u in (s["feedUrl"], s["feedUrl2"]) if u and u.startswith("http")), None)
        if url:
            cands.append(dict(broadcaster=ARD_ORGS.get(ps.get("organizationName"), "ARD"), station=ps.get("title") or "ARD", title=s["title"], rss=url, urn=s["coreId"]))
    return cands, {s["coreId"]: s for s in sets}


def discover_srg():
    cands = []
    for bu in ("srf", "rtr"):
        for kind in ("radio", "tv"):
            shows = get_json(f"https://il.srgssr.ch/integrationlayer/2.0/{bu}/showList/{kind}/alphabetical?pageSize=unlimited")["showList"]
            for s in shows:
                # sd and hd carry the same enclosures for audio shows
                url = s.get("podcastFeedSdUrl") or s.get("podcastFeedHdUrl")
                if url:
                    cands.append(dict(broadcaster="SRG SSR", station=bu.upper(), title=s["title"], rss=url))
    return cands


def discover_dradio():
    cands = []
    for page, station in (("https://www.deutschlandfunk.de/podcasts", "Deutschlandfunk"),
                          ("https://www.deutschlandfunkkultur.de/program-and-podcast", "Deutschlandfunk Kultur")):
        host = urllib.parse.urlparse(page).netloc
        for url in sorted(set(re.findall(r'href="(https://%s/[a-z0-9-]+\.xml)"' % re.escape(host), get(page).decode()))):
            cands.append(dict(broadcaster="Deutschlandradio", station=station, rss=url))
    nova = get("https://www.deutschlandfunknova.de/podcasts").decode()
    for slug in sorted(set(re.findall(r"/podcasts/download/([a-z0-9-]+)", nova))):
        cands.append(dict(broadcaster="Deutschlandradio", station="Deutschlandfunk Nova", rss=f"https://www.deutschlandfunknova.de/podcast/{slug}"))
    return cands


def discover_orf():
    cands = []
    for station, pods in get_json("https://audioapi.orf.at/radiothek/api/2.0/podcasts")["payload"].items():
        for p in pods:
            if p["urls"].get("feed"):
                cands.append(dict(broadcaster="ORF", station=ORF_STATIONS.get(station, station), title=p["title"], rss=p["urls"]["feed"]))
    return cands


# checking

def secs(s):
    s = (s or "").strip()
    try:
        if ":" in s:
            v = 0.0
            for part in s.split(":"):
                v = v * 60 + float(part)
            return v
        return float(s) if s else None
    except ValueError:
        return None


def pubdate(item):
    p = item.findtext("pubDate") or item.findtext(ATOM + "published") or item.findtext(ATOM + "updated")
    if not p:
        return None
    try:
        d = email.utils.parsedate_to_datetime(p)
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(p.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d if d <= NOW else None  # some feeds pre-announce episodes


def media_kind(enc):
    t = (enc.get("type") or "").lower()
    u = (enc.get("url") or enc.get("href") or "").lower().split("?")[0]
    if t.startswith("audio") or u.endswith(AUDIO_EXT):
        return "audio"
    if t.startswith("video") or u.endswith(VIDEO_EXT):
        return "video"
    return "other"


def plain(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def short(s):
    # first sentence only: enough to recognise a show, not a copy of the broadcaster's text
    s = plain(s)
    m = re.match(r"(.{30,160}?[.!?])(\s|$)", s)
    if m:
        return m.group(1)
    return s if len(s) <= 160 else s[:160].rsplit(" ", 1)[0] + " …"


def parse(body):
    root = ET.fromstring(body)
    ch = root.find("channel")
    if ch is not None:
        title, desc, items = ch.findtext("title"), ch.findtext("description") or ch.findtext(ITUNES + "summary"), ch.findall("item")
    elif root.tag == ATOM + "feed":
        title, desc, items = root.findtext(ATOM + "title"), root.findtext(ATOM + "subtitle"), root.findall(ATOM + "entry")
    else:
        raise ValueError("not a feed")
    eps = []
    for it in items:
        encs = [e.attrib for e in it.findall("enclosure")] + [l.attrib for l in it.findall(ATOM + "link") if l.get("rel") == "enclosure"]
        eps.append(dict(
            date=pubdate(it),
            kinds=[media_kind(e) for e in encs],
            urls=[re.sub(r"^https?://", "", (e.get("url") or e.get("href") or "").split("?")[0]) for e in encs],
            dur=secs(it.findtext(ITUNES + "duration")),
        ))
    if not eps:
        raise ValueError("no items")
    return dict(title=plain(title), description=short(desc), **stats(eps))


def stats(eps):
    dates = sorted(e["date"] for e in eps if e["date"])
    kinds = collections.Counter(k for e in eps for k in e["kinds"])
    durs = [e["dur"] for e in eps if e["dur"] and 0 < e["dur"] < 86400]
    recent = [d for d in dates if d >= ACTIVE]
    days = sorted({d.date() for d in recent})
    per_week, bulk = None, False
    if dates and dates[-1] >= ACTIVE:
        if len(recent) >= 10 and len(days) <= 2:
            gaps = [(b - a).total_seconds() for a, b in zip(recent, recent[1:])]
            # rolling hourly news vs. a whole series uploaded at once
            if statistics.median(gaps) >= 1200:
                per_week = 7.0
            else:
                bulk = True
        elif len(days) >= 3:
            per_week = min(7.0, (len(days) - 1) / max((days[-1] - days[0]).days, 1) * 7)
    return dict(
        last=dates[-1].date().isoformat() if dates else None,
        audio=kinds["audio"], video=kinds["video"],
        per_week=per_week, bulk=bulk,
        dur=statistics.mean(durs) if durs else None,
        dur_median=statistics.median(durs) if durs else None,
        dur_n=len(durs),
        encs={u for e in eps for u in e["urls"] if u},
    )


def check_all(urls):
    by_host = collections.defaultdict(list)
    for u in dict.fromkeys(urls):
        by_host[urllib.parse.urlparse(u).netloc].append(u)
    results = {}

    def run(host_urls):
        host = urllib.parse.urlparse(host_urls[0]).netloc
        gap = 1.5 if any(s in host for s in SLOW_HOSTS) else 0.3
        for u in host_urls:
            try:
                results[u] = parse(get(u))
            except Exception as e:  # any failure just means: not a working feed this run
                results[u] = f"{type(e).__name__}: {e}"[:200]
            time.sleep(gap)

    with cf.ThreadPoolExecutor(32) as ex:
        list(ex.map(run, by_host.values()))
    return results


# rendering

def fmt_dur(s):
    if s is None:
        return ""
    if s < 60:
        return f"{round(s)} s"
    m = round(s / 60)
    return f"{m // 60}:{m % 60:02d} h" if m >= 60 else f"{m} min"


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower().replace("ö", "oe").replace("ü", "ue").replace("ä", "ae")).strip("-")


def opml(title, rows):
    groups = collections.defaultdict(list)
    for f, _ in rows:
        groups[(f["broadcaster"], f["station"])].append(f)
    out = ['<?xml version="1.0" encoding="UTF-8"?>', f"<opml version=\"2.0\"><head><title>{html.escape(title)}</title></head><body>"]
    for (b, st), fs in groups.items():
        out.append(f"<outline text={quoteattr(b if st == b else f'{b} – {st}')}>")
        out += [f'<outline type="rss" text={quoteattr(f["title"])} title={quoteattr(f["title"])} xmlUrl={quoteattr(f["rss"])}/>' for f in fs]
        out.append("</outline>")
    out.append("</body></opml>")
    return "\n".join(out) + "\n"


PAGE = """<!doctype html><html lang=de><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>Podcasts der Öffentlich-Rechtlichen – DE/AT/CH</title>
<style>
body{font:14px system-ui,sans-serif;margin:1.5em}table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid #ddd;padding:4px 6px;vertical-align:top;text-align:left}
#feeds th{position:sticky;top:0;background:#f4f4f4;cursor:pointer}.d{color:#666;font-size:12px}
#feeds td:last-child{font-size:12px;word-break:break-all;max-width:28em}tr.old{opacity:.55}
#bar{display:flex;gap:1em;align-items:center;margin:.8em 0}input[type=search]{width:24em;padding:4px}
p,ul{max-width:70em}details{margin:.6em 0}summary{cursor:pointer}
</style>
<h1>Podcasts der Öffentlich-Rechtlichen – DE/AT/CH</h1>
<p>Offizielle RSS-Feeds von ARD, ZDF, Deutschlandradio, Deutscher Welle, ORF und SRG SSR, wöchentlich geprüft (Stand __DATE__).
Audio/Video zählt die Mediendateien, die gerade im Feed stehen – viele Sender kürzen ihre Feeds, das ist also nicht das ganze Archiv.
Erscheint = Tage mit neuer Folge pro Woche im letzten Jahr. Ø Dauer laut Feed (Maus drüber: Median). Grau = seit über einem Jahr keine neue Folge.</p>
<p>OPML zum Import: <a href=feeds.opml>alle</a>, <a href=feeds-aktiv.opml>nur aktive</a>, __OPML__ ·
<a href=feeds.json>JSON</a> · Fehlt ein Feed oder ist einer falsch? <a href=__REPO__>__REPO__</a></p>
<details><summary>__NMISSING__ ARD-Sendungen ohne offiziellen RSS-Feed (nur in ARD Sounds)</summary>
<table><thead><tr><th>Station<th>Sendung<th>Grund</thead><tbody>__MISSING__</tbody></table></details>
<div id=bar><input type=search id=q placeholder="Filter (Sender, Titel, Beschreibung …)" autofocus>
<label><input type=checkbox id=act> nur aktive</label><span id=n></span></div>
<table id=feeds><thead><tr><th>Anstalt<th>Station<th>Sendung<th>Letzte Folge<th>Audio<th>Video<th>Erscheint<th>Ø Dauer<th>RSS</thead><tbody>
__ROWS__</tbody></table>
<script>
const rs=[...document.querySelectorAll('#feeds tbody tr')],q=document.getElementById('q'),a=document.getElementById('act'),n=document.getElementById('n');
rs.forEach(r=>r._t=r.textContent.toLowerCase());
function f(){const t=q.value.toLowerCase().split(/\\s+/).filter(Boolean);let c=0;
rs.forEach(r=>{const v=t.every(w=>r._t.includes(w))&&(!a.checked||!r.classList.contains('old'));r.hidden=!v;c+=v});n.textContent=c+' Feeds'}
q.oninput=a.onchange=f;f();
document.querySelectorAll('#feeds th').forEach((th,i)=>th.onclick=()=>{const tb=th.closest('table').tBodies[0],d=th.dataset.d=th.dataset.d=='1'?'-1':'1';
[...tb.rows].sort((x,y)=>{const p=x.cells[i],s=y.cells[i];return(p.dataset.v!==undefined?p.dataset.v-s.dataset.v:p.textContent.localeCompare(s.textContent,'de',{numeric:true}))*d}).forEach(r=>tb.appendChild(r))});
</script>
"""


def render(rows, missing):
    SITE.mkdir(exist_ok=True)
    esc = html.escape
    trs = []
    for f, r in rows:
        old = not r["last"] or r["last"] < ACTIVE.date().isoformat()
        freq = "Archiv-Upload" if r["bulk"] else f"{r['per_week']:.1f}/Wo" if r["per_week"] is not None else ""
        dur_note = f"Median {fmt_dur(r['dur_median'])}, {r['dur_n']} Folgen mit Dauerangabe" if r["dur"] else ""
        trs.append(
            f"<tr{' class=old' if old else ''}><td>{esc(f['broadcaster'])}<td>{esc(f['station'])}"
            f"<td><b>{esc(f['title'])}</b><div class=d>{esc(r['description'])}</div>"
            f"<td>{r['last'] or ''}<td data-v={r['audio']}>{r['audio']}<td data-v={r['video']}>{r['video'] or ''}"
            f"<td data-v={r['per_week'] or 0:.3f}>{freq}"
            f"<td data-v={r['dur'] or 0:.0f} title={quoteattr(dur_note)}>{fmt_dur(r['dur'])}"
            f"<td><a href={quoteattr(f['rss'])}>{esc(f['rss'])}</a></tr>\n")
    broadcasters = sorted({f["broadcaster"] for f, _ in rows})
    page = (PAGE.replace("__DATE__", NOW.date().isoformat())
            .replace("__REPO__", REPO_URL)
            .replace("__OPML__", ", ".join(f"<a href=feeds-{slug(b)}.opml>{esc(b)}</a>" for b in broadcasters))
            .replace("__NMISSING__", str(len(missing)))
            .replace("__MISSING__", "".join(f"<tr><td>{esc(m['station'])}<td><a href={quoteattr(m['url'])}>{esc(m['title'])}</a><td>{esc(m['reason'])}</tr>" for m in missing))
            .replace("__ROWS__", "".join(trs)))
    (SITE / "index.html").write_text(page)
    active = [(f, r) for f, r in rows if r["last"] and r["last"] >= ACTIVE.date().isoformat()]
    (SITE / "feeds.opml").write_text(opml("Podcasts der Öffentlich-Rechtlichen", rows))
    (SITE / "feeds-aktiv.opml").write_text(opml("Podcasts der Öffentlich-Rechtlichen (aktiv)", active))
    for b in broadcasters:
        (SITE / f"feeds-{slug(b)}.opml").write_text(opml(f"Podcasts: {b}", [(f, r) for f, r in rows if f["broadcaster"] == b]))
    data = [{**f, **{k: v for k, v in r.items() if k != "encs"}} for f, r in rows]
    (SITE / "feeds.json").write_text(json.dumps(data, ensure_ascii=False, indent=0))


def dump(rows):
    return "[\n" + ",\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n]\n"


def main():
    feeds = json.loads((ROOT / "feeds.json").read_text())
    missing = json.loads((ROOT / "missing.json").read_text())
    known = {key(f["rss"]) for f in feeds}

    cands, ard_sets = [], None
    for name, fn in (("ard", discover_ard), ("srg", discover_srg), ("dradio", discover_dradio), ("orf", discover_orf)):
        try:
            got = fn()
            if name == "ard":
                got, ard_sets = got
            log(f"discover {name}: {len(got)} candidates")
            cands += got
        except Exception as e:
            log(f"discover {name} failed, keeping existing feeds: {e}")
    new = {}
    for c in cands:
        if key(c["rss"]) not in known:
            new.setdefault(key(c["rss"]), c)

    log(f"checking {len(feeds)} known feeds and {len(new)} candidates")
    results = check_all([f["rss"] for f in feeds] + [c["rss"] for c in new.values()])

    owner = {}  # enclosure url -> feed that published it

    def alias_of(r):
        # a url sharing most enclosures with a known feed is another address
        # for that feed; exact equality is too strict for feeds that change
        # between two fetches
        hits = collections.Counter(owner[u] for u in r["encs"] if u in owner)
        if hits:
            other, n = hits.most_common(1)[0]
            if n >= len(r["encs"]) / 2:
                return other
        return None

    def claim(f, r):
        for u in r["encs"]:
            owner.setdefault(u, f["rss"])

    kept, rows = [], []
    # hand-curated feeds claim their enclosures before auto-added ones, so an
    # alias that slipped in while its original was failing is removed again
    for f in sorted(feeds, key=lambda f: f.get("added", "")):
        r = results[f["rss"]]
        if isinstance(r, dict):
            if "added" in f and (other := alias_of(r)):
                log(f"drop {f['rss']} (alias of {other})")
                continue
            f.pop("fails", None)
            claim(f, r)
            rows.append((f, r))
        else:
            f["fails"] = f.get("fails", 0) + 1
            if f["fails"] >= DROP_AFTER:
                log(f"drop {f['rss']} ({r})")
                continue
            log(f"fail {f['fails']}/{DROP_AFTER} {f['rss']} ({r})")
        kept.append(f)
    for c in new.values():
        r = results[c["rss"]]
        if not isinstance(r, dict) or not (r["audio"] or r["video"]) or alias_of(r):
            continue
        f = dict(broadcaster=c["broadcaster"], station=c["station"], title=c.get("title") or r["title"], rss=c["rss"], added=NOW.date().isoformat())
        claim(f, r)
        log(f"add {f['rss']} ({f['title']})")
        kept.append(f)
        rows.append((f, r))

    if ard_sets is not None:
        ok = {key(u) for u, r in results.items() if isinstance(r, dict)}
        with_feed = {c["urn"] for c in cands if "urn" in c and key(c["rss"]) in ok}

        def still_missing(m):
            urn = re.search(r"urn:ard:show:[0-9a-f]+", m["url"]).group(0)
            s = ard_sets.get(urn)
            return s and urn not in with_feed and (s["lastItemAdded"] or "") >= ACTIVE.date().isoformat()
        missing = [m for m in missing if still_missing(m)]

    order = lambda f: (f["broadcaster"], f["station"], f["title"].lower())
    kept.sort(key=order)
    rows.sort(key=lambda fr: order(fr[0]))
    (ROOT / "feeds.json").write_text(dump(kept))
    (ROOT / "missing.json").write_text(dump(missing))
    render(rows, missing)
    log(f"{len(rows)} working feeds, {len(kept) - len(rows)} failing, {len(missing)} ARD shows without feed")


if __name__ == "__main__":
    main()
