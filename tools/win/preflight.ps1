<#
  preflight.ps1 - READ-ONLY state report before a Windows sitting (changes nothing, touches no hardware).
  Usage (elevated PowerShell, anywhere):  powershell -ExecutionPolicy Bypass -File tools\win\preflight.ps1
#>
$ErrorActionPreference = 'Continue'
$bad = 0
function Line($ok, $name, $msg) { $t = if ($ok) { 'OK  ' } else { 'FIX ' }; if (-not $ok) { $script:bad++ }; Write-Host ("[{0}] {1}: {2}" -f $t, $name, $msg) }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Line $admin 'Administrator' $(if ($admin) { 'elevated' } else { 'NOT elevated - reopen the terminal as Administrator' })

# Memory integrity (HVCI) must be off for the driver to load
$hv = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity' -ErrorAction SilentlyContinue).Enabled
Line ($hv -ne 1) 'Memory integrity' $(if ($hv -eq 1) { 'ON - turn off: Windows Security > Device security > Core isolation > Memory integrity, then reboot' } else { "off (Enabled=$hv)" })

# Vulnerable driver blocklist: 1 = on (blocks the ThrottleStop driver), 0 = off, missing = default (on on Win11 22H2+)
$bl = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Config' -ErrorAction SilentlyContinue).VulnerableDriverBlocklistEnable
Line ($bl -eq 0) 'Vulnerable driver blocklist' $(if ($bl -eq 0) { 'off' } else { "ON or default (value=$bl) - turn off: Windows Security > Device security > Core isolation > Microsoft Vulnerable Driver Blocklist, reboot (the toggle may need Memory integrity off first)" })

# Other mailbox clients
$xtu = Get-Service -Name 'XtuService' -ErrorAction SilentlyContinue
if ($xtu) {
    Line ($xtu.Status -eq 'Stopped') 'XtuService' "$($xtu.Status) (start type $($xtu.StartType)); stop with: Stop-Service XtuService   (permanent: Set-Service XtuService -StartupType Manual)"
} else { Line $true 'XtuService' 'not installed' }
foreach ($n in 'ThrottleStop', 'OCCT', 'HWiNFO64', 'HWiNFO32', 'XtuUiLauncher', 'PerfTune') {
    $p = Get-Process -Name $n -ErrorAction SilentlyContinue
    Line (-not $p) "process $n" $(if ($p) { 'RUNNING - close it (it may write MSR 0x150; HWiNFO read-only is tolerated but close it for the first apply)' } else { 'not running' })
}

# Python
if (Get-Command py -ErrorAction SilentlyContinue) { Line $true 'Python' ((& py -3 --version 2>&1) -join ' ') }
elseif (Get-Command python -ErrorAction SilentlyContinue) { Line $true 'Python' ((& python --version 2>&1) -join ' ') }
else { Line $false 'Python' 'neither py -3 nor python found' }

# Driver
$svc = Get-Service -Name 'ThrottleStop' -ErrorAction SilentlyContinue
$sys = Join-Path $env:LOCALAPPDATA 'arltuner\ThrottleStop.sys'
Line ([bool]$svc -and $svc.Status -eq 'Running') 'driver service ThrottleStop' $(if ($svc) { "$($svc.Status)" } else { "not installed (carved file present: $(Test-Path $sys))" })
Line (Test-Path $sys) 'carved driver file' $sys

Write-Host ''
Write-Host $(if ($bad -eq 0) { 'preflight: all OK' } else { "preflight: $bad item(s) to fix" })
