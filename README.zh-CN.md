# stash-scrapers

[English](README.md) | 简体中文

由 [@GTX2090ti](https://github.com/GTX2090ti) 维护的 Stash 自用削刮器合集。

| 削刮器 | 站点 | 能力 |
|---|---|---|
| **avbase** | [avbase.net](https://www.avbase.net) | Scene：名称 / URL / Fragment / 查询 Fragment；Performer：名称 / URL。聚合 fanza / getchu / gyutto 多来源，**getchu 优先、gyutto 回退**（字段级回退链）。 |
| **GetchuDL** | [dl.getchu.com](https://dl.getchu.com) | Scene & Gallery：名称 / URL / Fragment / 查询 Fragment。dl.getchu.com 全文搜索（自动处理 EUC-JP 编码）。 |

## 通过 Stash 安装（推荐）

1. 打开 Stash → **Settings** → **Metadata Providers**
2. 在 **Available Scrapers** 下点击 **Add Source**
3. 粘贴这个 URL：

   ```
   https://raw.githubusercontent.com/GTX2090ti/stash-scrapers/main/index.yml
   ```

4. 点进该来源，对 `avbase` / `GetchuDL` 点 **Install**

削刮器会安装到 `scrapers/<id>/` 子目录，之后可直接在 UI 里升级。

> 注意：如果你之前是手动把削刮器文件拷到 `scrapers/` 根目录的，装完包后请删除那些散装文件，否则 Stash 会出现重复的削刮器条目。

## 手动安装

把削刮器文件下载到 Stash 的 `scrapers/` 目录：

- [`scrapers/avbase/avbase.yml`](scrapers/avbase/avbase.yml) + [`avbase.py`](scrapers/avbase/avbase.py)
- [`scrapers/GetchuDL/GetchuDL.yml`](scrapers/GetchuDL/GetchuDL.yml) + [`GetchuDL.py`](scrapers/GetchuDL/GetchuDL.py)

然后在 Settings 里点 **Reload scrapers**。

## GetchuDL 代理（大陆用户）

大陆网络无法直连 dl.getchu.com。`GetchuDL.py` 按以下顺序解析代理：

1. Stash 容器上的 `GETCHU_PROXY` 环境变量
2. 脚本同目录下的 `GetchuDL.proxy` 文件（一行，如 `http://192.168.2.210:7890`）
3. 标准的 `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` 环境变量
4. 直连

建议用 (1)/(2) 而不是给容器设全局 `HTTPS_PROXY`：全局代理会把 Stash 自身流量（StashDB 等）也走代理，可能搞坏其他削刮器。

## 环境要求

- Stash v0.26+（python 削刮器跑在 stash docker 镜像内，镜像自带 `python3` 及 `requests` / `lxml`）
- `avbase.py`：无需额外配置
- `GetchuDL.py`：如在墙内，按上文说明配置代理

## 版本管理

[`index.yml`](index.yml) 中每个包带 `version` / `date` / `sha256`。更新 `zips/` 下的 zip 时记得同步更新，Stash 才能识别升级。
