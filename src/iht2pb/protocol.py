from __future__ import annotations

from dataclasses import dataclass

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ffe4-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ffe9-0000-1000-8000-00805f9b34fb"

INIT_WRITES = (
    (WRITE_UUID, b"\x55\xaa\x19\x01\x00\x19"),
    (NOTIFY_UUID, b"\x55\xaa\x1a\x01\x00\x1a"),
)

TEMPERATURE_SELECTORS = {0x02: 1, 0x04: 2, 0x06: 3}
TARGET_COMMANDS = {0x0D: 1, 0x0E: 2, 0x0F: 3}
TARGET_COMMAND_BY_PROBE = {probe: command for command, probe in TARGET_COMMANDS.items()}
ALARM_ENABLE_COMMANDS = {0x12: 1, 0x13: 2, 0x14: 3}
ALARM_ENABLE_COMMAND_BY_PROBE = {
    probe: command for command, probe in ALARM_ENABLE_COMMANDS.items()
}

TEMP_BASE = 255
NEGATIVE_TEMP_HIGH_BYTE = 254
POSITIVE_TEMP_MAX_HIGH_BYTE = 11


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


def checksum(payload: bytes | bytearray) -> int:
    return sum(payload) & 0xFF


def decode_temperature(data: bytes | bytearray) -> ProbeReading | None:
    if len(data) < 6:
        return None

    probe = TEMPERATURE_SELECTORS.get(data[2])
    if probe is None:
        return None

    high = data[4]
    low = data[5]
    if high >= NEGATIVE_TEMP_HIGH_BYTE:
        celsius = (TEMP_BASE * (high - TEMP_BASE) + (low - TEMP_BASE)) / 10
    elif high <= POSITIVE_TEMP_MAX_HIGH_BYTE:
        celsius = (TEMP_BASE * high + low) / 10
    else:
        return None

    return ProbeReading(probe=probe, celsius=celsius)


def decode_alarm_target(data: bytes | bytearray) -> AlarmTarget | None:
    if len(data) < 9:
        return None
    if data[0] != 0x55 or data[1] != 0xAA:
        return None
    if data[3] != 0x04:
        return None
    if checksum(data[:-1]) != data[-1]:
        return None

    probe = TARGET_COMMANDS.get(data[2])
    if probe is None:
        return None

    raw_target = (data[4] << 8) | data[5]
    return AlarmTarget(probe=probe, celsius=raw_target / 10)


def decode_alarm_enabled(data: bytes | bytearray) -> AlarmEnabled | None:
    if len(data) < 6:
        return None
    if data[0] != 0x55 or data[1] != 0xAA:
        return None
    if data[3] != 0x01:
        return None
    if checksum(data[:-1]) != data[-1]:
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
