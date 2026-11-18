# BlueBridge — repurposing the BPM as an OEM Bluetooth⇄MS-CAN gateway + dash text injector

## The idea in one line

The BPM is already **a Bluetooth radio + a UART + an MS-CAN controller + a V850, bolted
into the car's harness on switched power** — so turn it into a *transparent Bluetooth-serial
⇄ MS-CAN bridge* that also **writes arbitrary text to the factory FDIM**. A factory-hardware,
factory-connector TP-CANBridge that hides in plain sight, with zero aftermarket box and zero
extra loom.

Everything it needs is proven in `BPM-analysis.md`:

* a **serial Bluetooth port** (UARTA1, `UA1RX 0xFFFFFA16` / `UA1STR 0xFFFFFA14`, 300-byte RX ring),
* an **MS-CAN interface** (aFCAN @ `0x03FEC000`, module ID `0x781`, 125 kbit/s),
* an existing **"header + 4 lines" / 8-item-list text channel** it already paints on the FDIM.

Phone/laptop ⇄ **Bluetooth SPP** ⇄ BPM ⇄ **MS-CAN** ⇄ car. That's the entire product.

## Why this module specifically

- **It's OEM and invisible.** Lives on the real BPM connector, draws from the car's existing
  BT module slot. No one sees a dongle hanging off the OBD port.
- **It's already a bus-legal node.** NM heartbeat, 0x781 ID, correct bit timing — the car
  already expects it present, so it won't throw "module missing" faults the way a yanked
  node would.
- **It already renders to the dash.** You don't have to invent a dash-text path — the BPM's
  media/menu/list channel to the FDIM is sitting right there.
- **It maps 1:1 onto your stack.** This is TP-CANBridge's job on OEM silicon; TES / TES Lite
  is the client; and it's the literal "phone-as-key over BT, actuate over CAN" shape of the
  BLE-CAN immobiliser.

## What you can build on top of it

1. **Wireless MS-CAN sniffer/injector.** Stream every MS-CAN frame to a phone/laptop over
   SPP and inject frames back. Body/comfort/infotainment diagnostics that a generic ELM327
   (HS-CAN/OBD only) can't reach — climate, ICC, BCM, audio.
2. **Hidden dash notifier.** Reuse the FDIM text channel to push live data onto the factory
   screen: TES gauge values, DTC pop-ups, boost/AFR from the Barra, "service due", geofence
   alerts — rendered OEM, no head-unit mod.
3. **Phone-auth gate.** Only a paired handset's MAC/BT-link can open the SPP→CAN path →
   the authentication front-end for the BLE-CAN immobiliser, on hardware already trusted by
   the car.
4. **Config/flash bridge for other TES gear.** Wireless front door to anything else on
   MS-CAN (e.g. flashing/arming TP-CANBridge nodes) without plugging in.

## Two build routes (be honest about effort)

### A. Patch the OEM firmware — lowest effort, keeps HFP working
Leave the BT stack and CAN driver intact; add two services by hooking existing dispatchers:

- **UART side:** the RX ISR at `0x17C4C` already frames bytes into a ring at `-0x62d6[gp]`.
  Add a service-channel decoder on that ring: a framed `RAWCAN <id><dlc><data>` command →
  push to an aFCAN TX buffer; and a `TEXT <row> <string>` command → feed the existing FDIM
  text path.
- **CAN side:** at the CAN-RX handler (follow vec `0x0B0 → 0xED64`), tee received frames out
  UARTA1 as SPP bytes.
- Needs: the UART framing/opcodes to the baseband (bench capture), the aFCAN TX routine
  (writes near `0x03FEC000`/`0x03FEC058`), and the FDIM text-send function. Smallest diff,
  HFP stays alive, the car stays happy.

### B. Blank-slate V850 firmware — most control
Fresh app using the known-good aFCAN config (`0x3DF00` block) + UARTA1 init + a thin SPP
framing layer. Drop HFP, make the module a pure gateway. More work, but it develops and
tests entirely under **this repo's V850ES emulator** (model the BT baseband as a UART peer,
run it on the virtual MS-CAN bus next to the FDIM/AIM images, exactly like the AIM↔FDIM
iPod demo). Add a `bpm.bin` auto-detect to `emu/main.c` (256 KB image, BPM header) so the
emulator runs it like the other units.

## Minimal on-wire protocol (SPP framing)

```
host -> BPM :  7E <len> <cmd> <payload...> <crc8> 7E
   cmd 0x01 RAWCAN   : <id_hi id_lo> <dlc> <d0..d7>         -> aFCAN TX
   cmd 0x02 TEXT     : <channel> <row> <utf8...>            -> FDIM media/menu/list
   cmd 0x03 FILTER   : <id_lo id_hi mask...>                -> set RX pass list
BPM -> host :
   evt 0x81 CANFRAME : <ts> <id> <dlc> <data>               (tee of MS-CAN RX)
   evt 0x82 STATUS   : <nm state> <bt link> <volts>
```

Framed, CRC'd, and small enough to live inside the 300-byte RX ring without touching the
BT stack's own buffers.

## Bench prerequisites (before writing a byte of patch)

1. Scope `UA1TX`/`UA1RX` at boot → baud + the host↔baseband framing, and read the baseband
   part off the PCB.
2. Confirm MS-CAN transceiver class on the BPM connector (HS vs LSFT) — same `Gotcha #1`
   caveat as the FDIM Pi spec.
3. Capture the BPM's real TX set (NM + the FDIM text frames) so a patched build keeps the
   car's NM master happy and reuses the exact text-channel IDs.

## Status

Firmware-confirmed: interfaces, SFRs, ISR roles, the dash text channel, CAN cfg block.
Needs a car/bench capture: UART framing + baseband identity, aFCAN TX routine address, and
the FDIM text-channel CAN byte layout. Those three close the loop from "proven feasible" to
"flashable."
