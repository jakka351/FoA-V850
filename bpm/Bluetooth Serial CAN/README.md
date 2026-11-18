# BPM SPP⇄CAN modified firmware — what it does & how it works

**Base:** FG Falcon Bluetooth Phone Module (BPM), `8R29-14D511-AB`, Renesas V850ES, MS-CAN ID `0x781`
**Flashable firmware:** `8R29-14D511-JL-SPPCAN.PHF` — four detour hooks armed + HMI strings
rebranded (the live bridge). The builder can also emit a dormant/stock-string variant via flags
(see `patch/`); the only other PHF kept in `bpm/` is the untouched stock image `8R29-14D511-AB.PHF`.

**Authoritative spec:** [`BRIDGE-SPEC.md`](BRIDGE-SPEC.md) (verified primitives, RAM map, bench checklist).

---

## What it does

Turns the factory BPM into a transparent **Bluetooth-serial ⇄ MS-CAN bridge** on factory
hardware and the factory connector. Once armed, a paired phone/laptop on the module’s Bluetooth
serial link can **send** standard-ID CAN frames onto the car’s MS-CAN bus and **receive** the CAN
frames the module sees — over a tiny byte protocol, while the module stays a normal bus citizen
(keeps its NM heartbeat and `0x781` ID, HFP stack intact).

```
host → module :  AA  id_hi id_lo  dlc  d0..d7   (12 bytes) → MS-CAN TX
module → host :  55  id_hi id_lo  dlc  d0..d7              for each MS-CAN RX frame
```

`AA`/`55` sync; `id_hi:id_lo` = 11-bit standard ID; `dlc` + 8 data bytes. Default RX = the 9 IDs
the module already accepts; full promiscuous sniffing is an optional bench-gated step (BRIDGE-SPEC §7).

---

## How it works

**Where the code lives.** The stock app ends at `0x1EB67`; `0x1EB68…0x3DF00` is erased flash. The
~1.1 KB shim is injected there — no stock byte below `0x1EB68` is touched — and the PHF is
re-emitted with every per-record checksum and the header `FILE CHECKSUM` recomputed.

**How it hooks in (armed).** Four one-instruction detours, each re-executing the instruction it
displaces and preserving the registers stock relies on:

| Site | Displaced | Does |
|---|---|---|
| `0x17C7A` UART-RX ISR | `st.b r13,0[r11]` | hands each received byte to `bridge_rx` (the AA-framer) |
| `0xF77E` CAN-RX extractor | `st.w r6,0[sp]` | tees each received OEM-ID frame to `bridge_cantx` |
| `0xEE66` CAN-RX (catch-all) | `st.w r6,0[sp]` | tees buffers 30/31 (for the optional full sniff) |
| `0x12BFA` main loop | `jarl 0x15D14` | drains the TX ring to the UART in task context (stock call kept) |

**The data path** mirrors the stock design — ISRs enqueue, the main loop drains — so no interrupt
ever blocks on the slow UART:

```
phone ─BT→ UART-RX ISR → bridge_rx → can0_tx → CAN0 buffer 1 → MS-CAN
MS-CAN → CAN-RX ISR → bridge_cantx → TX ring → (main-loop drain) → UART-TX → phone
```

Key routines: `bridge_rx` (framer), `can0_tx` (`MIDH=id<<2`, `MIDL=0`, DLC, 8 data, `MCTRL 0x0100`
then `0x0200`; buffer 1 configured once like stock buffer 0), `bridge_cantx` (`id=MIDH>>2`, push
`55`+frame), `tx_putc`/`tx_drain` (single-producer/consumer ring, non-blocking bounded drain to
`UA1TX 0xFFFFFA17` gated on `UA1STR & 0x80`).

---

## Before you arm / flash

Reverse-engineered — a bench project, not a drive-ready image. The SAFE build is safe to inspect
and flash-test (shim dormant). Before arming/flashing, work through **BRIDGE-SPEC §6**: the
baseband’s transparent-SPP mode and baud (not in the image — scope `UA1TX`/`UA1RX`), the scratch-RAM
paint test, the main-loop drain rate (`TXDROP`), the `C0INFO` TX gate, and the bootloader’s
acceptance of a re-checksummed image. **Only flash a module you can recover in boot/BDM mode.**
