# patch/ — BPM SPP ⇄ MS-CAN bridge patch kit

Turns the FG Falcon BPM (`8R29-14D511`) into a Bluetooth-serial ⇄ MS-CAN bridge by
injecting a shim into free flash and (optionally) hooking the UART-RX and CAN-RX paths.

```
phfpatch.py            Ford PHF parse / byte-patch / add-region / re-checksum. Round-trips byte-exact.
spp_can_bridge.s       the bridge shim (V850ES asm): uart_putc, can0_tx, bridge_rx, bridge_cantx
build_spp_can_phf.py   inject shim + (opt) arm detour hooks + emit a re-checksummed PHF + verify
```

## Build

```bash
# assemble the shim with your V850 toolchain
v850-elf-gcc -c -o spp.o patch/spp_can_bridge.s
v850-elf-objcopy -O binary spp.o shim.bin

# SAFE build: shim lives in free flash, hooks NOT armed -> module behaves exactly stock
python3 patch/build_spp_can_phf.py --shim shim.bin -o bpm/SW-8R29-14D511-AB-SPPCAN.PHF

# after you've validated the shim on the emulator / bench, arm the detour hooks:
python3 patch/build_spp_can_phf.py --shim shim.bin --arm -o bpm/SW-8R29-14D511-AB-SPPCAN.PHF
```

`SW-8R29-14D511-AB-SPPCAN.PHF` in `bpm/` right now is the **safe, unarmed** build (64-byte
reserved stub at `0x1EB68`, `FILE CHECKSUM 0xE642`, every stock byte below `0x1EB68`
untouched, verified to round-trip through `phf2bin`). Replace the stub with your assembled
`shim.bin` via `--shim`.

## What's proven vs. what needs a bench capture

**[RE-VERIFIED] from the image** (`bpm/BPM-analysis.md`):
- free-flash injection window `0x1EB68–0x3DF00` (127,896 B of 0xFF)
- UART-RX ISR `0x17C4C`; CAN0 INTC0 ISR `0xED64` → dispatcher `0xEA44` → extractor `0xEE66`
- aFCAN CAN0 base `0x3FEC000`, `C0INTS 0x3FEC058`, message buffers `0x3FEC100 + n·0x20`
- the PHF checksum math + record layout (phfpatch round-trips to `0xE5C3` with zero patches)
- the branch encoder (self-tests byte-for-byte against real firmware `jarl`/`jr`)

**[VERIFY] before you arm hooks and flash** — marked inline in `spp_can_bridge.s`:
1. **Baseband link.** The UART framing to the BT baseband and how to put it in a
   transparent data/SPP mode is *not* in this image (no AT/CSR strings; TX goes through a
   pointer in the INTUA1T path). Scope `UA1TX`/`UA1RX` at boot to get baud + framing. Until
   this is known, the bridge can move bytes but the baseband won't be in pass-through.
2. **UARTA1 TX-ready bit** in `uart_putc` (which `UA1STR`/`UA1CTL0` bit = buffer empty).
3. **aFCAN TX buffer config** in `shim_init`/`can0_tx`: free TX buffer index, `MCONF` TX
   direction, `MCTRL` TRQ bit, and the `MIDL/MIDH` standard-ID split — confirm against the
   Renesas aFCAN manual and the fw's own CAN init (mask setup at `0xF21C`).
4. **Hook trampoline offsets** in `build_spp_can_phf.py --arm` must match your shim's symbol
   map (shim_init / trampoline_rx / trampoline_cantx), and each trampoline must re-execute
   the instruction the hook overwrote before returning.

## ⚠️ Recovery

Flashing an unvalidated image can brick the module. Validate first on the repo's V850ES
emulator (model the baseband as a UART peer, run `bpm.bin` on the virtual MS-CAN bus), and
only flash a module you can recover in boot/BDM mode. The factory bootloader may also enforce
more than the FILE CHECKSUM (e.g. a signature); if so a re-checksummed image alone is rejected.

## Host protocol (once the baseband is in data mode)
```
host -> module :  AA  id_hi id_lo  dlc  d0..d7      -> transmitted on MS-CAN
module -> host :  55  id_hi id_lo  dlc  d0..d7      for every MS-CAN frame received
```
