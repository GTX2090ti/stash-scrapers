# stash-scrapers

[English](README.md) | 简体中文

由 [@GTX2090ti](https://github.com/GTX2090ti) 维护的 Stash 自用削刮器合集。

| 削刮器 | 站点 | 能力 |
|---|---|---|
| **avbase** | [avbase.net](https://www.avbase.net) | Scene：名称 / URL / Fragment / 查询 Fragment；Performer：名称 / URL。聚合 fanza / getchu / gyutto 多来源，**getchu 优先、gyutto 回退**（字段级回退链）。 |
| **GetchuDL** | [dl.getchu.com](https://dl.getchu.com) | Scene & Gallery：名称 / URL / Fragment / 查询 Fragment。dl.getchu.com 全文搜索（自动处理 EUC-JP 编码）。 |
| **Getchu** | [www.getchu.com](https://www.getchu.com) | Scene：URL / Fragment。**实体商品**（DVD / Blu-ray / CD / 游戏 / 同人等）—— 与覆盖纯数字版 dl.getchu.com 的 GetchuDL 互补。**不支持按名称搜索**（站内搜索对非浏览器请求直接 403）。处理 R18 年龄门与 EUC-JP / JIS X 0213 解码。纯标准库。 |
| **Fantia** | [fantia.jp](https://fantia.jp) | Scene & Gallery：URL / Fragment，同时覆盖投稿（`/posts/<id>`）与商店商品（`/products/<id>`）。Fragment 增强版 —— 会出现在 *Scrape with…* 菜单并支持批量。会员限定投稿需可选地配置 CookieCloud 登录。 |
| **MissAV** | [missav.live](https://missav.live) / [missav123.com](https://missav123.com) | Scene：名称 / URL / Fragment / 查询 Fragment；Performer：名称 / URL。社区版 `MissAV_en` / `MissAV_jp` 的 Fragment 增强重写。**同时解析 `en` 与 `zh-CN` 两种语言站点**，跨镜像回退，把存库 URL 归一到 Stash 自身 HTTP 客户端唯一能过的域名，并从页面 `Actress:` / `女优:` 行补齐演员（社区版的 `og:video:actor` XPath 已匹配不到任何东西）。纯标准库。 |

## 通过 Stash 安装（推荐）

1. 打开 Stash → **Settings** → **Metadata Providers**
2. 在 **Available Scrapers** 下点击 **Add Source**
3. 粘贴这个 URL：

   ```
   https://raw.githubusercontent.com/GTX2090ti/stash-scrapers/main/index.yml
   ```

4. 点进该来源，对 `avbase` / `GetchuDL` / `Getchu` / `Fantia` / `MissAV` 点 **Install**

削刮器会安装到 `scrapers/<id>/` 子目录，之后可直接在 UI 里升级。

> 注意：如果你之前是手动把削刮器文件拷到 `scrapers/` 根目录的，装完包后请删除那些散装文件，否则 Stash 会出现重复的削刮器条目。

## 手动安装

把削刮器文件下载到 Stash 的 `scrapers/` 目录：

- [`scrapers/avbase/avbase.yml`](scrapers/avbase/avbase.yml) + [`avbase.py`](scrapers/avbase/avbase.py)
- [`scrapers/GetchuDL/GetchuDL.yml`](scrapers/GetchuDL/GetchuDL.yml) + [`GetchuDL.py`](scrapers/GetchuDL/GetchuDL.py)
- [`scrapers/Getchu/Getchu.yml`](scrapers/Getchu/Getchu.yml) + [`Getchu.py`](scrapers/Getchu/Getchu.py)
- [`scrapers/Fantia/Fantia.yml`](scrapers/Fantia/Fantia.yml) + [`fantia.py`](scrapers/Fantia/fantia.py)
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

## MissAV 的镜像与 403 陷阱

MissAV 有多个镜像，行为**并不一致**，所以脚本与 Stash 是**故意**用不同域名的：

- **脚本自己抓取**时优先 `missav.live`（约 1 秒），`missav123.com` 兜底。
- **Stash 自身**只能访问 `missav123.com`。`sceneByURL` 按 URL 前缀匹配后，是由 **Stash 自己的 Go HTTP 客户端**去抓那个 URL 的，**不经过脚本**。`missav.live` / `missav.ai` / `missav.ws` 对这个客户端一律回 **HTTP 403**（TLS/JA3 指纹风控，把 `scraperUserAgent` 改成浏览器 UA 也没用），只有 `missav123.com` 返回 200。

由此带来两个结论：

- 存进库的 scene URL 会归一到 `missav123.com`，日后重刮不会 403。
- 库里**已有**的 `missav.live` URL **不会**命中 *Search by URL*。改用 *Scrape with…* 即可 —— 那条路径走脚本，永远可用。
- 某些镜像对特定出口 IP 会被墙或 403，可用 `MISSAV_MIRRORS=missav.live,missav123.com` 覆盖内置列表。

演员取自页面的 `Actress:`（en）/ `女优:`（zh-CN）行，以及存在时的 `og:video:actor`。`missav.live` 的 `en` 页两者都没有 —— 此时如实留空，而不是从标题猜人名。

代理：`MISSAV_PROXY`（仅本削刮器生效），否则用标准 `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`，再否则直连。

## 环境要求

- Stash v0.26+（python 削刮器跑在 stash docker 镜像内，镜像自带 `python3` 及 `requests` / `lxml`）；`Fantia` 面向 v0.28+
- `avbase.py`：无需额外配置
- `GetchuDL.py`：如在墙内，按上文说明配置代理
- `Getchu.py`：纯标准库，无需额外配置；如在墙内设置 `HTTPS_PROXY` / `HTTP_PROXY`。仅支持 URL / Fragment 入口，**没有名称搜索**
- `fantia.py`：除 `requests` 外无需额外依赖；如需会员内容按上文配置 CookieCloud。其余均为标准库（CookieCloud 客户端内置纯 Python AES 兜底，不需要额外的加密库）
- `missav.py`：纯标准库，无需额外配置；如在墙内设置 `MISSAV_PROXY`（或标准代理环境变量）

## 版本管理

[`index.yml`](index.yml) 中每个包带 `version` / `date` / `sha256`。更新 `zips/` 下的 zip 时记得同步更新，Stash 才能识别升级。
