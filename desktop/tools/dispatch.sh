#!/usr/bin/env bash
# Dispatch one desktop task to a headless Claude Code worker in its own git worktree (host lnx).
# Usage: desktop/tools/dispatch.sh G-202 tauri-scaffold [model] [notes-file]
#   model: sonnet (default, claude-sonnet-5-5) | opus (claude-opus-5-5, read-only reviewer use) ; notes-file: extra round notes appended to the prompt
# Run it as a tracked background job; it blocks until the worker exits. Output: desktop/progress/logs/<ID>.out
set -euo pipefail
ID="$1"; SLUG="$2"; MODEL="${3:-sonnet}"; NOTES="${4:-}"
ROOT="$(git -C "$(dirname "$0")/../.." rev-parse --show-toplevel)"
WT="$ROOT/.claude/worktrees/$ID"; BR="task/$ID-$SLUG"
LOGS="$ROOT/desktop/progress/logs"; mkdir -p "$LOGS"
case "$MODEL" in sonnet) M=claude-sonnet-5-5 ;; opus) M=claude-opus-5-5 ;; *) echo "bad model $MODEL" >&2; exit 2 ;; esac

if [ ! -d "$WT" ]; then
  if git -C "$ROOT" show-ref --verify --quiet "refs/heads/$BR"; then git -C "$ROOT" worktree add -q "$WT" "$BR"
  else git -C "$ROOT" worktree add -q -b "$BR" "$WT" main; fi
fi

# the task's section from TASKS.md: from its bold header to the next bold task header
SECTION="$(awk -v id="**$ID " 'index($0,id)==1{p=1} p&&/^\*\*(G|H)-/&&index($0,id)!=1{exit} p' "$ROOT/desktop/TASKS.md")"
[ -n "$SECTION" ] || { echo "task $ID not found in TASKS.md" >&2; exit 2; }

PROMPT="$LOGS/$ID.prompt"
{
cat <<EOF
You are an implementer worker on the arltuner project (host lnx), task $ID, working ONLY in this git worktree: $WT (branch $BR).
Read AGENTS.md first, then desktop/PLAN.md (the sections your task names), then only the files your task lists.

Hard rules (violating any = task failed):
- NO hardware: never sudo, never touch /dev/cpu, /sys write paths, wrmsr, modprobe, systemctl, the installed helper or /run/arltuner. Simulator, fake sysfs and fixtures only.
- If you run the simulator helper, always give it temp paths: python3 tools/arltuner-helper.py --sim --socket --socket-path \$TMP/h.sock --audit \$TMP/audit.jsonl --lock \$TMP/lock --state-dir \$TMP/state (plus --helper-toml with your own user if needed). Never use the default audit/lock paths.
- Edit only the files your task lists under Owns. Need another file changed? Stop and say so in the report.
- Python stdlib only for Python code (D-012). UI: plain HTML/CSS/JS ES modules, no npm, no CDN, no bundler. Rust: only the crates the task names.
- No downloads besides crates.io fetches done by cargo for the crates named. No new third-party dependencies.
- Tests: python3 tools/run_tests.py must be green before you finish (plus cargo test / node --test where your task uses them).
- Commit on your branch with messages "$ID: <summary>". Do not merge, push or touch main.
- Shared cargo target dir is already exported (CARGO_TARGET_DIR); do not change it.

Finish with a report of at most 300 words: changed files, tests run with counts, acceptance items met/not met, open questions.

Your task (from desktop/TASKS.md):
$SECTION
EOF
[ -n "$NOTES" ] && [ -f "$NOTES" ] && { echo; echo "Round notes from the orchestrator:"; cat "$NOTES"; }
} > "$PROMPT"

WEBDENY='"WebFetch" "WebSearch"'; [ "${ALLOW_WEB:-0}" = 1 ] && WEBDENY='"Bash(curl *)"'
eval "WEBDENY_ARR=($WEBDENY)"
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$HOME/.cache/arltuner-desktop-target}"
cd "$WT"
echo "[$(date -Is)] dispatch $ID model=$M wt=$WT" > "$LOGS/$ID.out"
claude -p --model "$M" --effort high --permission-mode acceptEdits \
  --allowedTools "Read" "Edit" "Write" "Glob" "Grep" "TodoWrite" ${ALLOW_WEB:+"WebFetch" "WebSearch"} \
    "Bash(python3 *)" "Bash(cargo *)" "Bash(node *)" "Bash(git status*)" "Bash(git diff*)" "Bash(git log*)" "Bash(git add*)" "Bash(git commit*)" \
    "Bash(git show*)" "Bash(ls *)" "Bash(mkdir *)" "Bash(wc *)" "Bash(head *)" "Bash(tail *)" "Bash(grep *)" "Bash(find *)" "Bash(chmod *)" \
    "Bash(bash desktop/tools/*)" "Bash(./desktop/tools/*)" "Bash(desktop/tools/*)" "Bash(bash tools/*)" "Bash(mktemp*)" "Bash(rm -rf /tmp/*)" "Bash(rm -rf $HOME/.hermes/cache/scratch/*)" "Bash(sha256sum *)" "Bash(file *)" \
  --disallowedTools "Bash(sudo *)" "Bash(su *)" "Bash(pkexec *)" "Bash(systemctl *)" "Bash(wrmsr *)" "Bash(rdmsr *)" "Bash(modprobe *)" "Bash(git push*)" "Bash(git checkout main*)" "Bash(git merge*)" "${WEBDENY_ARR[@]}" \
  < "$PROMPT" >> "$LOGS/$ID.out" 2>&1 || rc=$?
echo "[$(date -Is)] exit ${rc:-0} $ID" >> "$LOGS/$ID.out"
