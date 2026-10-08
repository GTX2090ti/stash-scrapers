# stash-scrapers

English | [简体中文](README.zh-CN.md)

Personal Stash scrapers maintained by [@GTX2090ti](https://github.com/GTX2090ti).

| Scraper | Site | Capabilities |
|---|---|---|
| **avbase** | [avbase.net](https://www.avbase.net) | Scene: name / URL / fragment / query-fragment; Performer: name / URL. Aggregates fanza / getchu / gyutto sources with **getchu-first, gyutto-fallback** priority (field-level fallback chain). |
| **GetchuDL** | [dl.getchu.com](https://dl.getchu.com) | Scene & Gallery: name / URL / fragment / query-fragment. Full-text search on dl.getchu.com (EUC-JP encoding handled). |
| **Getchu** | [www.getchu.com](https://www.getchu.com) | Scene: URL / fragment. **Physical goods** (DVD / Blu-ray / CD / games / doujin) — the counterpart to GetchuDL, which covers the digital-only dl.getchu.com. No name search (getchu's search endpoint 403s non-browser requests). Handles the R18 age gate and EUC-JP / JIS X 0213 decoding. Pure stdlib. |
| **Fantia** | [fantia.jp](https://fantia.jp) | Scene & Gallery: URL / fragment, covering both posts (`/posts/<id>`) and shop products (`/products/<id>`). Fragment-capable build — shows up in *Scrape with…* and supports batch. Optional CookieCloud login for members-only posts. |
| **MissAV** | [missav.live](https://missav.live) / [missav123.com](https://missav123.com) | Scene: name / URL / fragment / query-fragment; Performer: name / URL. Fragment-capable rewrite of the community `MissAV_en` / `MissAV_jp`. Parses **both** the `en` and `zh-CN` locales, cycles mirrors, normalises stored scene URLs, and fills in performers from the page's `Actress:` / `女优:` row (the community build's `og:video:actor` XPath no longer matches anything). Pure stdlib. |

## Install via Stash (recommended)

1. Open Stash -> **Settings** -> **Metadata Providers**
2. Under **Available Scrapers**, click **Add Source**
3. Paste this URL:

   ```
   https://raw.githubusercontent.com/GTX2090ti/stash-scrapers/main/index.yml
   ```

4. Click the source, then **Install** next to `avbase` / `GetchuDL` / `Getchu` / `Fantia` / `MissAV`

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
- `missav.py`: pure stdlib, no extra setup. Set `MISSAV_PROXY` (or the standard
  proxy env vars) if you are behind the GFW

## Versioning

Each package in [`index.yml`](index.yml) carries `version` / `date` / `sha256`.
Bump them when the zips under `zips/` change so Stash detects updates.
