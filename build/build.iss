; mp-relay Windows installer
;
; Builds a single .exe that installs mp-relay alongside the embedded Python
; runtime + NSSM, optionally registering it as a Windows service. Modeled
; after windows-moviepilot/build/build.iss; major differences:
;
;   - Smaller payload (no frontend dist, no Nginx — just FastAPI + Jinja).
;   - Python runtime is bundled with deps already pre-installed at CI time
;     into ``Python\Lib\site-packages\`` (no pip-install at user install
;     time → no network, no SSL surprises). See ``.github/workflows/
;     build-installer.yml``.
;   - We do NOT bundle mdcx / MoviePilot / qBittorrent: those are external
;     deps the user provides. Installer warns if any are missing in
;     ``[Code] InitializeSetup`` so the user can fix before .env config.
;   - .env preserved on upgrade (Inno's onlyifdoesntexist flag).
;   - state.db / *.db-wal / *.db-shm preserved on upgrade (uninsneveruninstall +
;     not in [Files] at all → upgrade leaves them alone).
;
; Build invocation (Windows + Inno Setup 6 required):
;   iscc /DMyAppVersion=<version> build.iss
;
; Required files next to this script at build time (CI puts them there):
;   ..\app\           (mp-relay source)
;   ..\templates\
;   ..\requirements.txt
;   ..\.env.example
;   .\Python\         (full Python runtime with deps pre-installed)
;   .\nssm.exe        (NSSM v2.24)

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0.dev"
#endif

#define MyAppName        "mp-relay"
#define MyAppPublisher   "naughtyGitCat"
#define MyAppURL         "https://github.com/naughtyGitCat/mp-relay"
#define MyAppExeName     "mp-relay.bat"
#define MyServiceName    "mp-relay"

[Setup]
; Stable AppId so upgrades land in the same install dir / registry slot.
AppId={{6c2d3e7f-9a4b-4c11-8b5d-mprelay00001}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableDirPage=no
DisableProgramGroupPage=yes
OutputDir=exe
OutputBaseFilename=mp-relay-Setup-{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UsePreviousAppDir=yes
ChangesEnvironment=no

[Languages]
Name: "english";      MessagesFile: "compiler:Default.isl"
; To enable Chinese wizard: drop ChineseSimplified.isl alongside this file
; and uncomment the line below.
;Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
; Service mode is the standard install — leave checked. Power users who want
; to inspect logs interactively can uncheck and use the desktop launcher.
Name: "service";     Description: "Install as Windows service (auto-start on boot)"; GroupDescription: "Service"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
; Lazy-setup task: optionally bootstrap mdcx (uv + Python 3.13 + ~250 MB
; deps + headless Chromium ~150 MB) right after install. Unchecked by
; default — fast install path stays fast; user can opt in here, or run
; "Setup mdcx" Start-Menu shortcut later.
Name: "setupmdcx";   Description: "Set up mdcx now (downloads ~300 MB, takes ~5 minutes)"; GroupDescription: "Optional: scrape pipeline"; Flags: unchecked

[Files]
; --- mp-relay Python source. Excludes development cruft + the user's local
;     .venv, runtime SQLite, and .env. We DON'T list .env / state.db here at
;     all — they're created by [Run] / first launch and we never touch them
;     on upgrade.
Source: "..\app\*";       DestDir: "{app}\app";       Flags: ignoreversion recursesubdirs createallsubdirs; \
    Excludes: "__pycache__,*.pyc,*.pyo"
Source: "..\templates\*"; DestDir: "{app}\templates"; Flags: ignoreversion recursesubdirs createallsubdirs

; --- Reference docs / config schema. Read-only, overwritten on upgrade.
Source: "..\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\.env.example";     DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md";        DestDir: "{app}"; Flags: ignoreversion

; --- Ship default .env on FIRST install only. ``onlyifdoesntexist`` skips it
;     on upgrade (user's edits preserved). ``uninsneveruninstall`` keeps it
;     on uninstall so a reinstall doesn't lose config.
Source: "..\.env.example"; DestDir: "{app}"; DestName: ".env"; \
    Flags: onlyifdoesntexist uninsneveruninstall

; --- Embedded Python runtime (deps pre-installed by CI into site-packages)
Source: "Python\*"; DestDir: "{app}\Python"; Flags: ignoreversion recursesubdirs createallsubdirs

; --- Launchers / service scripts
Source: "launcher.bat";          DestDir: "{app}"; DestName: "{#MyAppExeName}"; Flags: ignoreversion
Source: "nssm.exe";              DestDir: "{app}"; Flags: ignoreversion
Source: "service-install.ps1";   DestDir: "{app}"; Flags: ignoreversion
Source: "service-uninstall.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "setup-mdcx.ps1";        DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}";           Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\Edit config (.env)";     Filename: "notepad.exe"; Parameters: """{app}\.env"""
Name: "{group}\Setup mdcx (run once)";  Filename: "powershell.exe"; \
    Parameters: "-ExecutionPolicy Bypass -NoProfile -NoExit -File ""{app}\setup-mdcx.ps1"" -InstallDir ""{app}"""; \
    Comment: "Bootstrap mdcx (uv + Python 3.13 + Chromium ~300 MB total). Run once after install."
Name: "{group}\Open install folder";    Filename: "{app}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";     Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; Always pop a dialog reminding the user to edit .env before / after install
; — service won't start with the placeholder passwords. Skip in silent mode.
Filename: "notepad.exe"; \
    Parameters: """{app}\.env"""; \
    Description: "Open .env in Notepad to set passwords (REQUIRED before service starts)"; \
    Flags: postinstall skipifsilent unchecked nowait

; If user opted into service mode: install + start the service.
Filename: "powershell.exe"; \
    Parameters: "-ExecutionPolicy Bypass -NoProfile -File ""{app}\service-install.ps1"" -InstallDir ""{app}"""; \
    StatusMsg: "Installing Windows service..."; \
    Tasks: service; \
    Flags: runhidden waituntilterminated

; If user opted into mdcx setup: run setup-mdcx.ps1 in a visible console so
; they can watch the ~300 MB download. waituntilterminated keeps the wizard's
; "Finish" button gated until setup actually finishes — important because
; the next [Run] step (.env editor) shouldn't open before MDCX_DIR is in there.
Filename: "powershell.exe"; \
    Parameters: "-ExecutionPolicy Bypass -NoProfile -File ""{app}\setup-mdcx.ps1"" -InstallDir ""{app}"" -NoServiceRestart"; \
    StatusMsg: "Bootstrapping mdcx (downloading ~300 MB, please wait)..."; \
    Tasks: setupmdcx; \
    Flags: waituntilterminated

; If NOT service mode: offer to launch interactively after install.
Filename: "{app}\{#MyAppExeName}"; \
    Description: "{cm:LaunchProgram,{#MyAppName}}"; \
    Flags: nowait postinstall skipifsilent shellexec unchecked

[UninstallRun]
; Always remove the service (no-op if not present). Runs before file deletion.
Filename: "powershell.exe"; \
    Parameters: "-ExecutionPolicy Bypass -NoProfile -File ""{app}\service-uninstall.ps1"" -InstallDir ""{app}"""; \
    RunOnceId: "RemoveMpRelayService"; \
    Flags: runhidden waituntilterminated

[UninstallDelete]
; Clean up logs + caches. Do NOT touch state.db / state.db-wal / .env —
; those persist across uninstall so a reinstall picks up where it left off.
Type: filesandordirs; Name: "{app}\service-stdout.log"
Type: filesandordirs; Name: "{app}\service-stderr.log"
Type: filesandordirs; Name: "{app}\service-stdout.log.*"
Type: filesandordirs; Name: "{app}\service-stderr.log.*"
Type: filesandordirs; Name: "{app}\service-logs"
Type: filesandordirs; Name: "{app}\Python\Lib\site-packages\__pycache__"
Type: filesandordirs; Name: "{app}\Python\__pycache__"
; mdcx tree (created by setup-mdcx.ps1, not by this installer's [Files]).
; Includes its .venv, browsers/, .git, ~300 MB total. Without this rule,
; uninstall leaves it behind because Inno only auto-removes what it placed.
Type: filesandordirs; Name: "{app}\mdcx"

[Code]
{ Stop the service before file copy on upgrade — locked python.exe / .pyd
  files would otherwise fail to overwrite. Mirrors windows-moviepilot's
  PrepareToInstall flow. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  NeedsRestart := False;

  if Exec('sc.exe', 'query "{#MyServiceName}"', '', SW_HIDE,
          ewWaitUntilTerminated, ResultCode) and (ResultCode = 0) then
  begin
    Log('Existing mp-relay service found, stopping before file copy...');
    Exec('net.exe', 'stop "{#MyServiceName}"', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
    Sleep(3000);
  end;

  { Free port 5000 if any orphan process is holding it. Common after a hard
    restart where NSSM didn't get a clean shutdown. }
  Exec('powershell.exe',
       '-NoProfile -Command "Get-NetTCPConnection -State Listen -LocalPort 5000 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1000);
end;

{ Pre-install sanity check: warn if mdcx / MoviePilot / qBittorrent aren't
  visible. We can't bundle them and the user will hit a wall during runtime
  config if they're missing — better to surface it now. Soft warn only;
  user may have valid reasons (different paths, remote services).}
function InitializeSetup(): Boolean;
var
  Missing: String;
begin
  Result := True;
  Missing := '';

  { Common mdcx fork install location — see config.py:MDCX_DIR default. }
  if not DirExists('E:\mdcx-src') then
    Missing := Missing + '  - E:\mdcx-src (mdcx fork)' + #13#10;

  { MoviePilot's typical install (we reuse its nssm.exe in deploy script;
    here just check for presence as a hint that the user has the rest of
    the homelab stack). }
  if not DirExists(ExpandConstant('{commonpf32}') + '\MoviePilot') and
     not DirExists(ExpandConstant('{commonpf}') + '\MoviePilot') then
    Missing := Missing + '  - MoviePilot (Windows installer)' + #13#10;

  if Missing <> '' then
  begin
    if MsgBox(
      'mp-relay depends on these external tools, which are NOT visible at standard locations:' + #13#10 + #13#10 +
      Missing + #13#10 +
      'You can still install mp-relay now if these live elsewhere or on remote hosts ' +
      '(configure paths in .env after install). Continue?',
      mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
  end;
end;
