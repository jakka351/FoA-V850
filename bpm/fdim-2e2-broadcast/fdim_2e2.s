# ==============================================================================
# fdim_2e2.s -- Mk2 FG Falcon FDIM (AR79-14D017-JL) "broadcast 0x2E2" shim
# ==============================================================================
# Purpose: a Mk1 FDIM (8R29-14D017-AC) periodically transmits CAN 0x2E2
#   (Audio_Mode_Switches); the Mk2 FDIM does NOT. Its absence leaves the ICC/
#   audio-head looking incomplete, so the BEM withholds IgnitionState=IgnitionOn
#   in 0x406, which makes the BPM NRC-0x22 every $10 85/87 session. This shim makes
#   the Mk2 FDIM emit 0x2E2 periodically, emulating the Mk1.
#
# Mechanism: the CAN0 TX-complete vector (slot 53 @0x41D4, "jr 0x15C5E") is
#   redirected here. On every TX-complete IRQ we rate-limit with a RAM counter and,
#   when it expires, build+fire 0x2E2 on the otherwise-unused message buffer MB1,
#   then chain into the real ISR at 0x15C5E. MB1's completion is harmless: the FDIM
#   scheduler (0x15330) looks up the per-buffer table for buffer 1, finds 0xFFFF
#   (unused) and exits. We run at raw interrupt entry (before the ISR saves any
#   GPR), so we preserve every register and hand the ISR an untouched context by
#   replicating its own entry (add -4,sp / st.w r1) before jumping past it.
#
# Target facts [RE-VERIFIED against AR79-14D017-JL]: aFCAN CAN0 @0x3FEC000; MB m
#   base 0x3FEC100+m*0x20 (+8 MDLC,+9 MCONF,+A MIDL,+C MIDH,+E MCTRL); TX = write
#   fields then MCTRL 0x0100 then 0x0200; MB1 (0x3FEC120) unused; free flash 0x24840;
#   SP 0x3FFEFAC (scratch above it is free); ISR entry 0x15C5E = add -4,sp / st.w
#   r1,0[sp] / prepare @0x15C64.
# ==============================================================================

    .equ MB1,        0x03FEC120     # free TX message buffer
    .equ O_MDLC,     0x08
    .equ O_MCONF,    0x09
    .equ O_MIDL,     0x0A
    .equ O_MIDH,     0x0C
    .equ O_MCTRL,    0x0E
    .equ MIDH_2E2,   0x0B88         # (0x2E2 & 0x7FF) << 2
    .equ MCONF_TX,   0x01
    .equ MCTRL_RDY,  0x0100
    .equ MCTRL_TRQ,  0x0200

    .equ CTR,        0x03FFEFC0     # 1 byte: rate-limit counter (free RAM above SP)
    .equ IFLAG,      0x03FFEFC1     # 1 byte: MB1-configured flag
    .equ RATE,       8              # emit 0x2E2 once per RATE TX-complete IRQs

    .equ ISR_SAVE,   0x00015C64     # real ISR, just past its own "add -4,sp / st.w r1"
    .equ PAYLOAD_ADDR, 0x00024840   # payload sits at the shim start (SHIM_ORG); referenced as a const

    .text
    # 0x2E2 Audio_Mode_Switches payload, DLC 8. Placed FIRST so its address is the fixed
    # SHIM_ORG (loadable as a constant; a `mov <label>` can't be assembled). Default =
    # idle/all-zero (presence frame); refine from a Mk1 capture / the emulator if needed.
    .global payload_2e2
payload_2e2:
    .byte 0, 0, 0, 0, 0, 0, 0, 0

    .global fdim_hook
fdim_hook:
    addi  -64, sp, sp              # our frame (save r1,r6..r19)
    st.w  r1,   0[sp]
    st.w  r6,   4[sp]
    st.w  r7,   8[sp]
    st.w  r8,  12[sp]
    st.w  r9,  16[sp]
    st.w  r10, 20[sp]
    st.w  r11, 24[sp]
    st.w  r12, 28[sp]
    st.w  r13, 32[sp]
    st.w  r14, 36[sp]
    st.w  r15, 40[sp]
    st.w  r16, 44[sp]
    st.w  r17, 48[sp]
    st.w  r18, 52[sp]
    st.w  r19, 56[sp]

    # ---- lazy one-time MB1 config as a standard-ID TX buffer ----
    mov   IFLAG, r1
    ld.bu 0[r1], r10
    cmp   r0, r10
    bne   .Linited
    mov   MB1, r10
    mov   MCONF_TX, r11
    st.b  r11, O_MCONF[r10]
    st.h  r0,  O_MIDL[r10]
    mov   IFLAG, r1
    mov   1, r11
    st.b  r11, 0[r1]
.Linited:
    # ---- rate-limit ----
    mov   CTR, r1
    ld.bu 0[r1], r10
    add   1, r10
    st.b  r10, 0[r1]
    mov   RATE, r11
    cmp   r11, r10
    bnl   .Lfire                   # counter >= RATE -> emit (short branch)
    jr    .Ldone                   # else not yet (long-range skip)
.Lfire:
    st.b  r0, 0[r1]                # reset counter

    # ---- build + fire 0x2E2 on MB1 ----
    mov   MB1, r10
    mov   MIDH_2E2, r11
    st.h  r11, O_MIDH[r10]
    st.h  r0,  O_MIDL[r10]
    mov   8, r11
    st.b  r11, O_MDLC[r10]
    mov   PAYLOAD_ADDR, r12
    mov   0, r13
.Lcopy:
    mov   r12, r14
    add   r13, r14
    ld.bu 0[r14], r15
    mov   r10, r16
    add   r13, r16
    st.b  r15, 0[r16]
    add   1, r13
    cmp   8, r13
    bl    .Lcopy
    mov   MCTRL_RDY, r11
    st.h  r11, O_MCTRL[r10]
    mov   MCTRL_TRQ, r11
    st.h  r11, O_MCTRL[r10]

.Ldone:
    ld.w   4[sp], r6
    ld.w   8[sp], r7
    ld.w  12[sp], r8
    ld.w  16[sp], r9
    ld.w  20[sp], r10
    ld.w  24[sp], r11
    ld.w  28[sp], r12
    ld.w  32[sp], r13
    ld.w  36[sp], r14
    ld.w  40[sp], r15
    ld.w  44[sp], r16
    ld.w  48[sp], r17
    ld.w  52[sp], r18
    ld.w  56[sp], r19
    ld.w   0[sp], r1              # r1 = interrupted value
    addi  64, sp, sp             # pop our frame; sp = interrupt-entry sp
    add   -4, sp                 # replicate real ISR 0x15C5E: add -4,sp
    st.w  r1, 0[sp]              # replicate 0x15C60: st.w r1,0[sp] (saves interrupted r1)
    mov   ISR_SAVE, r1           # r1 now scratch; ISR at 0x15C64 does not re-save it
    jmp   [r1]
