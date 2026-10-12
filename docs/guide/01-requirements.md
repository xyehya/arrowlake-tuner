# 01 Requirements

This page lists what the machine needs before you install: CPU, BIOS settings, Linux kernel, Python, the init system and optional tools. It is for anyone
who has not run the tool yet. The app's own **System → Preflight** card checks most of this for you; this page explains each check.

## 1. CPU
Intel Core Ultra 200S desktop (Arrow Lake-S): CPU family 6, model 198, stepping 2 (`platforms/arl-s.toml`). Check:

```
grep -E '^(cpu family|model|stepping)\s*:' /proc/cpuinfo | sort -u
```

Expected: `cpu family : 6`, `model : 198`, `stepping : 2`. A different model makes the preflight check `cpuid` report **read-only**; an unlisted stepping
of model 198 is only a warning. Core counts, which cores are "favored", V/F point ratios and turbo tables are read live from your CPU and sysfs, not
assumed (D-036). The reference chip is a Core Ultra 9 285K (8 P-cores + 16 E-cores, no SMT) [MEASURED win 2026-09-30]; other 200S parts are expected
to work the same way but are not tested here [UNVERIFIED].

## 2. BIOS / firmware settings (once)
The CPU's overclocking mailbox accepts runtime changes only if the firmware allows it. Names vary by board vendor.

| Setting | Value | Why |
|---|---|---|
| **Undervolt Protection (UVP)** | Disabled | With UVP on, the CPU refuses offsets below the fused floor (mailbox return code `0x13 UndervoltProtection`) [DECOMPILED registers.md "Return codes"] |
| **OC Lock** | Disabled | With OC Lock on, every OC-mailbox write is refused (return code `0x01 OCLocked`) [DECOMPILED, registers.md "Return codes"] |
| Overclocking support (some boards) | Enabled | The firmware flag `OcSupport` defaults to off in Intel's reference firmware [DOC FSP FspmUpd.h, registers.md "BIOS/FSP gating flags"] |
| Secure Boot | Disabled, or a kernel with lockdown `none` | Secure Boot normally turns on kernel lockdown, which blocks MSR writes (section 4) |
| Per-core V/F offsets in BIOS | Leave at Auto / 0 | The app writes offsets itself at runtime. A BIOS offset stacks with what the app writes, so the values you read from the CPU would include it |

On the reference machine (ASRock Z890 Steel Legend WiFi, BIOS 3.40) UVP was off, OC Lock off and Secure Boot off [user-reported 2026-09-30, HARDWARE.md]; writes were then accepted [MEASURED win 2026-10-01, lnx 2026-10-01, platform-arl-s.md §1]. These
two switches are the reason runtime writes work there. On other platforms a firmware may accept a write and silently ignore it
[COMMUNITY github tarilka0gg/system-lab, linux-path.md §1], so the tool reads back every write and you should check the read-back, not only the
absence of an error.

## 3. Operating system
| Need | Detail | How the preflight sees it |
|---|---|---|
| Linux on bare metal | A virtual machine does not pass MSR writes to the hardware | `hypervisor`: read-only if the `hypervisor` cpuinfo flag is set |
| systemd (recommended) | `install.sh` installs and enables `arltuner-helper.socket` (socket activation: systemd starts the root helper on the first connection and it exits after about 10 idle minutes) and creates `/run/arltuner` through `tmpfiles.d`. **Any other init system works too**, but then the installer enables nothing: you start the helper yourself with `sudo /usr/bin/python3 -I /usr/lib/arltuner/arltuner-helper --device --socket` ([02](02-install.md)) | `helper_service`: the socket `/run/arltuner/helper.sock` exists, is a root-owned socket, and `/etc/arltuner/helper.toml` admits somebody |
| No polkit, no password prompt | The project uses no polkit and no `pkexec`. The root helper is a service on a Unix socket; who may use it is an allowlist of user names in `/etc/arltuner/helper.toml` (the installer puts the user who ran `sudo bash install.sh` there). Trade-offs: [12 §7](12-safety-and-internals.md) | |
| A desktop session | The GUI is a local web page; a browser must open on the same machine (a desktop polkit agent is **not** needed) | |
| Python 3.12 or newer | Standard library only; nothing to `pip install`. `install.sh` uses `/usr/bin/python3` and refuses if it is older than 3.12 | `python` |
| A current browser | Chromium or Firefox. The GUI is served on a random loopback address (`127.x.y.z`, see [03](03-first-run.md)); it is not reachable from the network | |
| Root (sudo) | Only to run `install.sh` / `uninstall.sh` and the restore script | |

Check: `python3 --version`; `systemctl --version` if you want the systemd units.

The reference machine runs Fedora 44 with kernel 7.2 [user-reported 2026-09-30, HARDWARE.md]. The code does not depend on the distribution
(SPEC decision O-1: any Linux distro), but only Fedora is tested.

## 4. Kernel: the `msr` driver, lockdown, `allow_writes`
The tool reaches the CPU through `/dev/cpu/N/msr`. Every OC-mailbox request, **including a read**, is a write to MSR `0x150`
[DECOMPILED registers.md "OC mailbox"], so all mailbox traffic needs working MSR writes.

| Requirement | Check | If it fails |
|---|---|---|
| `/dev/cpu/N/msr` exists for every CPU | `ls /dev/cpu/*/msr \| wc -l` (24 on the reference chip) | Preflight `msr_device`. If the module exists but is not loaded: `sudo modprobe msr` (an explicit step; the tool never does it). Fedora builds the driver into the kernel (`CONFIG_X86_MSR=y`) [DOC src.fedoraproject.org f44 kernel config, linux-path.md §1] |
| Kernel lockdown is `none` | `cat /sys/kernel/security/lockdown` shows `[none]` | With `[integrity]` or `[confidentiality]` MSR writes are blocked and the app is read-only (Secure Boot is the usual cause) [DOC include/linux/security.h, linux-path.md §1] |
| `msr.allow_writes` is not `off` | `cat /sys/module/msr/parameters/allow_writes` as root (the file is root-only; an unprivileged preflight shows `unknown` and the root helper decides) | `on` or `default` are fine. To set it: boot parameter `msr.allow_writes=on`, or write `on` to that file as root [DOC kernel msr driver, linux-path.md §1] |
| Root access to the msr nodes | The nodes are `root:msr 0660`, but **group membership is not enough**: opening as an ordinary user fails with EPERM | The root helper (a service, [12](12-safety-and-internals.md)) does the access. You do not need to change permissions [MEASURED lnx 2026-10-08, linux-path.md §1] |

Effects you should expect: the first MSR write of a boot sets kernel taint bit 2 (`TAINT_CPU_OUT_OF_SPEC`), and the kernel logs
`msr: Write to unrecognized MSR 0x150 ... tainting CPU_OUT_OF_SPEC`. Both are normal [MEASURED lnx 2026-10-01, linux-path.md §1]. The installer
allows exactly this taint bit (`/etc/arltuner/helper.toml`: `allow_taint_bits = [2]`).

## 5. Optional tools
The GUI works without them; a feature whose tool is missing says `missing: install <package>`.

| Tool | Used for | Package name the app shows when it is missing |
|---|---|---|
| `stress-ng` | Stress tab CPU screens; the command-line `vf-dump --measure` | `stress-ng` |
| `stressapptest` | Stress tab memory screen | `stressapptest` |
| `perf` | DRAM read/write bandwidth on the dashboard | `perf` (Fedora) / `linux-tools` (Debian, Ubuntu) |
| `turbostat` | optional | `kernel-tools` (Fedora) / `linux-tools` (Debian, Ubuntu) |
| `sensors` | optional | `lm_sensors` (Fedora) / `lm-sensors` (Debian, Ubuntu) |
| `nvidia-smi` | optional GPU reading | the NVIDIA driver utilities |

The GUI's **Curves → Measure** does not need `stress-ng`: the helper loads the unit with its own pinned busy loops (`src/arltuner/helper/ops_measure.py`).

Sources: package hints in `src/arltuner/platform/preflight.py` (`OPTIONAL_TOOLS`); the Fedora one-liner is in the repository README:

```
sudo dnf install stress-ng stressapptest kernel-tools perf lm_sensors
```

The helper runs stress tools only from `/usr/bin/stress-ng` and `/usr/bin/stressapptest`, and only as your own user, never as root
(`docs/ARCHITECTURE.md`, G-116). A tool installed elsewhere (for example under `/usr/local/bin`) is not used for the Stress tab.

OCCT, Linpack, y-cruncher and similar are your own choice for long acceptance runs; the project's gate uses them (see [08](08-stability-testing.md)).
Run them as an ordinary user, never as root: OCCT's Linux build writes MSR `0x150` at start when it has raw I/O privilege, which makes it a second
mailbox client [MEASURED lnx 2026-10-02, linux-path.md §1].

## 6. Other software that must not run
Do not run another tool that talks to the same mailbox while you tune: HWiNFO, A-Tuning and similar, or a root OCCT. The mailbox has one owner at a time
(SAFETY.md "Mailbox-session discipline").

## 7. Check list
- [ ] `grep` above shows family 6, model 198
- [ ] UVP and OC Lock disabled in BIOS; Secure Boot off or lockdown `none`
- [ ] bare metal, Python 3.12 or newer, systemd (or you will start the helper by hand)
- [ ] `/dev/cpu/*/msr` present; `allow_writes` not `off`
- [ ] optional packages installed for the features you want
- [ ] you know how to cold-boot (power off, wait, power on) and, ideally, how to clear CMOS on your board

Next: [02 Install](02-install.md) · Up: [guide index](README.md)
