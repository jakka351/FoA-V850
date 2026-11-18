# patch/ — BPM SPP ⇄ MS-CAN bridge patch kit

Turns the FG Falcon BPM (`8R29-14D511`) into a Bluetooth-serial ⇄ MS-CAN bridge by injecting a
V850ES shim into free flash and splicing four detour hooks into the stock firmware.

```
phfpatch.py            Ford PHF parse / byte-patch / add-region / re-checksum (round-trips byte-exact)
spp_can_bridge.s       the bridge shim: trampolines + bridge_rx/can0_tx/bridge_cantx/tx_putc/tx_drain/shim_init
build_spp_can_phf.py   assemble shim -> inject -> (--arm) hooks -> re-checksum PHF -> verify (objdump + round-trip)
```

Full design, verified primitives, and the bench-validation checklist are in
[`../BRIDGE-SPEC.md`](../BRIDGE-SPEC.md). Extraction evidence is in
[`../../out/extract-primitives.md`](../../out/extract-primitives.md).

## Build

```bash
# the flashable firmware — four hooks armed + HMI strings rebranded (bench-validate before flashing):
python "bpm/Bluetooth Serial CAN/patch/build_spp_can_phf.py" --arm -o bpm/8R29-14D511-JL-SPPCAN.PHF

# optional dormant variant (shim present, no hooks -> behaviourally stock): drop --arm
# optional stock HMI strings instead of the rebrand: add --no-brand
```

The builder runs the V850 cross-toolchain itself (`v850-elf-as/ld/objcopy/nm` under
`C:/SysGCC/v850-elf/bin`, override with `--toolchain` or `$V850_BIN`), so there is no hand-kept
`shim.bin` or offset table — the hook targets come from the assembled symbol map. It then
**verifies** the result and fails the build if anything is off:

- the branch encoder self-tests byte-for-byte against real firmware `jr`/`jarl` instructions;
- arming **aborts** unless the stock bytes at each hook site match the instruction we expect to
  displace (wrong base image → no patch);
- after emit, it `phf2bin`s old+new and confirms **only** the shim region and the armed hook
  sites changed, then `objdump`s each hook to confirm the jump/target.

- the HMI strings are rebranded in place, each validated to fit its flash slot (NUL-padded, no
  spill into the neighbouring string).

Current result — `verify: PASS`:

```
8R29-14D511-JL-SPPCAN.PHF : 1180-byte shim @0x1EB68, 4 hooks, 29 strings rebranded, FILE CHECKSUM 0x2118
```

## The four hooks (see BRIDGE-SPEC §4)

| Site | Displaced instr | → trampoline | Purpose |
|---|---|---|---|
| `0x17C7A` | `st.b r13,0[r11]` | `trampoline_rx` | tee each UART-RX byte to the framer |
| `0xF77E` | `st.w r6,0[sp]` | `tramp_f77e` | tee each received OEM-ID CAN frame to the host |
| `0xEE66` | `st.w r6,0[sp]` | `tramp_ee66` | tee catch-all buffers 30/31 (for the optional full sniff) |
| `0x12BFA` | `jarl 0x15D14` | `tramp_drain` | drain the TX ring to UART in task context (stock call preserved) |

Each trampoline re-executes the displaced instruction and restores every register the host relies
on, so stock behaviour (HFP, pairing, NM) is preserved alongside the bridge.

## ⚠️ Recovery

Reverse-engineered modification — a bench project, not a drive-ready image. Validate first
(ideally on the repo’s V850ES emulator with a modelled baseband) and only flash a module you can
recover in boot/BDM mode. The factory bootloader may enforce more than the FILE CHECKSUM. Work
through `../BRIDGE-SPEC.md` §6 before arming/flashing.

## Host protocol (once the baseband is in data mode)

```
host -> module :  AA  id_hi id_lo  dlc  d0..d7      -> transmitted on MS-CAN (standard 11-bit ID)
module -> host :  55  id_hi id_lo  dlc  d0..d7      for every MS-CAN frame received
```
