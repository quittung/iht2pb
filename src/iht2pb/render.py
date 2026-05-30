from __future__ import annotations

from .protocol import (
    decode_activation_echo,
    decode_alarm_enabled,
    decode_alarm_target,
    decode_device_name,
    decode_display_state,
    decode_fahrenheit,
    decode_firmware_version,
    decode_hold_state,
    decode_probe_connection,
    decode_probe_snapshot,
    decode_temperature,
    valid_frame,
)

PALETTE = (31, 32, 33, 34, 35, 36, 91, 92, 93, 94, 95, 96)


def format_temperature(value: object) -> str:
    if isinstance(value, int | float):
        return f"{value:.1f} C"
    return str(value)


def describe_notification(data: bytes | bytearray) -> dict[str, object]:
    reading = decode_temperature(data)
    if reading is not None:
        return {"kind": "temperature", "probe": reading.probe, "celsius": reading.celsius}

    fahrenheit = decode_fahrenheit(data)
    if fahrenheit is not None:
        return {
            "kind": "fahrenheit",
            "probe": fahrenheit.probe,
            "fahrenheit": fahrenheit.fahrenheit,
        }

    snapshot = decode_probe_snapshot(data)
    if snapshot is not None:
        return {
            "kind": "probe_snapshot",
            "probe": snapshot.probe,
            "celsius": snapshot.celsius,
        }

    connection = decode_probe_connection(data)
    if connection is not None:
        return {
            "kind": "probe_connection",
            "probe2": connection.probe2,
            "probe3": connection.probe3,
        }

    hold_state = decode_hold_state(data)
    if hold_state is not None:
        return {"kind": "hold_state", "held": hold_state.held}

    display_state = decode_display_state(data)
    if display_state is not None:
        return {"kind": "display_state", "on": display_state.on}

    target = decode_alarm_target(data)
    if target is not None:
        return {"kind": "alarm_target", "probe": target.probe, "celsius": target.celsius}

    enabled = decode_alarm_enabled(data)
    if enabled is not None:
        return {"kind": "alarm_enabled", "probe": enabled.probe, "enabled": enabled.enabled}

    device_name = decode_device_name(data)
    if device_name is not None:
        return {"kind": "device_name", "name": device_name.name}

    firmware = decode_firmware_version(data)
    if firmware is not None:
        return {"kind": "firmware_version", "version": firmware.version}

    if decode_activation_echo(data) is not None:
        return {"kind": "activation_echo"}

    # Looks like a frame (has the header) but fails validation: truncated or
    # corrupt. Distinct from a well-formed frame with an unrecognized command.
    if len(data) >= 2 and data[0] == 0x55 and data[1] == 0xAA and not valid_frame(data):
        return {"kind": "broken_frame"}

    return {"kind": "unknown"}


def packet_summary(decoded: dict[str, object]) -> str:
    kind = decoded["kind"]
    if kind == "temperature":
        return f"temperature probe_{decoded['probe']} {format_temperature(decoded['celsius'])}"
    if kind == "fahrenheit":
        return f"fahrenheit probe_{decoded['probe']} {decoded['fahrenheit']:.1f} F"
    if kind == "probe_snapshot":
        return f"snapshot probe_{decoded['probe']} {format_temperature(decoded['celsius'])}"
    if kind == "probe_connection":
        present = [
            f"probe_{n}" for n, on in ((2, decoded["probe2"]), (3, decoded["probe3"])) if on
        ]
        return f"connection {', '.join(present) if present else 'probe 1 only'}"
    if kind == "hold_state":
        return f"hold {'on' if decoded['held'] else 'off'}"
    if kind == "display_state":
        return f"display {'on' if decoded['on'] else 'off'}"
    if kind == "alarm_target":
        return f"alarm_target probe_{decoded['probe']} {format_temperature(decoded['celsius'])}"
    if kind == "alarm_enabled":
        return f"alarm_enabled probe_{decoded['probe']} {'on' if decoded['enabled'] else 'off'}"
    if kind == "device_name":
        return f"device_name {decoded['name']}"
    if kind == "firmware_version":
        return f"firmware {decoded['version']}"
    if kind == "activation_echo":
        return "activation echo"
    if kind == "broken_frame":
        return "broken frame"
    return "unknown"


def colorize(text: str, value: int, enabled: bool) -> str:
    if not enabled:
        return text
    color = PALETTE[value % len(PALETTE)]
    return f"\033[{color}m{text}\033[0m"


def packet_hex(data: bytes, color: bool) -> str:
    return " ".join(colorize(f"{byte:02x}", byte, color) for byte in data)


def is_hex_byte(token: str) -> bool:
    return len(token) == 2 and all(c in "0123456789abcdefABCDEF" for c in token)


def recording_frame_bytes(line: str) -> tuple[str | None, bytes | None]:
    """Extract an optional leading timestamp and the run of hex bytes from a line.

    Anything before the hex (a timestamp, a `#0001` packet number) is skipped and
    anything after it (a human comment) is ignored, so both the raw and the
    annotated recording formats parse. Returns (timestamp, data); data is None
    when the line has no hex bytes.
    """
    tokens = line.split()
    timestamp = tokens[0] if tokens and not is_hex_byte(tokens[0]) else None
    hex_tokens: list[str] = []
    started = False
    for token in tokens:
        if is_hex_byte(token):
            hex_tokens.append(token)
            started = True
        elif started:
            break
    if not hex_tokens:
        return timestamp, None
    return timestamp, bytes.fromhex("".join(hex_tokens))
