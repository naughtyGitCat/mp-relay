# JAV magnet sources

mp-relay queries multiple sources concurrently for each 番号 search and merges + dedupes the results. Two sources are enabled by default and work without authentication; two more are opt-in because they're Cloudflare-protected.

## Default (no setup needed)

| Source | URL | Notes |
|---|---|---|
| `sukebei` | `sukebei.nyaa.si` | RSS feed, fast, broad coverage, real seeder counts |
| `javbus` | `www.javbus.com` | Curated, has 高清 / 中文字幕 / 4K tags, no seeders |

## Opt-in (require browser-extracted cookies)

| Source | URL | Why opt-in |
|---|---|---|
| `javdb` | `javdb.com` | Cloudflare WAF blocks server-side requests without browser session |
| `missav` | `missav.com` / `.ws` / `.ai` | Same — Cloudflare on every domain |

### How to extract a cookie string

1. Open the site in your browser, complete any age / login challenges manually
2. DevTools → Network tab → reload page → click the document request
3. In Request Headers, find `Cookie:` — copy the entire value (it's a long `key=value; key=value; ...` string)
4. Paste into `E:\mp-relay\.env`:

   ```ini
   JAVDB_COOKIE=_jdb_session=...; cf_clearance=...; over18=1
   MISSAV_COOKIE=cf_clearance=...; ...
   ```

5. Add the source name to `JAV_SEARCH_SOURCES`:

   ```ini
   JAV_SEARCH_SOURCES=sukebei,javbus,javdb,missav
   ```

6. `Restart-Service mp-relay`

### Cookie lifetime

- `cf_clearance` (the Cloudflare bypass token) is **per-IP and per-User-Agent**. If your network egress IP changes (e.g. mp-relay routes through a proxy that the cookie wasn't issued for), Cloudflare invalidates it.
- Login cookies (`_jdb_session`, etc.) typically last weeks. Refresh when JavDB / MissAV start returning 403 again.
- mp-relay logs `[app.jav_search] javdb search → HTTP 403` when a cookie expires; check `E:\mp-relay\service-stderr.log`.

### Disabling a source

Either remove its name from `JAV_SEARCH_SOURCES`:

```ini
JAV_SEARCH_SOURCES=sukebei,javbus
```

Or leave the cookie empty (the source self-skips and emits a `result=empty` metric).

## How sources interact

- All enabled sources query in parallel — slowest source bounds the search latency
- Same `info_hash` from multiple sources is deduped; **first batch wins** (source order in the array we iterate is `sukebei → javbus → javdb → missav`, so sukebei's seeder counts are kept when there's overlap)
- Final ranking: suspicion ASC → quality DESC → seeders DESC → size DESC
- Each candidate carries a `source` field surfaced in the UI and in `mp_relay_jav_search_total{source=...}`

## Adding a new source

1. Implement `async def _fetch_<name>(code: str) -> list[dict]` in `app/jav_search.py` returning candidates via `_build_candidate(...)`
2. Register in `_SOURCE_FETCHERS`
3. Add HTML-fixture tests in `tests/test_jav_search_multi.py`
