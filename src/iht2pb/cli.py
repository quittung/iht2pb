from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError


DEFAULT_SCAN_TIMEOUT = 10.0
IHT2PB_NAME_FRAGMENT = "ink@iht-2pb"
IHT2PB_NOTIFY_UUID = "0000ffe4-0000-1000-8000-00805f9b34fb"
IHT2PB_WRITE_UUID = "0000ffe9-0000-1000-8000-00805f9b34fb"
IHT2PB_INIT_WRITES = (
    (IHT2PB_WRITE_UUID, b"\x55\xaa\x19\x01\x00\x19"),
    (IHT2PB_NOTIFY_UUID, b"\x55\xaa\x1a\x01\x00\x1a"),
)
IHT2PB_PROBE_SELECTORS = {0x02: 1, 0x04: 2, 0x06: 3}
IHT2PB_TEMP_BASE = 255
IHT2PB_NEG_HI = 254
IHT2PB_POS_MAX_HI = 11


@dataclass(frozen=True)
class FoundDevice:
    device: BLEDevice
    advertisement: AdvertisementData


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

    found.sort(key=lambda item: item.advertisement.rssi, reverse=True)
    return found


def _format_temperature(value: object) -> str:
    if isinstance(value, int | float):
        return f"{value:.1f} C"
    return str(value)


def _decode_temperature_packet(data: bytes | bytearray) -> tuple[str, float] | None:
    if len(data) < 6:
        return None

    probe_number = IHT2PB_PROBE_SELECTORS.get(data[2])
    if probe_number is None:
        return None

    high = data[4]
    low = data[5]
    if high >= IHT2PB_NEG_HI:
        temperature = (
            IHT2PB_TEMP_BASE * (high - IHT2PB_TEMP_BASE)
            + (low - IHT2PB_TEMP_BASE)
        ) / 10
    elif high <= IHT2PB_POS_MAX_HI:
        temperature = (IHT2PB_TEMP_BASE * high + low) / 10
    else:
        return None

    return f"temperature_probe_{probe_number}", temperature


def _print_scan_result(found: FoundDevice) -> None:
    name = found.advertisement.local_name or found.device.name or "(unknown name)"
    print(f"{found.device.address}  RSSI {found.advertisement.rssi:>4}  {name}")


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
    found = await _scan(args.scan_timeout, args.address)
    if not found:
        target = f" at {args.address}" if args.address else ""
        print(f"No IHT-2PB device found{target}.", file=sys.stderr)
        return 1

    selected = found[0]
    name = (
        selected.advertisement.local_name or selected.device.name or selected.device.address
    )
    print(f"Connecting to {name} ({selected.device.address})...", file=sys.stderr)

    stop_event = asyncio.Event()
    disconnected_event = asyncio.Event()
    latest: dict[str, object] = {}

    def on_notify(_sender: object, data: bytearray) -> None:
        decoded = _decode_temperature_packet(data)
        if decoded is None:
            return
        key, temperature = decoded

        latest[key] = temperature
        if args.json:
            print(json.dumps({"address": selected.device.address, "readings": latest}))
        else:
            rendered = ", ".join(
                f"{key.replace('temperature_', '')}: {_format_temperature(value)}"
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
        selected.device,
        disconnected_callback=on_disconnect,
        timeout=args.connect_timeout,
    ) as client:
        await client.start_notify(IHT2PB_NOTIFY_UUID, on_notify)
        for char_uuid, payload in IHT2PB_INIT_WRITES:
            try:
                await client.write_gatt_char(char_uuid, payload, response=False)
            except BleakError:
                pass

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
