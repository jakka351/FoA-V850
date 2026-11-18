# Headless Bluetooth pairing — findings & solution

The concern: with no FDIM screen and no buttons, how do you put the BPM in pairing mode and know
the PIN? Full reverse-engineering in [`../out/pairing-findings.md`](../out/pairing-findings.md);
this is the distilled answer. **Short version: the PIN is now hardcoded to `1234`; entry needs one
more small firmware step (or a bench check) — see §3.**

## 1. How stock pairing works (3-party)

- The **Bluetooth baseband** owns the RF pairing state.
- The **V850 host** (this firmware) owns the 4-digit PIN *and* renders the screens.
- The **FDIM** delivers menu/button events over MS-CAN.

Flow: user picks "PAIR A NEW PHONE" on the dash → FDIM sends an HMI frame on CAN
`0x2CF/0x2CD/0x2C1` (+ own `0x781`) → extractor `0xF77E` → HMI interpreter `0x16324/0x16BC8` → host
sends a "become discoverable" command to the baseband (one of the `0x6840–0x6EC8` opcode wrappers →
sender `0x18F08`). The host also **generates a 4-digit PIN** (routine `0x6934`, pseudo-random),
stores it at `gp-0x6E55` (`0x03FFD1B7`), **shows it on the dash** (screen code `0x01`, painter
`0x1B8D4`) **and sends it to the baseband** (command opcode 3). So the **host is the PIN authority**
— whatever it puts at `gp-0x6E55` is the PIN the phone must enter.

Two headless problems follow: (a) you can't read a random on-screen PIN, and (b) nothing triggers
"become discoverable" without the FDIM. The module does **not** auto-pair — at boot with no phone it
just shows the passive "pair a new phone" screen and waits.

## 2. PIN — SOLVED (hardcoded `1234`)

Because the PIN is host-generated, we replaced the generator at `0x6934` with "write these four
fixed ASCII digits and return" — so the PIN is deterministic end-to-end (dash + baseband). This is
in the builder (`--pin 1234`, default on; `--random-pin` keeps stock):

```
PIN @ 0x6934: generator replaced -> fixed PIN = 1234   (dash now shows "USE PIN 1234")
```

Enter **1234** on the phone when it prompts. Change it with `--pin 0000` etc. (any 4 digits).
`verify: PASS` — only the 42-byte routine body changed, inside its own footprint.

## 3. Entry (discoverable) — the remaining piece

With the PIN fixed, the only thing left is making the module **discoverable** without the dash:

- **First, a bench check (cheapest):** many BT basebands advertise/are connectable at power-on until
  a phone is bonded. If this one does, headless pairing already works now — power the module with no
  paired phone, scan from the phone, pair with `1234`. Scope `UA1TX` at boot or just try to discover
  it. (Boot sends *no* discoverable command, but the baseband may self-advertise.)
- **If it doesn't self-advertise → auto-discoverable patch (next step, ~1 hook):** call the host's
  "become discoverable" wrapper (the `0x6840–0x6EC8` → `0x18F08` path the HMI uses) once at startup
  when the paired-phone list is empty — i.e. do in firmware exactly what the FDIM "PAIR A NEW PHONE"
  selection does. Needs the exact wrapper/opcode pinned (the R/E found the family; identifying the
  specific discoverable opcode + a boot hook is the remaining work). I can implement this on request.
- **Alternative (no reflash):** once *any* MS-CAN tester is on the bus it can inject the FDIM HMI
  frame to drive the menu — but that defeats "headless," so the boot patch is preferred.

> Note: the SPP bridge link itself can't relay the PIN to bootstrap pairing — SPP isn't connected
> until *after* pairing. That's why hardcoding the PIN (not relaying it) is the right fix.

## 4. Headless pairing procedure (target)

1. Flash `8R29-14D511-JL-SPPCAN.PHF` (armed + branded + PIN `1234`; + auto-discoverable patch once §3
   is done, if the baseband doesn't self-advertise).
2. Power the module with no phone paired → it becomes discoverable.
3. On the phone: scan, select the module, enter **1234**.
4. Paired → open the SPP port → the OrionMaui app (or any SPP terminal) is live on MS-CAN.
