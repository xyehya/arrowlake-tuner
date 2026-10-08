<#
  reapply_current.ps1 - Windows twin of tools/lnx/reapply_current.sh: re-apply the profiles listed in profiles/CURRENT after a boot.
    powershell -ExecutionPolicy Bypass -File tools\win\reapply_current.ps1          # interactive: type CONFIRM <token> once per profile
    powershell -ExecutionPolicy Bypass -File tools\win\reapply_current.ps1 -Check   # read-only vf-dump, no writes
  Elevated PowerShell, ThrottleStop driver running (tools\win\ts_driver.py status). Output is unfiltered (the CONFIRM prompt must stay visible).
#>
param([switch]$Check)
$ErrorActionPreference = 'Stop'
$repo = Resolve-Path (Join-Path $PSScriptRoot '..\..')
Set-Location $repo
$lm = Join-Path $env:LOCALAPPDATA 'linuxmon'
New-Item -ItemType Directory -Force -Path $lm, 'captures\win' | Out-Null
$audit = Join-Path $lm 'audit.jsonl'

$policy = $null; $profiles = @()
foreach ($l in Get-Content 'profiles\CURRENT') {
    if ($l -match '^\s*policy\s*=\s*"?([^"#]+?)"?\s*$') { $policy = $Matches[1] }
    elseif ($l -match '^\s*profile\s*=\s*"?([^"#]+?)"?\s*$') { $profiles += $Matches[1] }
}
if (-not $policy -or $profiles.Count -eq 0) { Write-Error 'profiles/CURRENT has no policy/profile lines'; exit 2 }

if (Get-Command py -ErrorAction SilentlyContinue) { $pyExe = 'py'; $pyPre = @('-3') } else { $pyExe = 'python'; $pyPre = @() }
function Run-Lm { param([string[]]$LmArgs) & $pyExe @pyPre 'tools\arltuner.py' @LmArgs }

$env:ARLTUNER_ALLOW_MAILBOX = '1'
$env:PYTHONUNBUFFERED = '1'
if ($Check) {
    Run-Lm @('vf-dump', '--device-win', '--policy', $policy, '--audit', $audit)
    exit $LASTEXITCODE
}
foreach ($prof in $profiles) {
    $name = [IO.Path]::GetFileNameWithoutExtension($prof)
    $ts = Get-Date -Format 'yyyyMMddTHHmmss'
    Write-Host "=== applying $prof (policy $policy) ==="
    # no pipe: a piped python's CONFIRM prompt (no newline) can stay hidden in Windows PowerShell; a transcript records the session instead
    Start-Transcript -Path "captures\win\reapply-$name-$ts.txt" | Out-Null
    Run-Lm @('apply', ($prof -replace '/', '\'), '--device-win', '--policy', $policy, '--audit', $audit)
    $rc = $LASTEXITCODE
    Stop-Transcript | Out-Null
    if ($rc -ne 0) { Write-Error "apply $prof failed (exit $rc); stopping"; exit $rc }
}
Write-Host 'done. Verify with: powershell -File tools\win\reapply_current.ps1 -Check'
