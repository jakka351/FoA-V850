; ------------------------------------------------------------------------------
; spp_can_bridge.s  —  BPM (8R29-14D511) Bluetooth-serial <-> MS-CAN bridge shim
; ------------------------------------------------------------------------------
; Target : Renesas V850ES, FG Falcon Bluetooth Phone Module
; Injects: free flash at 0x1EB68 (127,896 bytes of 0xFF available to 0x3DF00)
; Build  : v850 GCC/GHS  ->  objcopy -O binary  ->  patch/build_spp_can_phf.py
;
; Protocol over the BT serial link (once the baseband is in a data/SPP pass mode):
;   host -> module :  0xAA  id_hi id_lo  dlc  d0..d7           (12 bytes) -> CAN0 TX
;   module -> host :  0x55  id_hi id_lo  dlc  d0..d7           for every CAN0 RX frame
;
; Everything below marked [RE-VERIFIED] is taken from the teardown in bpm/.
; Everything marked [VERIFY] must be confirmed on the bench / against the Renesas
; aFCAN + UARTA manuals before you arm the hooks and flash. Do NOT flash a module
; you cannot recover in boot/BDM mode.
; ------------------------------------------------------------------------------

; ---- SFR addresses -----------------------------------------------------------
    .equ UA1STR,   0xFFFFFA14      ; [RE-VERIFIED] UARTA1 status  (RX ISR reads this)
    .equ UA1RX,    0xFFFFFA16      ; [RE-VERIFIED] UARTA1 receive data
    .equ UA1TX,    0xFFFFFA18      ; [VERIFY] UARTA1 transmit data (fw accesses via ptr)
    .equ UA1CTL0,  0xFFFFFA10      ; [VERIFY] UARTA1 control 0 (for TX-enable/ready bit)

    .equ CAN0_BASE, 0x03FEC000     ; [RE-VERIFIED]
    .equ C0CTRL,   0x03FEC050      ; [VERIFY] global control
    .equ C0INTS,   0x03FEC058      ; [RE-VERIFIED] interrupt status (ISR masks 0x20)
    .equ C0BRP,    0x03FEC05A      ; bit-timing (already set by fw; cfg blob @0x3DF00)
    .equ C0BTR,    0x03FEC05C
    ; message buffer m lives at CAN0_BASE + 0x100 + m*0x20  [RE-VERIFIED @0xEE70]
    .equ MB_BASE,  0x03FEC100
    .equ MB_STRIDE,0x20
    .equ TXBUF,    15              ; [VERIFY] dedicated TX buffer index (pick one fw leaves free)
    ; buffer field offsets: +0..7 MDATA, +8 MDLC, +9 MCONF, +0xA MIDL, +0xC MIDH, +0xE MCTRL
    .equ O_MDLC,   0x08
    .equ O_MCONF,  0x09
    .equ O_MIDL,   0x0A
    .equ O_MIDH,   0x0C
    .equ O_MCTRL,  0x0E
    ; aFCAN MxCTRL uses set/clear half-words. [VERIFY] exact bits in the uPD703xxx aFCAN manual.
    .equ MCTRL_SET_TRQ, 0x0100     ; [VERIFY] request transmit
    .equ MCONF_TX,      0x00       ; [VERIFY] buffer direction = transmit

; ---- scratch RAM (unused high internal RAM; confirm free in your RAM map) -----
    .equ RXSTATE,  0x03FFEFC0      ; [VERIFY] 1 byte  : framer state (0=idle,1..n collecting)
    .equ RXBUF,    0x03FFEFC1      ; [VERIFY] 11 bytes: id_hi,id_lo,dlc,d0..d7 staging

; ==============================================================================
;  shim_init  — call once from the reset path (optional hook) to arm TXBUF
; ==============================================================================
    .global shim_init
shim_init:
    ; configure message buffer TXBUF as a transmit buffer (direction + mask-off).
    ; [VERIFY] exact MCONF write + any C0CTRL enable; the fw already brought aFCAN up,
    ; so this only needs to claim one free buffer for TX.
    mov   MB_BASE + TXBUF*MB_STRIDE, r10
    mov   MCONF_TX, r11
    st.b  r11, O_MCONF[r10]
    jmp   [lp]

; ==============================================================================
;  uart_putc(r6 = byte)  — send one byte to the BT baseband over UARTA1
; ==============================================================================
    .global uart_putc
uart_putc:
1:  ; [VERIFY] poll the UARTA1 transmit-ready/empty bit. The fw drives TX from the
    ; INTUA1T interrupt via a pointer; simplest robust shim path is a polled write.
    ld.bu UA1STR[r0], r7           ; [VERIFY] which bit = "TX buffer empty" on this part
    andi  0x80, r7, r7             ; [VERIFY] mask the TX-ready bit
    cmp   r0, r7
    be    1b
    st.b  r6, UA1TX[r0]
    jmp   [lp]

; ==============================================================================
;  can0_tx(r6=id_hi, r7=id_lo, r8=dlc, r9=ptr->8 data bytes)
;      transmit a standard-ID frame via TXBUF
; ==============================================================================
    .global can0_tx
can0_tx:
    prepare {lp}, 0
    mov   MB_BASE + TXBUF*MB_STRIDE, r10
    ; ID (standard 11-bit): MIDL/MIDH layout is aFCAN-specific -> [VERIFY] shift/split
    st.b  r7, O_MIDL[r10]
    st.b  r6, O_MIDH[r10]
    ; DLC
    st.b  r8, O_MDLC[r10]
    ; copy 8 data bytes r9[0..7] -> MDATA0..7
    mov   0, r12
2:  mov   r9, r13
    add   r12, r13
    ld.b  0[r13], r14
    mov   r10, r15
    add   r12, r15
    st.b  r14, 0[r15]
    add   1, r12
    cmp   8, r12
    blt   2b
    ; request transmit
    mov   MCTRL_SET_TRQ, r11
    st.h  r11, O_MCTRL[r10]
    dispose 0, {lp}
    jmp   [lp]

; ==============================================================================
;  bridge_rx(r6 = received byte)   — called from the UARTA1 RX ISR hook (0x17C4C)
;      little state machine: 0xAA sync, then 11 bytes (id_hi,id_lo,dlc,d0..d7)
; ==============================================================================
    .global bridge_rx
bridge_rx:
    prepare {lp}, 0
    ld.bu RXSTATE[r0], r7
    cmp   r0, r7                    ; state==0 ? waiting for sync
    bne   3f
    mov   0xAA, r8
    cmp   r8, r6
    bne   9f                        ; not our sync -> stay idle
    mov   1, r8
    st.b  r8, RXSTATE[r0]
    br    9f
3:  ; collecting: store byte at RXBUF+(state-1), advance; at 12 -> transmit
    mov   RXBUF-1, r8
    add   r7, r8
    st.b  r6, 0[r8]
    add   1, r7
    st.b  r7, RXSTATE[r0]
    cmp   12, r7                    ; 1(sync consumed)+11 collected
    blt   9f
    ; full frame -> can0_tx(id_hi,id_lo,dlc,&data)
    mov   RXBUF, r9                 ; r9 -> id_hi
    ld.bu 0[r9], r6                 ; id_hi
    ld.bu 1[r9], r7                 ; id_lo
    ld.bu 2[r9], r8                 ; dlc
    add   3, r9                     ; r9 -> d0
    jarl  can0_tx, lp
    st.b  r0, RXSTATE[r0]           ; reset state
9:  dispose 0, {lp}
    jmp   [lp]

; ==============================================================================
;  bridge_cantx(r6 = CAN0 RX buffer index)  — called from the CAN RX path hook
;      (after the fw extractor at 0xEE66 has the frame). Emit 0x55 + id + dlc + 8 data.
; ==============================================================================
    .global bridge_cantx
bridge_cantx:
    prepare {lp, r20, r21}, 0
    mov   r6, r20                   ; buffer index
    ; compute buffer base = MB_BASE + idx*0x20
    shl   5, r20
    mov   MB_BASE, r21
    add   r20, r21                  ; r21 -> MBx
    ; 0x55 sync
    mov   0x55, r6
    jarl  uart_putc, lp
    ; id_hi, id_lo
    ld.bu O_MIDH[r21], r6
    jarl  uart_putc, lp
    ld.bu O_MIDL[r21], r6
    jarl  uart_putc, lp
    ; dlc
    ld.bu O_MDLC[r21], r6
    jarl  uart_putc, lp
    ; 8 data bytes
    mov   0, r20
4:  mov   r21, r22
    add   r20, r22
    ld.bu 0[r22], r6
    jarl  uart_putc, lp
    add   1, r20
    cmp   8, r20
    blt   4b
    dispose 0, {lp, r20, r21}
    jmp   [lp]
