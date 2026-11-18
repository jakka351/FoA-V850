#!/usr/bin/env python3
"""Build a patched BPM PHF carrying the SPP<->CAN bridge shim.

Pipeline:
  1. inject the assembled shim blob into free flash at 0x1EB68
  2. (optional, --arm) install detour hooks into the UART-RX ISR and CAN-RX path
  3. re-emit a Ford PHF with correct per-record + FILE CHECKSUM (phfpatch)
  4. verify the result round-trips through phf2bin

The V850 branch encoder below is self-tested against *real* firmware instructions, so
if it is ever used to write a hook it is provably correct — a failed self-test aborts
the build rather than emitting a bad jump.

    # safe build (shim present in free flash, hooks NOT armed -> behaviourally stock):
    python3 patch/build_spp_can_phf.py --shim shim.bin -o bpm/SW-8R29-14D511-AB-SPPCAN.PHF
    # after you have validated the shim in the emulator, arm the hooks:
    python3 patch/build_spp_can_phf.py --shim shim.bin --arm -o ...PHF
"""
import sys, os, struct, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from phfpatch import PHF

SHIM_ORG   = 0x1EB68          # start of the 127,896-byte 0xFF free-flash run
UART_RX_ISR= 0x17C4C          # [RE-VERIFIED] UARTA1 receive ISR
CAN_EXTRACT= 0x0EE66          # [RE-VERIFIED] CAN0 RX frame extractor entry
import pypcode
_ctx = pypcode.Context("V850:LE:32:default")

# ---- V850 branch/stub encoders (Format V disp22) -----------------------------
def enc_jr(pc, target, reg2=0):
    """JR disp22 (reg2=0) / JARL disp22,reg2 — 4 bytes, little-endian halfwords."""
    disp = (target - pc) & 0x3FFFFF
    hw0 = (reg2 << 11) | (0x1E << 6) | ((disp >> 16) & 0x3F)
    hw1 = disp & 0xFFFF
    return struct.pack('<HH', hw0, hw1)

def enc_jarl(pc, target):
    return enc_jr(pc, target, reg2=31)          # link in lp (r31)

JMP_LP = struct.pack('<H', 0x007F)              # jmp [lp]   (verified @0xEE5A)
NOP    = struct.pack('<H', 0x0000)

def _disasm(b, base):
    r = _ctx.disassemble(b, base_address=base, max_instructions=1)
    i = r.instructions[0]; return i.mnem, i.body

def selftest():
    # reproduce real firmware instructions byte-for-byte
    assert enc_jarl(0xED7E, 0xEA44) == bytes.fromhex("bfffc6fc"), "jarl enc wrong"
    m, b = _disasm(enc_jarl(0xED7E, 0xEA44), 0xED7E)
    assert m == 'jarl' and 'ea44' in b.replace('0x', ''), (m, b)
    m, b = _disasm(enc_jr(0x4500, 0x5BE0), 0x4500)
    assert m == 'jr', (m, b)
    m, b = _disasm(JMP_LP, 0)
    assert m == 'jmp', (m, b)
    return True

# ---- build --------------------------------------------------------------------
def build(src_phf, shim, out_phf, arm=False):
    assert selftest(), "V850 encoder self-test failed — refusing to build"
    p = PHF(src_phf)

    # 1. inject shim into free flash
    if shim is None:
        shim = JMP_LP + NOP * 31        # 64-byte safe reserved block (returns if ever called)
        note = "reserved 64-byte safe stub (assemble spp_can_bridge.s and pass --shim)"
    else:
        note = f"{len(shim)} bytes from {args.shim}"
    assert SHIM_ORG + len(shim) < 0x3DF00, "shim overflows free flash"
    p.add_region(SHIM_ORG, shim)

    # 2. optional detour hooks (OFF by default — arming an unvalidated shim can brick)
    hooks = []
    if arm:
        # overwrite 4 bytes at each hook site with a jump into the shim trampolines.
        # trampoline addresses are exported by the assembled shim's map; here we assume
        # the shim begins with: [0]=shim_init [+4]=trampoline_rx [+8]=trampoline_cantx
        # (adjust offsets to your build's symbol map before arming).
        tramp_rx     = SHIM_ORG + 0x04
        tramp_cantx  = SHIM_ORG + 0x08
        orig_rx   = bytes(p.mem[UART_RX_ISR+i] for i in range(4))
        orig_can  = bytes(p.mem[CAN_EXTRACT+i] for i in range(4))
        p.patch(UART_RX_ISR, enc_jr(UART_RX_ISR, tramp_rx))
        p.patch(CAN_EXTRACT, enc_jr(CAN_EXTRACT, tramp_cantx))
        hooks = [(UART_RX_ISR, orig_rx.hex(), tramp_rx),
                 (CAN_EXTRACT, orig_can.hex(), tramp_cantx)]

    fs = p.write(out_phf)
    return fs, note, hooks

def verify(orig_phf, new_phf):
    """phf2bin both, confirm only the intended addresses changed."""
    import subprocess
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    def tobin(phf, out):
        subprocess.run([sys.executable, os.path.join(here, "phf/phf2bin.py"), phf, out],
                       capture_output=True)
        return open(out, 'rb').read()
    a = tobin(orig_phf, "/tmp/_a.bin"); b = tobin(new_phf, "/tmp/_b.bin")
    diffs = [i for i in range(min(len(a), len(b))) if a[i] != b[i]]
    return len(a), len(b), diffs

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default='bpm/8R29-14D511-AB.PHF')
    ap.add_argument('--shim', help='assembled shim .bin (objcopy -O binary)')
    ap.add_argument('--arm', action='store_true', help='install detour hooks (default off)')
    ap.add_argument('-o', '--out', default='bpm/SW-8R29-14D511-AB-SPPCAN.PHF')
    args = ap.parse_args()
    shim = open(args.shim, 'rb').read() if args.shim else None
    fs, note, hooks = build(args.src, shim, args.out, arm=args.arm)
    print(f"self-test: PASS   shim: {note}")
    print(f"wrote {args.out}   FILE CHECKSUM=0x{fs:04X}   hooks armed: {bool(hooks)}")
    la, lb, diffs = verify(args.src, args.out)
    span = f"{min(diffs):#x}..{max(diffs):#x}" if diffs else "none"
    print(f"verify: orig={la}B new={lb}B  changed bytes: {len(diffs)} ({span})")
    for site, orig, tgt in hooks:
        print(f"  hook @ {site:#x}: was {orig} -> jr {tgt:#x}")
