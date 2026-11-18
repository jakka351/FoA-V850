# bpm/ — FG Falcon Bluetooth Phone Module (8R29-14D511 / BPM, module ID 0x781)

Reverse-engineering of the FG Falcon **BPM (Bluetooth Phone Module)** — a headless Renesas
V850ES node that bridges an external **Bluetooth baseband (UARTA1 serial)** to the car's
**MS-CAN bus (aFCAN @ 0x03FEC000)**, and renders its phone UI on the FDIM over a CAN text
channel. Same PHF/V850ES/aFCAN world as the FDIM and AIM units, so the repo's existing
toolchain applies directly.

```
bpm/
  8R29-14D511-AB.PHF      original firmware (Ford PHF, FILE CHECKSUM 0xE5C3)
  bpm.bin                 phf2bin output (253,704 bytes, sum16 verified 0xE5C3)
  decode_bpm_tables.py    vectors + strings + CAN cfg + interface map -> out/bpm-tables.json
  BPM-analysis.md         firmware teardown (interfaces, ISRs, image map, method notes)
  BlueBridge-repurpose.md repurpose spec: BT-SPP <-> MS-CAN gateway + OEM dash text injector
  out/
    bpm-tables.json       machine-readable decode
    strings.txt           full string sweep (strings -n4 -tx)
    survey.txt            survey.py output (vectors, strings, SFR footprint)
```

## Quick start
```bash
python3 phf/phf2bin.py bpm/8R29-14D511-AB.PHF bpm/bpm.bin     # verify sum16 = 0xE5C3
python3 analysis/survey.py bpm/bpm.bin 0x4500 0x1f000         # vectors/strings/SFRs
python3 analysis/adis.py  bpm/bpm.bin 0x17c4c 0x17d40         # UARTA1 RX ISR, SFR-annotated
python3 bpm/decode_bpm_tables.py bpm/bpm.bin bpm/out          # -> bpm/out/bpm-tables.json
```

## TL;DR of the teardown
- **MCU:** Renesas V850ES. App/vector base `0x4500`, reset → `0x5BE0`. 3 live vectors.
- **BT serial:** UARTA1 (`UA1STR 0xFFFFFA14`, `UA1RX 0xFFFFFA16`), RX ISR `0x17C4C`,
  300-byte RX ring + framing state machine → link to an external BT baseband (host protocol,
  not named in-image; HFP/pairing/voice-tag logic lives in the V850).
- **CAN:** aFCAN `0x03FEC000` (cfg reg `0x03FEC058`), MS-CAN 125 kbit/s, module ID `0x781`;
  bit-timing block at `0x3DF00` (`a5 05 1e 02 00 02 e5 fd`).
- **Dash channel:** BPM paints a "header + 4 lines" media/menu channel and an 8-item list on
  the FDIM — a ready-made OEM text pipe to the dash (reused by BlueBridge).
- Author credits in-image: S/W **Adam Brown**, H/W **Mustafa Mustafa**.

See `BlueBridge-repurpose.md` for the repurpose: a transparent **Bluetooth-SPP ⇄ MS-CAN**
gateway on OEM hardware that also writes arbitrary text to the factory FDIM.
