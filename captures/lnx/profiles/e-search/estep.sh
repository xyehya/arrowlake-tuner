#!/bin/bash
# estep.sh PROFILE_NAME [BUILDS=3] — one E-search step: guarded apply of captures/lnx/profiles/e-search/PROFILE_NAME.toml through the installed
# CLI, a 6 s all-E load sample (ratio/VID per cluster), then BUILDS all-core compile builds with auto-stop at the first ICE / new MCE.
# Prints one RESULT line. Orchestrator-only (hardware writes); run only with the operator's go for the sitting.
set -u
NAME="$1"; BUILDS="${2:-3}"; S=/home/yk/.hermes/cache/scratch; CAP=/home/yk/AI/arrowlake-tuner/captures/lnx; P=$CAP/profiles/e-search/$NAME.toml
TAG="es-$NAME"; LOG=$CAP/estep-$NAME-$(date +%Y%m%dT%H%M%S).txt
[ -f "$P" ] || { echo "no profile $P"; exit 2; }
if pgrep -x ninja-build >/dev/null || pgrep -f "compile_stress_al[l]24" >/dev/null; then echo "a build is already running"; exit 2; fi
sudo install -m 0644 -o root "$P" "/root/$NAME.toml"
$S/apply2.sh "/root/$NAME.toml" > "$LOG" 2>&1
grep -q "^Result: OK" "$LOG" || { echo "RESULT $NAME APPLY-FAILED: $(grep -E '^Result|refused|Stopped|tripped' "$LOG" | head -3 | tr '\n' ' ') log=$LOG"; exit 1; }
bad=$(sed -n '/^Apply report/,$p' "$LOG" | grep -E "cmd 0x11 write|msr 0x650 write" | grep -vc "read-back equal")
(stress-ng --cpu 16 --taskset 8-23 --timeout 6s >/dev/null 2>&1 &); sleep 4
samp=$(sudo python3 -c "
import os
def rd(c,m):
    f=os.open(f'/dev/cpu/{c}/msr',os.O_RDONLY); v=int.from_bytes(os.pread(f,8,m),'little'); os.close(f); return v
print(' '.join(f'EC{i}:{(rd(c,0x198)>>8)&0xff}x/{((rd(c,0x198)>>32)&0xffff)/8192:.4f}V' for i,c in enumerate((8,12,16,20))))")
sleep 3; echo "applied $NAME (non-verified writes: $bad) load sample: $samp" | tee -a "$LOG"
mkdir -p $CAP/segv-$TAG
sudo perf record -q -e signal:signal_generate --filter 'sig == 11' -a -g --switch-output=60s -o $CAP/segv-$TAG/segv.data -- \
  sudo -u yk env RUNS="$BUILDS" bash ~/.local/share/arltuner-dev/compile_stress_all24.sh "$TAG" > $CAP/segv-$TAG/run.log 2>&1 &
sleep 8; $S/stopwatch.sh "$TAG" > $CAP/segv-$TAG/stopwatch.log 2>&1; wait
sum=$(ls -t $CAP/compile-stress-$TAG-*-summary.txt 2>/dev/null | head -1)
done_n=$(grep -c "^CASE" "$sum" 2>/dev/null); ice=$(grep -o "ice=[0-9]*" "$sum" 2>/dev/null | cut -d= -f2 | paste -sd+ | bc)
cpus=""; for f in $CAP/segv-$TAG/segv.data*; do cpus+=$(sudo perf script -i "$f" -F comm,cpu 2>/dev/null | awk '/cc1plus|cc1 |cicc|ptxas|cudafe/ {gsub(/[\[\]]/,"",$2); printf "%s ", $2}'); done
mce=$(sudo journalctl -k -b --no-pager | grep -ciE 'mce|machine check|hardware error')
echo "RESULT $NAME builds=$done_n/$BUILDS ice=${ice:-0} segv_cpus=[${cpus}] mce=$mce sample=[$samp] stop=$(tail -1 $CAP/segv-$TAG/stopwatch.log | cut -c1-60)" | tee -a "$LOG"
