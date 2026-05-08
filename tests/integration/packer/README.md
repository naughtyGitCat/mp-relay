# Packer integration test — fully unattended Win11 install + mp-relay smoke test

End-to-end automation of "spin up clean Win11 24H2, silent-install the latest
mp-relay-Setup.exe, hit /health to confirm the service is alive". No GUI
clicking, no manual OOBE.

## What you get

```
packer build  →  ~25 minutes  →  packer-mp-relay-test.vhdx
                                   (golden image; clone with diff disks
                                    for fast subsequent test VMs)
```

Per-build steps Packer drives:

1. Create Hyper-V Gen2 VM (8 GB / 4 vCPU / vTPM / Default Switch)
2. Mount the Win11 ISO + auto-built secondary CD with `autounattend.xml`
3. Boot → Win11 setup runs unattended (`autounattend.xml`)
4. Auto-create local admin `packer` / `Packerpass1!`
5. Auto-logon, run `FirstLogonCommands` (firewall off, WinRM up, Defender off)
6. Packer connects via WinRM, runs the build's provisioners:
   - Sanity-check admin token
   - Push `mp-relay-Setup-<v>.exe` over WinRM
   - `Start-Process /VERYSILENT` install
   - Smoke-test `/health`, `/api/setup/status`, `/setup`
7. `shutdown /s` → Packer exports the VHDX

## Prereqs (on the Hyper-V host, .13)

- **Hyper-V** with `Microsoft-Hyper-V-All` feature enabled
- **Packer** ≥ 1.10 (`choco install packer` or `scoop install packer`)
- **The Win11 24H2 ISO** built once via uupdump.net (see `iso_path` var)
- **The latest `mp-relay-Setup-<v>.exe`** (download CI artifact via
  `gh run download <run-id> -R naughtyGitCat/mp-relay`)

## Run

```powershell
cd <repo>\tests\integration\packer
copy local.pkrvars.hcl.example local.pkrvars.hcl
notepad local.pkrvars.hcl   # fix paths for your host

packer init    .
packer validate -var-file=local.pkrvars.hcl .
packer build   -var-file=local.pkrvars.hcl  .
```

Expect ~20–30 min total: ~10 min Win11 install, ~5 min OOBE + first logon,
~30 sec WinRM handshake, ~30 sec installer, ~5 sec smoke tests, ~30 sec
shutdown + export.

## Status (2026-05-08)

**Scaffolding is in place but the autounattend.xml ↔ Hyper-V Gen2 + Win11 24H2
combination has not been brought to a working end-to-end build yet.** The
`mp-relay-test.pkr.hcl` template, autounattend.xml, and provisioner scripts
all parse cleanly (`packer validate` passes), but live builds bisect the
following crashes/loops in Win11 setup:

### Known landmines (verified empirically by bisect)

| Element | Result on Hyper-V Gen2 + 24H2 (build 26100.8328) |
|---|---|
| Empty autounattend (just root element) | Setup boots to language picker. No loop. ✓ |
| `pass=windowsPE/InternationalCore + Setup/UserData/AcceptEula` | Same as empty. ✓ |
| Add `pass=specialize/Shell-Setup/ComputerName+TimeZone` | ✓ |
| Add `pass=oobeSystem/OOBE` block | ✓ |
| Add `UserAccounts/LocalAccounts/LocalAccount` | **Crashes setup → 7-second boot loop, never installs** |
| Add `UserAccounts/AdministratorPassword` instead | ✓ |
| Add `DiskConfiguration` | **Boot loop** |
| Add `ImageInstall` (`InstallTo` or `InstallToAvailablePartition`) | **Boot loop** |

This means a working autounattend currently can ONLY:
- Pre-set the language pack
- Use `AdministratorPassword` (built-in account, NOT a new local user)
- Skip OOBE screens

i.e. it can't drive disk partitioning or image picking. Setup has to be
driven through those phases interactively.

### Other gotchas worth knowing

- **Boot loader fails with Secure Boot ON for UUP-built ISOs** — even
  with `secure_boot_template = "MicrosoftWindows"`. Set
  `enable_secure_boot = false` (verified at the UEFI Boot Summary
  screen via Hyper-V WMI screenshot).
- **"Press any key to boot from CD"** — Hyper-V's synthetic keyboard
  doesn't deliver keystrokes to pre-OS bootmgr. Boot_command never
  catches the prompt. Workaround: rebuild the ISO with
  `efisys_noprompt.bin` instead of `efisys.bin`:
  ```
  cdimage -m -o -u2 -udfver102 \
    "-bootdata:2#p0,e,bC:\extract\boot\etfsboot.com#pEF,e,bC:\extract\efi\microsoft\boot\efisys_noprompt.bin" \
    C:\extract C:\out\win11-noprompt.iso
  ```
- **UUP-built zh-CN ISO image name is `Windows 11 专业版`**, not
  `Windows 11 Pro`. Use `<Key>/IMAGE/INDEX</Key><Value>1</Value>` if
  you do specify ImageInstall (locale-neutral).
- **Packer's `cd_files` requires `oscdimg`/`mkisofs`/`xorriso`/`hdiutil`** on
  the host — Windows hosts don't ship any of those. Pre-build the
  secondary ISO with `cdimage.exe` (bundled with uupdump's tools dir)
  and use `secondary_iso_images` instead.

### Where to pick this up

1. Find a known-working autounattend.xml from a current Win11 24H2
   community Packer template (check StefanScherer/packer-windows or
   rgl/windows-vagrant for recent updates).
2. **OR** switch to differencing-disk approach: manually do OOBE on a
   base VM once, sysprep + shutdown, then clone via
   `New-VHD -ParentPath base.vhdx -Differencing` for fast subsequent
   test VMs. Less elegant than Packer but works today.
3. **OR** use the official MS Win11 ISO from TechBench rather than a
   UUP-built one — fewer landmines around bootmgr signature, image
   names. Requires a TechBench account (free).

## Troubleshooting

- **"WinRM timeout"**: usually means autounattend hasn't gotten to
  `FirstLogonCommands` yet. The default 45 min timeout is generous; if
  it really runs out, RDP/console into the VM (vmconnect) and check
  whether OOBE is stuck somewhere new in 24H2 (Microsoft sometimes
  adds new mandatory screens).
- **"product key not accepted"**: build edition mismatch. Open the ISO,
  inspect `sources/install.wim` with `Get-WindowsImage`, find the right
  index, and update `<Value>Windows 11 Pro</Value>` in autounattend.xml.
- **TPM / Secure Boot errors at install**: confirm `enable_tpm = true`.
  Set `enable_secure_boot = false` for UUP-built ISOs (see above).

## Why these specific autounattend choices

- **Local admin via PlainText password**: this is a throwaway test VM.
  The base64-UTF16-LE Password element is finicky; we don't gain
  meaningful security by using it. Don't reuse this template for prod.
- **`HideOnlineAccountScreens=true` + BypassNRO reg write**: 24H2 added
  a hard "let's connect to a network" OOBE step that hides
  `HideOnlineAccountScreens` from doing its job. The reg key is the
  documented bypass.
- **Firewall fully off + WinRM unencrypted basic auth**: this is the
  Packer-recommended bare-minimum setup for the WinRM communicator on
  a single-host test network. Don't apply this template to anything
  reachable from outside the host.
