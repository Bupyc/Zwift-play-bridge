"""Remap one Zwift Play controller to Zwift Ride controls on macOS."""

import argparse
import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import sys
import tomllib

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

import zwift_dircon as dircon


SIDES = {0x02: "right", 0x03: "left", 0x0E: "right", 0x0F: "left"}
LEGACY_BUTTONS = {
    "right": {"Y": 2, "Z": 3, "A": 4, "B": 5, "FRONT": 6, "SHIFT": 7},
    "left": {"UP": 2, "LEFT": 3, "RIGHT": 4, "DOWN": 5, "FRONT": 6, "SHIFT": 7},
}
KEYPAD_BUTTONS = {
    "right": {"A": 0x10, "B": 0x20, "Y": 0x40, "Z": 0x80, "SHIFT": 0x1000, "FRONT": 0x8000},
    "left": {"LEFT": 0x01, "UP": 0x02, "RIGHT": 0x04, "DOWN": 0x08, "SHIFT": 0x0100, "FRONT": 0x0800},
}
LS1, RS1 = 0x0100, 0x1000
DIGITAL = {
    "nav_left": 0x0001, "nav_up": 0x0002, "nav_right": 0x0004, "nav_down": 0x0008,
    "select": 0x0010, "back": 0x0020, "power_up": 0x0040, "ride_on": 0x0080,
    "shift_down": LS1, "shift_up": RS1, "shift_both": LS1 | RS1,
    "shift_down_2": 0x0200, "shift_up_2": 0x2000,
}
ANALOG = {"steer_left", "steer_right", "brake"}
ACTIONS = DIGITAL.keys() | ANALOG | {"ignore"}
DEFAULT_CONFIG = Path(__file__).with_name("zwift-play-bridge.toml")
RIDE_ON = b"RideOn"


@dataclass(frozen=True)
class Config:
    buttons: dict[str, str]
    left_buttons: dict[str, str]
    axis: str
    invert: bool
    deadzone: int
    full_scale: int
    paddle_left: str
    paddle_right: str


def load_config(path):
    with path.open("rb") as file:
        raw = tomllib.load(file)
    buttons, left_buttons = raw.get("buttons", {}), raw.get("left_buttons", {})
    for table, bindings, side in (("buttons", buttons, "right"), ("left_buttons", left_buttons, "left")):
        valid = LEGACY_BUTTONS[side]
        if not isinstance(bindings, dict) or bindings.keys() - valid.keys():
            raise ValueError(f"{table} must use only " + ", ".join(valid))
    paddle = raw.get("paddle", {})
    if not isinstance(paddle, dict):
        raise ValueError("Expected a [paddle] table")
    if raw.keys() - {"buttons", "left_buttons", "paddle"} or paddle.keys() - {
        "axis", "invert", "deadzone", "full_scale", "left", "right"
    }:
        raise ValueError("Unknown configuration key")
    axis = paddle.get("axis", "lr")
    invert = paddle.get("invert", False)
    deadzone = paddle.get("deadzone", 8)
    full_scale = paddle.get("full_scale", 100)
    left, right = paddle.get("left", "steer_left"), paddle.get("right", "steer_right")
    if axis not in ("lr", "ud") or type(invert) is not bool:
        raise ValueError("paddle.axis must be lr or ud and paddle.invert must be a boolean")
    if type(deadzone) is not int or type(full_scale) is not int or not 0 <= deadzone < full_scale:
        raise ValueError("paddle values must be integers with 0 <= deadzone < full_scale")
    for action in (*buttons.values(), *left_buttons.values(), left, right):
        if not isinstance(action, str) or action not in ACTIONS:
            raise ValueError(f"Unknown action {action!r}. Use --list-actions.")
    return Config(buttons, left_buttons, axis, invert, deadzone, full_scale, left, right)


def signed(value):
    return (value >> 1) ^ -(value & 1)


def decode_input(data, side):
    if data[:1] == b"\x07":
        fields = dircon.varint_fields(data[1:])
        pressed = {name for name, key in LEGACY_BUTTONS[side].items() if fields.get(key) == 0}
        return pressed, signed(fields.get(8, 0)), signed(fields.get(9, 0))
    if data[:1] != b"\x23":
        return None
    mask, paddles = 0, {}
    for field, value in dircon.protobuf_fields(data[1:]):
        if field == 1 and isinstance(value, int):
            mask = value ^ 0xFFFFFFFF
        elif field == 3 and isinstance(value, bytes):
            entry = dircon.varint_fields(value)
            paddles[entry.get(1, 0)] = signed(entry.get(2, 0))
    pressed = {name for name, bit in KEYPAD_BUTTONS[side].items() if mask & bit}
    horizontal = max(paddles.get(0, 0), paddles.get(1, 0), key=abs)
    vertical = max(paddles.get(2, 0), paddles.get(3, 0), key=abs)
    return pressed, horizontal, vertical


def map_input(data, config, side="right"):
    inputs = decode_input(data, side)
    if inputs is None:
        return None
    pressed, horizontal, vertical = inputs
    bindings = config.left_buttons if side == "left" else config.buttons
    actions = [(action, 100) for name, action in bindings.items() if name in pressed]
    axis = horizontal if config.axis == "lr" else vertical
    if config.invert:
        axis = -axis
    if abs(axis) > config.deadzone:
        amount = min(100, round((abs(axis) - config.deadzone) * 100 / (config.full_scale - config.deadzone)))
        actions.append((config.paddle_left if axis < 0 else config.paddle_right, amount))

    mask = 0
    analog = dict.fromkeys(ANALOG, 0)
    for action, amount in actions:
        if amount and action in DIGITAL:
            mask |= DIGITAL[action]
        elif action in analog:
            analog[action] = max(analog[action], amount)
    steering = analog["steer_right"] - analog["steer_left"]
    brake = analog["brake"]
    left = min(0, steering) if steering < 0 and -steering >= brake else brake
    right = max(0, steering) if steering > 0 and steering >= brake else -brake
    return mask, left, right


async def scan_controllers():
    devices = []
    for device, advertisement in (await BleakScanner.discover(timeout=6, return_adv=True)).values():
        data = advertisement.manufacturer_data.get(0x094A, b"")
        if len(data) in (3, 7) and data[0] in SIDES:
            name = advertisement.local_name or device.name or "Zwift Play"
            devices.append((device, name, data[-2:].hex().upper(), SIDES[data[0]]))
    return devices


def select_controller(devices, selector):
    target = selector.strip().casefold()
    matches = [(device, side, suffix) for device, name, suffix, side in devices
               if target in {name.casefold(), suffix.casefold(), f"zwift play-{side[0]} {suffix.casefold()}"}]
    if len(matches) > 1:
        suffixes = ", ".join(suffix for _, _, suffix in matches)
        raise ValueError(f"Multiple controllers match {selector!r}. Use one of these suffixes: {suffixes}")
    return matches[0][:2] if matches else None


async def watch_play(selector, config, endpoint, monitor):
    while True:
        logging.info("Looking for %s. Wake your controller with a button press.", selector)
        try:
            devices = await scan_controllers()
        except (BleakError, OSError, TimeoutError) as error:
            logging.warning("Bluetooth scan: %s", error)
            await asyncio.sleep(1)
            continue
        selected = select_controller(devices, selector)
        if selected is None:
            continue
        device, side = selected
        try:
            ready, disconnected = asyncio.Event(), asyncio.Event()
            last = None

            def receive(_, payload):
                nonlocal last
                data = bytes(payload)
                if data.startswith(RIDE_ON):
                    ready.set()
                try:
                    state = map_input(data, config, side)
                except ValueError:
                    logging.debug("Ignoring malformed Play input: %s", data.hex(" "))
                    return
                if state is None:
                    return
                ready.set()
                endpoint.update(state)
                if monitor and data != last:
                    last = data
                    pressed, horizontal, vertical = decode_input(data, side)
                    logging.info("buttons=%s LR=%d UD=%d → mask=0x%04x ZL=%d ZR=%d",
                                 ",".join(sorted(pressed)) or "-", horizontal, vertical, *state)

            async with BleakClient(device, timeout=15, pair=False,
                                   disconnected_callback=lambda _: disconnected.set()) as client:
                for characteristic in (dircon.OUT, dircon.TX):
                    await client.start_notify(characteristic.hex(), receive)
                await client.write_gatt_char(dircon.RX.hex(), RIDE_ON, response=False)
                await asyncio.wait_for(ready.wait(), 5)
                logging.info("%s Play %s connected", side.capitalize(), selector)
                await disconnected.wait()
        except (BleakError, OSError, TimeoutError, ValueError) as error:
            logging.warning("Play %s: %s", selector, error)
        finally:
            endpoint.update(dircon.IDLE)
        await asyncio.sleep(1)


async def run(selector, config, name, port, monitor):
    endpoint = dircon.Controller(name)
    if monitor:
        await watch_play(selector, config, endpoint, True)
        return
    async with await asyncio.start_server(endpoint.connect, "0.0.0.0", port):
        registration = await asyncio.create_subprocess_exec(
            "/usr/bin/dns-sd", "-R", endpoint.name, dircon.SERVICE_TYPE, "local.", str(port),
            *endpoint.txt,
        )
        logging.info("Select %s in Zwift Power Source, then Controls. Restore your trainer as Power Source.",
                     endpoint.name)
        tasks = [
            asyncio.create_task(watch_play(selector, config, endpoint, False)),
            asyncio.create_task(endpoint.heartbeat()),
            asyncio.create_task(registration.wait()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            raise RuntimeError("Bonjour registration stopped. Check the dns-sd output.")
        finally:
            for task in tasks:
                task.cancel()
            if registration.returncode is None:
                try:
                    registration.terminate()
                except ProcessLookupError:
                    pass
            await asyncio.gather(*tasks, return_exceptions=True)
            await registration.wait()
            await endpoint.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--device", metavar="NAME_OR_SUFFIX", help="Bluetooth name or four-digit hex suffix")
    mode.add_argument("--scan", action="store_true", help="List supported nearby Zwift Play controllers")
    mode.add_argument("--list-actions", action="store_true", help="Show action names for the TOML config")
    parser.add_argument("--name", default=dircon.DEFAULT_NAME, help="Virtual controller name shown in Zwift")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--port", type=int, default=36866, help="Direct Connect TCP port")
    parser.add_argument("--monitor", action="store_true", help="Show physical inputs without advertising to Zwift")
    args = parser.parse_args()
    if args.list_actions:
        print("\n".join(sorted(ACTIONS)))
        return
    if sys.platform != "darwin":
        parser.error("This release requires macOS and Bonjour.")
    if args.monitor and not args.device:
        parser.error("--monitor requires --device")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    name = args.name.strip()
    if not 1 <= len(name.encode()) <= 63:
        parser.error("--name must contain 1 to 63 bytes")
    if args.device is not None and not args.device.strip():
        parser.error("--device must not be empty")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        if args.scan:
            devices = asyncio.run(scan_controllers())
            for _, bluetooth_name, suffix, side in devices:
                print(f"{bluetooth_name} ({side}, {suffix})  --device {suffix}")
            if not devices:
                parser.exit(1, "No compatible Zwift Play found. Wake it and scan again.\n")
        else:
            asyncio.run(run(args.device, load_config(args.config), name, args.port, args.monitor))
    except (BleakError, OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
