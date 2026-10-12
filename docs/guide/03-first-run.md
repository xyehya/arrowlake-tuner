# 03 First run

This page walks through starting the GUI for the first time, what the polkit prompt does, how the login link works, and how to read the
System tab's preflight. It is for someone who has just run `install.sh` ([02](02-install.md)).

## 1. Start the GUI
From the checkout, as your normal user (not root):
```
python3 tools/arltuner-gui.py --open
```
What happens:

1. The script starts a small web server on a **random loopback address** `127.x.y.z` and a random port (G-142, D-042). Nothing is reachable from the network, and each run has its own address, so
   the browser's session cookie of one run is not sent to any other local service. Only if that bind fails does it fall back to `127.0.0.1` and print a warning.
2. It starts the root helper with `pkexec /usr/lib/arltuner/arltuner-helper --device`. Your desktop shows the **polkit password prompt**
   ("Authentication is required to run the arrowlake-tuner helper"). Enter your administrator password. This is the only privilege step, and it
   is needed on every start; polkit is configured not to remember the authorisation.
3. It writes a **launcher page** and prints `listening on http://127.x.y.z:PORT` (the address and port are random every run) and `open once: file://.../launcher.html`. With `--open` the
   default browser opens it.
4. On the launcher page press **Open arrowlake-tuner**. The page posts a one-time token to the server and redirects you into the app.

The launcher is `$XDG_RUNTIME_DIR/arltuner/launcher.html` (else `/tmp/arltuner-<uid>/launcher.html`), mode 0600, in a directory only you can enter (0700). The token
never appears in a command line or URL. The link works **once** and expires if unused (900 s in the code, `Config.token_ttl_s`); the launcher file is
deleted after the first use.

If no helper is installed (you only have a checkout), the script starts without a helper: the page loads but every call that needs the helper
answers 503. That mode is for development. `--no-helper` forces it.

### Options of `tools/arltuner-gui.py`
| Option | Meaning |
|---|---|
| `--open` | open the launcher in the default browser |
| `--no-helper` | do not start any helper (API calls answer 503) |
| `--helper CMD ...` | use another helper command, everything after the flag is the command (development, tests) |
| `--launcher PATH` | where to write the launcher page |
| `--web-root DIR` | GUI files; default is `./web` of the checkout if it exists, else `/usr/share/arltuner/web` |

### Lost the link, closed the tab, session expired
Run the same command again while the first one is still running:
```
python3 tools/arltuner-gui.py --open
```
It detects the running server, asks it for a fresh single-use launcher (via a signal) and prints `open once: ...` again, without a second password
prompt or a second server. A browser session expires after 30 minutes idle or 12 hours in total (`Config.session_idle_s`, `session_max_s`); the
same command gets you back in.

### Stop
Press **Ctrl+C** in the terminal that runs the GUI. The helper exits with it. Stopping the GUI changes nothing on the CPU: whatever was applied
stays applied until a cold boot.

## 2. What you see
The header has the app name, a mode tag, the tabs, the refresh slider and two connection pills. The tabs, in the order you use them:

| Tab | What it is for | Guide page |
|---|---|---|
| **Dashboard** | live core map (clock, busy, temperature, VID), package power, board Vcore, DRAM bandwidth, memory card, mailbox operations this boot | this page |
| **Curves** | measure, store and compare V/F curves | [05](05-measuring-curves.md) |
| **Tune** | read the ratios and voltages the CPU holds, change them, review, apply | [06](06-undervolting.md), [07](07-overclocking.md) |
| **Stress** | CPU or memory stress screens | [08](08-stability-testing.md) |
| **Profiles** | save, compare, accept and re-apply tunings | [09](09-profiles-and-restore.md) |
| **System** | helper state, operations this boot, **Tuning limits** (your policy next to what your CPU reports; [12 §4](12-safety-and-internals.md)), preflight | below |
| **Audit** | the audit log of mailbox operations, with a filter (**All** / **Writes & refusals**) and the request named in words | [12](12-safety-and-internals.md) |

Dashboard controls: the **Refresh** slider (0.25, 0.5, 1, 2 or 5 s; changing it restarts the sampler), **Pause**, a chart window of 1, 5 or
15 minutes, and a core-map metric (Clock, Busy, Temp, VID) with a fixed or automatic colour scale. The Memory card shows trained values read from
the memory controller (frequency, gear, command rate, timings, DIMM temperatures); **All timings** opens the full table and **Read again**
re-reads them. A reading the tool cannot get is shown as unavailable with a reason, never as a number (SPEC-v3 §2).

The **VID** shown per core is the voltage the core *requests* (MSR `0x198` bits 47:32 divided by 8192). It is not a measurement of the voltage
delivered to the core; no such telemetry exists on this platform [MEASURED lnx 2026-10-01, platform-285k.md §5].

## 3. Preflight (System tab)
**System → Preflight** lists one line per check, worst first. The header mode tag and a banner summarise it.

| Mode | Meaning |
|---|---|
| `write` | every check passed; tuning is possible |
| `read-only` | at least one check failed that blocks writes; the dashboard and Curves measurement still work, but Tune cannot read or apply (a mailbox read is a write) |
| `refuse` | the app cannot run (Python older than 3.12) |

| Check | Passes when | Typical fix |
|---|---|---|
| `cpuid` | family 6, model 198 and a known stepping on every CPU | none: another CPU is not supported |
| `hypervisor` | no `hypervisor` flag in `/proc/cpuinfo` | run on bare metal |
| `lockdown` | `/sys/kernel/security/lockdown` is `[none]` (or absent) | disable Secure Boot or boot a kernel without lockdown |
| `topology` | P and E CPU sets read from sysfs, E clusters of equal size | none: the CPU is not an Arrow Lake-S layout |
| `msr_device` | `/dev/cpu/N/msr` for every CPU | `sudo modprobe msr` |
| `msr_allow_writes` | not `off` (shows `unknown` for an unprivileged read; the root helper's own preflight decides) | boot with `msr.allow_writes=on` |
| `pkexec`, `polkit`, `polkit_agent` | installed, and an authentication agent runs in the session | install polkit; log in to a desktop session |
| `python` | 3.12 or newer | update Python |
| `tool:*` | optional tools found; `missing: install <pkg>` otherwise (never changes the mode) | install the package ([01](01-requirements.md)) |

The unprivileged preflight shown by the page is for display. The root helper re-runs the checks before every write and refuses writes unless the
mode is `write` and no check is `unknown` (D-027).

## 4. A safe first look (no writes)
Before any tuning, in this order:

1. **Dashboard**: confirm all cores show clocks and VIDs, the core map matches your CPU (P-cores and E-cores, favored P-cores marked with a star),
   and temperatures look sane at idle.
2. **System**: preflight mode should say `write`. Fix anything listed above first.
3. **Curves → Measure** with **Quick**: [05](05-measuring-curves.md). It writes only cpufreq limits (restored afterwards) and sends no mailbox traffic.
4. Read [04 Arrow Lake V/F primer](04-arrow-lake-vf-primer.md) before pressing anything in **Tune**.

The Tune page's **Read ratios & voltages** is a series of mailbox reads, and each one is a WRMSR; it is allowed in `write` mode only. See
[06](06-undervolting.md).

## 5. Troubleshooting the start
| Symptom | Likely cause |
|---|---|
| No password prompt, "helper: n/a" | the helper is not installed (checkout only) or `--no-helper` was used; install ([02](02-install.md)) |
| Prompt never appears, helper does not start | no polkit authentication agent in the session (`polkit_agent` check); run from a normal desktop login, not a bare SSH session |
| `arltuner-gui: ...: owned by uid ... / lets others in` | the launcher directory is not one only you can access; fix its owner/mode (0700) or pass `--launcher` |
| Browser shows the launcher but the button does nothing | the one-time token expired or was used; run the command again for a fresh link |
| Banner "Read-only mode" | see section 3; the banner lists the failing checks and their reasons |

More: [11 Troubleshooting and FAQ](11-troubleshooting-faq.md).

Next: [04 Arrow Lake V/F primer](04-arrow-lake-vf-primer.md) · Previous: [02 Install](02-install.md)
