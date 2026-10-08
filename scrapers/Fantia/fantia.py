#!/usr/bin/env python3
# Fantia (fantia.jp) scraper for Stash -- fragment-capable build.
# Copyright (C) 2026 stash-fantiajp contributors
#
# Derived from FantiaJp in stashapp/CommunityScrapers
# (https://github.com/stashapp/CommunityScrapers, commit b59e842d),
# licensed under the GNU Affero General Public License v3.0.
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Affero General Public License for more details.
"""Fantia (fantia.jp) scraper for Stash -- fragment-capable build.

Why this fork
-------------
Upstream ``FantiaJp`` (community stable, 2024-05-22) declares only
``sceneByURL`` / ``galleryByURL``.  Stash's "Scrape with ..." menu lists only
scrapers whose ``supported_scrapes`` include ``FRAGMENT``, so upstream never
appears in that menu -- you have to paste a post URL by hand every time.
This build adds ``sceneByFragment`` / ``galleryByFragment`` plus a local
post-id resolver, so the menu entry works.

It also fixes three upstream defects:
  * upstream reads the ``csrf-token`` but throws away the ``Set-Cookie`` from
    the same response, then makes a half-anonymous API call;
  * upstream lets ``IndexError`` / ``KeyError`` escape (a deleted or
    members-only post crashes the process instead of failing softly);
  * upstream prints diagnostics to stderr unconditionally -- Stash records
    every stderr line as an ERROR, so a *successful* scrape looked broken.

Modes (argv[1], case-insensitive)
---------------------------------
  post | sceneByURL | galleryByURL
      input {"url": "https://fantia.jp/posts/12345"}
  fragment | sceneByFragment | galleryByFragment
      input {"title": ..., "code": ..., "urls": [...], "filename": ...}

Auth
----
Fantia hides posts behind a login; ``/api/v1/posts/<id>`` answers HTTP 422
``{"error_text": "システムエラー"}`` for anything the caller may not see (it does
NOT distinguish "deleted" from "members-only" from "not logged in").
Supply a cookie, in priority order:
  1. env  FANTIA_COOKIE='_session_id=...'
  2. file ``fantia_cookie.txt`` next to this script
  3. file ``/root/.stash/fantia_cookie.txt`` (Stash config dir)
  4. the self-hosted CookieCloud server (fetched live, cached 1 h) -- needs
     CC_COOKIECLOUD_URL / CC_COOKIECLOUD_KEY / CC_COOKIECLOUD_PASSWORD env vars
Anonymous scraping only works for genuinely public posts.

Debug: set ``FANTIA_DEBUG=1`` to get stderr traces.
"""

import hashlib
import html as html_mod
import json
import os
import random
import re
import sys
import time
from datetime import datetime

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - reported, never raised
    sys.stderr.write("[Fantia] missing dependency: requests\n")
    sys.exit(0)

try:  # keep TLS-warning noise out of Stash's log
    import urllib3

    urllib3.disable_warnings()
except Exception:  # noqa: BLE001
    pass

NAME = "Fantia"
DEBUG = os.environ.get("FANTIA_DEBUG") == "1"
HERE = os.path.dirname(os.path.abspath(__file__))

# Retry policy for transient failures (batch scraping hits Fantia hard).
# RETRIES extra attempts for network errors / 429 / 5xx; 403 gets exactly
# one retry (it may be throttling, or a permanently bad cookie -- don't
# double every item's latency in the bad-cookie case).  422 never retries.
RETRIES = int(os.environ.get("FANTIA_RETRIES", "2"))
BACKOFF = float(os.environ.get("FANTIA_BACKOFF", "1.0"))  # seconds, exponential

BASE = "https://fantia.jp"
POST_URL = BASE + "/posts/%s"
API_URL = BASE + "/api/v1/posts/%s"
PRODUCT_URL = BASE + "/products/%s"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

RE_POST_IN_URL = re.compile(r"fantia\.jp/posts/(\d+)", re.I)
RE_PRODUCT_IN_URL = re.compile(r"fantia\.jp/products/(\d+)", re.I)
# Product scenes carry code FANTIA-P<id>; the P keeps them distinguishable
# from post ids when only the code survives on a scene.
RE_PRODUCT_CODE = re.compile(r"FANTIA-P(\d{5,})", re.I)
RE_ID_IN_TEXT = re.compile(r"(?:FANTIA[-_ ]?)?(\d{5,})", re.I)
RE_BARE_ID = re.compile(r"^\s*(\d{5,})\s*$")
RE_CSRF = re.compile(r'name=["\']csrf-token["\']\s+content=["\']([^"\']+)', re.I)
# Products pages ship structured data instead of a JSON API:
#   * class="gtm-json" blocks -- fanclub_name, tag[], content_id
#   * one application/ld+json block -- Product (name, image[], brand) and
#     VideoObject (uploadDate)
#   * <div class="product-description"> -- full, untruncated description
RE_GTM_JSON = re.compile(
    r'<script[^>]*class="[^"]*gtm-json[^"]*"[^>]*>(.*?)</script', re.S)
RE_JSON_LD = re.compile(
    r'<script type="application/ld\+json">\s*(\[.*?\])\s*</script>', re.S)


def _product_description(page):
    """Full product description text.

    Structure: <div class="product-description"><h3 ...>heading</h3>
    <div class="mb-30"> ... </div></div>.  Capture from after the heading
    until the section closes or the next <h3, capped for safety.
    """
    i = page.find("product-description")
    if i == -1:
        return ""
    h3 = page.find("</h3>", i)
    start = h3 + 5 if h3 != -1 else i
    rest = page[start:start + 12000]
    ends = [x for x in (rest.find("</div></div>"), rest.find("<h3"))
            if x != -1]
    return _strip_html(rest[:min(ends)] if ends else rest)
# Netscape cookies.txt: domain \t flag \t path \t secure \t expiry \t name \t value
RE_COOKIE_LINE = re.compile(
    r"^\s*([^\s#][^\t]*?)\t([^\t]*)\t([^\t]*)\t([^\t]*)\t([^\t]*)\t"
    r"([^\t]*)\t(.*)$")

# --- CookieCloud (optional self-hosted cookie sync) ------------------------ #
# Configure via environment variables; without them the scraper falls back to
# anonymous mode (only genuinely public posts).  In Stash, set these under
# Settings -> System -> Application Paths is NOT enough -- script scrapers
# inherit the stash server process env, so export them where Stash runs
# (e.g. docker-compose `environment:` or systemd Environment= lines).
CC_URL = os.environ.get("CC_COOKIECLOUD_URL", "")
CC_KEY = os.environ.get("CC_COOKIECLOUD_KEY", "")
CC_PASSWORD = os.environ.get("CC_COOKIECLOUD_PASSWORD", "")
CC_DOMAIN = "fantia.jp"
CC_TIMEOUT = float(os.environ.get("CC_TIMEOUT", "8"))
CC_TTL = int(os.environ.get("CC_TTL", "3600"))  # seconds the cache stays fresh
CC_CACHE = os.path.join(
    os.environ.get("CC_CACHE_DIR") or HERE, ".fantia_cookiecc.json")

# Keys Stash accepts in a scraped scene/gallery result.  Stash hands us its own
# update fragment (which carries `id`, `files`, ...); echoing those back logs
# `json: unknown field "id"`, so failures are filtered through this whitelist.
_FRAGMENT_KEYS = ("title", "code", "details", "director", "urls", "date",
                  "image", "studio", "tags", "performers")

DATE_FORMATS = ("%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S", "%Y/%m/%d")


def dbg(msg):
    """stderr is reserved for real problems unless FANTIA_DEBUG=1."""
    if DEBUG:
        sys.stderr.write("[%s] %s\n" % (NAME, msg))


def warn(msg):
    sys.stderr.write("[%s] %s\n" % (NAME, msg))


def safe_fragment(payload):
    """Echo the input back on failure so Stash keeps what the scene has."""
    return {k: v for k, v in (payload or {}).items() if k in _FRAGMENT_KEYS}


# --------------------------------------------------------------------------- #
# CookieCloud: fetch fantia.jp cookies from the self-hosted sync server
# --------------------------------------------------------------------------- #
def _evp_bytes_to_key(password, salt, key_len=32, iv_len=16):
    """OpenSSL EVP_BytesToKey(MD5, iterations=1) as used by crypto-js."""
    out, prev = b"", b""
    while len(out) < key_len + iv_len:
        prev = hashlib.md5(prev + password + salt).digest()
        out += prev
    return out[:key_len], out[key_len:key_len + iv_len]


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
    # 3) built-in pure-python implementation (NIST-test-vector grade)
    w = _expand_key(key)
    out, prev = b"", iv
    for i in range(0, len(ciphertext), 16):
        block = ciphertext[i:i + 16]
        dec = _pure_decrypt_block(block, w)
        out += bytes(x ^ y for x, y in zip(dec, prev))
        prev = block
    return _pkcs7_unpad(out)


# --------------------------------------------------------------------------- #
# Auth / HTTP
# --------------------------------------------------------------------------- #
def _read_cookie_text(path):
    """Parse one cookie file into a raw `Cookie:` header value."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError as e:  # noqa: BLE001
        dbg("cannot read %s: %s" % (path, e))
        return ""
    pairs = []
    for line in raw.splitlines():
        m = RE_COOKIE_LINE.match(line)
        if m:  # Netscape cookies.txt
            pairs.append("%s=%s" % (m.group(6), m.group(7)))
        elif "=" in line and not line.strip().startswith("#"):
            pairs.append(line.strip().rstrip(";"))
    return "; ".join(pairs)


def _cookie_from_file():
    """Search order: 1) next to this script, 2) Stash config dir (/root/.stash)."""
    candidates = [os.path.join(HERE, "fantia_cookie.txt"),
                  # HERE = .../.stash/scrapers/community/FantiaJp -> config is 3 up
                  os.path.abspath(os.path.join(HERE, "..", "..", "..",
                                               "fantia_cookie.txt"))]
    for path in candidates:
        if os.path.isfile(path):
            cookie = _read_cookie_text(path)
            if cookie:
                dbg("cookie loaded from %s (%d chars)" % (path, len(cookie)))
                return cookie
    return ""


def _cc_passphrase():
    return hashlib.md5(
        ("%s-%s" % (CC_KEY, CC_PASSWORD)).encode("utf-8")
    ).hexdigest()[:16].encode("utf-8")


def extract_domain_cookie(cookie_data, domain, now=None):
    """Merge all cookies of `domain` and its subdomains into one header value.

    Subdomains are applied after the bare domain so more-specific values win.
    Expired cookies (per expirationDate) are dropped.
    """
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
        dbg("cookiecloud: dropped expired cookies: %s" % ", ".join(sorted(set(dropped))))
    return "; ".join("%s=%s" % (n, merged[n]) for n in sorted(merged))


def _cc_cache_read(max_age):
    try:
        with open(CC_CACHE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if time.time() - float(data.get("ts", 0)) <= max_age:
            cookie = data.get("cookie") or ""
            if cookie:
                dbg("cookiecloud cache hit (%d chars, age %.0fs)"
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
        dbg("cannot write cache %s: %s" % (CC_CACHE, e))


def cookie_from_cookiecloud(force=False):
    """Fetch fantia.jp cookies from CookieCloud; 1-hour on-disk cache.

    Uses a proxy-free request: the Stash container routes outbound traffic
    through HTTP(S)_PROXY, but the CookieCloud server sits on the same LAN.
    """
    if not CC_URL or not CC_KEY or not CC_PASSWORD:
        dbg("cookiecloud: CC_COOKIECLOUD_URL/_KEY/_PASSWORD not set -- skipped")
        return ""
    if not force:
        cached = _cc_cache_read(CC_TTL)
        if cached:
            return cached

    try:
        import base64 as b64mod
        resp = requests.get("%s/get/%s" % (CC_URL.rstrip("/"), CC_KEY),
                            timeout=CC_TIMEOUT, verify=False,
                            proxies={"http": None, "https": None})
        resp.raise_for_status()
        payload = resp.json()
        encrypted = payload.get("encrypted") or payload.get("data")
        if not encrypted:
            raise RuntimeError("encrypted field empty (plugin not uploaded?)")
        iv_b64 = payload.get("iv") or ""
        blob = b64mod.b64decode(encrypted)
        passphrase = _cc_passphrase()

        def _cryptojs(key_len):
            if blob[:8] != b"Salted__":
                raise ValueError("not crypto-js format")
            salt, ct = blob[8:16], blob[16:]
            k, iv = _evp_bytes_to_key(passphrase, salt, key_len, 16)
            return _aes_cbc_decrypt(k, iv, ct)

        def _fixed_iv():
            iv = b64mod.b64decode(iv_b64) if iv_b64 else b"\x00" * 16
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
            raise RuntimeError("decrypt failed (%s): %r" % (last_err and
                              type(last_err).__name__, last_err))
        cookie_data = data.get("cookie_data") or {}
        cookie = extract_domain_cookie(cookie_data, CC_DOMAIN)
        if not cookie:
            raise RuntimeError("no %s cookies in cloud data "
                               "(log into fantia.jp in the browser once)" % CC_DOMAIN)
        if "_session_id" not in cookie:
            warn("cookiecloud: no _session_id for %s -- not logged in?" % CC_DOMAIN)
        dbg("cookiecloud ok via %s (%d chars)" % (used, len(cookie)))
        _cc_cache_write(cookie)
        return cookie
    except Exception as e:  # noqa: BLE001 -- degrade to older cache / anonymous
        dbg("cookiecloud fetch failed: %s" % e)
        stale = _cc_cache_read(30 * 86400)  # stale-but-working beats nothing
        if stale:
            dbg("cookiecloud: falling back to stale cache")
            return stale
        return ""


def new_session():
    """A requests.Session that keeps its own cookies (upstream lost them)."""
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": UA,
        "Accept-Language": "ja,en;q=0.8",
        "Referer": BASE + "/",
    })
    env_cookie = os.environ.get("FANTIA_COOKIE", "").strip()
    if env_cookie:
        cookie, source = env_cookie, "env"
    else:
        cookie = _cookie_from_file()
        source = "file" if cookie else ""
        if not cookie:
            cookie = cookie_from_cookiecloud()
            source = "cookiecloud" if cookie else ""
    if cookie:
        sess.headers["Cookie"] = cookie
        dbg("using cookie (%d chars) from %s" % (len(cookie), source))
    else:
        dbg("no cookie configured - only genuinely public posts will work")
    return sess


def get_csrf(sess):
    """Fetch the homepage; its cookies stick to `sess` and the token is returned."""
    try:
        r = sess.get(BASE + "/", timeout=15, verify=False)
    except requests.RequestException as e:
        dbg("homepage request failed: %s" % e)
        return ""
    m = RE_CSRF.search(r.text or "")
    if not m:
        dbg("csrf-token not found on homepage (status %s)" % r.status_code)
        return ""
    if not sess.cookies.get("_session_id"):
        dbg("warning: homepage set no _session_id")
    return m.group(1)


def fetch_post(sess, post_id, csrf):
    """GET the private JSON API. Returns (post_dict|None, message).

    Transient failures -- network errors, HTTP 429 / 5xx, and the first
    403 (which may just be throttling) -- are retried with exponential
    backoff + jitter.  422 is permanent (not visible) and never retried.
    """
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": POST_URL % post_id,
    }
    if csrf:
        headers["X-CSRF-Token"] = csrf
    attempts = 1 + max(0, RETRIES)
    last = "unreachable"
    for attempt in range(attempts):
        if attempt:
            delay = BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, 0.4)
            dbg("retry %d/%d for post %s in %.1fs (%s)"
                % (attempt, RETRIES, post_id, delay, last))
            time.sleep(delay)
        try:
            r = sess.get(API_URL % post_id, headers=headers, timeout=15,
                         verify=False)
        except requests.RequestException as e:
            last = "request failed: %s" % e
            continue
        if r.status_code == 422:
            return None, ("HTTP 422: post %s is not visible to this session "
                          "(deleted, members-only, or not logged in -- Fantia "
                          "does not distinguish)" % post_id)
        if r.status_code == 403:
            if attempt == 0:  # one retry: throttling heals, a bad cookie won't
                last = "HTTP 403: blocked (invalid cookie or IP throttled)"
                continue
            return None, last
        if r.status_code == 429 or r.status_code >= 500:
            last = "HTTP %s" % r.status_code
            continue
        if r.status_code != 200:
            return None, "HTTP %s" % r.status_code
        try:
            data = r.json()
        except ValueError:
            return None, "response was not JSON"
        post = data.get("post")
        if not isinstance(post, dict):
            return None, "JSON contained no 'post' object"
        return post, ""
    return None, last


# --------------------------------------------------------------------------- #
# Mapping to Stash
# --------------------------------------------------------------------------- #
def _s(v):
    """Coerce a JSON value into a clean string.

    Fantia's post JSON is not consistent: thumb/title-like fields are
    sometimes dicts ({"og": .., "large": ..}, {"src": ..}) instead of
    strings, which crashed str.strip() with AttributeError.  Dig into
    the common sub-keys instead of blowing up.
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        for k in ("large", "src", "url", "og", "orig", "main", "name", "text"):
            if v.get(k):
                return _s(v[k])
        return ""
    return str(v).strip()


def pic(post):
    for key in ("thumb_large", "thumb", "thumb_micro"):
        v = _s(post.get(key))
        if v:
            # upstream trick: the micro thumbnail URL nests the full-size one
            return v.replace("micro_", "") if key == "thumb_micro" else v
    return ""


def parse_date(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = re.match(r"(\d{4})[-/](\d{2})[-/](\d{2})", raw)
    return "%s-%s-%s" % m.groups() if m else ""


def build(post, post_id):
    fanclub = post.get("fanclub") or {}
    creator = _s(fanclub.get("creator_name"))
    performers = []
    if creator:
        img = ""
        user = fanclub.get("user") or {}
        if isinstance(user.get("image"), dict):
            img = (user["image"].get("large") or "").strip()
        performers.append({"name": creator, "image": img} if img
                          else {"name": creator})
    tags = []
    for t in post.get("tags") or []:
        n = (t or {}).get("name")
        if n:
            tags.append({"name": n})
    out = {
        "title": _s(post.get("title")),
        "code": "FANTIA-%s" % post_id,
        "details": _s(post.get("comment")),
        "urls": [POST_URL % post_id],
        "date": parse_date(post.get("posted_at")),
        "image": pic(post),
        "studio": {"name": "Fantia.jp"},
        "performers": performers,
        "tags": tags,
    }
    return {k: v for k, v in out.items() if v}


def scrape_url(url, sess, csrf_holder):
    """Scrape by post or product URL. `csrf_holder` is a 1-item list."""
    url = url or ""
    m = RE_POST_IN_URL.search(url)
    if m:
        return scrape_id(m.group(1), sess, csrf_holder)
    m = RE_PRODUCT_IN_URL.search(url)
    if m:
        return scrape_product_id(m.group(1), sess)
    dbg("no post/product id in %r" % url)
    return {}


def scrape_id(post_id, sess, csrf_holder):
    if not csrf_holder:
        csrf_holder.append(get_csrf(sess))
    t0 = time.time()
    post, msg = fetch_post(sess, post_id, csrf_holder[0])
    if post is None:
        warn("post %s: %s" % (post_id, msg))
        return {}
    frag = build(post, post_id)
    dbg("scraped %r in %.2fs" % (frag.get("title"), time.time() - t0))
    return frag


# --------------------------------------------------------------------------- #
# Products (fantia.jp/products/<id>) -- HTML scraping, there is no JSON API
# --------------------------------------------------------------------------- #
def _strip_html(fragment):
    """HTML -> plain text, keeping line structure of <p> and <br>."""
    txt = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    txt = re.sub(r"(?i)</p\s*>", "\n", txt)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = html_mod.unescape(txt)
    lines = [ln.strip() for ln in txt.splitlines()]
    out, blank = [], False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def parse_product(page):
    """Extract a product fragment dict from the /products/<id> HTML page."""
    gtm = {}
    for m in RE_GTM_JSON.finditer(page):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("content_type") == "product":
            gtm = data
            break
    ld_product, ld_video = {}, {}
    for m in RE_JSON_LD.finditer(page):
        try:
            arr = json.loads(m.group(1))
        except ValueError:
            continue
        if not isinstance(arr, list):
            arr = [arr]
        for item in arr:
            if not isinstance(item, dict):
                continue
            if item.get("@type") == "Product" and item.get("image"):
                ld_product = item
            elif item.get("@type") == "VideoObject":
                ld_video = item

    title = _s(ld_product.get("name")) or _s(gtm.get("content_title"))
    if not title:
        return None

    image = ""
    for u in ld_product.get("image") or []:
        if isinstance(u, str) and u.strip():
            # micro_ thumbnails embed the full-size URL, same trick as posts
            image = u.replace("micro_", "") if "micro_" in u else u
            break

    date = _s(ld_video.get("uploadDate"))[:10]

    details = _product_description(page)
    if not details:
        details = _s(ld_product.get("description"))

    performers = []
    creator = _s(gtm.get("fanclub_name"))
    if creator:
        performers.append({"name": creator})

    tags = [{"name": _s(t)} for t in gtm.get("tag") or [] if _s(t)]

    return {
        "title": title,
        "details": details,
        "date": date,
        "image": image,
        "performers": performers,
        "tags": tags,
    }


def fetch_product(sess, product_id):
    """GET the /products/<id> page. Returns (html|None, message)."""
    url = PRODUCT_URL % product_id
    attempts = 1 + max(0, RETRIES)
    last = "unreachable"
    for attempt in range(attempts):
        if attempt:
            delay = BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, 0.4)
            dbg("retry %d/%d for product %s in %.1fs (%s)"
                % (attempt, RETRIES, product_id, delay, last))
            time.sleep(delay)
        try:
            r = sess.get(url, timeout=15, verify=False)
        except requests.RequestException as e:
            last = "request failed: %s" % e
            continue
        if r.status_code == 404 or r.status_code == 410:
            return None, "HTTP %s: product %s does not exist" % (
                r.status_code, product_id)
        if r.status_code == 403:
            if attempt == 0:
                last = "HTTP 403: blocked (invalid cookie or IP throttled)"
                continue
            return None, last
        if r.status_code == 429 or r.status_code >= 500:
            last = "HTTP %s" % r.status_code
            continue
        if r.status_code != 200:
            return None, "HTTP %s" % r.status_code
        return r.text, ""
    return None, last


def build_product(pdata, product_id):
    out = {
        "title": pdata.get("title") or "",
        "code": "FANTIA-P%s" % product_id,
        "details": pdata.get("details") or "",
        "urls": [PRODUCT_URL % product_id],
        "date": pdata.get("date") or "",
        "image": pdata.get("image") or "",
        "studio": {"name": "Fantia.jp"},
        "performers": pdata.get("performers") or [],
        "tags": pdata.get("tags") or [],
    }
    return {k: v for k, v in out.items() if v}


def scrape_product_id(product_id, sess):
    t0 = time.time()
    page, msg = fetch_product(sess, product_id)
    if page is None:
        warn("product %s: %s" % (product_id, msg))
        return {}
    pdata = parse_product(page)
    if pdata is None:
        warn("product %s: page contained no product data" % product_id)
        return {}
    frag = build_product(pdata, product_id)
    dbg("scraped product %r in %.2fs" % (frag.get("title"), time.time() - t0))
    return frag


# --------------------------------------------------------------------------- #
# Fragment -> post id (no network)
# --------------------------------------------------------------------------- #
def _stem(path):
    base = os.path.basename((path or "").replace("\\", "/"))
    return os.path.splitext(base)[0].strip()


def post_id_from_payload(payload):
    """Resolve (kind, id) locally; kind is "post" or "product".

    A hit means ONE request instead of a search.
    """
    # 1) a fantia URL already on the scene -- product first (disjoint patterns)
    for u in list(payload.get("urls") or []) + [payload.get("url") or ""]:
        m = RE_PRODUCT_IN_URL.search(u or "")
        if m:
            return "product", m.group(1)
        m = RE_POST_IN_URL.search(u or "")
        if m:
            return "post", m.group(1)
    # 2) our own code field (Stash feeds it back on re-scrapes)
    code = (payload.get("code") or "").strip()
    if code:
        m = RE_PRODUCT_CODE.search(code)
        if m:
            return "product", m.group(1)
        m = RE_ID_IN_TEXT.search(code)
        if m:
            return "post", m.group(1)
    # 3) id embedded in the filename, e.g. "FANTIA-1234567.mp4" or
    #    "FANTIA-P1035222.mp4"
    fname = payload.get("filename") or payload.get("path") or ""
    if fname:
        m = RE_PRODUCT_CODE.search(_stem(fname))
        if m:
            return "product", m.group(1)
        m = RE_ID_IN_TEXT.search(_stem(fname))
        if m:
            return "post", m.group(1)
    # 4) bare id used as the whole filename stem or title -- files ripped from
    #    Fantia are often named "12345678.mp4".  Weak signal, but a wrong guess
    #    only costs one request: nothing parses, so main() echoes the fragment
    #    back and the scene keeps its existing metadata.
    for cand in (_stem(fname), (payload.get("title") or "").strip()):
        m = RE_BARE_ID.match(cand or "")
        if m:
            return "post", m.group(1)
    return "", ""


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def read_payload():
    raw = sys.stdin.read()
    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError as e:
        dbg("bad json input: %s" % e)
        return {}


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "post").strip().lower()
    payload = read_payload()
    by_url = mode in ("post", "url") or mode.endswith("byurl")
    sess = new_session()
    csrf_holder = []

    if by_url:
        url = (payload.get("url") or "").strip()
        frag = scrape_url(url, sess, csrf_holder) if url else {}
    else:  # *,ByFragment
        kind, post_id = post_id_from_payload(payload)
        if post_id:
            dbg("fragment resolved from local id (no search): %s %s"
                % (kind, post_id))
            if kind == "product":
                frag = scrape_product_id(post_id, sess)
            else:
                frag = scrape_id(post_id, sess, csrf_holder)
        else:
            dbg("no post id could be resolved from the fragment input")
            frag = {}
        if not frag:
            # Soft failure: never wipe metadata the scene already has.
            frag = safe_fragment(payload)
            if not frag and payload.get("urls"):
                frag = {"urls": payload["urls"]}

    sys.stdout.write(json.dumps(frag, ensure_ascii=False))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
