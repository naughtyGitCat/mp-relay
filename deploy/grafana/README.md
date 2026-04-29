# Grafana / Prometheus integration for mp-relay

## What's in here

- `dashboard.json` — Grafana dashboard JSON (10 panels covering submissions, pipeline runs, QC, merges, mdcx, in-flight tasks)
- `../prometheus-scrape.yml` — scrape job snippet to drop into your Prometheus config

## Wiring it up

### 1. Add scrape job to Prometheus

On the Prometheus host (`onething-oes-831`), append the contents of `prometheus-scrape.yml` to the existing `scrape_configs:` list in `prometheus.yml`, then reload:

```bash
ssh onething-oes-831
sudo vi /etc/prometheus/prometheus.yml      # paste under scrape_configs
sudo systemctl reload prometheus
# or: sudo killall -HUP prometheus
```

Verify mp-relay metrics are flowing:

```bash
curl -s http://onething-oes-831:9090/api/v1/label/__name__/values \
  | jq '.data[] | select(startswith("mp_relay_"))'
```

### 2. Import dashboard into Grafana

Grafana → Dashboards → New → Import → "Upload JSON file" → pick `dashboard.json`.
Datasource: pick your existing Prometheus datasource (the JSON references `prometheus` UID which Grafana auto-remaps on import).

### 3. Sanity-check

The dashboard should be useful immediately. Submit a JAV code through mp-relay and within 30s you should see the bar in "Submissions / 5min by kind" tick up.

## Metric reference

| Metric | Type | Labels |
|---|---|---|
| `mp_relay_submit_total` | Counter | `kind`, `result` |
| `mp_relay_jav_search_total` | Counter | `result` (cached/hit/empty/error) |
| `mp_relay_jav_search_duration_seconds` | Histogram | — |
| `mp_relay_pipeline_step_total` | Counter | `step`, `outcome` (ok/fail/skip) |
| `mp_relay_pipeline_step_duration_seconds` | Histogram | `step` |
| `mp_relay_pipeline_run_total` | Counter | `terminal_state` |
| `mp_relay_qc_total` | Counter | `result`, `reason_class` |
| `mp_relay_qc_retry_total` | Counter | `outcome` (swapped/exhausted/no_alt/no_code) |
| `mp_relay_files_deleted_total` | Counter | `category` (junk/dupe/sample/post_mdcx) |
| `mp_relay_extras_relocated_total` | Counter | — |
| `mp_relay_multipart_merged_total` | Counter | `outcome` (concat_copy/fallback_rename) |
| `mp_relay_disc_remux_total` | Counter | `kind` (bdmv/dvd), `outcome` |
| `mp_relay_mdcx_total` | Counter | `result` (ok/fail/timeout/skipped) |
| `mp_relay_inflight` | Gauge | `state` |

## Useful PromQL one-liners

```promql
# QC pass rate over 1h
sum(increase(mp_relay_qc_total{result="pass"}[1h]))
  / clamp_min(sum(increase(mp_relay_qc_total[1h])), 1)

# How often does the retry chain save us?
sum(rate(mp_relay_qc_retry_total{outcome="swapped"}[6h]))
  / sum(rate(mp_relay_qc_total{result="fail"}[6h]))

# p95 mdcx scrape time
histogram_quantile(0.95,
  sum by (le) (rate(mp_relay_pipeline_step_duration_seconds_bucket{step="mdcx"}[15m])))

# Scrape failure rate
sum(rate(mp_relay_mdcx_total{result="fail"}[1h]))
  / clamp_min(sum(rate(mp_relay_mdcx_total[1h])), 1)
```
