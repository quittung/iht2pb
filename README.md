# IHT-2PB Python interface

Small command-line wrapper for reading an Inkbird IHT-2PB Bluetooth cooking
thermometer from Python.

BLE protocol support already exists in
[`inkbird-ble`](https://pypi.org/project/inkbird-ble/). Its current
documentation lists `IHT-2PB` as a GATT notification device named like
`Ink@IHT-2PB#...`, exposing three probe temperatures. It also handles the two
activation writes that the thermometer needs before it starts streaming.

This CLI currently talks to the thermometer directly with
[`bleak`](https://github.com/hbldh/bleak), using the same protocol details,
because the `inkbird-ble` 1.5.1 notify path hit a local dependency API mismatch
during testing.

## Setup

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

On Linux, make sure Bluetooth is powered on:

```sh
bluetoothctl power on
```

## Use

Turn on the thermometer and keep it nearby.

Scan for the device:

```sh
.venv/bin/iht2pb scan
```

Watch temperatures:

```sh
.venv/bin/iht2pb watch
```

If several devices are visible, pass the address printed by `scan`:

```sh
.venv/bin/iht2pb watch --address AA:BB:CC:DD:EE:FF
```

JSON output for scripts:

```sh
.venv/bin/iht2pb watch --json
```

Exit after the first notification:

```sh
.venv/bin/iht2pb watch --once
```

## Notes

The IHT-2PB does not broadcast temperature readings in advertisements. It must
be connected over GATT notifications, so the phone app and this script should
not be connected to the thermometer at the same time.
