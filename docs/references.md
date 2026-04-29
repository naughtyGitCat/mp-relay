# 参考项目调研笔记

本项目设计阶段参考过以下开源项目。**全部只是架构 / 数据源 / 用户体验上的借鉴，没有作为依赖**。

## yuukiy/JavSP

- URL: https://github.com/yuukiy/JavSP
- 语言: Python
- 类型: 本地刮削器（命令行）

**借鉴点：**
- **番号识别正则**：覆盖了各大厂牌（S1/SOD/IDEAPOCKET/MOODYZ/...）、素人系列（FC2-PPV、HEYZO、1pondo、10musume、carib...）的命名规则。我们的 [`app/classifier.py`](../app/classifier.py) 里的 `_JAV_PATTERNS` 就参考了它的覆盖面。
- **多源 fallback**：JavSP 用 javbus / javdb / jav321 多个数据源串行 fallback。mdcx 自身已经支持类似机制，但万一 mdcx 全部失败，可以借 JavSP 的剩余数据源做最后一层兜底。
- **本地批量整理流程**：扫描目录 → 提取番号 → 查源 → 写 NFO → 重命名移动 —— 和我们 watcher 触发 mdcx 的流程一致。

**和本项目区别**：JavSP 是 CLI 单次运行，没有持久 watcher 也没有 web UI；我们做的是常驻服务。

## dirtyracer1337/Jellyfin.Plugin.PhoenixAdult

- URL: https://github.com/dirtyracer1337/Jellyfin.Plugin.PhoenixAdult
- 语言: C#（Jellyfin 插件）
- 类型: Jellyfin 端直接刮削成人内容

**借鉴点：**
- **mdcx 失败时的兜底**：PhoenixAdult 在 Jellyfin 服务器内运行，能直接给媒体项打 metadata，不依赖 NFO。如果 mdcx 把某个种子打到 `E:\Jav_failed` 但媒体值得抢救，可以让 Jellyfin 装这个插件试试自动补全。
- **Studio / Tag / Performer 的 Jellyfin 字段映射**：我们写 NFO 时哪些字段实际能在 Jellyfin UI 里看见，可以照着它的代码确认。

**和本项目区别**：PhoenixAdult 在线刮削、Jellyfin 内嵌；我们是离线 NFO 模式（`Jellyfin custom-lib NFO-only lockdown` 见用户 memory）。

## guyueyingmu/avbook

- URL: https://github.com/guyueyingmu/avbook
- 语言: Python + Flask
- 类型: 番号 / 演员 / 系列检索 + 收藏 web UI

**借鉴点：**
- **演员维度的发现界面**：按演员页可看到该演员所有作品列表 → "想看 / 已看 / 下载中"标记 → 一键订阅。我们 Phase 2 做"演员发现"时直接照搬这个交互。
- **三维分类**：按演员 / 按厂牌 / 按系列三种入口，对应 mdcx 的 `actors / studio / series` 字段。
- **数据库 schema**：avbook 的 SQLite 表结构（演员 / 番号 / 系列三表）可作 Phase 2 schema 草图。

**和本项目区别**：avbook 重检索和"想看清单"，不管下载/刮削；我们重下载 → 刮削 → 入库自动化。

## gfriends/gfriends

- URL: https://github.com/gfriends/gfriends
- 类型: 演员头像图床仓库（GitHub 仓库当 CDN 用）

**借鉴点：**
- **作为 mdcx 头像源**：mdcx 配置里可以加 gfriends 作为头像数据源。如果 mdcx 没有自带这个 source，Phase 3 我们的服务可以做后处理 — 扫 NFO 的 actor 字段，到 gfriends 拉对应头像，写到 `metadata\people\<actor>\folder.jpg` 让 Jellyfin 用。
- **数据格式**：仓库按 `<actor_name_kana>/<actor_name>.jpg` 组织，名字优先用日文罗马音。

**和本项目区别**：gfriends 是被动数据源；我们是主动消费方。

## zyd16888/sehuatang

- URL: https://github.com/zyd16888/sehuatang
- 类型: 色花堂论坛 (sehuatang.org) 抓取脚本

**借鉴点（Phase 1 番号搜种最重要的数据源）：**
- **番号 → 磁力链映射**：色花堂帖子里大量"番号 + 磁力链"组合，是 Phase 1"输入 SSIS-001 → 列出磁力链候选"的现成数据源。
- **抓取节流策略**：色花堂有反爬，这个项目的节流 / cookie 处理可以照搬。
- **格式化输出**：它的输出格式（番号、标题、大小、做种数、磁力链）正好对应我们 `qBT add_url` 需要的字段。

**和本项目区别**：zyd16888/sehuatang 是抓取工具，单次运行；我们是常驻服务，会把它包装成"按需查询，结果缓存到 SQLite"的形式（避免反复抓取被封）。

## 备注

- 所有这些项目都 **不会作为运行时依赖** 引入，避免 license / 维护负担。
- Phase 1（番号搜种）实施时，**优先借鉴 zyd16888/sehuatang 的抓取逻辑**，但用我们自己的代码写一份贴合本服务架构的版本。
- 如果发现某个项目有特别贴合的子模块，再单独评估是否合并 / 复用。
