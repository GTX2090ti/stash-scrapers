# stash-scrapers

Personal Stash scrapers maintained by [@GTX2090ti](https://github.com/GTX2090ti).

| Scraper | Site | Capabilities |
|---|---|---|
| **avbase** | [avbase.net](https://www.avbase.net) | Scene: name / URL / fragment / query-fragment; Performer: name / URL. Aggregates fanza / getchu / gyutto sources with **getchu-first, gyutto-fallback** priority (field-level fallback chain). |
| **GetchuDL** | [dl.getchu.com](https://dl.getchu.com) | Scene & Gallery: name / URL / fragment / query-fragment. Full-text search on dl.getchu.com (EUC-JP encoding handled). |

## Install via Stash (recommended)

1. Open Stash -> **Settings** -> **Metadata Providers**
2. Under **Available Scrapers**, click **Add Source**
3. Paste this URL:

   ```
   https://raw.githubusercontent.com/GTX2090ti/stash-scrapers/main/index.yml
   ```

4. Click the source, then **Install** next to `avbase` / `GetchuDL`

Scrapers install into `scrapers/<id>/` subdirectories and are managed/upgradable from the UI.

> Note: if you previously copied these scrapers manually into the `scrapers/` root,
> remove those loose files after installing from this source, otherwise Stash will
> show duplicate scraper entries.

## Manual install

Download the scraper files and place them in your Stash `scrapers/` directory:

- [`scrapers/avbase/avbase.yml`](scrapers/avbase/avbase.yml) + [`avbase.py`](scrapers/avbase/avbase.py)
- [`scrapers/GetchuDL/GetchuDL.yml`](scrapers/GetchuDL/GetchuDL.yml) + [`GetchuDL.py`](scrapers/GetchuDL/GetchuDL.py)

Then click **Reload scrapers** in Settings.

## GetchuDL proxy (CN users)

dl.getchu.com is unreachable directly from mainland China. `GetchuDL.py` resolves a
proxy in this order:

1. `GETCHU_PROXY` env var on the Stash container
2. a `GetchuDL.proxy` file next to the script (single line, e.g. `http://192.168.2.210:7890`)
3. standard `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` env vars
4. direct connection

Prefer (1)/(2) over a global `HTTPS_PROXY`: a global proxy also reroutes Stash's own
traffic (StashDB etc.) and can break other scrapers.

## Requirements

- Stash v0.26+ (python script scrapers run inside the stash docker image, which ships
  `python3` with `requests` / `lxml`)
- `avbase.py`: no extra setup
- `GetchuDL.py`: set the proxy as described above if you are behind the GFW

## Versioning

Each package in [`index.yml`](index.yml) carries `version` / `date` / `sha256`.
Bump them when the zips under `zips/` change so Stash detects updates.
