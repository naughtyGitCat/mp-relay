# mp-relay

把"贴磁力链 / 输入媒体名"统一收口到一个 Web UI，自动分发到合适的下载/刮削管道：

```
┌── 输入 ─────────────────────────────────┐
│ magnet:?xt=...   ──┐                    │
│ http://*.torrent ──┼─ 分类器             │
│ 媒体名 / 番号    ──┘                     │
└──────────────────────┬──────────────────┘
                       ▼
       ┌───────────────┴───────────────┐
       │                               │
   普通媒体                         JAV
       │                               │
       ▼                               ▼
   MoviePilot                      qBT (JAV cat)
   /api/v1/download/add            G:\Downloads\JAV-staging
       │                               │
       ▼                               ▼
   下载 → MP 自动                   watcher 等下载完
   刮削 → D:\电影\               → mdcx scrape dir
                                  → E:\Jav (Jellyfin)
                                  → E:\Jav_failed (失败)
```

跑在 Windows 媒体服务器（与 MoviePilot / qBittorrent / mdcx 同机），单页 Web UI 监听 `:5000`。

## 设计目标

- **单输入框**：贴啥都行，自动识别 magnet / .torrent URL / 番号 / 媒体名
- **不重造轮子**：MoviePilot 已有的事不重做（TMDB 识别 / 站点搜索 / 整理入库）
- **JAV 走专门管道**：MoviePilot 识别不了番号，由 mdcx 接管
- **全自动**：watcher 监 qBT 完成事件 → 自动调 mdcx → 自动归档

## Phase Roadmap

| Phase | 范围 | 状态 |
|---|---|---|
| 0 | 主框架 + 分类器 + MP 接入 + qBT 接入 + watcher + mdcx 调用 | 当前 |
| 1 | 番号搜种（输入 SSIS-001 → 馒头/老师 PT 站搜索 → 列候选 → 用户选） | TODO |
| 2 | 演员/系列发现（输入演员名 → 列出该演员所有番号 → 批量订阅） | TODO |
| 3 | 海报/Fanart 补全（mdcx 失败时回落到 gfriends 等社区库） | TODO |

## Reference projects（同类目设计参考）

写本项目时调研过的相关项目，**不是依赖**，只是借鉴架构 / 数据源 / 元数据策略：

| Repo | 借鉴点 |
|---|---|
| [yuukiy/JavSP](https://github.com/yuukiy/JavSP) | 番号识别正则（覆盖各厂牌格式）；本地批量整理流程；多源 fallback |
| [dirtyracer1337/Jellyfin.Plugin.PhoenixAdult](https://github.com/dirtyracer1337/Jellyfin.Plugin.PhoenixAdult) | Jellyfin 端直接刮削方案；可作为 mdcx 失败时的后备 metadata 源 |
| [guyueyingmu/avbook](https://github.com/guyueyingmu/avbook) | 演员维度的发现 / 索引 UI 思路；按厂牌/类别筛选 |
| [gfriends/gfriends](https://github.com/gfriends/gfriends) | 演员头像数据库（commit-only 仓库），mdcx 找不到头像时可回落 |
| [zyd16888/sehuatang](https://github.com/zyd16888/sehuatang) | 色花堂论坛抓取 / 番号 → 磁力链映射，**Phase 1 番号搜种最实用的数据源** |

每个的笔记单独放在 [`docs/references.md`](docs/references.md)。

## 配置

复制 `.env.example` 到 `.env`，关键字段：

```ini
MP_URL=http://localhost:3000
MP_USER=admin
MP_PASS=change-me

QBT_URL=http://localhost:8080
QBT_USER=admin
QBT_PASS=change-me
QBT_JAV_CATEGORY=JAV
QBT_JAV_SAVEPATH=G:\Downloads\JAV-staging

# mdcx fork that exposes a CLI entry point (`mdcx.cmd.main`):
# https://github.com/sqzw-x/mdcx — base project (GUI-first)
# https://github.com/naughtyGitCat/mdcx — fork that adds the CLI
MDCX_DIR=E:\mdcx-src
MDCX_PYTHON=E:\mdcx-src\.venv\Scripts\python.exe
MDCX_MODULE=mdcx.cmd.main
```

> ⚠ **Personal-use tool.** Designed for my homelab; defaults assume a single-user
> Windows machine on a trusted LAN. Do not expose `:5000` to the internet —
> there is no auth on mp-relay itself, and it can add arbitrary downloads.

## 部署

**推荐**：从 [Releases](https://github.com/naughtyGitCat/mp-relay/releases) 下载最新的
`mp-relay-Setup-<版本>.exe`，双击安装。安装包自带 Python 运行时 + NSSM，向导默认勾选
"Install as Windows service"，安装完打开 `.env` 填密码即可。详见
[`deploy/README.md`](deploy/README.md)。

**开发迭代**：用 [`deploy/install-on-windows.ps1`](deploy/install-on-windows.ps1) 脚本
（scp 源码到主机 + 创建 venv + 注册服务），改完代码 rsync + restart 服务即可，不用每次发版。

构建 `.exe` 安装器的细节：[`build/README.md`](build/README.md)。打 tag 后 GitHub Actions
自动 build + 附到 release。

## 风险提示

- MDCX 调用失败时会留在 `E:\Jav_failed`，需要定期人工处理
- qBT category save_path 改了之后，已有种子的下载位置不会自动迁移
- watcher 用 polling（默认 60s 间隔），对 qBT 友好但延迟最大 60s
