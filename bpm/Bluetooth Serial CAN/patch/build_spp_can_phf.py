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

# Optional 5th hook: auto-discoverable-at-boot for headless pairing. Splices the
# "No Phone Paired" one-shot (set1 6,-0x6E46[gp]) so the module enters pairing + goes
# discoverable with no FDIM. Armed with --arm unless --no-auto-pair.
AUTOPAIR_HOOK = ("auto_discoverable", 0x142C2, bytes.fromhex("c437ba91"), "tramp_autopair", "jr")

# ---- HMI string rebrand: (flash addr, replacement) ---------------------------
# The BPM paints these labels on the FDIM. We overwrite them IN PLACE to show the
# bridge identity. Each replacement is validated to fit its original slot (the
# string's bytes + the run of NULs up to the next string) so nothing spills into
# the neighbouring string; the rest of the slot is NUL-padded. All are display-only
# labels (loaded into r7 and handed to the FDIM formatter), never compared in logic.
BRAND = [
    (0x4E08, "INTERFACE VIA CAN"),       # was "PAIR A NEW PHONE"
    (0x4E1A, "BTSPP-CAN ACTIVE"),        # was "Bluetooth active"
    (0x4E2C, "ENTER PIN CODE"),          # was "Enter passkey"
    (0x4E3D, "INTERFACE VIA CAN"),
    (0x4E4F, "BTSPP-CAN ACTIVE"),
    (0x4E61, "ENTER PIN CODE"),
    (0x4E70, "INTERFACE VIA CAN"),
    (0x4E84, "Serial CAN Port Opened"),  # was "Paired successfully"
    (0x4E9B, "INTERFACE VIA CAN"),
    (0x4EDA, "INTERFACE VIA CAN"),
    (0x4F13, "INTERFACE VIA CAN"),
    (0x4F4F, "INTERFACE VIA CAN"),
    (0x4F61, "CAN PORT BUSY"),           # was "Bluetooth busy"
    (0x4F98, "Opening CAN port"),        # was "Connecting..."
    (0x5120, "CAN PORT DOWN"),           # was "Disconnected" (x6)
    (0x512E, "CAN PORT DOWN"),
    (0x5157, "CAN PORT DOWN"),
    (0x5165, "CAN PORT DOWN"),
    (0x5189, "CAN PORT DOWN"),
    (0x5197, "CAN PORT DOWN"),
    (0x5204, "CAN LIVE!"),               # was "Connected"
    (0x520F, "BTSPP-CAN ONLINE"),        # was "Connected"
    (0x575E, "BTSPP-CAN IDLE"),          # was "No Phone Paired"
    (0x576F, "Pair to open CAN bridge"), # was "Please pair a new phone"
    (0x5788, "Pair to open CAN bridge"),
    (0x57A3, "SPPCAN BRIDGE"),           # was "BPM VERSION:"
    (0x57B1, "SW MOD: JAKKA351"),        # was "S/W: Adam Brown"  (mod author)
    (0x57C2, "SW MOD: JAKKA351"),
    (0x57D3, "TESTERPRESENT 3E"),        # was "H/W: Mustafa Mustafa"  (UDS 0x3E easter egg)
]

def _slot(p, addr):
    """Bytes available at addr = the string's chars + the NUL run up to the next string."""
    e = addr
    while e in p.mem and 0x20 <= p.mem[e] < 0x7F: e += 1   # original chars
    while e in p.mem and p.mem[e] == 0:            e += 1   # NUL/pad run
    return e - addr

# PIN generator routine (fills r6 with 4 random ASCII digits). See SCOPE/pairing R-E.
PIN_SITE   = 0x6934
DEFAULT_PIN = "1234"
# HMI string slots that label the passkey (overwritten to show the hardcoded PIN).
PIN_LABEL_SLOTS = (0x4E2C, 0x4E61)

def apply_brand(p, pin=None):
    """Overwrite BRAND strings in place, each NUL-padded to fill its slot. When a fixed
    `pin` is set, the 'Enter passkey' slots show it. Returns (ranges, report)."""
    brand = list(BRAND)
    if pin:
        label = f"USE PIN {pin}"
        brand = [(a, label if a in PIN_LABEL_SLOTS else s) for a, s in brand]
    ranges, report = [], []
    for addr, new in brand:
        slot = _slot(p, addr)
        nb = new.encode("ascii")
        if len(nb) + 1 > slot:
            sys.exit(f"ABORT brand @ {addr:#x}: {new!r} needs {len(nb)+1} bytes "
                     f"but slot is only {slot}")
        old = ""
        o = addr
        while o in p.mem and 0x20 <= p.mem[o] < 0x7F:
            old += chr(p.mem[o]); o += 1
        p.patch(addr, nb + b"\x00" * (slot - len(nb)))   # new + NUL pad, no spill
        ranges.append((addr, addr + slot))
        report.append((addr, old, new))
    return ranges, report

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
    for _, site, _, _, _ in HOOKS + [AUTOPAIR_HOOK]:
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

def assemble_snippet(asm, org):
    """Assemble a tiny V850 asm string linked at `org`; return its raw bytes."""
    as_, ld, oc = tool("v850-elf-as"), tool("v850-elf-ld"), tool("v850-elf-objcopy")
    td = tempfile.mkdtemp(prefix="sppsnip_")
    s, obj, elf, binf = (os.path.join(td, x) for x in ("s.s", "s.o", "s.elf", "s.bin"))
    open(s, "w").write(".text\n.global _s\n_s:\n" + asm + "\n")
    subprocess.run([as_, "-mv850e", "-o", obj, s], check=True)
    subprocess.run([ld, f"-Ttext={org:#x}", "-e", "_s", "-o", elf, obj], check=True)
    subprocess.run([oc, "-O", "binary", elf, binf], check=True)
    return open(binf, "rb").read()

def apply_pin(p, pin):
    """Hardcode the 4-digit Bluetooth pairing PIN by replacing the host PIN generator at
    PIN_SITE (0x6934, r6 = dest buffer) with 'write these 4 ASCII digits, return'. The
    original is a ~188-byte PRNG routine with a single caller, so the small replacement
    fits and nothing branches into it mid-body. Returns (start, end)."""
    if not (len(pin) == 4 and pin.isdigit()):
        sys.exit(f"ABORT pin: {pin!r} must be exactly 4 digits")
    asm = "\n".join(f"mov {ord(c)}, r10\nst.b r10, {i}[r6]" for i, c in enumerate(pin)) + "\njmp [lp]"
    code = assemble_snippet(asm, PIN_SITE)
    # footprint guard: must stay well inside the original routine (next fn well past here)
    if len(code) > 0x80:
        sys.exit(f"ABORT pin: patch {len(code)} bytes too large for the routine footprint")
    for i, b in enumerate(code):
        p.mem[PIN_SITE + i] = b
    return (PIN_SITE, PIN_SITE + len(code))

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
def build(src_phf, out_phf, arm=False, brand=True, pin=DEFAULT_PIN, auto_pair=True):
    assert selftest(), "V850 encoder self-test failed -- refusing to build"
    shim, syms, elf = assemble_shim()
    p = PHF(src_phf)
    p.add_region(SHIM_ORG, shim)

    pin_range = apply_pin(p, pin) if pin else None
    brand_ranges, brand_report = (apply_brand(p, pin) if brand else ([], []))

    hooks = []
    if arm:
        hook_list = HOOKS + ([AUTOPAIR_HOOK] if auto_pair else [])
        for name, site, displaced, sym, kind in hook_list:
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
    return fs, len(shim), syms, hooks, elf, brand_ranges, brand_report, pin_range

# ---- verification -------------------------------------------------------------
def phf2bin(phf, out):
    subprocess.run([sys.executable, os.path.join(REPO, "phf", "phf2bin.py"), phf, out],
                   capture_output=True)
    return open(out, "rb").read()

def verify(orig_phf, new_phf, shim_len, hooks, brand_ranges=(), pin_range=None):
    td = tempfile.mkdtemp(prefix="sppverify_")
    a = phf2bin(orig_phf, os.path.join(td, "a.bin"))
    b = phf2bin(new_phf,  os.path.join(td, "b.bin"))
    diffs = [i for i in range(min(len(a), len(b))) if a[i] != b[i]]
    # expected changed set: shim region + 4 bytes per armed hook + rebranded strings + PIN patch
    expected = set(range(SHIM_ORG, SHIM_ORG + shim_len))
    for _, site, _, _, _, _ in hooks:
        expected |= set(range(site, site + 4))
    for s, e in brand_ranges:
        expected |= set(range(s, e))
    if pin_range:
        expected |= set(range(pin_range[0], pin_range[1]))
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
    ap.add_argument('--no-brand', action='store_true', help='keep stock HMI strings (default: rebrand)')
    ap.add_argument('--pin', default=DEFAULT_PIN, help=f'hardcode the 4-digit BT pairing PIN (default {DEFAULT_PIN})')
    ap.add_argument('--random-pin', action='store_true', help='keep the stock per-session random PIN')
    ap.add_argument('--no-auto-pair', action='store_true', help='do not arm the auto-discoverable-at-boot hook')
    ap.add_argument('--toolchain', help='dir containing v850-elf-* (else $V850_BIN)')
    ap.add_argument('-o', '--out', default=os.path.join(REPO, "bpm", "8R29-14D511-JL-SPPCAN.PHF"))
    args = ap.parse_args()
    if args.toolchain:
        TOOLCHAIN = args.toolchain
    pin = None if args.random_pin else args.pin

    fs, shim_len, syms, hooks, elf, brand_ranges, brand_report, pin_range = build(
        args.src, args.out, arm=args.arm, brand=not args.no_brand, pin=pin,
        auto_pair=not args.no_auto_pair)
    print(f"self-test: PASS")
    print(f"shim: {shim_len} bytes @ {SHIM_ORG:#x} (ends {SHIM_ORG+shim_len:#x}, free to {FREE_END:#x})")
    print(f"wrote {args.out}   FILE CHECKSUM=0x{fs:04X}   hooks armed: {len(hooks)}   strings rebranded: {len(brand_report)}")
    if pin_range:
        print(f"  PIN  @ {PIN_SITE:#07x}: hardcoded pairing PIN = {pin} (generator replaced, {pin_range[1]-pin_range[0]} bytes)")
    for name, site, orig, sym, tgt, enc in hooks:
        print(f"  hook {name:<14} @ {site:#07x}: {orig} -> {enc}  ({sym} @ {tgt:#x})")
    for addr, old, new in brand_report:
        print(f"  str  @ {addr:#07x}: {old!r:<24} -> {new!r}")

    la, lb, diffs, unexpected, hook_ok = verify(args.src, args.out, shim_len, hooks, brand_ranges, pin_range)
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
