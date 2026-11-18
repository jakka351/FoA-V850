# BPM SPP⇄CAN modified firmware — what it does & how it works

**File:** `SW-8R29-14D511-AB-SPPCAN.PHF`
**Base:** FG Falcon Bluetooth Phone Module (BPM), `8R29-14D511-AB`, Renesas V850ES, MS-CAN ID `0x781`
**Status:** SAFE build — bridge shim present in free flash, **detour hooks NOT armed** (behaves exactly like stock until you arm them)

---

## What it does

Turns the factory BPM into a **transparent Bluetooth-serial ⇄ MS-CAN bridge**. Once armed,
a paired phone/laptop talking to the module's Bluetooth serial link can:

- **send** CAN frames onto the car's MS-CAN bus, and
- **receive** every MS-CAN frame the module sees,

over a tiny byte protocol. It's a factory-hardware, factory-connector CAN gateway — no
aftermarket box, and the module stays a normal bus citizen (keeps its NM heartbeat and
`0x781` ID), so the car doesn't flag a missing module.

Host protocol over the BT serial link:

```
phone -> module :  AA  id_hi id_lo  dlc  d0 d1 d2 d3 d4 d5 d6 d7   (12 bytes)  -> MS-CAN TX
module -> phone :  55  id_hi id_lo  dlc  d0 d1 d2 d3 d4 d5 d6 d7               for each MS-CAN RX frame
```

`AA`/`55` are the sync bytes; `id_hi:id_lo` is the 11-bit standard ID; `dlc` is the data
length; then 8 data bytes.

---

## How it works

### 1. Where the code lives
The stock app ends at `0x1EB67`; everything from `0x1EB68` to `0x3DF00` is erased flash
(`0xFF`). The bridge shim is injected there — it doesn't touch a single stock byte, so the
original firmware is bit-for-bit intact below `0x1EB68`. The PHF is re-emitted with every
per-record checksum and the header `FILE CHECKSUM` recomputed so the bootloader accepts it.

### 2. How it hooks in (when armed)
Two **detour hooks** splice the shim into the stock execution paths found during the
teardown:

- **Receive-from-phone** → hook in the **UARTA1 receive ISR at `0x17C4C`**. The stock ISR
  already pulls each byte from `UA1RX (0xFFFFFA16)` into a ring buffer; the hook also hands
  that byte to `bridge_rx`.
- **Receive-from-bus** → hook in the **CAN0 RX path** (`INTC0` ISR `0xED64` → dispatcher
  `0xEA44` → frame extractor `0xEE66`). After the stock code has a frame in a message
  buffer, the hook calls `bridge_cantx` to stream it out to the phone.

Each hook overwrites one 4-byte instruction with a `jr` into a shim trampoline; the
trampoline re-executes the displaced instruction, does the bridge work, then returns — so
the stock behaviour (hands-free, pairing, etc.) is preserved alongside the new path.

### 3. The shim routines
- **`bridge_rx(byte)`** — a small state machine. Waits for the `AA` sync byte, then collects
  the next 11 bytes (`id_hi,id_lo,dlc,d0..d7`) into a RAM staging buffer. On the 11th byte it
  calls `can0_tx` and resets.
- **`can0_tx(id,dlc,*data)`** — loads a dedicated aFCAN transmit message buffer
  (`0x3FEC100 + n·0x20`): writes the ID (`MIDL/MIDH`), DLC (`MDLC`), copies the 8 data bytes
  (`MDATA0..7`), then sets the transmit-request bit in `MCTRL`. The controller sends it on
  MS-CAN.
- **`bridge_cantx(bufindex)`** — reads the just-received frame straight out of its aFCAN
  message buffer and emits `55 + id + dlc + 8 data` by calling `uart_putc` for each byte.
- **`uart_putc(byte)`** — polls the UARTA1 transmit-ready bit, then writes the byte to
  `UA1TX (0xFFFFFA18)` — i.e. out the Bluetooth serial link to the phone.

### 4. Data flow end to end
```
 phone/laptop  --BT SPP-->  UARTA1 RX ISR(0x17C4C) -> bridge_rx -> can0_tx -> aFCAN -> MS-CAN bus
 MS-CAN bus   --> aFCAN INTC0(0xED64) -> extractor(0xEE66) -> bridge_cantx -> uart_putc -> UARTA1 TX --BT SPP--> phone
```

---

## Before you arm / flash it

This is a reverse-engineered modification; treat it as a bench project, not a drive-ready
image. Confirm these first (all marked `[VERIFY]` in `patch/spp_can_bridge.s`):

1. **The Bluetooth baseband link.** The UART framing to the BT baseband and how to put it in
   a transparent data/SPP mode aren't in the firmware image — capture the baseband link
   (scope `UA1TX`/`UA1RX` at boot) to confirm baud + framing. Until then the bridge moves
   bytes but the baseband won't be passing SPP data through.
2. **UARTA1 TX-ready bit** used in `uart_putc`.
3. **aFCAN TX-buffer config** (free buffer index, `MCONF` direction, `MCTRL` TRQ bit,
   `MIDL/MIDH` ID split) — check against the Renesas aFCAN manual and the fw's own CAN init.
4. Arm the hooks with `build_spp_can_phf.py --arm` only after validating the shim (ideally on
   the V850ES emulator with a modelled baseband), and **only flash a module you can recover in
   boot/BDM mode** — the bootloader may enforce more than the FILE CHECKSUM.

The PHF shipped here is the **unarmed** build: safe to inspect and flash-test because the
shim is dormant until the hooks are installed.