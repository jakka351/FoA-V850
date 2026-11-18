# ==============================================================================
# spp_can_bridge.s  --  BPM (8R29-14D511) Bluetooth-serial <-> MS-CAN bridge shim
# ==============================================================================
# Target : Renesas V850ES, FG Falcon Bluetooth Phone Module (CAN0 aFCAN, MS-CAN)
# Lives  : free flash at 0x1EB68 (127,896 bytes of 0xFF up to 0x3DF00)
# Build  : v850-elf-as -mv850e  ->  ld -Ttext=0x1EB68  ->  objcopy -O binary
#          (driven by build_spp_can_phf.py, which also installs the detour hooks)
#
# Host protocol over the BT serial link (baseband in a transparent SPP data mode):
#   host -> module :  0xAA  id_hi id_lo  dlc  d0..d7      (12 bytes) -> CAN0 TX
#   module -> host :  0x55  id_hi id_lo  dlc  d0..d7      for every CAN0 RX frame
#
# EVERY constant below is [RE-VERIFIED] against bpm/bpm.bin (see bpm/out/
# extract-primitives.md and BRIDGE-SPEC.md) UNLESS marked [BENCH]. Items marked
# [BENCH] are correct per the static image but should be confirmed on the bench
# before flashing a module you cannot recover in boot/BDM mode.
#
# Design (mirrors the stock architecture: ISRs enqueue, the main loop drains):
#   * UART-RX hook @0x17C7A tees each received byte to bridge_rx (the framer).
#   * bridge_rx assembles a 12-byte host frame and calls can0_tx -> CAN0 TX buf 1.
#   * CAN-RX hooks @0xF77E (OEM-ID frames) and @0xEE66 (catch-all buffers 30/31)
#     tee each received CAN frame to bridge_cantx, which pushes 0x55+id+dlc+data
#     into OUR software TX ring (single producer = CAN-RX ISR context).
#   * The main-loop hook @0x12BFA (stock jarl 0x15D14) drains that ring to UARTA1
#     (single consumer = task context), so no ISR ever blocks on the UART.
# ==============================================================================

# ---- UARTA1 SFRs -------------------------------------------------------------
    .equ UA1STR,   0xFFFFFA14      # [RE-VERIFIED] status; bit7(0x80)=TX busy (TSF)
    .equ UA1TX,    0xFFFFFA17      # [RE-VERIFIED] transmit data register (NOT 0xFA18)

# ---- aFCAN CAN0 message buffers ----------------------------------------------
    .equ MB_BASE,  0x03FEC100      # [RE-VERIFIED] buffer m = MB_BASE + m*0x20
    .equ TXBUF,    1               # [RE-VERIFIED] stock leaves buffers 1..20 inert
    .equ MB1,      0x03FEC120      # [RE-VERIFIED] MB_BASE + 1*0x20  (our raw-TX buffer)
    # field offsets inside a message buffer (0..7 MDATA, 8 MDLC, 9 MCONF,
    # 0xA MIDL[16], 0xC MIDH[16], 0xE MCTRL[16])            [RE-VERIFIED @0xEE66/0xF50C]
    .equ O_MDLC,   0x08
    .equ O_MCONF,  0x09
    .equ O_MIDL,   0x0A
    .equ O_MIDH,   0x0C
    .equ O_MCTRL,  0x0E
    # aFCAN MCTRL set/clear model [RE-VERIFIED]: upper byte sets flag n, lower byte
    # clears flag n.  bit0=RDY, bit1=TRQ, bit2=DN.
    .equ MCTRL_SET_RDY, 0x0100     # per-frame step 1
    .equ MCTRL_SET_TRQ, 0x0200     # per-frame step 2 -> fires the transmit
    .equ MCTRL_CLR_RDY, 0x0001     # clear RDY before reconfiguring a buffer
    .equ MCTRL_ENABLE,  0x0800     # [RE-VERIFIED] per-buffer enable (stock buf0 init)
    .equ MCONF_TX,      0x01       # [RE-VERIFIED] stock buffer-0 (TX) MCONF value

# ---- scratch RAM  (0x03FFEFA4..0x03FFEFEF; above initial SP 0x03FFEFA4) -------
# [BENCH] confirm free with a RAM fill/read-back test; 0x03FFEFF0..FFF left as margin.
    .equ RXSTATE,  0x03FFEFA4      # 1  : framer state (0=idle, 1..11 collecting)
    .equ RXBUF,    0x03FFEFA5      # 11 : id_hi,id_lo,dlc,d0..d7 staging
    .equ INITFLAG, 0x03FFEFB0      # 1  : TX buffer configured yet?
    .equ TXHEAD,   0x03FFEFB1      # 1  : ring producer index
    .equ TXTAIL,   0x03FFEFB2      # 1  : ring consumer index
    .equ TXDROP,   0x03FFEFB3      # 1  : ring-overflow counter (diagnostic)
    .equ TXRING,   0x03FFEFB4      # RINGSIZE bytes
    .equ RINGSIZE, 60
    .equ TXBURST,  8               # max bytes drained per main-loop pass

# ---- hook return / stock-call targets  (absolute; reached via jmp [reg]) ------
    .equ RET_RX,     0x00017C7E    # instr after displaced st.b @0x17C7A
    .equ RET_F77E,   0x0000F782    # instr after displaced st.w @0xF77E
    .equ RET_EE66,   0x0000EE6A    # instr after displaced st.w @0xEE66
    .equ STOCK_15D14,0x00015D14    # stock main-loop routine the drain hook wraps

    .text

# ==============================================================================
#  Detour trampolines  (the builder points each hook jump at these)
# ==============================================================================

# ---- UART-RX tee: hook @0x17C7A displaces  st.b r13,0[r11]  (r13 = RX byte) ---
    .global trampoline_rx
trampoline_rx:
    st.b  r13, 0[r11]              # replay displaced instruction (host state intact)
    addi  -64, sp, sp             # save caller-saved window (r1,r6..r19,lp)
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
    st.w  lp,  60[sp]
    andi  0xFF, r13, r6           # arg0 = received byte (mask: stock ld.b @0x17C76 sign-extends)
    jarl  bridge_rx, lp
    ld.w   0[sp], r1
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
    ld.w  60[sp], lp
    addi  64, sp, sp
    mov   RET_RX, r1
    jmp   [r1]

# ---- CAN-RX tee (OEM IDs): hook @0xF77E displaces  st.w r6,0[sp]  (r6 = index) -
    .global tramp_f77e
tramp_f77e:
    st.w  r6, 0[sp]               # replay displaced instruction (host sp)
    jarl  cantx_common, lp        # saves regs, calls bridge_cantx(r6=index), restores
    mov   RET_F77E, r1
    jmp   [r1]

# ---- CAN-RX tee (catch-all buffers 30/31): hook @0xEE66 displaces st.w r6,0[sp] -
#      dormant unless an RX mask is widened (see BRIDGE-SPEC.md, optional sniffer).
    .global tramp_ee66
tramp_ee66:
    st.w  r6, 0[sp]               # replay displaced instruction
    jarl  cantx_common, lp
    mov   RET_EE66, r1
    jmp   [r1]

# shared body for both CAN-RX trampolines; r6 = buffer index at entry
cantx_common:
    addi  -64, sp, sp
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
    st.w  lp,  60[sp]
    jarl  bridge_cantx, lp        # r6 (index) still live
    ld.w   0[sp], r1
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
    ld.w  60[sp], lp
    addi  64, sp, sp
    jmp   [lp]

# ---- main-loop drain: hook @0x12BFA retargets  jarl 0x15D14 -> tramp_drain ------
#      lp = 0x12BFE (return into the main loop) on entry.
    .global tramp_drain
tramp_drain:
    addi  -4, sp, sp
    st.w  lp, 0[sp]               # save the main-loop return address
    jarl  thunk_15d14, lp         # perform the stock work we displaced
    # preserve stock's register state across our drain so the main loop is unaffected
    addi  -64, sp, sp
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
    jarl  tx_drain, lp
    ld.w   0[sp], r1
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
    addi  64, sp, sp
    ld.w  0[sp], lp
    addi  4, sp, sp
    jmp   [lp]                    # back into the main loop (0x12BFE)

thunk_15d14:
    mov   STOCK_15D14, r1
    jmp   [r1]

# ==============================================================================
#  bridge_rx(r6 = received byte)  --  host->module framer
#      0xAA sync, then 11 bytes (id_hi,id_lo,dlc,d0..d7); on completion -> can0_tx
# ==============================================================================
    .global bridge_rx
bridge_rx:
    addi  -4, sp, sp
    st.w  lp, 0[sp]
    mov   RXSTATE, r7
    ld.bu 0[r7], r8               # state
    cmp   r0, r8
    bne   .Lrx_collect
    # idle: expect 0xAA
    mov   0xAA, r9
    cmp   r9, r6
    bne   .Lrx_done               # not sync -> stay idle
    mov   1, r9
    st.b  r9, 0[r7]               # state = 1
    br    .Lrx_done
.Lrx_collect:
    mov   RXBUF, r9
    add   r8, r9
    add   -1, r9                  # &RXBUF[state-1]
    st.b  r6, 0[r9]
    add   1, r8
    st.b  r8, 0[r7]               # state++
    mov   12, r9
    cmp   r9, r8
    bl    .Lrx_done               # <12 collected -> keep waiting
    st.b  r0, 0[r7]               # full frame -> reset state
    mov   RXBUF, r9
    ld.bu 0[r9], r6               # id_hi
    ld.bu 1[r9], r7               # id_lo
    ld.bu 2[r9], r8               # dlc
    add   3, r9                   # &d0
    jarl  can0_tx, lp
.Lrx_done:
    ld.w  0[sp], lp
    addi  4, sp, sp
    jmp   [lp]

# ==============================================================================
#  can0_tx(r6=id_hi, r7=id_lo, r8=dlc, r9=&data[8])  --  transmit via TXBUF
# ==============================================================================
    .global can0_tx
can0_tx:
    addi  -4, sp, sp
    st.w  lp, 0[sp]
    mov   INITFLAG, r1            # lazy one-time TX-buffer config
    ld.bu 0[r1], r10
    cmp   r0, r10
    bne   .Ltx_inited
    jarl  shim_init, lp
.Ltx_inited:
    shl   8, r6                   # id_hi<<8
    or    r7, r6                  # | id_lo
    mov   0x7FF, r10
    and   r10, r6                 # id & 0x7FF
    shl   2, r6                   # MIDH value = id<<2
    mov   MB1, r10
    mov   2000, r11               # bounded wait for RDY|TRQ clear
.Ltx_wait:
    ld.hu O_MCTRL[r10], r12
    andi  0x3, r12, r12
    cmp   r0, r12
    be    .Ltx_rdy
    add   -1, r11
    cmp   r0, r11
    bne   .Ltx_wait
.Ltx_rdy:
    di
    st.h  r0, O_MIDL[r10]         # MIDL = 0 (standard ID)
    st.h  r6, O_MIDH[r10]         # MIDH = id<<2
    st.b  r8, O_MDLC[r10]         # DLC
    mov   0, r12                  # copy 8 data bytes -> MDATA0..7
.Ltx_copy:
    mov   r9, r13
    add   r12, r13
    ld.bu 0[r13], r14
    mov   r10, r15
    add   r12, r15
    st.b  r14, 0[r15]
    add   1, r12
    cmp   8, r12
    bl    .Ltx_copy
    mov   MCTRL_SET_RDY, r11      # fire: set RDY then TRQ (exactly like stock @0xF600)
    st.h  r11, O_MCTRL[r10]
    mov   MCTRL_SET_TRQ, r11
    st.h  r11, O_MCTRL[r10]
    ei
    ld.w  0[sp], lp
    addi  4, sp, sp
    jmp   [lp]

# ==============================================================================
#  shim_init  --  configure TXBUF as a standard-ID transmit buffer (like stock buf0)
# ==============================================================================
    .global shim_init
shim_init:
    mov   MB1, r10
    mov   MCTRL_CLR_RDY, r11      # clear RDY, wait until buffer idle (bounded)
    st.h  r11, O_MCTRL[r10]
    mov   2000, r11
.Lsi_wait:
    ld.hu O_MCTRL[r10], r12
    andi  0x1, r12, r12
    cmp   r0, r12
    be    .Lsi_ok
    add   -1, r11
    cmp   r0, r11
    bne   .Lsi_wait
.Lsi_ok:
    mov   MCONF_TX, r11
    st.b  r11, O_MCONF[r10]       # MCONF = 0x01 (standard-ID TX)
    st.h  r0,  O_MIDL[r10]        # MIDL = 0
    mov   MCTRL_ENABLE, r11
    st.h  r11, O_MCTRL[r10]       # enable buffer (bit3)
    mov   INITFLAG, r1
    mov   1, r11
    st.b  r11, 0[r1]
    jmp   [lp]

# ==============================================================================
#  bridge_cantx(r6 = CAN0 RX buffer index)  --  module->host: enqueue 0x55+frame
# ==============================================================================
    .global bridge_cantx
bridge_cantx:
    addi  -4, sp, sp
    st.w  lp, 0[sp]
    # frame-atomic drop: need >= 12 free bytes, else skip the WHOLE frame so the
    # ring never holds a partial frame (which would desync the host parser).
    mov   TXHEAD, r1
    ld.bu 0[r1], r10              # head (0..RINGSIZE-1)
    mov   TXTAIL, r1
    ld.bu 0[r1], r11              # tail
    cmp   r11, r10
    bnl   .Lcx_hge                # head >= tail (unsigned)
    mov   RINGSIZE, r1
    add   r1, r10                 # unwrap: head += RINGSIZE
.Lcx_hge:
    sub   r11, r10                # used = head - tail  (0..RINGSIZE-1)
    mov   48, r1                  # RINGSIZE-1-12 + 1 : used < 48  <=>  free >= 12
    cmp   r1, r10
    bl    .Lcx_room
    mov   TXDROP, r1              # no room -> drop whole frame, bump counter
    ld.bu 0[r1], r10
    add   1, r10
    st.b  r10, 0[r1]
    ld.w  0[sp], lp
    addi  4, sp, sp
    jmp   [lp]
.Lcx_room:
    shl   5, r6                   # index * 0x20
    mov   MB_BASE, r7
    add   r7, r6
    mov   r6, r16                 # r16 = buffer base (preserved across tx_putc)
    mov   0x55, r6                # sync byte
    jarl  tx_putc, lp
    ld.hu O_MIDH[r16], r17        # MIDH
    shr   2, r17                  # id = MIDH>>2
    mov   0x7FF, r6
    and   r6, r17                 # id & 0x7FF
    mov   r17, r6
    shr   8, r6                   # id_hi
    jarl  tx_putc, lp
    mov   r17, r6
    andi  0xFF, r6, r6            # id_lo
    jarl  tx_putc, lp
    ld.bu O_MDLC[r16], r6         # DLC
    andi  0x0F, r6, r6
    jarl  tx_putc, lp
    mov   0, r18                  # 8 data bytes
.Lcx_data:
    mov   r16, r7
    add   r18, r7
    ld.bu 0[r7], r6
    jarl  tx_putc, lp
    add   1, r18
    cmp   8, r18
    bl    .Lcx_data
    ld.w  0[sp], lp
    addi  4, sp, sp
    jmp   [lp]

# ==============================================================================
#  tx_putc(r6 = byte)  --  producer: push one byte into the software TX ring
# ==============================================================================
    .global tx_putc
tx_putc:
    mov   TXHEAD, r1
    ld.bu 0[r1], r10              # head
    mov   r10, r11
    add   1, r11
    mov   RINGSIZE, r12
    cmp   r12, r11
    bl    .Ltp_nowrap
    mov   0, r11                  # wrap
.Ltp_nowrap:
    mov   TXTAIL, r12
    ld.bu 0[r12], r12            # tail
    cmp   r12, r11
    bne   .Ltp_store
    mov   TXDROP, r1             # ring full -> drop, bump counter
    ld.bu 0[r1], r12
    add   1, r12
    st.b  r12, 0[r1]
    jmp   [lp]
.Ltp_store:
    mov   TXRING, r12
    add   r10, r12
    st.b  r6, 0[r12]             # ring[head] = byte
    mov   TXHEAD, r1
    st.b  r11, 0[r1]            # head = next
    jmp   [lp]

# ==============================================================================
#  tx_drain  --  consumer (task context): drain ring to UARTA1, bounded & non-blocking
# ==============================================================================
    .global tx_drain
tx_drain:
    mov   TXBURST, r19
.Ltd_loop:
    cmp   r0, r19
    be    .Ltd_done
    mov   TXHEAD, r1
    ld.bu 0[r1], r10
    mov   TXTAIL, r1
    ld.bu 0[r1], r11
    cmp   r10, r11
    be    .Ltd_done               # ring empty
    mov   4000, r12               # bounded wait for UART TX ready
.Ltd_tsf:
    mov   UA1STR, r1
    ld.bu 0[r1], r13
    andi  0x80, r13, r13
    cmp   r0, r13
    be    .Ltd_send
    add   -1, r12
    cmp   r0, r12
    bne   .Ltd_tsf
    br    .Ltd_done               # UART stuck busy -> leave rest for next pass
.Ltd_send:
    mov   TXRING, r1
    add   r11, r1
    ld.bu 0[r1], r14              # byte = ring[tail]
    mov   UA1TX, r1
    st.b  r14, 0[r1]             # transmit
    add   1, r11
    mov   RINGSIZE, r12
    cmp   r12, r11
    bl    .Ltd_nowrap
    mov   0, r11
.Ltd_nowrap:
    mov   TXTAIL, r1
    st.b  r11, 0[r1]            # tail = next
    add   -1, r19
    br    .Ltd_loop
.Ltd_done:
    jmp   [lp]

# ==============================================================================
#  auto-discoverable-at-boot  --  headless pairing with no FDIM
#      Hook @0x142C2 (the "No Phone Paired" one-shot, displaces set1 6,-0x6E46[gp]).
#      When the module first decides no phone is paired (fully booted, baseband up),
#      replay the displaced set1 then run the SAME sequence the FDIM "PAIR A NEW
#      PHONE" menu does (0xDAFE..0xDB30): precondition 0x1936C, setup 0x1900C,
#      set PIN 0x6934 (hardcoded), send PIN 0x68C8, become discoverable 0x6858 (60s).
#      Fires exactly once (the one-shot guard bit6 of gp-0x6E46). Stock fns are
#      reached through thunks (jarl can't carry an absolute target from the shim).
# ==============================================================================
    .equ RET_AUTOPAIR, 0x000142C6
    .equ F_1936C,      0x0001936C      # precondition check (r6=1 -> r10==0 means proceed)
    .equ F_1900C,      0x0001900C      # pairing-state setup
    .equ F_6934b,      0x00006934      # PIN generator (now writes fixed PIN)
    .equ F_68C8,       0x000068C8      # send-PIN-to-baseband (opcode 3)
    .equ F_6858,       0x00006858      # become-discoverable (r6 = window seconds)

    .global tramp_autopair
tramp_autopair:
    set1  6, -0x6E46[gp]          # replay displaced one-shot guard set
    addi  -64, sp, sp
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
    st.w  lp,  60[sp]
    jarl  auto_pair, lp
    ld.w   0[sp], r1
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
    ld.w  60[sp], lp
    addi  64, sp, sp
    mov   RET_AUTOPAIR, r1
    jmp   [r1]

auto_pair:
    addi  -4, sp, sp
    st.w  lp, 0[sp]
    mov   1, r6                   # precondition: 0x1936C(1) must return 0
    jarl  thk_1936c, lp
    cmp   r0, r10
    bne   .Lap_done               # not ready / already pairing -> skip
    jarl  thk_1900c, lp           # pairing-state setup
    movea -0x6E55, gp, r6         # PIN buffer
    mov   4, r7
    jarl  thk_6934, lp            # (re)write the (fixed) 4-digit PIN
    clr1  4, -0x6E67[gp]
    mov   4, r6
    movea -0x6E55, gp, r7
    jarl  thk_68c8, lp            # send PIN to the baseband
    clr1  0, -0x6E68[gp]
    movea 60, r0, r6              # 60-second discoverable window
    jarl  thk_6858, lp            # become discoverable
.Lap_done:
    ld.w  0[sp], lp
    addi  4, sp, sp
    jmp   [lp]

thk_1936c:  mov F_1936C, r1
    jmp   [r1]
thk_1900c:  mov F_1900C, r1
    jmp   [r1]
thk_6934:   mov F_6934b, r1
    jmp   [r1]
thk_68c8:   mov F_68C8, r1
    jmp   [r1]
thk_6858:   mov F_6858, r1
    jmp   [r1]
