from __future__ import annotations

from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

from .protocol import INIT_WRITES

DEFAULT_SCAN_TIMEOUT = 10.0
IHT2PB_NAME_FRAGMENT = "ink@iht-2pb"


@dataclass(frozen=True)
class FoundDevice:
    device: BLEDevice
    advertisement: AdvertisementData | None


def is_iht2pb(
    advertisement: AdvertisementData, device: BLEDevice, address: str | None
) -> bool:
    if address:
        return device.address.lower() == address.lower()

    name = (advertisement.local_name or device.name or "").lower()
    return IHT2PB_NAME_FRAGMENT in name


async def scan(timeout: float, address: str | None = None) -> list[FoundDevice]:
    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    found: list[FoundDevice] = []

    for device, advertisement in discovered.values():
        if is_iht2pb(advertisement, device, address):
            found.append(FoundDevice(device, advertisement))

    found.sort(
        key=lambda item: item.advertisement.rssi if item.advertisement else -999,
        reverse=True,
    )
    return found


async def activate(client: BleakClient) -> None:
    for char_uuid, payload in INIT_WRITES:
        try:
            await client.write_gatt_char(char_uuid, payload, response=False)
        except BleakError:
            pass


def bleak_target(found: FoundDevice) -> BLEDevice | str:
    if found.advertisement is None:
        return found.device.address
    return found.device


def display_name(found: FoundDevice) -> str:
    return (
        (found.advertisement.local_name if found.advertisement else None)
        or found.device.name
        or found.device.address
    )


def found_from_address(address: str) -> FoundDevice:
    return FoundDevice(BLEDevice(address, None, {}), None)
