# stash-scrapers

[English](README.md) | 简体中文

由 [@GTX2090ti](https://github.com/GTX2090ti) 维护的 Stash 自用削刮器合集。

| 削刮器 | 站点 | 能力 |
|---|---|---|
| **avbase** | [avbase.net](https://www.avbase.net) | Scene：名称 / URL / Fragment / 查询 Fragment；Performer：名称 / URL。聚合 fanza / getchu / gyutto 多来源，**getchu 优先、gyutto 回退**（字段级回退链）。 |
| **GetchuDL** | [dl.getchu.com](https://dl.getchu.com) | Scene & Gallery：名称 / URL / Fragment / 查询 Fragment。dl.getchu.com 全文搜索（自动处理 EUC-JP 编码）。 |
| **Getchu** | [www.getchu.com](https://www.getchu.com) | Scene：URL / Fragment。**实体商品**（DVD / Blu-ray / CD / 游戏 / 同人等）—— 与覆盖纯数字版 dl.getchu.com 的 GetchuDL 互补。**不支持按名称搜索**（站内搜索对非浏览器请求直接 403）。处理 R18 年龄门与 EUC-JP / JIS X 0213 解码。纯标准库。 |
| **Fantia** | [fantia.jp](https://fantia.jp) | Scene & Gallery：URL / Fragment，处理**投稿**（`/posts/<id>`）。Fragment 增强版 —— 会出现在 *Scrape with…* 菜单并支持批量。自 **v1.1.0** 起只认投稿命名空间，因此对商品 URL 做 *Search by URL* 时会自动路由到 **FantiaProducts**，不再撞车。会员限定投稿需可选地配置 CookieCloud 登录。 |
| **FantiaProducts** | [fantia.jp](https://fantia.jp) | Scene & Gallery：URL / Fragment / 查询 Fragment，**只处理商店商品命名空间**（`/products/<id>`）。products 专用版，字段提取更深（schema.org `Product`、未模糊的 `image[]`、`content_viewed` 的 `tag[]` / `fanclub_category`、商品编号识别），code 为 `FANTIA-P<id>`，与投稿 id 永不冲突。自 **v1.1.0** 起还刮取发售日期（取商品封面图的 `Last-Modified`，因为 fantia 本身不暴露日期字段）。会员限定商品需可选地配置 CookieCloud 登录。 |
| **MissAV** | [missav.live](https://missav.live) / [missav123.com](https://missav123.com) | Scene：名称 / URL / Fragment / 查询 Fragment；Performer：名称 / URL。社区版 `MissAV_en` / `MissAV_jp` 的 Fragment 增强重写。**同时解析 `en` 与 `zh-CN` 两种语言站点**，跨镜像轮询，存库 scene URL 统一归一，并从页面 `Actress:` / `女优:` 行补齐演员（社区版的 `og:video:actor` XPath 已匹配不到任何东西）。纯标准库。 |

## 通过 Stash 安装（推荐）

1. 打开 Stash → **Settings** → **Metadata Providers**
2. 在 **Available Scrapers** 下点击 **Add Source**
3. 粘贴这个 URL：

   ```
   https://raw.githubusercontent.com/GTX2090ti/stash-scrapers/main/index.yml
   ```

4. 点进该来源，对 `avbase` / `GetchuDL` / `Getchu` / `Fantia` / `FantiaProducts` / `MissAV` 点 **Install**

削刮器会安装到 `scrapers/<id>/` 子目录，之后可直接在 UI 里升级。

> 注意：如果你之前是手动把削刮器文件拷到 `scrapers/` 根目录的，装完包后请删除那些散装文件，否则 Stash 会出现重复的削刮器条目。

## 手动安装

把削刮器文件下载到 Stash 的 `scrapers/` 目录：

- [`scrapers/avbase/avbase.yml`](scrapers/avbase/avbase.yml) + [`avbase.py`](scrapers/avbase/avbase.py)
- [`scrapers/GetchuDL/GetchuDL.yml`](scrapers/GetchuDL/GetchuDL.yml) + [`GetchuDL.py`](scrapers/GetchuDL/GetchuDL.py)
- [`scrapers/Getchu/Getchu.yml`](scrapers/Getchu/Getchu.yml) + [`Getchu.py`](scrapers/Getchu/Getchu.py)
- [`scrapers/Fantia/Fantia.yml`](scrapers/Fantia/Fantia.yml) + [`fantia.py`](scrapers/Fantia/fantia.py)
- [`scrapers/FantiaProducts/FantiaProducts.yml`](scrapers/FantiaProducts/FantiaProducts.yml) + [`FantiaProducts.py`](scrapers/FantiaProducts/FantiaProducts.py)
- [`scrapers/MissAV/MissAV.yml`](scrapers/MissAV/MissAV.yml) + [`missav.py`](scrapers/MissAV/missav.py)

然后在 Settings 里点 **Reload scrapers**。

## Getchu 代理（大陆用户）

dl.getchu.com 与 www.getchu.com 在大陆网络均无法直连。

`GetchuDL.py` 按以下顺序解析代理：

1. Stash 容器上的 `GETCHU_PROXY` 环境变量
2. 脚本同目录下的 `GetchuDL.proxy` 文件（一行，如 `http://192.168.2.210:7890`）
3. 标准的 `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` 环境变量
4. 直连

`Getchu.py` 只读取标准的 `HTTPS_PROXY` / `HTTP_PROXY` 环境变量。

建议用 (1)/(2) 而不是给容器设全局 `HTTPS_PROXY`：全局代理会把 Stash 自身流量（StashDB 等）也走代理，可能搞坏其他削刮器。

## Fantia 登录（可选）

Fantia 会隐藏会员限定投稿 —— `/api/v1/posts/<id>` 对当前 session 不可见的内容一律返回 HTTP 422（已删除、会员限定、未登录，Fantia 不作区分）。`fantia.py` 按以下优先级获取 session cookie：`FANTIA_COOKIE` 环境变量 → 脚本同目录的 `fantia_cookie.txt`（原始串或 Netscape `cookies.txt` 格式）→ **CookieCloud**（自建，实时拉取，缓存 1 小时）。在 Stash 进程所在环境设置 `CC_COOKIECLOUD_URL` / `CC_COOKIECLOUD_KEY` / `CC_COOKIECLOUD_PASSWORD` 即可启用 CookieCloud 通路。完全不配 cookie 时只能刮到真正公开的投稿。

商店商品（`fantia.jp/products/<id>`）的 code 为 `FANTIA-P<id>`，以免与投稿 id 混淆。fantia.jp 走标准 `HTTPS_PROXY` / `HTTP_PROXY` 环境变量代理；CookieCloud 请求始终绕过代理。

`FantiaProducts.py` 用同样的 cookie 来源（`FANTIA_COOKIE` 环境变量 → CookieCloud 的 `CC_COOKIECLOUD_*`），进程内 + 磁盘缓存 `CC_TTL` 秒（默认 3600）。在 products 这一侧这件事比听起来重要：fantia.jp 对**匿名**请求会员限定商品时返回的是**登录页**（HTTP 200，但没有 schema.org `Product` 块），此前表现为每个会员商品都"削刮失败"且毫无提示。代理解析顺序：`FANTIAPROD_PROXY` → `FantiaProducts.proxy` 文件 → 标准代理环境变量 → 直连。

**`Fantia` 与 `FantiaProducts` 怎么分工：** 两者分别负责 fantia.jp 用同一个数字 id 挂着的两个命名空间，自 **v1.1.0** 起不再重叠：

| URL | 路由到 | 原因 |
|---|---|---|
| `fantia.jp/posts/<id>` | **Fantia** | 走 JSON API（`/api/v1/posts/<id>`），容错畸形投稿 JSON |
| `fantia.jp/products/<id>` | **FantiaProducts** | HTML 抓取，字段提取更深（schema.org `Product`、未模糊的 `image[]`、`content_viewed` 的 `tag[]` / `fanclub_category`、商品编号识别、发售日期） |

**商品的发售日期（v1.1.0 起）：** fantia.jp 的商品页**根本不暴露**发售日期字段 —— 没有 `datePublished`、没有 `og:updated_time`、没有带日期的 `itemprop`，`/api/products/<id>` 也是 404。整页唯一的 ISO 日期是**当前登录账号**的 `membership_registration_date`，那是用户自己的注册日，绝不会被采用。因此 `FantiaProducts` 改从商品自己的封面图取日期：`c.fantia.jp/uploads/product/image/<id>/...` 的 HTTP `Last-Modified` 就是这张图的上传时刻，也就是商品上架的时刻，且与商品 id 严格单调（500000 → 2024-01-30、700000 → 2024-09-12、800000 → 2025-03-21）。这个值顺带从"为了取封面本来就要做的那次 GET"里读出，不额外发请求。样片视频的 `VideoObject.uploadDate` 只作兜底 —— 它是**样片**的日期而非商品的日期，样片重传就会漂移（products/800000 报 2026-10-13，而它的封面是 2025-03-21）。投稿侧不受影响：`Fantia` 本来就从投稿 JSON 的 `posted_at` 取日期。

拆分之前两个 yml 都声明了 `fantia.jp/products/`，商品 URL 会匹配到 Stash 恰好挑中的那个 —— 这也是会员商品有时只填了一半字段的原因。`Fantia` 仍可通过 fragment 入口（或手动 *Scrape with… Fantia*）接受商品 URL，因为 `fantia.py` 本身就能读 `/products/`；只是不再参与 URL 路由的竞争。

## MissAV 的镜像与 403 陷阱

MissAV 有多个镜像，行为**并不一致**，所以 yml 把两个都声明上、脚本内部再轮询：

- **脚本自己抓取**时优先 `missav.live`（约 1 秒），`missav123.com` 兜底。
- `missav.live` 对**非 Python 客户端**一律回 **HTTP 403**（TLS/JA3 指纹风控，把 `scraperUserAgent` 改成浏览器 UA 也没用）。这是真的，但**只影响"由 Stash 自己去抓页面"的刮削器**，也就是 `action: scrapeXPath`。本刮削器不受影响 —— 它所有请求都由 Python 发出，能过这个风控。

这里有个很容易踩的误判：以为 `sceneByURL` 会让 Stash 去抓被匹配到的页面。**并不会。** 对 `action: script` 刮削器，Stash 只按 URL 前缀**挑**刮削器，然后把 URL 交给脚本。2026-10-08 在 Stash v0.31.1 实测：

| yml 里的 `urls` | 被刮的 URL | 结果 |
|---|---|---|
| `[missav123.com]` | `missav.live/...` | `Internal system error: index out of range [0] with length 0` —— 在**挑刮削器**阶段就失败，根本没到抓取 |
| `[missav.live, missav123.com]` | `missav.live/...` | 正常返回 |

所以两个镜像都声明，库里无论存的是哪种 URL 都能命中 *Search by URL*。存库 URL 归一到 `missav123.com` 只是为了让同一部作品经由两个镜像访问时去重成一条 URL。

若某个镜像对特定出口 IP 被墙或 403，可用 `MISSAV_MIRRORS=missav.live,missav123.com` 覆盖内置列表。

演员取自页面的 `Actress:`（en）/ `女优:`（zh-CN）行，以及存在时的 `og:video:actor`。`missav.live` 的 `en` 页两者都没有 —— 此时如实留空，而不是从标题猜人名。

代理：`MISSAV_PROXY`（仅本削刮器生效），否则用标准 `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`，再否则直连。

## 环境要求

- Stash v0.26+（python 削刮器跑在 stash docker 镜像内，镜像自带 `python3` 及 `requests` / `lxml`）；`Fantia` 面向 v0.28+
- `avbase.py`：无需额外配置
- `GetchuDL.py`：如在墙内，按上文说明配置代理
- `Getchu.py`：纯标准库，无需额外配置；如在墙内设置 `HTTPS_PROXY` / `HTTP_PROXY`。仅支持 URL / Fragment 入口，**没有名称搜索**
- `fantia.py`：除 `requests` 外无需额外依赖；如需会员内容按上文配置 CookieCloud。其余均为标准库（CookieCloud 客户端内置纯 Python AES 兜底，不需要额外的加密库）
- `FantiaProducts.py`：纯标准库，无需额外配置。CookieCloud 登录与 `fantia.py` 相同；如在墙内设置 `FANTIAPROD_PROXY`（或标准代理环境变量）
- `missav.py`：纯标准库，无需额外配置；如在墙内设置 `MISSAV_PROXY`（或标准代理环境变量）

## 版本管理

[`index.yml`](index.yml) 中每个包带 `version` / `date` / `sha256`。更新 `zips/` 下的 zip 时记得同步更新，Stash 才能识别升级。
