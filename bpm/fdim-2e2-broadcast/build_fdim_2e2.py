#!/usr/bin/env python3
"""Patch the Mk2 FG Falcon FDIM (AR79-14D017-JL) to periodically broadcast CAN 0x2E2,
emulating the Mk1 FDIM (8R29-14D017-AC) so the BEM reports IgnitionState=IgnitionOn and
the BPM stops NRC-0x22'ing diagnostic sessions.

Pipeline: assemble fdim_2e2.s at the free-flash org, inject it, redirect the CAN0
TX-complete vector (0x41D4) to the shim, recompute the Ford PHF checksum, and verify
(phf2bin round-trip + only-intended-bytes + objdump the hook).  Reuses the audited
phfpatch + branch encoder from the BPM kit.

    python build_fdim_2e2.py                 # -> AR79-14D017-JL-2E2.PHF (default payload)
    python build_fdim_2e2.py --payload 0001020304050607   # custom 8-byte 0x2E2 payload
"""
import sys, os, struct, argparse, subprocess, re, shutil, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))                      # .../V850
PATCHKIT = os.path.join(REPO, "bpm", "Bluetooth Serial CAN", "patch")
sys.path.insert(0, PATCHKIT)
from phfpatch import PHF

SHIM_ASM  = os.path.join(HERE, "fdim_2e2.s")
SHIM_ORG  = 0x24840          # start of the FDIM free-flash run (0xFF to 0x3FFFB)
FREE_END  = 0x3FFFC          # tail config word lives at 0x3FFFC
HOOK_SITE = 0x41D4           # CAN0 TX-complete vector slot 53
HOOK_ORIG = bytes.fromhex("81078a1a")   # jr 0x15C5E (the real ISR)
TOOLCHAIN = os.environ.get("V850_BIN", "C:/SysGCC/v850-elf/bin")

def enc_jr(pc, target, reg2=0):
    disp = (target - pc) & 0x3FFFFF
    return struct.pack('<HH', (reg2 << 11) | (0x1E << 6) | ((disp >> 16) & 0x3F), disp & 0xFFFF)

def tool(name):
    for ext in ("", ".exe"):
        c = os.path.join(TOOLCHAIN, name) + ext
        if os.path.isfile(c): return c
    return shutil.which(name) or sys.exit(f"ERROR: {name} not found ({TOOLCHAIN} or PATH)")

def selftest():
    # same byte-exact check as the BPM kit (real firmware jr/jarl)
    assert enc_jr(0xED7E, 0xEA44, 31) == bytes.fromhex("bfffc6fc")
    assert enc_jr(0x17D96, 0x17C54) == bytes.fromhex("bf07befe")
    assert -0x200000 <= (SHIM_ORG - HOOK_SITE) <= 0x1FFFFF, "hook out of jr range"
    return True

def assemble(payload):
    as_, ld, oc, nm = tool("v850-elf-as"), tool("v850-elf-ld"), tool("v850-elf-objcopy"), tool("v850-elf-nm")
    td = tempfile.mkdtemp(prefix="fdim2e2_")
    src = os.path.join(td, "s.s")
    # inject the chosen payload into a copy of the asm
    asm = open(SHIM_ASM).read()
    asm = re.sub(r"\.byte 0, 0, 0, 0, 0, 0, 0, 0",
                 ".byte " + ", ".join(str(b) for b in payload), asm, count=1)
    open(src, "w").write(asm)
    obj, elf, binf = (os.path.join(td, x) for x in ("s.o", "s.elf", "s.bin"))
    subprocess.run([as_, "-mv850e", "-o", obj, src], check=True)
    subprocess.run([ld, f"-Ttext={SHIM_ORG:#x}", "-e", "fdim_hook", "-o", elf, obj], check=True)
    subprocess.run([oc, "-O", "binary", elf, binf], check=True)
    shim = open(binf, "rb").read()
    nmout = subprocess.run([nm, elf], capture_output=True, text=True).stdout
    syms = {m.group(2): int(m.group(1), 16)
            for m in re.finditer(r"(?m)^([0-9a-fA-F]+)\s+\S+\s+(\S+)$", nmout)}
    assert SHIM_ORG + len(shim) < FREE_END, "shim overflows free flash"
    return shim, syms

def phf2bin(phf, out):
    subprocess.run([sys.executable, os.path.join(REPO, "phf", "phf2bin.py"), phf, out], capture_output=True)
    return open(out, "rb").read()

def build(src_phf, out_phf, payload):
    assert selftest()
    shim, syms = assemble(payload)
    tgt = syms["fdim_hook"]
    p = PHF(src_phf)
    have = bytes(p.mem.get(HOOK_SITE + i, 0xFF) for i in range(4))
    if have != HOOK_ORIG:
        sys.exit(f"ABORT: hook @ {HOOK_SITE:#x} expected {HOOK_ORIG.hex()} but image has {have.hex()}")
    p.add_region(SHIM_ORG, shim)
    enc = enc_jr(HOOK_SITE, tgt)
    p.patch(HOOK_SITE, enc)
    fs = p.write(out_phf)
    return fs, len(shim), tgt, enc

def verify(src_phf, out_phf, shim_len):
    td = tempfile.mkdtemp(prefix="fdimv_")
    a = phf2bin(src_phf, os.path.join(td, "a.bin"))
    b = phf2bin(out_phf, os.path.join(td, "b.bin"))
    diffs = [i for i in range(min(len(a), len(b))) if a[i] != b[i]]
    expected = set(range(SHIM_ORG, SHIM_ORG + shim_len)) | set(range(HOOK_SITE, HOOK_SITE + 4))
    unexpected = [d for d in diffs if d not in expected]
    od = tool("v850-elf-objdump")
    sl = os.path.join(td, "hook.bin"); open(sl, "wb").write(b[HOOK_SITE:HOOK_SITE + 4])
    dis = subprocess.run([od, "-D", "-b", "binary", "-m", "v850e", f"--adjust-vma={HOOK_SITE:#x}", sl],
                         capture_output=True, text=True).stdout
    return len(a), len(b), diffs, unexpected, dis.strip().splitlines()[-1].strip()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(REPO, "AR79-14D017-JL.PHF"))
    ap.add_argument("-o", "--out", default=os.path.join(HERE, "AR79-14D017-JL-2E2.PHF"))
    ap.add_argument("--payload", default="0000000000000000", help="8-byte 0x2E2 payload, hex")
    ap.add_argument("--toolchain")
    args = ap.parse_args()
    if args.toolchain: TOOLCHAIN = args.toolchain
    payload = bytes.fromhex(args.payload)
    assert len(payload) == 8, "payload must be 8 bytes"

    fs, shim_len, tgt, enc = build(args.src, args.out, payload)
    print(f"self-test: PASS")
    print(f"shim: {shim_len} B @ {SHIM_ORG:#x} (fdim_hook @ {tgt:#x})")
    print(f"hook @ {HOOK_SITE:#x}: {HOOK_ORIG.hex()} -> {enc.hex()}  (jr -> shim)")
    print(f"payload 0x2E2: {payload.hex()}")
    print(f"wrote {args.out}   FILE CHECKSUM=0x{fs:04X}")
    la, lb, diffs, unexpected, hookdis = verify(args.src, args.out, shim_len)
    span = f"{min(diffs):#x}..{max(diffs):#x}" if diffs else "none"
    print(f"verify: orig={la}B new={lb}B  changed: {len(diffs)} ({span})")
    if unexpected:
        print(f"  !! UNEXPECTED changed bytes: {[hex(x) for x in unexpected[:8]]}"); sys.exit(1)
    print(f"  OK: only shim region + hook site changed")
    print(f"  hook disasm: {hookdis}")
    if "jr" not in hookdis or f"{tgt:x}" not in hookdis.replace("0x", "").lower():
        print("  !! hook disasm mismatch"); sys.exit(1)
    print("verify: PASS")
