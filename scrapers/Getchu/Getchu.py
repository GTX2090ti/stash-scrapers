# -*- coding: utf-8 -*-
"""
Getchu.com 刮削器（www.getchu.com 实体商品页 —— DVD / Blu-ray / CD / 游戏 / 同人等）

针对 Stash 的 Python 脚本刮削器接口。能力：
  - sceneByURL        支持的 URL：
                        https://www.getchu.com/item/<id>/
                        https://www.getchu.com/soft.phtml?id=<id>
                        https://ssl.getchu.com/soft.phtml?id=<id>  （同构）
  - sceneByFragment   从条目 URL / 文件名 / 标题里的 getchu 数字 id 直接构造 URL（免搜索）

★ 不提供 sceneByName
  getchu 的站内搜索（/php/search.phtml、/php/nsearch.phtml）对非浏览器请求直接 403 /
  返回空结果（WAF 拦截），实测无法稳定使用。因此本刮削器只走 URL 入口；
  在 Stash 里请用「Search by URL」或已存 URL 的条目刮削，不要指望按名称匹配。

实测要点（2026-10）：
  1. 年龄认证：getchu 全站有 R18 年龄门。放行方式是先访问一次
     https://www.getchu.com/pc/?gc=gc ，服务器下发 cookie
     `getchu_adalt_flag=getchu.com`（值就是字面量 "getchu.com"）。
     ★ 手动往请求里塞 `Cookie: getchu_adalt_flag=1` 无效，会被 302 回
       /php/attestation.html；必须真的请求一次 gate 页拿 Set-Cookie。
     认证页本体：/php/attestation.html?aurl=<原始URL>
  2. 编码：响应声明 EUC-JP，但页面含 JIS X 0213 字符（㌢ ① ⊿ 等），
     必须 strict-first 解码链，**永不用 errors="replace"**（否则相邻字符被带坏）。
  3. 封面：og:image 恒为 /brandnew/<id>/c<id>package.jpg
     （缩略图为 rc<id>package.jpg，不要抓那个）。
  4. 站内搜索无效 → 靠 URL / fragment 里的数字 id 定位。
  5. 商品页**没有样本图/预览图**（实测 8 个页面：只有 package.jpg 与
     package_100.jpg 之类周边推荐图），故不产出 gallery。
"""

import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://www.getchu.com"
GATE_URL = BASE + "/pc/?gc=gc"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

DECODE_FALLBACKS = ("euc_jisx0213", "euc_jis_2004", "cp932", "utf-8")

# ---------------------------------------------------------------- 表字段标签
LABELS = {
    "商品名": "title",
    "サークル": "circle",
    "ブランド": "brand",
    "メーカー": "maker",
    "発売日": "date",
    "媒体": "media",
    "メディア": "media",
    "ジャンル": "genres",
    "サブジャンル": "sub_genres",
    "品番": "part_number",
    "JANコード": "jan",
    "年齢制限": "age_rating",
    "販売元": "distributor",
    "発売元": "distributor",
    "製作": "producer",
    "シリーズ": "series",
    "タイトル": "subtitle",
    "収録時間": "duration",
    "総収録時間": "duration",
    "再生時間": "duration",
    "原作": "source",
    "監督": "director",
    "_DESCRIPTION": "description",
}

# <td align="right">ラベル：</td><td align="top">値</td>
ROW_RE = re.compile(
    r"<t[dh][^>]*>\s*(?:<[^>]+>\s*)*([^<>]{1,24}?)\s*[:：]\s*(?:</[^>]+>\s*)*</t[dh]>\s*"
    r"<t[dh][^>]*>(.*?)</t[dh]>\s*(?=<t[dh]|</tr>)",
    re.S | re.I,
)
A_RE = re.compile(r"<a\b[^>]*href=[\"']([^\"']*)[\"'][^>]*>(.*?)</a>", re.S | re.I)

# 「（このブランドの作品一覧）」这类纯导航链接的文字，出现在值里要剔掉
NAV_SUFFIX_RE = re.compile(r"\s*[（(]\s*この[^）)]*の(?:作品|comed)?一覧\s*[)）]")

ITEM_URL_RE = re.compile(
    r"(?:www\.|ssl\.)?getchu\.com/(?:item/(\d+)|soft\.phtml\?id=(\d+))", re.I)
BARE_ID_RE = re.compile(r"(?<!\d)(\d{6,8})(?!\d)")

_opener = None
_cj = None


# ---------------------------------------------------------------- 网络
def _opener_get():
    """带 cookie 的 opener：第一次调用时过年龄认证门。"""
    global _opener, _cj
    if _opener is None:
        _cj = http.cookiejar.CookieJar()
        _opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(_cj))
        # Stash 容器里可能配了 HTTPS_PROXY；build_opener 默认已读环境变量代理，
        # 但 urllib 的 ProxyHandler 默认会绕过环境变量，所以显式用环境代理。
        proxy = _env_proxy()
        if proxy:
            _opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(_cj),
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            )
        _pass_gate()
    return _opener


def _env_proxy():
    import os
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(k)
        if v:
            return v.strip()
    return None


def _pass_gate():
    """访问 gate 页换年龄认证 cookie。失败不抛 —— 后面请求会自己暴露问题。"""
    for url in (GATE_URL, BASE + "/?gc=gc"):
        try:
            _opener.open(urllib.request.Request(
                url, headers={"User-Agent": UA}), timeout=30).read()
            if any(c.name == "getchu_adalt_flag" for c in _cj):
                return True
        except Exception:
            continue
    return False


def decode_page(raw, declared=""):
    """strict-first 解码链：每个字节都合法才算成功，绝不 errors='replace'。"""
    order = ([declared] if declared else []) + [
        e for e in DECODE_FALLBACKS if e != declared]
    for enc in order:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def fetch(url, tries=3):
    op = _opener_get()
    last = None
    for _ in range(tries):
        try:
            r = op.open(urllib.request.Request(
                url, headers={"User-Agent": UA, "Referer": BASE + "/"}), timeout=30)
            raw = r.read()
            m = re.search(r"charset=([\w-]+)", r.headers.get("Content-Type", ""), re.I)
            return r.status, r.url, decode_page(raw, m.group(1) if m else "")
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


# ---------------------------------------------------------------- 解析
def _strip_tags(seg):
    seg = re.sub(r"<(script|style)\b.*?</\1>", " ", seg, flags=re.S | re.I)
    # 「[一覧]」这类辅助跳转整体删掉（含文字）
    seg = re.sub(r"<a\b[^>]*>[\s\[【]*(?:一覧|View|\[\s*一覧\s*\])[\s\]】]*</a>",
                 " ", seg, flags=re.S | re.I)
    seg = re.sub(r"<a\b[^>]*>.*?</a>",
                 lambda m: re.sub(r"<[^>]+>", " ", m.group(0)), seg, flags=re.S | re.I)
    seg = re.sub(r"<[^>]+>", " ", seg)
    seg = seg.replace("&nbsp;", " ")
    seg = re.sub(r"&amp;", "&", seg)
    seg = re.sub(r"[ \t\r\f\v]+", " ", seg)
    return seg.strip(" \t\r\n　、，,")


def _clean_value(seg):
    v = _strip_tags(seg)
    v = NAV_SUFFIX_RE.sub("", v)
    # 分隔符统一：全角斜杠、读点、顿号都当分隔
    return re.sub(r"\s*[/、,]\s*", " / ", v).strip(" /")


def parse_specs(html):
    """把规格表解析成 {key: value}；列表型字段值是 list。"""
    out = {}
    for m in ROW_RE.finditer(html):
        label = re.sub(r"<[^>]+>", "", m.group(1)).strip().strip("：:").strip()
        key = LABELS.get(label)
        if not key:
            continue
        val = _clean_value(m.group(2))
        if not val:
            continue
        if key in ("media", "genres", "sub_genres", "age_rating"):
            parts = [p.strip() for p in re.split(r"/", val) if p.strip()]
            bucket = out.setdefault(key, [])
            for p in parts:
                if p not in bucket:
                    bucket.append(p)
        else:
            out.setdefault(key, val)
    return out


# 标题尾部与作品名无关的固定尾巴（站点模板文案，非厂商名，不能当 studio）
TITLE_TAIL_NOISE = re.compile(
    r"\s*[（(]\s*(?:更多資訊|更多信息|更多資訊|詳細|更多情报)\s*[)）]\s*$")


def parse_title(html):
    """标题取 <h2>，末尾 (…) 是厂商/品牌，但也可能只是「関連商品」尾巴。"""
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S | re.I)
    m = re.search(r"<h2[^>]*>(.*?)</h2>", body, re.S | re.I)
    if not m:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S | re.I)
    if not m:
        return None, None
    seg = re.sub(r"<nobr>.*?</nobr>", " ", m.group(1), flags=re.S | re.I)
    full = _strip_tags(seg)
    full = TITLE_TAIL_NOISE.sub("", full)
    full = re.sub(r"\s+", " ", full).strip(" \t　|")

    studio = None
    ms = re.search(r"[（(]\s*([^（）()]{1,60}?)\s*[)）]\s*$", full)
    if ms:
        cand = ms.group(1).strip()
        if cand and not cand.startswith("この"):
            studio = cand
            full = full[: ms.start()].strip(" \t　-–—|")
    return full or None, studio


def parse_cover(html):
    m = re.search(r"<meta property=[\"']og:image[\"'] content=[\"']([^\"']+)[\"']",
                  html, re.I)
    if not m:
        return None
    u = m.group(1).strip()
    if not u:
        return None
    return u if u.startswith("http") else BASE + u


def parse_description(html):
    """取「商品紹介」区块正文。"""
    m = re.search(r"(?:商品紹介|作品紹介|ストーリー|详细介绍)", html)
    if not m:
        return None
    # 从标题往后取一段，到下一个 <h3 为止，避免把全页脚注都吞进来
    seg = html[m.end(): m.end() + 6000]
    nxt = seg.find("<h3")
    if nxt > 0:
        seg = seg[:nxt]
    t = re.sub(r"<(script|style)\b.*?</\1>", " ", seg, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</p>", "\n", t, flags=re.I)
    t = _strip_tags(t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t[:3000] or None


def parse_date(raw):
    """2027/01/27 -> 2027-01-27"""
    if not raw:
        return None
    m = re.search(r"(\d{4})\s*[/年.]\s*(\d{1,2})\s*[/月.]\s*(\d{1,2})", raw)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return "%04d-%02d-%02d" % (y, mo, d)


# ---------------------------------------------------------------- URL / id
def id_from_url(url):
    m = ITEM_URL_RE.search(url or "")
    if not m:
        return None
    return m.group(1) or m.group(2) or None


def id_from_text(text):
    """从 fragment（URL / 标题 / 文件名 / path）里挖 getchu 数字 id。"""
    if not text:
        return None
    i = id_from_url(text)
    if i:
        return i
    # 路径里的文件名常常就是 id：1323268.mp4 / getchu-1323268
    best = None
    for m in BARE_ID_RE.finditer(text):
        cand = m.group(1)
        if best is None or len(cand) > len(best):
            best = cand
    return best


def canonical_url(item_id):
    return "%s/item/%s/" % (BASE, item_id)


# ---------------------------------------------------------------- 主解析
def scrape_item(html, item_id, url):
    title, title_tail = parse_title(html)
    specs = parse_specs(html)

    studio = (specs.get("circle") or specs.get("brand")
              or specs.get("maker") or title_tail)

    tags = []
    for k in ("genres", "sub_genres", "media", "series", "age_rating"):
        v = specs.get(k)
        if isinstance(v, list):
            tags += [x for x in v if x]
        elif v:
            tags.append(v)
    if specs.get("source"):
        tags.append(specs["source"])
    if specs.get("director"):
        tags.append(specs["director"])
    if specs.get("producer"):
        tags.append(specs["producer"])
    # 去重保序
    seen = set()
    tags = [t for t in tags if not (t in seen or seen.add(t))]

    scene = {
        "title": title,
        "code": "GETCHU-%s" % item_id,
        "date": parse_date(specs.get("date")),
        # ★ studio / tags 都必须是「对象数组」，不能是裸字符串。
        #   Stash 0.31.1 的 ScrapedScene 里这两个字段是结构体切片：
        #     studio -> models.ScrapedStudio  {name, url}
        #     tags   -> []models.ScrapedTag    {name}
        #   给字符串会分别报：
        #     cannot unmarshal string into Go struct field ScrapedScene.studio of type models.ScrapedStudio
        #     cannot unmarshal string into Go struct field ScrapedScene.tags   of type models.ScrapedTag
        #   180 上 GetchuDL / EHGetchu / FantiaProducts / avbase / missav 全部都是对象形式。
        #   ⚠️ 脚本能独立跑通 ≠ Stash 能解析。类型契约只有 Stash 报错才暴露，必须真机验证。
        "studio": {"name": studio} if studio else None,
        "tags": [{"name": t} for t in tags],
        "details": parse_description(html),
        "urls": [canonical_url(item_id), url] if url and url != canonical_url(item_id) else [canonical_url(item_id)],
        "image": parse_cover(html),
    }
    if specs.get("part_number"):
        scene["details"] = ((scene["details"] or "") +
                            "\n品番: %s" % specs["part_number"]).strip()
    if specs.get("jan"):
        scene["details"] = (scene["details"] + "\nJAN: %s" % specs["jan"]).strip()
    if specs.get("duration"):
        scene["details"] = (scene["details"] + "\n収録時間: %s" %
                            specs["duration"]).strip()
    return {k: v for k, v in scene.items() if v}


def _validate(html, item_id):
    """确认拿到的是商品页而不是年龄认证页 / 404 / 站点首页。

    ★ dl.getchu.com/index.php?action=item&id=N 就会 200 返回站点首页 ——
      HTTP 200 不代表有效页面，解析器会安静返回垃圾数据，比 404 更难发现。
    """
    if "attestation" in html[:4000] or "年齢認証ページ" in html[:6000]:
        raise ValueError("hit getchu age gate (age verification) for id=%s" % item_id)
    if "発売日" not in html and "商品名" not in html:
        raise ValueError("page is not a getchu item page (id=%s)" % item_id)
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    title = (m.group(1).strip() if m else "")
    if "見つかりません" in title or "404" in title:
        raise ValueError("getchu 404 page (id=%s)" % item_id)


def do_item(item_id, src_url=None):
    url = canonical_url(item_id)
    try:
        _, _, html = fetch(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError("getchu item not found (404): %s" % url) from e
        raise
    _validate(html, item_id)
    return scrape_item(html, item_id, src_url or url)


# ---------------------------------------------------------------- Stash 入口
def _read_payload():
    """Stash 把 JSON 写到 stdin；argv[1] 只是模式名（url 不在 argv 里）。"""
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw and raw.strip() else {}
    except Exception:
        return {}


def scene_by_url(url):
    item_id = id_from_url(url)
    if not item_id:
        raise ValueError("not a getchu item url: %r" % (url,))
    return do_item(item_id, url)


def scene_by_fragment(payload):
    """Stash 会把 scene 所有字段 serialize 进来，id/url/title/path 都有可能带 id。"""
    for key in ("url", "urls"):
        v = payload.get(key)
        if isinstance(v, list):
            v = v[0] if v else None
        if isinstance(v, str):
            i = id_from_text(v)
            if i:
                return do_item(i, v if v.startswith("http") else None)
    for key in ("title", "name", "details", "code", "path", "id"):
        v = payload.get(key)
        if isinstance(v, str):
            i = id_from_text(v)
            if i:
                return do_item(i)
    raise ValueError("no getchu id found in fragment: %r" %
                     {k: payload.get(k) for k in ("id", "title", "code", "urls")})


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    payload = _read_payload()

    if mode in ("sceneByURL", "sceneByUrl", "scene"):
        out = scene_by_url(payload.get("url") or payload.get("urls", [""])[0])
    elif mode in ("sceneByFragment", "fragment"):
        out = scene_by_fragment(payload)
    elif mode in ("sceneByName", "name", "query", "sceneByQueryFragment"):
        # 站内搜索被 WAF 挡死，明确不支持，避免安静返回错误数据
        raise ValueError(
            "getchu scraper does not support name search: "
            "site search is WAF-blocked for non-browser requests; "
            "use a getchu item URL instead")
    else:
        raise ValueError("unsupported mode: %r" % mode)

    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
