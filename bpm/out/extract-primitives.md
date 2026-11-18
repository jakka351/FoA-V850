# BPM SPP<->CAN bridge — stock-firmware primitive extraction

Machine-generated consolidation of the 5-reader + synthesis workflow (run wf_9f94ffc0-20c),
cross-checked by hand against bpm/bpm.bin. Addresses are physical = bpm.bin file offsets.

## Consolidated spec


# BPM Bluetooth-serial ⇄ MS-CAN Bridge — Consolidated Firmware Implementation Spec

Target: FG Falcon BPM `8R29-14D511`, Renesas V850ES (uPD70F3372-class), little-endian.
Image `bpm/bpm.bin` = 0x3DF08 bytes, file offset == code address, app code starts at 0x4500.
Existing work in repo: `bpm/Bluetooth Serial CAN/patch/{spp_can_bridge.s, build_spp_can_phf.py, phfpatch.py}`.
All addresses below were re-verified by me against `bpm/out/code.asm` / raw bytes (quoted per item).

Host protocol (unchanged): phone→module `0xAA id_hi id_lo dlc d0..d7` ⇒ CAN TX; module→phone `0x55 id_hi id_lo dlc d0..d7` per CAN RX frame.

---

## 1. CAN TX — standard-11-bit ID write formula (CONFIRMED)

- Full 11-bit ID from host bytes: `id11 = ((id_hi << 8) | id_lo) & 0x7FF`.
- **`MIDHm = (id11 << 2) & 0xFFFF`** (16-bit store to MB+0x0C). **`MIDLm = 0x0000`** (16-bit store to MB+0x0A).
- Proven 3 independent ways: (a) TX ID table @0x4850 holds pre-shifted values — `0x1e24>>2=0x789`, `0x1504>>2=0x541`, `0x0b8c>>2=0x2e3`; (b) RX filter table @0x48f8[0]=`0x1e04`, `0x1e04>>2=0x781` = BPM module ID; (c) stock TX routine stores the table word ONLY to MIDH (`0xF568 st.h r10,-0x3ef4[r1]`), never MIDL. The sole MIDL writer in the image is the buf30/31 catch-all setup `0xF1D6`.
- **RX read-back:** `stdID = (MIDH >> 2) & 0x7FF`; `IDE = (MIDH >> 15) & 1` (all stock traffic is standard, IDE=0); extended would be `(MIDH[12:0]<<16)|MIDL`.

## 2. CAN TX — dedicated raw-TX buffer, MCONF, MCTRL (CONFIRMED)

- aFCAN CAN0 base `0x03FEC000`; message buffer m at **`0x03FEC100 + m*0x20`** (`0xEE70 mov 0x3fec100,r10`; `0xEE80 shl 0x5,r14`). Offsets: MDATA0..7 +0x00..07, **MDLC +0x08**, **MCONF +0x09 (byte)**, **MIDL +0x0A (16b)**, **MIDH +0x0C (16b)**, **MCTRL +0x0E (16b)** (verified by the 0xEE9C–0xEEC0 load displacements).
- **Claim buffer index in 1..20** (recommend **m=1**; m=15 as used by the current shim is equally safe). Stock configures buffers 1–20 as **MCONF=0x00, MCTRL=0x001E, RDY never set, enable bit never set** (`0xF0CA st.b r0,-0x3ef7` = MCONF=0, `0xF0C2 st.h r7(=0x1e)`), i.e. inert/unused. Do NOT use buffer 0 (stock NM/FDIM TX) or 21–31 (RX filters).
- **MCONF_TX = 0x01** (byte) — copy stock TX buffer 0 (`0xF066 st.b r18(=1),-0x3ef7[r1]`). The shim's current `MCONF_TX=0x00` is insufficient.
- aFCAN MCTRL set/clear model (CONFIRMED): lower-byte bit n **clears** flag n, upper-byte bit (8+n) **sets** flag n. bit0=RDY, bit1=TRQ, bit2=DN.
- **One-time buffer init** (replicate stock buf0 `0xF018–0xF072`): `MCTRL=0x0002` (clear TRQ) → poll-write `MCTRL=0x0001` until `(MCTRL&1)==0` → `MCTRL=0x001E` (clear stale DN/MOW; buf0 used 0x001C) → `MCONF=0x01` → `MCTRL=0x0800` (enable; optional for a pure-polling shim).
- **Per-frame TX** (replicate `0xF550–0xF60C`): poll RDY clear → `MDLC=dlc` → `MDATA0..7` → `MIDL=0x0000` → `MIDH=(id11<<2)` → **`MCTRL=0x0100` (set RDY)** → **`MCTRL=0x0200` (set TRQ)** (`0xF600` then `0xF60C`). TRQ-set is **0x0200**, not 0x0100. The current shim writes only 0x0100 and labels it "SET_TRQ" — it sets RDY and never fires TRQ, so it never transmits. This is a must-fix.
- Busy check: `MCTRL & 0x0003` nonzero ⇒ TX in progress (RDY|TRQ); hardware clears both on completion.
- No automatic block transmission: C0GMABT(0x3FEC006)/C0GMABTD(0x3FEC008) are never referenced; TX is per-buffer single-shot TRQ.
- Because the 5 CAN-init call sites rewrite ALL 32 buffers, the shim must **re-assert the TX buffer's MCONF + init sequence on every transmit** (idempotent; buffers 1–20 are never otherwise driven).
- Optional gate: stock checks `C0INFO(0x3FEC053) & 0x10` at `0xF528` and aborts TX if set (likely bus-off / not-ready). Shim may mirror this; semantics are uncertain (bench).

## 3. CAN RX — masks must be widened for a full sniffer (CONFIRMED)

- All four masks are **0x1FFFFFFF** (H=0x1FFF from table @0x4AC0, L=0xFFFF from table @0x4AC8; written `0xF21C–0xF28C`). aFCAN mask bit=1 ⇒ compare, 0 ⇒ don't-care, so stock is **exact-match, NOT accept-all**. Buffers 21–29 exact-match 9 OEM IDs (0x781,0x406,0x317,0x2FC,0x2F2,0x2E2,0x2CF,0x2CD,0x2C1); buffers 30–31 exact-match ID 0x000 (dormant).
- **To sniff everything:** zero the masks used by the catch-all buffers 30/31. MCONF mask-select (bits[4:3], LIKELY): buf21-29=0x09→MASK1, buf30=0x11→MASK2, buf31=0x19→MASK3. Write **C0MASK2L=0x3FEC044, C0MASK2H=0x3FEC046, C0MASK3L=0x3FEC048, C0MASK3H=0x3FEC04A all = 0x0000**. Leave MASK1 intact so the 9 OEM buffers (21–29) keep feeding the HMI.
- Cleanest method (avoids needing CAN init-mode at runtime): **patch the flash mask tables** so stock init programs widened masks — set halfwords **0x4AC2 & 0x4AC4 (MASK2H/MASK3H)** and **0x4ACA & 0x4ACC (MASK2L/MASK3L)** to `00 00`.
- Priority caveat (LIKELY, bench-verify): aFCAN stores each frame in the lowest matching buffer. With MASK2/3 widened, frames with the 9 OEM IDs still land in buffers 21–29 (→ extractor 0xF77C); all other IDs land in 30/31 (→ extractor 0xEE66). **For complete coverage, tee at BOTH extractors** (§4). Two widened catch-all buffers (30 and 31) also give double-buffering against back-to-back frames.
- Preserve bit timing: C0BRP=0x09 (table 0x4ABC), C0BTR=0x3508 (0x4ABE), C0IE=0x2700 (0x4ABA) — ~125 kbit/s MS-CAN.

## 4. CAN RX — tee points & index register (CONFIRMED)

- Real RX chain: **vector 0x0B4 @0x45B4 `jr 0xEB64`** → body `0xEB6C` (clears C0INTS bit1 `0xEB74`, loops C0RGPT `0x3FEC060`, `idx=(C0RGPT>>8)&0xFF`). idx<30 → `r6=idx; jarl 0xF77C`; idx>=30 → `r6=idx; jarl 0xEE64(→0xEE66)` then `jarl 0xF6AC`.
- **At BOTH extractor entries the RX buffer index is in `r6`.**
- **Tee #1 (buffers 30/31) — hook body entry `0xEE66`**, displaced instruction `st.w r6, 0x0[sp]` = bytes **`63 37 01 00`**, return to **`0xEE6A`**. r6 = index (live). `0xEE66` reached once per frame, straight-line, no internal loop. Extractor copies the frame to RAM scratch then clears DN via `MCTRL=0x0004` (`0xEECE`).
- **Tee #2 (buffers 21–29) — hook body entry `0xF77E`**, displaced instruction `st.w r6, 0x0[sp]` = bytes **`63 37 01 00`**, return to **`0xF782`**. r6 = index (set at dispatcher `0xEBA2 mov r15,r6`). Needed only if OEM IDs must also reach the phone.
- Trampoline for either tee: save r6 → `mov r6,r6`(arg) → `jarl bridge_cantx` → restore r6 → re-execute `st.w r6,0x0[sp]` → `jr <return>`. Read the frame directly from the live buffer (`MB_BASE + idx*0x20`) at entry (DN not yet cleared). Emit `0x55, id_hi=(stdID>>8), id_lo=(stdID&0xFF)` where `stdID=(MIDH>>2)&0x7FF`, `dlc`, 8 data. The current shim emits raw MIDH/MIDL low bytes (wrong: sends the shifted value and MIDL=0) — must decode via `>>2`.

## 5. UART TX — byte-send method, register + bit (CONFIRMED)

- **UA1TX = 0xFFFFFA17** (NOT 0xFFFFFA18). Verified by `0x17AAA 405717fa st.b r10,-0x5e9[r0]` and `0x17DC2 407717fa st.b r14,-0x5e9[r0]`, both annotated UA1TX, plus `analysis/sj3_regs.json`. A grep for any access to 0xFFFFFA18 returns nothing. The shim `.equ UA1TX,0xFFFFFA18` and the ram-init reader are wrong.
- **UA1STR = 0xFFFFFA14; TX-ready = `(UA1STR & 0x80) == 0`** (bit7 = TX busy). Stock polled path: `0x17A76 tst1 0x7,UA1STR` then store at `0x17AAA` only when bit7==0.
- **Recommended writer (self-contained, polled, non-blocking usage):**
  ```
  uart_putc:  ; r6 = byte
  1: ld.bu UA1STR[r0], r7
     andi  0x80, r7, r7
     bne   1b            ; loop WHILE busy (bit7 set)
     st.b  r6, UA1TX[r0] ; UA1TX = 0xFFFFFA17
     jmp   [lp]
  ```
  The current shim uses `be 1b` (loops while ready, writes while busy) — inverted; must be `bne 1b`.
- Do NOT call the stock enqueue at 0x17AB6 (CALLT cooperative-scheduler coupled). If the shim writes UA1TX directly while stock INTUA1T is live, mask it: **UA1TIC=0xFFFFF168 bit6=1** (INTUA1T vector 0x45A4→0x17D9C). Stock TX ring (only if reused): base 0x03FFDC08, size 0x12C, head(u16) 0x03FFDD3A, tail(u16) 0x03FFDD38.

## 6. UART RX — instruction-aligned hook (CONFIRMED)

- Hook the normal-receive store: **HOOK_ADDR = 0x17C7A**, displaced instruction `st.b r13, 0x0[r11]` = bytes **`4B 6F 00 00`** (4 bytes), **return to 0x17C7E**. The RX byte is in **r13** (read at `0x17C76 ld.b -0x5ea[r0],r13` = UA1RX 0xFFFFFA16).
- Live-out regs at the hook = **r11 (ring ptr) and r13 (byte)**; the trampoline must preserve both and re-execute the `st.b`; r10/r12 are dead (usable as scratch).
- The ISR re-enables interrupts at body entry (`0x17C54 ei`), so the trampoline runs with interrupts on and can be preempted — keep it short/reentrancy-tolerant.
- Do NOT hook 0x17C4C (the entry stub `add -0x4,sp / st.w r1 / br` — a 4-byte jr there corrupts the stack setup). The current `build_spp_can_phf.py` uses `UART_RX_ISR=0x17C4C`; change it to 0x17C7A.
- Alt hook 0x17C76 (`ld.b UA1RX,r13`, bytes `00 6F 16 FA`) if the byte must be re-read. This single hook does NOT cover the error/state paths (0x17CB4/0x17CBE/0x17CD8); those are not the normal data path.

## 7. Free flash + scratch RAM (CONFIRMED / LIKELY)

- **Shim free flash: 0x1EB68 .. 0x3DEFF = 127,896 bytes of 0xFF** (verified by scanning the image). The 8-byte CAN config blob lives at **0x3DF00** (`a5 05 1e 02 00 02 e5 fd`) — the builder's `assert SHIM_ORG+len < 0x3DF00` must hold.
- RAM: gp=**0x0400400C** (`0x5BE6`), ep=0x03FFC000 (`0x5BF4`), **initial SP=0x03FFEFA4** (`0x5BEE`), RAM 0x03FFC000–0x03FFEFFF cleared to 0 at reset (`0x5C48` loop to 0x3FFF000). Highest observed static global ≈ 0x03FFED27.
- **Scratch block: reserve 0x03FFEE20 .. 0x03FFEFA3 (388 B)** by lowering SP: patch the immediate in `0x5BEE mov 0x3ffefa4,sp` (file bytes `23 06 a4 ef ff 03`) — change `a4 ef ff 03` at file offset 0x5BF0 to `20 ee ff 03`. (Minimal alt: SP=0x03FFEE64 for 320 B.) The current shim puts RXSTATE/RXBUF at 0x03FFEFC0/C1, which is ABOVE SP in the referenced 0x3FFEFxx tail (0x3FFEFFF is used as a pointer constant) — must relocate into the reserved block.

## 8. shim_init strategy (CONFIRMED reasoning)

- **Lazy self-init.** No clean one-time "after CAN+UART up" hook exists: early init 0x183D8 is clock-only; CAN init 0xEED8 has 5 callers; UART init 0x17588 has 8. Guard on a magic word in scratch RAM; on first use configure the TX buffer, and **re-assert TX-buffer config before every transmit** (reinit rewrites all buffers). Optional pre-main hook: redirect `0x5C62 jarl 0x12bac` (bytes `80 ff 4a cf`) to a shim pre-init that sets up scratch then jumps to 0x12BAC (CAN buffer part still must be lazy).

## 9. Non-blocking drain architecture (RECOMMENDED)

- Both ISRs must stay short. The current `bridge_cantx` calls `uart_putc` 12× with a blocking poll inside the CAN RX ISR (~1 ms at ~115 kbaud) — this can overrun the 2 catch-all buffers and disturb NM timing. Fix:
  - **CAN RX tee (ISR):** format `0x55+id+dlc+8data` into a shim-owned TX ring in scratch RAM; return. No UART polling in the ISR.
  - **UART RX hook (ISR):** run the host-frame state machine only; on a complete `0xAA` frame, enqueue a CAN-TX request (or call `can0_tx`, which is itself non-blocking: writes buffer + sets TRQ, hardware sends async). No blocking on CAN RDY in the ISR.
  - **Drain in the main super-loop** (periodic, interrupt-free): `main=0x12BAC`, body loops via `br 0x12BAE`. Repoint one per-iteration call (e.g. `0x12BFA jarl 0x15d14`, bytes `80 ff 1a 31`) to a shim drain that (a) writes queued UART bytes while `(UA1STR&0x80)==0` and ring non-empty, (b) services queued CAN-TX requests when the TX buffer is free, then tail-calls/returns into 0x15d14. Optionally also drain a bounded count (≤4 bytes) at the top of the UART-RX hook.

## 10. Required fixes to the existing project

Shim `spp_can_bridge.s`: (1) UA1TX 0xFFFFFA18→0xFFFFFA17; (2) `uart_putc` `be 1b`→`bne 1b`; (3) `can0_tx` ID: `id11=((id_hi<<8)|id_lo)&0x7FF`, `MIDH(16b)=id11<<2`, `MIDL(16b)=0` (remove raw byte split); (4) add `MCTRL=0x0200` after `0x0100` (set TRQ); (5) `MCONF_TX` 0x00→0x01; (6) `shim_init` must run the full buf0 init sequence, not just one MCONF store; (7) relocate RXSTATE/RXBUF into 0x03FFEE20+ reserved block; (8) `bridge_cantx` decode `stdID=(MIDH>>2)&0x7FF` and enqueue rather than blocking-poll.
Builder `build_spp_can_phf.py`: (9) `UART_RX_ISR` 0x17C4C→0x17C7A (return 0x17C7E); (10) keep `CAN_EXTRACT=0xEE66` (return 0xEE6A) and add a second hook at 0xF77E (return 0xF782) if OEM IDs must be teed; (11) also apply the MASK2/MASK3 flash-table zeroing (0x4AC2/0x4AC4/0x4ACA/0x4ACC → 0x0000) for the sniffer; (12) trampoline symbol-map offsets must match the assembled shim; each trampoline re-executes the displaced `st.w r6,0x0[sp]` (CAN) / `st.b r13,0x0[r11]` (UART). Builder host needs `pypcode` installed (import at module top); the branch self-test must pass or the build aborts. `phfpatch.py` recomputes per-record + FILE CHECKSUM correctly and round-trips.

## 11. Must be captured on the bench (not derivable from the image)

The single hard blocker is the **Bluetooth baseband UART framing and how to put the baseband into transparent SPP data mode** — not present in the image (no AT/CSR strings; stock TX goes through the INTUA1T ring). Also: resolved UART baud (UA1CTL2 divisor 0x57=87 confirmed, base clock unknown), C0INFO bit4 semantics, MCONF mask-select confirmation, aFCAN multi-match priority, stack high-water after lowering SP, INTUA1T runtime contention, and whether the factory bootloader enforces more than FILE CHECKSUM (signature). See the structured `externally_unverifiable` list.


## Final concrete values

| name | value | confidence | source |
|---|---|---|---|
| UA1TX | 0xFFFFFA17 | confirmed | 0x17AAA, 0x17DC2 (st.b r?,-0x5e9[r0]); analysis/sj3_regs.json |
| UA1STR | 0xFFFFFA14 | confirmed | 0x17A76 tst1 0x7,-0x5ec[r0] |
| UART_TX_READY_CONDITION | (UA1STR & 0x80) == 0 (bit7 set = busy); poll with 'bne 1b' then store | confirmed | 0x17A76 -> store 0x17AAA |
| UA1RX | 0xFFFFFA16 | confirmed | 0x17C76 ld.b -0x5ea[r0],r13 |
| UA1TIC (mask INTUA1T, bit6=1) | 0xFFFFF168 | confirmed | sj3_regs.json; INTUA1T vector 0x45A4->0x17D9C |
| UART_RX_HOOK_ADDR | 0x17C7A | confirmed | 0x17C7A st.b r13,0x0[r11] |
| UART_RX_HOOK_DISPLACED_BYTES | 4B 6F 00 00 (st.b r13,0x0[r11]) | confirmed | 0x17C7A |
| UART_RX_HOOK_RETURN_ADDR | 0x17C7E | confirmed | 0x17C7E ld.h -0x62d6[gp],r14 |
| UART_RX_BYTE_REG | r13 | confirmed | 0x17C76 |
| UART_RX_HOOK_PRESERVE_REGS | r11, r13 | confirmed | 0x17C6C/0x17C74/0x17C7A |
| CAN0_BASE | 0x03FEC000 | confirmed | 0xEEE4 mov 0x3fec000,r11 |
| MB_BASE / MB_STRIDE | 0x03FEC100 / 0x20 (MB m = base + m*0x20) | confirmed | 0xEE70 / 0xEE80 shl 0x5 |
| MB_FIELD_OFFSETS | MDATA0..7=+0..7, MDLC=+8(byte), MCONF=+9(byte), MIDL=+0xA(16b), MIDH=+0xC(16b), MCTRL=+0xE(16b) | confirmed | 0xEE9C/0xEEA8/0xEEB4/0xEEC0 |
| MIDH_WRITE_FORMULA | MIDH = (id11 & 0x7FF) << 2  (16-bit store) | confirmed | 0xF568; tables 0x4850,0x48F8 |
| MIDL_WRITE_VALUE | 0x0000 (16-bit store; standard ID) | confirmed | TX routine has no MIDL store; only MIDL writer is 0xF1D6 (catch-all) |
| RX_ID_READBACK_FORMULA | stdID = (MIDH >> 2) & 0x7FF ; IDE = (MIDH>>15)&1 | confirmed | 0x48F8 table decode; 0xEEB4 |
| RAW_TX_BUFFER_INDEX | 1 (any of 1..20 is inert in stock; m=15 also safe) | confirmed | 0xF084-0xF0DC loop: MCONF=0x00 @0xF0CA, MCTRL=0x001E @0xF0C2, no RDY/enable |
| MCONF_TX | 0x01 (byte; copies stock TX buffer 0) | confirmed | 0xF066 st.b r18(=1),-0x3ef7[r1] |
| MCTRL_SET_RDY | 0x0100 | confirmed | 0xF600 st.h r18,-0x3ef2[r1] |
| MCTRL_SET_TRQ | 0x0200 (fires the transmit; written after 0x0100) | confirmed | 0xF60C st.h r14,-0x3ef2[r1] |
| MCTRL_POLL_CLEAR_RDY | 0x0001 (write, wait until (MCTRL&1)==0 before editing buffer) | confirmed | 0xF03C/0xF048 |
| MCTRL_FLAG_CLEAR_INIT | 0x001E (buffers 1-31; stock buf0 used 0x001C) | confirmed | 0xF0C2 (0x001E) / 0xF05C (0x001C) |
| MCTRL_ENABLE | 0x0800 (upper bit3; optional for polling shim) | confirmed | 0xF072 / 0xF152 |
| MCTRL_BUSY_MASK | 0x0003 (RDY/TRQ; nonzero = TX in progress) | confirmed | bit0=RDY(0xF03C), bit1=TRQ(0xF60C) |
| MCTRL_CLEAR_DN | 0x0004 (RX: re-arm buffer after read; stock does this) | confirmed | 0xEECE st.h r14(=4),-0x3ef2[r1] |
| GMABT/GMABTD | unused (never write; C0GMABT 0x3FEC006 / C0GMABTD 0x3FEC008 not referenced) | confirmed | absence across image |
| CAN_RX_TEE_1_ADDR (buffers 30/31) | 0xEE66 ; displaced bytes 63 37 01 00 (st.w r6,0x0[sp]) ; return 0xEE6A | confirmed | 0xEE66; dispatcher 0xEBAE/0xEBB0 |
| CAN_RX_TEE_1_INDEX_REG | r6 | confirmed | 0xEE66 st.w r6; set at 0xEBAE mov r16,r6 |
| CAN_RX_TEE_2_ADDR (buffers 21-29) | 0xF77E ; displaced bytes 63 37 01 00 (st.w r6,0x0[sp]) ; return 0xF782 | confirmed | 0xF77E; dispatcher 0xEBA2/0xEBA4 |
| CAN_RX_TEE_2_INDEX_REG | r6 | confirmed | 0xEBA2 mov r15,r6; 0xF77E st.w r6 |
| RX_DISPATCHER | 0xEB64 (vector 0x0B4 @0x45B4), body 0xEB6C; clears C0INTS bit1 (0x0002) | confirmed | vector 0x45B4 jr 0xEB64; 0xEB74 C0INTS=0x2; 0xEB7C C0RGPT |
| C0RGPT (RX history; idx=(val>>8)&0xFF, bit1=empty) | 0x03FEC060 | confirmed | 0xEB7C/0xEB82/0xEB92 |
| CAN_RX_MASKS_CURRENT | C0MASK1..4 all = 0x1FFFFFFF (exact 29-bit match; NOT accept-all) | confirmed | 0xF21C-0xF28C; tables 0x4AC0(H)=0x1FFF,0x4AC8(L)=0xFFFF |
| MASK_WIDEN_RUNTIME | Write C0MASK2L=0x3FEC044,2H=0x3FEC046,3L=0x3FEC048,3H=0x3FEC04A all = 0x0000 (leaves MASK1/OEM intact) | likely | 0xF240/0xF24E (MASK2), 0xF25C/0xF26A (MASK3); mask-select MCONF bits[4:3] |
| MASK_WIDEN_FLASH_PATCH | Set flash halfwords 0x4AC2,0x4AC4 (MASK2H/3H) and 0x4ACA,0x4ACC (MASK2L/3L) to 0x0000 | likely | mask table @0x4AC0/0x4AC8 read into init 0xF20C-0xF28C |
| CAN_BIT_TIMING (preserve) | C0BRP=0x09, C0BTR=0x3508, C0IE=0x2700 (~125 kbit/s) | confirmed | 0xEFA4/0xEFBA/0xEFCC; tables 0x4ABC/0x4ABE/0x4ABA |
| SHIM_FREE_FLASH | 0x1EB68 .. 0x3DEFF (127896 bytes of 0xFF); config blob at 0x3DF00 (do not touch) | confirmed | binary 0xFF run scan; blob @0x3DF00 = a5 05 1e 02 00 02 e5 fd |
| RAM gp / SP / ep | gp=0x0400400C, SP=0x03FFEFA4, ep=0x03FFC000; RAM 0x03FFC000-0x03FFEFFF zeroed at reset | confirmed | 0x5BE6 / 0x5BEE / 0x5BF4 / 0x5C48 |
| SCRATCH_BLOCK | 0x03FFEE20 .. 0x03FFEFA3 (388 B) by lowering SP; patch file 0x5BF0 'a4 ef ff 03'->'20 ee ff 03' | likely | 0x5BEE mov 0x3ffefa4,sp |
| SHIM_INIT_STRATEGY | Lazy self-init (flag in scratch RAM) + re-assert TX-buffer MCONF/init before every TX | confirmed | CAN init 0xEED8 (5 callers); buffer loops 0xEFDA-0xF20A rewrite all 32 |
| UART_TX_DRAIN_CONTEXT | Main super-loop 0x12BAC (repoint a per-iteration jarl, e.g. 0x12BFA 'jarl 0x15d14' bytes 80 ff 1a 31); ISRs only enqueue | likely | 0x12BAC-0x12C02 (loops via br 0x12BAE) |
| CAN_RX_ISR_vs_TX_premise | 0xED64 (vec 0x0B0) = C0INTS bit5 -> 0xEA44 (TX-complete, reads C0TGPT 0x3FEC064); NOT the RX path | confirmed | 0xED76 andi 0x20; 0xEA68 C0TGPT; vectors 0x45B0/0x45B8 |
| STOCK_UART_TX_RING (if reused) | base 0x03FFDC08, size 0x12C, head(u16) 0x03FFDD3A, tail(u16) 0x03FFDD38 | confirmed | 0x17DA8-0x17DD4; gp 0x5BE6 |

## Contradictions resolved

- UA1TX address: uart-tx reader says 0xFFFFFA17 (CORRECT); ram-init reader, the task-premise header, and the existing shim spp_can_bridge.s:21 all say 0xFFFFFA18 (WRONG). Resolved to 0xFFFFFA17 by bytes 405717fa/407717fa at 0x17AAA/0x17DC2 and analysis/sj3_regs.json; nothing in the image accesses 0xFFFFFA18.
- CAN RX ISR chain: the task premise and the patch README say 'INTC0 ISR 0xED64 -> dispatcher 0xEA44 -> extractor 0xEE66'. Actual: vector 0x0B4 (@0x45B4) -> RX dispatcher 0xEB64/body 0xEB6C -> extractors 0xF77C (idx<30) and 0xEE66 (idx>=30). 0xED64 (vec 0x0B0) only services C0INTS bit5 and calls 0xEA44, which is the TX-complete handler (reads C0TGPT 0x3FEC064). can-rx reader is correct; the premise/README conflate RX with TX-complete.
- Raw-TX buffer choice: can-tx says buffers 1-20 are inert, use buffer 1 (MCONF=0x01); ram-init says all 32 are 'configured' so use buffer 31; the shim uses buffer 15 with MCONF=0x00. Resolved: use a buffer in 1-20 (confirmed inert: MCONF=0x00, no RDY/enable) with MCONF=0x01. ram-init's buffer 31 is WRONG (31 is a stock RX catch-all buffer, MCONF=0x19). The shim's MCONF=0x00 is insufficient (should be 0x01).
- Buffers 1-20 label: can-rx calls them 'transmit buffers'; can-tx calls them 'inert/unused'. Both agree on the bytes (MCONF=0x00, MCTRL=0x001E, RDY/enable never set). They are configured-but-not-driven = safe to claim; the 'transmit buffers' label is misleading.
- MCTRL TX-trigger value: the shim defines MCTRL_SET_TRQ=0x0100 and writes only that. Confirmed: 0x0100 SETS RDY (bit0); TRQ (bit1) is set by 0x0200. Stock writes 0x0100 then 0x0200 (0xF600/0xF60C). As written the shim never asserts TRQ and would never transmit.
- UART-RX hook address: build_spp_can_phf.py uses UART_RX_ISR=0x17C4C (the entry stub with stack setup); uart-rx-isr reader (and byte analysis) require 0x17C7A (clean 4-byte st.b, byte already in r13). Resolved to 0x17C7A.
- Scratch RAM placement: the shim puts RXSTATE/RXBUF at 0x03FFEFC0/0x03FFEFC1, which is above the initial SP 0x03FFEFA4 and inside the referenced 0x3FFEFxx tail (0x3FFEFFF is used as a pointer constant). ram-init's reserved block 0x03FFEE20-0x03FFEFA3 (SP lowered) is the correct location.

## Residual bench-only items (cannot be resolved from the static image)

- Bluetooth baseband UART framing and the command/mode to put the baseband into transparent SPP data pass-through. Not in the image (no AT/CSR strings; stock TX is pointer-driven through the INTUA1T ring). This is the primary blocker: the shim can move bytes, but without pass-through the baseband will not relay them. Needs a boot-time scope/logic capture of UA1TX/UA1RX and reading the baseband part number off the PCB.
- UARTA1 baud rate: UA1CTL2 divisor k=0x57 (87) is confirmed (0x176E6/0x17746), but the resolved baud needs the UARTA1 base clock (fXX and the UA1CTL1 clock-select / PLL). ~115200 is plausible (f~=20 MHz -> f/(2*87)=~114.9k) but must be confirmed by the clock init or a scope on the TX pin.
- C0INFO (0x3FEC053) bit4 (0x10) tested at 0xF528 before transmit: likely a bus-off / transmit-not-ready gate. Whether the shim should mirror this gate, and its exact semantics, need a bench capture of C0INFO during normal operation and bus-off.
- aFCAN MCONF mask-select field (bits[4:3] mapping 0x09->MASK1, 0x11->MASK2, 0x19->MASK3) is inferred from consistency, not a datasheet. Bench-verify that zeroing C0MASK2/C0MASK3 actually makes buffers 30/31 accept arbitrary IDs.
- aFCAN multi-match storage priority ('frame stored in the lowest matching buffer number'): with MASK2/3 widened, confirm on the bus that buffer 30 receives all non-OEM IDs and that the 9 OEM IDs still land in buffers 21-29 (so the HMI keeps working) — and whether a single widened catch-all overruns under back-to-back frames.
- Whether CAN mask registers can be rewritten at runtime without entering aFCAN initialization mode. If init-mode is required (disrupting the bus), prefer the flash-table patch (0x4AC2/0x4AC4/0x4ACA/0x4ACC) so stock init programs the widened masks. Needs manual/bench confirmation.
- Design decision + validation: preserve OEM RX (let buffers 21-29 feed 0xF77C and the normal HMI) vs hijack everything (disable 21-29 by clearing their RDY). Affects whether a second tee at 0xF77E is required and whether the factory dash/HFP keeps functioning.
- Stack high-water mark after lowering SP to 0x03FFEE20 (~248 B headroom). Needs a RAM fill-pattern test (paint 0x03FFC000-0x03FFEFA4, exercise all BPM features, read back lowest untouched address) before relying on the 388 B scratch block.
- INTUA1T runtime enable/contention: whether the repurposed mode leaves the stock INTUA1T TX ISR active. If the shim writes UA1TX directly while INTUA1T drives the ring, mask UA1TIC (0xFFFFF168) bit6. Needs bench confirmation of UA1TIC state at runtime.
- aFCAN TX throughput vs CAN->BT frame rate at the real baud, and whether the polled/main-loop drain keeps up without overrunning the 2 catch-all RX buffers. Needs a bench bus-load + timing measurement.
- Post-processor 0xF6AC (called after 0xEE66 for buffers 30/31): confirm it does not consume/gate the frame in a way that interferes with the tee.
- ep-relative reads at 0x03FFBF1C / 0x03FFBFEC (below the documented RAM base 0x03FFC000): confirm whether RAM extends below 0x03FFC000 or these alias an undefined region. Bench memory probe.
- Factory bootloader acceptance: whether it enforces more than the recomputed FILE CHECKSUM (e.g. a signature). A re-checksummed PHF alone may be rejected. Confirm, and only flash a module recoverable in boot/BDM mode.
- FDIM dash-text channel CAN byte layout (for the optional dash-notifier feature; data pointers @0x4874 -> 0x03FFD758+). Not needed for the raw BT<->CAN bridge, but needs a bench CAN capture if the text feature is built.

## Per-reader recommended values

### FG Falcon BPM aFCAN CAN0 transmit path (Renesas V850ES, bpm/bpm.bin). Stock standard-ID MS-CAN TX: ID encoding, MCONF/MCTRL values, buffer map, and the runtime transmit routine.

- **CAN0_BASE** = 0x03FEC000  — aFCAN CAN0 register block base
- **MB_BASE** = 0x03FEC100  — message buffer 0 base; buffer m = MB_BASE + m*0x20
- **MB_STRIDE** = 0x20  — bytes per message buffer
- **RAW_TX_BUFFER_INDEX** = 1  — Recommended dedicated raw-TX buffer. Buffers 1-20 are all inert in stock (MCONF=0x00, RDY never set), so any of them is safe; buffer 1 chosen. AVOID buffer 0 (stock NM/FDIM TX) and buffers 21-31 (stock RX filters).
- **TXBUF1_MDLC** = 0x03FEC128  — byte: data length (1..8)
- **TXBUF1_MDATA0** = 0x03FEC120  — data bytes 0..7 at +0x00..+0x07
- **TXBUF1_MCONF** = 0x03FEC129  — byte config register
- **TXBUF1_MIDL** = 0x03FEC12A  — 16-bit low identifier; write 0x0000 for standard ID
- **TXBUF1_MIDH** = 0x03FEC12C  — 16-bit high identifier; write (id&0x7FF)<<2
- **TXBUF1_MCTRL** = 0x03FEC12E  — 16-bit control (set/clear paired bits)
- **MCONF_TX** = 0x01  — byte written to MCONFm to configure a standard-ID transmit buffer (copies stock buffer 0). For reference stock RX filter buffers use 0x09.
- **MIDH_FORMULA** = (id & 0x7FF) << 2  — THE linchpin: standard 11-bit ID goes here, left-shifted 2. e.g. 0x781->0x1E04, 0x5A0->0x1680
- **MIDL_VALUE** = 0x0000  — always 0 for standard-ID frames
- **MCTRL_CLEAR_RDY** = 0x0001  — write to clear RDY (bit0); poll until (MCTRL & 0x0001)==0 before editing MCONF/ID/data
- **MCTRL_FLAG_CLEAR** = 0x001E  — one-time init: clear stale TRQ/DN/MOW/bit4 (stock buffer 0 used 0x001C after a prior 0x0002)
- **MCTRL_ENABLE** = 0x0800  — one-time init: set per-buffer enable bit3 after MCONF (matches stock buffer 0). Optional if you poll instead of using the per-buffer interrupt.
- **MCTRL_SET_RDY** = 0x0100  — per-frame step 1: set RDY (upper-byte set of bit0)
- **MCTRL_SET_TRQ** = 0x0200  — per-frame step 2: set TRQ (upper-byte set of bit1) -> fires the transmit. Stock writes 0x0100 then 0x0200 as two separate 16-bit stores.
- **MCTRL_BUSY_MASK** = 0x0003  — read MCTRLm and mask: nonzero (RDY|TRQ still set) means a transmit is in progress; hardware clears both on completion
- **MCTRL_CLEAR_DN** = 0x0004  — RX side only: write after reading a received buffer to clear DN (not needed for pure TX)
- **PER_FRAME_SEQUENCE** = poll RDY clear -> MDLC=dlc -> MDATA0..n -> MIDL=0 -> MIDH=(id&0x7FF)<<2 -> MCTRL=0x0100 -> MCTRL=0x0200  — exact order replicating stock routine 0xF50C/0xF550 (stock omits the explicit MIDL=0 because init leaves it 0; write it anyway for safety)
- **GMABT** = unused  — do NOT use automatic block transmission; stock never touches C0GMABT(0x3FEC006)/C0GMABTD(0x3FEC008)

### FG Falcon BPM aFCAN CAN0 receive path (Renesas V850ES, little-endian) — buffer-index trace, field offsets, acceptance filter

- **CAN0_BASE** = 0x03FEC000  — aFCAN CAN0 register block base
- **MB_BASE / MB_STRIDE** = 0x03FEC100 / 0x20  — message buffer m base = 0x03FEC100 + (m<<5); 32 buffers m=0..31
- **MB field offsets** = MDATA0..7=+0x00..0x07, MDLC=+0x08 (byte), MCONF=+0x09 (byte), MIDL=+0x0A (16b), MIDH=+0x0C (16b), MCTRL=+0x0E (16b)  — verified by the 0xEE66 load/store displacements
- **MCTRL_CLEAR_DN** = 0x0004  — write to MCTRLm after reading a frame to clear DN (Data New) and re-arm the RX buffer; do this in any tee shim
- **C0RGPT** = 0x03FEC060  — RX history; index of newest received buffer = (C0RGPT>>8)&0xFF; bit1(0x2)=history-empty flag
- **C0TGPT** = 0x03FEC064  — TX history (used by TX-complete handler 0xEA44); not on the RX path
- **idx register at 0xEE66** = r6 (then r6<<5 + 0x03FEC100 = MB base)  — the single answer to deliverable (a)
- **C0MASK1L..4H addresses** = C0MASK1L=0x03FEC040,1H=42,2L=44,2H=46,3L=48,3H=4A,4L=4C,4H=4E  — each mask is 32-bit split L(low16)+H(high16)
- **Current mask values (ALL FOUR)** = 0x1FFFFFFF (H=0x1FFF, L=0xFFFF)  — full 29-bit compare = exact match; NOT accept-all. Deliverable (b): masks MUST be widened for a sniffer.
- **Accept-all widening write** = MASKnL=0x0000 and MASKnH=0x0000  — aFCAN mask: 1=compare, 0=don't-care; all-zero mask => assigned buffer accepts every ID
- **Recommended sniffer hook** = Set C0MASK2=0x00000000 (and C0MASK3=0) so buffers 30/31 become catch-all; they already feed extractor 0xEE66 which records full MIDL/MIDH+DLC+data.  — Caveat: aFCAN stores a matching frame in the LOWEST matching buffer#, so buffers 21-29 (MASK1, exact 9 IDs) still skim those IDs to 0xF77C. For literally every frame, also neutralize 21-29 (clear their RDY) or widen MASK1 and reduce to one buffer, OR tee at BOTH 0xF77C and 0xEE66.
- **Currently accepted RX IDs (stock)** = buf21-29 (std 11-bit): 0x781,0x406,0x317,0x2FC,0x2F2,0x2E2,0x2CF,0x2CD,0x2C1 ; buf30,31: 0x000  — 0x781 = BPM own module ID; buf30/31 (ID 0x000) are effectively dormant in stock fw
- **MIDH->ID decode** = stdID = (MIDH>>2)&0x7FF ; IDE = (MIDH>>15)&1 ; extID uses MIDH[12:0]<<16 | MIDL  — all stock RX buffers are standard frames (IDE=0)
- **Extractor RAM scratch (fixed dest)** = data=gp-0x7EB4, DLC=gp-0x7EAC, MIDL=gp-0x7EAA, MIDH=gp-0x7EA8, MCTRL=gp-0x7EA6  — 0xEE66 overwrites one scratch for buffers 30 and 31; read here or redirect for the bridge
- **RX tee points** = 0xEE66 (buffers 30-31), 0xF77C (buffers 21-29), dispatcher loop 0xEB92-0xEBC8  — 0xEE66 is once-per-frame with no internal loop; both extractors clear DN via MCTRL=0x0004
- **Bit timing** = C0BRP=0x09, C0BTR=0x3508, C0IE=0x2700  — 125 kbit/s MS-CAN per BPM-analysis.md; preserve these when reconfiguring
- **CAN init entry** = 0xEED8 (->prologue 0xF2B4 ->body 0xEEDC); masks written at 0xF21C-0xF28C; called with r6=0 (r24=0)  — where to patch mask/buffer config if widening at init time rather than runtime

### UART RX ISR @0x17C4C (UARTA1 receive, vector 0x0A0): instruction-aligned byte-tee hook mapping for a BT->CAN bridge

- **HOOK_ADDR** = 0x17C7A  — Overwrite this one instruction with a 4-byte jr to the trampoline. Right after the byte is in r13, on the normal receive path.
- **DISPLACED_INSTR** = st.b r13, 0x0[r11]  — The single instruction the hook replaces; trampoline must re-execute it verbatim (needs r11 and r13 intact).
- **DISPLACED_BYTES** = 4B 6F 00 00  — Exact 4 bytes at 0x17C7A (little-endian as stored). Save these to re-emit in the trampoline.
- **DISPLACED_LEN** = 4  — Exactly fills a 4-byte jr disp22; no partial-instruction leftover.
- **PATCH_AT_0x17C7A** = jr (TRAMPOLINE - 0x17C7A)  — 4-byte V850 jr disp22. Encode like the ROM example at 0x17D96 (bf07befe = jr 0x17C54), or assemble directly.
- **TRAMPOLINE_RETURN_ADDR** = 0x17C7E  — After re-executing the displaced st.b, jr back here (next instruction, a clean 4-byte boundary: ld.h -0x62d6[gp],r14).
- **RX_BYTE_REG** = r13  — Holds the freshly-read UART RX byte at the hook point. This is the value to pass to bridge_rx(byte).
- **MUST_PRESERVE_REGS** = r11, r13  — Only live-out regs at 0x17C7A. Save/restore both around the bridge_rx call; the displaced st.b consumes them. r10 and r12 are dead here and usable as scratch.
- **BRIDGE_RX_ARG_REG** = r6  — V850 EABI first argument register. Trampoline should `mov r13, r6` before calling bridge_rx. Confirm against the shim's actual ABI.
- **ALT_HOOK_ADDR** = 0x17C76  — Alternative: displace the ld.b (bytes 00 6F 16 FA, 4B). Trampoline re-executes the UA1RX read into r13 (idempotent single read), tees, then jr to 0x17C7A. Preferred hook is 0x17C7A per 'after the byte is available'.
- **UA1RX** = 0xFFFFFA16  — UARTA1 receive data register; source of the RX byte (ld.b -0x5ea[r0]).
- **UA1STR** = 0xFFFFFA14  — UARTA1 status; bit0 tested at 0x17C58 (overrun branch to 0x17CD8), bit1 at 0x17C5E (framing-error branch to 0x17CBE).
- **RINGBUF_BASE** = gp - 0x6530  — Normal-path RX ring buffer base (r11 = base + index).
- **RINGBUF_INDEX** = gp - 0x62d6  — 16-bit write index into the RX ring buffer.
- **RINGBUF_SIZE** = 0x12C (300)  — Index wraps to 0 at 300 (movea 0x12c,r0,r1; cmp at 0x17C90).
- **JR_ENCODING_TEMPLATE** = 0x17D96: BF 07 BE FE = jr 0x17C54  — Use as a worked reference for assembling the 4-byte jr at the hook.

### FG Falcon BPM (Renesas V850ES) — stock UARTA1 byte-transmit path to the Bluetooth baseband

- **UA1TX** = 0xFFFFFA17  — UARTA1 transmit data register. Store the outgoing byte here (st.b). Correct the shim's 0xFFFFFA18.
- **UA1STR** = 0xFFFFFA14  — UARTA1 status register. Poll this before writing UA1TX.
- **TX_BUSY_MASK** = 0x80  — Bit 7 of UA1STR = transmit busy/in-progress (likely TSF). Ready to write when this bit is CLEAR.
- **TX_READY_CONDITION** = (UA1STR & 0x80) == 0  — The condition under which stock writes UA1TX (0x17a76 -> 0x17aaa). Poll loop: 'bne' back while busy, i.e. spin while (UA1STR & 0x80) != 0, then store.
- **uart_putc_polled** = uart_putc: 1: ld.bu UA1STR[r0],r7 ; andi 0x80,r7,r7 ; bne 1b ; st.b r6,UA1TX[r0] ; jmp [lp]  — RECOMMENDED self-contained writer (r6=byte). UA1STR=0xFFFFFA14, UA1TX=0xFFFFFA17. Note 'bne 1b' (not 'be'). Mirrors stock polled path; needs no gp/CTBP/scheduler; safe from any non-blocking context.
- **UA1TIC** = 0xFFFFF168  — UARTA1 transmit interrupt control register. Optional: set bit6 (mask) to disable INTUA1T while the shim does polled writes, if you want to guarantee no interaction with the stock TX ISR. Bit6=1 masks, =0 enables.
- **UA1CTL0 / UA1CTL1 / UA1CTL2** = 0xFFFFFA10 / 0xFFFFFA11=0x00 / 0xFFFFFA12=0x57  — Stock UARTA1 config; divisor k=0x57 (87). Leave as-is (fw already inits UARTA1) unless you need a different baud for the BT baseband.
- **TX_RING (if you ever reuse stock ISR path instead)** = base=0x03FFDC08 size=0x12C, head(u16)=0x03FFDD3A, tail(u16)=0x03FFDD38, gp=0x0400400C  — NOT recommended for the shim: enqueue is scheduler-coupled and an empty ring also means the INTUA1T ISR won't auto-start a transmission. Polled writer avoids all of this.

### FG Falcon BPM (Renesas V850ES/Fx3, uPD70F3372-class) - scratch RAM extent, startup hook point, and periodic drain context for a BT(UARTA1)<->CAN(aFCAN) bridge shim. Analysis of bpm/bpm.bin via bpm/out/code.asm.

- **gp_base** = 0x0400400C  — Global pointer; add to any -0xNNNN[gp] disp to get the absolute RAM address. RAM base = gp-0x800C.
- **sp_init_original** = 0x03FFEFA4  — Stock initial stack pointer, set at reset 0x5BEE. Stack grows down from here.
- **ep_base** = 0x03FFC000  — Element pointer = internal RAM base.
- **ram_range** = 0x03FFC000-0x03FFEFFF  — 12 KB internal RAM, entirely BSS-cleared at reset (clear loop runs to 0x03FFF000 exclusive).
- **bridge_state_block** = 0x03FFEE20-0x03FFEFA3  — RECOMMENDED 388-byte scratch block for framer state + 256B TX ring + indices. Obtained by lowering the reset SP (see sp_init_new). Lies above all observed globals (top 0x03FFED27) and below the original SP; it is zero at boot because the reset clear loop zeroes up to 0x03FFF000. This is the only way to get a contiguous >=320B region that provably avoids both globals and pointer-accessed buffers.
- **bridge_state_block_min** = 0x03FFEE64-0x03FFEFA3  — ALTERNATIVE minimal 320-byte block (set SP to 0x03FFEE64 instead) if the 388B version leaves too little stack. Leaves ~316B stack budget vs ~252B for the 388B version.
- **sp_init_new** = 0x03FFEE20  — New reset SP value to reserve the 388B block. Patch the 32-bit immediate in the instruction at 0x5BEE (mov 0x3ffefa4, sp; file bytes 23 06 a4 ef ff 03): change the little-endian immediate 'a4 ef ff 03' at file offset 0x5BF0 to '20 ee ff 03'. CAUTION: reduces stack headroom from ~636B to ~248B - must bench-verify stack high-water before relying on it.
- **shim_init_strategy** = LAZY (self-init on first use)  — There is no clean one-time 'after CAN+UART init' hook: early init 0x183d8 is clock-only, and both CAN init (0xEED8, 5 callers) and UART init (0x17588, 8 callers) are multi-call reinit routines. Make shim_init guard on a magic/flag word in the scratch block; on first call configure the shim's CAN TX message buffer. Because stock CAN reinit rewrites all 32 buffers, also re-assert the TX buffer's MIDL/MIDH/MDLC/MCONF before every transmit (cheap).
- **periodic_drain_hook** = 0x12BFA (jarl 0x15d14 in main loop)  — To drain the UART TX ring outside interrupt context, repoint one of the per-iteration main-loop calls (e.g. the jarl 0x15d14 at 0x12BFA, bytes 80 ff 1a 31) to a shim drain routine that does its work then tail-calls/returns into 0x15d14. Runs every super-loop iteration. Alternatively drain opportunistically from the UARTA1-RX ISR path.
- **startup_hook_alt** = 0x5C62 (jarl 0x12bac)  — If a pre-main one-time hook is still wanted, redirect reset's 'jarl 0x12bac' (bytes 80 ff 4a cf at 0x5C62) to a shim pre-init that sets up our scratch block then jumps to 0x12bac. NOTE this runs BEFORE stock CAN/UART init (which happen inside main), so the CAN-buffer part must still be lazy.
- **uart_tx_regs** = UA1TX=0xFFFFFA18, UA1STR=0xFFFFFA14  — For the TX-ring drain: poll UA1STR TX-empty/ready bit, then write the next byte to UA1TX. UA1RX=0xFFFFFA16, UA1CTL0=0xFFFFFA10.
- **can_tx_buffer_candidate** = m=31, base 0x3FEC4E0  — All 32 buffers are configured by stock, so pick the highest index to repurpose as the shim TX buffer and re-assert its config each TX. Offsets: +0x00..07 MDATA, +0x08 MDLC, +0x09 MCONF, +0x0A MIDL, +0x0C MIDH, +0x0E MCTRL. UNCERTAIN - must confirm with a bench CAN capture that stock does not rely on m=31 for a specific RX filter; a buffer stock only RX-filters but never TXs is the safest choice.

