# BPM SPP⇄CAN bridge — implementation spec (corrected build)

Authoritative engineering note for the rewritten `spp_can_bridge.s` + `build_spp_can_phf.py`.
Every value below is **[VERIFIED]** against `bpm/bpm.bin` (disassembly cross-checked by hand and
by a 5-reader extraction workflow — see [out/extract-primitives.md](../out/extract-primitives.md))
unless tagged **[BENCH]** (correct per the static image, but confirm on hardware before flashing).

This replaces the earlier draft kit, which was a 64-byte no-op stub with an unworkable hook
mechanism. See “What changed vs the first draft” at the bottom.

---

## 1. What it is

The factory BPM becomes a transparent **Bluetooth-serial ⇄ MS-CAN** bridge while staying a
normal bus citizen (keeps its `0x781` NM identity, HFP stack untouched).

```
host → module :  AA  id_hi id_lo  dlc  d0..d7      (12 bytes) → transmitted on MS-CAN (std 11-bit)
module → host :  55  id_hi id_lo  dlc  d0..d7      for every MS-CAN frame the module receives
```

RX coverage of the default build is the **9 OEM IDs** the module already accepts
(`0x781,0x406,0x317,0x2FC,0x2F2,0x2E2,0x2CF,0x2CD,0x2C1`) plus anything routed to the two
catch-all buffers. Full promiscuous sniffing is an **optional, bench-gated** step (§7).

---

## 2. Verified primitives (the foundation the shim is built on)

| Primitive | Value | Evidence |
|---|---|---|
| aFCAN CAN0 base | `0x03FEC000` | `sj3_regs.json`, init `0xEF18` |
| Message buffer m | `0x03FEC100 + m*0x20` | `shl 5` @ `0xEE80`,`0xF5A8`,`0xF78C` |
| Buffer field offsets | `+8` MDLC, `+9` MCONF, `+A` MIDL₁₆, `+C` MIDH₁₆, `+E` MCTRL₁₆ | extractor `0xEE66`/`0xF77E` |
| **Std-ID encoding** | `MIDH=(id&0x7FF)<<2`, `MIDL=0`; RX `id=MIDH>>2` | table `0x48F8[0]=0x1E04=0x781<<2` |
| MCTRL set/clear | upper byte **sets** flag n, lower byte **clears** n; bit0 RDY, bit1 TRQ, bit2 DN | `0xEECE`,`0xF600`,`0xF4BE` |
| TX trigger | `MCTRL=0x0100` (set RDY) then `0x0200` (set TRQ) | `0xF600`/`0xF60C` |
| TX buffer config | `MCONF=0x01`, enable `MCTRL=0x0800` (stock buffer 0) | `0xF066`/`0xF072` |
| Free TX buffer | **buffer 1** (buffers 1–20 inert, `MCONF=0x00`) | init `0xF084–0xF0DC` |
| No auto-block-TX | `C0GMABT/GMABTD` never referenced | whole-image grep |
| UART1 status | `UA1STR=0xFFFFFA14`, **bit7 `0x80` = TX busy** (ready when clear) | polled TX `0x17A76` |
| **UART1 TX data** | `UA1TX=0xFFFFFA17` (not `0xFA18`) | stores `0x17AAA`, `0x17DC2` |
| UART1 RX data | `UA1RX=0xFFFFFA16`, byte in **r13** at `0x17C76` | RX ISR `0x17C76` |
| RAM | `0x3FFC000–0x3FFEFFF`; SP=`0x3FFEFA4`↓; gp=`0x0400400C`; ep=`0x3FFC000` | reset `0x5BE0` |
| CAN-RX dispatcher | vector `0x0B4` → `0xEB6C`; idx `(C0RGPT>>8)&0xFF` into r6 | `0xEB6C` |
| Main extractor | **`0xF77E`** (buffers <30, r6=idx); `0xEE66` only buffers 30/31 | `0xF77E`,`0xEE66` |
| Main-loop drain site | `0x12BFA` `jarl 0x15D14` inside loop `0x12BAE…0x12C02` | `0x12BFA` |

---

## 3. Shim architecture (`spp_can_bridge.s`, injected at `0x1EB68`)

Mirrors the stock architecture — **ISRs enqueue, the main loop drains** — so no interrupt
ever blocks on the slow UART.

```
 phone ─BT SPP→ UART1-RX ISR ─hook 0x17C7A→ bridge_rx (framer) ─→ can0_tx ─→ CAN0 buffer 1 ─→ MS-CAN
 MS-CAN ─→ CAN0-RX ISR ─hook 0xF77E/0xEE66→ bridge_cantx ─→ tx_putc ─→ [software TX ring]
                                                                              │
          main loop ─hook 0x12BFA→ tramp_drain → (stock 0x15D14) + tx_drain ──┘─→ UART1-TX → phone
```

Routines: `bridge_rx` (AA-sync framer), `can0_tx` (load buffer 1 + `0x0100`/`0x0200` trigger,
under `di/ei`, lazy `shim_init` on first use), `bridge_cantx` (read MB, push `55`+frame),
`tx_putc`/`tx_drain` (single-producer/single-consumer ring), `shim_init` (configure buffer 1
like stock buffer 0).

### RAM map (`0x3FFEFA4–0x3FFEFEF`, above SP — stack grows the other way) **[BENCH: paint-test]**
`RXSTATE` `FA4`·1 │ `RXBUF` `FA5`·11 │ `INITFLAG` `FB0`·1 │ `TXHEAD` `FB1`·1 │ `TXTAIL` `FB2`·1 │
`TXDROP` `FB3`·1 │ `TXRING` `FB4`·60. (`0x3FFEFF0–FFF` left as margin; `0x3FFEFFF` is used as a
pointer constant elsewhere.)

---

## 4. The four detour hooks (installed by `--arm`)

Each overwrites exactly one 4-byte instruction on an instruction edge; the builder refuses to
arm if the stock bytes don’t match, and each trampoline **re-executes the displaced instruction**
then returns to the next one. Registers the host relies on are saved/restored around every bridge
call, and `tramp_drain` preserves the stock routine’s return state across the drain.

| Site | Displaced | → | Return |
|---|---|---|---|
| `0x17C7A` | `st.b r13,0[r11]` (`4b6f0000`) | `jr trampoline_rx` | `0x17C7E` |
| `0xF77E` | `st.w r6,0[sp]` (`63370100`) | `jr tramp_f77e` | `0xF782` |
| `0xEE66` | `st.w r6,0[sp]` (`63370100`) | `jr tramp_ee66` | `0xEE6A` |
| `0x12BFA` | `jarl 0x15D14` (`80ff1a31`) | `jarl tramp_drain` | `0x12BFE` |

---

## 5. Build & verify

```bash
# the flashable firmware — armed hooks + rebranded HMI strings:
python "bpm/Bluetooth Serial CAN/patch/build_spp_can_phf.py" --arm -o bpm/8R29-14D511-JL-SPPCAN.PHF
# optional dormant variant (shim present, no hooks -> behaviourally stock): drop --arm
# optional stock HMI strings: add --no-brand
```

The builder assembles the shim with `v850-elf-as/ld` (at `C:/SysGCC/v850-elf/bin`, or
`--toolchain`/`$V850_BIN`), reads its symbol map, arms the hooks, rebrands the HMI strings in
place (length-checked), recomputes the PHF checksums, then **verifies**: round-trips through
`phf2bin`, confirms *only* the shim region + armed hook sites + rebranded string bytes changed,
and `objdump`s each hook site to confirm the jump target. The shipped **`8R29-14D511-JL-SPPCAN.PHF`**
(armed with 5 hooks + branded + hardcoded pairing PIN `1234`, FILE CHECKSUM `0x54D4`) reports
**verify: PASS**, with the injected bytes confirmed byte-identical to the assembled shim.

The build also makes the module **pair fully headless**: it hardcodes the 4-digit PIN (generator
`0x6934` → fixed `1234`, `--pin`/`--random-pin`) and arms a 5th hook (`--no-auto-pair` to skip) that
auto-enters pairing/discoverable at boot when no phone is paired — see [`PAIRING.md`](PAIRING.md).

### Adversarial review (applied)
A 4-dimension review + per-finding verification (8 agents) found one confirmed defect and one
robustness gap, both now fixed; register-preservation, `di/ei`, hook placement and the builder
were independently confirmed correct:
- **[fixed] sync byte never matched.** Stock reads UART RX with a *signed* `ld.b` (`0x17C76`), so
  `0xAA` arrives as `0xFFFFFFAA`; the full-word compare in `bridge_rx` never matched and the
  host→CAN direction was dead. `trampoline_rx` now masks the byte (`andi 0xFF, r13, r6`).
- **[fixed] partial-frame desync.** `bridge_cantx` now drops a whole 12-byte frame atomically
  (free-space check) instead of a single byte, so the ring can never emit a truncated frame.

---

## 6. Bench-validation checklist (do before flashing a recoverable module)

These are genuinely **not** resolvable from the static image:

1. **Baseband SPP mode.** How the V850↔Bluetooth-baseband UART is put into transparent data
   pass-through is not in the image (no AT/CSR strings). Scope `UA1TX`/`UA1RX` at boot. Until
   known, the shim moves bytes correctly but they may not traverse the BT link as SPP data.
2. **Baud.** `UA1CTL2` divisor `0x57` (87) is confirmed; the resolved baud needs the UART base
   clock. Affects TX throughput, not correctness.
3. **RAM freeness.** Paint `0x3FFC000–0x3FFEFA4`, exercise all BPM features, confirm
   `0x3FFEFA4–0x3FFEFEF` is never touched by stock.
4. **Main-loop drain rate.** Confirm `0x12BFA` runs often enough for your CAN→BT frame rate;
   watch `TXDROP`. If it saturates, raise baud or move the drain to a timer ISR (sole consumer).
5. **C0INFO gate.** Stock checks `C0INFO & 0x10` before transmitting (`0xF528`); confirm whether
   the shim should mirror this bus-state gate.
6. **Bootloader.** Confirm the factory loader enforces nothing beyond the FILE CHECKSUM (a
   re-checksummed PHF may still be rejected if it checks a signature). Only flash a module you can
   recover in boot/BDM mode.

---

## 7. Optional: full promiscuous sniff **[BENCH]**

Default RX = the 9 OEM IDs (via the `0xF77E` tee). To see *every* frame, widen one RX mask so a
catch-all buffer (30/31) accepts all IDs — those frames then arrive at `0xEE66` (already hooked).
Caveats to confirm on the bench: aFCAN stores a frame in the lowest matching buffer (OEM IDs still
go to 21–29 → `0xF77E`, so hook both — the build already does); whether masks can be rewritten
without entering init mode; and that post-processor `0xF6AC` doesn’t mishandle the extra traffic.

---

## What changed vs the first draft
- Real **UA1TX `0xFA17`** + TSF ready-bit (was wrong `0xFA18`, guessed bit).
- Real **std-ID encoding `id<<2`** in MIDH (was raw byte writes → wrong IDs).
- Real CAN-RX tee at **`0xF77E`** (was `0xEE66`, which only sees dormant buffers → teed nothing).
- **Instruction-aligned hooks that re-execute the displaced instruction**, with full register
  save/restore (was a 2-instruction-straddling overwrite with no trampoline → ISR corruption).
- **Non-blocking task-context TX drain** (was a blocking polled write inside the CAN ISR).
- **`shim_init` wired via lazy first-use** (was never called → TX buffer unconfigured).
- Builder **assembles the shim, verifies displaced bytes before arming, and objdump-verifies**
  (was pypcode-dependent with fixed offsets that didn’t match the shim).
