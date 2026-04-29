# Deploy mp-relay on the Windows host

Target: `10.100.100.13` (Windows). Service runs alongside MoviePilot, qBittorrent, mdcx.

## Layout on the Windows side

```
C:\mp-relay\
├── app\              ← Python source
├── templates\
├── .venv\            ← created by deploy script
├── .env              ← config (copy from .env.example, fill in)
├── state.db          ← runtime SQLite (auto-created)
└── nssm.exe          ← reuse the one from MoviePilot install (or copy)
```

## Steps (one-shot)

The script `deploy/install-on-windows.ps1` does all of this in one go. From your dev machine:

```bash
# 1. scp the project
scp -r ~/github/mp-relay the2n@10.100.100.13:C:/mp-relay-tmp

# 2. run the install script remotely
ssh the2n@10.100.100.13 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\mp-relay-tmp\deploy\install-on-windows.ps1 -Source C:\mp-relay-tmp -Target C:\mp-relay'
```

What it does:
1. Move source from `C:\mp-relay-tmp` to `C:\mp-relay`
2. Create venv at `C:\mp-relay\.venv` using system Python (must be 3.11+)
3. `pip install -r requirements.txt`
4. Reuse `C:\Program Files (x86)\MoviePilot\nssm.exe`, register service `mp-relay`
5. Start the service
6. Verify it listens on `:5000`

## Manual steps (if you want)

```powershell
# Move source
Move-Item C:\mp-relay-tmp C:\mp-relay
cd C:\mp-relay

# Create venv with system Python
& "C:\Program Files\Python312\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Configure
copy .env.example .env
notepad .env   # fill in passwords

# Test run (foreground)
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 5000
# → browser hits http://10.100.100.13:5000

# Install as service (reuse nssm.exe from MoviePilot install)
$nssm = "C:\Program Files (x86)\MoviePilot\nssm.exe"
$python = "C:\mp-relay\.venv\Scripts\python.exe"
& $nssm install mp-relay $python -m uvicorn app.main:app --host 0.0.0.0 --port 5000
& $nssm set mp-relay AppDirectory C:\mp-relay
& $nssm set mp-relay AppEnvironmentExtra "PYTHONUNBUFFERED=1"
& $nssm set mp-relay Start SERVICE_AUTO_START
& $nssm set mp-relay AppStdout C:\mp-relay\service-stdout.log
& $nssm set mp-relay AppStderr C:\mp-relay\service-stderr.log
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
  ~/github/mp-relay/ the2n@10.100.100.13:C:/mp-relay/
ssh the2n@10.100.100.13 'powershell -Command "Restart-Service mp-relay"'
```

## Uninstall

```powershell
& $nssm stop mp-relay
& $nssm remove mp-relay confirm
Remove-Item C:\mp-relay -Recurse -Force
```

## Troubleshooting

- **502 / blank UI**: check `C:\mp-relay\service-stderr.log`
- **mdcx never fires**: hit `http://localhost:5000/health` — `mdcx` field tells you what's wrong
- **qBT category not created**: check qBT WebUI is reachable from inside the service (`netstat -an | findstr 8080`)
- **qBT login 401**: re-check `.env` `QBT_PASS`
- **服务起不来**: `& $nssm status mp-relay` 看 stdout/stderr 路径
