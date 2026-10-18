"""scratch helper (not committed): splice.py FILE START_PREFIX END_PREFIX|EOF NEWFILE -- replace lines [START, END) of FILE with NEWFILE."""
import sys

path, start, end, new = sys.argv[1:5]
lines = open(path, encoding="utf-8").read().split("\n")
si = next(i for i, l in enumerate(lines) if l.startswith(start))
ei = len(lines) if end == "EOF" else next(i for i, l in enumerate(lines) if i > si and l.startswith(end))
block = open(new, encoding="utf-8").read().rstrip("\n").split("\n")
tail = [] if end == "EOF" else lines[ei:]
gap = ["", ""] if tail and tail[0].startswith(("def ", "class ", "@", "#:")) else [""]
out = lines[:si] + block + (gap if tail and block[-1] != "" else []) + tail
if end == "EOF":
    out = lines[:si] + block + [""]
open(path, "w", encoding="utf-8").write("\n".join(out))
print(f"replaced lines {si + 1}..{ei} ({ei - si} lines) with {len(block)} lines")
