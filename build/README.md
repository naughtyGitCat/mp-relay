# mp-relay Windows installer build scripts

Source files for the Inno Setup `.exe` installer. The CI workflow at
`.github/workflows/build-installer.yml` consumes these and produces a single
`mp-relay-Setup-<version>.exe` per release.

## Files

| File | Purpose |
|---|---|
| `build.iss`              | Inno Setup main script — install / upgrade / uninstall logic |
| `launcher.bat`           | Foreground launcher used when the user opts OUT of service mode |
| `service-install.ps1`    | Registers + starts the NSSM-wrapped Windows service |
| `service-uninstall.ps1`  | Reverse — stops + removes the service |
| `nssm.exe`               | NSSM 2.24 (public domain), wraps `python.exe` as a Windows service |

> `nssm.exe` is committed (331 KB) so neither local builds nor CI need to re-fetch it.
> Origin: https://nssm.cc/release/nssm-2.24.zip → `nssm-2.24/win64/nssm.exe`.
> SHA256 `f689ee9af94b00e9e3f0bb072b34caaf207f32dcb4f5782fc9ca351df9a06c97` —
> CI verifies this on every build to guard against accidental tampering.

## How CI builds the installer

Triggered on tag push (`v*`). Steps in `.github/workflows/build-installer.yml`:

1. **Checkout source** at the tagged commit.
2. **Download Python 3.12 embed**, expand to `build/Python/`.
3. **Inject pip** (`get-pip.py`) and `pip install -r requirements.txt` into
   `build/Python/Lib/site-packages/`. This is the trick that lets the
   installer ship a self-contained Python runtime without needing pip /
   network at user-install time.
4. **Run `iscc /DMyAppVersion=<tag> build/build.iss`** on the Windows runner,
   which produces `build/exe/mp-relay-Setup-<tag>.exe`.
5. **Attach the .exe** to the GitHub Release for the tag.

## Local build (Windows, for testing)

```powershell
# Requires Inno Setup 6 installed (https://jrsoftware.org/isdl.php).
# 1. Stage the Python runtime + deps into build\Python\
$python = "C:\Program Files\Python312\python.exe"
$build  = "C:\github\mp-relay\build"

# Mirror the system Python into build\Python (full distribution, not embed-zip):
robocopy "C:\Program Files\Python312" "$build\Python" /E /XD __pycache__ Doc

# Install requirements into the bundled runtime
& "$build\Python\python.exe" -m pip install -r "$build\..\requirements.txt"

# 2. Compile
cd $build
iscc /DMyAppVersion=2.0.0.local build.iss
# → build\exe\mp-relay-Setup-2.0.0.local.exe
```

## Why bundled Python (and not the embed-zip distribution)?

`python-3.12.X-embed-amd64.zip` is much smaller (~14 MB) but has known issues
with importing some stdlib modules and pip in particular. mp-relay ships only
~50 MB of dependencies (httpx, fastapi, lxml, p115client, …), so trading
~80 MB of Python install size for a robust install isn't a big deal. The
windows-moviepilot installer uses the same approach for the same reason.

## What the installer does NOT bundle

- **mdcx** (`E:\mdcx-src`) — the user installs the fork separately
- **MoviePilot / qBittorrent** — already in the user's homelab stack
- **state.db** — created on first run; preserved across upgrades
- **.env** — copied from `.env.example` on first install only; preserved across upgrades

`build.iss` warns at install time if `E:\mdcx-src` or `MoviePilot` aren't
visible at default paths — soft warning, the user can dismiss if they live
elsewhere or on remote hosts.

## Service mode default

Service-mode checkbox is checked by default in the wizard:

- Service named `mp-relay`, auto-start on boot, restart-on-crash with 5 s delay
- Logs at `{install-dir}\service-logs\stdout.log` + `stderr.log`, 10 MB rotation
- Runs as LocalSystem (good enough for talking to localhost qBT / MP / mdcx
  unless you need access to mapped network drives — in which case re-bind to
  a user account via `services.msc → mp-relay → Log On`)

Uncheck the service task to install the launcher only — desktop shortcut
opens `mp-relay.bat` in foreground for ad-hoc runs / log inspection.

## Upgrading

1. Run the new installer over the existing install. Inno Setup's
   `PrepareToInstall` stops the running service, copies the new files
   (`.env` and `state.db` are preserved by `onlyifdoesntexist` /
   not-listed-in-Files semantics), then re-registers + starts the service.
2. Health check: `http://localhost:5000/health` should return 200 within
   5–10 s. If not, see `service-logs\stderr.log`.

## Uninstalling

`Add or Remove Programs → mp-relay → Uninstall`.

Removes:
- `app\` + `templates\` + `Python\` + service-logs
- The Windows service (via `service-uninstall.ps1`)

Keeps:
- `.env` (your config) — delete manually if you want a clean slate
- `state.db` (task history) — delete manually for full reset
