"""Bridge one or two Zwift Play controllers to Zwift Ride controls on macOS."""

import argparse
import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
import logging
from pathlib import Path
import sys
import tomllib

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

import zwift_dircon as dircon


logger = logging.getLogger("zwift_play_bridge")
SIDES = {0x02: "right", 0x03: "left", 0x0E: "right", 0x0F: "left"}
# Legacy ZwiftPlayNotification assigns btnSide to field 6 and btnFront to field 7.
LEGACY_BUTTONS = {
    "right": {"Y": 2, "Z": 3, "A": 4, "B": 5, "SHIFT": 6, "FRONT": 7},
    "left": {"UP": 2, "LEFT": 3, "RIGHT": 4, "DOWN": 5, "SHIFT": 6, "FRONT": 7},
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
FIRMWARE_REVISION = "00002a26-0000-1000-8000-00805f9b34fb"
SHIFT_MASK = LS1 | RS1 | DIGITAL["shift_down_2"] | DIGITAL["shift_up_2"]
LOCAL_HAPTIC_PULSE = bytes.fromhex("12 12 08 0a 06 08 02 10 00 18 20")


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
    return None if inputs is None else map_controls(inputs, config, side)


def map_controls(inputs, config, side):
    pressed, horizontal, vertical = inputs
    if config is None:
        mask = sum(KEYPAD_BUTTONS[side][name] for name in pressed)
        paddle = max(-100, min(100, horizontal))
        return (mask, paddle, 0) if side == "left" else (mask, 0, paddle)

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


def select_controller(devices, selector, expected_side=None):
    target = selector.strip().casefold()
    matches = [(device, side, suffix) for device, name, suffix, side in devices
               if target in {name.casefold(), suffix.casefold(), f"zwift play-{side[0]} {suffix.casefold()}"}]
    if expected_side is not None:
        matching_side = [match for match in matches if match[1] == expected_side]
        if not matching_side:
            for _, side, suffix in matches:
                if target in (suffix.casefold(), f"zwift play-{side[0]} {suffix.casefold()}"):
                    raise ValueError(f"{selector!r} is a {side} controller, expected {expected_side}")
        matches = matching_side
    if len(matches) > 1:
        suffixes = ", ".join(suffix for _, _, suffix in matches)
        raise ValueError(f"Multiple controllers match {selector!r}. Use one of these suffixes: {suffixes}")
    return matches[0][:2] if matches else None


class PlayInputs:
    def __init__(self, endpoint, local_haptics=False):
        self.endpoint = endpoint
        self.states = {"left": dircon.IDLE, "right": dircon.IDLE}
        self.clients = {}
        self.local_haptics = local_haptics
        self.local_effects = {}
        self.braking = set()

    def update(self, side, state):
        previous = self.states[side]
        self.states[side] = state
        left, right = self.states["left"], self.states["right"]
        self.endpoint.update((
            left[0] | right[0],
            max(left[1], right[1], key=abs),
            max(right[2], left[2], key=abs),
        ))
        if self.local_haptics and side in self.clients:
            new_buttons = state[0] & ~previous[0]
            brake = max(state[1], -state[2])
            start_brake = brake >= 20 and side not in self.braking
            if start_brake:
                self.braking.add(side)
            elif brake <= 10:
                self.braking.discard(side)
            if new_buttons & DIGITAL["ride_on"]:
                self.start_local_haptic(side, "ride_on")
            elif new_buttons & SHIFT_MASK:
                self.start_local_haptic(side, "shift")
            elif start_brake:
                self.start_local_haptic(side, "brake")

    def start_local_haptic(self, side, effect):
        if not self.local_haptics:
            return
        previous = self.local_effects.get(side)
        if previous is not None:
            previous.cancel()
        self.local_effects[side] = asyncio.create_task(self.play_local_haptic(side, effect))

    async def play_local_haptic(self, side, effect):
        client = self.clients[side]
        if effect == "ride_on":
            await asyncio.sleep(0.55)
        for pulse in range({"shift": 1, "brake": 2, "connect": 3, "ride_on": 3}[effect]):
            if pulse:
                await asyncio.sleep(0.15)
            if self.clients.get(side) is not client:
                return
            if effect == "ride_on" and not self.states[side][0] & DIGITAL["ride_on"]:
                return
            try:
                logger.debug("Bridge -> Play %s GATT RX write without response: %s",
                             side, LOCAL_HAPTIC_PULSE.hex(" "))
                async with asyncio.timeout(1):
                    await client.write_gatt_char(dircon.RX.hex(), LOCAL_HAPTIC_PULSE, response=False)
            except (BleakError, OSError, TimeoutError) as error:
                logger.warning("Play %s vibration write failed: %s", side, error)
                return


async def send_ride_on(client, selector):
    try:
        value = await asyncio.wait_for(client.read_gatt_char(FIRMWARE_REVISION), timeout=3)
        firmware = value.decode("ascii", errors="replace").strip("\0 \r\n\t")
    except (BleakError, OSError, TimeoutError) as error:
        logger.debug("Firmware revision unavailable for %s (%s)", selector, error)
        firmware = ""
    major = firmware.lstrip("vV").split(".", 1)[0]
    versioned = major.isdecimal() and int(major) >= 2
    message = RIDE_ON + b"\x02\x03" if versioned else RIDE_ON
    logger.info("Play %s firmware %s, sending %s", selector, firmware or "unknown",
                "RideOn 02 03" if versioned else "RideOn")
    await client.write_gatt_char(dircon.RX.hex(), message, response=False)


async def watch_play(selector, expected_side, config, inputs, monitor, bluetooth_lock):
    while True:
        side = None
        try:
            ready, disconnected = asyncio.Event(), asyncio.Event()
            last = None

            def receive(characteristic, payload):
                nonlocal last
                data = bytes(payload)
                if data.startswith(RIDE_ON):
                    ready.set()
                try:
                    controls = decode_input(data, side)
                except ValueError:
                    logger.debug("Ignoring malformed Play input: %s", data[:96].hex(" "))
                    return
                if logger.isEnabledFor(logging.DEBUG) and (controls is None or data != last):
                    logger.debug("Play %s %s -> Bridge GATT notify uuid=%s len=%d data=%s",
                                 side, selector, characteristic.uuid, len(data), data[:96].hex(" "))
                if controls is None:
                    return
                ready.set()
                pressed, horizontal, vertical = controls
                state = map_controls(controls, config, side)
                inputs.update(side, state)
                if data != last:
                    last = data
                    if monitor:
                        logger.info("%s %s buttons=%s LR=%d UD=%d → mask=0x%04x ZL=%d ZR=%d", side, selector,
                                    ",".join(sorted(pressed)) or "-", horizontal, vertical, *state)

            async with AsyncExitStack() as connection:
                # Serialize discovery and the initial GATT setup. Active links keep running.
                async with bluetooth_lock:
                    logger.info("Looking for %s. Wake your controller with a button press.", selector)
                    selected = select_controller(await scan_controllers(), selector, expected_side)
                    if selected is None:
                        continue
                    device, side = selected
                    client = await connection.enter_async_context(BleakClient(
                        device, timeout=15, pair=False,
                        disconnected_callback=lambda _: disconnected.set(),
                    ))
                    for characteristic in (dircon.OUT, dircon.TX):
                        await client.start_notify(characteristic.hex(), receive)
                    await send_ride_on(client, selector)
                    await asyncio.wait_for(ready.wait(), 5)
                    inputs.clients[side] = client
                    logger.info("%s Play %s connected", side.capitalize(), selector)
                    inputs.start_local_haptic(side, "connect")
                await disconnected.wait()
                logger.info("%s Play %s disconnected", side.capitalize(), selector)
        except (BleakError, OSError, TimeoutError) as error:
            logger.warning("Play %s: %s", selector, error)
        finally:
            if side is not None:
                inputs.clients.pop(side, None)
                inputs.braking.discard(side)
                effect = inputs.local_effects.pop(side, None)
                if effect is not None:
                    effect.cancel()
                    await asyncio.gather(effect, return_exceptions=True)
                inputs.update(side, dircon.IDLE)
        await asyncio.sleep(1)


async def watch_inputs(targets, config, endpoint, monitor, local_haptics):
    inputs = PlayInputs(endpoint, local_haptics)
    bluetooth_lock = asyncio.Lock()
    tasks = [
        asyncio.create_task(watch_play(selector, side, config, inputs, monitor, bluetooth_lock))
        for side, selector in targets.items()
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run(targets, config, name, port, monitor, local_haptics=False):
    endpoint = dircon.Controller(name)
    if monitor:
        await watch_inputs(targets, config, endpoint, True, local_haptics)
        return
    async with await asyncio.start_server(endpoint.connect, "0.0.0.0", port):
        registration = await asyncio.create_subprocess_exec(
            "/usr/bin/dns-sd", "-R", endpoint.name, dircon.SERVICE_TYPE, "local.", str(port),
            *endpoint.txt,
        )
        logger.info("Select %s in Zwift Power Source, then Controls. Restore your trainer as Power Source.",
                    endpoint.name)
        tasks = [
            asyncio.create_task(watch_inputs(targets, config, endpoint, False, local_haptics)),
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
    mode.add_argument("--both", action="store_true", help="Use one left and one right controller")
    mode.add_argument("--scan", action="store_true", help="List supported nearby Zwift Play controllers")
    mode.add_argument("--list-actions", action="store_true", help="Show action names for the TOML config")
    parser.add_argument("--left", metavar="NAME_OR_SUFFIX", help="Left controller to use with --both")
    parser.add_argument("--right", metavar="NAME_OR_SUFFIX", help="Right controller to use with --both")
    parser.add_argument("--passthrough", action="store_true", help="Keep the stock Play bindings in --both mode")
    parser.add_argument("--name", default=dircon.DEFAULT_NAME, help="Virtual controller name shown in Zwift")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--port", type=int, default=36866, help="Direct Connect TCP port")
    parser.add_argument("--monitor", action="store_true", help="Show physical inputs without advertising to Zwift")
    parser.add_argument("--local-haptics", action="store_true", help="Generate vibration from local controller input")
    parser.add_argument("--debug", action="store_true", help="Show protocol traffic in the terminal")
    parser.add_argument("--log-file", type=Path, help="Append a detailed protocol log to this file")
    args = parser.parse_args()
    if args.both:
        if not (args.left and args.left.strip() and args.right and args.right.strip()):
            parser.error("--both requires --left and --right")
    elif args.left is not None or args.right is not None or args.passthrough:
        parser.error("--left, --right and --passthrough require --both")
    if args.monitor and not (args.device or args.both):
        parser.error("--monitor requires --device or --both")
    if args.list_actions:
        print("\n".join(sorted(ACTIONS)))
        return
    if sys.platform != "darwin":
        parser.error("This release requires macOS and Bonjour.")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    name = args.name.strip()
    if not 1 <= len(name.encode()) <= 63:
        parser.error("--name must contain 1 to 63 bytes")
    if args.device is not None and not args.device.strip():
        parser.error("--device must not be empty")
    try:
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG if args.debug else logging.INFO)
        handlers = [console]
        log_path = args.log_file.expanduser().resolve() if args.log_file is not None else None
        if log_path is not None:
            handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
        logging.basicConfig(level=logging.INFO, handlers=handlers, datefmt="%Y-%m-%d %H:%M:%S",
                            format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s")
        if args.debug or log_path is not None:
            logger.setLevel(logging.DEBUG)
        if log_path is not None:
            logger.info("Protocol log: %s", log_path)
        if args.scan:
            devices = asyncio.run(scan_controllers())
            for _, bluetooth_name, suffix, side in devices:
                print(f"{bluetooth_name} ({side}, {suffix})  --device {suffix}")
            if not devices:
                parser.exit(1, "No compatible Zwift Play found. Wake it and scan again.\n")
        else:
            targets = {"left": args.left, "right": args.right} if args.both else {None: args.device}
            config = None if args.passthrough else load_config(args.config)
            asyncio.run(run(targets, config, name, args.port, args.monitor, args.local_haptics))
    except (BleakError, OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
