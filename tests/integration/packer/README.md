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

## Troubleshooting

- **"WinRM timeout"**: usually means autounattend hasn't gotten to
  `FirstLogonCommands` yet. The default 45 min timeout is generous; if
  it really runs out, RDP/console into the VM (vmconnect) and check
  whether OOBE is stuck somewhere new in 24H2 (Microsoft sometimes
  adds new mandatory screens).
- **"product key not accepted"**: build edition mismatch. Open the ISO,
  inspect `sources/install.wim` with `Get-WindowsImage`, find the right
  index, and update `<Value>Windows 11 Pro</Value>` in autounattend.xml.
- **TPM / Secure Boot errors at install**: confirm `enable_tpm = true`
  + `enable_secure_boot = true` in the .pkr.hcl. Hyper-V Gen2 supports
  vTPM out of the box on Windows Server / Win11 hosts.

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
