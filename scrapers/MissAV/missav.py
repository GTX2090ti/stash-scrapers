#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MissAV (missav.live / missav123.com) scraper for Stash -- action: script, pure stdlib.

Modes (argv[1]); the payload arrives as JSON on stdin:
  sceneByURL            {"url": "..."}                  -> ScrapedScene
  sceneByQueryFragment  {"url" | "title" | "code" | ...} -> ScrapedScene
  sceneByFragment       full scene fragment             -> ScrapedScene
  sceneByName           {"query" | "name" | "title"}    -> list of candidates
  performerByURL        {"url": ".../actresses/<Name>"} -> ScrapedPerformer
  performerByName       {"query" | "name"}              -> list of candidates

Site notes (all measured 2026-10; every one of these was a real bug first)

  * Video page URL: https://<host>/[dmNNN/]<locale>/<dvd_id>.  The locale may be
    omitted, in which case the site serves traditional Chinese; this script
    always uses /en/.

  * The URL's last segment (dvd_id) is NOT the catalogue code -- it is a site
    slug.  Examples: abp-123 -> ABP-123 (coincidence), dvaj-581 -> AJ-581,
    1pondo-* -> CARIB-* (a different studio entirely), ipzz-921 -> IPZZ-921.
    Deriving the code from the URL silently returns a *different, existing*
    title (ABP-123 -> BP-123 is the live example) and raises no error at all.
    The page's <span>Code:</span> row is therefore the single source of truth:
    the URL only locates the page, the code is always read from the page.

  * The info block is server-rendered label/value pairs, but the labels are
    localised: /en/ -> Release date / Code / Title / Genre / Series / Maker /
    Director / Actress; /cn/ -> 发行日期 / 番号 / 标题 / 类型 / 系列 / 发行商 /
    导演 / 女优.  See LABELS.  Two traps that each broke whole-page parsing:
      - on /cn/ the label is followed by a newline and indentation
        (<span>番号:</span>\n <span ...>), so anchors must be whitespace-
        tolerant regexes rather than literal strings;
      - the boundary-anchor table must hold *compiled regexes*.  Storing the
        candidate strings instead makes lookups miss silently (that table is
        keyed by logical name), _next_anchor then returns end-of-document, and
        the segment swallows the entire footer.

  * Performers differ per locale -- do not assume "none":
      /cn/ has a <span>女优:</span> row; missav123.com's English pages have an
      <span>Actress:</span> row.  The word is Actress, not Performers; missing
      that anchor lets the title segment run greedily into the following <a>
      and return the actress name as the title (IPZZ-921 yields "Sakai Mio").
      missav.live's /en/ pages carry neither -- leave the field empty instead of
      guessing a name from the title.

  * Actress pages (/actresses/<Name>): performerByURL / performerByName.  The
    name comes from og:title ("Watch <Name>'s AV Online"); images come from
    og:image (DMM) plus the portrait on fourhoi.com.
      - performerByURL returns a *single object*; returning [] makes Stash
        report "could not unmarshal json from script output";
      - performerByName must return a list of *objects*, not URL strings.
        Name search goes through the scene search page (/<locale>/search/<kw>),
        which renders an actress block above the scene cards.  The
        /actresses?search=<kw> endpoint ignores the search parameter entirely
        and answers with the unfiltered ranking (page 1 of 1479).

  * Primary title is the *original* title (the page's 标题 / Title row).  The
    machine-translated og:title (which starts with the code) is appended to
    details as an "EN:" line, and only used as a fallback when the page has no
    original-title row.  Do not use the <title> tag: it is truncated at 55
    characters, whereas og:title is complete.

  * Date comes from the page's release-date row only.  og:video:release_date is
    a re-upload date and can be years later (ABP-123: page 2014-04-01 vs og
    2023-04-22).

  * 404s are real, but the body's og:title is the *site* title ("MissAV | Watch
    HD JAV Online ...") and is easily mistaken for a valid page.  Always assert
    with _looks_like_scene().

  * The default Go user agent (Go-http-client) gets 403; any browser UA works,
    so one is set explicitly.

  * Covers live on a separate CDN (fourhoi.com).  That CDN rejects requests
    whose Referer points at itself (403); a missing Referer, or one pointing at
    a missav host, returns 200.  See _headers().

Mirrors and the canonical host

  * The script prefers missav.live (~1 s) and falls back to missav123.com.  The
    yml declares both, because sceneByURL only matches by URL prefix and then
    hands the URL straight to this script.
  * URLs written to the database are normalised to missav123.com.  The original
    reason was a belief that Stash re-fetches stored URLs with its own Go client
    -- to which missav.live answers 403 (TLS/JA3 filtering that scraperUserAgent
    does not fix).  Measured 2026-10-08 on Stash v0.31.1: that belief is wrong
    for action: script scrapers.  Stash never fetches the page; it selects a
    scraper by prefix and runs the script.  The normalisation is kept, but only
    so that one scene reached through two mirrors dedupes to a single URL.

Environment:
  MISSAV_DEBUG=1       stderr diagnostics (silent by default -- Stash records
                       every stderr line as an ERROR log entry)
  MISSAV_TIMEOUT=20    per-request timeout in seconds
  MISSAV_RETRIES=2     per-request retries
  MISSAV_MIRRORS       comma-separated mirrors, overrides the built-in list
  MISSAV_PROXY         proxy used by this scraper only, e.g.
                       http://192.168.2.210:7890.  Preferred over a global
                       HTTPS_PROXY, which would also reroute Stash's own
                       traffic.  Falls back to HTTP(S)_PROXY / ALL_PROXY.

Self-test (offline):
  MISSAV_SELFTEST=1 python3 missav.py
  Page-level assertions additionally need missav_ABP-123.html and
  missav_kv139_cn.html next to the script; they are skipped when absent.
"""

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

NAME = "MissAV"
DEBUG = os.environ.get("MISSAV_DEBUG", "").strip() not in ("", "0", "false", "False")

# missav.live 最快；missav123.com 慢但唯一能过 Stash 引擎（Go 客户端）的风控。
# 其余镜像当前对本出口 403/SSL 失败，留作可配置兜底。
DEFAULT_MIRRORS = ("missav.live", "missav123.com")
# ★ 写进库、也写进 yml urls 的规范域名。选 missav123.com 的唯一理由：
#   Stash 的 scrapeURL 会用**自己的 Go 客户端**去抓被匹配的 URL（不走我们的脚本），
#   实测 missav.live / missav.ai / missav.ws 对 Go 一律 403（设 scraperUserAgent=Chrome 也一样，
#   属 TLS/JA3 指纹风控），只有 missav123.com 200。存 live 的 URL 会让日后重刮必 403。
CANONICAL_HOST = "missav123.com"

LOCALE = "en"  # 统一用英文站：Title 字段是日文原题，og:title 是英文翻译，两者互补

_TIMEOUT = float(os.environ.get("MISSAV_TIMEOUT", "20") or 20)
_RETRIES = int(os.environ.get("MISSAV_RETRIES", "2") or 2)


def _mirrors():
    raw = os.environ.get("MISSAV_MIRRORS", "").strip()
    if not raw:
        return list(DEFAULT_MIRRORS)
    out = [m.strip().lower().replace("https://", "").replace("http://", "").strip("/")
           for m in raw.split(",")]
    return [m for m in out if m]


def dbg(msg):
    """诊断输出走 stderr 且默认静默 —— Stash 会把 stderr 每行记成 ERROR 日志。"""
    if DEBUG:
        sys.stderr.write("[%s] %s\n" % (NAME, msg))
        sys.stderr.flush()


# ---------------------------------------------------------------- HTTP

def _opener():
    """显式读取容器的 HTTP(S)_PROXY 环境变量。

    ★ urllib 的 ProxyHandler 传空 dict 会**禁用**环境变量代理，所以必须自己回落读取；
      想让进程走代理又不想影响 Stash 其它流量时，用 MISSAV_PROXY 单点覆盖。
    """
    proxies = {}
    explicit = os.environ.get("MISSAV_PROXY", "").strip()
    if explicit:
        proxies["http"] = proxies["https"] = explicit
        dbg("using explicit MISSAV_PROXY %s" % explicit)
    else:
        for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            v = os.environ.get(k)
            if v:
                proxies["http"] = proxies.get("http") or v
                proxies["https"] = proxies.get("https") or v
    if proxies:
        dbg("using proxy %s" % proxies["https"])
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxies))


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 封面 CDN（fourhoi.com）的根域。★ 实测：Referer 指向 CDN 自己会 403，
# 指向 missav 域或干脆不带都 200 —— 这是 CDN 的自引用防护，别写成 netloc。
COVER_REFERER = "https://missav.live/"


def _headers(netloc):
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    host = (netloc or "").lower()
    if "fourhoi" in host:
        h["Referer"] = COVER_REFERER          # 冒充站内取图
    elif "missav" in host:
        h["Referer"] = "https://%s/" % host
    # 其它域不带 Referer
    return h


def get(url, binary=False, timeout=None):
    """带重试的 GET。每次失败后重建 opener —— 中毒的 keep-alive 隧道不会自愈。"""
    tmo = timeout or _TIMEOUT
    last = None
    netloc = urllib.parse.urlparse(url).netloc
    for attempt in range(_RETRIES + 1):
        try:
            t0 = time.time()
            req = urllib.request.Request(url, headers=_headers(netloc))
            with _opener().open(req, timeout=tmo) as r:
                data = r.read()
            dbg("GET %s -> %s %dB %.2fs" % (url, r.status, len(data), time.time() - t0))
            return data if binary else data.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (404, 410):
                dbg("GET %s -> %s (terminal)" % (url, e.code))
                return None
            dbg("GET %s -> HTTP %s (attempt %d)" % (url, e.code, attempt + 1))
        except Exception as e:  # SSLEOF / ReadTimeout / ConnectionReset ...
            last = e
            dbg("GET %s -> %r (attempt %d)" % (url, e, attempt + 1))
        if attempt < _RETRIES:
            time.sleep(0.6 * (attempt + 1))
    dbg("GET %s failed finally: %r" % (url, last))
    return None


# ---------------------------------------------------------------- URL 归一

def canonical_url(url):
    """把任意 missav 镜像域名的 URL 归一到 CANONICAL_HOST，path / query 原样保留。

    ★ 只换 host、绝不动 path —— scene 页的 path 由 parse_scene 按番号单独构造，
      演员页（/actresses/<Name>）则直接把原 path 搬过来（missav123.com 上实测可用）。
    """
    if not url:
        return url
    p = urllib.parse.urlsplit(url)
    if not p.netloc or "missav" not in p.netloc.lower():
        return url
    return urllib.parse.urlunsplit(("https", CANONICAL_HOST, p.path, p.query, ""))


# ---------------------------------------------------------------- 解析

def _meta(html, prop):
    m = re.search(r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"' % re.escape(prop), html)
    if not m:
        m = re.search(r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="%s"' % re.escape(prop), html)
    return _unescape(m.group(1)).strip() if m else ""


def _safe_chr(n):
    """数字实体 -> 字符。越界/代理区一律给 U+FFFD。

    ★ 不能直接 chr()：落进代理区（U+D800-U+DFFF）会产出孤立代理码位，
      后面 json.dumps(ensure_ascii=False) 写出的就是非法 UTF-8，Stash 直接报解析错。
    """
    if n < 0 or n > 0x10FFFF or 0xD800 <= n <= 0xDFFF:
        return "\ufffd"
    return chr(n)


def _unescape(s):
    if not s:
        return ""
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&apos;", "'").replace("&nbsp;", " "))
    # ★ 数字实体必须用通用规则收，不能枚举两位写法：站点写的是 &#039;（三位、补零）
    #   而不是 &#39; —— 枚举式替换会漏，表现为 details 里残留 "&#039;"。
    s = re.sub(r"&#(\d+);", lambda m: _safe_chr(int(m.group(1))), s)
    s = re.sub(r"&#[xX]([0-9a-fA-F]+);", lambda m: _safe_chr(int(m.group(1), 16)), s)
    return re.sub(r"\s+", " ", s).strip()


# ★ missav 的字段标签**随 locale 变语言**，不是可配置的。实测：
#     /en/ -> Release date / Code / Title / Genre / Maker / Director / Series
#     /cn/ -> 发行日期 / 番号 / 标题 / 类型 / 发行商 / 导演 / 系列
#   所以标签一律用「逻辑名 -> 各语言写法」的映射表，绝不能只写英文。
LABELS = {
    "date":    ("Release date", "发行日期"),
    "code":    ("Code", "番号"),
    "title":   ("Title", "标题"),
    "genre":   ("Genre", "类型"),
    "maker":   ("Maker", "发行商"),
    "director": ("Director", "导演"),
    "series":  ("Series", "系列"),
    # 演员行。★ 英文站叫 Actress（不是 Performers）—— 漏了它会导致 Title:/标题: 段的
    # 边界锚点失效，_info_row 贪婪匹配到后面的 <a>，把**演员名**当原题返回。
    "performer": ("Actress", "Performers", "女优"),
}
# 边界锚点全集：截断信息区段落时用
# ★ 边界锚点全集：截断信息区段落时用。直接存编译好的正则 —— 若存候选串，
#   查表会拿 '类型' 之类当键去查 LABELS（键是 genre/date 这类逻辑名）而全部落空，
#   _next_anchor 会静默返回文末，导致段落吞掉整个页脚（曾产出 51 个假标签）。
_ALL_LABEL_RES = [re.compile(r"<span>\s*%s\s*:\s*</span>\s*" % re.escape(c))
                  for v in LABELS.values() for c in v if c]


# ★ 锚点是**正则**而非字符串：`/cn/` 站点的标签后面带换行 + 缩进
#     （<span>番号:</span>\n <span class="font-medium">KV-139</span>），
#     而 `/en/` 站点没有。用字符串 find 会整体漏匹配（曾表现为整页被判成 404）。
# ★ 必须**逐个候选尝试**：只试第一个（英文）会让 /cn/ 站整页匹配失败。
def _anchor_res(name):
    return [re.compile(r"<span>\s*%s\s*:\s*</span>\s*" % re.escape(c))
            for c in LABELS.get(name, ()) if c]


def _find_anchor(html, name):
    """返回该逻辑名标签之后的偏移；所有语言的写法都试一遍。找不到返回 -1。"""
    for rx in _anchor_res(name):
        m = rx.search(html)
        if m:
            return m.end()
    return -1


def _next_anchor(html, start):
    """从 ``start`` 往后找**任意语言**的下一个信息区标签，返回其起始偏移。"""
    best = len(html)
    for rx in _ALL_LABEL_RES:
        m = rx.search(html, start)
        if m and start <= m.start() < best:
            best = m.start()
    return best


def _looks_like_scene(html):
    """★ 防御软 404：404 页 body 的 og:title 是站点名，且没有番号行。"""
    og = _meta(html, "og:title")
    if any(mk in og for mk in SITE_TITLE_MARKERS):
        return False
    return _find_anchor(html, "code") >= 0


def _info_row(html, name):
    """信息区 label-value 取值。``name`` 取 LABELS 的键。

    页面形如  <div class="text-secondary"><span>Code:</span><span class="font-medium">ABP-123</span></div>
    值可能是 text，也可能是一串 <a>（Genre/Maker/Director/Series）。
    """
    i = _find_anchor(html, name)
    if i < 0:
        return []
    # 截到下一个 label（任意语言）或本区结束
    seg = html[i:_next_anchor(html, i + 1)]
    # 优先取 <a> 文本（多个 = 标签/厂牌/导演），否则取 font-medium span 的纯文本
    links = [_unescape(re.sub(r"<[^>]+>", "", a)) for a in re.findall(r"<a\b[^>]*>(.*?)</a>", seg, re.S)]
    links = [x for x in links if x]
    if links:
        return links
    m = re.search(r'class="font-medium"[^>]*>(.*?)</(?:span|time)>', seg, re.S)
    if m:
        return [_unescape(re.sub(r"<[^>]+>", "", m.group(1)))]
    m = re.search(r"<time[^>]*>(.*?)</time>", seg, re.S)
    if m:
        return [_unescape(re.sub(r"<[^>]+>", "", m.group(1)))]
    return []


def _first(html, name):
    v = _info_row(html, name)
    return v[0] if v else ""


# 番号：2+ 字母 + 可选分隔 + 数字。missav 的 dvd_id 本身就是番号（ABP-123）。
RE_CODE = re.compile(r"\b([A-Z]{2,}[-_ ]?\d{2,6})\b")
# 站内的 code 全大写带横线；dvd_id 段是小写或原样
RE_URL_CODE = re.compile(r"^/([a-z]{2}/)?(?:dm\d+/[a-z]{2}/)?([^/?#]+)/?$", re.I)
MEDIA_EXT = (".mp4", ".mkv", ".avi", ".wmv", ".mov", ".flv", ".ts", ".m4v", ".mpg", ".mpeg",
             ".rmvb", ".rm", ".webm", ".iso", ".m2ts", ".vob", ".asf", ".ogm", ".divx")
RE_SAFE_CODE = re.compile(r"^[A-Za-z]{2,}[-_]?\d{2,6}$")


def normalize_code(raw, strict=False):
    """把各种形态收敛成 ABP-123 形态；不像番号返回 ''。

    strict=True  —— 输入**就是**一个 code（页面 Code: 字段、搜索标题首词），
                    只允许整体匹配，绝不从中段截取。
    strict=False —— 输入是自由文本（og:title、文件名），允许定位子串。
    """
    if not raw:
        return ""
    s = str(raw).strip()
    for ext in MEDIA_EXT:
        if s.lower().endswith(ext):
            s = s[: -len(ext)]
            break
    s = re.sub(r"[\[\](){}]", " ", s)
    if strict:
        # 整体匹配：允许 "ABP-123" / "ABP 123" / "abp_123" / "ABP123"
        compact = re.sub(r"[\s_]+", "-", s).strip("-").upper()
        if not RE_SAFE_CODE.match(compact):
            return ""
        # "ABP123" -> "ABP-123"（拆字母段与数字段，两者都要非空）
        m2 = re.fullmatch(r"([A-Z]{2,})-?(\d{2,6})", compact)
        return "%s-%s" % (m2.group(1), m2.group(2)) if m2 else ""
    m = RE_CODE.search(s.upper())
    if not m:
        return ""
    code = m.group(1).upper().replace("_", "-").replace(" ", "-")
    # 去掉尾部分隔符残留
    code = re.sub(r"-{2,}", "-", code).strip("-")
    return code if RE_SAFE_CODE.match(code) else ""


def code_from_text(text):
    """从任意文本（文件名/title/URL）里挖番号。

    ★ 负向规则：绝不让媒体扩展名/分辨率被当成番号（mp4 -> MP-4、2160p -> ...）。
    """
    if not text:
        return ""
    s = str(text).strip()

    # 1) 本身就是 URL -> 取最后一段
    if "://" in s or s.lower().startswith("missav."):
        p = urllib.parse.urlparse(s if "://" in s else "https://" + s)
        segs = [x for x in p.path.split("/") if x]
        # 末段常带语言后缀/推荐锚
        for seg in reversed(segs[-2:] if len(segs) > 1 else segs):
            cand = normalize_code(seg)
            if cand:
                return cand
        s = segs[-1] if segs else ""

    # 2) 纯文件名/标题：先剥扩展名再匹配，且要求字母在前
    s = s.replace("_", " ").replace(".", " ") if re.search(r"[A-Za-z]{2,}[- ]?\d", s) else s
    for ext in MEDIA_EXT:
        if s.lower().endswith(ext):
            s = s[: -len(ext)]
            break
    # ★ 字母段必须从词首开始，否则 "1pondo-abc123" 会截成 ABC-123（丢掉厂牌段）
    m = re.search(r"(?:^|[^A-Za-z0-9])([A-Za-z]{2,})[-_ ]?(\d{2,6})(?![0-9])", s)
    if m:
        cand = "%s-%s" % (m.group(1).upper(), m.group(2))
        if RE_SAFE_CODE.match(cand):
            return cand
    return ""


# ---------------------------------------------------------------- 页面 -> payload

SITE_TITLE_MARKERS = (
    "Watch HD JAV Online",
    "MissAV | Watch",
    "Free &amp; High Quality AV",
    "Free & High Quality AV",
)


def _cover_data_uri(url):
    """封面转 base64 data URI。Stash 接受 URL，但内联更稳。失败回落原 URL。

    ★ 403 陷阱：CDN 会拒绝「Referer 指向自己」的请求，见 _headers() 的说明。
    """
    if not url:
        return ""
    for cand in (url, url.replace("cover-n", "cover-t"), url.replace("/cover-t", "/cover-n")):
        if not cand:
            continue
        raw = get(cand, binary=True, timeout=25)
        if raw and len(raw) > 1024:
            try:
                return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
            except Exception:
                break
    dbg("cover fetch failed, falling back to plain URL")
    return url


def parse_scene(html, page_url):
    """把作品页 HTML 解析成 Stash ScrapedScene。失败返回 {}。"""
    if not html or not _looks_like_scene(html):
        dbg("page is not a scene page (404/soft-404/redirect)")
        return {}

    code = normalize_code(_first(html, "code"), strict=True) or ""
    og_title = _meta(html, "og:title")              # _meta 内部已 unescape
    jp_title = _unescape(_first(html, "title"))      # 原题/原演员名，常与标题不符
    details = _meta(html, "og:description")

    if not code:
        # 兜底：从 og:title 头部抠（"<CODE> <english title>"）
        code = normalize_code(og_title)
    if not code:
        dbg("cannot resolve code")
        return {}

    # ★ 标题优先用**原题**（页面「标题:」行，日文/中文原题），这是库里最该显示的。
    #   og:title（英文机翻）只作为补充写进 details。
    #   两者都必须拿对：原题行的边界靠 LABELS 里的 Actress/女优 锚点截断 ——
    #   漏了 Actress 会让段落贪婪延伸到后面的 <a>，把演员名误当原题。
    #   注意别改用 <title> 标签：它被截断到 55 字符（"...of satisfac"）。
    title = jp_title or og_title or code
    if og_title and og_title != title:
        # og:title 首词是番号，Stash 里 title 单独显示时番号已由 code 字段承担，去掉更干净
        en = og_title[len(code):].strip() if og_title.startswith(code) else og_title
        if en:
            extra = "EN: %s" % en
            details = ("%s\n%s" % (details, extra)).strip() if details else extra

    # ★ 演员：英文站**没有**演员字段（官方 XPath 依赖的 og:video:actor 已消失），
    #   但中文站有：<span>女优:</span> 行 + og:video:actor 两个来源，交叉去重。
    #   拿不到就如实留空，不从标题猜人名（误判成本高）。
    performers = []
    for cand in _info_row(html, "performer") + [_meta(html, "og:video:actor")]:
        c = _unescape(cand)
        if c and c not in performers:
            performers.append(c)

    makers = _info_row(html, "maker")
    genres = _info_row(html, "genre")
    series = _info_row(html, "series")
    director = _first(html, "director")

    tags = []
    for t in genres + series + ([director] if director else []):
        if t and t.lower() != "porn stars" and t not in tags:
            tags.append(t)
    performers_out = [{"name": n} for n in performers]

    date = _first(html, "date")
    if date:
        date = date[:10]

    # ★ 存进库的 URL 统一规范到 CANONICAL_HOST（missav123.com）。
    #   原因：yml 的 sceneByURL 靠前缀匹配把 scrapeURL 路由给 Stash 自己的 Go 客户端去抓页面，
    #   而实测 missav.live/ai/ws 对 Go 的 TLS 指纹一律 403，只有 missav123.com 能过。
    #   若把 missav.live 的 URL 存进库，用户以后用「Search by URL」重刮就会 403。
    #   原始 og:url 保留在 details 里，便于回溯。
    og_url = _meta(html, "og:url") or page_url
    canonical = "https://%s/%s/%s" % (CANONICAL_HOST, LOCALE, code)
    if og_url and og_url != canonical:
        details = ("%s\nSource: %s" % (details, og_url)).strip() if details else "Source: %s" % og_url
    cover = _meta(html, "og:image")

    payload = {
        "title": title,
        "code": code,
        "urls": [canonical],
        "details": details,
        "tags": [{"name": t} for t in tags],
    }
    if date:
        payload["date"] = date
    if makers:
        payload["studio"] = {"name": makers[0]}
    if performers_out:
        payload["performers"] = performers_out
    if cover:
        data_uri = _cover_data_uri(cover)
        if data_uri:
            payload["image"] = data_uri if data_uri.startswith("data:") else cover
    dbg("parsed %s title=%r date=%s tags=%d" % (code, title[:40], date, len(tags)))
    return payload


def scrape_code(code):
    """按番号直连作品页 —— 走这条快路径只需 1 个请求。"""
    for host in _mirrors():
        url = "https://%s/%s/%s" % (host, LOCALE, code)
        html = get(url)
        if not html:
            continue
        p = parse_scene(html, url)
        if p:
            # 归一到 canonical URL
            return p
    dbg("scrape_code failed for %s on all mirrors" % code)
    return {}


# ---------------------------------------------------------------- 搜索

# 搜索结果卡片：<div class="thumbnail group"> ... </div>
# ★ 关键：URL 末段（dvd_id）**不是番号**（dvaj-581 的真番号是 AJ-581，1pondo-* 是 CARIB），
#   只有卡片标题文本的首词才是真番号（"ABP-123 Momoka Sakai, ..."）。
#   所以 code 一律从标题文本取，绝不从 URL 反推 —— 否则会稳定返回另一个真实存在的作品。
RE_RESULT_CARD = re.compile(r'class="thumbnail group"', re.I)
RE_CARD_LINK = re.compile(r'<a[^>]+href="(https://(?:missav\.[a-z]+)/[^"]+)"[^>]*alt="([^"]*)"', re.I)
# 标题锚点：class="text-secondary ..."，属性间可能有换行 -> 用 [\s\S] 兜住
RE_CARD_TITLE = re.compile(
    r'<a(?=[^>]*class="text-secondary)[^>]*>([\s\S]{5,200}?)</a>', re.I)
RE_CARD_IMG = re.compile(r'data-src="(https://fourhoi\.com/[^"]+cover-t\.jpg)"', re.I)
RE_CARD_DUR = re.compile(r'text-xs text-nord5 bg-gray-800[^>]*>\s*([\d:]+)\s*<', re.I)
# 标题文本首词：字母+数字，可能带 studio 前缀残留，这里只做形状校验，具体对错靠页面 Code: 复核
RE_LEAD_CODE = re.compile(r"^\s*([A-Za-z]{2,}[-_]?\d{2,6})\b")


def search(query, limit=10):
    """服务端渲染的搜索结果页，解析出候选场景数组。"""
    kw = urllib.parse.quote_plus(query.strip())
    out = []
    seen = set()
    for host in _mirrors():
        url = "https://%s/%s/search/%s" % (host, LOCALE, kw)
        html = get(url)
        if not html:
            continue
        blocks = [html[m.end():] for m in RE_RESULT_CARD.finditer(html)]
        for blk in blocks:
            seg = blk[:5000]
            m = RE_CARD_LINK.search(seg)
            if not m:
                continue
            link = m.group(1)
            # 去掉语言切换后缀（?lang=xx / #anchor）保持 URL 干净
            link = link.split("#")[0].split("?")[0]
            # 标题文本优先（首词是真番号），退化用 alt
            title_txt = ""
            mt = RE_CARD_TITLE.search(seg)
            if mt:
                title_txt = _unescape(re.sub(r"<[^>]+>", "", mt.group(1)))
            if not title_txt:
                title_txt = _unescape(m.group(2))
            mc = RE_LEAD_CODE.search(title_txt)
            code = normalize_code(mc.group(1), strict=True) if mc else ""
            if not code or code in seen:
                continue
            seen.add(code)
            item = {"title": title_txt or code, "code": code, "urls": [link]}
            mi = RE_CARD_IMG.search(seg)
            if mi:
                item["image"] = mi.group(1)
            md = RE_CARD_DUR.search(seg)
            if md:
                item["details"] = "Duration: %s" % md.group(1)
            out.append(item)
            if len(out) >= limit:
                return out
        if out:
            break
    return out


# ---------------------------------------------------------------- 入口适配

_FRAGMENT_KEYS = ("title", "code", "details", "director", "urls", "url", "date",
                 "image", "studio", "tags", "performers")


def safe_fragment(p):
    """回吐 fragment 时必须白名单过滤，否则 Stash 记 unknown field 警告。"""
    return {k: v for k, v in (p or {}).items() if k in _FRAGMENT_KEYS}


def read_input():
    """Stash 把 payload 以 JSON 写到 stdin；argv[1] 只是模式名，不是参数。"""
    data = ""
    try:
        if not sys.stdin.isatty():
            data = sys.stdin.read() or ""
    except Exception:
        data = ""
    data = data.strip()
    if not data and len(sys.argv) > 2:
        data = sys.argv[2].strip()
    if not data:
        return {}
    try:
        v = json.loads(data)
    except Exception:
        v = data.strip().strip('"')     # 裸 URL / 裸字符串
    if isinstance(v, str):
        return {"url": v} if "://" in v or "/" in v else {"title": v}
    return v if isinstance(v, dict) else {}


def _candidate_texts(payload):
    """按优先级收集可能含番号的文本：code / title / urls / 文件名。"""
    out = []
    for k in ("code", "title", "details"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    for k in ("url",):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    for u in (payload.get("urls") or []):
        if isinstance(u, str) and u.strip():
            out.append(u.strip())
    for f in (payload.get("files") or []):
        if not isinstance(f, dict):
            continue
        # ★ path 要整条扫，不只 basename —— 番号常常只出现在目录名里
        for k in ("path", "basename"):
            v = f.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
    return out


def do_scene_by_url(url):
    """URL 入口。兼容裸番号字符串。"""
    if not url:
        return {}
    if "://" not in url and "/" not in url:
        code = code_from_text(url) or normalize_code(url)
        return scrape_code(code) if code else {}
    html = get(url)
    if not html:
        # 换镜像重建
        p = urllib.parse.urlparse(url)
        segs = [s for s in p.path.split("/") if s]
        code = ""
        for seg in reversed(segs):
            code = normalize_code(seg)
            if code:
                break
        return scrape_code(code) if code else {}
    return parse_scene(html, url)


def do_fragment(payload):
    """fragment 入口：先本地解析番号（0 请求），失败再搜索。"""
    texts = _candidate_texts(payload)
    for t in texts:
        code = code_from_text(t)
        if code:
            dbg("fragment resolved code=%s from %r" % (code, t[:50]))
            r = scrape_code(code)
            if r:
                return r
    # 番号解析失败：拿 title 关键词去搜索
    for t in texts:
        if len(t) < 3:
            continue
        cands = search(t, limit=3)
        cands = [c for c in cands if c.get("code")]
        if cands:
            dbg("fragment resolved via search for %r -> %s" % (t[:40], cands[0]["code"]))
            got = scrape_code(cands[0]["code"])
            if got:
                return got
    dbg("fragment unresolved; echoing input to preserve original data")
    return safe_fragment(payload)


def do_name(query):
    """名称入口：必须返回 JSON **数组**（Stash 会逐元素反序列化成 ScrapedScene）。"""
    q = (query or "").strip()
    if not q:
        return []
    # 纯番号走快路径，直接返回单条
    code = normalize_code(q) or code_from_text(q)
    if code and RE_SAFE_CODE.match(code):
        p = scrape_code(code)
        return [p] if p else []
    return search(q)


# ---------------------------------------------------------------- 演员

# 演员页 URL = https://<host>/<locale>/actresses/<Name>
#   页面没有 span 标签式的资料表，可用字段只有三个：
#     og:title -> "Watch <Name>'s AV Online"（名字唯一来源）
#     og:image -> DMM 官方头像（pics.dmm.co.jp，http 明文）
#     fourhoi.com/actress/<id>-t.jpg -> 竖版图
RE_ACTOR_NAME = re.compile(r"Watch\s+(.+?)(?:'s|&#039;s)\s+AV\s+Online", re.I)
RE_HORO_IMG = re.compile(r'https://fourhoi\.com/actress/(\d+)-(t|n)\.jpg', re.I)

# 演员卡片（搜索结果页里作品列表**之前**的区块）:
#   <a href="https://<host>/<locale>/actresses/<Name>" class="text-nord13">
#     <div class="..."><img src="https://fourhoi.com/actress/<id>-t.jpg" alt="Sakai Mio" ...></div>
#   </a>
# ★ 演员名搜索**不能**用 /<locale>/actresses?search=<kw>：该端点的 search 参数被服务端忽略，
#   返回的是全量演员榜（第一页 25 人、翻页到 1479 页），与关键词无关。
#   真正带演员命中结果的是**场景搜索页** /<locale>/search/<kw>，它在作品卡片前先给演员区块。
RE_ACTRESS_CARD = re.compile(
    r'<a[^>]+href="(https://[^"]*?/actresses/[^"]+)"[^>]*>\s*'
    r'<div[^>]*>\s*<img[^>]+src="(https://fourhoi\.com/actress/\d+-t\.jpg)"[^>]*alt="([^"]*)"',
    re.I)


def scrape_performer(url):
    """演员页 -> Stash ScrapedPerformer。失败返回 {}。

    ★ 演员入口的返回形状与 scene 不同：必须回**单个对象**。
      返回 [] 会让 Stash 报 "could not unmarshal json from script output"。
    """
    html = get(url)
    if not html:
        return {}
    # 名字：og:title 形如 "Watch Sakai Mio's AV Online"
    name = ""
    m = RE_ACTOR_NAME.search(_meta(html, "og:title"))
    if m:
        name = _unescape(m.group(1)).strip()
    if not name:
        # 兜底：从 URL 的 actresses/<Name> 段取
        pu = urllib.parse.unquote(urllib.parse.urlparse(url).path)
        if "/actresses/" in pu:
            name = pu.rsplit("/actresses/", 1)[1].split("/")[0].replace("-", " ").strip()
    if not name:
        dbg("performer name unresolved")
        return {}

    # 头像：优先 DMM 官方图（og:image），竖版图作补充
    images = []
    dmm = _meta(html, "og:image")
    if dmm.startswith("http"):
        # ★ 页面给的是 http 明文；DMM 的 https 端点同样 200，统一升级（避免混合内容）
        images.append(re.sub(r"^http://", "https://", dmm))
    mh = RE_HORO_IMG.search(html)
    if mh:
        images.append("https://fourhoi.com/actress/%s-t.jpg" % mh.group(1))

    # 与 scene 同理：存进库的 URL 归一，保证日后重刮走的是 Stash 引擎能过的域名
    performer = {"name": name, "urls": [canonical_url(url)]}
    if images:
        performer["images"] = images
    dbg("performer %r images=%d" % (name, len(images)))
    return performer


def do_performer_by_url(url):
    u = (url or "").strip()
    if not u or "://" not in u:
        return {}
    return scrape_performer(u)


def do_performer_by_name(query):
    """演员名入口：返回 ScrapedPerformer **数组**。

    ★ 元素必须是对象。曾直接回传一批 URL 字符串，Stash 会因无法反序列化成
      ScrapedPerformer 而整条入口报错 —— 形状错了，内容再对也没用。
    """
    q = (query or "").strip()
    if not q:
        return []
    kw = urllib.parse.quote_plus(q)
    for host in _mirrors():
        html = get("https://%s/%s/search/%s" % (host, LOCALE, kw))
        if not html:
            continue
        out, seen = [], set()
        for m in RE_ACTRESS_CARD.finditer(html):
            link = _unescape(m.group(1)).split("?")[0].split("#")[0]
            name = _unescape(m.group(3))
            key = link.rsplit("/actresses/", 1)[-1]
            if not name or key in seen:
                continue
            seen.add(key)
            out.append({"name": name, "urls": [canonical_url(link)],
                        "images": [m.group(2)]})
            if len(out) >= 5:
                break
        if out:
            dbg("performerByName %r -> %d candidate(s)" % (q, len(out)))
            return out
    dbg("performerByName %r -> no actress card on search page" % q)
    return []


# ---------------------------------------------------------------- main

def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    payload = read_input()

    try:
        if mode in ("performerByURL", "performerByQueryURL"):
            # ★ 演员 URL 入口返回**单个对象**（不是数组）
            url = payload.get("url") or ""
            if not url and isinstance(payload.get("urls"), list) and payload["urls"]:
                url = payload["urls"][0]
            result = do_performer_by_url(url if isinstance(url, str) else "")
        elif mode in ("performerByName", "performerByQueryName"):
            q = payload.get("query") or payload.get("name") or ""
            result = do_performer_by_name(q if isinstance(q, str) else "")
        elif mode == "performerByFragment":
            url = payload.get("url") or ""
            result = do_performer_by_url(url.strip()) if isinstance(url, str) and "://" in url else {}
        elif mode in ("galleryByURL", "galleryByFragment", "galleryByQueryFragment"):
            # missav 没有图集，yml 未声明 gallery 入口；显式返回空而非报错
            result = {}
        elif mode in ("sceneByName", "sceneByQueryName"):
            query = payload.get("query") or payload.get("title") or payload.get("name") or ""
            if not query and isinstance(payload.get("url"), str):
                query = payload["url"]
            result = do_name(query)
        elif mode in ("sceneByFragment", "sceneByQueryFragment"):
            url = payload.get("url") or ""
            if isinstance(url, str) and url.strip() and "://" in url:
                # QueryFragment 入口收到的是完整 URL（§23：v0.31.1 走这条）
                result = do_scene_by_url(url.strip())
            else:
                result = do_fragment(payload)
        elif mode in ("sceneByURL", "sceneByQueryURL", "scene"):
            url = payload.get("url") or ""
            if not url and isinstance(payload.get("urls"), list) and payload["urls"]:
                url = payload["urls"][0]
            result = do_scene_by_url(url) if isinstance(url, str) else {}
        else:
            # 未知模式：尽力而为
            url = payload.get("url") or ""
            result = do_scene_by_url(url) if isinstance(url, str) and url else do_fragment(payload)
    except Exception as e:      # ★ 顶层兜底：任何异常也必须打印合法 JSON，否则 Stash 报 EOF
        dbg("unhandled exception: %r" % (e,))
        import traceback
        if DEBUG:
            traceback.print_exc(file=sys.stderr)
        # 返回形状必须与各入口约定一致：名称类入口要数组，其余要对象
        if mode in ("sceneByName", "sceneByQueryName", "performerByName",
                    "performerByQueryName"):
            result = []
        else:
            result = {}

    # 空结果必须是 []（名称类入口）/ {}（其余入口），绝不能被统一转成同一个形状
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.flush()
    return 0


# ---------------------------------------------------------------- 本地自检
if __name__ == "__main__" and os.environ.get("MISSAV_SELFTEST"):
    def _t(name, got, want):
        ok = got == want
        print("%-46s %s  got=%r want=%r" % (name, "PASS" if ok else "FAIL", got, want))
        return ok

    allok = True
    allok &= _t("code_from_text ABP-123.mp4", code_from_text("ABP-123.mp4"), "ABP-123")
    allok &= _t("code_from_text abp_123.mkv", code_from_text("abp_123.mkv"), "ABP-123")
    allok &= _t("code_from_text dir/SSIS-001/SSIS-001.mp4",
                code_from_text("/media/PT/SSIS-001/SSIS-001.mp4"), "SSIS-001")
    allok &= _t("code_from_text missav url",
                code_from_text("https://missav.live/dm13/en/ABP-123"), "ABP-123")
    allok &= _t("no code: 2024.mp4", code_from_text("2024.mp4"), "")
    allok &= _t("no code: 2160p.mkv", code_from_text("2160p.mkv"), "")
    allok &= _t("no code: 4K video.mp4", code_from_text("4K video.mp4"), "")
    allok &= _t("no code: mp4 only", code_from_text("mp4"), "")
    allok &= _t("normalize MIDV-123", normalize_code("MIDV-123"), "MIDV-123")
    allok &= _t("normalize arbon-001", normalize_code("arbon-001"), "ARBON-001")
    # ★ 锁住「从中间截取」这个假阳性：ABP-123 曾被解析成 BP-123（真实存在的另一个作品）
    allok &= _t("strict ABP-123", normalize_code("ABP-123", strict=True), "ABP-123")
    allok &= _t("strict abp123", normalize_code("abp123", strict=True), "ABP-123")
    allok &= _t("strict abp 123", normalize_code("abp 123", strict=True), "ABP-123")
    allok &= _t("strict reject '1pondo-abc123'", normalize_code("1pondo-abc123", strict=True), "")
    allok &= _t("loose still finds in text", normalize_code("ABP-123 Momoka Sakai"), "ABP-123")
    # ★ 锁住 dvd_id != 番号这条认知：搜索取码只能靠标题首词，不能靠 URL
    allok &= _t("LEAD_CODE from title", bool(RE_LEAD_CODE.search("ABP-123 Momoka Sakai, x")), True)
    allok &= _t("LEAD_CODE reject bare", bool(RE_LEAD_CODE.search("just a title")), False)
    # ★ 实体解码回归：站点用三位补零写法 &#039;，枚举式替换会漏
    allok &= _t("unescape &#039;", _unescape("Tsukasa&#039;s"), "Tsukasa's")
    allok &= _t("unescape &#x27;", _unescape("a&#x27;b"), "a'b")
    allok &= _t("unescape leaves no &#", "&#" in _unescape("a&#039;b"), False)
    allok &= _t("safe_chr surrogate guarded", _safe_chr(0xD800), "\ufffd")

    # ★ 演员卡片解析（离线夹具，取自搜索结果页真实结构）。
    #   回归点：(1) 导航里的 /actresses/ranking 不能被当成演员结果；
    #          (2) performerByName 的元素必须是对象，不能是 URL 字符串。
    card_html = (
        '<a href="https://missav.live/en/actresses/Sakai%20Mio" class="text-nord13">'
        '<div class="overflow-hidden mx-auto h-20 w-20 rounded-full">'
        '<img src="https://fourhoi.com/actress/1106362-t.jpg" alt="Sakai Mio" '
        'class="object-cover object-top w-full h-full"></div></a>'
        '<a href="https://missav.live/en/actresses/ranking" class="text-nord13">'
        '<div class="x"><span>Ranking</span></div></a>')
    cards = RE_ACTRESS_CARD.findall(card_html)
    allok &= _t("actress card count (ranking excluded)", len(cards), 1)
    allok &= _t("actress card name", cards[0][2] if cards else "", "Sakai Mio")
    allok &= _t("actress card image", cards[0][1] if cards else "",
                "https://fourhoi.com/actress/1106362-t.jpg")
    allok &= _t("canonical_url actress",
                canonical_url("https://missav.live/en/actresses/Sakai%20Mio"),
                "https://missav123.com/en/actresses/Sakai%20Mio")
    allok &= _t("canonical_url scene keeps path",
                canonical_url("https://missav.live/dm13/en/ABP-123?x=1"),
                "https://missav123.com/dm13/en/ABP-123?x=1")
    allok &= _t("canonical_url leaves foreign host alone",
                canonical_url("https://fourhoi.com/actress/1-t.jpg"),
                "https://fourhoi.com/actress/1-t.jpg")

    html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "missav_ABP-123.html"), encoding="utf-8").read() \
        if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "missav_ABP-123.html")) else ""
    if html:
        p = parse_scene(html, "https://missav.live/dm13/en/ABP-123")
        allok &= _t("parse code", p.get("code"), "ABP-123")
        allok &= _t("parse date", p.get("date"), "2014-04-01")
        allok &= _t("parse studio", (p.get("studio") or {}).get("name"), "Prestige")
        allok &= _t("parse has tags", len(p.get("tags") or []) > 0, True)
        allok &= _t("parse url canonicalized",
                    p.get("urls"), ["https://missav123.com/en/ABP-123"])
        allok &= _t("parse keeps source url in details",
                    "Source: https://missav.live/dm13/en/ABP-123" in (p.get("details") or ""), True)
        # ★ 锁住标题来源：主标题必须是**原题**，英文机翻只进 details
        allok &= _t("parse title is original JP",
                    (p.get("title") or "") == "酒井ももか、満足度満点新人ソープ DX", True)
        allok &= _t("parse keeps EN in details",
                    "EN: Momoka Sakai, newcomer soap DX" in (p.get("details") or ""), True)
        allok &= _t("parse title has no raw entity",
                    "&#039;" not in (p.get("title") or ""), True)

    # ★ 中文站回归（曾整页解析失败）：/cn/ 标签是中文的，且标签后带换行+缩进。
    #   missav_kv139_cn.html 是 2026-10 实抓的 KV-139 页面。
    cn_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "missav_kv139_cn.html")
    if os.path.exists(cn_path):
        c = parse_scene(open(cn_path, encoding="utf-8").read(),
                        "https://missav.live/dm13/cn/kv-139")
        allok &= _t("cn parse code", c.get("code"), "KV-139")
        allok &= _t("cn parse date", c.get("date"), "2014-05-16")
        allok &= _t("cn parse studio", (c.get("studio") or {}).get("name"), "映天")
        allok &= _t("cn parse performer", [x["name"] for x in (c.get("performers") or [])],
                    ["みづなれい"])
        # ★ 锁住「段落吞掉页脚」这个 bug：曾一次产出 51 个标签，含 KV-139.mp4 / 返回最顶
        allok &= _t("cn tags bounded", len(c.get("tags") or []), 6)
        allok &= _t("cn no footer junk in tags",
                    any("返回最顶" in (t.get("name") or "") for t in (c.get("tags") or [])), False)
    print("\nSELFTEST", "ALL PASS" if allok else "HAS FAILURE")
    sys.exit(0 if allok else 1)

elif __name__ == "__main__":
    sys.exit(main())
