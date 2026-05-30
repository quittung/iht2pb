from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from datetime import datetime
from collections.abc import Sequence
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

from .protocol import (
    INIT_WRITES,
    NOTIFY_UUID,
    WRITE_UUID,
    AlarmEnabled,
    AlarmTarget,
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
    encode_alarm_enabled,
    encode_alarm_target,
    split_frames,
    valid_frame,
)

DEFAULT_SCAN_TIMEOUT = 10.0
IHT2PB_NAME_FRAGMENT = "ink@iht-2pb"
PALETTE = (31, 32, 33, 34, 35, 36, 91, 92, 93, 94, 95, 96)


@dataclass(frozen=True)
class FoundDevice:
    device: BLEDevice
    advertisement: AdvertisementData | None


def _is_iht2pb(
    advertisement: AdvertisementData, device: BLEDevice, address: str | None
) -> bool:
    if address:
        return device.address.lower() == address.lower()

    name = (advertisement.local_name or device.name or "").lower()
    return IHT2PB_NAME_FRAGMENT in name


async def _scan(timeout: float, address: str | None = None) -> list[FoundDevice]:
    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    found: list[FoundDevice] = []

    for device, advertisement in discovered.values():
        if _is_iht2pb(advertisement, device, address):
            found.append(FoundDevice(device, advertisement))

    found.sort(
        key=lambda item: item.advertisement.rssi if item.advertisement else -999,
        reverse=True,
    )
    return found


def _format_temperature(value: object) -> str:
    if isinstance(value, int | float):
        return f"{value:.1f} C"
    return str(value)


def _describe_notification(data: bytes | bytearray) -> dict[str, object]:
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

    # Looks like a frame (has the header) but fails validation — truncated or
    # corrupt. Distinct from a well-formed frame with an unrecognized command.
    if len(data) >= 2 and data[0] == 0x55 and data[1] == 0xAA and not valid_frame(data):
        return {"kind": "broken_frame"}

    return {"kind": "unknown"}


def _packet_summary(decoded: dict[str, object]) -> str:
    kind = decoded["kind"]
    if kind == "temperature":
        return f"temperature probe_{decoded['probe']} {_format_temperature(decoded['celsius'])}"
    if kind == "fahrenheit":
        return f"fahrenheit probe_{decoded['probe']} {decoded['fahrenheit']:.1f} F"
    if kind == "probe_snapshot":
        return f"snapshot probe_{decoded['probe']} {_format_temperature(decoded['celsius'])}"
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
        return f"alarm_target probe_{decoded['probe']} {_format_temperature(decoded['celsius'])}"
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


def _colorize(text: str, value: int, enabled: bool) -> str:
    if not enabled:
        return text
    color = PALETTE[value % len(PALETTE)]
    return f"\033[{color}m{text}\033[0m"


def _packet_hex(data: bytes, color: bool) -> str:
    return " ".join(_colorize(f"{byte:02x}", byte, color) for byte in data)


def _print_scan_result(found: FoundDevice) -> None:
    name = (
        found.advertisement.local_name if found.advertisement else None
    ) or found.device.name or "(unknown name)"
    rssi = f"{found.advertisement.rssi:>4}" if found.advertisement else " n/a"
    print(f"{found.device.address}  RSSI {rssi}  {name}")


async def _find_one(args: argparse.Namespace) -> FoundDevice | None:
    timeout = getattr(args, "scan_timeout", getattr(args, "timeout", DEFAULT_SCAN_TIMEOUT))
    found = await _scan(timeout, args.address)
    if not found:
        if args.address:
            return FoundDevice(BLEDevice(args.address, None, {}), None)
        target = f" at {args.address}" if args.address else ""
        print(f"No IHT-2PB device found{target}.", file=sys.stderr)
        return None
    return found[0]


async def _activate(client: BleakClient) -> None:
    for char_uuid, payload in INIT_WRITES:
        try:
            await client.write_gatt_char(char_uuid, payload, response=False)
        except BleakError:
            pass


def _print_targets(address: str, targets: dict[int, AlarmTarget], as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "address": address,
                    "alarm_targets": {
                        str(probe): target.celsius
                        for probe, target in sorted(targets.items())
                    },
                }
            )
        )
        return

    for probe, target in sorted(targets.items()):
        print(f"probe_{probe}: {target.celsius:.1f} C")


def _print_alarm_enabled(
    address: str, enabled: dict[int, AlarmEnabled], as_json: bool
) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "address": address,
                    "alarm_enabled": {
                        str(probe): state.enabled
                        for probe, state in sorted(enabled.items())
                    },
                }
            )
        )
        return

    for probe, state in sorted(enabled.items()):
        print(f"probe_{probe}: {'on' if state.enabled else 'off'}")


def _bleak_target(found: FoundDevice) -> BLEDevice | str:
    if found.advertisement is None:
        return found.device.address
    return found.device


async def cmd_scan(args: argparse.Namespace) -> int:
    found = await _scan(args.timeout, args.address)
    if not found:
        target = f" at {args.address}" if args.address else ""
        print(f"No IHT-2PB device found{target}.", file=sys.stderr)
        return 1

    for item in found:
        _print_scan_result(item)
    return 0


async def cmd_watch(args: argparse.Namespace) -> int:
    selected = await _find_one(args)
    if selected is None:
        return 1

    name = (
        (selected.advertisement.local_name if selected.advertisement else None)
        or selected.device.name
        or selected.device.address
    )
    print(f"Connecting to {name} ({selected.device.address})...", file=sys.stderr)

    stop_event = asyncio.Event()
    disconnected_event = asyncio.Event()
    latest: dict[str, object] = {}

    def on_notify(_sender: object, data: bytearray) -> None:
        updated = False
        for frame in split_frames(data):
            reading = decode_temperature(frame)
            if reading is None:
                continue
            latest[f"probe_{reading.probe}"] = reading.celsius
            updated = True
        if not updated:
            return

        if args.json:
            print(json.dumps({"address": selected.device.address, "readings": latest}))
        else:
            rendered = ", ".join(
                f"{key}: {_format_temperature(value)}"
                for key, value in sorted(latest.items())
            )
            print(rendered)

        if args.once:
            stop_event.set()

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:
            pass

    def on_disconnect(_client: BleakClient) -> None:
        disconnected_event.set()

    async with BleakClient(
        _bleak_target(selected),
        disconnected_callback=on_disconnect,
        timeout=args.connect_timeout,
    ) as client:
        await client.start_notify(NOTIFY_UUID, on_notify)
        await _activate(client)

        wait_tasks = [
            asyncio.create_task(stop_event.wait()),
            asyncio.create_task(disconnected_event.wait()),
        ]
        if args.duration is not None:
            wait_tasks.append(asyncio.create_task(asyncio.sleep(args.duration)))

        done, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            task.result()

    return 0


async def cmd_raw_watch(args: argparse.Namespace) -> int:
    selected = await _find_one(args)
    if selected is None:
        return 1

    name = (
        (selected.advertisement.local_name if selected.advertisement else None)
        or selected.device.name
        or selected.device.address
    )
    print(f"Connecting to {name} ({selected.device.address})...", file=sys.stderr)
    print("Every notification will be printed.", file=sys.stderr)

    stop_event = asyncio.Event()
    disconnected_event = asyncio.Event()
    packet_count = 0

    def on_notify(sender: object, data: bytearray) -> None:
        nonlocal packet_count
        timestamp = datetime.now().isoformat(timespec="milliseconds")
        payload = bytes(data)
        # A notification may bundle several frames; print one line per frame.
        # Fall back to the raw value if nothing parses, so it stays visible.
        for frame in split_frames(payload) or [payload]:
            packet_count += 1
            decoded = _describe_notification(frame)
            if args.json:
                print(
                    json.dumps(
                        {
                            "timestamp": timestamp,
                            "packet": packet_count,
                            "sender": str(sender),
                            "hex": frame.hex(" "),
                            "decoded": decoded,
                        }
                    ),
                    flush=True,
                )
            else:
                print(
                    f"{timestamp} #{packet_count:04d} "
                    f"{_packet_hex(frame, args.color)}  {_packet_summary(decoded)}",
                    flush=True,
                )

        if args.once:
            stop_event.set()

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:
            pass

    def on_disconnect(_client: BleakClient) -> None:
        disconnected_event.set()

    async with BleakClient(
        _bleak_target(selected),
        disconnected_callback=on_disconnect,
        timeout=args.connect_timeout,
    ) as client:
        await client.start_notify(NOTIFY_UUID, on_notify)
        await _activate(client)

        wait_tasks = [
            asyncio.create_task(stop_event.wait()),
            asyncio.create_task(disconnected_event.wait()),
        ]
        if args.duration is not None:
            wait_tasks.append(asyncio.create_task(asyncio.sleep(args.duration)))

        done, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            task.result()

    return 0


async def cmd_targets(args: argparse.Namespace) -> int:
    selected = await _find_one(args)
    if selected is None:
        return 1

    targets: dict[int, AlarmTarget] = {}
    stop_event = asyncio.Event()

    def on_notify(_sender: object, data: bytearray) -> None:
        for frame in split_frames(data):
            target = decode_alarm_target(frame)
            if target is not None:
                targets[target.probe] = target
        if len(targets) == 3:
            stop_event.set()

    async with BleakClient(_bleak_target(selected), timeout=args.connect_timeout) as client:
        await client.start_notify(NOTIFY_UUID, on_notify)
        await _activate(client)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=args.duration)
        except TimeoutError:
            pass

    if not targets:
        print("No alarm targets received.", file=sys.stderr)
        return 1

    _print_targets(selected.device.address, targets, args.json)
    return 0


async def cmd_alarm_enabled(args: argparse.Namespace) -> int:
    selected = await _find_one(args)
    if selected is None:
        return 1

    enabled: dict[int, AlarmEnabled] = {}
    stop_event = asyncio.Event()

    def on_notify(_sender: object, data: bytearray) -> None:
        for frame in split_frames(data):
            state = decode_alarm_enabled(frame)
            if state is not None:
                enabled[state.probe] = state
        if len(enabled) == 3:
            stop_event.set()

    async with BleakClient(_bleak_target(selected), timeout=args.connect_timeout) as client:
        await client.start_notify(NOTIFY_UUID, on_notify)
        await _activate(client)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=args.duration)
        except TimeoutError:
            pass

    if not enabled:
        print("No alarm enabled states received.", file=sys.stderr)
        return 1

    _print_alarm_enabled(selected.device.address, enabled, args.json)
    return 0


async def cmd_set_target(args: argparse.Namespace) -> int:
    selected = await _find_one(args)
    if selected is None:
        return 1

    payload = encode_alarm_target(args.probe, args.celsius)
    targets: dict[int, AlarmTarget] = {}
    initial_targets_event = asyncio.Event()
    write_confirmed_event = asyncio.Event()
    write_sent = False

    def on_notify(_sender: object, data: bytearray) -> None:
        nonlocal write_sent
        for frame in split_frames(data):
            target = decode_alarm_target(frame)
            if target is None:
                continue
            targets[target.probe] = target
            if (
                write_sent
                and target.probe == args.probe
                and abs(target.celsius - args.celsius) < 0.05
            ):
                write_confirmed_event.set()
        if len(targets) == 3:
            initial_targets_event.set()

    async with BleakClient(_bleak_target(selected), timeout=args.connect_timeout) as client:
        await client.start_notify(NOTIFY_UUID, on_notify)
        await _activate(client)
        try:
            await asyncio.wait_for(initial_targets_event.wait(), timeout=2.0)
        except TimeoutError:
            pass
        write_sent = True
        try:
            await client.write_gatt_char(WRITE_UUID, payload, response=True)
        except BleakError:
            await client.write_gatt_char(WRITE_UUID, payload, response=False)
        try:
            await asyncio.wait_for(write_confirmed_event.wait(), timeout=args.duration)
        except TimeoutError:
            targets[args.probe] = AlarmTarget(args.probe, args.celsius)

    if args.json:
        _print_targets(selected.device.address, {args.probe: targets[args.probe]}, True)
    else:
        print(f"probe_{args.probe}: {targets[args.probe].celsius:.1f} C")
    return 0


async def cmd_set_alarm_enabled(args: argparse.Namespace) -> int:
    selected = await _find_one(args)
    if selected is None:
        return 1

    desired = args.state == "on"
    payload = encode_alarm_enabled(args.probe, desired)
    enabled: dict[int, AlarmEnabled] = {}
    confirmed_event = asyncio.Event()
    write_sent = False

    def on_notify(_sender: object, data: bytearray) -> None:
        nonlocal write_sent
        for frame in split_frames(data):
            state = decode_alarm_enabled(frame)
            if state is None:
                continue
            enabled[state.probe] = state
            if write_sent and state.probe == args.probe and state.enabled == desired:
                confirmed_event.set()

    async with BleakClient(_bleak_target(selected), timeout=args.connect_timeout) as client:
        await client.start_notify(NOTIFY_UUID, on_notify)
        await _activate(client)
        await asyncio.sleep(0.5)
        write_sent = True
        try:
            await client.write_gatt_char(WRITE_UUID, payload, response=True)
        except BleakError:
            await client.write_gatt_char(WRITE_UUID, payload, response=False)
        try:
            await asyncio.wait_for(confirmed_event.wait(), timeout=args.duration)
        except TimeoutError:
            enabled[args.probe] = AlarmEnabled(args.probe, desired)

    if args.json:
        _print_alarm_enabled(selected.device.address, {args.probe: enabled[args.probe]}, True)
    else:
        print(f"probe_{args.probe}: {'on' if enabled[args.probe].enabled else 'off'}")
    return 0


def _is_hex_byte(token: str) -> bool:
    return len(token) == 2 and all(c in "0123456789abcdefABCDEF" for c in token)


def _recording_frame_bytes(line: str) -> tuple[str | None, bytes | None]:
    """Extract an optional leading timestamp and the run of hex bytes from a line.

    Anything before the hex (a timestamp, a `#0001` packet number) is skipped and
    anything after it (a human comment) is ignored, so both the raw and the
    annotated recording formats parse. Returns (timestamp, data); data is None
    when the line has no hex bytes.
    """
    tokens = line.split()
    timestamp = tokens[0] if tokens and not _is_hex_byte(tokens[0]) else None
    hex_tokens: list[str] = []
    started = False
    for token in tokens:
        if _is_hex_byte(token):
            hex_tokens.append(token)
            started = True
        elif started:
            break
    if not hex_tokens:
        return timestamp, None
    return timestamp, bytes.fromhex("".join(hex_tokens))


async def cmd_decode(args: argparse.Namespace) -> int:
    sources = args.files or ["-"]
    sink = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    try:
        if args.out and not args.json:
            recreate = "iht2pb decode " + " ".join(sources) + f" -o {args.out}"
            print(f"# generated by: {recreate}", file=sink)
        counter = 0
        for path in sources:
            stream = sys.stdin if path == "-" else open(path, encoding="utf-8")
            try:
                for raw_line in stream:
                    line = raw_line.rstrip("\n")
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        counter = 0  # section header: restart packet numbering
                        print(line, file=sink)
                        continue
                    if not stripped:
                        print(file=sink)
                        continue
                    timestamp, data = _recording_frame_bytes(line)
                    if data is None:
                        continue
                    for frame in split_frames(data) or [data]:
                        counter += 1
                        decoded = _describe_notification(frame)
                        if args.json:
                            print(
                                json.dumps(
                                    {
                                        "timestamp": timestamp,
                                        "packet": counter,
                                        "hex": frame.hex(" "),
                                        "decoded": decoded,
                                    }
                                ),
                                file=sink,
                            )
                        else:
                            prefix = f"{timestamp} " if timestamp else ""
                            print(
                                f"{prefix}#{counter:04d} "
                                f"{_packet_hex(frame, args.color)}  {_packet_summary(decoded)}",
                                file=sink,
                            )
            finally:
                if path != "-":
                    stream.close()
    finally:
        if args.out:
            sink.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iht2pb",
        description="Read an Inkbird IHT-2PB Bluetooth thermometer.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="find nearby IHT-2PB thermometers")
    scan.add_argument("--timeout", type=float, default=DEFAULT_SCAN_TIMEOUT)
    scan.add_argument("--address", help="only match this Bluetooth address")
    scan.set_defaults(func=cmd_scan)

    watch = subparsers.add_parser("watch", help="print probe temperature updates")
    watch.add_argument("--address", help="Bluetooth address from the scan command")
    watch.add_argument("--scan-timeout", type=float, default=DEFAULT_SCAN_TIMEOUT)
    watch.add_argument("--connect-timeout", type=float, default=30.0)
    watch.add_argument("--duration", type=float, help="seconds to run before exiting")
    watch.add_argument("--once", action="store_true", help="exit after one update")
    watch.add_argument("--json", action="store_true", help="print JSON lines")
    watch.set_defaults(func=cmd_watch)

    raw_watch = subparsers.add_parser(
        "raw-watch", help="print every raw GATT notification packet"
    )
    raw_watch.add_argument("--address", help="Bluetooth address from the scan command")
    raw_watch.add_argument("--scan-timeout", type=float, default=DEFAULT_SCAN_TIMEOUT)
    raw_watch.add_argument("--connect-timeout", type=float, default=30.0)
    raw_watch.add_argument("--duration", type=float, help="seconds to run before exiting")
    raw_watch.add_argument("--once", action="store_true", help="exit after one notification")
    raw_watch.add_argument("--json", action="store_true", help="print JSON lines")
    raw_watch.add_argument(
        "--color",
        action="store_true",
        help="colorize raw packet bytes by byte value",
    )
    raw_watch.set_defaults(func=cmd_raw_watch)

    targets = subparsers.add_parser("targets", help="read configured alarm targets")
    targets.add_argument("--address", help="Bluetooth address from the scan command")
    targets.add_argument("--scan-timeout", type=float, default=DEFAULT_SCAN_TIMEOUT)
    targets.add_argument("--connect-timeout", type=float, default=30.0)
    targets.add_argument("--duration", type=float, default=5.0)
    targets.add_argument("--json", action="store_true", help="print JSON")
    targets.set_defaults(func=cmd_targets)

    set_target = subparsers.add_parser("set-target", help="set a probe alarm target")
    set_target.add_argument("probe", type=int, choices=(1, 2, 3))
    set_target.add_argument("celsius", type=float)
    set_target.add_argument("--address", help="Bluetooth address from the scan command")
    set_target.add_argument("--scan-timeout", type=float, default=DEFAULT_SCAN_TIMEOUT)
    set_target.add_argument("--connect-timeout", type=float, default=30.0)
    set_target.add_argument("--duration", type=float, default=5.0)
    set_target.add_argument("--json", action="store_true", help="print JSON")
    set_target.set_defaults(func=cmd_set_target)

    alarm_enabled = subparsers.add_parser(
        "alarm-enabled", help="read hardware alarm on/off states"
    )
    alarm_enabled.add_argument("--address", help="Bluetooth address from the scan command")
    alarm_enabled.add_argument("--scan-timeout", type=float, default=DEFAULT_SCAN_TIMEOUT)
    alarm_enabled.add_argument("--connect-timeout", type=float, default=30.0)
    alarm_enabled.add_argument("--duration", type=float, default=5.0)
    alarm_enabled.add_argument("--json", action="store_true", help="print JSON")
    alarm_enabled.set_defaults(func=cmd_alarm_enabled)

    set_alarm_enabled = subparsers.add_parser(
        "set-alarm-enabled", help="turn a probe hardware alarm on or off"
    )
    set_alarm_enabled.add_argument("probe", type=int, choices=(1, 2, 3))
    set_alarm_enabled.add_argument("state", choices=("on", "off"))
    set_alarm_enabled.add_argument("--address", help="Bluetooth address from the scan command")
    set_alarm_enabled.add_argument("--scan-timeout", type=float, default=DEFAULT_SCAN_TIMEOUT)
    set_alarm_enabled.add_argument("--connect-timeout", type=float, default=30.0)
    set_alarm_enabled.add_argument("--duration", type=float, default=5.0)
    set_alarm_enabled.add_argument("--json", action="store_true", help="print JSON")
    set_alarm_enabled.set_defaults(func=cmd_set_alarm_enabled)

    decode = subparsers.add_parser(
        "decode",
        help="decode a saved recording (timestamp + raw bytes) to annotated form",
    )
    decode.add_argument(
        "files",
        nargs="*",
        help="recording files to decode; omit or pass '-' to read stdin",
    )
    decode.add_argument(
        "-o",
        "--out",
        help="write to this file (prefixed with a recreate-command header) instead of stdout",
    )
    decode.add_argument("--json", action="store_true", help="print JSON lines")
    decode.add_argument(
        "--color", action="store_true", help="colorize raw packet bytes by byte value"
    )
    decode.set_defaults(func=cmd_decode)

    return parser


async def async_main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return await args.func(args)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
