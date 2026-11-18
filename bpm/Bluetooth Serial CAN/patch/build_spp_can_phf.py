#!/usr/bin/env python3
"""Build a patched BPM PHF carrying the SPP<->CAN bridge shim.

Pipeline:
  1. assemble spp_can_bridge.s with the V850 toolchain, linked at 0x1EB68,
     and read its symbol map (no external shim.bin / hand-kept offsets).
  2. inject the shim into free flash at 0x1EB68.
  3. (--arm) install four detour hooks, each verified against the exact stock
     instruction it displaces before patching:
        0x17C7A  st.b r13,0[r11]   -> jr   trampoline_rx     (UART-RX byte tee)
        0xF77E   st.w r6,0[sp]      -> jr   tramp_f77e         (CAN-RX OEM-ID tee)
        0xEE66   st.w r6,0[sp]      -> jr   tramp_ee66         (CAN-RX catch-all tee)
        0x12BFA  jarl 0x15D14       -> jarl tramp_drain        (main-loop TX drain)
  4. re-emit a Ford PHF with correct per-record + FILE CHECKSUM (phfpatch).
  5. verify: round-trips through phf2bin, only the intended bytes changed, and
     every armed hook disassembles to the right jump/target (objdump).

Usage (from the repo root):
    # SAFE build (shim present in free flash, hooks NOT armed -> behaviourally stock):
    python3 "bpm/Bluetooth Serial CAN/patch/build_spp_can_phf.py" \
            -o bpm/8R29-14D511-JL-SPPCAN.PHF
    # ARMED build (hooks spliced in; bench-validate before flashing):
    python3 "bpm/Bluetooth Serial CAN/patch/build_spp_can_phf.py" --arm \
            -o bpm/8R29-14D511-JL-SPPCAN.PHF

The V850 branch encoder is self-tested against real firmware instructions, so a
failed self-test aborts the build rather than emitting a bad jump.
"""
import sys, os, struct, argparse, subprocess, re, shutil, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from phfpatch import PHF

HERE      = os.path.dirname(os.path.abspath(__file__))
REPO      = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))   # .../V850
SHIM_ASM  = os.path.join(HERE, "spp_can_bridge.s")
SHIM_ORG  = 0x1EB68          # start of the 127,896-byte 0xFF free-flash run
FREE_END  = 0x3DF00          # aFCAN cfg block begins here

# default cross toolchain (override with --toolchain or $V850_BIN)
TOOLCHAIN = os.environ.get("V850_BIN", "C:/SysGCC/v850-elf/bin")

# ---- detour hook table: (name, site, displaced_instr_bytes, shim_symbol, kind) ----
HOOKS = [
    ("uart_rx",        0x17C7A, bytes.fromhex("4b6f0000"), "trampoline_rx", "jr"),
    ("can_rx_f77e",    0x0F77E, bytes.fromhex("63370100"), "tramp_f77e",    "jr"),
    ("can_rx_ee66",    0x0EE66, bytes.fromhex("63370100"), "tramp_ee66",    "jr"),
    ("mainloop_drain", 0x12BFA, bytes.fromhex("80ff1a31"), "tramp_drain",   "jarl"),
]

# ---- V850 branch encoder (Format V disp22) -----------------------------------
def enc_jr(pc, target, reg2=0):
    """JR disp22 (reg2=0) / JARL disp22,reg2 -> 4 bytes, little-endian halfwords."""
    disp = (target - pc) & 0x3FFFFF
    hw0 = (reg2 << 11) | (0x1E << 6) | ((disp >> 16) & 0x3F)
    hw1 = disp & 0xFFFF
    return struct.pack('<HH', hw0, hw1)

def enc_jarl(pc, target):
    return enc_jr(pc, target, reg2=31)          # link in lp (r31)

def selftest():
    # reproduce real firmware instructions byte-for-byte (no disassembler needed)
    assert enc_jarl(0xED7E, 0xEA44) == bytes.fromhex("bfffc6fc"), "jarl enc wrong"
    assert enc_jr(0x17D96, 0x17C54) == bytes.fromhex("bf07befe"), "jr enc wrong"
    # disp must fit signed 22-bit for every hook we will install
    for _, site, _, _, _ in HOOKS:
        d = (SHIM_ORG - site)
        assert -0x200000 <= d <= 0x1FFFFF, f"hook at {site:#x} out of jr/jarl range"
    return True

# ---- toolchain helpers --------------------------------------------------------
def tool(name):
    cand = os.path.join(TOOLCHAIN, name)
    for ext in ("", ".exe"):
        if os.path.isfile(cand + ext):
            return cand + ext
    found = shutil.which(name)
    if found:
        return found
    sys.exit(f"ERROR: {name} not found in {TOOLCHAIN} or PATH (set --toolchain/$V850_BIN)")

def assemble_shim():
    """Assemble + link spp_can_bridge.s at SHIM_ORG; return (bytes, {sym:addr})."""
    as_, ld, oc, nm = tool("v850-elf-as"), tool("v850-elf-ld"), \
                      tool("v850-elf-objcopy"), tool("v850-elf-nm")
    td = tempfile.mkdtemp(prefix="sppbuild_")
    obj, elf, binf = (os.path.join(td, x) for x in ("spp.o", "spp.elf", "shim.bin"))
    subprocess.run([as_, "-mv850e", "-o", obj, SHIM_ASM], check=True)
    subprocess.run([ld, f"-Ttext={SHIM_ORG:#x}", "-e", "trampoline_rx",
                    "-o", elf, obj], check=True)
    subprocess.run([oc, "-O", "binary", elf, binf], check=True)
    shim = open(binf, "rb").read()
    syms = {}
    out = subprocess.run([nm, elf], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        m = re.match(r"([0-9a-fA-F]+)\s+\S+\s+(\S+)", line)
        if m:
            syms[m.group(2)] = int(m.group(1), 16)
    assert SHIM_ORG + len(shim) < FREE_END, "shim overflows free flash"
    return shim, syms, elf

# ---- build --------------------------------------------------------------------
def build(src_phf, out_phf, arm=False):
    assert selftest(), "V850 encoder self-test failed -- refusing to build"
    shim, syms, elf = assemble_shim()
    p = PHF(src_phf)
    p.add_region(SHIM_ORG, shim)

    hooks = []
    if arm:
        for name, site, displaced, sym, kind in HOOKS:
            have = bytes(p.mem.get(site + i, 0xFF) for i in range(4))
            if have != displaced:
                sys.exit(f"ABORT: hook '{name}' @ {site:#x} expected "
                         f"{displaced.hex()} but image has {have.hex()} -- "
                         f"wrong base image, refusing to arm")
            if sym not in syms:
                sys.exit(f"ABORT: shim symbol '{sym}' not found in map")
            tgt = syms[sym]
            enc = enc_jarl(site, tgt) if kind == "jarl" else enc_jr(site, tgt)
            p.patch(site, enc)
            hooks.append((name, site, displaced.hex(), sym, tgt, enc.hex()))

    fs = p.write(out_phf)
    return fs, len(shim), syms, hooks, elf

# ---- verification -------------------------------------------------------------
def phf2bin(phf, out):
    subprocess.run([sys.executable, os.path.join(REPO, "phf", "phf2bin.py"), phf, out],
                   capture_output=True)
    return open(out, "rb").read()

def verify(orig_phf, new_phf, shim_len, hooks):
    td = tempfile.mkdtemp(prefix="sppverify_")
    a = phf2bin(orig_phf, os.path.join(td, "a.bin"))
    b = phf2bin(new_phf,  os.path.join(td, "b.bin"))
    diffs = [i for i in range(min(len(a), len(b))) if a[i] != b[i]]
    # expected changed set: shim region + 4 bytes per armed hook
    expected = set(range(SHIM_ORG, SHIM_ORG + shim_len))
    for _, site, _, _, _, _ in hooks:
        expected |= set(range(site, site + 4))
    unexpected = [d for d in diffs if d not in expected]
    # objdump each armed hook site in the NEW image and confirm the jump target
    od = tool("v850-elf-objdump")
    hook_ok = []
    for name, site, _, sym, tgt, _ in hooks:
        sl = os.path.join(td, f"hook_{site:x}.bin")
        open(sl, "wb").write(b[site:site + 4])
        dis = subprocess.run([od, "-D", "-b", "binary", "-m", "v850e",
                              f"--adjust-vma={site:#x}", sl],
                             capture_output=True, text=True).stdout
        ok = (f"{tgt:x}" in dis.replace("0x", "").lower()) and \
             (("jarl" in dis) if name == "mainloop_drain" else ("jr" in dis))
        hook_ok.append((name, ok, dis.strip().splitlines()[-1].strip() if dis.strip() else ""))
    return len(a), len(b), diffs, unexpected, hook_ok

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default=os.path.join(REPO, "bpm", "8R29-14D511-AB.PHF"))
    ap.add_argument('--arm', action='store_true', help='install detour hooks (default off)')
    ap.add_argument('--toolchain', help='dir containing v850-elf-* (else $V850_BIN)')
    ap.add_argument('-o', '--out', default=os.path.join(REPO, "bpm", "8R29-14D511-JL-SPPCAN.PHF"))
    args = ap.parse_args()
    if args.toolchain:
        TOOLCHAIN = args.toolchain

    fs, shim_len, syms, hooks, elf = build(args.src, args.out, arm=args.arm)
    print(f"self-test: PASS")
    print(f"shim: {shim_len} bytes @ {SHIM_ORG:#x} (ends {SHIM_ORG+shim_len:#x}, free to {FREE_END:#x})")
    print(f"wrote {args.out}   FILE CHECKSUM=0x{fs:04X}   hooks armed: {len(hooks)}")
    for name, site, orig, sym, tgt, enc in hooks:
        print(f"  hook {name:<14} @ {site:#07x}: {orig} -> {enc}  ({sym} @ {tgt:#x})")

    la, lb, diffs, unexpected, hook_ok = verify(args.src, args.out, shim_len, hooks)
    span = f"{min(diffs):#x}..{max(diffs):#x}" if diffs else "none"
    print(f"verify: orig={la}B new={lb}B  changed bytes: {len(diffs)} ({span})")
    if unexpected:
        print(f"  !! {len(unexpected)} UNEXPECTED changed bytes: "
              f"{[hex(x) for x in unexpected[:8]]}")
        sys.exit(1)
    print(f"  OK: only the shim region + armed hook sites changed")
    for name, ok, line in hook_ok:
        print(f"  hook {name:<14} disasm: {'OK ' if ok else 'BAD'}  {line}")
    if any(not ok for _, ok, _ in hook_ok):
        sys.exit(1)
    print("verify: PASS")
