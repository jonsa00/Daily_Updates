#!/usr/bin/env python3
"""Fetch the RSS/Atom feeds listed in sources.json and build dashboard.html.

Standard library only. Run manually with:  python3 build_dashboard.py
A feed that fails falls back to its last good copy in cache.json.
"""
import concurrent.futures
import email.utils
import html
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / "sources.json"
CACHE = ROOT / "cache.json"
IMAGE_CACHE = ROOT / "images.json"
OUTPUT = Path(os.environ.get("DASHBOARD_OUT", ROOT / "dashboard.html"))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) DailyUpdatesDashboard/1.0"
TIMEOUT = 20

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "rss1": "http://purl.org/rss/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "media": "http://search.yahoo.com/mrss/",
}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl.create_default_context()) as r:
            return r.read()
    except urllib.error.URLError as e:
        if isinstance(e, urllib.error.HTTPError):
            raise
        # macOS system Python's old TLS stack can't reach some servers; curl can.
        return subprocess.run(["curl", "-sfL", "-A", UA, "-m", str(TIMEOUT), url],
                              check=True, capture_output=True).stdout


def text(el):
    return (el.text or "").strip() if el is not None else ""


def clean(s, limit=280):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "…"


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        d = None
    if d is None:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)", re.I)
OG_RE = re.compile(r"<meta[^>]+(?:property|name)=[\"'](?:og:image|twitter:image)(?::src)?[\"'][^>]*>", re.I)
CONTENT_RE = re.compile(r"content=[\"']([^\"']+)", re.I)


def first_image(item, *blobs):
    for tag in ("media:content", "media:thumbnail"):
        for m in item.findall(tag, NS):
            if m.get("url") and (m.get("medium") in (None, "image") or tag == "media:thumbnail"):
                return m.get("url")
    enc = item.find("enclosure")
    if enc is not None and (enc.get("type") or "").startswith("image"):
        return enc.get("url")
    for b in blobs:
        m = IMG_RE.search(b or "")
        if m and m.group(1).startswith("http") and not re.search(r"pixel|feeds\.feedburner|1x1|tracking", m.group(1)):
            return html.unescape(m.group(1))
    return None


def page_image(url):
    """Preview image (og:image) from the article's own page, or "" if none."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10, context=ssl.create_default_context()) as r:
            head = r.read(300000).decode("utf-8", "ignore")
    except Exception:
        try:
            head = subprocess.run(["curl", "-sfL", "-A", UA, "-m", "10", url], capture_output=True,
                                  timeout=15).stdout[:300000].decode("utf-8", "ignore")
        except Exception:
            return ""
    for tag in OG_RE.findall(head):
        m = CONTENT_RE.search(tag)
        if m and m.group(1).startswith("http"):
            return html.unescape(m.group(1))
    return ""


def parse_feed(raw):
    root = ET.fromstring(raw)
    items = []
    if root.tag.endswith("rss") or root.find("channel") is not None:
        nodes = root.iter("item")
        for it in nodes:
            desc = text(it.find("description")) or text(it.find("content:encoded", NS))
            full = text(it.find("content:encoded", NS))
            items.append({
                "title": clean(text(it.find("title")), 300),
                "link": text(it.find("link")) or text(it.find("guid")),
                "summary": clean(desc),
                "date": parse_date(text(it.find("pubDate")) or text(it.find("dc:date", NS))),
                "image": first_image(it, desc, full),
            })
    elif root.tag == "{%s}feed" % NS["atom"]:
        for e in root.findall("atom:entry", NS):
            link = ""
            for l in e.findall("atom:link", NS):
                if l.get("rel") in (None, "alternate"):
                    link = l.get("href", "")
                    break
            desc = text(e.find("atom:summary", NS)) or text(e.find("atom:content", NS))
            items.append({
                "title": clean(text(e.find("atom:title", NS)), 300),
                "link": link,
                "summary": clean(desc),
                "date": parse_date(text(e.find("atom:published", NS)) or text(e.find("atom:updated", NS))),
                "image": first_image(e, desc),
            })
    else:  # RSS 1.0 / RDF
        for it in root.findall("rss1:item", NS):
            items.append({
                "title": clean(text(it.find("rss1:title", NS)), 300),
                "link": text(it.find("rss1:link", NS)),
                "summary": clean(text(it.find("rss1:description", NS))),
                "date": parse_date(text(it.find("dc:date", NS))),
                "image": None,
            })
    return [i for i in items if i["title"] and i["link"]]


def load_source(src, cfg, cache):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=cfg.get("max_age_hours", 48))
    try:
        items = parse_feed(fetch(src["url"]))
        status = "ok"
    except Exception as e:  # network, HTTP, or XML error: use last good copy
        cached = cache.get(src["url"], [])
        items = [dict(i, date=parse_date(i["date"])) for i in cached]
        status = "cached: %s" % type(e).__name__ if cached else "failed: %s" % e
    for i in items:
        # Some feeds label local times as UTC, which puts stories in the future; shift back whole hours.
        if i["date"] and i["date"] > now + timedelta(minutes=10):
            i["date"] -= timedelta(hours=-(-(i["date"] - now).total_seconds() // 3600))
    fresh = [i for i in items if i["date"] is None or i["date"] >= cutoff]
    fresh.sort(key=lambda i: i["date"] or now, reverse=True)
    return src, fresh[: cfg.get("max_items_per_source", 8)], status


def fetch_quote(ind):
    """Latest price, day change and 1-month daily closes for one symbol (Yahoo Finance chart API)."""
    sym = urllib.parse.quote(ind["symbol"])
    last_err = None
    for host in ("query1", "query2"):
        try:
            raw = fetch("https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1mo&interval=1d" % (host, sym))
            res = json.loads(raw)["chart"]["result"][0]
            break
        except Exception as e:
            last_err = e
    else:
        raise last_err
    meta = res["meta"]
    bars = [(t, c) for t, c in zip(res.get("timestamp", []), res["indicators"]["quote"][0]["close"]) if c is not None]
    price, at = meta["regularMarketPrice"], meta.get("regularMarketTime")
    off = meta.get("gmtoffset", 0)
    day = lambda ts: (ts + off) // 86400
    # The last daily bar is today's session when it shares the quote's local date.
    if bars and at and day(bars[-1][0]) == day(at):
        bars[-1] = (bars[-1][0], price)
        prev = bars[-2][1] if len(bars) > 1 else None
    else:
        prev = bars[-1][1] if bars else None
        bars.append((at, price))
    closes = [c for _, c in bars]
    return {
        "name": ind["name"], "symbol": ind["symbol"], "unit": ind.get("unit", ""),
        "currency": meta.get("currency"), "price": price, "prev": prev,
        "change": price - prev if prev else None,
        "pct": (price / prev - 1) * 100 if prev else None,
        "month_pct": (price / closes[0] - 1) * 100 if closes and closes[0] else None,
        "time": datetime.fromtimestamp(at, timezone.utc).isoformat() if at else None,
        "spark": [round(c, 4) for c in closes],
    }


def load_markets(cfg, cache):
    """Quotes grouped as in sources.json; a symbol that fails keeps its last good quote, marked stale."""
    inds = [i for g in cfg.get("indicators", []) for i in g["items"]]
    quotes, report = {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futs = {pool.submit(fetch_quote, i): i for i in inds}
        for f in concurrent.futures.as_completed(futs):
            ind = futs[f]
            try:
                quotes[ind["symbol"]] = f.result()
                status = "ok"
            except Exception as e:
                old = cache.get("quote:" + ind["symbol"])
                if old:
                    quotes[ind["symbol"]] = dict(old, stale=True)
                status = ("cached: " if old else "failed: ") + type(e).__name__
            report.append(("Indicators", ind["name"], status, 1 if ind["symbol"] in quotes else 0))
    groups = [{"group": g["group"], "items": [quotes[i["symbol"]] for i in g["items"] if i["symbol"] in quotes]}
              for g in cfg.get("indicators", [])]
    return groups, quotes, report


def build():
    log = ROOT / "refresh.log"  # hourly runs append here; keep it from growing forever
    if log.exists() and log.stat().st_size > 500_000:
        log.write_text("")
    cfg = json.loads(SOURCES.read_text())
    try:
        cache = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        cache = {}

    jobs = [(sec, src) for sec in cfg["sections"] for src in sec["sources"]]
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futs = {pool.submit(load_source, src, cfg, cache): (sec["id"], src["url"]) for sec, src in jobs}
        for f in concurrent.futures.as_completed(futs):
            results[futs[f]] = f.result()

    # Fill in missing pictures from article pages; remember the ones found between runs.
    try:
        images = json.loads(IMAGE_CACHE.read_text())
    except (OSError, ValueError):
        images = {}
    missing = {i["link"] for _, items, _ in results.values() for i in items
               if not i["image"] and i["link"] not in images}
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for link, img in zip(missing, pool.map(page_image, missing)):
            if img:
                images[link] = img
    live = set()
    for _, items, _ in results.values():
        for i in items:
            live.add(i["link"])
            i["image"] = i["image"] or images.get(i["link"]) or None
    IMAGE_CACHE.write_text(json.dumps({k: v for k, v in images.items() if k in live}))

    markets, quotes, report = load_markets(cfg, cache)
    new_cache, sections = {"quote:" + k: v for k, v in quotes.items() if not v.get("stale")}, []
    for k, v in cache.items():  # keep last good quotes for symbols that failed this run
        if k.startswith("quote:") and k not in new_cache:
            new_cache[k] = v
    for sec in cfg["sections"]:
        seen, articles = set(), []
        for src in sec["sources"]:
            _, items, status = results[(sec["id"], src["url"])]
            report.append((sec["title"], src["name"], status, len(items)))
            if status == "ok":
                new_cache[src["url"]] = [dict(i, date=i["date"].isoformat() if i["date"] else None) for i in items]
            elif src["url"] in cache:
                new_cache[src["url"]] = cache[src["url"]]
            for i in items:
                key = i["link"].split("?")[0]
                if key in seen:
                    continue
                seen.add(key)
                articles.append({
                    "title": i["title"], "link": i["link"], "image": i["image"],
                    "summary": "" if i["summary"].startswith("Article URL:") else i["summary"],
                    "date": i["date"].isoformat() if i["date"] else None,
                    "source": src["name"], "kind": src.get("kind", "mainstream"),
                })
        articles.sort(key=lambda a: a["date"] or "", reverse=True)
        sections.append({"id": sec["id"], "title": sec["title"], "layout": sec.get("layout"), "dir": sec.get("dir"), "board": sec.get("board", False), "sources": [s["name"] for s in sec["sources"]], "articles": articles})

    CACHE.write_text(json.dumps(new_cache))
    data = {"generated": datetime.now(timezone.utc).isoformat(), "sections": sections, "markets": markets,
            "health": [{"section": r[0], "source": r[1], "status": r[2], "count": r[3]} for r in report]}
    template = (ROOT / "template.html").read_text()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    OUTPUT.write_text(template.replace("/*__DATA__*/null", payload), encoding="utf-8")

    width = max(len(r[1]) for r in report)
    for sec, name, status, n in report:
        print("%-10s %-*s %3d  %s" % (sec[:10], width, name, n, status))
    print("Wrote", OUTPUT)
    return 0


if __name__ == "__main__":
    sys.exit(build())
