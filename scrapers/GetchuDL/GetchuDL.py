#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Getchu scraper for Stash
========================

Scrapes metadata from dl.getchu.com (DL版 Getchu, 同人/CG集/动画/游戏) and
falls back to a generic meta/table parser for www.getchu.com pages.

Design notes
------------
* Zero third-party dependencies (stdlib only) -> no pip install needed inside
  the Stash container. Parsing is regex based on the same selectors that the
  MDC/JavSP crawlers use:
      og:title / og:image
      td[text()='サークル']              -> studio
      td[contains(.,'配信開始日')]        -> date
      td[text()='作者']                  -> director
      td[text()='趣向']/a               -> tags
      td[text()='作品内容']              -> details
      a.highslide[href*='/data/item_img/'] -> extra images
* dl.getchu.com requires the cookie ``adult_check_flag=1`` and serves EUC-JP.
* Supports sceneByName (name search) / sceneByURL / galleryByURL /
  sceneByFragment / galleryByFragment.

Performance
-----------
Measured on a CN home NAS behind a cross-border proxy (Stash container):

    CONNECT + TLS handshake to dl.getchu.com   ~0.8 s   (once per connection)
    one more request on a *reused* tunnel      ~0.36 s
    one request with a *fresh* connection      ~0.9  s
    the dl.getchu.com search endpoint itself   ~2.0 s   (site side, unavoidable)

That is why this scraper:

  1. reuses a single HTTP/1.1 tunnel for the whole scrape (the "search then
     item" fragment path drops from ~2.9 s to ~2.3 s);
  2. resolves an item id locally whenever it can -- an explicit getchu URL, an
     existing ``DLID-xxxxx`` code, or an id inside the filename skips the slow
     search entirely (3 requests -> 1, ~3.4 s -> ~1.4 s);
  3. scans the item page's ``<td>`` rows only once instead of once per field;
  4. only tries the search variants that actually return results.

Proxy (CN users cannot reach Getchu directly)
---------------------------------------------
Resolution order, first hit wins:
  1. ``GETCHU_PROXY`` env var on the Stash container
  2. a file named ``GetchuDL.proxy`` next to this script (one line, e.g.
     ``http://192.168.2.24:7893``) -- editable without restarting the container,
     just Reload scrapers
  3. the standard ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY`` env vars
  4. no proxy (direct)

Prefer (1) or (2) over setting a global HTTPS_PROXY on the container: a global
proxy also reroutes Stash's own traffic (StashDB, scraping other sites), which
usually is not what you want.

Tuning
------
``GETCHU_TIMEOUT``  request timeout in seconds (default 20)
``GETCHU_DEBUG``    set to 1 for per-step timing on stderr

Install
-------
1. Drop this file and GetchuDL.yml into your Stash scrapers directory
   (default ``~/.stash/scrapers``, inside Docker ``/root/.stash/scrapers``).
   Both files must sit in the SAME directory -- the yml calls ``GetchuDL.py``
   as a bare filename.
2. Stash's own Docker image ships WITHOUT Python. Install it once inside the
   container (Alpine-based images use apk, Debian-based ones apt):
       docker exec -it stash apk add --no-cache python3     # alpine
       docker exec -it stash apt-get update && apt-get install -y python3
   To survive container recreation, bake it into a Dockerfile instead:
       FROM stashapp/stash:latest
       RUN apk add --no-cache python3
3. Settings > Metadata Providers > Reload scrapers.
4. If Python is not auto-detected, set Settings > System > Application Paths >
   Python Executable Path to the python3 binary.
No pip packages are required -- this scraper is stdlib-only by design.
"""

import gzip
import html
import http.client
import json
import os
import re
import ssl
import sys
import time
import urllib.parse

NAME = "GetchuDL"
DL_BASE = "https://dl.getchu.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
COOKIES = {"adult_check_flag": "1"}
TIMEOUT = int(os.environ.get("GETCHU_TIMEOUT", "12"))
DEBUG = (os.environ.get("GETCHU_DEBUG") or "").strip() not in ("", "0", "false")
MAX_REDIRECTS = 4

# label candidates per field, tried in order (dl.getchu.com first, then
# www.getchu.com commercial naming)
LABELS = {
    "studio": ["サークル", "ブランド", "メーカー", "レーベル"],
    "date": ["配信開始日", "発売日", "配信日"],
    "author": ["作者", "原画", "イラスト"],
    "genre": ["趣向", "ジャンル", "カテゴリ", "シリーズ"],
    "details": ["作品内容", "商品紹介", "作品紹介"],
}

RE_META = re.compile(r"<meta\b[^>]*>", re.I | re.S)
RE_META_ATTR = re.compile(r'([\w:-]+)\s*=\s*["\']([^"\']*)["\']', re.S)
RE_TD = re.compile(r"<td\b[^>]*>(.*?)</td>\s*<td\b[^>]*>(.*?)</td>", re.I | re.S)
RE_TAG = re.compile(r"(?s)<[^>]+>")
RE_SCRIPT = re.compile(r"(?is)<(script|style)\b[^>]*>.*?</\1>")
RE_BR = re.compile(r"(?i)<br\s*/?>")
RE_BLOCKEND = re.compile(r"(?i)</(p|div|tr|li|h\d)>")
RE_A = re.compile(r"<a\b[^>]*>(.*?)</a>", re.I | re.S)
RE_TITLE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.I | re.S)
RE_TITLE_SUFFIX = re.compile(r"\s*[|｜]\s*(DL\.)?Getchu\.com.*$", re.I)
RE_CHARSET = re.compile(r"charset=([\w-]+)", re.I)
RE_ITEM_LINK = re.compile(r'href=["\'](?:https?://dl\.getchu\.com)?(/i/item\d+)', re.I)
RE_ITEM_IMG = re.compile(
    r"""<a\b[^>]*href=["']([^"']*?/data/item_img/[^"']+)["'][^>]*>""", re.I)
# Search-results page parsing (sceneByName). Each result card carries the item
# link twice: once wrapping the thumbnail <img> (empty anchor text) and once as
# the title link (the real title text). We match on the title link and skip the
# empty-text image link.
RE_TITLE_LINK = re.compile(
    r'<a href="https://dl\.getchu\.com/i/item(\d+)">(.*?)</a>', re.I | re.S)
RE_CIRCLE = re.compile(
    r'dojin_circle_detail\.php\?id=\d+">(.*?)</a>', re.I | re.S)
# "GETCHU-" spelling accepted alongside "DLID-": files are commonly named
# GETCHU-4045244.mp4 / [GETCHU-49519]... (both refer to the same item id).
RE_DLID = re.compile(r"(?:DLID|GETCHU)[-_ ]?(\d{4,})", re.I)
RE_ITEMID = re.compile(r"item[-_/]?(\d{4,})", re.I)
RE_ANYID = re.compile(r"(\d{4,})")
# A bare item id used on its own as the title or the filename stem. The getchu
# downloader names its output "4048195.mp4" with no DLID-/item- prefix, and a
# full-text search for a bare id returns nothing, so it must be recognised here.
RE_BARE_ID = re.compile(r"^(\d{6,})$")

# dl.getchu.com serves EUC-JP, but its pages carry JIS X 0213 characters
# (``㌢`` U+3322, ``①`` U+2460, ``⊿`` U+22BF ...) that Python's ``euc-jp`` codec
# cannot decode. With ``errors="replace"`` one unknown character yields U+FFFD
# *and desynchronises the multi-byte stream*, so its neighbours turn into
# garbage too: "１４８㌢" scraped as "１４８\uFFFD造痢\uFFFD", "原ネ申" as
# "仝競\uFFFD/申". ``euc_jisx0213`` is a superset of JIS X 0208 and decodes
# those pages byte-exactly -- verified on item4051260 / 4052569 / 4058496
# (22 / 18 / 30 U+FFFD with euc-jp, 0 with euc_jisx0213).
DECODE_FALLBACKS = ("euc_jisx0213", "euc_jis_2004", "cp932", "utf-8")


def dbg(msg):
    """Diagnostics -- stderr, and ONLY under ``GETCHU_DEBUG=1``.

    Stash classifies *anything* a scraper writes to stderr as an ``Error`` line
    in its log. Emitting the normal trace unconditionally therefore makes every
    successful scrape look like a failure ("[Scrape / GetchuDL] ... using proxy
    from HTTPS_PROXY", "HTTP 404 for ..."), and buries the messages that really
    do matter. Silenced by default, exactly like the stock scrapers.
    """
    if DEBUG:
        sys.stderr.write("[%s] %s\n" % (NAME, msg))
        sys.stderr.flush()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class FetchError(Exception):
    def __init__(self, code, url, detail=""):
        Exception.__init__(self, "HTTP %s for %s %s" % (code, url, detail))
        self.code = code
        self.url = url


def resolve_proxy():
    """Proxy URL to use, or '' to go direct.

    Order, first hit wins:
      1. ``GETCHU_PROXY`` env var
      2. a ``GetchuDL.proxy`` file next to this script
      3. the standard ``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``ALL_PROXY`` env vars
      4. direct

    Step 3 is done HERE rather than left to urllib: this scraper talks
    ``http.client`` directly, so an empty return really does mean "no proxy".
    (Leaving it implicit once caused the scraper to silently go direct while the
    container had ``HTTPS_PROXY`` set -- every request then stalled until the
    timeout instead of using the working proxy.)
    """
    env = (os.environ.get("GETCHU_PROXY") or "").strip()
    if env:
        dbg("using proxy from GETCHU_PROXY: %s" % env)
        return env
    cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "GetchuDL.proxy")
    if os.path.isfile(cfg):
        try:
            with open(cfg, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        dbg("using proxy from GetchuDL.proxy: %s" % line)
                        return line
        except OSError as e:
            dbg("cannot read GetchuDL.proxy: %s" % e)
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                "ALL_PROXY", "all_proxy"):
        val = (os.environ.get(key) or "").strip()
        if val:
            dbg("using proxy from %s: %s" % (key, val))
            return val
    dbg("no proxy configured, going direct")
    return ""


class Fetcher(object):
    """Minimal HTTP/1.1 client that keeps ONE tunnel alive across requests.

    Stash runs a scraper as a fresh process per scene, so the only chance to
    amortise the (expensive) CONNECT+TLS handshake is within a single scrape.
    sceneByFragment needs two requests (search + item), which is exactly where
    this pays off. Crossing a proxy, the reused hop costs ~0.36 s instead of
    ~0.9 s.

    Redirects are followed (up to MAX_REDIRECTS) and gzip is transparently
    decoded. Any transport error drops the connection and retries once.
    """

    def __init__(self, proxy=None):
        self.proxy = resolve_proxy() if proxy is None else proxy
        self._conn = None
        self._host = None
        self._ctx = ssl.create_default_context()

    # -- connection management ------------------------------------------- #
    def _connect(self, host):
        if self.proxy:
            pu = urllib.parse.urlsplit(self.proxy)
            port = pu.port or (443 if pu.scheme == "https" else 80)
            conn = http.client.HTTPSConnection(
                pu.hostname, port, timeout=TIMEOUT, context=self._ctx)
            conn.set_tunnel(host, 443, headers={"User-Agent": UA})
        else:
            conn = http.client.HTTPSConnection(
                host, 443, timeout=TIMEOUT, context=self._ctx)
        conn.connect()
        return conn

    def _drop(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._conn = None
        self._host = None

    # -- requests -------------------------------------------------------- #
    def get(self, url):
        """Return (status, raw_bytes, charset). Raises FetchError on failure."""
        last = None
        for _attempt in (0, 1):
            try:
                return self._get_once(url)
            except Exception as e:  # noqa: BLE001 - retry once, then surface
                last = e
                self._drop()
                if DEBUG:
                    dbg("retrying after %s: %s" % (type(e).__name__, e))
        if isinstance(last, FetchError):
            raise last
        raise FetchError(0, url, str(last))

    def _get_once(self, url):
        for _hop in range(MAX_REDIRECTS):
            status, raw, charset, location = self._request(url)
            if status in (301, 302, 303, 307, 308) and location:
                url = urllib.parse.urljoin(url, location)
                if DEBUG:
                    dbg("redirect -> %s" % url)
                continue
            if status >= 500:
                raise FetchError(status, url)
            return status, raw, charset
        raise FetchError(0, url, "too many redirects")

    def _request(self, url):
        u = urllib.parse.urlsplit(url)
        if u.scheme == "http":  # the site is https-capable; avoid plain tunnels
            u = u._replace(scheme="https")
            url = urllib.parse.urlunsplit(u)
        host = u.hostname
        path = u.path or "/"
        if u.query:
            path += "?" + u.query

        t_start = time.time()
        if self._conn is None or self._host != host:
            self._drop()
            self._conn = self._connect(host)
            self._host = host
        t0 = time.time()
        conn_cost = t0 - t_start

        headers = {
            "User-Agent": UA,
            "Cookie": "; ".join("%s=%s" % kv for kv in COOKIES.items()),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
            "Accept-Encoding": "gzip",
            "Connection": "keep-alive",
            "Referer": DL_BASE + "/",
            "Host": host,  # required when talking through a CONNECT tunnel
        }
        t_start = time.time()
        self._conn.request("GET", path, headers=headers)
        resp = self._conn.getresponse()
        raw = resp.read()
        status = resp.status
        ctype = resp.getheader("Content-Type") or ""
        cenc = (resp.getheader("Content-Encoding") or "").lower()
        location = resp.getheader("Location") or ""
        will_close = resp.will_close
        if will_close:
            self._drop()
        if "gzip" in cenc:
            try:
                raw = gzip.decompress(raw)
            except Exception:  # noqa: BLE001 - serve it raw, better than failing
                pass
        if DEBUG:
            dbg("GET %s -> %s %dB conn=%.2fs req=%.2fs reused=%s"
                % (url, status, len(raw), conn_cost, time.time() - t0, not will_close))
        elif status >= 400:
            dbg("GET %s -> %s" % (url, status))
        m = RE_CHARSET.search(ctype)
        return status, raw, (m.group(1) if m else ""), location


def decode_page(raw, declared=""):
    """Decode a page body *without ever* using ``errors="replace"``.

    The declared charset is tried first, with **strict** decoding: a strict
    success proves every byte is valid, so nothing can be lost. Only a strict
    failure falls through to the alternates, and only if they all fail do we
    give up with replacement characters.
    """
    order = [declared] if declared else []
    order += [e for e in DECODE_FALLBACKS if e not in order]
    for enc in order:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_text(fetcher, url, encoding="euc-jp"):
    """GET a page and decode it, honouring the response charset when given."""
    status, raw, charset = fetcher.get(url)
    if status >= 400:
        raise FetchError(status, url)
    text = decode_page(raw, charset or encoding)
    if "adult_check" in text[:4000] and "adult_check_flag" not in text:
        dbg("possible age-gate page returned")
    return text


# --------------------------------------------------------------------------- #
# HTML helpers
# --------------------------------------------------------------------------- #
def stripped(fragment):
    """HTML fragment -> plain text."""
    if not fragment:
        return ""
    s = RE_SCRIPT.sub(" ", fragment)
    s = RE_BR.sub("\n", s)
    s = RE_BLOCKEND.sub("\n", s)
    s = RE_TAG.sub("", s)
    s = html.unescape(s)
    # The site double-escapes some entities (e.g. "&amp;#128293;" for an emoji),
    # so a single pass leaves "&#128293;" in the text. Numeric entities always
    # decode cleanly, so a targeted second pass only fixes those.
    if "&#" in s:
        s = html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def meta_map(page):
    """Collect all <meta> attributes keyed by property/name (lowercased)."""
    out = {}
    for m in RE_META.finditer(page):
        tag = m.group(0)
        attrs = {
            k.lower(): html.unescape(v)
            for k, v in RE_META_ATTR.findall(tag)
        }
        key = attrs.get("property") or attrs.get("name") or ""
        if key and "content" in attrs:
            out.setdefault(key.lower(), attrs["content"])
    return out


def td_rows(page):
    """Yield (label_text, value_html) for every <td>label</td><td>value</td> pair."""
    for m in RE_TD.finditer(page):
        yield stripped(m.group(1)), m.group(2)


def find_td(rows, keys):
    """Value HTML of the td whose label matches one of *keys*.

    ``rows`` is the pre-computed ``list(td_rows(page))`` -- parsing it once per
    page instead of once per field shaves ~30 ms off every scrape.

    Two passes, because dl.getchu.com tables are messy:

    1. exact label match, keys tried in the order given (so ``趣向`` wins over
       the ``カテゴリ`` row, which would otherwise be hit first because it sits
       earlier in the document);
    2. tolerant substring match, but only on *plausible* labels -- some ``<td>``
       tags carry a ``>`` inside an attribute, which makes the row regex start
       mid-attribute and produce a garbled "label" that used to swallow the
       site header as if it were the サークル row.
    """
    for k in keys:
        for label, value in rows:
            if label == k:
                return value
    for k in keys:
        for label, value in rows:
            if _plausible_label(label) and k in label:
                return value
    return ""


def _plausible_label(label):
    """Reject row "labels" that are obviously not table headers."""
    if not label or len(label) > 16:
        return False
    return not any(c in label for c in ("{", "}", ";", "<", ">"))


def td_text(rows, keys):
    return stripped(find_td(rows, keys))


def td_links(rows, keys):
    value = find_td(rows, keys)
    if not value:
        return []
    out = []
    for m in RE_A.finditer(value):
        t = stripped(m.group(1))
        if t:
            out.append(t)
    if not out:
        t = stripped(value)
        if t:
            out = [x.strip() for x in re.split(r"[,、/\s]+", t) if x.strip()]
    return out


def abs_url(u, base=DL_BASE):
    if not u:
        return ""
    u = html.unescape(u.strip())
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return base + u
    if not re.match(r"^https?://", u, re.I):
        return base + "/" + u.lstrip("/")
    return u


def normalize_date(s):
    if not s:
        return ""
    m = re.search(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})", s)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{4})\s*[-/年.]\s*(\d{1,2})", s)
    if m:
        return "%04d-%02d-01" % (int(m.group(1)), int(m.group(2)))
    m = re.search(r"(\d{4})", s)
    if m:
        return "%s-01-01" % m.group(1)
    return ""


# --------------------------------------------------------------------------- #
# ID / URL handling
# --------------------------------------------------------------------------- #
def extract_id(url):
    for pat in (r"/i/item(\d+)", r"item[_/-]?(\d+)", r"[?&]id=(\d+)"):
        m = re.search(pat, url, re.I)
        if m:
            return m.group(1)
    m = RE_ANYID.search(url)
    return m.group(1) if m else ""


def dl_url_for(item_id):
    return "%s/i/item%s" % (DL_BASE, item_id)


def _stem(path):
    """Filename without directories and extension."""
    base = os.path.basename((path or "").replace("\\", "/"))
    return os.path.splitext(base)[0].strip()


# Keys Stash accepts in a scraped scene/gallery fragment (see ScrapedScene in
# stash's scrapers.go). Anything else -- notably ``id``, which Stash puts into
# the fragment it sends us -- makes Stash log
# ``Warning reading script result: json: unknown field "id"``.
_FRAGMENT_KEYS = ("title", "code", "details", "director", "urls", "date",
                  "image", "studio", "tags", "performers")


def safe_fragment(payload):
    """Whitelist-filter the payload we echo back when a scrape fails.

    main() echoes the input back on failure so a failed lookup never wipes the
    metadata a scene already has. That fragment arrives with Scene-only keys
    (``id``, ``files`` ...) which are not part of the scraper result schema, so
    they must be dropped before echoing.
    """
    return {k: v for k, v in (payload or {}).items() if k in _FRAGMENT_KEYS}


def _id_from_payload(payload):
    """Local (no network) item-id resolution -- the fast path.

    Called before any search: an id we can trust means ONE request instead of
    three, which is the difference between ~1.4 s and ~3.4 s.
    """
    # 1) explicit getchu url
    for u in list(payload.get("urls") or []) + [payload.get("url") or ""]:
        if u and "getchu.com" in u:
            return ("url", u)
    # 2) an existing DLID code (Stash passes it back on re-scrapes)
    code = (payload.get("code") or "").strip()
    if code:
        m = RE_DLID.search(code)
        if m:
            return ("id", m.group(1))
        if re.fullmatch(r"\d{4,}", code):
            return ("id", code)
    # 3) an id embedded in the filename, e.g. "... [DLID-4024984].zip"
    # Stash's scene fragment carries paths under files[] (fileInput.path),
    # not under "filename"/"path" -- read both shapes.
    fname = payload.get("filename") or payload.get("path") or ""
    if not fname:
        for f in payload.get("files") or []:
            p = f.get("path") if isinstance(f, dict) else None
            if p:
                fname = p
                break
    if fname:
        m = RE_DLID.search(fname) or RE_ITEMID.search(fname)
        if m:
            return ("id", m.group(1))
    # 4) a bare item id used as the whole filename stem or title -- files
    #    downloaded from dl.getchu.com are literally named "4048195.mp4".
    #    Note this is a weak signal: if the id does not exist on the site the
    #    item page simply fails to parse, the fragment stays empty and Stash
    #    keeps its existing metadata (see main()), so a false positive is
    #    harmless -- it only costs one request.
    for cand in (_stem(fname), (payload.get("title") or "").strip()):
        if cand:
            m = RE_BARE_ID.match(cand)
            if m:
                return ("id", m.group(1))
    return ("", "")


def search_url_list(keyword):
    """Build the ordered list of dl.getchu.com search URLs for *keyword*.

    Only the two variants that were verified to return results are tried; a
    variant without ``search_category_id=`` never matched anything in testing
    and just cost an extra round trip.
    """
    kw = keyword.replace("●", " ").strip()
    if not kw:
        return []
    # dl.getchu.com expects EUC-JP for the keyword. Plain euc-jp first (that is
    # what a Japanese browser would send); euc_jisx0213 only covers keywords
    # holding JIS X 0213 characters such as "①" that euc-jp cannot encode.
    enc_kw = ""
    for enc in ("euc-jp", "euc_jisx0213", "utf-8"):
        try:
            enc_kw = urllib.parse.quote_plus(kw, encoding=enc)
            break
        except (UnicodeEncodeError, LookupError):
            continue
    if not enc_kw:
        enc_kw = urllib.parse.quote_plus(kw)
    return [
        "%s/search/search_list.php?dojin=1&search_category_id="
        "&search_keyword=%s&action=search&set_category_flag=1" % (DL_BASE, enc_kw),
        "%s/search/search_list.php?dojin=1&search_keyword=%s&action=search"
        % (DL_BASE, enc_kw),
    ]


def search_dl(keyword, fetcher):
    """Full-text search on dl.getchu.com, returns the first item URL.

    Kept for the fragment fast-path (resolve_fragment). Returns "" when the
    keyword matches nothing.
    """
    for url in search_url_list(keyword):
        try:
            page = fetch_text(fetcher, url)
        except Exception as e:  # noqa: BLE001 - scraper must never raise
            dbg("search failed: %s" % e)
            continue
        hits = RE_ITEM_LINK.findall(page)
        if hits:
            dbg("search hit: %s" % hits[0])
            return DL_BASE + hits[0]
        dbg("search returned no item link (%s)" % url)
    return ""


def _small_img_for(page, item_id):
    """Thumbnail URL for an item, taken from the search page if present,
    otherwise synthesised from the known /data/item_img path shape."""
    m = re.search(
        r'src=["\'](/data/item_img/\d+/%s/%ssmall\.jpg)["\']'
        % (item_id, item_id), page, re.I)
    if m:
        return DL_BASE + m.group(1)
    return "%s/data/item_img/%s/%s/%ssmall.jpg" % (
        DL_BASE, item_id[:-2], item_id, item_id)


def parse_search(page):
    """Parse a dl.getchu.com search-results page into a list of scene fragments.

    Each entry is a candidate for Stash's name-search dropdown: it carries the
    title, the canonical item URL (so Stash can re-scrape via sceneByURL once the
    user picks it), the DLID code, the thumbnail and the circle as studio.
    """
    out = []
    seen = set()
    for m in RE_TITLE_LINK.finditer(page):
        item_id = m.group(1)
        if item_id in seen:
            continue
        title = stripped(m.group(2))
        if not title:
            continue  # the empty-text image link, not the title link
        seen.add(item_id)
        url = "%s/i/item%s" % (DL_BASE, item_id)
        info = {
            "title": title,
            "url": url,
            "code": "DLID-%s" % item_id,
            "image": _small_img_for(page, item_id),
        }
        # circle / studio sits in a (...) after the title link, same card
        cm = RE_CIRCLE.search(page, m.end(), m.end() + 500)
        if cm:
            studio = stripped(cm.group(1))
            if studio:
                info["studio"] = {"name": studio}
        out.append(info)
    return out


def search_scenes(keyword, fetcher):
    """Full-text search returning the list of candidate scene fragments."""
    for url in search_url_list(keyword):
        try:
            page = fetch_text(fetcher, url)
        except Exception as e:  # noqa: BLE001 - scraper must never raise
            dbg("search failed: %s" % e)
            continue
        results = parse_search(page)
        if results:
            dbg("search returned %d results" % len(results))
            return results
        dbg("search returned no results (%s)" % url)
    return []


# --------------------------------------------------------------------------- #
# Item page parsing
# --------------------------------------------------------------------------- #
def parse_item(page, url):
    metas = meta_map(page)
    rows = list(td_rows(page))
    item_id = extract_id(url)
    is_dl = "dl.getchu.com" in url

    title = (metas.get("og:title") or "").strip()
    if not title:
        m = RE_TITLE.search(page)
        title = stripped(m.group(1)) if m else ""
    title = RE_TITLE_SUFFIX.sub("", title).strip()

    info = {
        "title": title,
        "url": url,
        "code": ("DLID-%s" % item_id) if (is_dl and item_id) else "",
        "studio": td_text(rows, LABELS["studio"]),
        "date": normalize_date(td_text(rows, LABELS["date"])),
        "director": td_text(rows, LABELS["author"]),
        "tags": td_links(rows, LABELS["genre"]),
        "details": td_text(rows, LABELS["details"]),
        "image": abs_url(metas.get("og:image") or ""),
    }

    # fallback for details: og:description
    if not info["details"]:
        d = (metas.get("og:description") or "").strip()
        if d and d != info["title"]:
            info["details"] = d

    # extra images (gallery art)
    extra = []
    for m in RE_ITEM_IMG.finditer(page):
        extra.append(abs_url(m.group(1)))
    seen, arts = set(), []
    for u in extra:
        if u not in seen:
            seen.add(u)
            arts.append(u)
    info["extra_images"] = arts
    return info


def build_scene(info, url):
    frag = {}
    if info.get("title"):
        frag["title"] = info["title"]
    if info.get("url"):
        frag["url"] = info["url"]
    if info.get("date"):
        frag["date"] = info["date"]
    if info.get("details"):
        frag["details"] = info["details"]
    if info.get("code"):
        frag["code"] = info["code"]
    if info.get("image"):
        frag["image"] = info["image"]
    if info.get("studio"):
        frag["studio"] = {"name": info["studio"]}
    if info.get("director"):
        frag["director"] = info["director"]
    if info.get("tags"):
        frag["tags"] = [{"name": t} for t in info["tags"]]
    return frag


def build_gallery(info, url):
    frag = {}
    if info.get("title"):
        frag["title"] = info["title"]
    if info.get("url"):
        frag["urls"] = [info["url"]]
    if info.get("date"):
        frag["date"] = info["date"]
    if info.get("details"):
        frag["details"] = info["details"]
    if info.get("studio"):
        frag["studio"] = {"name": info["studio"]}
    if info.get("tags"):
        frag["tags"] = [{"name": t} for t in info["tags"]]
    return frag


def build_search_result(info):
    """A search candidate for Stash's name-search dropdown.

    Stash requires the byName result to be a JSON *array* of ScrapedScene-like
    objects; each must carry a ``url`` so the engine can re-scrape the chosen
    item through sceneByURL. ``code`` / ``image`` / ``studio`` only enrich the
    candidate list.
    """
    frag = {}
    if info.get("title"):
        frag["title"] = info["title"]
    if info.get("url"):
        frag["url"] = info["url"]
    if info.get("code"):
        frag["code"] = info["code"]
    if info.get("image"):
        frag["image"] = info["image"]
    if info.get("studio"):
        frag["studio"] = info["studio"]
    return frag


def scrape(url, kind, fetcher):
    """Fetch + parse a Getchu item URL, returns a fragment dict."""
    t0 = time.time()
    try:
        page = fetch_text(fetcher, url)
    except FetchError as e:
        # A 404 here means the item id is not in getchu's catalogue at all
        # (delisted / never existed). Verified 2026-09-11: no alternative form
        # helps -- /index.php?action=item&id=N returns the bare site landing
        # page (12.6 KB, no item markup) and www.getchu.com/soft.phtml?id=N
        # 301-redirects to www.getchu.com/item/N/ which is a 404 as well.
        dbg("HTTP %s for %s" % (e.code, url))
        return {}
    except Exception as e:  # noqa: BLE001
        dbg("request error: %s" % e)
        return {}

    info = parse_item(page, url)
    if not info.get("title"):
        dbg("no title parsed from %s" % url)
        return {}
    if DEBUG:
        dbg("parsed %r in %.2fs" % (info.get("title"), time.time() - t0))
    else:
        dbg("parsed: %s" % info.get("title"))
    return build_gallery(info, url) if kind == "gallery" else build_scene(info, url)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def resolve_fragment(payload, fetcher):
    """sceneByFragment/galleryByFragment: find an item URL from Stash input."""
    kind, val = _id_from_payload(payload)
    if kind == "url":
        dbg("fragment resolved from url: %s" % val)
        return val
    if kind == "id":
        url = dl_url_for(val)
        dbg("fragment resolved from id (no search): %s" % url)
        return url
    # last resort: full-text search on the title (the slow path, ~2 s)
    title = (payload.get("title") or "").strip()
    if title:
        dbg("fragment needs a search for %r" % title)
        return search_dl(title, fetcher)
    return ""


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "sceneByURL"
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:  # noqa: BLE001
        dbg("bad json input: %s" % e)
        payload = {}

    kind = "gallery" if mode.lower().startswith("gallery") else "scene"
    fetcher = Fetcher()

    if mode == "sceneByName":
        # Stash sends the query under "q"; be tolerant of other field names and
        # of a manual `python GetchuDL.py sceneByName <term>` invocation.
        q = (payload.get("q") or payload.get("query") or payload.get("name")
             or payload.get("title") or "").strip()
        if not q and len(sys.argv) > 2:
            q = sys.argv[2].strip()
        results = []
        if q:
            try:
                results = [build_search_result(i)
                           for i in search_scenes(q, fetcher)]
            except Exception as e:  # noqa: BLE001 - must emit valid JSON
                dbg("sceneByName error: %s" % e)
        # byName MUST return a JSON array (even when empty), never an object.
        sys.stdout.write(json.dumps(results, ensure_ascii=False))
        sys.stdout.flush()
        return

    if mode in ("sceneByURL", "galleryByURL"):
        url = (payload.get("url") or "").strip()
        frag = scrape(url, kind, fetcher) if url else {}
    else:  # *,ByFragment
        url = resolve_fragment(payload, fetcher)
        if url:
            frag = scrape(url, kind, fetcher)
            if not frag:
                # Nothing scraped (e.g. a bare id that is not in getchu's
                # catalogue any more -> HTTP 404): echo the input back so Stash
                # keeps the metadata the scene already has.
                frag = safe_fragment(payload)
        else:
            dbg("no match for fragment input")
            frag = safe_fragment(payload)
        if not frag and payload.get("urls"):
            frag = {"urls": payload["urls"]}

    sys.stdout.write(json.dumps(frag, ensure_ascii=False))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
