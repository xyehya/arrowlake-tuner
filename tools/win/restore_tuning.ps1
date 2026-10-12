<#
  restore_tuning.ps1 - after any Windows boot: bring the CPU back to YOUR accepted tuning and prove it by read-back.
    powershell -ExecutionPolicy Bypass -File tools\win\restore_tuning.ps1                # operator: type CONFIRM once (or double-click tools\win\Restore-Tuning.cmd)
    powershell -ExecutionPolicy Bypass -File tools\win\restore_tuning.ps1 -Orchestrated  # orchestrator session only, after the operator's go-ahead in chat
    -From DIR     the accepted state: CURRENT, EXPECTED-words.txt, profiles\*.toml (default %LOCALAPPDATA%\arltuner\accepted). The app writes it on Linux
                  (Profiles > Set as accepted, /etc/arltuner/accepted); copy that directory here. Nothing in this repo is one (D-036): absent = stop.
    -Policy FILE  the policy for the guarded writes (default DIR\policy.toml; copy /etc/arltuner/policy.toml next to the accepted state, or name another).
  DIR\fingerprint.json (written by 'Set as accepted' on Linux) tells this machine which cores are which: the Linux CPU -> Windows logical-processor map is derived at
  run time from Windows' own data and settled by guarded reads of the fused V/F point of every P-core. A state accepted before that existed has none: stop, re-accept on Linux.
  Steps: checks (admin, XtuService stopped, no OCCT/XTU) -> one CONFIRM -> sc start ThrottleStop -> [-Before: vf-dump] -> apply each CURRENT profile ->
  vf-dump -> compare every V/F word with DIR\EXPECTED-words.txt -> sc stop ThrottleStop. Stops at the first failure; nothing is retried. Revert = cold boot.
  Works from stock (cold boot) and from a warm reboot (offsets kept on CPUs 1-7/E, CPU 0 reset by the BIOS): the planner steps from what it reads.
  Captures: captures\win\restore-<ts>-*.txt. Audit: %LOCALAPPDATA%\linuxmon\audit.jsonl.
  One confirmation per run (operator 2026-10-05, D-020): the operator types CONFIRM once; the per-command guard tokens are then answered by
  tools\lnx\orchestrator_confirm.py, as with -Orchestrated. All guard checks (identity, policy, read-back, word compare) are unchanged.
#>
# -Before: also dump the state before applying (diagnostic: warm vs cold boot; costs ~132 of the 600 ops/boot, so default off).
param([switch]$Orchestrated, [switch]$Before, [string]$From = (Join-Path $env:LOCALAPPDATA 'arltuner\accepted'), [string]$Policy = '')
$ErrorActionPreference = 'Stop'
$repo = Resolve-Path (Join-Path $PSScriptRoot '..\..')
Set-Location $repo
$lm = Join-Path $env:LOCALAPPDATA 'linuxmon'
New-Item -ItemType Directory -Force -Path $lm, 'captures\win' | Out-Null
$audit = Join-Path $lm 'audit.jsonl'
$ts = Get-Date -Format 'yyyyMMddTHHmmss'

# The accepted words come from DIR\EXPECTED-words.txt, one line per core / E-cluster: "<cpu> <OC word P2=0> <point 1> ... <point n>" (hex, no 0x).
# String keys: an [ordered] dictionary indexed with an int is positional.
function Fail([string]$msg) { Write-Host "STOP: $msg" -ForegroundColor Red; exit 1 }

# --- checks
$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { Fail 'not elevated' }
$xtu = Get-Service XtuService -ErrorAction SilentlyContinue
if ($xtu -and $xtu.Status -ne 'Stopped') { Fail 'XtuService is running (Stop-Service XtuService)' }
$busy = Get-Process OCCT, XtuUiLauncher, PerfTune, ThrottleStop -ErrorAction SilentlyContinue
if ($busy) { Fail "close first: $(($busy.Name | Sort-Object -Unique) -join ', ')" }

# --- the user's accepted state (D-036): absent = stop
$wordsFile = Join-Path $From 'EXPECTED-words.txt'
if (-not (Test-Path (Join-Path $From 'CURRENT')) -or -not (Test-Path $wordsFile)) {
    Fail "no accepted state in $From. Save a profile as accepted in the app (Linux: Profiles > Set as accepted) and copy /etc/arltuner/accepted there, or pass -From DIR"
}
$fingerprint = Join-Path $From 'fingerprint.json'
if (-not (Test-Path $fingerprint)) { Fail "no fingerprint.json in $From. Re-accept on Linux to record the fingerprint (Tune > Read, then Profiles > Set as accepted) and copy /etc/arltuner/accepted here, or pass -From DIR" }
$policy = if ($Policy) { $Policy } else { Join-Path $From 'policy.toml' }
if (-not (Test-Path $policy)) { Fail "policy $policy not found: copy /etc/arltuner/policy.toml next to the accepted state or pass -Policy FILE" }
$profiles = @()
foreach ($l in Get-Content (Join-Path $From 'CURRENT')) {   # a legacy 'policy =' line is ignored: the policy is -Policy / DIR\policy.toml
    if ($l -match '^\s*profile\s*=\s*"?([^"#]+?)"?\s*$') {
        $rel = $Matches[1]
        if ($rel -notmatch '^profiles/[A-Za-z0-9._-]+\.toml$' -or $rel.Contains('..') -or -not (Test-Path (Join-Path $From ($rel -replace '/', '\')))) { Fail "CURRENT names an unusable profile '$rel'" }
        $profiles += (Join-Path $From ($rel -replace '/', '\'))
    }
}
if ($profiles.Count -eq 0) { Fail "$From\CURRENT lists no profile" }
$Expected = [ordered]@{}
foreach ($l in Get-Content $wordsFile) {
    if ($l -match '^\s*(#|$)') { continue }
    $parts = $l.Trim() -split '\s+'
    $Expected[$parts[0]] = (($parts[1..($parts.Count - 1)] | ForEach-Object { [Convert]::ToUInt32($_, 16).ToString('X') }) -join ' ')
}
if ($Expected.Count -eq 0) { Fail "$wordsFile holds no words" }

if (Get-Command py -ErrorAction SilentlyContinue) { $py = @('py', '-3') } else { $py = @('python') }
$env:ARLTUNER_ALLOW_MAILBOX = '1'
$env:PYTHONUNBUFFERED = '1'

# --- one confirmation for the whole run (operator mode); -Orchestrated = the orchestrator already has the operator's go-ahead in chat
if (-not $Orchestrated) {
    Write-Host "Restore the accepted tuning: $(($profiles | ForEach-Object { Split-Path $_ -Leaf }) -join ' -> ')  (from $From, policy $policy)"
    Write-Host 'Writes V/F point offsets via the ThrottleStop driver; every write is read back; revert = cold boot.'
    if ((Read-Host 'Type CONFIRM to proceed') -cne 'CONFIRM') { Fail 'not confirmed - nothing was sent' }
}

# One arltuner command; the confirm wrapper answers its guard token (the run was confirmed above / in chat). Full output: the capture file.
function Run-Lm([string]$tag, [string[]]$LmArgs) {
    $f = "captures\win\restore-$ts-$tag.txt"
    $cmd = @($py[1..9] | Where-Object { $_ }) + @('tools\arltuner.py') + $LmArgs + @('--device-win', '--fingerprint', $fingerprint, '--policy', $policy, '--audit', $audit)
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
        Run-Lm ("apply-" + [IO.Path]::GetFileNameWithoutExtension($prof)) @('apply', $prof)
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
