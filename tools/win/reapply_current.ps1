<#
  reapply_current.ps1 - Windows twin of tools/lnx/reapply_current.sh: re-apply the profiles listed in YOUR accepted CURRENT after a boot.
    powershell -ExecutionPolicy Bypass -File tools\win\reapply_current.ps1          # interactive: type CONFIRM <token> once per profile
    powershell -ExecutionPolicy Bypass -File tools\win\reapply_current.ps1 -Check   # read-only vf-dump, no writes
    -From DIR (default %LOCALAPPDATA%\arltuner\accepted) and -Policy FILE (default DIR\policy.toml): see tools\win\restore_tuning.ps1. No accepted state = stop (D-036).
  DIR\fingerprint.json (written by 'Set as accepted' on Linux) is required: the Linux CPU -> Windows LP map is derived from it at run time. None = stop, re-accept on Linux.
  Elevated PowerShell, ThrottleStop driver running (tools\win\ts_driver.py status). Output is unfiltered (the CONFIRM prompt must stay visible).
#>
param([switch]$Check, [string]$From = (Join-Path $env:LOCALAPPDATA 'arltuner\accepted'), [string]$Policy = '')
$ErrorActionPreference = 'Stop'
$repo = Resolve-Path (Join-Path $PSScriptRoot '..\..')
Set-Location $repo
$lm = Join-Path $env:LOCALAPPDATA 'arltuner'
New-Item -ItemType Directory -Force -Path $lm, 'captures\win' | Out-Null
$audit = Join-Path $lm 'audit.jsonl'

if (-not (Test-Path (Join-Path $From 'CURRENT'))) { Write-Error "no accepted state in $From. Save a profile as accepted in the app and copy /etc/arltuner/accepted there, or pass -From DIR"; exit 2 }
$fingerprint = Join-Path $From 'fingerprint.json'
if (-not (Test-Path $fingerprint)) { Write-Error "no fingerprint.json in $From. Re-accept on Linux to record the fingerprint (Tune > Read, then Profiles > Set as accepted) and copy /etc/arltuner/accepted here, or pass -From DIR"; exit 2 }
$policy = if ($Policy) { $Policy } else { Join-Path $From 'policy.toml' }
if (-not (Test-Path $policy)) { Write-Error "policy $policy not found: copy /etc/arltuner/policy.toml next to the accepted state or pass -Policy FILE"; exit 2 }
$profiles = @()
foreach ($l in Get-Content (Join-Path $From 'CURRENT')) {   # a legacy 'policy =' line is ignored
    if ($l -match '^\s*profile\s*=\s*"?([^"#]+?)"?\s*$') {
        $rel = $Matches[1]
        if ($rel -notmatch '^profiles/[A-Za-z0-9._-]+\.toml$' -or $rel.Contains('..') -or -not (Test-Path (Join-Path $From ($rel -replace '/', '\')))) { Write-Error "CURRENT names an unusable profile '$rel'"; exit 2 }
        $profiles += (Join-Path $From ($rel -replace '/', '\'))
    }
}
if ($profiles.Count -eq 0) { Write-Error "$From\CURRENT lists no profile"; exit 2 }

if (Get-Command py -ErrorAction SilentlyContinue) { $pyExe = 'py'; $pyPre = @('-3') } else { $pyExe = 'python'; $pyPre = @() }
function Run-Lm { param([string[]]$LmArgs) & $pyExe @pyPre 'tools\arltuner.py' @LmArgs }

$env:ARLTUNER_ALLOW_MAILBOX = '1'
$env:PYTHONUNBUFFERED = '1'
if ($Check) {
    Run-Lm @('vf-dump', '--device-win', '--fingerprint', $fingerprint, '--policy', $policy, '--audit', $audit)
    exit $LASTEXITCODE
}
foreach ($prof in $profiles) {
    $name = [IO.Path]::GetFileNameWithoutExtension($prof)
    $ts = Get-Date -Format 'yyyyMMddTHHmmss'
    Write-Host "=== applying $prof (policy $policy) ==="
    # no pipe: a piped python's CONFIRM prompt (no newline) can stay hidden in Windows PowerShell; a transcript records the session instead
    Start-Transcript -Path "captures\win\reapply-$name-$ts.txt" | Out-Null
    Run-Lm @('apply', $prof, '--device-win', '--fingerprint', $fingerprint, '--policy', $policy, '--audit', $audit)
    $rc = $LASTEXITCODE
    Stop-Transcript | Out-Null
    if ($rc -ne 0) { Write-Error "apply $prof failed (exit $rc); stopping"; exit $rc }
}
Write-Host 'done. Verify with: powershell -File tools\win\reapply_current.ps1 -Check'
