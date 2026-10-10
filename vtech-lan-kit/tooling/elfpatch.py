#!/usr/bin/env python3
"""
elfpatch.py -- in-place, same-size ELF64 patcher for the bionic TUTK libs.

Turns a bionic TUTK `.so` into one that loads natively against the *host* libc on glibc AND musl,
by three byte-level edits only (no new PT_LOAD, no program-header move, no section resize):

  1. strip-versioning   remove DT_VERSYM/DT_VERNEED/DT_VERNEEDNUM from .dynamic and zero the
                        .gnu.version (VERSYM) entries -> every import is unversioned, so glibc binds
                        it by name (musl ignores versions already).
  2. needed fixups      drop `DT_NEEDED libc.so/libm.so/libdl.so` (symbols resolve from the host libc
                        already in the process); optionally repoint one of them, in place, to the
                        companion soname (same-or-shorter length -> reuses the freed .dynstr bytes).
  3. rename-imports     overwrite selected UNDEFINED dynsym names in .dynstr *in place, same length*,
                        to unique tokens the companion `.so` defines (ABI-divergent + host-missing set).
  3b. runpath-origin    add DT_RUNPATH "$ORIGIN", its string written IN PLACE over a dropped DT_NEEDED
                        name (libc.so: 7 chars == len("$ORIGIN")), the entry itself going into the
                        DT_NULL slack the drops freed. Needed on musl: it resolves a DT_NEEDED soname
                        (libtk.so, siblings) only via a PATH SEARCH (LD_LIBRARY_PATH / runpath / system
                        dirs), never by matching an object already loaded by absolute path -- and an
                        in-process host embedding the loader can't set LD_LIBRARY_PATH after startup.
                        glibc resolves either way, so this is harmless there too.
  4. redirect-tp-canary (aarch64 only) -- the one instruction-level edit. bionic arm64 reads the stack
                        canary from TLS slot TPIDR_EL0+0x28, which glibc/musl don't own (it lands in the
                        host's static TLS instead). Every `mrs Xt, TPIDR_EL0` (exact encoding
                        0xD53BD040|Rt) in executable sections becomes `adrp Xt, <page of vaddr 0>`, so
                        the untouched `ldr [Xt,#0x28]` that follows reads the library's own mapped
                        Elf64_Ehdr.e_shoff instead: a per-library constant, so the canary check always
                        balances. Same instruction size (4 bytes each). Trade-off: the canary becomes a
                        PREDICTABLE constant (accidental stack overflows are still caught; the
                        exploit-hardening value of a random canary is lost).

Pure stdlib, little-endian ELF64 only. Everything is written back within the existing section spans
(the .dynamic rewrite pads with DT_NULL), so file offsets, program headers and .gnu.hash are untouched.

Usage:
  elfpatch.py IN.so OUT.so [--strip-versioning]
      [--drop-needed libc.so,libm.so,libdl.so] [--repoint-needed libc.so=libv.so]
      [--rename getaddrinfo=Getaddrinfo,pthread_mutex_lock=Pthread_mutex_lock]
      [--rename-file renames.txt]        # one `old=new` per line, '#' comments ok
      [--runpath-origin]                 # needs a dropped NEEDED name >= 7 chars (libc.so)
      [--redirect-tp-canary]             # aarch64 only
      [--dry-run]
"""
import struct, sys, argparse

# --- ELF64 constants ---
SHT_DYNSYM = 11
DT_NULL, DT_NEEDED = 0, 1
DT_STRTAB_REFS = {1, 14, 15, 29}          # DT_NEEDED, DT_SONAME, DT_RPATH, DT_RUNPATH
DT_RPATH, DT_RUNPATH = 15, 29
DT_VERSYM, DT_VERNEED, DT_VERNEEDNUM = 0x6ffffff0, 0x6ffffffe, 0x6fffffff
STRIP_TAGS = {DT_VERSYM, DT_VERNEED, DT_VERNEEDNUM}
SHN_UNDEF = 0
SHF_EXECINSTR = 0x4
EM_X86_64, EM_AARCH64 = 62, 183
MRS_TPIDR_EL0, MRS_TPIDR_EL0_MASK = 0xD53BD040, 0xFFFFFFE0   # mrs Xt, TPIDR_EL0 (Rt in bits[4:0])

def mrs_tpidr_rt(word):
    """Return Rt if `word` is `mrs Xt, TPIDR_EL0`, else None."""
    return word & 0x1F if (word & MRS_TPIDR_EL0_MASK) == MRS_TPIDR_EL0 else None

def enc_adrp(rd, pc, target):
    """Encode `adrp Xrd, target` located at vaddr `pc`."""
    delta = ((target & ~0xFFF) - (pc & ~0xFFF)) >> 12
    if not -(1 << 20) <= delta < (1 << 20):
        raise SystemExit("adrp out of range")
    delta &= (1 << 21) - 1
    return 0x90000000 | ((delta & 3) << 29) | ((delta >> 2) << 5) | rd

def dec_adrp(word, pc):
    """Return (rd, target_page) if `word` is ADRP, else None."""
    if (word & 0x9F000000) != 0x90000000:
        return None
    imm = ((word >> 5) & 0x7FFFF) << 2 | ((word >> 29) & 3)
    if imm & (1 << 20):
        imm -= 1 << 21
    return word & 0x1F, (pc & ~0xFFF) + (imm << 12)

class Elf:
    def __init__(self, path):
        self.b = bytearray(open(path, "rb").read())
        if self.b[:4] != b"\x7fELF" or self.b[4] != 2 or self.b[5] != 1:
            raise SystemExit("not a little-endian ELF64")
        (self.e_machine,) = struct.unpack_from("<H", self.b, 0x12)
        (self.e_shoff,) = struct.unpack_from("<Q", self.b, 0x28)
        self.e_shentsize, self.e_shnum, self.e_shstrndx = struct.unpack_from("<HHH", self.b, 0x3a)
        self.secs = []
        self.freed_strs = []      # (dynstr offset, len) of dropped DT_NEEDED names, reusable in place
        for i in range(self.e_shnum):
            o = self.e_shoff + i * self.e_shentsize
            name, typ, flags, addr, off, size, link, info, align, ent = struct.unpack_from("<IIQQQQIIQQ", self.b, o)
            self.secs.append(dict(name=name, type=typ, flags=flags, addr=addr, off=off, size=size,
                                  link=link, entsize=ent))
        strtab = self.secs[self.e_shstrndx]
        for s in self.secs:
            end = self.b.index(b"\0", strtab["off"] + s["name"])
            s["sname"] = self.b[strtab["off"] + s["name"]:end].decode()

    def sec(self, sname):
        for s in self.secs:
            if s["sname"] == sname:
                return s
        return None

    # ---- .dynstr helpers ----
    def _dynstr_off(self):
        return self.sec(".dynstr")["off"]

    def _str_at(self, rel):
        base = self._dynstr_off()
        end = self.b.index(b"\0", base + rel)
        return self.b[base + rel:end].decode()

    def _overwrite_str(self, rel, new):
        """Overwrite the string at .dynstr+rel in place; require len(new) <= len(old)."""
        base = self._dynstr_off()
        end = self.b.index(b"\0", base + rel)
        old = self.b[base + rel:end].decode()
        if len(new) > len(old):
            raise SystemExit(f"rename target '{new}' longer than '{old}' -- same-size edits only")
        nb = new.encode()
        self.b[base + rel:base + rel + len(nb)] = nb
        self.b[base + rel + len(nb)] = 0  # new terminator; leftover old bytes become dead string tail
        return old

    # ---- .dynamic ----
    def _dyn_entries(self):
        d = self.sec(".dynamic")
        out = []
        for o in range(d["off"], d["off"] + d["size"], 16):
            tag, val = struct.unpack_from("<qQ", self.b, o)
            out.append((tag, val))
        return d, out

    def _write_dyn(self, d, entries):
        cap = d["size"] // 16
        kept = [(t, v) for (t, v) in entries if t != DT_NULL]
        if len(kept) + 1 > cap:
            raise SystemExit("no room in .dynamic (would need to grow -- unexpected)")
        buf = b"".join(struct.pack("<qQ", t, v) for (t, v) in kept)
        buf += struct.pack("<qQ", DT_NULL, 0) * (cap - len(kept))  # DT_NULL pad/terminate
        self.b[d["off"]:d["off"] + d["size"]] = buf

    def strip_versioning(self):
        # Removing DT_VERSYM/DT_VERNEED/DT_VERNEEDNUM from .dynamic is sufficient: with no DT_VERSYM,
        # the loader does no version processing (imports bind by name; exports stay GLOBAL via .dynsym).
        # We deliberately do NOT touch the .gnu.version table bytes -- zeroing versym would mark entries
        # index 0 (VER_NDX_LOCAL), which is only a hazard if something still reads it; leaving it dead
        # and unreferenced is safer. The .gnu.version* section headers remain (cosmetic to readelf only).
        d, entries = self._dyn_entries()
        entries = [(t, v) for (t, v) in entries if t not in STRIP_TAGS]
        self._write_dyn(d, entries)
        return "removed DT_VERSYM/DT_VERNEED/DT_VERNEEDNUM from .dynamic (loader now binds by name)"

    def fix_needed(self, drop, repoint):
        d, entries = self._dyn_entries()
        out, msgs = [], []
        for tag, val in entries:
            if tag == DT_NEEDED:
                name = self._str_at(val)
                if name in repoint:
                    old = self._overwrite_str(val, repoint[name])
                    out.append((tag, val)); msgs.append(f"repoint NEEDED {old} -> {repoint[name]}")
                    continue
                if name in drop:
                    self.freed_strs.append((val, len(name)))
                    msgs.append(f"drop NEEDED {name}"); continue
            out.append((tag, val))
        self._write_dyn(d, out)
        return msgs

    def runpath_origin(self):
        want = "$ORIGIN"
        d, entries = self._dyn_entries()
        if any(t in (DT_RPATH, DT_RUNPATH) for t, _ in entries):
            raise SystemExit("lib already has DT_RPATH/DT_RUNPATH -- refusing to add another")
        slot = next(((o, n) for (o, n) in self.freed_strs if n >= len(want)), None)
        if slot is None:
            raise SystemExit("--runpath-origin needs a dropped DT_NEEDED name >= 7 chars (e.g. libc.so)")
        off, n = slot
        # the freed string must be referenced by nothing else (dynsym names / other string tags)
        ds = self.sec(".dynsym")
        refs = [struct.unpack_from("<I", self.b, o)[0] for o in range(ds["off"], ds["off"] + ds["size"], ds["entsize"])]
        refs += [v for (t, v) in entries if t in DT_STRTAB_REFS]
        if any(off <= r <= off + n for r in refs):
            raise SystemExit(f"freed .dynstr slot @{off} is still referenced -- can't reuse it")
        self._overwrite_str(off, want)
        self._write_dyn(d, entries + [(DT_RUNPATH, off)])
        return f"DT_RUNPATH {want} (string reuses the freed .dynstr slot @{off})"

    def rename_imports(self, mapping):
        ds = self.sec(".dynsym"); done = {}
        for o in range(ds["off"], ds["off"] + ds["size"], ds["entsize"]):
            st_name, st_info, st_other, st_shndx, st_value, st_size = struct.unpack_from("<IBBHQQ", self.b, o)
            if st_shndx != SHN_UNDEF:      # only UNDEFINED imports
                continue
            nm = self._str_at(st_name)
            if nm in mapping:
                self._overwrite_str(st_name, mapping[nm]); done[nm] = mapping[nm]
        missing = set(mapping) - set(done)
        return done, missing

    # ---- code ----
    def exec_words(self):
        """Yield (file_off, vaddr, word) for every aligned 4-byte word in executable PROGBITS sections."""
        for s in self.secs:
            if s["flags"] & SHF_EXECINSTR and s["type"] == 1:   # SHT_PROGBITS
                for i in range(0, s["size"] - 3, 4):
                    (w,) = struct.unpack_from("<I", self.b, s["off"] + i)
                    yield s["off"] + i, s["addr"] + i, w

    def redirect_tp_canary(self):
        if self.e_machine != EM_AARCH64:
            raise SystemExit(f"--redirect-tp-canary is aarch64-only (e_machine={self.e_machine})")
        n = 0
        for off, va, w in list(self.exec_words()):
            rt = mrs_tpidr_rt(w)
            if rt is not None:
                struct.pack_into("<I", self.b, off, enc_adrp(rt, va, 0)); n += 1
        return n

    def save(self, path):
        open(path, "wb").write(self.b)


def parse_kv(s):
    out = {}
    for pair in s.split(","):
        pair = pair.strip()
        if pair:
            k, v = pair.split("="); out[k.strip()] = v.strip()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp"); ap.add_argument("out")
    ap.add_argument("--strip-versioning", action="store_true")
    ap.add_argument("--drop-needed", default="")
    ap.add_argument("--repoint-needed", default="")   # e.g. libc.so=libv.so
    ap.add_argument("--rename", default="")
    ap.add_argument("--rename-file", default="")
    ap.add_argument("--runpath-origin", action="store_true")
    ap.add_argument("--redirect-tp-canary", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    e = Elf(a.inp)
    if e.e_machine not in (EM_X86_64, EM_AARCH64):
        raise SystemExit(f"unsupported e_machine {e.e_machine} (want x86_64=62 or aarch64=183)")
    ph0 = struct.unpack_from("<Q", e.b, 0x20)[0], struct.unpack_from("<H", e.b, 0x38)[0]  # (e_phoff, e_phnum)

    if a.strip_versioning:
        print("[strip]", e.strip_versioning())
    drop = set(x.strip() for x in a.drop_needed.split(",") if x.strip())
    repoint = parse_kv(a.repoint_needed) if a.repoint_needed else {}
    if drop or repoint:
        for m in e.fix_needed(drop, repoint):
            print("[needed]", m)
    if a.runpath_origin:
        print("[runpath]", e.runpath_origin())
    ren = parse_kv(a.rename) if a.rename else {}
    if a.rename_file:
        for line in open(a.rename_file):
            line = line.split("#", 1)[0].strip()
            if line:
                k, v = line.split("="); ren[k.strip()] = v.strip()
    if ren:
        done, missing = e.rename_imports(ren)
        print(f"[rename] {len(done)} renamed" + (f"; NOT FOUND: {sorted(missing)}" if missing else ""))

    if a.redirect_tp_canary:
        print(f"[canary] redirected {e.redirect_tp_canary()} `mrs Xt, TPIDR_EL0` -> `adrp Xt, <vaddr 0 page>`")

    ph1 = struct.unpack_from("<Q", e.b, 0x20)[0], struct.unpack_from("<H", e.b, 0x38)[0]
    assert ph0 == ph1, "program header table moved -- must never happen"
    print(f"[ok] program headers unchanged (e_phoff={ph1[0]}, e_phnum={ph1[1]})")

    if a.dry_run:
        print("[dry-run] not writing"); return
    e.save(a.out); print(f"[wrote] {a.out}")


if __name__ == "__main__":
    main()
