"""G-138: ``fingerprint.json`` of the accepted state: what the Windows restore needs to find the same cores again without any constant of a particular chip.

Linux CPU numbers mean nothing on Windows and Windows cannot be asked for them, so ``profiles.accept`` (Linux, root helper) records, next to
``EXPECTED-words.txt``, for **every Linux CPU** its kind, XTU index, E-cluster, favored flag and the fused V/F point word (cmd ``0x09 P1=0x11 P2=0``,
the *identity word*: distinct per physical P-core, shared inside an E cluster), plus the CPU's vendor / family / model / brand string::

    {"schema": 1, "recorded": "2026-10-12T10:00:00Z",
     "cpu": {"vendor": "GenuineIntel", "family": 6, "model": 198, "brand": "..."},
     "cpus": [{"cpu": 0, "kind": "P", "index": 0, "cluster": null, "favored": true, "fused_word": "0x5B439"}, ...]}

The Windows side (``msr.winport`` and the CLI's identity check) derives its Linux-CPU -> logical-processor map at run time and holds it against
these words. Pure Python, standard library only, no hardware access.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from arltuner.topology import KIND_E, KIND_P, KINDS, Core, Topology

__all__ = [
    "FINGERPRINT_FILE",
    "SCHEMA",
    "CpuId",
    "Fingerprint",
    "FingerprintCpu",
    "FingerprintError",
    "REACCEPT_HINT",
    "build_fingerprint",
    "cpu_id_from_cpuinfo",
    "load_fingerprint",
    "parse_fingerprint",
]

FINGERPRINT_FILE = "fingerprint.json"
SCHEMA = 1
REACCEPT_HINT = "re-accept on Linux to record the fingerprint (Tune > Read, then Profiles > Set as accepted), then copy the accepted directory here"
_MAX_BYTES = 256 * 1024


class FingerprintError(Exception):
    """The fingerprint is missing, malformed or not enough for the Windows restore; the text says what to do."""


def _norm(text: str) -> str:
    return " ".join(text.split())


@dataclass(frozen=True)
class CpuId:
    """Vendor string, CPUID family / model and the brand string of the part the state was accepted on."""

    vendor: str
    family: int
    model: int
    brand: str

    def mismatch(self, other: CpuId) -> str | None:
        """``None`` when ``other`` is the same part (whitespace-insensitive brand compare), else a short text."""
        for name, mine, theirs in (("vendor", _norm(self.vendor), _norm(other.vendor)), ("family", self.family, other.family), ("model", self.model, other.model), ("brand", _norm(self.brand), _norm(other.brand))):
            if mine != theirs:
                return f"{name} is {theirs!r}, the accepted state was recorded on {mine!r}"
        return None


@dataclass(frozen=True)
class FingerprintCpu:
    cpu: int
    kind: str
    index: int
    cluster: int | None
    favored: bool
    word: int | None  # the 0x09 P1=0x11 P2=0 word; None = not recorded


@dataclass(frozen=True)
class Fingerprint:
    cpu_id: CpuId | None
    cpus: tuple[FingerprintCpu, ...]

    def topology(self) -> Topology:
        """The Linux topology of the machine the state was accepted on (the Windows run has no other source for it)."""
        cores = tuple(Core(c.kind, c.index, c.cpu, c.cluster, c.favored) for c in self.cpus)
        try:
            return Topology(cores, favored_source="fingerprint" if any(c.favored for c in cores) else None)
        except ValueError as exc:
            raise FingerprintError(f"{FINGERPRINT_FILE} describes an impossible CPU layout: {exc}") from None

    def word_of(self, cpu: int) -> int | None:
        for c in self.cpus:
            if c.cpu == cpu:
                return c.word
        return None

    def require_restorable(self) -> Topology:
        """The topology, after checking that the fingerprint carries what the restore needs: CPU identity, a word for every P-core and E cluster,
        P words distinct (otherwise the P-cores cannot be told apart), and the cluster words consistent. ``FingerprintError`` otherwise."""
        if self.cpu_id is None:
            raise FingerprintError(f"{FINGERPRINT_FILE} records no CPU identity (vendor / family / model / brand): {REACCEPT_HINT}")
        topo = self.topology()
        if not topo.cpus_of_kind(KIND_P) or not topo.cpus_of_kind(KIND_E):
            raise FingerprintError(f"{FINGERPRINT_FILE} must list both P and E CPUs")
        by_cpu = {c.cpu: c for c in self.cpus}
        p_words: dict[int, int] = {}
        for cpu in topo.cpus_of_kind(KIND_P):
            word = by_cpu[cpu].word
            if word is None:
                raise FingerprintError(f"{FINGERPRINT_FILE} has no fused word for P-core CPU {cpu} (the state was accepted without a full read): read the whole system on Linux and {REACCEPT_HINT}")
            if word in p_words:
                raise FingerprintError(
                    f"P-cores CPU {p_words[word]} and CPU {cpu} have the same fused word {word:#x}: the P-cores cannot be told apart on Windows, so the restore is refused"
                )
            p_words[word] = cpu
        for cluster in sorted({c.cluster for c in self.cpus if c.cluster is not None}):
            words = {by_cpu[cpu].word for cpu in topo.cluster_cpus(cluster)}
            if None in words or len(words) != 1:
                raise FingerprintError(f"{FINGERPRINT_FILE} has no single fused word for E cluster {cluster}: read the whole system on Linux and {REACCEPT_HINT}")
        return topo

    def to_json(self, recorded: str) -> str:
        doc = {
            "schema": SCHEMA,
            "recorded": recorded,
            "cpu": None if self.cpu_id is None else {"vendor": self.cpu_id.vendor, "family": self.cpu_id.family, "model": self.cpu_id.model, "brand": self.cpu_id.brand},
            "cpus": [
                {"cpu": c.cpu, "kind": c.kind, "index": c.index, "cluster": c.cluster, "favored": c.favored, "fused_word": None if c.word is None else f"{c.word:#X}".replace("0X", "0x")}
                for c in self.cpus
            ],
        }
        return json.dumps(doc, indent=1) + "\n"


def cpu_id_from_cpuinfo(text: str) -> CpuId | None:
    """Vendor / family / model / brand of the first processor entry of ``/proc/cpuinfo`` text; ``None`` when any of them is missing."""
    block: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if block:
                break
            continue
        key, sep, value = line.partition(":")
        if sep:
            block.setdefault(key.strip(), value.strip())
    try:
        vendor, brand = block["vendor_id"], block["model name"]
        family, model = int(block["cpu family"]), int(block["model"])
    except (KeyError, ValueError):
        return None
    return CpuId(vendor, family, model, _norm(brand)) if vendor and brand else None


def build_fingerprint(topology: Topology, fused_words: Mapping[int, int] | None, cpu_id: CpuId | None) -> Fingerprint:
    """The fingerprint of the running system: every CPU of ``topology`` with the live ``fused_words`` (cpu -> word, as ``helper.fused_words`` keeps them)."""
    words = fused_words or {}
    return Fingerprint(cpu_id, tuple(FingerprintCpu(c.cpu, c.kind, c.index, c.cluster, c.favored, words.get(c.cpu)) for c in sorted(topology.cores, key=lambda c: c.cpu)))


def _int(value: object, what: str, *, lo: int = 0, hi: int = 1 << 32) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value < hi:
        raise FingerprintError(f"{FINGERPRINT_FILE}: {what} must be an integer in {lo}..{hi - 1}, got {value!r}")
    return value


def parse_fingerprint(text: str) -> Fingerprint:
    """Parse and structurally validate the file text (``require_restorable`` judges completeness). ``FingerprintError`` on anything unexpected."""
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise FingerprintError(f"{FINGERPRINT_FILE} is not valid JSON ({exc}): {REACCEPT_HINT}") from None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise FingerprintError(f"{FINGERPRINT_FILE} has schema {doc.get('schema') if isinstance(doc, dict) else None!r}, this version reads schema {SCHEMA}: {REACCEPT_HINT}")
    raw_cpu = doc.get("cpu")
    cpu_id: CpuId | None = None
    if raw_cpu is not None:
        if not isinstance(raw_cpu, dict) or not isinstance(raw_cpu.get("vendor"), str) or not isinstance(raw_cpu.get("brand"), str):
            raise FingerprintError(f"{FINGERPRINT_FILE}: 'cpu' must have string 'vendor' and 'brand'")
        cpu_id = CpuId(raw_cpu["vendor"], _int(raw_cpu.get("family"), "cpu.family"), _int(raw_cpu.get("model"), "cpu.model"), _norm(raw_cpu["brand"]))
        if not cpu_id.vendor or not cpu_id.brand:
            cpu_id = None
    rows = doc.get("cpus")
    if not isinstance(rows, list) or not rows:
        raise FingerprintError(f"{FINGERPRINT_FILE}: 'cpus' must be a non-empty list")
    cpus: list[FingerprintCpu] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") not in KINDS:
            raise FingerprintError(f"{FINGERPRINT_FILE}: every entry of 'cpus' needs a 'kind' of P or E")
        cpu = _int(row.get("cpu"), "cpus[].cpu", hi=8192)
        cluster = row.get("cluster")
        if cluster is not None:
            cluster = _int(cluster, "cpus[].cluster", hi=4096)
        if (row["kind"] == KIND_E) != (cluster is not None):
            raise FingerprintError(f"{FINGERPRINT_FILE}: CPU {cpu}: only E CPUs have a cluster")
        favored = row.get("favored", False)
        if not isinstance(favored, bool):
            raise FingerprintError(f"{FINGERPRINT_FILE}: CPU {cpu}: 'favored' must be true or false")
        raw_word = row.get("fused_word")
        word: int | None = None
        if raw_word is not None:
            if not isinstance(raw_word, str) or not re.fullmatch(r"0[xX][0-9A-Fa-f]{1,8}", raw_word):
                raise FingerprintError(f"{FINGERPRINT_FILE}: CPU {cpu}: 'fused_word' must be a hex string like \"0x5B439\" or null")
            word = int(raw_word, 16)
        cpus.append(FingerprintCpu(cpu, row["kind"], _int(row.get("index"), "cpus[].index", hi=8192), cluster, favored, word))
    fingerprint = Fingerprint(cpu_id, tuple(sorted(cpus, key=lambda c: c.cpu)))
    fingerprint.topology()  # duplicate CPUs / indexes
    return fingerprint


def load_fingerprint(path: Path | str) -> Fingerprint:
    """Read ``path`` (at most 256 KiB). A missing file is the common case: an accepted state made before G-138 has none."""
    path = Path(path)
    try:
        with open(path, "rb") as handle:
            data = handle.read(_MAX_BYTES + 1)
    except FileNotFoundError:
        raise FingerprintError(f"no {FINGERPRINT_FILE} at {path}: {REACCEPT_HINT}") from None
    except OSError as exc:
        raise FingerprintError(f"cannot read {path}: {exc.strerror or exc}") from None
    if len(data) > _MAX_BYTES:
        raise FingerprintError(f"{path} is larger than {_MAX_BYTES} bytes")
    try:
        return parse_fingerprint(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise FingerprintError(f"{path} is not UTF-8 text: {REACCEPT_HINT}") from None
