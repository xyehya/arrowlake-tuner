# mk.py K -> e47-K.toml: good-p53p55-fav-e46m100 + P0 OC 58x at its fused 57x voltage (1426 mV) + P turbo 58,58,55x6
#          + E OC point 47x: EC0 1057+20K mV, EC1-3 1037+20K mV + E turbo 47x8.  (operator 2026-10-20)
import sys, re
from pathlib import Path
k = int(sys.argv[1]); here = Path(__file__).parent
v0, v1 = 1057 + 20 * k, 1037 + 20 * k
s = (here.parent / "good-p53p55-fav-e46m100.toml").read_text()
s = re.sub(r'name = "good-p53p55-fav-e46m100"', f'name = "e47-{k}"', s)
s = re.sub(r'^p = \[57, 57, 55.*$', 'p = [58, 58, 55, 55, 55, 55, 55, 55]  # P0 58x OC at 1-2 active cores, all-core 55x', s, flags=re.M)
s = re.sub(r'^e = \[46.*$', 'e = [47, 47, 47, 47, 47, 47, 47, 47]  # E 47x via the OC point', s, flags=re.M)
head = f"# e47-{k}: E 47x OC  EC0 {v0} mV / EC1-3 {v1} mV; P0 OC 58x at 1426 mV (fused 57x V); turbo P 58,58,55x6 E 47x8; rest = good-p53p55-fav-e46m100\n"
cores = '[[core]]\nkind = "P"\nindex = 0\nratio = 58\noc_voltage_mv = 1426\n\n' + "".join(
    f'[[core]]\nkind = "E"\nindex = {i}\nratio = 47\noc_voltage_mv = {v0 if i == 0 else v1}\n\n' for i in (0, 4, 8, 12))
s = s.replace("[[point]]", cores + "[[point]]", 1)
(here.parent / "e-search" / f"e47-{k}.toml").write_text(head + s)
print(f"e47-{k}: EC0 {v0} EC1-3 {v1}")
