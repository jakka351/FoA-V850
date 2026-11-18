#!/usr/bin/env python3
"""Decode tables from the FG Falcon BPM (Bluetooth Phone Module, 8R29-14D511) image.

Pulls the vector table, HMI/string table, the aFCAN bit-timing config block and the
SFR/peripheral footprint out of the phf2bin output and writes bpm-tables.json.

    python3 bpm/decode_bpm_tables.py bpm/bpm.bin bpm/out

Firmware facts this relies on (all verified against this image):
  * app/vector base ...... 0x4500   (reset vec -> 0x5BE0)
  * UARTA1 (BT serial) ... UA1STR 0xFFFFFA14, UA1RX 0xFFFFFA16  (RX ISR @ 0x17C4C)
  * aFCAN (MS-CAN) ....... base 0x03FEC000, cfg reg touched @ 0x03FEC058, module ID 0x781
  * CAN bit-timing block . 0x3DF00 : 'a5' magic + timing bytes
"""
import sys, struct, json, re, os

BASE = 0x4500
VEC_BASE = 0x4500
UA1STR, UA1RX = 0xFFFFFA14, 0xFFFFFA16
CAN_BASE = 0x03FEC000

def sext(v, bits):
    return v - (1 << bits) if v & (1 << (bits - 1)) else v

def vectors(img):
    out = []
    for i in range(0, 0x400, 0x10):
        a = VEC_BASE + i
        w0, w1 = struct.unpack_from('<H', img, a)[0], struct.unpack_from('<H', img, a + 2)[0]
        if (w0 & 0xF800) == 0 and (w0 & 0x07E0) == 0x0780:          # jr disp22
            disp = sext(((w0 & 0x3F) << 16) | (w1 & 0xFFFE), 22)
            out.append({"vec": i, "addr": a, "target": a + disp})
    return out

def strings(img, lo=0x4800, hi=0x5A00, minlen=4):
    out, i = [], lo
    while i < hi:
        j = i
        while j < hi and 0x20 <= img[j] < 0x7F:
            j += 1
        if j - i >= minlen:
            out.append({"addr": i, "s": img[i:j].decode('latin1')})
        i = max(j + 1, i + 1)
    return out

def can_cfg(img, at=0x3DF00):
    blk = img[at:at + 8]
    return {"addr": at, "raw": blk.hex(), "magic": blk[0], "note": "aFCAN bit-timing/config"}

def main():
    img = open(sys.argv[1], 'rb').read()
    outdir = sys.argv[2] if len(sys.argv) > 2 else "bpm/out"
    os.makedirs(outdir, exist_ok=True)
    tables = {
        "module": "BPM (Bluetooth Phone Module)",
        "part": "8R29-14D511-AB",
        "production_part": "8R29-14D509-AA",
        "can_module_id": 0x781,
        "mcu": "Renesas V850ES",
        "image_bytes": len(img),
        "interfaces": {
            "bt_serial": {"peripheral": "UARTA1", "UA1STR": UA1STR, "UA1RX": UA1RX,
                           "rx_isr": 0x17C4C, "rx_ring_len": 0x12C},
            "can": {"peripheral": "aFCAN", "base": CAN_BASE, "cfg_reg": 0x03FEC058,
                     "module_id": 0x781, "bus": "MS-CAN 125 kbit/s"},
        },
        "vectors": vectors(img),
        "can_cfg": can_cfg(img),
        "strings": strings(img),
    }
    with open(os.path.join(outdir, "bpm-tables.json"), "w") as f:
        json.dump(tables, f, indent=1)
    print("vectors(active):", len(tables["vectors"]),
          "strings:", len(tables["strings"]),
          "-> %s/bpm-tables.json" % outdir)

if __name__ == "__main__":
    main()
