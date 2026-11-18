# FG Falcon BPM (Bluetooth Phone Module) — firmware teardown

Reverse of `SW-8R29-14D511-AB.HEX` (PHF `8R29-14D511-AB`), the FG Falcon **Bluetooth
Phone Module**. Same Renesas V850ES family and PHF/aFCAN world as the FDIM and AIM units
in this repo, so the existing toolchain (`phf/phf2bin.py`, `analysis/survey.py`,
`analysis/adis.py`) drops straight onto it.

> Method note: this writeup is built from `phf2bin` + `survey.py` + `adis.py` + a full
> string sweep of the image — not from reading the raw Intel-HEX by eye. Items that need a
> live bench capture (the UART framing to the BT baseband, the baseband's identity, the
> exact CAN byte layouts) are called out as such.

## PHF header

```
APPLICATION> BPM Application         MASK> Rev0 3 July 2008
FILE NAME>   SW-8R29-14D511-AB.HEX   RELEASE DATE> 07/03/08
MODULE TYPE> Bluetooth Phone Module  PRODUCTION PART> 8R29-14D509-AA
RELEASED BY> Sumitomo Wiring Systems Australia
MODULE NAME> BPM   MODULE ID> 0x781   FILE CHECKSUM> 0xE5C3
```

`phf2bin.py` reproduces `sum16 = 0xE5C3`, so the extracted image is byte-exact. Author
credits are left in the image: **S/W: Adam Brown**, **H/W: Mustafa Mustafa**.

## Image map

| Region | Bytes | What |
|--------|-------|------|
| `0x04500-0x04663` | 356 | vector table + early init |
| `0x04700-0x0471B` | 28 | small config/const |
| `0x04800-0x059B0` | 4529 | **HMI string table** (phone-UI text pushed to the FDIM) |
| `0x05A00-0x1EB67` | ~103 KB | application code |
| `0x3DF00-0x3DF07` | 8 | **aFCAN bit-timing/config block** (`a5 05 1e 02 00 02 e5 fd`) |

Application/vector base is **0x4500** (not 0x4400 like the FDIM). Reset vector → `0x5BE0`.
Only three vectors are populated; the rest point at a common default handler or are `0xFFFF`:

| Vector | Target | Role |
|--------|--------|------|
| `0x000` (RESET) | `0x5BE0` | startup |
| `0x0A0` | `0x17C4C` | **UARTA1 receive ISR** (BT serial) |
| `0x0B0` | `0x0ED64` | second active ISR (CAN INTC0 / timer — confirm with `adis.py`) |

## Interfaces (the whole point of this module)

The BPM has no display of its own. It is a headless MS-CAN node whose only two real-world
I/O paths are a **UART to a Bluetooth baseband** and the **car's MS-CAN bus** — it bridges
the two and renders its UI on the FDIM over CAN.

### 1. Bluetooth serial link — UARTA1

The ISR at `0x17C4C` is unambiguous (`adis.py`, SFR-annotated):

```
017c58: tst1  0x0, [0xfffffa14] UA1STR      ; UART status bit 0 (overrun?)
017c5e: tst1  0x1, [0xfffffa14] UA1STR      ; framing/err
017c76: ld.b  [0xfffffa16] UA1RX, r13       ; read received byte
017c7a: st.b  r13, 0x0[r11]                  ; store into RX ring
017c84: st.h  r14, -0x62d6[gp]               ; ring write pointer ++
017c8c: movea 0x12c, r0, r1                  ; wrap at 300 (0x12C)
017ca4/ae: write 2 / 4 to -0x6533[gp]        ; RX state-machine flag
```

So **UARTA1** (`UA1STR 0xFFFFFA14`, `UA1RX 0xFFFFFA16`) is a byte-stream serial port into a
300-byte receive ring driven by a small framing state machine. This is the "Bluetooth
serial port": the V850 is the **host controller** and an external Bluetooth baseband is the
modem on the other end of the UART. The baseband itself isn't named in the image (no AT /
CSR / BlueCore strings), so the V850 talks to it with a vendor/host protocol over this UART.
The feature set the HMI exposes — pairing with passkey, 8-slot paired-phone list, HFP call
control (answer/reject/mute/swap/transfer/redial), voice tags — is all **host-side logic in
this V850**; the baseband just provides the RF + profile plumbing.

*Needs a bench capture:* exact UART baud and the framing/opcodes on the wire to the
baseband, and the baseband part number (scope `UA1TX`/`UA1RX` at boot, or read the PCB).

### 2. CAN — aFCAN @ 0x03FEC000, MS-CAN, module ID 0x781

Full 32-bit loads of `0x03FEC000` and `0x03FEC058` confirm the same **aFCAN** controller as
the other modules; the `0x3DF00` block (`a5…`, `a5` = valid magic) is its bit-timing/filter
config, consistent with the repo's 125 kbit/s MS-CAN. Module network ID is **0x781**. The
BPM both drives its UI onto the FDIM and takes button events back over this bus.

### 3. GPIO — port cluster 0x03FFF0xx

Port registers at `0x03FFF008 / 024 / 028-02E / 044-04C / 0E0-0E2` — BT-baseband
reset/enable lines, status inputs and the like.

## The FDIM text channel (important for repurposing)

The string table isn't just status text — it contains **template/placeholder rows** the BPM
streams to the FDIM to render arbitrary lists and text:

```
HEADER IN MEDIA TEXT      HEADER IN MENU TEXT        HEADER IN PAIRED PHONE LIST
  LINE 1 LINE 1 …           LINE 1 LINE 1 …            1. ITEM 1 IN LIST  …  8. ITEM 8 IN LIST
  LINE 2 … LINE 4 …         LINE 2 … LINE 4 …
```

i.e. the BPM already owns a generic **"header + 4 lines" media/menu channel and an 8-item
list channel** on CAN that the FDIM draws. That is a ready-made, OEM-rendered text pipe to
the dash — reusable for things that have nothing to do with phones (see
`BlueBridge-repurpose.md`).

## Reproduce

```bash
python3 phf/phf2bin.py bpm/8R29-14D511-AB.PHF bpm/bpm.bin      # verifies sum16 = 0xE5C3
python3 analysis/survey.py bpm/bpm.bin 0x4500 0x1f000          # vectors, strings, SFR footprint
python3 analysis/adis.py  bpm/bpm.bin 0x17c4c 0x17d40          # UARTA1 RX ISR (SFR-annotated)
python3 bpm/decode_bpm_tables.py bpm/bpm.bin bpm/out           # -> bpm/out/bpm-tables.json
```
