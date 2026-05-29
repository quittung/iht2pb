from __future__ import annotations

from dataclasses import dataclass

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ffe4-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ffe9-0000-1000-8000-00805f9b34fb"

INIT_WRITES = (
    (WRITE_UUID, b"\x55\xaa\x19\x01\x00\x19"),
    (NOTIFY_UUID, b"\x55\xaa\x1a\x01\x00\x1a"),
)

# Live per-probe streams: even command = Celsius, odd = Fahrenheit.
# Only probe 1 (0x02/0x03) has been captured; probes 2 and 3 are inferred.
TEMPERATURE_SELECTORS = {0x02: 1, 0x04: 2, 0x06: 3}
FAHRENHEIT_SELECTORS = {0x03: 1, 0x05: 2, 0x07: 3}
# One-shot snapshot of probes 1/2/3 (startup burst and rising hold edge).
SNAPSHOT_SELECTORS = {0x08: 1, 0x09: 2, 0x0A: 3}
TARGET_COMMANDS = {0x0D: 1, 0x0E: 2, 0x0F: 3}
TARGET_COMMAND_BY_PROBE = {probe: command for command, probe in TARGET_COMMANDS.items()}
ALARM_ENABLE_COMMANDS = {0x12: 1, 0x13: 2, 0x14: 3}
ALARM_ENABLE_COMMAND_BY_PROBE = {
    probe: command for command, probe in ALARM_ENABLE_COMMANDS.items()
}

HOLD_COMMAND = 0x01
DISPLAY_COMMAND = 0x0C
HOLD_FLAG_BIT = 0x04
DISPLAY_ON_BIT = 0x10
HEADER = b"\x55\xaa"


@dataclass(frozen=True)
class ProbeReading:
    probe: int
    celsius: float


@dataclass(frozen=True)
class AlarmTarget:
    probe: int
    celsius: float


@dataclass(frozen=True)
class AlarmEnabled:
    probe: int
    enabled: bool


@dataclass(frozen=True)
class ProbeSnapshot:
    probe: int
    celsius: float


@dataclass(frozen=True)
class FahrenheitReading:
    probe: int
    fahrenheit: float


@dataclass(frozen=True)
class HoldState:
    held: bool


@dataclass(frozen=True)
class DisplayState:
    on: bool


def checksum(payload: bytes | bytearray) -> int:
    return sum(payload) & 0xFF


def valid_frame(data: bytes | bytearray, payload_length: int | None = None) -> bool:
    if len(data) < 5:
        return False
    if bytes(data[:2]) != HEADER:
        return False
    if payload_length is not None and data[3] != payload_length:
        return False
    if len(data) != data[3] + 5:
        return False
    return checksum(data[:-1]) == data[-1]


def decode_temperature_value(data: bytes | bytearray) -> float | None:
    if not valid_frame(data, payload_length=2):
        return None

    raw = (data[4] << 8) | data[5]
    if raw >= 0x8000:  # two's-complement negative (inferred, never captured)
        raw -= 0x10000
    return raw / 10


def decode_temperature(data: bytes | bytearray) -> ProbeReading | None:
    if not valid_frame(data, payload_length=2):
        return None

    probe = TEMPERATURE_SELECTORS.get(data[2])
    if probe is None:
        return None

    celsius = decode_temperature_value(data)
    if celsius is None:
        return None

    return ProbeReading(probe=probe, celsius=celsius)


def decode_fahrenheit(data: bytes | bytearray) -> FahrenheitReading | None:
    if not valid_frame(data, payload_length=2):
        return None

    probe = FAHRENHEIT_SELECTORS.get(data[2])
    if probe is None:
        return None

    fahrenheit = decode_temperature_value(data)
    if fahrenheit is None:
        return None
    return FahrenheitReading(probe=probe, fahrenheit=fahrenheit)


def decode_probe_snapshot(data: bytes | bytearray) -> ProbeSnapshot | None:
    if not valid_frame(data, payload_length=2):
        return None

    probe = SNAPSHOT_SELECTORS.get(data[2])
    if probe is None:
        return None

    celsius = decode_temperature_value(data)
    if celsius is None:
        return None
    return ProbeSnapshot(probe=probe, celsius=celsius)


def decode_hold_state(data: bytes | bytearray) -> HoldState | None:
    if not valid_frame(data, payload_length=1) or data[2] != HOLD_COMMAND:
        return None
    return HoldState(held=bool(data[4] & HOLD_FLAG_BIT))


def decode_display_state(data: bytes | bytearray) -> DisplayState | None:
    if not valid_frame(data, payload_length=1) or data[2] != DISPLAY_COMMAND:
        return None
    return DisplayState(on=bool(data[4] & DISPLAY_ON_BIT))


def decode_alarm_target(data: bytes | bytearray) -> AlarmTarget | None:
    if not valid_frame(data, payload_length=4):
        return None

    probe = TARGET_COMMANDS.get(data[2])
    if probe is None:
        return None

    raw_target = (data[4] << 8) | data[5]
    return AlarmTarget(probe=probe, celsius=raw_target / 10)


def decode_alarm_enabled(data: bytes | bytearray) -> AlarmEnabled | None:
    if not valid_frame(data, payload_length=1):
        return None

    probe = ALARM_ENABLE_COMMANDS.get(data[2])
    if probe is None:
        return None

    return AlarmEnabled(probe=probe, enabled=data[4] != 0)


def encode_alarm_target(probe: int, celsius: float) -> bytes:
    command = TARGET_COMMAND_BY_PROBE.get(probe)
    if command is None:
        raise ValueError("probe must be 1, 2, or 3")
    if not -50 <= celsius <= 300:
        raise ValueError("alarm target must be between -50.0 C and 300.0 C")

    target = round(celsius * 10)
    if target < 0:
        # The observed app/device protocol likely supports signed values, but
        # target writes below 0 C are not useful for cooking and are unverified.
        raise ValueError("negative alarm targets are not implemented yet")

    payload = bytearray(
        [
            0x55,
            0xAA,
            command,
            0x04,
            (target >> 8) & 0xFF,
            target & 0xFF,
            0xFF,
            0xFF,
        ]
    )
    payload.append(checksum(payload))
    return bytes(payload)


def encode_alarm_enabled(probe: int, enabled: bool) -> bytes:
    command = ALARM_ENABLE_COMMAND_BY_PROBE.get(probe)
    if command is None:
        raise ValueError("probe must be 1, 2, or 3")

    payload = bytearray([0x55, 0xAA, command, 0x01, 0x01 if enabled else 0x00])
    payload.append(checksum(payload))
    return bytes(payload)
