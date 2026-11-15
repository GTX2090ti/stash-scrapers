#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cosplay-doga.net scraper for Stash (script scraper, self-contained: requests + lxml).

Modes (argv[1]):
  sceneByURL            stdin: {"url": "..."}          -> single ScrapedScene
  galleryByURL          stdin: {"url": "..."}          -> single ScrapedGallery
  sceneByFragment       stdin: full scene fragment     -> single ScrapedScene
  galleryByFragment     stdin: full gallery fragment   -> single ScrapedGallery
  sceneByQueryFragment  stdin: {"url"|"title"|...}     -> single ScrapedScene

Env:
  COSPLAYDOGA_DEBUG=1    stderr diagnostics (default silent: stderr lines become
                         ERROR entries in the Stash log)
  COSPLAYDOGA_TIMEOUT    per-request timeout seconds (default 20)

The site is a WordPress blog reviewing cosplay AV works; one page per work.
Fields available on the page: title, publish date, cover (og:image), body text,
category + tags. There is no product code, studio or performer data on the page,
so those are intentionally omitted rather than guessed.

Fragment id resolution order:
  1) existing cosplay-doga.net/post-<id>/ URL in fragment urls/url
  2) fragment "code" field
  3) numeric id in filename / file path
  4) numeric id in title
Year-like 4-digit values (19xx / 20xx) are never treated as a post id.
"""

import json
import os
import re
import sys
import time

import requests
from lxml import html

NAME = "CosplayDoga"
BASE = "https://www.cosplay-doga.net"
DEBUG = os.environ.get("COSPLAYDOGA_DEBUG") == "1"
TIMEOUT = float(os.environ.get("COSPLAYDOGA_TIMEOUT", "20"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"})

RE_URL_ID = re.compile(r"cosplay-doga\.net/post-(\d+)", re.I)
RE_POST_ID = re.compile(r"^post[-_ ]?(\d{3,8})$", re.I)
RE_BARE_ID = re.compile(r"^(\d{3,8})$")
RE_ID_IN_TEXT = re.compile(r"post[-_ ]?(\d{3,8})", re.I)

# Lines on every page that are site promotion, not work description.
ADS = (
    re.compile(r"^※本ページはプロモーション"),
    re.compile(r"^＼\s*FANZA"),
    re.compile(r"^【\s*↓?高画質"),
    re.compile(r"^⇒\s*高画質"),
)

_FRAGMENT_KEYS = ("title", "code", "details", "director", "urls", "url",
                  "date", "image", "studio", "tags", "performers")


def dbg(msg):
    if DEBUG:
        sys.stderr.write("[%s] %s\n" % (NAME, msg))
        sys.stderr.flush()


def _s(value):
    """Coerce an arbitrary JSON value to a stripped string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("name", "url", "value", "text"):
            if value.get(key):
                return _s(value[key])
        return ""
    if isinstance(value, list):
        return _s(value[0]) if value else ""
    return str(value).strip()


def safe_fragment(payload):
    """Echo back only keys Stash accepts, so a failed scrape keeps local data."""
    if not isinstance(payload, dict):
        return None
    return {k: v for k, v in payload.items() if k in _FRAGMENT_KEYS}


def collect_urls(payload):
    out = []
    if not isinstance(payload, dict):
        return out
    for key in ("url", "urls"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
        elif isinstance(value, list):
            out.extend(_s(v) for v in value if _s(v))
    return out


def collect_paths(payload):
    out = []
    if not isinstance(payload, dict):
        return out
    files = payload.get("files")
    if isinstance(files, list):
        for item in files:
            if isinstance(item, dict):
                for key in ("path", "basename", "file"):
                    if item.get(key):
                        out.append(_s(item[key]))
            elif isinstance(item, str):
                out.append(item)
    for key in ("path", "filename", "file"):
        if isinstance(payload.get(key), str) and payload[key].strip():
            out.append(payload[key].strip())
    return [p for p in out if p]


def is_year(value):
    return bool(re.fullmatch(r"(?:19|20)\d\d", value or ""))


def resolve_id(payload):
    """Return the post id (str) or None."""
    for url in collect_urls(payload):
        m = RE_URL_ID.search(url)
        if m:
            return m.group(1)

    code = _s(payload.get("code")) if isinstance(payload, dict) else ""
    for cand in (code, re.sub(r"[^0-9]", "", code)):
        if cand and not is_year(cand):
            m = RE_POST_ID.match(cand) or RE_BARE_ID.match(cand)
            if m:
                return m.group(1)

    for path in collect_paths(payload):
        stem = os.path.splitext(os.path.basename(path))[0]
        for cand in (stem, re.sub(r"[^0-9]", "", stem)):
            if not cand or is_year(cand):
                continue
            m = RE_POST_ID.match(cand) or RE_BARE_ID.match(cand)
            if m:
                return m.group(1)
        m = RE_ID_IN_TEXT.search(stem)
        if m and not is_year(m.group(1)):
            return m.group(1)

    title = _s(payload.get("title")) if isinstance(payload, dict) else ""
    if title:
        t = title.strip()
        if not is_year(t):
            m = RE_POST_ID.match(t) or RE_BARE_ID.match(t)
            if m:
                return m.group(1)
        m = RE_ID_IN_TEXT.search(t)
        if m and not is_year(m.group(1)):
            return m.group(1)

    return None


def post_url(post_id):
    return "%s/post-%s/" % (BASE, post_id)


def fetch(url, attempts=3):
    last = None
    for attempt in range(attempts):
        try:
            resp = session.get(url, timeout=TIMEOUT)
            if resp.status_code == 200:
                resp.encoding = resp.encoding or "utf-8"
                return resp.text
            last = "HTTP %s" % resp.status_code
            dbg("GET %s -> %s (attempt %d)" % (url, last, attempt + 1))
            if resp.status_code not in (403, 429, 500, 502, 503, 504):
                break
        except Exception as exc:  # noqa: BLE001 - log and retry
            last = "%s: %s" % (type(exc).__name__, exc)
            dbg("GET %s failed (%s) attempt %d" % (url, last, attempt + 1))
        time.sleep(0.8 * (attempt + 1))
    raise RuntimeError("failed to load %s: %s" % (url, last))


def clean(text):
    """Collapse whitespace. Accepts str, lxml elements, or anything else."""
    if text is None:
        return ""
    if isinstance(text, str):
        raw = text
    elif hasattr(text, "text_content"):
        raw = text.text_content()
    else:
        raw = str(text)
    return re.sub(r"\s+", " ", raw).strip()


def parse_page(page, url):
    """Parse one post page into a scene/gallery mapping."""
    doc = html.fromstring(page)

    def first(xpath):
        found = doc.xpath(xpath)
        return clean(found[0]) if found else ""

    data = {}

    title = first('//h1[contains(@class,"page-header-title")]')
    if not title:
        title = first('//meta[@property="og:title"]/@content')
        title = re.sub(r"\s+-\s+コスプレエロ動画情報まとめ\s*$", "", title)
    if title:
        data["title"] = title

    published = first('//meta[@property="article:published_time"]/@content')
    if published:
        data["date"] = published[:10]

    # Cover: the first in-article image is the real package art (560x420);
    # og:image is only a 216px thumbnail, so it is the fallback.
    image = first('//div[contains(@class,"entry-body")]//p//img/@src')
    if not image:
        image = first('//meta[@property="og:image"]/@content')
    if image:
        data["image"] = image

    canonical = first('//link[@rel="canonical"]/@href')
    data["urls"] = [canonical or url]

    # Article body: only the prose blocks directly under div.entry-body.
    # asides (ads / related posts) and div.entry-footer are skipped.
    blocks = []
    for body in doc.xpath('//div[contains(@class,"entry-body")]'):
        for el in body:
            if not isinstance(el.tag, str):
                continue
            tag = el.tag.lower()
            classes = el.get("class") or ""
            if tag == "aside" or "entry-footer" in classes:
                continue
            if tag not in ("p", "h2", "h3", "h4", "blockquote", "ul", "ol"):
                continue
            text = clean(el.text_content())
            if not text:
                continue
            if any(ad.match(text) for ad in ADS):
                continue
            blocks.append(text)
    if blocks:
        data["details"] = "\n".join(blocks)

    tags = []
    for node in doc.xpath('//div[contains(@class,"entry-meta-data-list--tags")]//a[@rel="tag"]'):
        name = clean(node.text_content())
        if name and name not in tags:
            tags.append(name)
    for node in doc.xpath('//div[contains(@class,"entry-meta-data-list--category")]//a'):
        name = clean(node.text_content())
        if name and name not in tags:
            tags.append(name)
    if tags:
        data["tags"] = [{"name": name} for name in tags]

    return data


def scrape_by_url(url):
    page = fetch(url)
    data = parse_page(page, url)
    dbg("parsed %s -> title=%r tags=%d" % (url, data.get("title"), len(data.get("tags") or [])))
    return data


def scrape_by_fragment(payload):
    post_id = resolve_id(payload)
    if not post_id:
        dbg("no post id in fragment")
        return safe_fragment(payload)
    url = post_url(post_id)
    dbg("fragment resolved to %s" % url)
    try:
        data = scrape_by_url(url)
    except Exception as exc:  # noqa: BLE001
        dbg("fragment scrape failed: %s" % exc)
        return safe_fragment(payload)
    if not data.get("title"):
        return safe_fragment(payload)
    return data


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("[%s] missing mode argument\n" % NAME)
        return 1

    mode = sys.argv[1]
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        sys.stderr.write("[%s] invalid JSON input\n" % NAME)
        return 1

    try:
        if mode in ("sceneByURL", "galleryByURL"):
            url = _s(payload.get("url"))
            if not url:
                result = None
            else:
                result = scrape_by_url(url)
        elif mode in ("sceneByFragment", "galleryByFragment", "sceneByQueryFragment"):
            result = scrape_by_fragment(payload)
        else:
            sys.stderr.write("[%s] unsupported mode %s\n" % (NAME, mode))
            return 1
    except Exception as exc:  # noqa: BLE001 - never crash without JSON
        dbg("error: %s" % exc)
        if mode in ("sceneByFragment", "galleryByFragment", "sceneByQueryFragment"):
            result = safe_fragment(payload)
        else:
            result = None

    # ScrapedGallery has no image field; only scenes take the cover.
    if result and mode.startswith("gallery"):
        result.pop("image", None)

    if result is None:
        sys.stdout.write("null")
    else:
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
