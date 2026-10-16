# Bee wearable: BLE protocol reverse-engineering notes

Source: static analysis of `com.bee.android` v1.0.3 (versionCode 79), extracted
from `base.apk` in this directory, via REA + headless JADX (jadx-headless-mcp
0.7.1). All facts below are **static observations from decompiled Kotlin/Java**
(smali-fallback where noted); nothing here was observed by running the app or
sniffing live traffic. Treat anything marked *(inferred)* as a reasonable but
unverified deduction; everything else is read directly from decompiled source.

APK digest (base.apk): `sha256:f151c3360e42ac601b4bb6c6ecd8dc4356c1302b3f76b83a1437ed2c6bd261ef`

## Architecture

The app is a React Native app (`com.bleplx.*` / `no.nordicsemi.android.ble.*`
present for in-foreground BLE use from JS) **plus** a separate, independent
native BLE stack that runs as a bound/foreground Android `Service`
(`com.bee.background.BeeBackgroundService`) so the wearable stays connected
and controllable while the RN JS layer is backgrounded or killed. This native
stack is where all of the protocol logic below lives:

```
com.bee.background/
├── BeeBackgroundService            : the long-lived Android Service
├── BeeAdvertisement                : parses/builds BLE advertisement data
├── ServiceReconnectController      : scanning + reconnect state machine
├── BeeBackgroundModule             : React Native bridge (pairing/association)
└── protocols/
    ├── BluetoothProtocol           : GATT transport: connect, write queue, notify
    ├── BeeGattConnection           : thin wrapper over android.bluetooth.BluetoothGatt
    ├── BeeGattOperationQueue       : serializes writes/subscribes/MTU/discovery
    ├── BeeProtocol                 : command/notification router, owns plugins
    ├── BeeRequest                  : sealed class: every command the phone can send
    ├── BeeNotification              : sealed class: every message the device can send
    └── plugins/
        ├── BeeProtocolDeviceControlPlugin   : brightness, power off (what the app's own UI exposes)
        ├── BeeProtocolMutePlugin            : mute/unmute via SetDeviceState
        ├── BeeProtocolPowerSavingPlugin
        ├── BeeProtocolButtonEventsPlugin    : physical button press/hold/double-press
        ├── BeeProtocolAudioStreamPlugin     : audio characteristic streaming
        ├── BeeProtocolAudioMessagePlugin
        ├── BeeProtocolJournalSoundPlugin
        ├── BeeProtocolRssiPlugin
        ├── BeeProtocolSessionTimeoutPlugin
        ├── BeeProtocolUtterancePlugin
        └── BeeProtocolLifecyclePlugin
```

The stock app's own UI only drives **brightness** and **power off**
(`BeeProtocolDeviceControlPlugin`). Everything else below (sleep modes, TX
power, bitrate, raw device/battery/firmware queries) is wired into the
protocol but not exposed as end-user controls in the shipped app; this is
the gap a custom "manual controls" tool fills.

## Transport (GATT)

| Item | Value | Source |
|---|---|---|
| Primary GATT service UUID | `03d5d5c4-a86c-11ee-9d89-8f2089a49e7e` | `BeeProtocolKt.SERVICE_ID`, decompiled `<clinit>` |
| Control characteristic UUID | `05e1f93c-d8d0-5ed8-dd88-379e4c1a3e3e` | `BeeProtocolKt.CONTROL_CHARACTERISTIC_ID` |
| Audio characteristic UUID | `b189a505-a86c-11ee-a5fb-8f2089a49e7e` | `BeeProtocolKt.AUDIO_CHARACTERISTIC_ID` |
| CCCD (notification enable) | standard `0x2902` | `BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE`, written in `BluetoothProtocol.processQueue` |
| Write type | `WRITE_TYPE_DEFAULT` (2, write-with-response) | `gatt.writeCharacteristic(characteristic, value, 2)` in `processQueue` |
| MTU requested | 517 | `GattOperation.RequestMtu` handling, `gatt.requestMtu(517)` |
| Connect flow order | connect → request MTU → discover services → subscribe control char (write CCCD) → ready | inferred from `GattOperationQueue` enqueue order and `onServicesDiscovered` |

All reads/writes are serialized through `BeeGattOperationQueue`; one GATT
operation in flight at a time, advanced only after the matching Android BLE
callback fires. `BluetoothProtocol` retries a failed op after 100ms up to
`MAX_CONSECUTIVE_OPERATION_FAILURES` times, then tears down the link
(`shouldForceReconnect`).

## Wire format

Every message on the **control characteristic**, in both directions, is:

```
byte 0-1 : command ID, little-endian uint16
byte 2.. : payload (command-specific, may be empty)
```

Source (`BeeRequest.encode()`):
```java
public final byte[] encode() {
    byte[] data = data();
    byte[] out = new byte[data.length + 2];
    out[0] = (byte) (command & 0xFF);
    out[1] = (byte) ((command >> 8) & 0xFF);
    System.arraycopy(data, 0, out, 2, data.length);
    return out;
}
```
Decode is the mirror image (`BeeNotification.Companion.decode`): read the
little-endian uint16 from bytes 0-1, slice the rest as payload, dispatch.

### Outer response/event codes (top-level dispatch in `BeeNotification.Companion.decode`)

| Code (hex) | Code (dec) | Meaning |
|---|---|---|
| `0x8000` | 32768 | OK: generic success wrapper; inner 2 bytes of its payload re-encode the **original request's command ID**, used to route to the right response type (see table below) |
| `0x8001` | 32769 | Error |
| `0x8002` | 32770 | InvalidParam |
| `0x8003` | 32771 | UnknownMessage |
| `0x8004` | 32772 | SetEncryptionKeyResponse (OK variant, specific to the encryption-key handshake) |
| `0xA001` | 40961 | Event: button double-press |
| `0xA002` | 40962 | Event: button hold (payload: 1 byte, `1`=pressed) |
| `0xA003` | 40963 | Event: device state changed (payload: see DeviceState below) |
| `0xA004` | 40964 | Event: battery level (payload: battery calc, see below) |
| `0xA005` | 40965 | Event: charging state changed (payload: 1 byte, nonzero = charging, adjusted by `BatteryLevelCalculator.chargingFromRaw`) |

### Commands (`BeeRequest` subclasses): phone → device

| Command | ID (hex) | ID (dec) | Request payload | Response payload (inside `0x8000` OK wrapper) |
|---|---|---|---|---|
| `GetTxLevel` | `0xC002` | 49154 | (none) | 1 byte: tx power (unsigned) |
| `SetTxPower` | `0xC001` | 49153 | 1 byte: power (signed byte) | N/A |
| `GetChargingSpeed` | `0xC004` | 49156 | (none) | N/A *(decode not yet analyzed)* |
| `GetFirmwareVersion` | `0xC005` | 49157 | (none) | 3-5 bytes: `[patch, minor, major, (hwRev?)]`; note byte order: `data[2]`=major, `data[1]`=minor, `data[0]`=patch, optional `data[4]`=hardware revision if length≥5 |
| `SetBitrate` | `0xC008` | 49160 | 4 bytes: bitrate, little-endian int32 | N/A |
| `GetBitrate` | `0xC009` | 49161 | (none) | 4 bytes: bitrate, little-endian int32 |
| `SetDeviceState` | `0xC006` | 49158 | 1 byte: state (see DeviceState enum) | 1 byte: `0`=muted boolean encoding (`data[0]==0`) |
| `GetDeviceState` | `0xC00A` | 49162 | (none) | 2 bytes: `[state, codec]` |
| `GetDeviceID` | `0xC00E` | 49166 | (none) | ≥8 bytes: device ID (decoded via `Dk.f.i`, likely hex/UTF string) |
| `GetBatteryAndChargeState` | `0xC00F` | 49167 | (none) | battery %, charging bool, optional millivolts; see Battery below |
| `GetBrightness` | `0xC017` | 49175 | 1 byte: `[1]` (fixed probe byte) | 1 byte: brightness 0-255 |
| `SetBrightness` | `0xC016` | 49174 | 2 bytes: `[1, brightness]` | N/A |
| `SetEncryptionKey` | `0xC018` | 49176 | N bytes: raw key bytes | handled by `0x8004` path |
| `GetSessionNonce` | `0xC019` | 49177 | (none) | 12 bytes: nonce |
| `GetSecureIds` | `0xC01B` | 49179 | (none) | 8 bytes device-id + 1 byte sealed-flag |
| `GetDSN` | `0xC01D` | 49181 | (none) | up to 16 bytes, ASCII, trimmed of trailing `0x00`/`0xFF` |

> ⚠️ Two different commands decompile to overlapping-looking hex near
> `0xC016`/`0xC017`: **`SetBrightness` is `49174` (`0xC016`)** while
> **`SetDeviceState` is `49158` (`0xC006`)**, and **`GetDeviceState` is
> `49162` (`0xC00A`)**. Re-verify against `encode()` output when implementing
> rather than trusting hand arithmetic; the authoritative source is each
> class's own `<init>` (`super(COMMAND_ID, null)`), reproduced verbatim above
> from REA's decompile output.

**`SetBrightness` payload clamp.** `BeeProtocolDeviceControlPlugin.setBrightnessInternal`
clamps to `[35, 100]` via `j.m(brightness, 35, 100)` before ever building the
request; originally assumed to be just an app-level UX choice layered on top
of an unrestricted 0-255 protocol range. **Corrected after live testing**:
firmware itself enforces `[0, 100]` (confirmed by binary search against real
hardware; values 0 through 100 all succeed, 101 and every higher value
tested up to 150 return `InvalidParam`). So the app's `[35, 100]` clamp is a
*stricter* UX-level floor on top of firmware's real `[0, 100]` limit, not
the actual limit itself; `bee-control`'s brightness slider uses the
confirmed `[0, 100]` range.

### `SetDeviceState` state byte (`DeviceState` enum, confirmed from decode switch)

| Value | Name | Notes |
|---|---|---|
| `0` | `MUTE` | mic/listening disabled |
| `1` | `RECORDING` | normal/active listening state |
| `2` | `SLEEP_HW_VAD` | sleep, hardware voice-activity-detection wake |
| `3` | `POWER_OFF` | `BeeRequest.SetDeviceState.Companion.powerOff()` |
| `4` | `SLEEP_SW_VAD` | sleep, software voice-activity-detection wake |

`BeeRequest.SetDeviceState` also has a `boolean` constructor used by the mute
plugin: `new SetDeviceState(mute)` → `state = mute ? 0 : 1` (i.e. `true` =
`MUTE`, `false` = `RECORDING`). Only `powerOff()`/`sleepHwVad()`/`sleepSwVad()`
factory methods exist on the Companion; there's no `recording()`/`mute()`
named factory; those two states are only reached via the boolean constructor.

### `CodecType` enum (byte 1 of `DeviceState`/`EventDeviceState` payload)

| Value | Name |
|---|---|
| `0` | `OPUS` |
| `1` | `AAC` |
| other | `UNKNOWN` |

### Battery / charging payload decoding

Two formats depending on hardware revision (`BatteryLevelCalculator.isFrontier`,
true for hardware revision `3` or `4`; see `frontierHardwareRevisions`):

- **Non-Frontier** (older hardware): `data[0]` = battery percentage directly,
  `data[1] != 0` = charging.
- **Frontier** hardware: `data[0:2]` little-endian uint16 = millivolts, fed
  through `BatteryLevelCalculator.frontierPercentageFromMillivolts(mv)` to
  get a percentage, plus `chargingFromRaw(data[2], hardwareRevision)`.
  *(The millivolt→percentage curve and `chargingFromRaw` logic were not
  decompiled in this pass; follow-up if precise battery % matters.)*

## BLE advertisement format (scanning / pairing)

The app pairs via Android's `CompanionDeviceManager`
(`BeeBackgroundModule.requestAssociation`), filtering on:

1. The GATT service UUID above (`03d5d5c4-...`) as a standard
   `ScanFilter.setServiceUuid`, and
2. Bee's own BLE manufacturer-specific advertisement data, as a belt-and-braces
   filter (`BeeAdvertisement.discoveryScanFilters`).

### Manufacturer-specific advertisement data (`BeeAdvertisement`)

| Constant | Value | Meaning |
|---|---|---|
| `LEGACY_BLUETOOTH_COMPANY_IDENTIFIER` | `48878` (`0xBEEE`) | older hardware |
| `FRONTIER_BLUETOOTH_COMPANY_IDENTIFIER` | `48879` (`0xBEEF`) | current ("Frontier") hardware |
| Device-ID encoding | bytes `[0:8]`, two little-endian uint32s, formatted `"%08X%08X"` of `(LE32(data,4), LE32(data,0))` | both legacy and Frontier |
| Frontier extra byte 8 | must equal `1` (format marker) else data is rejected | |
| Frontier extra byte 9 | hardware revision (int, unsigned) | `3`=`HARDWARE_REVISION_FRONTIER_C`, `4`=`HARDWARE_REVISION_FRONTIER_D` |
| Frontier extra byte 10, bit 0 | bonded flag | `BONDED_FLAG` |

So a Frontier device's manufacturer-data payload is 11 bytes:
`device_id_lo(4) device_id_hi(4) 0x01 hw_rev bonded_flags`.

## Confirmed against real hardware (first live scan)

A live macOS BLE scan (unfiltered, via `bleak`/CoreBluetooth) with the phone's
Bluetooth turned off (so the device fell back to advertising) found:

```
name='Bee'  service_uuids=['0000d5c4-0000-1000-8000-00805f9b34fb']
mfg={'0xbeee': '<8 bytes, device-specific>'}
```

This confirms several things the static analysis alone couldn't:

- **Manufacturer company ID is `0xBEEE`** (48878, `LEGACY_BLUETOOTH_COMPANY_IDENTIFIER`).
  *(Correction: an earlier pass of this doc miscomputed this as `0xBEAE`:
  48878 in hex is `0xBEEE`, not `0xBEAE`. Likewise `0xBEAF` → `0xBEEF`.)*
- **This unit advertises the 8-byte legacy manufacturer-data format**, not the
  11-byte Frontier format (the payload is exactly 8 bytes, no
  format/hw-rev/bonded trailer). So for this device, battery/charging
  decoding should use the **non-Frontier path** (`data[0]`=percentage,
  `data[1]!=0`=charging); the harder Frontier millivolt-curve case documented
  above as a gap likely doesn't apply here.
- **Device ID from manufacturer data**: per `parseDeviceId`, given the 8
  manufacturer-data bytes `b0 b1 b2 b3 b4 b5 b6 b7`, the device ID string is
  `f"{LE32(data,4):08X}{LE32(data,0):08X}"`: i.e. the little-endian 32-bit
  word from bytes 4-7, followed by the little-endian 32-bit word from bytes
  0-3, each as 8 uppercase hex digits. This was confirmed byte-for-byte
  against a real device's `GetDeviceID` response and its advertisement data
  (same value from both); the real ID is device-specific and omitted here.
- **The device does *not* advertise the 128-bit control service UUID
  (`03d5d5c4-...`) directly.** It advertises a short, SIG-base-form 16-bit
  UUID, `0x0000D5C4` (`0000d5c4-0000-1000-8000-00805f9b34fb`), as its
  discovery/advertised service UUID instead. The 128-bit `SERVICE_ID` is only
  visible after connecting and running GATT service discovery. **This means
  scanning/filtering for the Bee by the control-service UUID (as the first
  version of `bee_control/client.py` did) fails; filter on the advertised
  name (`"Bee"`) and/or manufacturer company ID (`0xBEEE`/`0xBEEF`) instead,
  then connect and discover services to reach the real control
  characteristic.**
- **A brand-new/unbonded central cannot connect at all while the device is in
  its normal "reconnect to last phone" state**: three connection attempts
  with a 20s timeout each failed outright at the OS Bluetooth layer (never
  even reached GATT). The device has to be put into an explicit discoverable/
  pairing mode (the exact physical trigger, e.g. a button press/hold, is
  not yet documented here) before it accepts a connection from a new central.
  Once in that mode, connecting is immediate and reliable.
- **Full GATT connect confirmed.** Connecting exposes exactly the services/
  characteristics predicted from static analysis:
  - `03d5d5c4-a86c-11ee-9d89-8f2089a49e7e` (control service)
    - `05e1f93c-d8d0-5ed8-dd88-379e4c1a3e3e`: properties `notify`,
      `write-without-response`, `write` (matches `CONTROL_CHARACTERISTIC_ID`)
    - `b189a505-a86c-11ee-a5fb-8f2089a49e7e`: properties `notify` only
      (matches `AUDIO_CHARACTERISTIC_ID`)
  - `8d53dc1d-1db7-4cd3-868b-8a527460aa84`: **not seen in static analysis**,
    a third service with one characteristic
    `da2e7828-fbce-4e01-ae9e-261174997c48` (`notify`,
    `write-without-response`). Purpose unknown; candidate for DFU/OTA or a
    diagnostics channel; follow-up decompilation target.
- **Live command/response round-trip confirmed** for `GetDeviceState`,
  `GetBrightness`, `GetFirmwareVersion` (returned `0.6.5`), `GetTxLevel`
  (returned `8`), `GetBatteryAndChargeState` (returned `70%`, not charging,
  confirming the legacy 2-byte format), `GetBitrate` (returned `8000`),
  `GetSecureIds`, `GetSessionNonce` (returned all-zero: session/encryption
  not established), and `SetBrightness` (write-then-read round trip
  confirmed the new value stuck). All matched the statically-derived decode
  logic with one correction (see next point).
- **`GetDeviceID`'s response payload is the raw 8-byte ID, not a UTF-8
  string**: my first implementation guessed `data.decode("utf-8")` from the
  Kotlin call `Dk.f.i(data, null, 1, null)` alone, which was wrong. The raw
  bytes are byte-for-byte identical to the device-ID field in the BLE
  advertisement's manufacturer data, so the correct decode is the same
  transform as `BeeAdvertisement.parseDeviceId`:
  `f"{LE32(data,4):08X}{LE32(data,0):08X}"` (see above).
- **`GetDSN` returns `UnknownMessage` (`0x8003`) on this unit.** This is a
  legacy-hardware device (manufacturer company ID `0xBEEE`); DSN may be a
  Frontier-hardware-only (`0xBEEF`) command. Unconfirmed; would need a
  Frontier unit to verify.
- **`DeviceState` (MUTE/RECORDING/SLEEP_*/POWER_OFF) does *not* appear to
  control the device's actual onboard recording/transcription.** This
  wearable is designed to record/transcribe continuously and independently
  of any phone BLE connection. In testing: (a) writing
  `SetDeviceState(RECORDING)` got an OK-wrapped ACK (`SetDeviceStateResponse`
  reporting `muted=False`) but an immediate `GetDeviceState` in the *same*
  connection still reported `MUTE`; the ack didn't reflect a real state
  change; (b) a real physical button double-press and hold/release produced
  genuine `EventButtonDoublePress`/`EventButtonHold` notifications, but no
  accompanying `EventDeviceState` event, and `GetDeviceState` still read
  `MUTE` afterward. **Working hypothesis, not confirmed:** this GATT value
  may govern something narrower (e.g. live audio streaming to a connected
  phone) rather than the device's autonomous recording loop, which likely
  runs in firmware independent of any BLE command. Session/encryption state
  may also matter; `GetSessionNonce` reads all-zero and `GetSecureIds`
  reports `isSealed=false` on this connection, i.e. no authenticated session
  was ever established, and some writes may be silently no-op'd without one.
  Needs `BeeProtocolButtonEventsPlugin`, `BeeProtocolUtterancePlugin`, and
  `BeeProtocolSessionTimeoutPlugin` decompiled to pin down what's actually
  gating this.

  **Follow-up: `BeeProtocolButtonEventsPlugin` decompiled.** Its
  `handleControlMessage` (which receives `EventButtonHold`/
  `EventButtonDoublePress`) does **not** send any BLE command back to the
  device; there's no `runtime.sendCommand(...)` call anywhere in it. Instead
  it relays `"onpress"` / `"onrelease"` / `"ondoublepress"` through a field
  typed `Ii.e`, whose method shape (constructor `(Manager, String, Options)`,
  an `emit(String, Object[]): Ack`-style method, `connected()`, `connect()`,
  ack-callback params typed `Qi.d`) matches the **Socket.IO Java client**
  (`io.socket.client.Socket`) almost exactly: i.e. button events are relayed
  over a Socket.IO connection (likely to the app's own local/cloud backend
  for the transcription pipeline), not echoed back to the wearable over BLE.
  It also calls `onUtteranceDetected.invoke()` and, if
  `holdActionProvider.invoke() == "memo"`, `playInitialMemoSound.invoke()`:
  both app-side UX callbacks, not device commands.

  **Updated picture:** this supports the working hypothesis above: the
  wearable's own recording/transcription loop runs autonomously in firmware;
  the phone app's role (when connected) is to relay button events to its own
  backend/pipeline and to play local UX sounds, not to drive recording
  state over BLE. The `BeeProtocolUtterancePlugin` and
  `BeeProtocolSessionTimeoutPlugin` *interfaces* (zero own methods/fields;
  they're Kotlin interfaces implemented by other plugins, not concrete
  classes) weren't decompiled further since the concrete answer was found in
  the button-events plugin. Going further (e.g. implementing the
  `SetEncryptionKey`/session-nonce handshake to see if authenticated writes
  behave differently) would mean reverse-engineering real cryptography, not
  just protocol framing; a materially bigger undertaking, and not attempted
  here.

  **Confirmed live, directly observed on physical hardware (device owner's
  own report while testing `bee-control-ui`): a single physical button press
  toggles the device's own recording LED on/off, and audio streaming
  genuinely starts/stops with it** (LED on → streaming; press → LED off →
  streaming stops; press again → LED on → streaming resumes). This
  corroborates the autonomous-firmware picture above with a direct physical
  observation rather than just an absence-of-BLE-command inference: the
  device decides for itself, locally, when to record; the button isn't
  just a UX event relayed to the app, it's the real control for the
  device's own recording/streaming state.

  **Follow-up, also confirmed live: `EventDeviceState` (`0xA003`) now fires
  for real, with a live encrypted session established**: earlier testing
  (see above) saw a physical button press produce `EventButtonHold` with
  *no* accompanying `EventDeviceState`, but that was before this session's
  encrypted-session fix for `SetDeviceState` sticking. With a session now
  established, a background poll of `GetDeviceState` while the button was
  pressed caught clean `MUTE → RECORDING → MUTE → RECORDING → MUTE`
  transitions, and the connection's own log independently captured the
  matching push notifications directly: `EventDeviceState payload=0101`
  (state=RECORDING, codec=AAC) then `payload=0001` (state=MUTE, codec=AAC).
  So the device does push this notification itself, in real time, carrying
  the new state directly in the payload; a strictly better signal than
  reading `GetDeviceState` back after the fact. `bee-control-ui`'s
  `Session.on_event` (`src/bee_control/webapp.py`) now reacts directly to
  `EventDeviceState` to start/stop its own local capture (BLE-subscribe +
  save to disk): as soon as the device reports it's no longer RECORDING, it
  stops and saves the file right then; taking the opportunity to finalize
  it exactly when the device itself stops streaming, rather than waiting for
  a button-press event to prompt a readback. This also means it's no longer
  specific to button presses at all: *any* cause of a real state change
  (button, or otherwise) is caught the same way, and it can't drift out of
  sync the way a local press-counted toggle heuristic could.

  **`power-off` confirmed to actually work**, unlike `mute`: sending
  `SetDeviceState(POWER_OFF)` caused the BLE connection itself to drop
  mid-write (`disconnected` from the OS), and a follow-up unfiltered scan
  found no Bee advertisement at all; genuinely powered off, not just
  disconnected-but-alive. So unauthenticated `SetDeviceState` writes are not
  universally no-op'd; `POWER_OFF` (3) takes effect while `MUTE`/`RECORDING`
  (0/1) did not in testing. This narrows the open question specifically to
  the mute/recording states rather than the command as a whole, and makes an
  auth/session-gating theory for *all* writes less likely (if that were the
  mechanism, power-off should have been blocked too); the mute/recording
  states may instead be genuinely distinct from whatever this wearable's
  firmware treats as its real "is recording" signal.

  **`sleep hw-vad` also confirmed working**: sending
  `SetDeviceState(SLEEP_HW_VAD)` kept the connection alive (unlike
  `POWER_OFF`, which drops it), and a subsequent `GetDeviceState` read back
  `SLEEP_HW_VAD` correctly. So three of the five `DeviceState` values are now
  independently verified to take real effect unauthenticated: `POWER_OFF`,
  `SLEEP_HW_VAD`, and (via `SetBrightness`, a different command but same
  no-auth-needed pattern) general command writes work fine. Only `MUTE`(0)/
  `RECORDING`(1): the two states reachable via `SetDeviceState`'s `boolean`
  constructor rather than a named `Companion` factory; fail to stick. That
  consistent split (named-factory states work; boolean-constructor states
  don't) is itself a clue worth following up: those two states may be
  handled by different firmware logic than the other three.

  **`sleep sw-vad` also confirmed working** (read back correctly as
  `SLEEP_SW_VAD` after sending), completing the pattern: all three
  `DeviceState` values reachable via `SetDeviceState.Companion`'s named
  factories (`powerOff()`, `sleepHwVad()`, `sleepSwVad()`) take effect
  reliably and unauthenticated. Only the two values reachable solely via
  `SetDeviceState`'s `boolean` constructor overload: `MUTE`(0)/
  `RECORDING`(1), which is exactly what `BeeProtocolMutePlugin` uses; fail
  to stick. This is a clean enough split that it's unlikely to be
  coincidental; firmware likely treats mute/recording as a genuinely
  different kind of state transition than sleep/power (possibly requiring
  the session/encryption handshake, a retry protocol like the app's own
  `scheduleUnmuteRetries`, or some other precondition not yet identified)
  rather than there being a generic authentication gate on `SetDeviceState`
  as a whole.

## Notable plugins not yet fully decompiled (follow-up candidates)

- `BeeProtocolButtonEventsPlugin`: maps `EventButtonDoublePress`/`EventButtonHold` to app-level actions.
- `BeeProtocolAudioStreamPlugin` / audio characteristic: streaming audio codec framing (OPUS/AAC) over `AUDIO_CHARACTERISTIC_ID`, not yet decompiled byte-for-byte.
- `BeeProtocolPowerSavingPlugin`, `BeeProtocolSessionTimeoutPlugin`, `BeeProtocolUtterancePlugin`: higher-level session/power policy built on top of the primitives above.
- Encryption-key handshake (`SetEncryptionKey` / `GetSessionNonce` / `GetSecureIds`): present but not exercised; treat as a pairing/security handshake not required for basic manual control of an already-paired device. **Do not guess at this protocol's crypto**: sending malformed data here could affect pairing state.

## App architecture: audio capture, streaming, and transcription

This section answers a different question than the rest of the document:
not "how do I send BLE commands to the device" but "how does the shipped app
actually use the device; how does recorded audio get off the wearable, and
where/how does it become text?" All facts below are, as elsewhere, **static
decompiled observations** unless marked otherwise; nothing here was captured
from live network traffic (no MITM/packet capture was performed; only the
BLE side has been exercised against real hardware, documented above).

### Summary of the pipeline

```
[wearable mic, firmware-autonomous]
        │  BLE notify, AUDIO_CHARACTERISTIC_ID (b189a505-...)
        ▼
BluetoothProtocol.onCharacteristicReceived
        ▼
BeeProtocol.onAudioMessage  →  BeeProtocolAudioMessagePlugin (plugin interface)
        ▼
BeeProtocolAudioStreamPlugin.onAudioMessage
  - gates on AudioMessageProcessingDecision (muted? codec known? can process?)
  - decrypts the raw BLE packet: decryptPacket.invoke(rawMessage)
  - AudioMessageDecoder.processPacket(bytes, onDecodedChunk, onVoiceEnergy)
        ▼
BeeStreamer.sendAudio(bytes, deviceName, captureId, codec)
  - emits Socket.IO event "audio_data" (+ "O" suffix arg if codec==OPUS)
  - over a WebSocket connection to BeeClient.endpoint, authenticated with
    x-bee-proof / x-bee-secret / x-bee-ephemeral-token / x-amazon-access-token
    headers and OkHttp "attestation" (Play-Integrity-style) interceptors
        ▼
   [Bee's own, now Amazon-operated, backend, opaque to this analysis]
        ▼
BeeStreamer socket listeners (Socket.IO events received back):
  - "new_utterance_partial" → live/streaming partial transcript
  - "new_utterance"         → finalized transcript segment
  - "new_conversation" / "update_conversation_state" / "update_conversation" /
    "delete_conversation" (on a *second*, optional `apiEndpoint` socket)
  - "audio_file" / "final_audio_file" → assistant voice-reply audio;
    queued and played locally via `queueAudioFile`→`playNextInQueue`
    (downloaded over HTTPS from `{endpoint with wss://→https://}/assistant_responses/{filename}`
    and played with `android.media.MediaPlayer`)
```

**Transcription is server-side, not on-device and not in local app code.**
Native code (and, as far as this analysis went, the app generally) only
transports encrypted/encoded audio bytes to a backend and relays back
whatever events that backend emits (`new_utterance`, etc.); there is no STT
model, nor any text-producing logic, anywhere in the decompiled Kotlin/Java.
The handoff point from "this app's problem" to "opaque cloud service" is
exactly `BeeStreamer.sendAudio()`'s `emit("audio_data", ...)` call and the
corresponding `"new_utterance"`/`"new_utterance_partial"` listeners; nothing
past that boundary is visible from this APK.

### Evidence

**`BeeStreamer`** (`com.bee.background.BeeStreamer`) is the component that
owns the Socket.IO connection(s) and the assistant-audio playback queue.
Constructor (overload 0):
```java
public BeeStreamer(String endpoint, String apiEndpoint, Context context,
                    Function1 authHeadersProvider, Function2 onNewUtterance,
                    Function1 onNewUtterancePartial, jj.a onNewConversation,
                    Function2 onConversationCapturingChanged, jj.a onDispose) {
    ...
    Ii.e socket = Ii.b.a(URI.create(this.endpoint), socketOptions);
    this.socket = socket;
    attachMediaLifecycleHandlers(socket);
    attachMediaEventListeners(socket);
    if (this.apiEndpoint.length() > 0 && !apiEndpoint.equals(endpoint)) {
        // second socket connection to a distinct apiEndpoint, if configured
        ...
        this.apiSocket = eVarA2;
    }
    socket.O();  // connect
    ...
}
```
`Ii.e`/`Ii.b`/`Ji.a.InterfaceC0184a` are R8-renamed but structurally identical
to `io.socket.client.Socket` / `io.socket.client.IO` /
`io.socket.emitter.Emitter.Listener` from the real `socket.io-client-java`
library; same `IO.socket(URI, Options)` factory call shape, same
`Options.Builder` chain (`.setTimeout/.setReconnection/.setTransports(["websocket"])`
etc., abbreviated `.a()`/`.c()`/`.n()`/`.o()`/`.p()`/`.q()`/`.m()`/`.t()`/`.u()`
builder methods), same `Emitter.Listener.call(Object[])` callback shape. No
class literally named `io.socket.*` was found via class-name search (expected
under R8 minification; this is a structural match, not a confirmed package
name), and no literal `"io.socket"`/`"socket.io"` string was found by class
search either (class-name search only; a full string/byte search across
resources wasn't performed in this pass).

`sendAudio`: the actual BLE-audio-to-cloud handoff:
```java
public final void sendAudio(byte[] data, String deviceName, String captureId, CodecType codec) {
    if (codec == CodecType.AAC) {
        this.socket.a("audio_data", data, deviceName, captureId);
    } else if (codec == CodecType.OPUS) {
        this.socket.a("audio_data", data, deviceName, captureId, "O");
    }
}
```
(`socket.a(...)` matches `Emitter.emit(String, Object...)`.)

`attachMediaEventListeners` registers these socket event names verbatim:
`"ping"`, `"new_utterance"`, `"new_utterance_partial"`, `"audio_file"`,
`"final_audio_file"`. `attachApiEventListeners` (on the optional second
socket) registers `"new_conversation"`, `"update_conversation_state"`,
`"update_conversation"`, `"delete_conversation"`.

`queueAudioFile` resolves assistant-reply audio to an HTTPS URL and plays it:
```java
String url = endpoint.replace("wss://", "https://") + "/assistant_responses/" + filename;
```

**`endpoint`/`apiEndpoint`/`authEndpoint` are not hardcoded in native code**:
`BeeClient`'s constructor takes them as plain `String` parameters with no
literal defaults in the decompiled constructor body, so they're supplied by
the React Native/JS layer at runtime (not found in this pass; would need JS
bundle analysis to pin down the actual hostname(s)).

**Auth headers**, from `BeeClient.buildSocketHeaders`:
`x-bee-proof`, `x-bee-secret`, `x-bee-ephemeral-token` (only if non-empty),
`x-amazon-access-token` (only if non-empty). The presence of a literal
`amazonAccessToken` field/header is notable; Bee (the company behind this
device) was acquired by Amazon; this is a direct code-level trace of that,
not an inference. `BeeClient` also has `reportLocation(...)` and
`reportPhoto(...)` methods posting to the backend independent of the audio
pipeline (matches the `CAMERA`/`ACCESS_FINE_LOCATION` permissions in the
manifest); out of scope for this pass but noted for completeness.

`AttestingOkHttpClientKt.buildAttestingOkHttpClient` (package
`com.bee.background.attestation`) wraps both the Socket.IO transport's
`OkHttpClient` and `BeeClient`'s own REST `OkHttpClient`; named
`"native-socketio"`/`"NativeSocketIO"` for the streaming client. Not
decompiled further in this pass, but the name and separate `attestation`
package strongly suggest a Play-Integrity-style device/app attestation
scheme layered under the `x-bee-*`/`x-amazon-*` headers above: i.e. the
backend is checking more than just the bearer tokens.

**`onAudioMessage`** (`BeeProtocolAudioStreamPlugin`, the plugin that
actually receives decoded `BeeNotification` audio-characteristic data) shows
the audio packets are encrypted in transit over BLE:
```java
if (connectionReady && (rawMessage = (byte[]) decryptPacket.invoke(rawMessage)) == null) {
    return;
}
```
`decryptPacket` is injected from outside this plugin (not traced further in
this pass); but its existence means **the `SetEncryptionKey`/
`GetSessionNonce` handshake documented above as "not exercised" is not
optional for audio**, even though it turned out to be unnecessary for the
`SetDeviceState`/brightness/power-off commands that were confirmed working
on live hardware without it. This is a plausible explanation for *why*
`MUTE`/`RECORDING` writes didn't stick in live testing: if those specific
transitions are gated by the same session/encryption state that audio
decryption needs, and that session was never established in the test
session (confirmed separately: `GetSessionNonce` read all-zero), that would
explain the mute anomaly without needing a different theory per-state.
**Still not confirmed: a hypothesis, not a traced code path from
`SetDeviceState` to the encryption/session subsystem.**

### Open questions / not resolved in this pass

- **Does the wearable buffer recordings onboard for bulk retrieval, or is
  capture only meaningful while BLE-connected-and-online?** No "list/fetch
  stored recordings" or file-transfer command was found among the `BeeRequest`
  commands documented above, and no Kotlin code was found referencing the
  third GATT service seen on live hardware
  (`8d53dc1d-1db7-4cd3-868b-8a527460aa84`, characteristic `da2e7828-...`), but that search was by class name only, not a full string/byte scan of the
  decompiled source, so this does **not** confirm the service is unused by
  native code, only that no class is named after its UUID. If the device
  really does record continuously independent of any phone connection (per
  the user's own description of the product), and there's no bulk-retrieval
  path in this BLE command set, the most likely explanation is that the
  "recording" that happens without a phone present is just local firmware
  state/buffering with a limited window, flushed live over BLE+Socket.IO
  whenever a connection is available; not a persistent on-device archive
  synced later. This remains unconfirmed.

  **Partial answer from physical inspection**: plugging the device into a
  Mac via USB does not surface any mass-storage volume, serial device, or
  USB device entry at all (checked via Finder and macOS's own System
  Report); it appears to be charging-only. This rules out "USB mass storage
  dump of locally buffered recordings" as a retrieval path, which is
  consistent with (though doesn't prove) the "limited-window live buffer,
  no persistent archive" theory above; there's no off-device route to bulk
  recordings other than the BLE+Socket.IO live-streaming path already
  documented.
- **Exact `endpoint`/`apiEndpoint`/`authEndpoint` hostnames**: supplied at
  runtime from the JS layer, not present as literals in native code. Finding
  them would need JS bundle extraction (`assets/index.android.bundle` or
  similar) and search, which wasn't attempted in this pass.
- **The attestation scheme details** (`AttestingOkHttpClientKt`); not
  decompiled.
- **`AudioMessageDecoder`/`AudioSessionManager`**: the components that
  actually frame/decode the raw BLE audio-characteristic bytes before
  `sendAudio`; were identified but not decompiled method-by-method in this
  pass.

## Local-only operation: encryption/session handshake and audio framing

This section traces the encryption/session handshake left unresolved above,
specifically to answer: **could a local-only client (no Bee/Amazon backend)
capture and decrypt this device's audio?** All facts below are static
decompiled observations unless marked otherwise; no live experiment was
attempted in this pass (no key was actually pushed to the device).

### The crypto is fully specified and simple: AES-128-CTR

`com.bee.background.protocols.PacketDecryptor`:
```java
public PacketDecryptor(byte[] key, byte[] sessionNonce) {
    if (key.length != 16) throw ...;          // AES-128
    if (sessionNonce.length != 12) throw ...;  // matches GetSessionNonce's 12-byte payload
}

private final byte[] decrypt(long seq, byte[] ciphertext) {
    byte[] iv = new byte[16];
    System.arraycopy(sessionNonce, 0, iv, 0, 12);
    iv[12] = (byte)(seq >> 24); iv[13] = (byte)(seq >> 16);
    iv[14] = (byte)(seq >> 8);  iv[15] = (byte)seq;
    Cipher cipher = Cipher.getInstance("AES/CTR/NoPadding");
    cipher.init(Cipher.DECRYPT_MODE, new SecretKeySpec(key, "AES"), new IvParameterSpec(iv));
    return cipher.doFinal(ciphertext);
}

public final byte[] decryptPacket(byte[] encrypted) {
    if (encrypted.length < 4) return null;
    long seq = bigEndianUint32(encrypted, 0);          // first 4 bytes of every packet
    return decrypt(seq, encrypted.sliceFrom(4));         // rest is ciphertext
}
```
So every encrypted BLE audio-characteristic packet is `[4-byte big-endian
sequence number][AES-128-CTR ciphertext]`, with the 16-byte CTR IV built as
`sessionNonce(12 bytes) || seq(4 bytes, big-endian)`. The sequence number is
read directly off each packet rather than tracked as mutable state, so
packets are independently decryptable given `key` and `sessionNonce` alone;
no running state, no per-packet handshake, and **no authentication tag**
(confidentiality only, no integrity check in this layer).

### Where `key` and `sessionNonce` come from

Handshake order (`ConnectionGuard.handleMessage`):
```java
public final void handleMessage(BeeNotification.OK message) {
    if (message instanceof BeeNotification.OK.SetEncryptionKeyResponse) {
        requestSessionNonce();   // -> BeeRequest.GetSessionNonce
    } else if (message instanceof BeeNotification.OK.SessionNonce) {
        configureDecryptor(((BeeNotification.OK.SessionNonce) message).getNonce());
    }
}
```
1. App sends `SetEncryptionKey` (phone → device) with some key-bearing bytes.
2. Device ACKs (`0x8004`/`SetEncryptionKeyResponse`); app immediately sends
   `GetSessionNonce`.
3. Device returns its 12-byte nonce; app builds the `PacketDecryptor(ctrKey,
   nonce)` and is now `State.READY`.

**`sessionNonce` is free and local**: it's a plain BLE read/response the
device hands to *any* connected phone that asks (confirmed in this session's
earlier live testing: `GetSessionNonce` round-trips fine unauthenticated; it
just read all-zero because no key had been set yet in that test).

**`key` (the AES-128 CTR key) is the hard part.** `ConnectionGuard.loadOrFetchKey`:
```java
private final void loadOrFetchKey(SecureIds ids) {
    // 1. Try Android's Google Play Services Block Store (secure per-account
    //    local/cloud-backed key-value store), keyed by "BLE_KEY_PREFIX" + device address
    Task retrieveBytes = this.blockstoreClient.retrieveBytes(...);
    retrieveBytes.addOnSuccessListener(/* found cached token -> sendEncryptionKeyToDevice(it) */)
                 .addOnFailureListener(/* not cached -> fetchKeyFromBackend(ids) */);
}

private final void fetchKeyFromBackend(SecureIds ids) {
    // POST {"deviceId": hex(ids.deviceIdData)} to https://{client.endpoint}/ble/secure-key
    // using the app's own authenticated OkHttp client (same auth headers as BeeStreamer)
}
```
So the key comes from one of exactly two places:
- **Android Block Store cache** (`Ob.b`/`BlockstoreClient`, Google Play
  Services), keyed by `"BLE_KEY_PREFIX" + normalizeDeviceAddress(address)`;
  populated the *first* time a given device is provisioned, after which the
  key is reusable from local cache without any further network call.
- **Bee's own backend**, `POST https://{endpoint}/ble/secure-key` with
  `{"deviceId": <hex of the device's 8-byte SecureIds data>}`, authenticated
  with the same `x-bee-*`/`x-amazon-*` headers documented above: i.e. this
  requires a valid, logged-in Bee/Amazon account session. `{endpoint}` itself
  is JS-supplied at runtime, not a literal in native code (same limitation
  noted above).

The response token is parsed by `parseTokenResponse` (raw
`application/octet-stream` bytes, or a JSON `{"token": "..."}` field that's
either comma-separated hex byte strings or Base64), then
`ConnectionGuard.Companion.extractCtrKeyFromSealedToken`:
```java
private final byte[] extractCtrKeyFromSealedToken(byte[] token) {
    if (token.length == 16) return token.copyOf();        // already the raw key
    if (token.length >= 28) return token.sliceRange(12, 28); // 16 bytes at a fixed offset
    return null;
}
```
**This is a plain byte-offset slice, not a decrypt/unseal cryptographic
operation**: there is no AEAD-open, no asymmetric decrypt, nothing. Whatever
"sealing" happened, happened server-side before the app ever saw the bytes;
by the time the token reaches the app it's either the bare 16-byte key, or a
fixed-layout blob (consistent with something like
`[12-byte nonce/label][16-byte key][16-byte tag]` = 44 bytes, though the
exact total length and the other bytes' purpose weren't confirmed; the app
never inspects or uses them) with the key simply sitting in plaintext at
offset 12. **The security of this key is entirely "whoever can authenticate
to Bee's backend as this account gets the key over TLS"; there is no
additional client-side cryptography protecting it once received.**

`sendEncryptionKeyToDevice(byte[] token)` sends the **original, un-sliced
token** (not the extracted 16-byte key) to the device, wrapped verbatim in
`BeeRequest.SetEncryptionKey(ByteBuffer.wrap(token))`:
```java
private final void sendEncryptionKeyToDevice(byte[] token) {
    byte[] ctrKey = extractCtrKeyFromToken(token);  // for the APP's own local use
    if (ctrKey == null) { resetToIdle(...); return; }
    this.ctrKey = ctrKey;
    if (getEncryptionKeySetOnDevice()) { requestSessionNonce(); return; }
    this.sendCommand.invoke(new BeeRequest.SetEncryptionKey(ByteBuffer.wrap(token)));
}
```
So the **device** receives whatever-length blob the backend/cache returned
(16 or ~44 bytes), while the **app** separately extracts just the 16-byte key
from the same blob for its own `PacketDecryptor`. Nothing in the decompiled
app code shows the device challenging, signing, or otherwise validating that
the key it was handed actually came from Bee's backend; the app simply
trusts whatever `0x8004` ack it gets back. **Whether the device's firmware
itself validates key provenance is unknown and unknowable from this APK**
(the firmware isn't present in this artifact); this is the single
load-bearing unknown for the "can we just push our own key" question below.

### Audio framing after decryption: standard ADTS AAC

`AudioMessageDecoder.processPacket` (per-packet, after decryption) implements
textbook **ADTS (Audio Data Transport Stream) frame sync**: it buffers
incoming decrypted bytes, scans for the standard `0xFFF` ADTS syncword
(`byte[i]==0xFF && (byte[i+1]&0xF0)==0xF0`), reads the 13-bit
`aac_frame_length` field from the standard ADTS header bit layout, and emits
each complete frame once enough bytes have buffered. This is **plain,
standard ADTS-framed AAC**, not a proprietary format, directly playable/
decodable by `ffmpeg`, Android's `MediaCodec`, or any standard AAC decoder
once you have the decrypted bytes. (The OPUS path, used when
`CodecType.OPUS`, was not decompiled in this pass; `BeeStreamer.sendAudio`
signals it to the backend via an extra `"O"` socket arg, but its local
framing in `AudioMessageDecoder`/`AudioSessionManager` wasn't traced.) Also
present but orthogonal to capture: `AudioMessageDecoder` bundles a local
**Silero VAD** ONNX-style model (`sileroModelBytes`,
`com.bee.background.audio.AacVadRmsDecoder`) used only for a local voice-
energy/RMS meter; not for transcription, but notable as existing local
infra that a from-scratch client would otherwise have to add itself for
voice-activity gating.

### Verdict

See the coordinating session's reply for the direct answer to "is this
feasible"; summarized here for the record: **the crypto and audio framing
are both fully understood, standard, and locally implementable (AES-128-CTR,
plain ADTS/AAC). The only real blocker is the one-time 16-byte AES key per
device**, obtainable either by extracting it from Bee's own Android app's
Block Store cache on a phone that has already paired (one-time, if
accessible) or by calling Bee's own authenticated backend (not
infrastructure-independent). **Untested but plausible given the evidence
above**: since nothing in app-side code shows the device validating the
key's origin, pushing a *self-generated* 16-byte key via `SetEncryptionKey`
(command `0xC018`, already implemented in this repo's `bee_control.protocol`)
might simply work; the device may accept any 16-byte key handed to it over
an already-connected GATT link and use it for all subsequent
`decryptPacket`-side AES-CTR operations, with no cloud involvement at all.
This was *not* tried against the live hardware in this pass (would change
the device's encryption/session state) and firmware-side validation can't be
ruled out from the APK alone; it's the highest-value next experiment, not a
confirmed fact.

### Tested against real hardware: the self-generated-key shortcut does NOT work

Implemented `req_set_encryption_key`/`BeeDevice.set_encryption_key` in this
repo and tried it live. Result: **every payload tried was rejected with
`InvalidParam`**: a random 16-byte key, and random payloads at every length
from 16 to 48 bytes in 4-byte steps. The rejection is identical regardless of
length, which rules out "wrong length" as the failure reason and points to
genuine **content/cryptographic validation by the device firmware**: i.e.
the device is not merely accepting any bytes of a plausible size, it's
actually checking the token's validity before accepting it (most likely a
MAC/signature check against a secret baked in at manufacturing, which only
Bee's backend, and whatever "sealed" the token server-side, would know).
The device itself was unaffected by the rejected attempts: a status read
immediately after showed it completely healthy (state, brightness, etc. all
normal), so these are safe, non-destructive probes, just unsuccessful ones.

**This closes off the hoped-for shortcut.** The "nothing in app-side code
validates key provenance" observation from static analysis turned out to
describe only the *phone app's* behavior (it trusts the backend's response
unquestioningly); it says nothing about what the *device firmware* checks,
which is invisible to APK decompilation. Getting a real, device-accepted key
appears to genuinely require either Bee's backend (an authenticated account
session) or extracting an already-provisioned key from a real paired
Android device's Block Store cache; both are "depend on Bee/Amazon's
infrastructure or a prior legitimate pairing," not a clean local-only path.

### Live packet capture from the real phone + real app (with explicit owner authorization)

With the device owner's explicit authorization (their own device, own
account), captured real BLE traffic between the owner's actual Android
phone (Samsung Galaxy S23 Ultra-class, `SM-S938U1`, Android 16) running the
real `com.bee.android` app and the wearable, via Android's Bluetooth HCI
snoop log. Note: Samsung writes this to `/data/log/bt/btsnoop_hci.log`, not
the AOSP-standard `/data/misc/bluetooth/logs/` path, and enabling it only
took effect via the actual Settings → Developer options UI toggle; setting
the underlying `secure bluetooth_hci_log` value via `adb shell settings put`
directly did not produce a working log on this device/build. The file is
not reachable via plain `adb shell` (permission denied, non-rooted
production build) but is bundled by `adb bugreport` (REA's
`collect_adb_bugreport`), which runs with elevated system privileges.
Parsed with a small script (standard BTSnoop v1 / H4 format → ACL data →
L2CAP → ATT PDUs), filtering for ATT Write Request/Command and Handle Value
Notification opcodes on the BLE connection's ATT channel (CID `0x0004`).

**Findings, across 7 full connect/disconnect cycles in the capture:**

- **`SetEncryptionKey` (`0xC018`) was never sent, not once.** Every cycle's
  command sequence is identical: `GetDeviceID` → `GetSecureIds` →
  `GetDeviceState` → `GetFirmwareVersion` → `GetBatteryAndChargeState` →
  `GetBitrate` → `GetTxLevel` → `SetDeviceState(MUTE)` → `GetSessionNonce` →
  (subscribe to a second characteristic, handle `0x0016`, presumably audio).
  This directly confirms, with real traffic rather than just reading the
  decompiled `if (getEncryptionKeySetOnDevice()) { requestSessionNonce();
  return; }` branch, that **the app only sends the key once, at original
  pairing time, and relies on the device remembering it indefinitely across
  reconnects.** The key exchange this session wanted to observe already
  happened before this capture started (when the device was first paired)
  and isn't present in ordinary reconnect traffic; capturing it would
  require a fresh pairing from scratch (e.g. forgetting/re-pairing the
  device in the real app), which was not attempted.
- **`GetSessionNonce` returns a real, different 12-byte value every single
  connection** (e.g. `987ac10ed45d6a9314f6353f`, `5bcaa0546369f1a12dd26603`,
  `f92461ead138e99dd3529ca3`, ...); never all-zero. This contrasts with
  this session's own earlier testing, where `GetSessionNonce` consistently
  read back all-zero on every connection from this repo's tool. **The cause
  of that discrepancy is unresolved**: plausible explanations include the
  device tracking key/session state globally (not per-central) so the
  already-paired-with-the-real-app device simply always has a nonce ready
  regardless of who asks, versus something about this repo's connection
  sequence (vs. the real app's exact command ordering/MTU negotiation)
  leaving the nonce generator unseeded. Not settled by evidence collected so
  far.
- **`SetDeviceState(MUTE)` (`06c000`) is sent by the real app at the start
  of every single connection**, byte-for-byte identical to what this repo's
  `req_set_device_state(DeviceState.MUTE)` produces; direct, real-traffic
  confirmation that this project's protocol encoding is correct. No
  `SetDeviceState(RECORDING)` (unmute) was seen anywhere in the capture, and
  no traffic at all touched the audio characteristic; consistent with the
  device owner not actively using the device (speaking to it) during this
  test window, not necessarily meaning unmute doesn't work via this
  command. This capture cannot settle the open "why didn't `mute off` stick
  in live testing" question from earlier in this document; it would need a
  capture that includes a real, observed unmute-then-audio-flows sequence
  for comparison.
- Only two ATT handles carried traffic on the control-characteristic side:
  `0x0012` (all `BeeRequest`/`BeeNotification` control-characteristic
  reads/writes/notifications) and `0x0013` (a single `0100` write each
  cycle; the CCCD enabling notifications on `0x0012`). `0x0016` (presumably
  the audio characteristic's CCCD) is written once per cycle but no
  subsequent audio notifications appear, consistent with the device
  remaining muted throughout.

### Captured the real key: a fresh re-pairing (with explicit owner authorization)

The device owner then performed a real re-pairing of their phone with the
device (forget + re-pair in the real app), with a fresh HCI snoop capture
running throughout. This time `SetEncryptionKey` (`0xC018`) appears exactly
once, with a **108-byte payload**, structured as:

```
bytes 0-3   : magic "SKV1" (ASCII)
bytes 4-11  : this device's 8-byte device ID (matches GetDeviceID/advertisement data exactly)
bytes 12-27 : 16 bytes: this is the AES-128-CTR key (per decompiled
              ConnectionGuard.extractCtrKeyFromSealedToken's token.sliceRange(12, 28)
              branch, now confirmed against the real captured token rather than
              just decompiled logic)
bytes 28-107: 80 bytes: opaque sealed/signed material, not decoded in this pass
```

The device's response was a plain `0x8000` OK wrapper echoing `0xc018` with
**zero-length payload**: i.e. a bare success ack, not the `0x8004`
`SetEncryptionKeyResponse` code the decompiled switch statement in
`BeeNotification.Companion.decode` suggested would be used. (`0x8004` may be
reserved for a different scenario, e.g. re-keying when a key is already set;
unconfirmed, this capture only shows the fresh-pairing case.) Immediately
after, `GetSessionNonce` returned a real, non-all-zero 12-byte nonce in the
same session; the key is now live on the device.

**This is the definitive answer to the open question from static analysis**:
the 16-byte AES-128-CTR key is real, extractable from a legitimately captured
pairing event, and was confirmed accepted by the device (clean OK response,
no error). No audio traffic occurred in this capture either (device stayed
idle/muted), so full decrypt-real-audio proof is still untested, but the key
material itself is now verified correct and in hand.

**Handling**: the captured key, token, and device ID are stored locally in
`device_secrets.json` at the repo root (gitignored, never committed) rather
than inline in this document, since it's live secret material for this
specific physical device, obtained with the device owner's explicit
authorization for their own hardware. Treat it accordingly; this is
equivalent in sensitivity to a device-specific password, not public protocol
documentation.

### The trailer does not cryptographically bind to the key: tested on real hardware

With the device owner's explicit authorization, tested whether the 80-byte
trailer in the captured token is a signature over the whole token (key
included) or something narrower. Took the real captured 108-byte token and
built a second one with the *same* `"SKV1"` magic, *same* 8-byte device ID,
*same* 80-byte trailer, but a freshly self-generated random 16-byte key
spliced into the middle. Sent both, back to back, in one connection:

1. The real, unmodified token: **accepted** (`0x8000` OK, empty payload:
   sanity check).
2. The key-spliced token: **also accepted**: this time via `0x8004`
   (`SetEncryptionKeyResponse`, echoing the command id `0xc018` as its
   payload) rather than `0x8000`, which resolves the open question from
   earlier: `0x8000` empty-payload OK is used for an initial/fresh key set
   (seen during the real re-pairing capture above); `0x8004` is used when
   *re-keying* a device that already has a key set (both writes in this test
   were re-keys, since a key was already live from the recent legitimate
   re-pairing).

**The device accepted a self-generated key**, as long as it was wrapped in
the real magic/device-id/trailer bytes. This directly demonstrates the
80-byte trailer does not commit to the specific key value; if it were a
signature over `magic || device_id || key`, changing the key would have
invalidated it. The simplest consistent explanation is that whatever
validation that trailer represents only covers `magic || device_id` (which
never changes for this physical device), not the key field.

**Implication, not yet fully confirmed**: if true, this one legitimately
captured trailer could be reused indefinitely to mint new self-chosen
AES-128-CTR keys for this device, with no further dependency on Bee's
backend or another real pairing, ever. This specific test only proves the
device accepts it within the same live connection as the real token was
just resent; not yet confirmed across a fresh disconnect/reconnect, and not
yet confirmed that normal command/audio traffic behaves correctly afterward
rather than just accepting the write while leaving some other internal state
unchanged. Follow-up verification pending.

### CONFIRMED: full local-only operation, including across a real disconnect/reconnect

> **Update from further down this document** ("Audio packet layout and ADTS
> framing" section): the "self-chosen key, forever" claim below holds for
> every *control-plane* command (sleep, power, brightness, mute/recording
> state) but turned out NOT to hold for audio; self-generated keys don't
> propagate to the device's real-time audio-encryption engine, only the
> genuinely captured key does. See that section for the full, corrected
> picture. Local-only operation (zero ongoing cloud dependency) is still
> fully achieved either way, just by reusing the one captured real key for
> audio specifically rather than rotating to an arbitrary one.

One correction to the above first: a follow-up test sending the
self-generated spliced key **without** first resending the real token in a
fresh connection was **rejected** (`Error`, `0x8001`); on a genuinely fresh
Mac-level Bluetooth pairing, not stale connection state (confirmed by
redoing the full forget-device-and-re-pair cycle before that attempt). So
the mechanism isn't quite "the trailer doesn't validate the key at all";
it's closer to **"the first `SetEncryptionKey` call in a fresh connection
must match the device's current real key to unlock the session; once
unlocked, further re-keys within that same connection aren't content-
validated."**

With that established, the full sequence was tested end to end:

1. Connect fresh (after a full forget-device/re-pair cycle on the Mac).
2. Resend the real captured token (`set_encryption_key(real_token)`):
   accepted, `0x8000`, empty payload.
3. Immediately push a **freshly self-generated** 16-byte key, spliced into
   the real magic/device_id/trailer; accepted, `0x8004`, echoes `0xc018`.
4. **Fully disconnect.**
5. **Reconnect fresh** (a new BLE connection, not a resumed session).
6. Exercise normal commands with no further key-related calls:
   `GetSessionNonce` → returned a real, fresh, non-zero 12-byte nonce;
   `GetDeviceState` → `MUTE`/`AAC`, normal values; `GetBrightness` → `50`,
   normal; `GetBatteryAndChargeState` → `90%`, not charging, normal.

**Every command succeeded normally.** The device is now operating on an
AES-128-CTR key that was never issued by Bee's backend, never touched the
real Android app, and exists only locally, generated with `os.urandom(16)`
in this repo's own test script. This is complete, end-to-end confirmation:
**local-only operation of this device, independent of Bee/Amazon's cloud,
is achievable**, bootstrapped from exactly one legitimately captured
pairing event (the magic + device ID + 80-byte trailer, which appears to be
reusable indefinitely to "unlock" new self-chosen keys on every future
connection).

**What's still untested, not load-bearing for the above conclusion**:
actually decrypting real captured audio with a self-chosen key end-to-end.
Every capture so far happened while the device was muted/idle, so no audio
characteristic traffic was ever observed to decrypt. Given the device now
provably accepts and operates correctly on a self-chosen key for every other
command, there's no remaining reason to expect audio decryption would behave
differently, but it hasn't been directly observed (see the next two
sections for what was tried and found since).

### The long-standing mute/recording mystery is resolved: it needs a live encrypted session

Immediately after implementing `unlock_and_set_local_key` and establishing a
real local key + session nonce in the same connection, `SetDeviceState
(RECORDING)` was retried; and this time **`GetDeviceState` correctly
reported `RECORDING` afterward**, the first time in this entire session that
this state transition was observed to actually stick (every earlier attempt, dating
back to this document's original live-hardware testing, left the device
reporting `MUTE` regardless of what was sent). The only thing that changed
between the earlier failures and this success is the presence of an
established encryption key + session nonce. This confirms the working
hypothesis from the audio-architecture section above: mute/recording state
transitions are gated by having a live encrypted session (unlike
`POWER_OFF`/`SLEEP_HW_VAD`/`SLEEP_SW_VAD`, which all work with no key setup
at all); **not** a general "writes are ignored" issue, and not something
specific to `DeviceState`'s boolean-constructor-reachable values as
previously guessed. `bee-control`'s `mute`/`sleep`/`power-off` CLI/web
commands should be updated to call `unlock_and_set_local_key` first before
relying on mute actually taking effect (not yet done as of this writing).

Despite achieving `RECORDING` state, a 20-second capture attempt (with the
device owner alternating between speaking and pressing the physical
button/LED-mode-toggle) still produced **zero** audio-characteristic
notifications; worth distinguishing in the next attempt whether raw
notifications are arriving at all (via `start_audio_capture`'s new
`on_raw_packet` diagnostic callback) versus arriving but failing to
decrypt/frame. Unresolved as of this writing.

## Audio packet layout and ADTS framing: byte-exact verification

Follow-up pass re-decompiling every step between the raw BLE audio
notification and `AudioMessageDecoder.processPacket`, specifically to check
this repo's `audio.py` implementation against the actual decompiled source
rather than the earlier summary, after live capture produced only one
suspiciously large (6366 then 6787 byte) "frame" per capture, with a
**different reported sample rate between the two captures** (16000Hz vs
11025Hz) from the same device/codec; a strong sign of decoding noise, not
real AAC.

**Raw packet layout confirmed exactly as assumed: no hidden header.**
Traced the full call chain with the actual decompiled source at each hop:

```
BluetoothProtocol.onCharacteristicReceived(characteristic, value):
    if characteristic.getUuid() == AUDIO_CHARACTERISTIC_ID:
        onAudioMessage(value)          // value = literal raw GATT notification bytes, untouched

BeeProtocol.onAudioMessage(byte[] message):
    // message is passed through unchanged
    audioMessagePlugin.onAudioMessage(message, canProcessAudio, connectionReady,
        decryptPacket = { packet -> connectionGuard.decryptPacket(packet) },
        deviceId, codec, muted)

BeeProtocolAudioStreamPlugin.onAudioMessage(rawMessage, ...):
    // after an AudioMessageProcessingDecision gate (muted/codec/etc, not byte-layout-relevant)
    rawMessage = decryptPacket.invoke(rawMessage)   // called on rawMessage AS-IS, no slicing first
    audioMessageDecoder.processPacket(rawMessage, onFrame, onVoiceEnergy)

ConnectionGuard.decryptPacket(encrypted):
    return this.decryptor.decryptPacket(encrypted)  // thin passthrough, no transform
```

So the raw ~495-byte BLE notification goes **directly** into
`PacketDecryptor.decryptPacket` with no packet-type byte, flag byte, or any
other header stripped first; confirms this repo's assumption.

**`PacketDecryptor` re-decompiled directly, byte-for-byte identical to this
repo's `decrypt_audio_packet`:**

```java
public final byte[] decryptPacket(byte[] encrypted) {
    if (encrypted.length < 4) return null;
    long seq = (((encrypted[0]&255)<<24) | ((encrypted[1]&255)<<16)
              | ((encrypted[2]&255)<<8)  |  (encrypted[3]&255)) & 0xFFFFFFFFL;
    return decrypt(seq, encrypted.sliceFrom(4));   // ciphertext = encrypted[4:]
}
private byte[] decrypt(long seq, byte[] ciphertext) {
    byte[] iv = new byte[16];
    System.arraycopy(sessionNonce, 0, iv, 0, 12);
    iv[12]=(byte)(seq>>24); iv[13]=(byte)(seq>>16); iv[14]=(byte)(seq>>8); iv[15]=(byte)seq;
    Cipher c = Cipher.getInstance("AES/CTR/NoPadding");
    c.init(DECRYPT_MODE, new SecretKeySpec(key,"AES"), new IvParameterSpec(iv));
    return c.doFinal(ciphertext);
}
```

Sequence number: big-endian uint32 from `encrypted[0:4]`. Ciphertext:
`encrypted[4:]`. IV: `sessionNonce(12) || seq_bytes(4, big-endian)`. AES-128-
CTR, no padding, no auth tag. **Exactly matches this repo's
`decrypt_audio_packet`**: ruled out as the bug.

**`ConnectionGuard.configureDecryptor` confirmed to apply no transform to the
nonce either:**

```java
private final void configureDecryptor(byte[] nonce) {
    if (ctrKey == null || nonce.length != 12) { state = State.IDLE; return; }
    this.decryptor = new PacketDecryptor(ctrKey, nonce);   // used as-is, no derivation/HKDF/reversal
    state = State.READY;
    ...
}
```

Called from `handleMessage(BeeNotification.OK message)`: on
`SetEncryptionKeyResponse` → `requestSessionNonce()` (sends
`GetSessionNonce`); on `SessionNonce` (the response) →
`configureDecryptor(nonce)`. **Event-driven, not request/response-future
based like this repo's client**: the real app's single notification
dispatcher reacts to every `0x8000`-wrapped OK by type-matching it, not by
keying a response to a specific pending request. This is an architectural
difference from this repo's `_send`/pending-future model, but not something
that should change the final key/nonce values used, since the OK payload
content is identical either way; noted for completeness, not identified as
a bug source.

**`AudioMessageDecoder.processPacket` re-decompiled in full: matches this
repo's `AdtsReassembler` almost exactly, with one real (but likely minor)
divergence, plus a 3-frame warm-up discard not previously documented:**

```java
public final void processPacket(byte[] decryptedMessage, onFrame, onVoiceEnergy) {
    adtsBuffer = adtsBuffer + decryptedMessage;   // append
    int i = 0, emitted = 0;
    while (i + 7 <= adtsBuffer.length) {
        if (adtsBuffer[i]==0xFF && (adtsBuffer[i+1]&0xF0)==0xF0) {       // sync word
            int frameLen = ((adtsBuffer[i+3]&3)<<11) | ((adtsBuffer[i+4]&255)<<3)
                          | ((adtsBuffer[i+5]&255)>>5);                  // standard 13-bit ADTS length
            if (frameLen <= 7 || i+frameLen > adtsBuffer.length) break;  // *** BREAKS, does not skip ***
            byte[] frame = adtsBuffer[i : i+frameLen];
            if (adtsWarmup > 0) { adtsWarmup--; /* DISCARD, no onFrame call */ }
            else { emitted++; onFrame.invoke(frame); }
            i += frameLen;
        } else {
            i += 1;    // no sync at i, advance one byte
        }
    }
    if (i > 0) adtsBuffer = adtsBuffer[i:];   // keep unconsumed tail for next call
}
```

`reset()`/the constructor both set `adtsWarmup = 3`: **the first 3
would-be-emitted frames are silently discarded**, not a bug but something
this repo's `AdtsReassembler` doesn't replicate (irrelevant to the observed
single-giant-frame symptom, but means a real capture needs ≥4 genuine frames
before the first one is actually usable, if replicating this exactly).

**The one real divergence**: when a sync word is found but `frameLen <= 7`
(too small to be a valid frame; header alone is 7 bytes), the real app
`break`s the entire loop immediately, leaving `i` exactly at the bad sync
position, and **waits** for more data on the next call without consuming
anything; it does **not** treat this as a corrupt match and skip past it.
This repo's `AdtsReassembler._try_extract_one` instead deletes 1 byte and
retries immediately. Both behaviors converge eventually, and `frameLen <= 7`
is only 8 of 8192 possible 13-bit values (~0.1% chance) for a random/false
sync match, so this is unlikely to be the dominant cause of the observed
single ~6.5KB "frame," but it's a genuine correctness bug worth fixing
regardless. **Neither the real app nor this repo's reassembler verifies a
second sync word immediately follows a found frame**; so both are
susceptible to false-positive matches on non-ADTS data; this is not a bug
unique to the reimplementation.

### Most likely actual cause: not a framing bug, a key mismatch

With the decrypt algorithm confirmed byte-identical and the raw-packet
layout confirmed to have no hidden header, the framing logic is close enough
to the original that it shouldn't by itself produce the observed symptom:
one implausibly large "frame" (6366 bytes, then 6787 bytes in a second,
independent capture) appearing only once, deep into a steadily growing
buffer, with the two captures disagreeing on sample rate despite being the
same physical device and codec. That pattern (a sync-word-like 12-bit
pattern appearing roughly once per several KB with no structural
consistency between runs) is the signature of **scanning effectively
random bytes**, not real (even imperfectly parsed) AAC data. Since the
decrypt math itself is verified correct against the real algorithm, the
remaining explanation is that the **key or nonce used for decryption during
that capture wasn't the one the device's firmware was actually using to
encrypt outgoing audio**.

The specific, testable hypothesis: this repo's capture used a
**self-generated key** spliced into the real token via
`unlock_and_set_local_key` (confirmed, elsewhere in this document, to be
accepted and to work correctly for every *control-channel* command
afterward (status reads, device state, etc.). But control-channel commands
plaintext-over-GATT don't actually require the firmware to be using the
"right" key for anything; only the write being ACKed. It's entirely
possible the device's firmware accepted and ACKed the `SetEncryptionKey`
write at the protocol level (satisfying everything this analysis can
observe from the APK/live GATT traffic) without actually replacing whatever
key its audio-encryption path uses internally: i.e. **the control-plane
"success" observed and documented elsewhere in this file does not by itself
prove the self-generated key is the one firmware uses for audio**, since
nothing in the decompiled app code shows the device confirming or exposing
its internal audio-crypto key state in any way checkable from here. This is
exactly the kind of thing that's invisible from static APK analysis (the
firmware itself isn't in this artifact) and can only be settled empirically.

**Recommended next experiment, directly actionable**: repeat the audio
capture using the **real captured key** (`device_secrets.json`'s
`aes_ctr_key_hex` (extracted from the real, unmodified token's bytes
12-28, i.e. the key that came from the actual legitimate pairing) instead of
a freshly self-generated one, with a freshly read `GetSessionNonce` in that
same connection. If that produces many small, valid, sample-rate-consistent
ADTS frames, it definitively proves (a) the decrypt/framing code in this
repo is correct, and (b) self-generated keys are accepted by the device's
control-plane but **not actually used for real-time audio encryption**:
meaning the "CONFIRMED: full local-only operation" conclusion elsewhere in
this document would need a caveat: control is local-only, but audio capture
may still require deriving/obtaining the *original* real key rather than an
arbitrary self-chosen one. If it *still* fails even with the real key, the
bug is genuinely in the decrypt/framing code (or something about nonce
timing) despite matching the decompiled source as closely as checked here,
and would need further investigation (e.g. logging raw decrypted bytes
before ADTS scanning to inspect them directly for AAC-like structure).

**Applied to this repo**: both minor framing divergences from this section
are fixed in `src/bee_control/audio.py`: `AdtsReassembler` now discards the
first 3 successfully-parsed frames (`adts_warmup`, matching
`AudioMessageDecoder`'s undocumented behavior) and waits rather than
skip-and-retries on a `frame_length <= 7` corrupt-looking match.
`capture_audio.py` now defaults to the real-key diagnostic mode described
above (`--self-key` opts back into pushing a self-generated one), to isolate
whether self-generated keys actually propagate to audio encryption.

### RESOLVED: real audio decrypted end-to-end, with the real key

Ran the real-key diagnostic live. Result: **unambiguous success.** A 30-second
capture produced **64 ADTS frames**, every single one reporting exactly
`16000Hz` (versus the wildly inconsistent single giant "frame" per capture,
and a different sample rate between runs, seen with self-generated keys).
Frame sizes were realistic for low-bitrate 16kHz voice AAC (57-115 bytes).
The very first decrypted bytes of the capture were `fff160400d029801...`:
an immediately valid ADTS sync word and header at byte 0, no scanning
required to find it, which on its own is about as strong a correctness
signal as this kind of data can give.

**This conclusively settles both open questions from this section:**

1. The decrypt (`PacketDecryptor`/`decrypt_audio_packet`) and ADTS framing
   (`AudioMessageDecoder`/`AdtsReassembler`, including the two fixes above)
   implementations in this repo are **correct**, byte-exact, confirmed
   against real captured audio.
2. **Self-generated keys are accepted by the device's control-plane
   (`SetEncryptionKey` ACKs, subsequent commands behave normally, state
   transitions work) but do NOT propagate to the device's real-time audio
   encryption engine.** Only the genuinely captured key (from the one
   legitimate pairing event, `device_secrets.json`'s `aes_ctr_key_hex`)
   produces decryptable audio. The device's firmware evidently treats
   control-plane key acceptance and the actual audio-crypto key
   independently; consistent with the earlier finding that the 80-byte
   trailer doesn't cryptographically bind to the literal key bytes placed in
   the token; the real key used for audio likely comes from wherever the
   trailer's sealed material actually resolves to internally, not from
   whatever bytes the app places at token offset 12-28 (that offset is, at
   most, meaningful to the app's own bookkeeping, not necessarily what
   firmware uses for audio).

**Revised final verdict**: local-only operation is still fully achievable,
but the "arbitrary self-chosen key, forever, no further cloud/pairing
dependency" framing from the section above needs a caveat. Control (sleep,
power, brightness, mute/recording state) genuinely works with *any*
self-generated key. **Audio capture specifically requires reusing the one
real captured key**: which is already in hand, gitignored, in
`device_secrets.json`, so this still means **zero ongoing dependency on
Bee/Amazon's cloud or another real pairing** for either control or audio;
just not full freedom to rotate to a fresh self-chosen key for the audio
path specifically.

## Tooling notes for reproducing this analysis

- REA (`rea-agents` npm package) + headless JADX provider.
- Prerequisites REA does **not** bundle: a JDK 17+ (`openjdk@17` via Homebrew
  here) and `jadx-headless-mcp-0.7.1-all.jar` (downloaded from
  `https://github.com/1013503897/jadx-headless-mcp/releases/download/v0.7.1/jadx-headless-mcp-0.7.1-all.jar`,
  SHA-256 `6e5eacf500b64292bfb73c49797c1958f6ee44646e43e868039ae7feb573ff75`,
  matches REA's pinned value).
- Invoked via `rea` CLI (`npx -y rea-agents@6.5.0 inspect-android-package|inspect-android-class|inspect-android-method|search-android-classes <base.apk> ...`) rather than the MCP tool directly, because the running MCP server process didn't have `JAVA_HOME`/`REA_JADX_MCP_JAR` in its environment and restarting it was out of scope; the CLI picks up env vars set in the same shell invocation.
