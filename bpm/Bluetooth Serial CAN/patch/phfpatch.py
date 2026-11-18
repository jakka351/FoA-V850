#!/usr/bin/env python3
"""Ford PHF patcher for the FoA V850 modules.

Parses an original PHF, lets you overwrite bytes at absolute addresses and/or add
new filled regions (e.g. injected code in free flash), then re-emits a structurally
identical PHF with every per-record checksum and the header FILE CHECKSUM recomputed.

Round-trips exactly: phf2bin(emit(parse(x))) == phf2bin(x) when no patches applied.

    from phfpatch import PHF
    p = PHF("bpm/8R29-14D511-AB.PHF")
    p.patch(0x17c4c, bytes.fromhex("....."))      # overwrite
    p.add_region(0x1eb68, shim_bytes)             # inject into free flash
    p.write("bpm/SW-8R29-14D511-AB-SPPCAN.PHF")
"""
import struct, sys

class PHF:
    def __init__(self, path):
        raw = open(path, 'rb').read()
        he = raw.find(b'$')
        self.header = raw[:he].decode('latin1')
        self.mem = {}                      # abs addr -> byte
        self.regions_seen = []             # (start,end) covered by original data recs
        base = 0
        pos = he + 1
        cur_start = None; cur_last = None
        while pos < len(raw):
            b = raw[pos]
            if b in (0x0A, 0x0D, 0x20): pos += 1; continue
            if b != 0x3A: pos += 1; continue
            ln = raw[pos+1]; addr = struct.unpack('>H', raw[pos+2:pos+4])[0]; typ = raw[pos+4]
            payload = raw[pos+5:pos+5+ln]
            if typ == 0x04:
                base = struct.unpack('>H', payload[:2])[0] << 16
            elif typ == 0x02:
                base = struct.unpack('>H', payload[:2])[0] << 4
            elif typ == 0x00:
                a = base + addr
                for i, by in enumerate(payload): self.mem[a+i] = by
            pos += 5 + ln + 1
        # derive contiguous filled regions
        self._recompute_regions()

    def _recompute_regions(self):
        self.regions_seen = []
        if not self.mem: return
        ks = sorted(self.mem)
        s = p = ks[0]
        for a in ks[1:]:
            if a == p+1: p = a; continue
            self.regions_seen.append((s, p+1)); s = p = a
        self.regions_seen.append((s, p+1))

    def patch(self, addr, data):
        for i, by in enumerate(data):
            if (addr+i) not in self.mem:
                raise ValueError(f"patch at {addr+i:#x} outside existing filled flash "
                                 f"(use add_region for injected code)")
            self.mem[addr+i] = by

    def add_region(self, addr, data):
        for i, by in enumerate(data): self.mem[addr+i] = by
        self._recompute_regions()

    @staticmethod
    def _cks(body):                        # body = len,addrhi,addrlo,type,*data
        return (-(sum(body))) & 0xFF

    def _rec(self, typ, addr16, payload):
        body = bytes([len(payload), (addr16>>8)&0xFF, addr16&0xFF, typ]) + bytes(payload)
        return b':'.replace(b':', b'\x3a') + body + bytes([self._cks(body)])

    def emit(self, chunk=32):
        out = bytearray()
        # header with refreshed FILE CHECKSUM (sum16 of all data bytes)
        filesum = sum(self.mem.values()) & 0xFFFF
        hdr = self.header
        import re
        hdr = re.sub(r'(FILE CHECKSUM>\s*)0x[0-9A-Fa-f]+',
                     lambda m: f"{m.group(1)}0x{filesum:04X}", hdr)
        out += hdr.encode('latin1') + b'$'
        cur_base = None
        for (s, e) in self.regions_seen:
            a = s
            while a < e:
                upper = (a >> 16) & 0xFFFF
                if upper != cur_base:
                    out += self._rec(0x04, 0x0000, struct.pack('>H', upper))
                    cur_base = upper
                n = min(chunk, e - a, 0x10000 - (a & 0xFFFF))   # don't cross 64K addr16 wrap
                payload = bytes(self.mem[a+i] for i in range(n))
                out += self._rec(0x00, a & 0xFFFF, payload)
                a += n
        out += self._rec(0x01, 0x0000, b'')          # EOF
        return bytes(out), filesum

    def write(self, path, chunk=32):
        data, filesum = self.emit(chunk)
        open(path, 'wb').write(data)
        return filesum

if __name__ == '__main__':
    p = PHF(sys.argv[1])
    fs = p.write(sys.argv[2])
    print(f"wrote {sys.argv[2]}  FILE CHECKSUM=0x{fs:04X}  regions={len(p.regions_seen)}")
