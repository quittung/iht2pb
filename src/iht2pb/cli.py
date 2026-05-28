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

from .protocol import (
    INIT_WRITES,
    NOTIFY_UUID,
    WRITE_UUID,
    AlarmTarget,
    ProbeReading,
    decode_alarm_target,
    decode_temperature,
    encode_alarm_target,
)

DEFAULT_SCAN_TIMEOUT = 10.0
IHT2PB_NAME_FRAGMENT = "ink@iht-2pb"


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
        reading = decode_temperature(data)
        if reading is None:
            return
        key = f"probe_{reading.probe}"

        latest[key] = reading.celsius
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


async def cmd_targets(args: argparse.Namespace) -> int:
    selected = await _find_one(args)
    if selected is None:
        return 1

    targets: dict[int, AlarmTarget] = {}
    stop_event = asyncio.Event()

    def on_notify(_sender: object, data: bytearray) -> None:
        target = decode_alarm_target(data)
        if target is None:
            return
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
        target = decode_alarm_target(data)
        if target is None:
            return
        targets[target.probe] = target
        if len(targets) == 3:
            initial_targets_event.set()
        if write_sent and target.probe == args.probe and abs(target.celsius - args.celsius) < 0.05:
            write_confirmed_event.set()

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
