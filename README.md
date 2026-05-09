# mp-relay

把"贴磁力链 / 输入媒体名 / 输入番号 / 找演员"统一收口到一个 Web UI，
自动分发到合适的下载/刮削管道：

```
┌── 输入 ──────────────────────────────────────────────────────────┐
│ magnet:?xt=...        ──┐                                        │
│ http://*.torrent      ──┤                                        │
│ 媒体名《繁花》/ tmdb ID  ─┼─ 分类器                              │
│ JAV 番号 (SSIS-001)    ──┤                                       │
│ 演员名（/discover）    ──┘                                       │
└─────────────────────────┬────────────────────────────────────────┘
                          ▼
        ┌─────────────────┼─────────────────────┐
        │                 │                     │
    普通媒体           JAV (本地)              JAV (云)
        │                 │                     │
        ▼                 ▼                     ▼
    MoviePilot       qBT (JAV cat)         115 离线
    /api/v1/         G:\Downloads\         offline_add_url
    download/add     JAV-staging                │
        │                │                     ▼
        ▼                ▼                cloud115_watcher
    MP 自动下载       qBT watcher          (60s 轮询)
    + 自动刮削         (60s 轮询)          → 同步到本地
    + 整理入库            │                     │
    → D:\电影\           │                     │
                          ├─────────────────────┘
                          ▼
                    post_download pipeline
                          │
                  ┌───────┼───────┬─────────────┐
                  ▼       ▼       ▼             ▼
              QC 检查   Merge   BDMV→mkv   sanitize 路径
              (大小、    (CD1+    (主播放      (剥 [4K]
              假文件)    CD2)    列重新封)   /@/() 等)
                          │
                          ▼
                    mdcx scrape (semaphore=2)
                          │
                  ┌───────┼───────────┐
                  ▼       ▼           ▼
              成功      no-match   失败
                │         │           │
                ▼         ▼           ▼
              E:\Jav   error 标记   E:\Jav_failed
              + 自动 cover-refill
              (从 JavDB CDN 拉缺失封面)
```

跑在 Windows 媒体服务器（与 MoviePilot / qBittorrent / mdcx 同机），单页 Web UI 监听 `:5000`。

## Web UI 一览

| 路径 | 用途 |
|---|---|
| **`/`** | 单输入框主页 + 最近任务列表（10s 自动刷新） |
| **`/discover`** | 演员发现页 —— 搜演员名 → 列出该演员所有番号（可隐藏已拥有）→ 多选批量"加入 qBT"或"加入 115" |
| **`/setup`** | 配置向导 —— 4 张卡（mdcx / MoviePilot / qBittorrent / Jellyfin），每张 Test connection + Save，热加载不重启 |
| **`/health`** | JSON 健康检查 —— mdcx / Telegram / Bangumi / 115 各服务状态 |
| **`/metrics`** | Prometheus 指标 —— `mp-relay-grafana.json` 仪表盘可直接导入 |
| `/auth/115` | 115 OAuth 设备码授权页（首次配 115 离线时用） |

## 设计目标

- **单输入框**：贴啥都行，自动识别 magnet / .torrent URL / 番号 / 媒体名 / 演员
- **不重造轮子**：MoviePilot 已有的事不重做（TMDB 识别 / 站点搜索 / 整理入库）
- **JAV 走专门管道**：MoviePilot 识别不了番号，由 mdcx 接管
- **全自动**：watcher 监 qBT / 115 完成事件 → post-download 管线 → mdcx → 归档
- **Fail-soft**：每一步都能检测失败 + 标记 + 通过 retry endpoint 重跑

## 主要功能

### Phase 1 — 番号搜种
单输入框输入番号 (`SSIS-001` / `snos-073`) → 多源搜索 → 列候选 → 用户选或 auto-pick：

- **搜种源**：sukebei + JavBus + JavDB + MissAV（并行查，按 hash 去重）
- **Auto-pick 排序**：suspicion_score↑ / 中文字幕↑ / 做种数↑ / 画质↑ / 体积↑
- **下载分发**：选完后通过 `/api/jav-add`（推 qBT）或 `/api/cloud115-add`（推 115）

### Phase 1.5 — 名字 fallback (`媒体名` / 中文名搜)
TMDB 识别不到时回退到 Bangumi（适合动漫 / JAV-with-anime-tie-in） + AniList（动漫别名）。

### Phase 1.8 / 1.9 — 115 离线 + 闭环归档
- `/auth/115` 一次 OAuth 授权 → mp-relay 持久持有 refresh token
- `cloud_offline_115` 任务 → `cloud115_watcher` 60s 轮询 → 完成的拖到本地 staging
- 自动调 post_download 管线 → mdcx → 归档；115 token 自动续

### Phase 2 — 演员/系列发现 (`/discover`)
- 输入演员名 → JavBus 搜 → 列出该演员所有番号
- 批量勾选 → "加入 qBT" 或 "加入 115" 一键搞定
- "隐藏已拥有" toggle 过滤掉本地库已有的（避免重复下）

### Phase 3 — Cover refill (`/api/cover-refill`)
mdcx 因 JavBus 被 Cloudflare 拦下载封面时，从 JavDB CDN 自动补图：
读 NFO 取 javdbid → `c0.jdbstatic.com/covers/<前两位>/<id>.jpg` → 写
`<code>-poster.jpg / -fanart.jpg / -thumb.jpg / folder.jpg`。
首次运行就修补了 114/210 个缺图条目。

### Phase 4 — 监控 + 通知
- Prometheus `/metrics` —— task counts / 各 stage durations / mdcx 成功率
- Grafana 仪表盘（`deploy/grafana/`）
- Telegram bot 推关键事件（QC failed exhausted / scrape failed / 等）

### Phase 5 — Setup wizard (`/setup`)
所有依赖服务的配置都在 web UI 里 Test+Save，**不再需要 SSH 编辑 .env**：

- mdcx 自动下载安装（点按钮触发 `setup-mdcx.ps1`，约 5 分钟 / 300 MB）
- 或指向已有 mdcx 路径
- MoviePilot / qBT / Jellyfin 各自 URL+鉴权信息测试再保存
- 保存后热加载，无需重启服务

## 配置

**推荐**：装好后开浏览器到 `/setup`，按卡片 Test+Save 完成配置。

**手工**：`.env.example → .env`，关键字段如下（也是 `/setup` 后台写入的字段）：

```ini
# MoviePilot
MP_URL=http://localhost:3000
MP_USER=admin
MP_PASS=change-me

# qBittorrent WebUI
QBT_URL=http://localhost:8080
QBT_USER=admin
QBT_PASS=change-me
QBT_JAV_CATEGORY=JAV
QBT_JAV_SAVEPATH=G:\Downloads\JAV-staging

# mdcx fork (CLI 入口为 mdcx.cmd.main)
# https://github.com/sqzw-x/mdcx               (上游 GUI 版)
# https://github.com/naughtyGitCat/mdcx        (本 fork 添加 CLI)
MDCX_DIR=E:\mdcx-src
MDCX_PYTHON=E:\mdcx-src\.venv\Scripts\python.exe
MDCX_MODULE=mdcx.cmd.main

# Jellyfin (可选，未来用于触发库刷新)
JELLYFIN_URL=
JELLYFIN_API_KEY=

# Telegram 通知 (可选)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

> ⚠ **Personal-use tool.** Designed for my homelab; defaults assume a single-user
> Windows machine on a trusted LAN. Do not expose `:5000` to the internet —
> there is no auth on mp-relay itself, and it can add arbitrary downloads.

## 部署

**普通用户**：从 [Releases](https://github.com/naughtyGitCat/mp-relay/releases)
下载最新的 `mp-relay-Setup-<版本>.exe`，双击安装。安装包自带 Python 运行时 + NSSM，
向导默认勾选 "Install as Windows service"，安装完打开浏览器到 `/setup` 配置依赖
服务即可。详见 [`deploy/README.md`](deploy/README.md)。

**开发迭代**：用 [`deploy/install-on-windows.ps1`](deploy/install-on-windows.ps1) 脚本
（scp 源码到主机 + 创建 venv + 注册服务），改完代码 rsync + restart 服务即可，
不用每次发版。

**构建 .exe**：[`build/README.md`](build/README.md) — 打 tag 后 GitHub Actions
自动 build + 附到 release。

**集成测试 (WIP)**：[`tests/integration/packer/README.md`](tests/integration/packer/README.md) ——
Packer + autounattend.xml 全自动跑 Win11 24H2 + 装 mp-relay + 烟测 `/health`，
Win11 24H2 解析器 bug 卡了几个雷已记录，未完成。当前 testing 用 Hyper-V checkpoint
冻结 `mp-relay-test` VM 当 pristine 基线。

## Reference projects（同类目设计参考）

写本项目时调研过的相关项目，**不是依赖**，只是借鉴架构 / 数据源 / 元数据策略：

| Repo | 借鉴点 |
|---|---|
| [yuukiy/JavSP](https://github.com/yuukiy/JavSP) | 番号识别正则（覆盖各厂牌格式）；本地批量整理流程；多源 fallback |
| [dirtyracer1337/Jellyfin.Plugin.PhoenixAdult](https://github.com/dirtyracer1337/Jellyfin.Plugin.PhoenixAdult) | Jellyfin 端直接刮削方案；可作为 mdcx 失败时的后备 metadata 源 |
| [guyueyingmu/avbook](https://github.com/guyueyingmu/avbook) | 演员维度的发现 / 索引 UI 思路；按厂牌/类别筛选 |
| [gfriends/gfriends](https://github.com/gfriends/gfriends) | 演员头像数据库（commit-only 仓库），mdcx 找不到头像时可回落 |
| [zyd16888/sehuatang](https://github.com/zyd16888/sehuatang) | 色花堂论坛抓取 / 番号 → 磁力链映射，**早期 Phase 1 番号搜种数据源** |

每个的笔记单独放在 [`docs/references.md`](docs/references.md)。

## 风险提示 / 已知坑

- **MDCX 调用失败时会留在 `E:\Jav_failed`**，需要定期人工处理（或调用
  `/api/cloud115/retry-failed-scrapes` 重试有 staging 文件的）
- **qBT category save_path 改了之后，已有种子的下载位置不会自动迁移**
- **watcher 用 polling**（默认 60s 间隔），对 qBT/115 友好但延迟最大 60s
- **mdcx 并发 = 2 (semaphore)** —— 早期版本曾"60 并发风暴"被 JavBus 限流卡死，
  现在硬限。
- **`/discover` 的 JavBus 抓取受 Cloudflare 影响** —— 期间封面通过
  `/api/img-proxy` 服务端代理 + Referer 注入绕过；mdcx 抓不到封面时由
  `cover-refill` 兜底。
- **115 download URL 绑 UA** —— mp-relay 用 pinned Chrome UA 同时给 sign URL +
  HTTP GET，否则 403。
- **路径含 `[4K] / @ / ()`** —— mdcx glob 行为踩坑，post_download 管线在跑
  mdcx 前 sanitize（剥这些字符）。
