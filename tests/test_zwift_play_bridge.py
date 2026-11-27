import asyncio
from dataclasses import replace
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

import zwift_dircon as dircon
import zwift_play_bridge as bridge


CONFIG = Path(__file__).resolve().parents[1] / "zwift-play-bridge.toml"
NEUTRAL = bytes.fromhex("07 08 00 10 01 18 01 20 01 28 01 30 01 38 01 40 00 48 00")


class RideOnTests(unittest.IsolatedAsyncioTestCase):
    async def test_selects_rideon_from_firmware_revision(self):
        for revision, message in (
            (b"1.3.1", b"RideOn"),
            (b"2.0.0", b"RideOn\x02\x03"),
            (b"2.0.1\x00", b"RideOn\x02\x03"),
            (b"3.1.0", b"RideOn\x02\x03"),
        ):
            with self.subTest(revision=revision):
                client = AsyncMock(spec=bridge.BleakClient)
                client.read_gatt_char.return_value = bytearray(revision)
                await bridge.send_ride_on(client, "test")
                client.read_gatt_char.assert_awaited_once_with("00002a26-0000-1000-8000-00805f9b34fb")
                client.write_gatt_char.assert_awaited_once_with(dircon.RX.hex(), message, response=False)

    async def test_uses_short_rideon_if_firmware_read_fails(self):
        client = AsyncMock(spec=bridge.BleakClient)
        client.read_gatt_char.side_effect = bridge.BleakError("Firmware Revision unavailable")
        await bridge.send_ride_on(client, "test")
        client.write_gatt_char.assert_awaited_once_with(dircon.RX.hex(), b"RideOn", response=False)


class DeviceSelectionTests(unittest.TestCase):
    def test_selects_by_advertised_name_and_suffix(self):
        first, second = object(), object()
        devices = [(first, "My Right Controller", "102A", "right"), (second, "Zwift Play", "B048", "left")]
        self.assertEqual(bridge.select_controller(devices, " my right controller "), (first, "right"))
        self.assertEqual(bridge.select_controller(devices, "b048"), (second, "left"))
        self.assertEqual(bridge.select_controller(devices, "Zwift Play-R 102A"), (first, "right"))
        self.assertEqual(bridge.select_controller(devices, "Zwift Play-L B048"), (second, "left"))

    def test_duplicate_advertised_names_require_the_suffix(self):
        devices = [(object(), "Zwift Play", "102A", "right"), (object(), "Zwift Play", "B048", "left")]
        with self.assertRaisesRegex(ValueError, "102A.*B048"):
            bridge.select_controller(devices, "Zwift Play")

    def test_both_mode_distinguishes_sides_with_the_same_name(self):
        right, left = object(), object()
        devices = [(right, "Zwift Play", "102A", "right"), (left, "Zwift Play", "B048", "left")]
        self.assertEqual(bridge.select_controller(devices, "Zwift Play", "left"), (left, "left"))
        self.assertEqual(bridge.select_controller(devices, "Zwift Play", "right"), (right, "right"))
        self.assertIsNone(bridge.select_controller(devices[:1], "Zwift Play", "left"))
        with self.assertRaisesRegex(ValueError, "right.*left"):
            bridge.select_controller(devices, "102A", "left")


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.config = bridge.load_config(CONFIG)

    def test_recorded_buttons_and_release_map_to_native_shifters(self):
        for offset, mask in ((4, 0x1000), (10, 0x0100)):
            pressed = bytearray(NEUTRAL)
            pressed[offset] = 0
            self.assertEqual(bridge.map_input(pressed, self.config), (mask, 0, 0))
        self.assertEqual(bridge.map_input(NEUTRAL, self.config), (0, 0, 0))
        self.assertIsNone(bridge.map_input(bytes.fromhex("19 10 5e"), self.config))

    def test_sram_button_presses_both_shifters_in_one_frame(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "sram.toml"
            path.write_text('[buttons]\nSHIFT = "shift_both"\n', encoding="utf-8")
            config = bridge.load_config(path)

        pressed = bytearray(NEUTRAL)
        pressed[12] = 0
        frame = dircon.keypad(bridge.map_input(pressed, config))
        self.assertEqual(frame, bytes.fromhex("23 08 ff dd ff ff 0f 1a 04 08 00 10 00 1a 04 08 01 10 00"))
        self.assertEqual(bridge.map_input(NEUTRAL, config), (0, 0, 0))

    def test_left_and_right_use_outward_paddles(self):
        for value, expected in (("c7 01", (0, -100, 0)), ("c8 01", (0, 0, 100)),
                                ("0c", (0, 0, 0))):
            sample = NEUTRAL[:-4] + b"\x40" + bytes.fromhex(value) + b"\x48\x00"
            self.assertEqual(bridge.map_input(sample, self.config), expected)
        inverted = replace(self.config, invert=True)
        self.assertEqual(bridge.map_input(bytes.fromhex("07 40 c7 01"), inverted), (0, 0, 100))

    def test_config_can_remap_a_button_to_steering_and_paddle_to_brake(self):
        config = replace(self.config, buttons={"A": "steer_left"}, paddle_left="brake")
        self.assertEqual(bridge.map_input(bytes.fromhex("07 20 00"), config), (0, -100, 0))
        self.assertEqual(bridge.map_input(bytes.fromhex("07 40 c7 01"), config), (0, 100, -100))

    def test_malformed_input_does_not_turn_into_a_press(self):
        with self.assertRaises(ValueError):
            bridge.map_input(bytes.fromhex("07 10 00 40 80"), self.config)

    def test_left_controller_maps_legacy_and_new_firmware(self):
        neutral_paddles = "1a 04 08 00 10 00 1a 04 08 01 10 00"
        for packet, expected_mask in (
            ("23 08 fd ff ff ff 0f " + neutral_paddles, 0x1000),  # Left 2.0.1, Up.
            ("23 08 f7 ff ff ff 0f " + neutral_paddles, 0x0100),  # Down.
            ("23 08 ff fd ff ff 0f " + neutral_paddles, 0x1100),  # Side shift.
            ("07 08 01 10 00", 0x1000),  # Legacy left, Up.
        ):
            self.assertEqual(bridge.map_input(bytes.fromhex(packet), self.config, "left"), (expected_mask, 0, 0))

    def test_new_left_paddle_can_map_a_brake_motion_to_steering(self):
        packet = "23 08 ff ff ff ff 0f 1a 05 08 00 10 c8 01 1a 04 08 01 10 00"
        self.assertEqual(bridge.map_input(bytes.fromhex(packet), self.config, "left"), (0, 0, 100))

    def test_passthrough_keeps_native_buttons_and_paddle_signs(self):
        for side, frame, state in (
            ("right", "07 10 00 18 00 20 00 28 00 30 00 40 c7 01", (0x10F0, 0, -100)),
            ("right", "07 40 c8 01", (0, 0, 100)),
            ("left", "23 08 fd ff ff ff 0f 1a 05 08 00 10 c8 01", (0x02, 100, 0)),
            ("left", "23 08 ff fd ff ff 0f 1a 05 08 00 10 c7 01", (0x0100, -100, 0)),
        ):
            with self.subTest(side=side, frame=frame):
                self.assertEqual(bridge.map_input(bytes.fromhex(frame), None, side), state)

    def test_one_side_releasing_preserves_the_other_side(self):
        endpoint = dircon.Controller()
        inputs = bridge.PlayInputs(endpoint)
        inputs.update("left", (0x0100, -75, 0))
        inputs.update("right", (0x1000, 0, -65))
        self.assertEqual(endpoint.state, (0x1100, -75, -65))
        inputs.update("left", dircon.IDLE)
        self.assertEqual(endpoint.state, (0x1000, 0, -65))
        inputs.update("right", dircon.IDLE)
        self.assertEqual(endpoint.state, dircon.IDLE)


class DirectConnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_handshake_and_remapped_input_over_tcp(self):
        endpoint = dircon.Controller()
        async with await asyncio.start_server(endpoint.connect, "127.0.0.1", 0) as server:
            reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname())
            self.addAsyncCleanup(writer.wait_closed)
            self.addCleanup(writer.close)

            async def receive():
                async with asyncio.timeout(1):
                    header = await reader.readexactly(6)
                    size = int.from_bytes(header[4:], "big")
                    return header[:4], await reader.readexactly(size)

            def request(op, sequence, data=b""):
                return struct.pack("!BBBBH", 1, op, sequence, 0, len(data)) + data

            writer.write(request(1, 1)[:3])
            await writer.drain()
            writer.write(request(1, 1)[3:] + request(4, 2, dircon.RX + b"RideOn\x02\x04")
                         + request(5, 3, dircon.TX + b"\x01"))
            await writer.drain()
            header, services = await receive()
            self.assertEqual(header, b"\x01\x01\x01\x00")
            self.assertIn(bytes.fromhex("0000fc8200001000800000805f9b34fb"), services)
            self.assertEqual((await receive())[0], b"\x01\x04\x02\x00")
            self.assertEqual((await receive())[0], b"\x01\x05\x03\x00")
            self.assertEqual(await receive(), (b"\x01\x06\x00\x00", dircon.TX + b"RideOn\x02\x00"))

            writer.write(request(5, 4, dircon.OUT + b"\x01"))
            await writer.drain()
            self.assertEqual((await receive())[0], b"\x01\x05\x04\x00")
            self.assertTrue((await receive())[1].startswith(dircon.OUT + b"\x2a"))
            idle = bytes.fromhex("23 08 ff ff ff ff 0f 1a 04 08 00 10 00 1a 04 08 01 10 00")
            self.assertEqual((await receive())[1], dircon.OUT + idle)

            state = bridge.map_input(bytes.fromhex("07 10 00 40 c7 01"), bridge.load_config(CONFIG))
            endpoint.update(state)
            mapped = bytes.fromhex("23 08 ff df ff ff 0f 1a 05 08 00 10 c7 01 1a 04 08 01 10 00")
            self.assertEqual((await receive())[1], dircon.OUT + mapped)
            endpoint.update((0, 0, 0))
            self.assertEqual((await receive())[1], dircon.OUT + idle)
            writer.close()
            await writer.wait_closed()
