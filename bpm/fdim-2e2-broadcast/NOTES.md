# Mk2 FDIM → broadcast 0x2E2 (Mk1 ICC emulation) — patch notes

**Output:** `AR79-14D017-JL-2E2.PHF` (patched Mk2 FDIM, FILE CHECKSUM `0x613A`, `verify: PASS`).
Built by `build_fdim_2e2.py` from `fdim_2e2.s` + the repo `AR79-14D017-JL.PHF`.

## Why — the full chain (firmware + DBC proven)
1. The BPM NRC-`0x22`s every `$10 85/87` session via a pre-gate at `0x8C9C` that requires
   **CAN `0x406` byte[1] & 0x1F == 4**.
2. DBC: `0x406` = `SeatBeltStatus` from the **BEM**; byte[1] = `IgnitionState` (`1=Off, 2=Acc, 4=IgnitionOn`).
   So the BPM only unlocks sessions when the BEM reports **IgnitionOn (4)**.
3. In-car with ignition physically ON the BEM still isn't reporting `4` → it's *withholding* IgnitionOn
   because the ICC/audio-head looks incomplete.
4. The repo's Mk1 FDIM analysis (`docs/FDIM-AC-mono-analysis.md` §3.1) lists the Mk1 FDIM TX set
   `0x55C, 0x2E2, 0x2FC, 0x307, 0x313, 0x315`; the Mk2 (`AR79-14D017-JL`) TX set is
   `0x7AE, 0x55C, 0x425, 0x315, 0x313, 0x307, 0x2FF, 0x2FC, 0x2F6`. The **only** frame the Mk1 sends
   that the Mk2 doesn't is **`0x2E2`**.
5. ⇒ Restoring the `0x2E2` broadcast should make the ICC look complete → BEM reports IgnitionOn →
   BPM session gate opens. This patch does that.

## How it works
- Redirects the **CAN0 TX-complete vector** (slot 53 `@0x41D4`, was `jr 0x15C5E`) to a shim in free
  flash `@0x24840`.
- The shim (runs at interrupt entry): lazily configures the unused buffer **MB1 (`0x3FEC120`)** as a
  standard-ID TX buffer, rate-limits (every `RATE`=8 TX-complete IRQs), builds **`0x2E2`** on MB1
  (`MIDH=0x0B88, MIDL=0, DLC=8`, payload from the shim), fires it (`MCTRL 0x0100`→`0x0200`), then
  **chains into the real ISR** — it replicates the ISR's own `add -4,sp / st.w r1` so the interrupted
  context (incl. `r1`) is preserved, then jumps to `0x15C64` (just past the ISR's `r1` save).
- MB1's own completion is harmless: the FDIM scheduler (`0x15330`) looks up its per-buffer table for
  buffer 1, finds `0xFFFF` (unused), and exits.

## Build
```bash
python "bpm/fdim-2e2-broadcast/build_fdim_2e2.py"                       # default (idle/zero payload)
python "bpm/fdim-2e2-broadcast/build_fdim_2e2.py" --payload 00112233... # custom 8-byte 0x2E2 payload
```
Verifies: `phf2bin` round-trip, only the shim region + the 4 hook bytes changed, and the hook
disassembles to `jr 0x24848`.

## Bench test (FDIM is flashable/reversible — unlike the BPM)
1. Flash `AR79-14D017-JL-2E2.PHF` to the Mk2 FDIM (keep the stock image + a BDM recovery path).
2. With ignition ON, sniff MS-CAN: confirm **`0x2E2` now appears** periodically, and watch **`0x406`
   byte[1]** — if it flips to `0x04` (IgnitionOn), the theory holds and the BPM should accept `$10 85/87`.
3. Re-try the BPM diagnostic session.

## Caveats / tuning (both easy to refine, no re-RE)
- **Payload = all-zero — now CONFIRMED ground truth (2026-11-18), not a guess.** Three independent
  methods agree the Mk1 FDIM (`8R29-14D017-AC`) transmits `0x2E2` = `00 00 00 00 00 00 00 00` at idle/RUN:
  (1) the in-repo V850 emulator run on the real Mk1 image logged **64** `0x2E2` frames (~250 ms cadence,
  two RUN stimuli) all byte-zero, and proved the firmware *explicitly* clears the shared TX buffer for
  this id (the preceding `0x2FC` frame on the same mailbox carried non-zero data `BE 7F 21 00 1F 00 02 04`);
  (2) static disasm of the Mk1 TX builder (`0x1A662`; `0x2E2` = slot 19, data source `0x03FFE18C`) shows
  byte0 = a 3-bit mode enum (idle 0), byte1 = momentary button bits (idle 0), bytes 2–7 have **no writer**
  (zeroed at reset); (3) the BPM parses only `0x2E2` byte0 (mode) + byte1 (buttons) and accepts an all-zero
  frame (DLC ≥ 2). **No rolling counter, no checksum** in this id. So `--payload 0000000000000000` (the
  default) *is* the real Mk1 idle frame. Only a held-button/mode event (not modeled; would need a live Mk1
  bus capture) momentarily sets byte0/byte1 bits — irrelevant to a "module present" broadcast.
- **Cadence** ≈ once per 8 CAN-TX-complete IRQs (`RATE` in `fdim_2e2.s`); change it for a different rate.
- **The session gate is confirmed on `0x406`, not `0x2E2`.** Static RE of the BPM shows the only
  NRC-`0x22` / `$10` gate is the pre-gate at `0x8C9C` reading `0x406` byte1 `& 0x1F == 4` (IgnitionOn,
  sourced by the BEM); `0x2E2` is **never read** by the BPM's diagnostic code. So `0x2E2 → unlock` is
  **indirect**: it works only if restoring `0x2E2` makes the BEM assert IgnitionOn in `0x406`. The BEM
  flash can't be read (failed dumps), so that last hop stays a **hypothesis** — this is a reversible bench
  test, not a guaranteed fix. The TX-diff is re-confirmed: `0x2E2` is the *only* Mk1-TX id the Mk2 omits.
  If `0x406` doesn't flip to `4` with `0x2E2` present, the BEM wants **more than this one frame** (widen
  the Mk1↔Mk2 diff / capture a live Mk1 bus) — it is **not** a different `0x2E2` payload (that's now known).
