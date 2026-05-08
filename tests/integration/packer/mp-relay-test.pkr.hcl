// Packer template — fully unattended Win11 24H2 VM that ends up with
// mp-relay installed + smoke-tested. Run on the Hyper-V host (.13).
//
//   packer init    .
//   packer validate -var-file=local.pkrvars.hcl .
//   packer build   -var-file=local.pkrvars.hcl  .
//
// Inputs (passed via -var or local.pkrvars.hcl):
//   iso_path       full path to the Win11 24H2 amd64 ISO
//                  (built once via uupdump; reused across runs)
//   installer_path full path to mp-relay-Setup-<v>.exe to install in the VM
//                  (typically downloaded fresh from GH artifact)
//   output_dir     where the resulting VHDX + Packer artifacts land
//
// Outputs:
//   <output_dir>/Virtual Hard Disks/packer-mp-relay-test.vhdx
//   This is your "golden image" — clone via differencing disks for fast
//   subsequent test VMs, or just rerun packer build to refresh.

packer {
  required_plugins {
    hyperv = {
      source  = "github.com/hashicorp/hyperv"
      version = ">= 1.1.4"
    }
  }
}

variable "iso_path" {
  type        = string
  description = "Absolute path to the Win11 24H2 amd64 ISO."
}

variable "iso_checksum" {
  type        = string
  default     = "none"
  description = "SHA256 of the ISO, or 'none' to skip verification (we trust the local file)."
}

variable "installer_path" {
  type        = string
  description = "Absolute path to mp-relay-Setup-<v>.exe on the host."
}

variable "output_dir" {
  type        = string
  default     = "I:/Hyper-V/packer-output/mp-relay-test"
  description = "Where Packer drops the resulting VHDX + metadata."
}

variable "vm_name" {
  type    = string
  default = "packer-mp-relay-test"
}

variable "memory_mb" {
  type    = number
  default = 8192
}

variable "cpus" {
  type    = number
  default = 4
}

variable "disk_size_mb" {
  type    = number
  default = 81920   // 80 GB; dynamic VHDX, only consumes what's used
}

variable "switch_name" {
  type        = string
  default     = "Default Switch"
  description = "Hyper-V virtual switch — Default Switch is NAT, lets the VM reach the internet for setup-mdcx etc."
}

source "hyperv-iso" "mp_relay_test" {
  iso_url          = var.iso_path
  iso_checksum     = var.iso_checksum

  vm_name          = var.vm_name
  generation       = 2
  cpus             = var.cpus
  memory           = var.memory_mb
  disk_size        = var.disk_size_mb

  switch_name        = var.switch_name
  // Secure Boot OFF for the test VM. UUP-built Win11 24H2 ISOs (the only
  // way to get a fresh ISO post-Sept-2024 without a TechBench / VL key)
  // ship a bootmgr signed with a cert chain that Hyper-V's
  // MicrosoftWindows template still rejects -> "boot loader failed"
  // at UEFI POST. With Secure Boot OFF the ISO loads cleanly. Win11
  // setup itself doesn't strictly require Secure Boot at install time;
  // it warns but proceeds, and our autounattend skips that warning.
  enable_secure_boot = false
  enable_tpm         = true
  // vTPM is still on so Win11 setup's TPM 2.0 check passes; equivalent
  // to clicking "Enable TPM" on a Gen2 VM in Hyper-V Manager.

  output_directory     = var.output_dir
  shutdown_command     = "shutdown /s /t 10 /f /d p:4:1 /c \"Packer shutdown\""
  shutdown_timeout     = "10m"

  // Windows 11 ISO bootloader shows "Press any key to boot from CD or DVD"
  // for ~5-6 seconds before falling through. The prompt appears AFTER
  // UEFI POST + bootmgr load, which on Hyper-V Gen2 takes ~7-9 seconds
  // from VM start. Need boot_wait long enough to get past UEFI but not
  // miss the prompt window. 8s + a wide spray of keystrokes catches it.
  // (Verified empirically by screenshotting at boot_wait=3s and seeing
  // the prompt arrive after our keystrokes had already been sent.)
  boot_wait = "8s"
  boot_command = [
    "<enter><wait500ms>",
    "<enter><wait500ms>",
    "<enter><wait500ms>",
    "<enter><wait500ms>",
    "<enter><wait500ms>",
    "<enter><wait500ms>",
    "<enter><wait500ms>",
    "<enter>",
  ]

  // Secondary ISO containing autounattend.xml. We pre-build this on the
  // dev machine (`hdiutil makehybrid -o autounattend.iso -iso -joliet
  // -default-volume-name PROVISION ./autounattend-files/`) and ship the
  // ready ISO alongside the .pkr.hcl. Originally tried Packer's
  // ``cd_files`` (auto-builds the ISO at run time) but it requires
  // oscdimg/mkisofs/xorriso/hdiutil on the Hyper-V host — none of which
  // ship on a stock Win11 host. Pre-building avoids that dependency.
  secondary_iso_images = ["./autounattend.iso"]

  // First boot has the install + reboots; we wait for WinRM with
  // generous timeout so disk + reboot quirks don't fail the build.
  communicator   = "winrm"
  winrm_username = "packer"
  winrm_password = "Packerpass1!"
  winrm_timeout  = "45m"
  winrm_use_ntlm = true
  winrm_port     = 5985
}

build {
  name    = "mp-relay-integration"
  sources = ["source.hyperv-iso.mp_relay_test"]

  // ----------------------------------------------------------------------
  // Sanity check that we have admin + a working WinRM channel
  // ----------------------------------------------------------------------
  provisioner "powershell" {
    inline = [
      "Write-Host '[provisioner] Connected as ' $env:USERNAME ' on ' $env:COMPUTERNAME",
      "$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)",
      "Write-Host '[provisioner] isAdmin=' $isAdmin",
      "if (-not $isAdmin) { throw 'Provisioner is not running with admin token — autounattend autologon broken?' }"
    ]
  }

  // ----------------------------------------------------------------------
  // Push the mp-relay installer into the VM
  // ----------------------------------------------------------------------
  provisioner "file" {
    source      = var.installer_path
    destination = "C:/Users/packer/Downloads/mp-relay-Setup.exe"
  }

  // ----------------------------------------------------------------------
  // Silent install + smoke test
  // ----------------------------------------------------------------------
  provisioner "powershell" {
    inline = [
      "$exe = 'C:/Users/packer/Downloads/mp-relay-Setup.exe'",
      "$log = 'C:/Users/packer/Downloads/mp-relay-install.log'",
      "Write-Host '[provisioner] Installing mp-relay (silent)...'",
      "Unblock-File -LiteralPath $exe -ErrorAction SilentlyContinue",
      "$proc = Start-Process -FilePath $exe -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/TASKS=service',\"/LOG=$log\" -Wait -PassThru",
      "if ($proc.ExitCode -ne 0) { Get-Content -Tail 30 $log; throw \"installer exit code $($proc.ExitCode)\" }",
      "Write-Host '[provisioner] Installer finished cleanly. Waiting 8s for service warmup...'",
      "Start-Sleep 8",
      "$r = Invoke-WebRequest -Uri http://127.0.0.1:5000/health -UseBasicParsing -TimeoutSec 10",
      "if ($r.StatusCode -ne 200) { throw \"/health returned $($r.StatusCode)\" }",
      "Write-Host '[provisioner] /health: ' $r.Content",
      "$s = Invoke-WebRequest -Uri http://127.0.0.1:5000/api/setup/status -UseBasicParsing -TimeoutSec 5",
      "if ($s.StatusCode -ne 200) { throw \"/api/setup/status returned $($s.StatusCode)\" }",
      "Write-Host '[provisioner] /api/setup/status reachable'",
      "$page = Invoke-WebRequest -Uri http://127.0.0.1:5000/setup -UseBasicParsing -TimeoutSec 5",
      "if ($page.StatusCode -ne 200) { throw \"/setup HTML returned $($page.StatusCode)\" }",
      "Write-Host '[provisioner] /setup HTML: ' $page.Content.Length ' bytes'",
      "Write-Host '[provisioner] All smoke tests passed.'"
    ]
  }
}
