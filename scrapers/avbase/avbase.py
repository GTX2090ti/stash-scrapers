#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
avbase.net scraper for Stash (script scraper, self-contained: requests + lxml).

Modes (argv[1]):
  sceneByURL            stdin: {"url": "..."}          -> single ScrapedScene
  sceneByName           stdin: {"name": "..."}         -> list of results
  sceneByFragment       stdin: full scene fragment     -> single ScrapedScene
  sceneByQueryFragment  stdin: {"url" | "title" | ...} -> single ScrapedScene
  performerByURL        stdin: {"url": ".../talents/<name>"} -> ScrapedPerformer
  performerByName       stdin: {"name": "..."}         -> list of candidates

Env:
  AVBASE_DEBUG=1  enable stderr diagnostics (default silent; stderr lines
                  become ERROR entries in the Stash log, keep it quiet)
  AVBASE_TIMEOUT  per-request timeout seconds (default 20)
  AVBASE_SOURCE_PRIORITY  comma-separated source preference used when a work
                  aggregates several shop entries (default "getchu,gyutto").
                  The first source with data wins; missing fields fall back
                  down the same chain.  Example: a single work may exist as
                  fanza "snyz108", "getchu-4045459" AND "gyutto-258032" --
                  getchu is preferred, gyutto is the fallback.

Fragment code resolution order:
  1) existing avbase.net/works/ URL in fragment urls/url
  2) fragment "code" field
  3) code pattern found in title
  4) code pattern found in filename (basename, stem)
  5) fallback: search by cleaned title/filename keywords
Code matching ignores zero-padding: SSIS001 == SSIS-001 == ssis_1.
"""

import base64
import json
import os
import re
import sys
import time
from urllib.parse import quote, quote_plus, urlsplit

import requests
from lxml import html

NAME = "avbase"
BASE_URL = "https://www.avbase.net"
DEBUG = os.environ.get("AVBASE_DEBUG") == "1"
TIMEOUT = float(os.environ.get("AVBASE_TIMEOUT", "20"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"}

session = requests.Session()
session.headers.update(HEADERS)  # requests honours HTTP(S)_PROXY env vars


def dbg(msg):
    if DEBUG:
        sys.stderr.write("[%s] %s\n" % (NAME, msg))
        sys.stderr.flush()


RETRIES = int(os.environ.get("AVBASE_RETRIES", "3"))


def get(url, **kw):
    last = None
    for attempt in range(1, RETRIES + 1):
        dbg("GET %s (attempt %d)" % (url, attempt))
        try:
            r = session.get(url, timeout=TIMEOUT, **kw)
            dbg("  -> %s (%d bytes)" % (r.status_code, len(r.content)))
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            dbg("  attempt %d failed: %s" % (attempt, e))
            # drop the pooled connection: a poisoned tunnel stays poisoned
            session.close()
            if attempt < RETRIES:
                time.sleep(1.5 * attempt)
    raise last


def get_tree(url):
    return html.fromstring(get(url).content)


# --------------------------------------------------------------------------
# code normalization
# --------------------------------------------------------------------------

RE_CODE = re.compile(r"(?i)([a-z]{2,10})[-_ ]?(\d{1,6})")
# Getchu item IDs are 5-9 digits -- the generic pattern above caps at 6 and
# would truncate "GETCHU-4070052" to 407005. Handles GETCHU-4045244 and
# "Getchu-item4065791" spellings alike.
RE_GETCHU = re.compile(r"(?i)(?:getchu[-_ ]?(\d{5,9}))|(?<![a-z])item(\d{5,9})")
RE_QUALITY = re.compile(
    r"(?i)[\[\(]?\b(4k|8k|2160p|1080p|720p|480p|uhd|hdr|x26[45]|h\.?26[45]|"
    r"hevc|avc|web[- ]?dl|blu[- ]?ray|bdrm|brip|dvdrip| unreleased ?c|c\.?jbc|"
    r"uncensored|leaked|amateur?|hq|jav|cd[1-9]|part[1-9]|[hp]264|[hp]265)\b[\]\)]?")


RE_MEDIA_EXT = re.compile(
    r"\.(mp4|mkv|avi|wmv|mov|ts|m2ts|webm|flv|rmvb|mpg|mpeg)$", re.I)


def strip_media_ext(text):
    # ".mp4" would otherwise parse as a fake "MP-4" code
    return RE_MEDIA_EXT.sub("", text or "")


def parse_code(text):
    """Return (letters_upper, int_number) or None."""
    if not text:
        return None
    m = RE_GETCHU.search(text)
    if m:
        return ("GETCHU", int(m.group(1) or m.group(2)))
    m = RE_CODE.search(text)
    if not m:
        return None
    # reject pure-year matches like "2024" (no letters) -- RE_CODE requires letters
    return (m.group(1).upper(), int(m.group(2)))


def codes_equal(a, b):
    return a is not None and b is not None and a == b


def clean_title_for_search(text):
    """Filename/title -> usable search keywords."""
    s = os.path.splitext(os.path.basename(text or ""))[0]
    s = RE_QUALITY.sub(" ", s)
    s = re.sub(r"[_\-.]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


RE_TOKEN = re.compile(r"[0-9A-Za-z぀-ヿ一-鿿]+")


def _lettered_tokens(text):
    """Tokens that contain at least one letter/kana/kanji char (no pure numbers)."""
    out = set()
    for t in RE_TOKEN.findall((text or "").lower()):
        if any(c.isalpha() for c in t):
            out.add(t)
    return out


def is_relevant_hit(query, hit):
    """A keyword-fallback hit must share at least one lettered token with the
    query -- avbase search is fuzzy and happily returns unrelated works for
    performer-name-ish or amateur filenames."""
    if not query:
        return False
    q = _lettered_tokens(query)
    if not q:
        return False
    hit_text = " ".join([
        str(hit.get("title") or ""), str(hit.get("code") or ""),
        " ".join(str(p.get("name") or "") for p in (hit.get("performers") or [])),
        str((hit.get("studio") or {}).get("name") or ""),
    ])
    return bool(q & _lettered_tokens(hit_text))


# --------------------------------------------------------------------------
# field extraction helpers
# --------------------------------------------------------------------------

def _txt(node):
    if node is None:
        return None
    s = "".join(node.itertext())
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _uniq(seq):
    seen, out = set(), []
    for item in seq:
        k = item.strip() if isinstance(item, str) else json.dumps(item)
        if item and k not in seen:
            seen.add(k)
            out.append(item)
    return out


def first_txt(tree, xpath):
    nodes = tree.xpath(xpath)
    return _txt(nodes[0]) if nodes else None


def parse_date(raw):
    if not raw:
        return None
    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", raw)
    if not m:
        return None
    return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))


RE_JS_DATE = re.compile(r"\b([A-Z][a-z]{2}) (\d{1,2}) (\d{4})\b")
RE_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?!\d)")
MONTH_NUM = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
             "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def parse_date_any(raw):
    """Accept 'YYYY/MM/DD' (DOM), 'Fri Oct 28 2022 ...' (JS Date strings in
    __NEXT_DATA__) and ISO 'YYYY-MM-DD' timestamps alike."""
    if not raw:
        return None
    d = parse_date(raw)
    if d:
        return d
    m = RE_JS_DATE.search(raw)
    if m:
        mon = MONTH_NUM.get(m.group(1))
        if mon:
            return "%04d-%02d-%02d" % (int(m.group(3)), mon, int(m.group(2)))
    m = RE_ISO_DATE.search(raw)
    return m.group(0) if m else None


# --------------------------------------------------------------------------
# multi-source product selection
# --------------------------------------------------------------------------
# One avbase work aggregates several shop entries ("products"), each with its
# own source ID -- e.g. work SNYZ-108 exists as fanza "snyz108",
# "getchu-4045459" and "gyutto-258032" simultaneously.  The user prefers
# getchu data with gyutto as fallback; everything else trails after.
SOURCE_PRIORITY = [s.strip().lower() for s in
                   os.environ.get("AVBASE_SOURCE_PRIORITY", "getchu,gyutto").split(",")
                   if s.strip()]


def ordered_products(products):
    """Products sorted by SOURCE_PRIORITY (stable; unknown sources keep their
    original order after the prioritised ones)."""
    prods = [p for p in (products or []) if isinstance(p, dict)]
    prio = SOURCE_PRIORITY

    def rank(p):
        src = str(p.get("source") or "").lower()
        return prio.index(src) if src in prio else len(prio)

    return sorted(prods, key=rank)


def pick_field(products, *keys):
    """Walk the priority-ordered products and return the first non-empty value
    at the given key chain (e.g. "maker", "name").  This implements the
    per-field fallback: getchu first, then gyutto, then whatever remains."""
    for p in ordered_products(products):
        v = p
        for k in keys:
            if not isinstance(v, dict):
                v = None
                break
            v = v.get(k)
        if v:
            return v
    return None


def fetch_image_as_data_uri(url):
    if not url:
        return None
    try:
        r = get(url)
        mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
        if not mime.startswith("image/"):
            mime = "image/jpeg"
        return "data:%s;base64,%s" % (mime, base64.b64encode(r.content).decode("ascii"))
    except Exception as e:
        dbg("image fetch failed (%s): falling back to URL" % e)
        return url


# --------------------------------------------------------------------------
# work page (https://www.avbase.net/works/<ID>)
# --------------------------------------------------------------------------

def is_work_page(tree):
    # a real work page has an H1 and the 名寄せID block; the site's generic
    # 404 page has neither
    return bool(tree.xpath("//h1")) and bool(
        tree.xpath('//span[contains(text(), "名寄せID")]'))


def scene_from_work_data(work, page_url):
    """Build a ScrapedScene from the __NEXT_DATA__ work payload.

    Source selection follows SOURCE_PRIORITY (getchu > gyutto > rest): the
    preferred source feeds title/image/date/studio, and any field it lacks
    falls back down the same priority chain -- e.g. getchu entries often ship
    an empty iteminfo, so the description then comes from gyutto, then fanza.
    """
    prods = ordered_products(work.get("products"))
    if not prods:
        return None
    best = prods[0]
    dbg("product priority: %s" % ", ".join(
        "%s=%s" % (p.get("source"), p.get("product_id")) for p in prods))
    dbg("selected source: %s (%s)" % (best.get("source"), best.get("product_id")))

    studio = pick_field(prods, "maker", "name")
    director = pick_field(prods, "iteminfo", "director")
    details = pick_field(prods, "iteminfo", "description")
    image_url = pick_field(prods, "image_url") or pick_field(prods, "thumbnail_url")

    urls = [page_url]
    for p in prods:
        u = p.get("url")
        if u and u not in urls:
            urls.append(u)

    tags = [{"name": t["name"]} for t in (work.get("tags") or [])
            if isinstance(t, dict) and t.get("name")]
    tags += [{"name": g["name"]} for g in (work.get("genres") or [])
             if isinstance(g, dict) and g.get("name")]

    performers = [{"name": c["actor"]["name"]}
                  for c in (work.get("casts") or [])
                  if isinstance(c, dict) and (c.get("actor") or {}).get("name")]

    scene = {
        "title": pick_field(prods, "title") or work.get("title"),
        "code": work.get("work_id") or None,
        "urls": urls,
        "date": parse_date_any(best.get("date")) or parse_date_any(work.get("min_date")),
        "studio": {"name": studio} if studio else None,
        "performers": performers or None,
        "tags": _uniq(tags) or None,
        "director": director,
        "details": details,
        "image": fetch_image_as_data_uri(image_url),
    }
    return {k: v for k, v in scene.items() if v is not None}


def scrape_work(url):
    try:
        r = get(url)
    except requests.RequestException as e:
        dbg("work page unavailable: %s" % e)
        return None
    tree = html.fromstring(r.content)

    # 1) __NEXT_DATA__: structured payload carrying the aggregated per-source
    #    products (fanza / getchu / gyutto / ...).  A single work can exist
    #    under different IDs per source (snyz108 vs getchu-4045459 vs
    #    gyutto-258032); SOURCE_PRIORITY decides which one feeds the scene.
    work = (((extract_next_data(tree) or {}).get("props") or {})
            .get("pageProps") or {}).get("work")
    if isinstance(work, dict) and work.get("products"):
        scene = scene_from_work_data(work, url)
        if scene:
            return scene
        dbg("NEXT_DATA present but unusable, falling back to DOM")

    # 2) DOM fallback (page without __NEXT_DATA__ or unexpected payload)
    if not is_work_page(tree):
        dbg("not a valid work page: %s" % url)
        return None

    code = first_txt(tree,
        '//span[contains(text(), "名寄せID")]/following-sibling::span')
    if code and ":" in code:
        # namespaced work page, e.g. "moodyz:MIDV-123" / "secondface:SSIS-001"
        code = code.rsplit(":", 1)[1].strip() or code
    date = parse_date(first_txt(tree, '//a[contains(@href, "/works/date/")]'))
    studio = first_txt(tree, '//a[contains(@href, "/makers/")]')
    director = first_txt(tree, '//a[contains(@href, "/works?q=")]')
    title = first_txt(tree, "//h1")

    performers = _uniq([
        n for n in (_txt(a) for a in tree.xpath('//a[contains(@href, "/talents/")]'))
        if n
    ])
    tags = _uniq([
        n for n in (_txt(a) for a in tree.xpath('//a[contains(@href, "/tags/")]'))
        if n
    ])

    details = None
    desc = tree.xpath('//h2[normalize-space(text())="紹介文"]/following-sibling::p')
    if desc:
        details = _txt(desc[0])

    # cover = first content image (skip favicons/data-uris/talent avatars)
    image_url = None
    for img in tree.xpath("//img"):
        src = (img.get("src") or "").strip()
        if not src.startswith("http") or "favicon" in src:
            continue
        if "actjpgs" in src:  # DMM performer head shots, never the cover
            continue
        if img.get("alt"):
            image_url = src
            break

    scene = {
        "title": title,
        "code": code,
        "urls": [url],
        "studio": {"name": studio} if studio else None,
        "performers": [{"name": n} for n in performers],
        "tags": [{"name": n} for n in tags],
        "date": date,
        "director": director,
        "details": details,
        "image": fetch_image_as_data_uri(image_url),
    }
    return {k: v for k, v in scene.items() if v is not None}


# --------------------------------------------------------------------------
# search (https://www.avbase.net/works?q=...)
# --------------------------------------------------------------------------

def result_from_work_data(w):
    """Search hit -> candidate fragment, honouring SOURCE_PRIORITY for the
    fields that vary per source (title / studio / cover)."""
    if not isinstance(w, dict) or not w.get("work_id"):
        return None
    prods = ordered_products(w.get("products"))
    studio = pick_field(prods, "maker", "name")
    r = {
        "title": pick_field(prods, "title") or w.get("title"),
        "code": w.get("work_id"),
        "urls": ["%s/works/%s" % (BASE_URL, quote(str(w["work_id"])))],
        "date": parse_date_any(w.get("min_date")),
        "studio": {"name": studio} if studio else None,
        "performers": [{"name": a["name"]} for a in (w.get("actors") or [])
                       if isinstance(a, dict) and a.get("name")],
        "image": pick_field(prods, "image_url") or pick_field(prods, "thumbnail_url"),
    }
    return {k: v for k, v in r.items() if v is not None}


def search(query):
    if not query:
        return []
    try:
        tree = get_tree("%s/works?q=%s" % (BASE_URL, quote_plus(query.strip())))
    except requests.RequestException as e:
        dbg("search failed: %s" % e)
        return []

    # 1) __NEXT_DATA__: the search page embeds the full work payloads
    #    (incl. per-source products) -- far more reliable than the DOM cards.
    pp = (((extract_next_data(tree) or {}).get("props") or {})
          .get("pageProps") or {})
    works = pp.get("works")
    if isinstance(works, list) and works:
        results = [r for r in (result_from_work_data(w) for w in works) if r]
        if results:
            dbg("search via NEXT_DATA: %d hits" % len(results))
            return results

    # 2) DOM fallback
    results = []
    for node in tree.xpath('//div[@class="relative"]'):
        # title link: /works/<ID> anchor with the longest text in the node
        best = None
        for a in node.xpath('.//a[starts-with(@href, "/works/") and '
                            'not(contains(@href, "/works/date"))]'):
            t = _txt(a)
            if t and (best is None or len(t) > len(best[0])):
                best = (t, a.get("href"))
        if not best:
            continue
        title, href = best
        code = first_txt(node, './/span[contains(@class, "font-bold")][1]')
        date = parse_date(first_txt(
            node, './/a[contains(@href, "/works/date/")][1]'))
        studio = first_txt(node, './/a[contains(@href, "/makers/")][1]')
        performers = _uniq([
            n for n in (_txt(a) for a in node.xpath('.//a[contains(@href, "/talents/")]'))
            if n
        ])
        img = node.xpath('.//img[starts-with(@src, "http") and '
                         'not(contains(@src, "favicon"))][1]/@src')
        results.append({
            "title": title,
            "code": code,
            "urls": [BASE_URL + href],
            "date": date,
            "studio": {"name": studio} if studio else None,
            "performers": [{"name": n} for n in performers],
            "image": img[0] if img else None,
        })
    return [{k: v for k, v in r.items() if v is not None} for r in results]


# --------------------------------------------------------------------------
# fragment resolution
# --------------------------------------------------------------------------

def resolve_fragment_url(payload):
    """Return a concrete avbase work URL for a fragment, or None."""
    # 1) existing avbase URL
    urls = list(payload.get("urls") or [])
    if payload.get("url"):
        urls.append(payload["url"])
    for u in urls:
        if u and "avbase.net/works/" in u:
            return u

    # 2..4) code from code field / title / filename
    candidates = []
    code_pc = parse_code(payload.get("code"))
    if code_pc:
        candidates.append(code_pc)
    for text in (payload.get("title"),
                 (payload.get("files") or [{}])[0].get("path"),
                 payload.get("path")):
        pc = parse_code(strip_media_ext(text))
        if pc and pc not in candidates:
            candidates.append(pc)

    if candidates:
        want = candidates[0]
        # getchu items have a deterministic avbase URL: /works/getchu-<id>.
        # avbase keyword search is unreliable for these, so go straight there.
        if want[0] == "GETCHU":
            direct = "%s/works/getchu-%d" % (BASE_URL, want[1])
            try:
                get(direct)
                dbg("getchu direct hit: %s" % direct)
                return direct
            except requests.RequestException as e:
                dbg("getchu direct miss (%s), falling back to search" % e)
        q = "%s-%s" % want
        hits = search(q)
        # prefer plain /works/<CODE> over namespaced variants (maker:CODE):
        # namespaced pages are per-source mirrors with a prefixed ID block
        plain = [h for h in hits
                 if codes_equal(parse_code(h.get("code")), want)
                 and ":" not in ((h.get("urls") or [""])[0].rsplit("/", 1)[-1])]
        fallback = [h for h in hits
                    if codes_equal(parse_code(h.get("code")), want)]
        chosen = (plain or fallback or [None])[0]
        if chosen:
            return (chosen.get("urls") or [None])[0]

    # 5) keyword search fallback (relevance-gated)
    for text in (payload.get("title"),
                 (payload.get("files") or [{}])[0].get("path"),
                 payload.get("path")):
        kw = clean_title_for_search(text)
        if kw:
            for hit in search(kw):
                if is_relevant_hit(kw, hit):
                    return (hit.get("urls") or [None])[0]
    return None


# --------------------------------------------------------------------------
# talent page (https://www.avbase.net/talents/<name>)
# --------------------------------------------------------------------------

RE_WIKI_LINK = re.compile(r'href="(https://ja\.wikipedia\.org/[^"]+)"')


def extract_next_data(tree):
    """avbase is Next.js: structured talent data lives in __NEXT_DATA__ JSON.
    Far more reliable than scraping the rendered DOM (profile blocks render
    collapsed and empty server-side)."""
    for s in tree.xpath('//script[@id="__NEXT_DATA__"]/text()'):
        try:
            return json.loads(s)
        except ValueError as e:
            dbg("__NEXT_DATA__ parse failed: %s" % e)
            return None
    return None


def talent_url_for(name):
    return "%s/talents/%s" % (BASE_URL, quote(name))


def scrape_talent(url):
    if not url or "avbase.net/talents/" not in url:
        return None
    try:
        tree = get_tree(url)
    except requests.RequestException as e:
        dbg("talent page unavailable: %s" % e)
        return None

    data = extract_next_data(tree)
    pp = ((data or {}).get("props") or {}).get("pageProps") or {}
    talent = pp.get("talent") or {}
    primary = talent.get("primary") or {}
    name = primary.get("name") or pp.get("name")
    if not name:
        dbg("no talent data on page: %s" % url)
        return None

    info = (talent.get("meta") or {}).get("basic_info") or {}
    aliases = _uniq([a.get("name") for a in talent.get("actors") or []
                     if a.get("name") and a.get("name") != name])

    measurements = None
    if info.get("bust") and info.get("waist") and info.get("hip"):
        measurements = "%s-%s-%s" % (info["bust"], info["waist"], info["hip"])

    details = []
    if info.get("blood_type"):
        details.append("血液型: " + info["blood_type"])
    if info.get("prefectures"):
        details.append("出身: " + info["prefectures"])
    if info.get("cup"):
        details.append("カップ: " + info["cup"])
    if info.get("hobby"):
        details.append("趣味: " + info["hobby"])

    urls = [url]
    m = RE_WIKI_LINK.search(talent.get("profile") or "")
    if m:
        urls.append(m.group(1))
    if primary.get("url"):
        urls.append(primary["url"])  # dmm actress page (affiliate link)
    for sns in (talent.get("meta") or {}).get("sns") or []:
        link = sns.get("url") if isinstance(sns, dict) else sns
        if link:
            urls.append(link)

    image = None
    if primary.get("image_url"):
        image = fetch_image_as_data_uri(primary["image_url"])

    perf = {
        "name": name,
        "gender": "FEMALE",
        # this Stash build expects aliases as a comma-joined string, not a list
        "aliases": ", ".join(aliases) if aliases else None,
        "birthdate": info.get("birthday"),
        "measurements": measurements,
        "height": info.get("height"),
        # Stash only recognises English country names ("日本" -> not recognized)
        "country": "Japan" if info.get("prefectures") else None,
        "details": chr(10).join(details) or None,
        "image": image,
        "urls": _uniq(urls),
    }
    return {k: v for k, v in perf.items() if v is not None}


def _talent_exists(url):
    try:
        get(url)
        return True
    except requests.RequestException as e:
        dbg("talent miss %s: %s" % (url, e))
        return False


def performer_by_name(name):
    if not name or not name.strip():
        return []
    name = name.strip()
    out = []

    direct = talent_url_for(name)
    if _talent_exists(direct):
        perf = scrape_talent(direct)
        if perf:
            return [perf]

    # fallback: works search -> actor names -> probe their talent pages
    seen = set()
    for hit in search(name):
        for p in hit.get("performers") or []:
            n = (p.get("name") or "").strip()
            if not n or n == name or n in seen:
                continue
            seen.add(n)
            url = talent_url_for(n)
            if _talent_exists(url):
                perf = scrape_talent(url)
                if perf:
                    out.append(perf)
            if len(out) >= 5:
                return out
    return out


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------

def scene_by_url(url):
    if not url:
        return {}
    return scrape_work(url) or {}


def scene_by_name(name):
    return search(name) if name else []


def scene_by_fragment(payload):
    payload = payload or {}
    url = resolve_fragment_url(payload)
    if not url:
        dbg("fragment could not be resolved: %s" % json.dumps(payload)[:200])
        return {}
    return scrape_work(url) or {}


def main():
    op = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}

    try:
        if op == "sceneByURL":
            result = scene_by_url(payload.get("url"))
        elif op == "sceneByName":
            result = scene_by_name(payload.get("name"))
        elif op in ("sceneByFragment", "sceneByQueryFragment"):
            result = scene_by_fragment(payload)
        elif op == "performerByURL":
            result = scrape_talent(payload.get("url")) or {}
        elif op == "performerByName":
            result = performer_by_name(payload.get("name"))
        else:
            dbg("unknown operation: %s" % op)
            result = {} if op not in ("sceneByName", "performerByName") else []
    except Exception as e:
        # never let the process die without output: Stash would report EOF
        sys.stderr.write("[%s] unhandled error in %s: %r\n" % (NAME, op, e))
        sys.stderr.flush()
        result = {} if op != "sceneByName" else []

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
