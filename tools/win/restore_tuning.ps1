<#
  restore_tuning.ps1 - after any Windows boot: bring the CPU back to YOUR accepted tuning and prove it by read-back.
    powershell -ExecutionPolicy Bypass -File tools\win\restore_tuning.ps1                # operator: type CONFIRM once (or double-click tools\win\Restore-Tuning.cmd)
    powershell -ExecutionPolicy Bypass -File tools\win\restore_tuning.ps1 -Orchestrated  # orchestrator session only, after the operator's go-ahead in chat
    -From DIR     the accepted state: CURRENT, EXPECTED-words.txt, profiles\*.toml (default %ProgramData%\arltuner\accepted). The app writes it on Linux
                  (Profiles > Set as accepted, /etc/arltuner/accepted); copy that directory here from an Administrator terminal. Nothing in this repo is one (D-036): absent = stop.
    -Policy FILE  the policy for the guarded writes (default DIR\policy.toml; copy /etc/arltuner/policy.toml next to the accepted state, or name another).
  Admin-only (G-142, D-042): this script runs elevated, so everything that decides what it does - the accepted state, the policy, the driver, the audit log, the
  read-back file the verdict is computed from - must sit in a directory only Administrators/SYSTEM can write (%ProgramData%\arltuner, locked down by
  'py -3 tools\win\ts_driver.py init-dir'). The script refuses a -From / -Policy directory that is not (ts_driver.py check-dir) and verifies the driver's SHA-256 and
  directory again right before 'sc start'. RESTORED is decided from the vf-dump read-back of THIS run (a JSON file in that directory), never from the audit file.
  G-147: the last profile is applied with --neutralize-residual (a residual OC point the accepted state does not have, which only a cold boot clears, is rewritten with a V target below the
  accepted top fused point so the unit runs the accepted voltage) and the verdict is 'arltuner compare-accepted': EQUIVALENT ("residual OC point (cleared by a cold boot); voltages as
  accepted") counts as success, only a real difference is BAD. There is no cpufreq on Windows: scaling_max_freq is a Linux-side step (restore_tuning.sh).
  DIR\fingerprint.json (written by 'Set as accepted' on Linux) tells this machine which cores are which: the Linux CPU -> Windows logical-processor map is derived at
  run time from Windows' own data and settled by guarded reads of the fused V/F point of every P-core. A state accepted before that existed has none: stop, re-accept on Linux.
  Steps: checks (admin, XtuService stopped, no OCCT/XTU, admin-only directories) -> one CONFIRM -> ts_driver.py start (service created for this run) -> [-Before: vf-dump] ->
  apply each CURRENT profile -> vf-dump -> compare every V/F word with DIR\EXPECTED-words.txt -> ts_driver.py remove (stop + delete the service). Stops at the first
  failure; nothing is retried. Revert = cold boot.
  Works from stock (cold boot) and from a warm reboot (offsets kept on CPUs 1-7/E, CPU 0 reset by the BIOS): the planner steps from what it reads.
  Captures: captures\win\restore-<ts>-*.txt (logs for reading, not evidence). Audit: %ProgramData%\arltuner\audit.jsonl.
  Not yet run on Windows with these changes (written on Linux): the first run is the bring-up test; read the ts_driver.py output carefully.
  One confirmation per run (operator 2026-10-05, D-020): the operator types CONFIRM once; the per-command guard tokens are then answered by
  tools\lnx\orchestrator_confirm.py, as with -Orchestrated. All guard checks (identity, policy, read-back, word compare) are unchanged.
#>
# -Before: also dump the state before applying (diagnostic: warm vs cold boot; costs ~132 of the 600 ops/boot, so default off).
param([switch]$Orchestrated, [switch]$Before, [string]$From = (Join-Path $env:ProgramData 'arltuner\accepted'), [string]$Policy = '')
$ErrorActionPreference = 'Stop'
$repo = Resolve-Path (Join-Path $PSScriptRoot '..\..')
Set-Location $repo
$lm = Join-Path $env:ProgramData 'arltuner'   # the admin-only directory (created and locked down by ts_driver.py init-dir)
New-Item -ItemType Directory -Force -Path 'captures\win' | Out-Null
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

# -I: isolated Python (no PYTHON* variables, no user site, no script directory or cwd on sys.path); tools\arltuner.py adds .\src from its own path
if (Get-Command py -ErrorAction SilentlyContinue) { $py = @('py', '-3', '-I') } else { $py = @('python', '-I') }
function Ts-Driver { & $py[0] @($py[1..9] | Where-Object { $_ }) 'tools\win\ts_driver.py' @args }

# --- admin-only directories: create/lock down the data directory, then refuse any input directory a non-admin could write
if (-not (Test-Path $lm)) { Ts-Driver init-dir; if ($LASTEXITCODE -ne 0) { Fail 'could not create the admin-only directory' } }
foreach ($d in @($lm, $From, (Split-Path -Parent (Resolve-Path $policy)))) {
    Ts-Driver check-dir $d | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "$d is not an admin-only directory (ts_driver.py check-dir). Copy the accepted state / policy into $lm from an Administrator terminal, or pass -From / -Policy pointing there" }
}
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
# A service for this run only: created from the SHA-256-verified copy in the admin-only directory, deleted again in the finally block below.
Ts-Driver start
if ($LASTEXITCODE -ne 0) { Fail 'ThrottleStop driver not started (py -3 tools\win\ts_driver.py carve --exe ...ThrottleStop.exe, then re-run)' }
Ts-Driver status
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
        $applyArgs = @('apply', $prof)
        if ($i -eq $profiles.Count) {
            # G-147: the LAST profile also looks for a residual OC point (an OC ratio the accepted state does not have; only a cold boot clears it) and rewrites it with a V target
            # below the accepted top fused point, so the unit runs the accepted voltage; the earlier profiles of the chain say which OC ratios ARE accepted.
            $applyArgs += '--neutralize-residual'
            for ($j = 0; $j -lt $i - 1; $j++) { $applyArgs += @('--accepted-before', $profiles[$j]) }
        }
        Run-Lm ("apply-" + [IO.Path]::GetFileNameWithoutExtension($prof)) $applyArgs
    }
    Write-Host "=== 3/3 read-back"
    # The verdict comes from THIS run's live read-back: vf-dump writes its own JSON (words as read from the CPU) into the admin-only directory - not from the
    # audit file and not from a file in the repo (both could be edited between the read and the compare).
    $readback = Join-Path $lm "readback-$ts.json"
    Remove-Item -LiteralPath $readback -ErrorAction SilentlyContinue
    Run-Lm 'after' @('vf-dump', '--json', $readback)
    if (-not (Test-Path -LiteralPath $readback)) { Fail "vf-dump wrote no read-back file ($readback)" }
    # G-147: the verdict is the CLI's compare-accepted (shared with restore_tuning.sh): identical words are OK; a residual OC point that runs the accepted voltages, or an OC word that
    # is the same state, is EQUIVALENT and counts as success; anything else is BAD. It reads THIS run's read-back file (admin-only directory), EXPECTED-words.txt and the fused words
    # of fingerprint.json (its stdout lines are the per-core list, "RESTORED: ..." or "STOP: ...").
    $cmp = & $py[0] @($py[1..9] | Where-Object { $_ }) 'tools\arltuner.py' 'compare-accepted' '--readback' $readback '--expected' $wordsFile '--fingerprint' $fingerprint
    $cmpRc = $LASTEXITCODE
    $cmp | ForEach-Object { Write-Host "$_" }
    if ($cmpRc -ne 0) { Fail 'the read-back differs from the accepted state (or the compare could not run) - report; cold boot = stock' }
    Write-Host "restore verified against $wordsFile ($($Expected.Count) cores/clusters)." -ForegroundColor Green
}
finally {
    Ts-Driver remove | Out-Null   # stop + delete the per-run service
    Write-Host "driver stopped and its service deleted (offsets stay until a cold boot)."
}
