#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FantiaProducts -- Stash scraper for fantia.jp/products/<id>
==========================================================

Why this scraper exists
-----------------------
Fantia serves **two independent objects under one numeric id**:

    fantia.jp/posts/<id>       a creator's post   -> FantiaJp
    fantia.jp/products/<id>    a shop product     -> this scraper

They are unrelated.  Live evidence (2026-09-18):

    id          /posts/<id>                    /products/<id>
    887480      404                            No.697 ... セクメト[H]#02
    732915      【巨乳】えちえちスポーティー…    【なんかちゃん】11月限定25%OFF…
    748144      【コスプレ】水着ガヴィル動画     【なんかちゃん】12月限定25%OFF…
    1006291     青橙 (るるノ屋)                 🌟Re:ゼロ…【buena320】

So a /products/ URL can never be served by FantiaJp, and a bare number is
genuinely ambiguous.  This scraper only ever synthesises /products/ URLs.

Why a *script* and not scrapeXPath
----------------------------------
The old XPath version could only do sceneByURL.  Stash 0.31.1 routes the
UI's "Scrape with ... <scraper>" dialog through
``scene_input{url}`` -> ``sceneByQueryFragment``, and a scene with no stored
URL goes through ``sceneByFragment``.  XPath scrapers cannot declare either,
so every UI entry returned::

    error while fragment scraping with scraper FantiaProducts:
    scraper operation not supported

Field extraction (all from one GET)
-----------------------------------
* ``<script type="application/ld+json">`` schema.org ``Product``
    -> name / description / image[] (UNBLURRED) / brand.name
* ``<meta property="og:*">``                  -> og:title / og:url / og:image
* ``<script type="application/ld+json">`` ``{"event":"content_viewed", ...}``
    -> tag[] (optional!) / fanclub_category / content_id

Code policy (fixed 2026-09-18)
------------------------------
The old rule took the *last* ``【...】`` of og:title.  That is unreliable:

    products/1006291  【buena320】     -> buena-320     (correct)
    products/732915   【9WD本編真打】   -> 9WD本編真打   (marketing label!)
    products/887480   (no 【】 at all)  -> the WHOLE og:title (pure garbage)

New rule, first hit wins:
  1. the first ``【...】`` that actually looks like a catalogue number
     (``^[A-Za-z]{2,}[-_ ]?\\d{2,}$``) -> normalised to ``buena-320``
  2. otherwise a stable synthetic id ``FANTIA-P<product id>``
Never the raw og:title.

Tag policy
----------
``content_viewed`` has a ``tag`` array on *some* products only (1006291 has
it, 887480 does not).  When absent the old regex matched nothing and Stash
fell back to the whole JSON blob, yielding tags like
``{event:content_viewed`` / ``fanclub_id:4147``.  Now: real tag[] when
present, otherwise ``fanclub_category`` (e.g. ``コスプレ``), otherwise nothing.

Proxy
-----
Resolution order, first hit wins:
  1. ``FANTIAPROD_PROXY`` env var on the Stash container
  2. a ``FantiaProducts.proxy`` file next to this script (one line)
  3. ``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``ALL_PROXY`` (either case)
  4. direct

Tuning: ``FANTIAPROD_TIMEOUT`` (default 20), ``FANTIAPROD_DEBUG=1`` (stderr).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

NAME = "FantiaProducts"
DEFAULT_TIMEOUT = 20

_PRODUCT_URL = "https://fantia.jp/products/%s"
# catalogue-code shaped: >=2 letters, optional separator, >=2 digits
_RE_CATALOGUE = re.compile(r"^[A-Za-z]{2,}[-_ ]?\d{2,}$")
_RE_CATALOGUE_SPLIT = re.compile(r"^([A-Za-z]{2,})[-_ ]?(\d{2,})$")
_RE_BRACKETS = re.compile(r"【([^】]{1,40})】")
_RE_OG_URL_PROD = re.compile(r"fantia\.jp/products/(\d+)")
_RE_CODE_P = re.compile(r"^FANTIA-P(\d+)$", re.I)
_RE_TAGGED_ID = re.compile(r"(?:fantia|products?)[-_\s]*(\d{5,})", re.I)
_RE_BARE_ID = re.compile(r"^(\d{5,})$")
_MEDIA_EXT = (".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".ts", ".webm",
              ".flv", ".rmvb", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".zip", ".rar", ".7z")


def _dbg(msg: str) -> None:
    # stderr is logged as Error by Stash -> gate it (see skill section 14)
    if os.environ.get("FANTIAPROD_DEBUG"):
        sys.stderr.write("[%s] %s\n" % (NAME, msg))
        sys.stderr.flush()


# ----------------------------------------------------------- CookieCloud auth
# fantia.jp serves member-only product pages as the LOGIN page to anonymous
# clients (HTTP 200 + og:title "login|Fantia[Fantia]"), so parse_product() used
# to return {} for every member product -> "scrape failed" in the UI.  Fix:
# carry the _session_id cookie.  Source (same as the Fantia scraper):
#   1. env FANTIA_COOKIE='_session_id=...'
#   2. self-hosted CookieCloud (CC_COOKIECLOUD_URL/_KEY/_PASSWORD, injected as
#      container env vars -- never hardcoded into this file)
CC_URL = os.environ.get("CC_COOKIECLOUD_URL", "")
CC_KEY = os.environ.get("CC_COOKIECLOUD_KEY", "")
CC_PASSWORD = os.environ.get("CC_COOKIECLOUD_PASSWORD", "")
CC_DOMAIN = "fantia.jp"
CC_TIMEOUT = float(os.environ.get("CC_TIMEOUT", "8"))
CC_TTL = int(os.environ.get("CC_TTL", "3600"))  # seconds the cache stays fresh
CC_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        ".fantiaprod_cookiecc.json")

_PROCESS_COOKIE = {"cookie": "", "ts": 0.0}


def _cc_passphrase():
    return hashlib.md5(
        ("%s-%s" % (CC_KEY, CC_PASSWORD)).encode("utf-8")
    ).hexdigest()[:16].encode("utf-8")


def _pkcs7_unpad(data):
    if not data:
        return data
    n = data[-1]
    if 1 <= n <= 16 and data[-n:] == bytes([n]) * n:
        return data[:-n]
    return data


def _build_tables():
    """AES S-box / inverse S-box (generated, not hardcoded)."""
    exp, log = [0] * 512, [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x ^= ((x << 1) ^ (0x1B if x & 0x80 else 0)) & 0xFF
        x &= 0xFF
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    sbox = [0] * 256
    for a in range(256):
        inv = 0 if a == 0 else exp[255 - log[a]]
        s = inv
        for _ in range(4):
            inv = ((inv << 1) | (inv >> 7)) & 0xFF
            s ^= inv
        sbox[a] = s ^ 0x63
    inv_sbox = [0] * 256
    for i, v in enumerate(sbox):
        inv_sbox[v] = i
    return sbox, inv_sbox


_SBOX, _INV_SBOX = _build_tables()
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36, 0x6C,
         0xD8, 0xAB, 0x4D]


def _gmul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _expand_key(key):
    """Key expansion for AES-128/192/256."""
    nk = len(key) // 4
    nr = nk + 6
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            t = [_SBOX[b] for b in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return w


def _add_round_key(s, w):
    for c in range(4):
        for r in range(4):
            s[r + 4 * c] ^= w[c][r]


def _inv_shift_rows(s):
    old = list(s)
    for c in range(4):
        for r in range(4):
            s[r + 4 * c] = old[r + 4 * ((c - r) % 4)]


def _inv_mix_columns(s):
    for c in range(4):
        a = [s[r + 4 * c] for r in range(4)]
        s[0 + 4 * c] = _gmul(a[0], 14) ^ _gmul(a[1], 11) ^ _gmul(a[2], 13) ^ _gmul(a[3], 9)
        s[1 + 4 * c] = _gmul(a[0], 9) ^ _gmul(a[1], 14) ^ _gmul(a[2], 11) ^ _gmul(a[3], 13)
        s[2 + 4 * c] = _gmul(a[0], 13) ^ _gmul(a[1], 9) ^ _gmul(a[2], 14) ^ _gmul(a[3], 11)
        s[3 + 4 * c] = _gmul(a[0], 11) ^ _gmul(a[1], 13) ^ _gmul(a[2], 9) ^ _gmul(a[3], 14)


def _pure_decrypt_block(block, w):
    nr = len(w) // 4 - 1
    s = list(block)
    _add_round_key(s, w[4 * nr:4 * nr + 4])
    for rnd in range(nr - 1, 0, -1):
        _inv_shift_rows(s)
        for i in range(16):
            s[i] = _INV_SBOX[s[i]]
        _add_round_key(s, w[4 * rnd:4 * rnd + 4])
        _inv_mix_columns(s)
    _inv_shift_rows(s)
    for i in range(16):
        s[i] = _INV_SBOX[s[i]]
    _add_round_key(s, w[0:4])
    return bytes(s)


def _aes_cbc_decrypt(key, iv, ciphertext):
    """AES-CBC decrypt with whatever backend the host python offers."""
    try:  # 1) pycryptodome
        from Crypto.Cipher import AES
        return _pkcs7_unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext))
    except ImportError:
        pass
    try:  # 2) cryptography
        from cryptography.hazmat.primitives.ciphers import Cipher, \
            algorithms, modes
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return _pkcs7_unpad(dec.update(ciphertext) + dec.finalize())
    except ImportError:
        pass
    # 3) built-in pure-python implementation
    w = _expand_key(key)
    out, prev = b"", iv
    for i in range(0, len(ciphertext), 16):
        block = ciphertext[i:i + 16]
        dec = _pure_decrypt_block(block, w)
        out += bytes(x ^ y for x, y in zip(dec, prev))
        prev = block
    return _pkcs7_unpad(out)


def _evp_bytes_to_key(password, salt, key_len=32, iv_len=16):
    """OpenSSL EVP_BytesToKey(MD5, iterations=1) as used by crypto-js."""
    out, prev = b"", b""
    while len(out) < key_len + iv_len:
        prev = hashlib.md5(prev + password + salt).digest()
        out += prev
    return out[:key_len], out[key_len:key_len + iv_len]


def extract_domain_cookie(cookie_data, domain, now=None):
    """Merge all cookies of `domain` and its subdomains into one header value."""
    domain = domain.lower().lstrip(".")
    now = now if now is not None else time.time()
    matched = []
    for k, items in (cookie_data or {}).items():
        if not isinstance(items, list):
            continue
        kk = str(k).lower().lstrip(".")
        if kk == domain or kk.endswith("." + domain):
            matched.append((len(kk), items))
    matched.sort(key=lambda x: x[0])  # bare domain first, subdomains override
    merged, dropped = {}, []
    for _, items in matched:
        for c in items:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name", "")).strip()
            value = c.get("value")
            if not name or value is None:
                continue
            exp = c.get("expirationDate")
            if exp:
                try:
                    exp = float(exp)
                    if exp > 1e12:
                        exp /= 1000
                    if 0 < exp < now:
                        dropped.append(name)
                        continue
                except (TypeError, ValueError):
                    pass
            merged[name] = str(value)
    if dropped:
        _dbg("cookiecloud: dropped expired cookies: %s" % ", ".join(sorted(set(dropped))))
    return "; ".join("%s=%s" % (n, merged[n]) for n in sorted(merged))


def _cc_cache_read(max_age):
    try:
        with open(CC_CACHE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if time.time() - float(data.get("ts", 0)) <= max_age:
            cookie = data.get("cookie") or ""
            if cookie:
                _dbg("cookiecloud cache hit (%d chars, age %.0fs)"
                     % (len(cookie), time.time() - float(data["ts"])))
                return cookie
    except Exception:  # noqa: BLE001 -- missing/corrupt cache is not fatal
        pass
    return ""


def _cc_cache_write(cookie):
    try:
        with open(CC_CACHE, "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "cookie": cookie}, fh)
    except OSError as e:  # noqa: BLE001
        _dbg("cannot write cache %s: %s" % (CC_CACHE, e))


def cookie_from_cookiecloud(force=False):
    """Fetch fantia.jp cookies from CookieCloud; 1-hour on-disk cache.

    Proxy-free on purpose: the Stash container routes outbound traffic through
    HTTP(S)_PROXY, but the CookieCloud server sits on the same LAN.
    """
    if not CC_URL or not CC_KEY or not CC_PASSWORD:
        _dbg("cookiecloud: CC_COOKIECLOUD_URL/_KEY/_PASSWORD not set -- skipped")
        return ""
    if not force:
        cached = _cc_cache_read(CC_TTL)
        if cached:
            return cached
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            "%s/get/%s" % (CC_URL.rstrip("/"), CC_KEY),
            headers={"User-Agent": "Mozilla/5.0"})
        with op.open(req, timeout=CC_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        encrypted = payload.get("encrypted") or payload.get("data")
        if not encrypted:
            raise RuntimeError("encrypted field empty (plugin not uploaded?)")
        iv_b64 = payload.get("iv") or ""
        blob = base64.b64decode(encrypted)
        passphrase = _cc_passphrase()

        def _cryptojs(key_len):
            if blob[:8] != b"Salted__":
                raise ValueError("not crypto-js format")
            salt, ct = blob[8:16], blob[16:]
            k, iv = _evp_bytes_to_key(passphrase, salt, key_len, 16)
            return _aes_cbc_decrypt(k, iv, ct)

        def _fixed_iv():
            iv = base64.b64decode(iv_b64) if iv_b64 else b"\x00" * 16
            return _aes_cbc_decrypt(passphrase, iv, blob)

        if blob[:8] == b"Salted__":
            candidates = [("crypto-js/AES-256", lambda: _cryptojs(32)),
                          ("crypto-js/AES-128", lambda: _cryptojs(16)),
                          ("aes-128-fixed-iv", _fixed_iv)]
        else:
            candidates = [("aes-128-fixed-iv", _fixed_iv),
                          ("crypto-js/AES-256", lambda: _cryptojs(32))]

        data, used, last_err = None, "", None
        for name, fn in candidates:
            try:
                data = json.loads(fn().decode("utf-8"))
                used = name
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
        if data is None:
            raise RuntimeError("decrypt failed: %r" % (last_err,))
        cookie_data = data.get("cookie_data") or {}
        cookie = extract_domain_cookie(cookie_data, CC_DOMAIN)
        if not cookie:
            raise RuntimeError("no %s cookies in cloud data "
                               "(log into fantia.jp in the browser once)" % CC_DOMAIN)
        if "_session_id" not in cookie:
            _dbg("cookiecloud: no _session_id for %s -- not logged in?" % CC_DOMAIN)
        _dbg("cookiecloud ok via %s (%d chars)" % (used, len(cookie)))
        _cc_cache_write(cookie)
        return cookie
    except Exception as e:  # noqa: BLE001 -- degrade to stale cache / anonymous
        _dbg("cookiecloud fetch failed: %s" % e)
        stale = _cc_cache_read(30 * 86400)  # stale-but-working beats nothing
        if stale:
            _dbg("cookiecloud: falling back to stale cache")
            return stale
        return ""


def _fantia_cookie():
    """Process-level memoised cookie: env FANTIA_COOKIE > CookieCloud."""
    now = time.time()
    if _PROCESS_COOKIE["cookie"] and now - _PROCESS_COOKIE["ts"] < CC_TTL:
        return _PROCESS_COOKIE["cookie"]
    cookie, source = "", ""
    env_cookie = (os.environ.get("FANTIA_COOKIE") or "").strip()
    if env_cookie:
        cookie, source = env_cookie, "env"
    else:
        cookie = cookie_from_cookiecloud()
        source = "cookiecloud" if cookie else ""
    if cookie:
        _PROCESS_COOKIE["cookie"] = cookie
        _PROCESS_COOKIE["ts"] = now
        _dbg("using cookie (%d chars) from %s" % (len(cookie), source))
    else:
        _dbg("no cookie configured - only genuinely public products will work")
    return cookie


def _s(value) -> str:
    """Tolerant stringify: Fantia JSON field types are not stable (str/dict/int)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for k in ("large", "src", "url", "og", "orig", "main", "name", "text", "value"):
            if value.get(k):
                return _s(value[k])
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            got = _s(item)
            if got:
                return got
        return ""
    return str(value).strip()


def _strip_media_ext(name: str) -> str:
    low = name.lower()
    for ext in _MEDIA_EXT:
        if low.endswith(ext):
            return name[: -len(ext)]
    return name


# --------------------------------------------------------------------------- proxy
def _proxy_file() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, NAME + ".proxy"), NAME + ".proxy"):
        try:
            with open(cand, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        return line
        except OSError:
            continue
    return ""


def _resolve_proxy() -> str:
    # urllib would pick env vars up implicitly, but being explicit keeps the
    # behaviour identical for the hand-rolled opener and for tests.
    for var in ("FANTIAPROD_PROXY", "HTTPS_PROXY", "https_proxy",
                "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        val = (os.environ.get(var) or "").strip()
        if val:
            _dbg("proxy from %s: %s" % (var, val))
            return val
    val = _proxy_file()
    if val:
        _dbg("proxy from %s.proxy: %s" % (NAME, val))
    return val


def _opener(proxy: str):
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    else:
        handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(handler)


def fetch(url: str, timeout: int = None) -> str:
    timeout = timeout or int(os.environ.get("FANTIAPROD_TIMEOUT") or DEFAULT_TIMEOUT)
    proxy = _resolve_proxy()
    op = _opener(proxy)
    last = None
    cookie = _fantia_cookie()
    for attempt in range(3):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en;q=0.9",
        }
        if cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(url, headers=headers)
        try:
            with op.open(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            _dbg("HTTP %s for %s" % (exc.code, url))
            if exc.code in (404, 410):
                return ""            # product withdrawn / never existed -> {}
            last = exc
        except Exception as exc:     # noqa: BLE001 - network noise is expected
            _dbg("attempt %d failed: %r" % (attempt + 1, exc))
            last = exc
        op = _opener(proxy)          # a poisoned keep-alive tunnel never heals
    _dbg("giving up on %s (%r)" % (url, last))
    return ""


# ----------------------------------------------------------------------- parsing
def _meta_map(html: str) -> dict:
    """order-agnostic <meta> parser: property/name may follow content."""
    out = {}
    for tag in re.findall(r"<meta\b[^>]*>", html, re.I):
        key = re.search(r'(?:property|name)\s*=\s*"([^"]+)"', tag, re.I)
        val = re.search(r'content\s*=\s*"([^"]*)"', tag, re.I)
        if key and val and key.group(1) not in out:
            out[key.group(1)] = val.group(1)
    return out


def _ld_json_blocks(html: str) -> list:
    blocks = []
    for raw in re.findall(r'<script[^>]*type\s*=\s*"application/ld\+json"[^>]*>(.*?)</script>',
                          html, re.S | re.I):
        raw = raw.strip()
        if not raw:
            continue
        try:
            blocks.append(json.loads(raw))
        except ValueError:
            continue
    return blocks


def _find_blocks(blocks: list, wanted: str) -> dict:
    for block in blocks:
        items = block if isinstance(block, list) else [block]
        for item in items:
            if isinstance(item, dict) and item.get("@type") == wanted:
                return item
    return {}


def _content_viewed(blocks: list) -> dict:
    for block in blocks:
        items = block if isinstance(block, list) else [block]
        for item in items:
            if isinstance(item, dict) and item.get("event") == "content_viewed":
                return item
    return {}


def _catalogue_code(text: str) -> str:
    for raw in _RE_BRACKETS.findall(text or ""):
        cand = raw.strip()
        if _RE_CATALOGUE.match(cand):
            m = _RE_CATALOGUE_SPLIT.match(cand)
            if m:
                return "%s-%s" % (m.group(1), m.group(2))
    return ""


def parse_product(html: str, url: str) -> dict:
    if not html:
        return {}
    blocks = _ld_json_blocks(html)
    product = _find_blocks(blocks, "Product")
    if not product:
        # fantia.jp answers a non-existent /products/<id> with the LOGIN page
        # (HTTP 200 + og:title "ログイン｜ファンティア[Fantia]").  Without this
        # guard we would emit the login page's title/image as a "successful"
        # scrape -- the worst possible outcome.  A real product page always
        # carries the schema.org Product block.
        _dbg("no schema.org Product block (login/redirect page) for %s" % url)
        return {}
    viewed = _content_viewed(blocks)
    meta = _meta_map(html)

    brand = _s((product.get("brand") or {}).get("name"))
    og_title = _s(meta.get("og:title"))

    # ---- title: schema.org name is already clean; else trim the og:title tail
    title = _s(product.get("name"))
    if not title and og_title:
        title = re.sub(r"\s*[（(]?[^（()）]*[)）]?の商品.*$", "", og_title).strip() or og_title

    # ---- id + code
    pid = ""
    m = _RE_OG_URL_PROD.search(_s(meta.get("og:url")) or url or "")
    if m:
        pid = m.group(1)
    if not pid:
        m = _RE_OG_URL_PROD.search(url or "")
        if m:
            pid = m.group(1)
    if not pid:
        pid = _s(viewed.get("content_id"))

    code = _catalogue_code(title) or _catalogue_code(og_title)
    if not code and pid:
        code = "FANTIA-P" + pid

    # ---- image: JSON-LD image[] is the UNBLURRED original
    image_url = ""
    images = product.get("image")
    if isinstance(images, list):
        for item in images:
            got = _s(item)
            if got:
                image_url = got
                break
    else:
        image_url = _s(images)
    if not image_url:
        image_url = _s(meta.get("og:image")).replace("blurred_ogp_", "")
    image = ""
    if image_url:
        try:
            proxy = _resolve_proxy()
            req = urllib.request.Request(image_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"})
            with _opener(proxy).open(req, timeout=int(
                    os.environ.get("FANTIAPROD_TIMEOUT") or DEFAULT_TIMEOUT)) as resp:
                blob = resp.read()
            if blob:
                image = "data:image/jpeg;base64," + base64.b64encode(blob).decode("ascii")
        except Exception as exc:  # noqa: BLE001
            _dbg("image fetch failed %r" % exc)
            image = image_url

    # ---- details
    details = _s(product.get("description"))
    details = details.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")

    # ---- tags
    tags = []
    raw_tags = viewed.get("tag")
    if isinstance(raw_tags, list):
        tags = [_s(t) for t in raw_tags]
    elif isinstance(raw_tags, str):
        tags = [t.strip().strip('"') for t in raw_tags.split(",")]
    category = _s(viewed.get("fanclub_category"))
    if category and category not in tags:
        tags.append(category)
    tags = [t for t in tags if t]

    # ---- performer: the half-width (<handle>) right before の商品
    performer = ""
    pm = re.search(r"\(([^)]+)\)の商品", og_title or title)
    if pm:
        performer = pm.group(1).strip()

    scene = {
        "title": title,
        "urls": [url] if url else (["https://fantia.jp/products/" + pid] if pid else []),
        "studio": {"name": "Fantia.jp"},
    }
    if code:
        scene["code"] = code
    if details:
        scene["details"] = details
    if image:
        scene["image"] = image
    if tags:
        scene["tags"] = [{"name": t} for t in tags]
    if performer:
        scene["performers"] = [{"name": performer}]
    if brand:
        scene["director"] = brand
    _dbg("parsed id=%s code=%s tags=%d performer=%s" % (pid, code, len(tags), performer))
    return scene


# ------------------------------------------------------------------- id lookup
def _basename(path: str) -> str:
    return (path or "").replace("\\", "/").rsplit("/", 1)[-1]


def product_id_from_payload(payload: dict) -> str:
    """Resolve a /products/<id> from a Stash scene fragment. '' when unsure."""
    if not isinstance(payload, dict):
        return ""
    for url in ([payload.get("url")] if isinstance(payload.get("url"), str) else []) + \
               (payload.get("urls") or []):
        m = _RE_OG_URL_PROD.search(_s(url))
        if m:
            return m.group(1)
    m = _RE_CODE_P.match(_s(payload.get("code")))
    if m:
        return m.group(1)

    # `tagged` keeps directory names such as /[fantia-732915]/ in play -- this
    # user's library stores the id in the FOLDER while the basename is a plain
    # release name (H01-DLG-ver1.05-....mp4).  `bare` only ever sees a value
    # that is a number on its own, so a year in a path cannot be mistaken.
    tagged, bare = [], []
    for key in ("title", "name", "code", "path", "filename", "dir"):
        val = _s(payload.get(key))
        if not val:
            continue
        tagged.append(val)
        bare.append(_basename(val))
    for entry in payload.get("files") or []:
        if not isinstance(entry, dict):
            continue
        for key in ("basename", "filename"):
            val = _s(entry.get(key))
            if val:
                tagged.append(val)
                bare.append(_strip_media_ext(val))
        for key in ("path", "dir"):
            val = _s(entry.get(key))
            if val:
                tagged.append(val)
                bare.append(_strip_media_ext(_basename(val)))

    for cand in tagged:
        m = _RE_TAGGED_ID.search(_strip_media_ext(cand))
        if m:
            return m.group(1)
    # last resort: a bare numeric title (this user's library stores product ids
    # as the scene title, e.g. 887480).  A miss simply yields {} downstream.
    for cand in bare:
        m = _RE_BARE_ID.match(_strip_media_ext(cand).strip())
        if m:
            return m.group(1)
    return ""


# NOTE: there is deliberately no "echo the input fragment back" fallback here.
# Returning {} already preserves whatever the scene has, while an echo would
# make a filename-only scene look like a 1-hit success and win the concurrency
# race against the real targeted scrape (skill section 19.6a).


def _slug_from_url(text: str) -> str:
    if not text:
        return ""
    m = _RE_OG_URL_PROD.search(text)
    if m:
        return _PRODUCT_URL % m.group(1)
    return ""


def as_payload(arg: str) -> dict:
    """Stash hands script scrapers a JSON object on stdin, but a bare URL /
    bare id also has to work (and does during manual testing)."""
    text = (arg or "").strip()
    if not text:
        return {}
    if text[0] in "{[":
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    if text.startswith("http") or "/" in text:
        return {"url": text}
    return {"title": text, "code": text}


def mode_by_url(arg: str) -> dict:
    payload = as_payload(arg)
    url = ""
    for cand in [payload.get("url")] + list(payload.get("urls") or []):
        url = _slug_from_url(_s(cand))
        if url:
            break
    if not url:
        pid = product_id_from_payload(payload)
        url = (_PRODUCT_URL % pid) if pid else ""
    if not url:
        _dbg("no products URL in input %r" % (arg or "")[:120])
        return {}
    return parse_product(fetch(url), url)


def mode_by_query_fragment(arg: str) -> dict:
    return mode_by_url(arg)


def mode_by_fragment(arg: str) -> dict:
    payload = as_payload(arg)
    pid = product_id_from_payload(payload)
    if not pid:
        _dbg("fragment: no product id in input %r" % (arg or "")[:120])
        return {}
    url = _PRODUCT_URL % pid
    scene = parse_product(fetch(url), url)
    if not scene:
        _dbg("fragment: %s produced nothing (withdrawn / not a product)" % url)
    # Deliberately NO echo of the input fragment: echoing makes a filename-only
    # scene look like a 1-hit success and win the concurrency race against the
    # real targeted scrape (see skill section 19.6a).  {} means "no result".
    return scene


# ------------------------------------------------------------------------ main
MODES = {
    "sceneByURL": mode_by_url,
    "galleryByURL": mode_by_url,
    "sceneByQueryFragment": mode_by_query_fragment,
    "galleryByQueryFragment": mode_by_query_fragment,
    "sceneByFragment": mode_by_fragment,
    "galleryByFragment": mode_by_fragment,
}


def _read_input() -> str:
    """Payload arrives on stdin from Stash; fall back to argv for manual runs."""
    data = ""
    try:
        if not sys.stdin.isatty():
            data = sys.stdin.read() or ""
    except Exception:  # noqa: BLE001 - closed stdin is not fatal
        data = ""
    data = data.strip()
    if not data and len(sys.argv) > 2:
        data = sys.argv[2].strip()
    return data


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    payload = _read_input()
    handler = MODES.get(mode)
    result = {}
    if handler is None:
        _dbg("unknown mode %r (argc=%d)" % (mode, len(sys.argv)))
        result = {}
    else:
        try:
            result = handler(payload) or {}
        except Exception as exc:  # noqa: BLE001 - must never emit EOF
            import traceback
            _dbg("EXCEPTION in %s: %r" % (mode, exc))
            if os.environ.get("FANTIAPROD_DEBUG"):
                traceback.print_exc(file=sys.stderr)
            result = {}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
