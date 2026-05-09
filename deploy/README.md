# Deploy mp-relay on a Windows host

Target: a Windows machine running MoviePilot + qBittorrent + mdcx (the fork at `E:\mdcx-src`).

## Two install paths

| Path | When |
|---|---|
| **`mp-relay-Setup-<v>.exe`** (Inno Setup installer) | normal install — bundled Python runtime + auto service registration. Get the latest from the [GitHub Releases page](https://github.com/naughtyGitCat/mp-relay/releases). |
| **`deploy/install-on-windows.ps1`** (this dir) | dev-iteration / scp-from-dev-machine — uses the host's system Python + reuses MoviePilot's nssm. Faster turnaround when hacking on source. |

Build details for the `.exe`: see [`../build/README.md`](../build/README.md).

## Inno Setup `.exe` install (recommended)

1. Download `mp-relay-Setup-<version>.exe` from Releases.
2. Double-click → wizard. Default `C:\Program Files (x86)\mp-relay\` — or pick any drive with space (E: recommended on this homelab to keep the boot SSD free).
3. Keep the **Install as Windows service** task checked (default).
4. **Recommended**: open `http://localhost:5000/setup` in browser → 4 cards
   (mdcx / MoviePilot / qBittorrent / Jellyfin), fill URL+creds, click
   "Test connection" then "Save" on each. Hot-loaded into running settings,
   no service restart. (Old workflow of editing `.env` in Notepad still
   works — wizard does the same I/O on `.env`.)
5. `services.msc → mp-relay → Start` (or `Restart-Service mp-relay`). Open `http://localhost:5000`.

The `/setup` page also has "下载并安装" buttons that auto-install **mdcx**
(via `setup-mdcx.ps1`, ~5 min, ~300 MB) and **MoviePilot** (via
`setup-moviepilot.ps1`, downloads latest Windows-MoviePilot release at
runtime) if you don't have them yet. Tail their progress in the
in-page log panel.

Upgrades: just run the new installer over the existing install. `.env` and `state.db` survive untouched. The wizard stops the running service before file copy and restarts it after.

Uninstall: `Add or Remove Programs → mp-relay → Uninstall`. Removes app + service. `.env` and `state.db` are kept by default — delete `C:\Program Files (x86)\mp-relay\` manually for a clean slate.

## Layout (matches both install paths)

```
<install-dir>\           ← C:\Program Files (x86)\mp-relay (.exe) or E:\mp-relay (ps1)
├── app\                 ← Python source
├── templates\
├── Python\              ← bundled runtime (.exe install) or .venv\ (ps1 install)
├── .env                 ← config (copy from .env.example, fill in passwords)
├── state.db             ← runtime SQLite (auto-created on first run)
├── nssm.exe             ← bundled (.exe) or reused from MoviePilot (ps1)
├── service-install.ps1
├── service-uninstall.ps1
├── service-logs\        ← stdout.log + stderr.log (10 MB rotation)
└── mp-relay.bat         ← foreground launcher (used when service mode unchecked)
```

## Dev-iteration install (PS1 path)

For when you're hacking on source and don't want to bump a release tag every time. Bigger differences vs the .exe path:

- Uses the host's **system Python** (must be 3.11+) — saves bundling
- Creates a **`.venv\`** instead of using a frozen `Python\` tree
- **Reuses** `C:\Program Files (x86)\MoviePilot\nssm.exe` instead of bundling
- Runs from `E:\mp-relay` rather than `Program Files`

Steps:

```bash
# 1. scp the project
scp -r ~/github/mp-relay <USER>@<HOST>:E:/mp-relay-tmp

# 2. run the install script remotely
ssh <USER>@<HOST> 'powershell -NoProfile -ExecutionPolicy Bypass -File E:\mp-relay-tmp\deploy\install-on-windows.ps1 -Source E:\mp-relay-tmp -Target E:\mp-relay'
```

What it does:
1. Move source from `E:\mp-relay-tmp` to `E:\mp-relay`
2. Create venv at `E:\mp-relay\.venv` using system Python (must be 3.11+)
3. `pip install -r requirements.txt`
4. Reuse `C:\Program Files (x86)\MoviePilot\nssm.exe`, register service `mp-relay`
5. Start the service
6. Verify it listens on `:5000`

## Manual steps (if you want)

```powershell
# Move source
Move-Item E:\mp-relay-tmp E:\mp-relay
cd E:\mp-relay

# Create venv with system Python
& "C:\Program Files\Python312\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Configure
copy .env.example .env
notepad .env   # fill in passwords

# Test run (foreground)
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 5000
# → browser hits http://<HOST>:5000

# Install as service (reuse nssm.exe from MoviePilot install)
$nssm = "C:\Program Files (x86)\MoviePilot\nssm.exe"
$python = "E:\mp-relay\.venv\Scripts\python.exe"
& $nssm install mp-relay $python -m uvicorn app.main:app --host 0.0.0.0 --port 5000
& $nssm set mp-relay AppDirectory E:\mp-relay
& $nssm set mp-relay AppEnvironmentExtra "PYTHONUNBUFFERED=1"
& $nssm set mp-relay Start SERVICE_AUTO_START
& $nssm set mp-relay AppStdout E:\mp-relay\service-stdout.log
& $nssm set mp-relay AppStderr E:\mp-relay\service-stderr.log
& $nssm set mp-relay AppRotateFiles 1
& $nssm set mp-relay AppRotateBytes 10485760
& $nssm set mp-relay AppExit Default Restart
& $nssm set mp-relay AppRestartDelay 5000
& $nssm set mp-relay DisplayName "mp-relay (magnet → MoviePilot/mdcx dispatcher)"
& $nssm start mp-relay
```

## Updating

```bash
# from dev machine
rsync -av --delete --exclude=.venv --exclude=state.db --exclude=.env \
  ~/github/mp-relay/ <USER>@<HOST>:E:/mp-relay/
ssh <USER>@<HOST> 'powershell -Command "Restart-Service mp-relay"'
```

## Moving between drives

If you need to relocate (e.g. C: → E:): see the `## Moving between drives` section
below — boils down to stop service, robocopy contents (excluding `.venv`),
recreate venv from system Python, update 4 NSSM paths (`Application`,
`AppDirectory`, `AppStdout`, `AppStderr`), restart, verify `/health`.

```powershell
Stop-Service mp-relay
robocopy C:\mp-relay E:\mp-relay /E /XD .venv __pycache__ .pytest_cache /XF *.pyc *.pyo
& "C:\Program Files\Python312\python.exe" -m venv E:\mp-relay\.venv
E:\mp-relay\.venv\Scripts\python.exe -m pip install -r E:\mp-relay\requirements.txt

$nssm = "C:\Program Files (x86)\MoviePilot\nssm.exe"
& $nssm set mp-relay Application E:\mp-relay\.venv\Scripts\python.exe
& $nssm set mp-relay AppDirectory E:\mp-relay
& $nssm set mp-relay AppStdout    E:\mp-relay\service-stdout.log
& $nssm set mp-relay AppStderr    E:\mp-relay\service-stderr.log
Start-Service mp-relay
Invoke-RestMethod http://localhost:5000/health
# Once verified, delete C:\mp-relay (may need a reboot to release Windows file locks)
```

## Uninstall

```powershell
& $nssm stop mp-relay
& $nssm remove mp-relay confirm
Remove-Item E:\mp-relay -Recurse -Force
```

## Optional integrations

- **Prometheus + Grafana**: see [`grafana/README.md`](grafana/README.md) — drops a scrape job into the existing Prometheus on `onething-oes-831` and imports a 10-panel dashboard.
- **Telegram notifications**: see [`telegram-setup.md`](telegram-setup.md) — 5-step BotFather → token → chat_id → `.env` → restart flow.
- **More magnet sources** (JavDB / MissAV): see [`jav-sources.md`](jav-sources.md) — opt-in via browser-cookie extraction. Defaults (sukebei + JavBus) work without any setup.

## Troubleshooting

- **502 / blank UI**: check `E:\mp-relay\service-stderr.log`
- **mdcx never fires**: hit `http://localhost:5000/health` — `mdcx` field tells you what's wrong
- **qBT category not created**: check qBT WebUI is reachable from inside the service (`netstat -an | findstr 8080`)
- **qBT login 401**: re-check `.env` `QBT_PASS`
- **服务起不来**: `& $nssm status mp-relay` 看 stdout/stderr 路径
- **Telegram `/health` shows error**: see [`telegram-setup.md`](telegram-setup.md#troubleshooting)
