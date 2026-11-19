# Mk2 FDIM (AR79-14D017-JL.PHF) — CAN TX analysis (for BPM compat)

RE of the patched Mk2 FDIM firmware to find what it broadcasts vs the Mk1 FDIM the BPM expects.
V850ES / aFCAN CAN0 (MS-CAN 125k), software v3.3.1, image 262144 B. Addresses = file offsets.

## Image
- Vector/handler jump-table **0x4100–0x41FF** (64 × 4-byte `jr`); app code/data **0x4500–0x2483F**.
- Startup `0x148C4` → main `0x149AE`; `gp=0x0400310C`, `sp=0x3FFEFAC`, `ep=0x3FFB000`.
- aFCAN init `0x14CCA` (CAN0 only; table-driven, 32 buffers). MB fields: `+8 MDLC, +9 MCONF, +A MIDL, +C MIDH, +E MCTRL`.

## What the Mk2 FDIM TRANSMITS
Single HW TX buffer **MB0 @ 0x3FEC100**, time-multiplexed over a **12-slot table** (MIDH @0x69D8 /
DLC @0x69F0 / dataptr @0x69FC). Generic sender **0x1520C** (writes id/dlc/data then `MCTRL=0x0100,0x0200`);
scheduler **0x15330** (slots 11→0, pending-flags @gp-25027), kicked by CAN0-TX-done ISR **0x15C5E**
(vector slot 53 @ 0x41D4); app enqueues via **0x151B4/0x151E2**.

**Transmitted IDs: `0x7AE, 0x55C, 0x425, 0x315, 0x313, 0x307, 0x2FF, 0x2FC, 0x2F6`** (all DLC 8;
0x7AE/0x2FF/0x2F6 double-slotted). Payloads built in RAM (not in flash) — exact bytes need a bus/emu capture.

## Target-ID presence
| ID | Mk2 FDIM | |
|---|---|---|
| **0x2E2** | **ABSENT** | not in TX **or** RX tables — not a configured id at all (prime suspect ✓) |
| 0x2CF / 0x2CD / 0x2C1 | **ABSENT** | zero footprint (ICC HMI/button — Mk1 only) |
| 0x2FC | **TX (slot 9)** | the one BPM-listen id the Mk2 does send |
| 0x406 / 0x317 / 0x2F2 | RX only | received from other modules (same as the BPM) |

→ Of the BPM listen-set `{0x406,0x317,0x2FC,0x2F2,0x2E2,0x2CF,0x2CD,0x2C1}`, the Mk2 FDIM transmits
**only 0x2FC**. Consistent with the NRC-0x22 cause: the Mk1 FDIM broadcast `0x2E2` (± `0x2Cx`); the Mk2 doesn't.

## Injection plan (to add a 0x2E2 broadcast)
- **Free TX buffer:** MB1 @ 0x3FEC120 (and MB2 @ 0x3FEC140) are init-disabled, unused → use **MB1**.
- **Hook (cadence):** repoint vector slot 53 **@0x41D4** (`jr 0x15C5E`, bytes `81 07 8a 1a`) → shim in
  free flash; shim rate-limits with a RAM counter, builds+fires 0x2E2 on MB1, then `jr 0x15C5E`.
  Fixed-rate alt: a timer-tick ISR (slots 12/13/16 → 0xD0EE/0xCF1A/0xCD4C). Zero-code alt: retarget a
  spare TX slot's MIDH (slot 8 @0x69E8 `0x0BFC`→`0x0B88`) + adjust its RAM payload source.
- **Free flash for the shim:** **0x24840–0x3FFFB** (~110 KB of 0xFF); keep tail word @0x3FFFC.
- **Frame-build recipe (match FDIM):** MCONF=0x01; **MIDH=(id&0x7FF)<<2** (0x2E2 → **0x0B88**); MIDL=0;
  MDLC=len; copy data→MDATA0..7; **MCTRL=0x0100 then 0x0200**. Re-checksum the PHF (Ford sum16) on emit.

## 0x2E2 payload — RESOLVED (2026-11-18): all-zero is the real Mk1 frame
The **0x2E2 idle payload is `00 00 00 00 00 00 00 00`**, confirmed three ways: the V850 emulator run on
the real Mk1 image (64 frames, all zero), static disasm of the Mk1 TX builder (`0x1A662` slot 19: byte0 =
3-bit mode enum, byte1 = button bits, bytes 2–7 no writer — all idle 0), and the BPM RX path (parses only
byte0+byte1, accepts all-zero at DLC ≥ 2). **No counter, no checksum.** The BPM's own NRC-`0x22` gate keys
on **`0x406`** (IgnitionOn, from the BEM), *not* `0x2E2`. So the one remaining unknown is purely whether
the (unreadable) BEM asserts `0x406 = IgnitionOn` on `0x2E2` **presence** — a live Mk1-FDIM bus capture is
the only thing that could add a held-button/mode payload, which a "module present" broadcast doesn't need.
