<#
  restore_tuning.ps1 - after any Windows boot: bring the CPU back to the accepted tuning (profiles/CURRENT) and prove it by read-back.
    powershell -ExecutionPolicy Bypass -File tools\win\restore_tuning.ps1                # operator: type CONFIRM once (or double-click tools\win\Restore-Tuning.cmd)
    powershell -ExecutionPolicy Bypass -File tools\win\restore_tuning.ps1 -Orchestrated  # orchestrator session only, after the operator's go-ahead in chat
  Steps: checks (admin, XtuService stopped, no OCCT/XTU) -> one CONFIRM -> sc start ThrottleStop -> [-Before: vf-dump] -> apply each CURRENT profile ->
  vf-dump -> compare every V/F word with $Expected -> sc stop ThrottleStop. Stops at the first failure; nothing is retried. Revert = cold boot.
  Works from stock (cold boot) and from a warm reboot (offsets kept on CPUs 1-7/E, CPU 0 reset by the BIOS): the planner steps from what it reads.
  Captures: captures\win\restore-<ts>-*.txt. Audit: %LOCALAPPDATA%\linuxmon\audit.jsonl.
  One confirmation per run (operator 2026-10-05, D-020): the operator types CONFIRM once; the per-command guard tokens are then answered by
  tools\lnx\orchestrator_confirm.py, as with -Orchestrated. All guard checks (identity, policy, read-back, word compare) are unchanged.
#>
# -Before: also dump the state before applying (diagnostic: warm vs cold boot; costs ~132 of the 600 ops/boot, so default off).
param([switch]$Orchestrated, [switch]$Before)
$ErrorActionPreference = 'Stop'
$repo = Resolve-Path (Join-Path $PSScriptRoot '..\..')
Set-Location $repo
$lm = Join-Path $env:LOCALAPPDATA 'linuxmon'
New-Item -ItemType Directory -Force -Path $lm, 'captures\win' | Out-Null
$audit = Join-Path $lm 'audit.jsonl'
$ts = Get-Date -Format 'yyyyMMddTHHmmss'

# Accepted words: p-step7 + E split3 (2026-10-04, BIOS LLC 6; captures/win/apply-p-step7-20261004T180158.txt read-back) (string keys: an
# [ordered] dictionary indexed with an int is positional) (P2 0 = OC word, then points 1..n). = profiles/EXPECTED-words.txt. Update together with profiles/CURRENT.
$Expected = [ordered]@{
    '0'  = '39 8 14 1E 30 F9A00035 FC600037 FCA00038 FA800039'
    '1'  = '0 8 14 1E 30 F9A00035 FB600037 38 39'
    '2'  = '0 8 14 1E 30 F9A00035 F5A00037 38 39'
    '3'  = '0 8 14 1E 30 F9A00035 FC600037 FC600038 FC000039'
    '4'  = '0 8 14 1E 30 F9A00035 FBC00037 38 39'
    '5'  = '0 8 14 1E 30 F9A00035 F6A00037 38 39'
    '6'  = '0 8 14 1E 30 F9A00035 F8400037 38 39'
    '7'  = '0 8 14 1E 30 F9A00035 F4600037 38 39'
    '8'  = '0 8 F 17 1E 28 FB80002E'
    '12' = '0 8 F 17 1E 28 FCC0002E'
    '16' = '0 8 F 17 1E 28 FCC0002E'
    '20' = '0 8 F 17 1E 28 FCC0002E'
}

function Fail([string]$msg) { Write-Host "STOP: $msg" -ForegroundColor Red; exit 1 }

# --- checks
$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { Fail 'not elevated' }
$xtu = Get-Service XtuService -ErrorAction SilentlyContinue
if ($xtu -and $xtu.Status -ne 'Stopped') { Fail 'XtuService is running (Stop-Service XtuService)' }
$busy = Get-Process OCCT, XtuUiLauncher, PerfTune, ThrottleStop -ErrorAction SilentlyContinue
if ($busy) { Fail "close first: $(($busy.Name | Sort-Object -Unique) -join ', ')" }

$policy = $null; $profiles = @()
foreach ($l in Get-Content 'profiles\CURRENT') {
    if ($l -match '^\s*policy\s*=\s*"?([^"#]+?)"?\s*$') { $policy = $Matches[1] }
    elseif ($l -match '^\s*profile\s*=\s*"?([^"#]+?)"?\s*$') { $profiles += $Matches[1] }
}
if (-not $policy -or $profiles.Count -eq 0) { Fail 'profiles/CURRENT has no policy/profile lines' }

if (Get-Command py -ErrorAction SilentlyContinue) { $py = @('py', '-3') } else { $py = @('python') }
$env:ARLTUNER_ALLOW_MAILBOX = '1'
$env:PYTHONUNBUFFERED = '1'

# --- one confirmation for the whole run (operator mode); -Orchestrated = the orchestrator already has the operator's go-ahead in chat
if (-not $Orchestrated) {
    Write-Host "Restore the accepted tuning: $($profiles -join ' -> ')  (policy $policy)"
    Write-Host 'Writes V/F point offsets via the ThrottleStop driver; every write is read back; revert = cold boot.'
    if ((Read-Host 'Type CONFIRM to proceed') -cne 'CONFIRM') { Fail 'not confirmed - nothing was sent' }
}

# One arltuner command; the confirm wrapper answers its guard token (the run was confirmed above / in chat). Full output: the capture file.
function Run-Lm([string]$tag, [string[]]$LmArgs) {
    $f = "captures\win\restore-$ts-$tag.txt"
    $cmd = @($py[1..9] | Where-Object { $_ }) + @('tools\arltuner.py') + $LmArgs + @('--device-win', '--policy', $policy, '--audit', $audit)
    & $py[0] @($py[1..9] | Where-Object { $_ }) 'tools\lnx\orchestrator_confirm.py' $py[0] @cmd *> $f
    $rc = $LASTEXITCODE
    Get-Content $f | Select-String 'identity check passed|Result:|refus|REFUS|mismatch|WHEA' | ForEach-Object { '  ' + $_.Line.Trim() }
    if ($rc -ne 0) { Fail "$tag failed (exit $rc), see $f" }
}

# --- driver
$svc = Get-Service ThrottleStop -ErrorAction SilentlyContinue
if (-not $svc) { Fail 'ThrottleStop driver not installed (py -3 tools\win\ts_driver.py install)' }
if ($svc.Status -ne 'Running') { sc.exe start ThrottleStop | Out-Null }
& $py[0] @($py[1..9] | Where-Object { $_ }) 'tools\win\ts_driver.py' 'status'
if ($LASTEXITCODE -ne 0) { Fail 'driver status not OK' }

try {
    if ($Before) {
        Write-Host "=== 1/3 read-only check (before)"
        Run-Lm 'before' @('vf-dump')
    }
    $i = 0
    foreach ($prof in $profiles) {
        $i++
        Write-Host "=== 2/3 apply $prof ($i/$($profiles.Count))"
        Run-Lm ("apply-" + [IO.Path]::GetFileNameWithoutExtension($prof)) @('apply', ($prof -replace '/', '\'))
    }
    Write-Host "=== 3/3 read-back"
    $t0 = (Get-Date).ToUniversalTime()
    Run-Lm 'after' @('vf-dump')

    # last read of each (cpu, point) in this read-back session
    $last = @{}
    foreach ($line in Get-Content $audit) {
        $e = $line | ConvertFrom-Json
        if ($e.event -ne 'result' -or $e.cmd -ne '0x10' -or $e.rc_name -ne 'Pass') { continue }
        if (([datetime]$e.ts).ToUniversalTime() -lt $t0) { continue }
        $last["$($e.cpu)/$([Convert]::ToInt32($e.p2, 16))"] = [Convert]::ToUInt32($e.data, 16).ToString('X')
    }
    $bad = 0
    foreach ($c in $Expected.Keys) {
        $got = (0..10 | Where-Object { $last.ContainsKey("$c/$_") } | ForEach-Object { $last["$c/$_"] }) -join ' '
        $ok = $got -eq $Expected["$c"]
        if (-not $ok) { $bad++ }
        Write-Host ("  cpu{0,-2} {1} {2}" -f $c, $(if ($ok) { 'OK ' } else { 'BAD' }), $got)
        if (-not $ok) { Write-Host "         want $($Expected["$c"])" }
    }
    if ($bad) { Fail "$bad core(s)/cluster(s) differ from the accepted state - report; cold boot = stock" }
    Write-Host "RESTORED: all $($Expected.Count) cores/clusters match the accepted state." -ForegroundColor Green
}
finally {
    sc.exe stop ThrottleStop | Out-Null
    Write-Host "driver stopped (offsets stay until a cold boot)."
}
