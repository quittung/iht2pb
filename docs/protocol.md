# Inkbird IHT-2PB BLE protocol notes

These notes describe the BLE protocol of the Inkbird IHT-2PB Bluetooth cooking
thermometer. The device advertises a name like `Ink@IHT-2PB#...`, but no
temperature data is present in the advertisements: a client must connect over
GATT and subscribe to notifications.

The device has three probe channels. Probe 1 is the built-in fold-out probe;
probes 2 and 3 are external sockets. Which external sockets are occupied is
reported by the [probe-connection bitmask](#probe-connection); an empty socket
streams nothing and only shows a stale garbage value in the snapshot.

Everything here is from direct captures of one unit (firmware `VER1.2.0`). Bytes
are written in hex.

## GATT

| Purpose | UUID |
| --- | --- |
| Service | `0000ffe0-0000-1000-8000-00805f9b34fb` |
| Notify characteristic | `0000ffe4-0000-1000-8000-00805f9b34fb` |
| Write characteristic | `0000ffe9-0000-1000-8000-00805f9b34fb` |

## Activation

After subscribing to notifications on `ffe4`, send these two writes. The device
does not start streaming until it receives them.

| Write to | Payload |
| --- | --- |
| `ffe9` | `55 aa 19 01 00 19` |
| `ffe4` | `55 aa 1a 01 00 1a` |

Note that the second write targets `ffe4`, the *notify* characteristic, not the
write characteristic. Both are sent write-without-response.

The device echoes the first activation back as a notification (`55 aa 19 01 00
19`) at the start of the response burst.

## Frame format

Every packet uses this frame:

```text
55 aa <command> <length> <payload...> <checksum>
```

| Field | Size | Meaning |
| --- | --- | --- |
| header | 2 | always `55 aa` |
| command | 1 | message type (see [Command reference](#command-reference)) |
| length | 1 | number of payload bytes (excludes header, command, length, checksum) |
| payload | `length` | command-specific |
| checksum | 1 | low byte of the sum of every preceding byte |

The checksum covers all bytes from the `55 aa` header up to but not including
the checksum itself:

```text
checksum = sum(packet_without_checksum) & 0xff
```

For example, in `55 aa 02 02 00 fd 00`:

```text
0x55 + 0xaa + 0x02 + 0x02 + 0x00 + 0xfd = 0x200  ->  checksum 0x00
```

A single notification usually carries one frame, but not always: the device may
pack several frames into one notification value, especially during a busy burst
(e.g. a three-probe startup). Parse a notification as a *stream* of frames —
read a frame using its `length` byte (total frame size is `length + 5`), then
continue from the next byte — rather than assuming one frame per notification.
Each packed frame is an ordinary, individually valid frame; none are special.

## Temperature encoding

Temperatures are a 16-bit big-endian value in tenths of a degree:

```text
value = ((high << 8) | low) / 10
```

The same encoding is used by live readings, the per-probe snapshot, and alarm
targets. The unit (C or F) depends on the command, not the encoding.

Examples:

| Bytes (`high low`) | Value |
| --- | --- |
| `00 fd` | 25.3 |
| `01 3d` | 31.7 |
| `03 07` | 77.5 |
| `ff f0` | -1.6 |

Sub-zero readings are ordinary two's-complement: a `high` byte of `0xff` (and,
for colder values, below) means the 16-bit value is negative.

```text
raw = (high << 8) | low
value = (raw - 0x10000) / 10   if raw >= 0x8000
```

## Command reference

| Command | Length | Meaning |
| --- | --- | --- |
| `0x01` | 1 | hold flag |
| `0x02` | 2 | probe 1 live temperature, Celsius |
| `0x03` | 2 | probe 1 live temperature, Fahrenheit |
| `0x04` | 2 | probe 2 live temperature, Celsius |
| `0x05` | 2 | probe 2 live temperature, Fahrenheit |
| `0x06` | 2 | probe 3 live temperature, Celsius |
| `0x07` | 2 | probe 3 live temperature, Fahrenheit |
| `0x08` | 2 | probe 1 snapshot temperature |
| `0x09` | 2 | probe 2 snapshot temperature |
| `0x0a` | 2 | probe 3 snapshot temperature |
| `0x0b` | 1 | probe-connection bitmask |
| `0x0c` | 1 | display / activity state |
| `0x0d` | 4 | probe 1 alarm target |
| `0x0e` | 4 | probe 2 alarm target |
| `0x0f` | 4 | probe 3 alarm target |
| `0x12` | 1 | probe 1 alarm enable |
| `0x13` | 1 | probe 2 alarm enable |
| `0x14` | 1 | probe 3 alarm enable |
| `0x15` | 1 | startup-only, unknown |
| `0x16` | var | device name string |
| `0x18` | var | firmware version string |
| `0x19` | 1 | activation echo |

## Live probe readings

While running, the device streams the connected probe's temperature, normally as
a Celsius/Fahrenheit pair:

```text
55 aa 02 02 01 3d 41   probe 1  31.7 C
55 aa 03 02 03 7b 82   probe 1  89.1 F
```

Commands `0x02`–`0x07` are alternating Celsius and Fahrenheit for the three
probes:

| Probe | Celsius | Fahrenheit |
| --- | --- | --- |
| 1 | `0x02` | `0x03` |
| 2 | `0x04` | `0x05` |
| 3 | `0x06` | `0x07` |

A socket with no probe streams nothing at all (no live updates for that
command). Only an occupied probe produces a stream, so the presence of a live
stream — together with the [probe-connection bitmask](#probe-connection) — tells
you which probes are real.

## Probe snapshot

Commands `0x08`, `0x09`, `0x0a` are a one-shot snapshot of probes 1, 2, and 3.
They have only been observed in two situations: the startup burst, and the
rising edge of hold (when hold is engaged, not when it is released). They use
the standard temperature encoding.

```text
55 aa 08 02 01 0e 18   probe 1  27.0 C
55 aa 09 02 02 4c 58   probe 2  58.8 C  (empty socket: garbage)
55 aa 0a 02 00 c9 d4   probe 3  20.1 C  (empty socket: garbage)
```

Each snapshot command maps to a fixed probe (`0x08`/`0x09`/`0x0a` = probe
1/2/3), regardless of how many probes are connected. For an occupied probe the
snapshot matches its live reading once settled. For an empty socket it carries a
stale garbage value (the `58.8`/`20.1` above are an empty probe 2 and 3), so
cross-check against the [probe-connection bitmask](#probe-connection) before
trusting a snapshot value.

The snapshot reflects stored state, not necessarily the live value: in the
startup burst it can lag the live stream by a fraction of a degree, and it is
sometimes seeded from the *previous* session's readings.

## Probe connection

Command `0x0b` reports which external sockets are occupied, as a bitmask in its
single payload byte:

| Bit | Probe |
| --- | --- |
| `0x40` | probe 2 present |
| `0x80` | probe 3 present |

Probe 1 is the built-in probe and is always present, so it is not represented.

| Packet | Probes 2/3 |
| --- | --- |
| `55 aa 0b 01 00 0b` | neither |
| `55 aa 0b 01 40 4b` | probe 2 only |
| `55 aa 0b 01 80 8b` | probe 3 only |
| `55 aa 0b 01 c0 cb` | both |

It is emitted during the startup burst (reflecting the state at connect time)
and again on every plug/unplug event, immediately before that probe's live
stream starts or stops.

This is the authoritative "is the socket occupied?" signal. An empty socket
sends no live stream and only appears as stale garbage in the
[snapshot](#probe-snapshot), so use `0x0b` to decide whether a probe-2/3 value
is real.

## Display / activity state

Command `0x0c` reports the display state, where the payload is a flag byte with
bit `0x10` set while the screen is lit.

| Packet | Meaning |
| --- | --- |
| `55 aa 0c 01 90 9c` | display on / woken |
| `55 aa 0c 01 80 8c` | display off |

`0x80` is the screen turning off on its idle timeout. `0x90` is the screen being
turned on or kept awake by *some* event: a significant temperature change, a
hardware button press, and so on, usually accompanied by a beep.

So `0x90` is useful as a generic "something happened" signal, but it does **not**
specifically mean the user pressed a button, since environmental changes trigger
it too.

## Hold state

The hold button emits command `0x01`. The payload is a flag byte; bit `0x04` is
the hold state (set = on).

| Packet | Meaning |
| --- | --- |
| `55 aa 01 01 fd fe` | hold on |
| `55 aa 01 01 f9 fa` | hold off |

Other bits in the byte carry additional state and are not fully decoded — for
example bit `0x01` tracks whether the device is running on probe 1 alone (the
byte reads `fc`/`f8` instead of `fd`/`f9` once an external probe is connected).
Test bit `0x04` specifically for hold rather than matching the whole byte.

Both directions are caused by a button press, so this is the only message that
can be tied directly to a deliberate user action rather than an environmental
change.

## Alarm targets

Commands `0x0d`, `0x0e`, `0x0f` carry the per-probe alarm target. They are
emitted during the startup burst, and the same packet shape can be written to
`ffe9` to set a target.

```text
55 aa <0d|0e|0f> 04 <target_hi> <target_lo> ff ff <checksum>
```

`target` is the standard temperature encoding (Celsius, tenths). The trailing
`ff ff` was constant in all captured packets; its meaning is unknown.

| Packet | Probe | Target |
| --- | --- | --- |
| `55 aa 0d 04 01 4a ff ff 59` | 1 | 33.0 C |
| `55 aa 0e 04 08 3e ff ff 55` | 2 | 211.0 C |
| `55 aa 0f 04 07 12 ff ff 29` | 3 | 181.0 C |

To set a 200.0 C target for probe 1:

```text
target = 2000 = 0x07d0
55 aa 0d 04 07 d0 ff ff e5
```

## Alarm enable

Commands `0x12`, `0x13`, `0x14` carry the per-probe hardware alarm on/off state.
Emitted during the startup burst; the same shape can be written to `ffe9`.

```text
55 aa <12|13|14> 01 <00|01> <checksum>
```

| Packet | Probe | State |
| --- | --- | --- |
| `55 aa 12 01 01 13` | 1 | enabled |
| `55 aa 13 01 00 13` | 2 | disabled |
| `55 aa 14 01 00 14` | 3 | disabled |

To enable the probe 1 alarm: `55 aa 12 01 01 13`. To disable it:
`55 aa 12 01 00 12`.

## Identity strings

Commands `0x16` and `0x18` carry ASCII strings; `length` is the string length.

| Packet | String |
| --- | --- |
| `55 aa 16 0f 49 6e 6b 40 49 48 54 2d 32 50 42 23 36 31 32 18` | `Ink@IHT-2PB#612` |
| `55 aa 18 08 56 45 52 31 2e 32 2e 30 fb` | `VER1.2.0` |

## Unknown startup packets

These appear in the startup burst and are not yet decoded:

| Packet | Notes |
| --- | --- |
| `55 aa 15 01 0a 1f` | single payload byte, `0x0a` observed |

## Startup burst

After activation the device replays its current state before settling into live
readings. The burst below is representative, not guaranteed: ordering can vary
slightly, live temperature updates may interleave, and this may not be the
minimal set.

```text
55 aa 19 01 00 19            activation echo
55 aa 01 01 f9 fa            hold off
55 aa 02 02 01 00 04         probe 1 live 25.6 C
55 aa 03 02 03 0d 14         probe 1 live 78.0 F
55 aa 08 02 00 fd 06         probe 1 snapshot 25.3 C  (stored/previous)
55 aa 09 02 02 4c 58         probe 2 snapshot 58.8 C  (empty socket)
55 aa 0a 02 00 c9 d4         probe 3 snapshot 20.1 C  (empty socket)
55 aa 0b 01 00 0b            probe connection: none (probe 1 only)
55 aa 0c 01 80 8c            display off
55 aa 0d 04 01 4a ff ff 59   probe 1 alarm target 33.0 C
55 aa 0e 04 08 3e ff ff 55   probe 2 alarm target 211.0 C
55 aa 0f 04 07 12 ff ff 29   probe 3 alarm target 181.0 C
55 aa 12 01 01 13            probe 1 alarm enabled
55 aa 13 01 00 13            probe 2 alarm disabled
55 aa 14 01 00 14            probe 3 alarm disabled
55 aa 15 01 0a 1f            unknown
55 aa 16 0f ...              device name
55 aa 18 08 ...              firmware version
```
