# stash-scrapers

English | [简体中文](README.zh-CN.md)

Personal Stash scrapers maintained by [@GTX2090ti](https://github.com/GTX2090ti).

| Scraper | Site | Capabilities |
|---|---|---|
| **avbase** | [avbase.net](https://www.avbase.net) | Scene: name / URL / fragment / query-fragment; Performer: name / URL. Aggregates fanza / getchu / gyutto sources with **getchu-first, gyutto-fallback** priority (field-level fallback chain). |
| **GetchuDL** | [dl.getchu.com](https://dl.getchu.com) | Scene & Gallery: name / URL / fragment / query-fragment. Full-text search on dl.getchu.com (EUC-JP encoding handled). |
| **Getchu** | [www.getchu.com](https://www.getchu.com) | Scene: URL / fragment. **Physical goods** (DVD / Blu-ray / CD / games / doujin) — the counterpart to GetchuDL, which covers the digital-only dl.getchu.com. No name search (getchu's search endpoint 403s non-browser requests). Handles the R18 age gate and EUC-JP / JIS X 0213 decoding. Pure stdlib. |
| **Fantia** | [fantia.jp](https://fantia.jp) | Scene & Gallery: URL / fragment for **creator posts** (`/posts/<id>`). Fragment-capable build — shows up in *Scrape with…* and supports batch. Since **v1.1.0** it claims the posts namespace only, so `Search by URL` on a product routes to **FantiaProducts** instead of colliding with it. Optional CookieCloud login for members-only posts. |
| **FantiaProducts** | [fantia.jp](https://fantia.jp) | Scene & Gallery: URL / fragment / query-fragment, **products namespace only** (`/products/<id>`). The products-specialised build: deeper field extraction (schema.org `Product`, unblurred `image[]`, `content_viewed` `tag[]` / `fanclub_category`, catalogue-code detection) and code `FANTIA-P<id>` that never collides with post ids. Studio is the circle name from `Product.brand`. Since **v1.2.0** the release date is derived from the product id via a calibrated anchor table (fantia exposes no date field; median error 0d, p90 1d for id ≥ 1000000). Since **v1.2.1** the id-derived date is cross-checked against the page's sample-video date: a disagreement of more than 30 days resolves to the older date. Since **v1.2.2** products below id 700000 (where the id map is not monotone) fall back to the page's sample-video date instead of coming back dateless. Optional CookieCloud login for members-only products. |
| **MissAV** | [missav.live](https://missav.live) / [missav123.com](https://missav123.com) | Scene: name / URL / fragment / query-fragment; Performer: name / URL. Fragment-capable rewrite of the community `MissAV_en` / `MissAV_jp`. Parses **both** the `en` and `zh-CN` locales, cycles mirrors, normalises stored scene URLs, and fills in performers from the page's `Actress:` / `女优:` row (the community build's `og:video:actor` XPath no longer matches anything). Pure stdlib. |

## Install via Stash (recommended)

1. Open Stash -> **Settings** -> **Metadata Providers**
2. Under **Available Scrapers**, click **Add Source**
3. Paste this URL:

   ```
   https://raw.githubusercontent.com/GTX2090ti/stash-scrapers/main/index.yml
   ```

4. Click the source, then **Install** next to `avbase` / `GetchuDL` / `Getchu` / `Fantia` / `FantiaProducts` / `MissAV`

Scrapers install into `scrapers/<id>/` subdirectories and are managed/upgradable from the UI.

> Note: if you previously copied these scrapers manually into the `scrapers/` root,
> remove those loose files after installing from this source, otherwise Stash will
> show duplicate scraper entries.

## Manual install

Download the scraper files and place them in your Stash `scrapers/` directory:

- [`scrapers/avbase/avbase.yml`](scrapers/avbase/avbase.yml) + [`avbase.py`](scrapers/avbase/avbase.py)
- [`scrapers/GetchuDL/GetchuDL.yml`](scrapers/GetchuDL/GetchuDL.yml) + [`GetchuDL.py`](scrapers/GetchuDL/GetchuDL.py)
- [`scrapers/Getchu/Getchu.yml`](scrapers/Getchu/Getchu.yml) + [`Getchu.py`](scrapers/Getchu/Getchu.py)
- [`scrapers/Fantia/Fantia.yml`](scrapers/Fantia/Fantia.yml) + [`fantia.py`](scrapers/Fantia/fantia.py)
- [`scrapers/FantiaProducts/FantiaProducts.yml`](scrapers/FantiaProducts/FantiaProducts.yml) + [`FantiaProducts.py`](scrapers/FantiaProducts/FantiaProducts.py)
- [`scrapers/MissAV/MissAV.yml`](scrapers/MissAV/MissAV.yml) + [`missav.py`](scrapers/MissAV/missav.py)

Then click **Reload scrapers** in Settings.

## Getchu proxy (CN users)

Both dl.getchu.com and www.getchu.com are unreachable directly from mainland China.

`GetchuDL.py` resolves a proxy in this order:

1. `GETCHU_PROXY` env var on the Stash container
2. a `GetchuDL.proxy` file next to the script (single line, e.g. `http://192.168.2.210:7890`)
3. standard `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` env vars
4. direct connection

`Getchu.py` reads only the standard `HTTPS_PROXY` / `HTTP_PROXY` env vars.

Prefer (1)/(2) over a global `HTTPS_PROXY`: a global proxy also reroutes Stash's own
traffic (StashDB etc.) and can break other scrapers.

## Fantia login (optional)

Fantia hides members-only posts — `/api/v1/posts/<id>` answers HTTP 422 for anything the
session may not see (deleted, members-only, or not logged in; Fantia does not
distinguish). `fantia.py` picks up a session cookie from, in priority order:
`FANTIA_COOKIE` env var → `fantia_cookie.txt` next to the script (raw or Netscape
`cookies.txt`) → **CookieCloud** (self-hosted, fetched live, cached 1 h). Set
`CC_COOKIECLOUD_URL` / `CC_COOKIECLOUD_KEY` / `CC_COOKIECLOUD_PASSWORD` where the Stash
process runs to enable the CookieCloud path. Without any cookie only genuinely public
posts scrape.

Shop products (`fantia.jp/products/<id>`) carry code `FANTIA-P<id>` to stay
distinguishable from post ids. Proxy for fantia.jp uses the standard
`HTTPS_PROXY` / `HTTP_PROXY` env vars; CookieCloud requests always bypass it.

`FantiaProducts.py` reads the same cookie sources (`FANTIA_COOKIE` env var →
CookieCloud via `CC_COOKIECLOUD_*`), cached in-process and on disk for `CC_TTL`
seconds (default 3600). On the products side this matters more than it sounds:
fantia.jp answers an **anonymous** request for a members-only product with the
**login page** (HTTP 200, no schema.org `Product` block), which used to surface
as a silent "scrape failed" for every member product. Proxy resolution for it is
`FANTIAPROD_PROXY` → a `FantiaProducts.proxy` file → the standard env vars →
direct.

**`Fantia` vs `FantiaProducts`:** they cover the two namespaces that fantia.jp
puts behind one numeric id, and since **v1.1.0** the two no longer overlap:

| URL | Routed to | Why |
|---|---|---|
| `fantia.jp/posts/<id>` | **Fantia** | JSON API (`/api/v1/posts/<id>`), tolerant of malformed post JSON |
| `fantia.jp/products/<id>` | **FantiaProducts** | HTML scrape with deeper field extraction (schema.org `Product`, unblurred `image[]`, `content_viewed` `tag[]` / `fanclub_category`, catalogue-code detection, id-derived release date) |

**Release date on products (since v1.1.0, id-derived since v1.2.0, cross-checked since v1.2.1):** fantia.jp
exposes **no** release-date field on a product page — no `datePublished`, no
`og:updated_time`, no date-bearing `itemprop`, no `window.__INITIAL_STATE__`, and
`/api/products/<id>` is a 404 even when logged in. The only ISO date in the
document is the *logged-in account*'s `membership_registration_date`, i.e. the
user's own signup date, which is never used.

What the page does carry is a numeric product id, and Fantia issues those
sequentially — so id → publication date is a monotone (if locally uneven)
mapping, with a measured density of 50…8100 ids/day. `FantiaProducts` therefore
ships a 221-anchor `(id, date)` table and interpolates. The table was calibrated
offline from ~1700 probed products: each anchor's date is
`min(cover-image Last-Modified, VideoObject.uploadDate)`, because both of those
are *upper bounds* on the publish date (an image or video is uploaded at or after
publication, and a later replacement only pushes the date later), so the min is
exact whenever the product has never been re-uploaded. The series is then made
monotone with a suffix-minimum pass, which equals the truth at every clean point
and pulls down only the polluted ones.

Measured leave-one-out accuracy against dual-signal ground truth (cover
`Last-Modified` == sample `uploadDate`):

| id range | median | p90 | max |
|---|---|---|---|
| ≥ 1040000 | 0d | 1d | 1d |
| ≥ 1000000 | 0d | 1d | 33d |
| ≥ 900000 | 0d | 1d | 33d |
| ≥ 700000 | 0d | 2d | 33d |

Below id 700000 the mapping is **not** monotone — long-deleted id ranges are
interleaved with products still on sale whose covers were re-uploaded — so no
id-derived date is attempted there, but those products are **not** left
dateless: the page's own `VideoObject.uploadDate` (the sample video) does not
depend on the id map and is the answer instead. The sample video is the right
fallback precisely because sellers replace cover images (e.g. for a new sale
campaign) but essentially never re-upload the sample — verified live on 649825:
cover `Last-Modified` 2026-10-13 (a sale-banner refresh) vs sample `uploadDate`
2024-06-19. Past the last anchor the date is extrapolated with the trailing
14-day id rate (capped at 60 days) and then also falls back to the page's
`VideoObject.uploadDate`. No image header is read at runtime; the table is
refreshed by re-running the calibration crawl described in the module
docstring.

Inside the table's coverage the page's `VideoObject.uploadDate` doubles as a
cross-check: both dates are upper bounds on the same publish date, and every
failure mode found in calibration (non-monotone id ranges, extrapolation drift,
a re-uploaded sample video) pushes one of them *later*, never earlier. So when
the two disagree by more than 30 days, one signal is structurally wrong and the
**older** date wins; small disagreements keep the id-derived value, which is the
more precise of the two.

Posts are unaffected: `Fantia` already takes the date from the post JSON
(`posted_at`).

Before the split both ymls declared `fantia.jp/products/`, so a product URL
matched whichever scraper Stash happened to pick — which is why member products
sometimes came back half-filled. `Fantia` still accepts a product URL through its
fragment entries (or a manual *Scrape with… Fantia*), since `fantia.py` can read
`/products/` on its own; it just no longer competes for the URL routing.

## MissAV mirrors and the 403 trap

MissAV runs several mirrors and they do **not** all behave the same, so the yml
declares both and the script cycles them:

- **The script** prefers `missav.live` (~1 s) and falls back to `missav123.com`.
- `missav.live` answers **HTTP 403** to non-Python clients (TLS/JA3 fingerprint
  filtering; setting `scraperUserAgent` to a browser UA does not help). That is
  real, but it only matters for scrapers where **Stash fetches the page
  itself** — i.e. `action: scrapeXPath`. It never reaches this scraper, because
  every request here is made by Python, which clears the filter.

A tempting but wrong assumption is that `sceneByURL` makes Stash fetch the
matched page. It does not: for `action: script` scrapers Stash only selects a
scraper by URL prefix and then runs the script. Measured 2026-10-08 on
Stash v0.31.1:

| yml `urls` | URL scraped | result |
|---|---|---|
| `[missav123.com]` | `missav.live/...` | `Internal system error: index out of range [0] with length 0` — failed while *selecting* a scraper, before any fetch |
| `[missav.live, missav123.com]` | `missav.live/...` | normal result |

Both mirrors are therefore declared, and a library holding either URL form still
matches *Search by URL*. Stored URLs are normalised to `missav123.com` purely so
that one scene reached through two mirrors dedupes to a single URL.

If a mirror is blocked or 403s for your egress IP, override the internal list
with `MISSAV_MIRRORS=missav.live,missav123.com`.

Performers are read from the page's `Actress:` (en) / `女优:` (zh-CN) row, plus
`og:video:actor` when present — on `missav.live`'s `en` pages neither exists, so
the performer field is legitimately left empty there rather than guessed from
the title.

Proxy: `MISSAV_PROXY` (scraper only), else the standard `HTTPS_PROXY` /
`HTTP_PROXY` / `ALL_PROXY`, else direct.

## Requirements

- Stash v0.26+ (python script scrapers run inside the stash docker image, which ships
  `python3` with `requests` / `lxml`); `Fantia` targets v0.28+
- `avbase.py`: no extra setup
- `GetchuDL.py`: set the proxy as described above if you are behind the GFW
- `Getchu.py`: pure stdlib, no extra setup; set `HTTPS_PROXY` / `HTTP_PROXY` if you are
  behind the GFW. URL / fragment entry only — there is no name search
- `fantia.py`: no extra setup beyond `requests`; optional CookieCloud login as
  described above. Everything else is stdlib (the CookieCloud client ships a
  pure-Python AES fallback, no extra crypto packages)
- `FantiaProducts.py`: pure stdlib, no extra setup. Same optional CookieCloud
  login as `fantia.py`; set `FANTIAPROD_PROXY` (or the standard proxy env vars)
  if you are behind the GFW
- `missav.py`: pure stdlib, no extra setup. Set `MISSAV_PROXY` (or the standard
  proxy env vars) if you are behind the GFW

## Versioning

Each package in [`index.yml`](index.yml) carries `version` / `date` / `sha256`.
Bump them when the zips under `zips/` change so Stash detects updates.
