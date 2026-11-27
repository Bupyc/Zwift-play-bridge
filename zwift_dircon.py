"""Serve a virtual Ride controller over Wahoo Direct Connect."""

import asyncio
from collections import defaultdict, deque
from hashlib import blake2s
from itertools import islice
import logging
from socket import gethostname
from struct import Struct
from time import monotonic
from uuid import UUID


logger = logging.getLogger("zwift_play_bridge.dircon")
HEADER = Struct("!BBBBH")
DISCOVER, CHARACTERISTICS, READ, WRITE, SUBSCRIBE, NOTIFY = range(1, 7)
READABLE, WRITABLE, NOTIFIABLE, INDICATABLE = 1, 2, 4, 8
IDLE = (0, 0, 0)


def short_uuid(number):
    return UUID(f"{number:08x}-0000-1000-8000-00805f9b34fb").bytes


ZAP_UUID = "00000001-19ca-4651-86e5-fa29dcdd09d1"
ZAP, OUT, RX, TX = (UUID(f"{number:08x}{ZAP_UUID[8:]}").bytes for number in range(1, 5))
OBC_UUID = "d273f680-d548-419d-b9d1-fa0472345229"
SERVICE_TYPE = "_wahoo-fitness-tnp._tcp"
DEFAULT_NAME = "Zwift Play Bridge"
ADVERTISED_SERVICES = (
    f"0xFC82,0x1818,0x1826,{ZAP_UUID},a026ee0d-0a7d-4ab3-97fa-f1500f9feb8b,{OBC_UUID}"
)
SERVICES = {
    short_uuid(0xFC82): {RX: WRITABLE, OUT: NOTIFIABLE, TX: NOTIFIABLE},
    UUID(OBC_UUID).bytes: {},
    short_uuid(0x1826): {
        short_uuid(0x2ACC): READABLE, short_uuid(0x2AD2): NOTIFIABLE,
        short_uuid(0x2AD5): READABLE, short_uuid(0x2AD6): READABLE,
        short_uuid(0x2AD8): READABLE, short_uuid(0x2AD9): WRITABLE | INDICATABLE,
        short_uuid(0x2ADA): NOTIFIABLE, short_uuid(0x2AD3): READABLE | NOTIFIABLE,
    },
    short_uuid(0x180A): {
        short_uuid(number): READABLE for number in (0x2A29, 0x2A25, 0x2A27, 0x2A26)
    },
    ZAP: {OUT: NOTIFIABLE, RX: WRITABLE, TX: INDICATABLE},
    short_uuid(0x180D): {short_uuid(0x2A37): NOTIFIABLE, short_uuid(0x2A38): READABLE},
}
PROPERTIES = {uuid: flags for service in SERVICES.values() for uuid, flags in service.items()}
READ_VALUES = {
    short_uuid(number): value for number, value in {
        0x2A29: b"Play Bridge", 0x2A26: b"1.0", 0x2A27: b"1.0",
        0x2ACC: bytes.fromhex("8a 40 00 00 1e 20 00 00"),
        0x2AD8: bytes.fromhex("00 00 d0 07 01 00"),
    }.items()
}


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append(128 | (value & 127))
        value >>= 7
    result.append(value)
    return bytes(result)


def protobuf_fields(data):
    """Read scalar and length-delimited fields used by ZAP messages."""
    values = iter(data)

    def read(first):
        value = first & 127
        for shift in (7, 14, 21, 28):
            if first < 128:
                return value
            first = next(values, None)
            if first is None:
                break
            value |= (first & 127) << shift
        if first is not None and first < 16:
            return value
        raise ValueError("Invalid protobuf varint")

    for first in values:
        tag = read(first)
        part = next(values, None)
        if tag < 8 or part is None:
            raise ValueError("Invalid protobuf field")
        value = read(part)
        if tag & 7 == 2:
            payload = bytes(islice(values, value))
            if len(payload) != value:
                raise ValueError("Truncated protobuf field")
            value = payload
        elif tag & 7:
            raise ValueError("Unsupported protobuf field")
        yield tag >> 3, value


def varint_fields(data):
    return {field: value for field, value in protobuf_fields(data) if isinstance(value, int)}


def blob(tag, data):
    return bytes([tag]) + varint(len(data)) + data


def keypad(state):
    mask, left, right = state
    frame = b"\x23\x08" + varint(mask ^ 0xFFFFFFFF)
    for location, value in enumerate((left, right)):
        zigzag = 2 * abs(value) - (value < 0)
        frame += blob(0x1A, bytes([0x08, location, 0x10]) + varint(zigzag))
    return frame


GET_VALUES = {
    16: b"\x08\x00",
    771: bytes.fromhex("08 00 10 64 18 00 20 00"),
    1024: bytes.fromhex("08 ff ff 03 10 03"),
}


class Session:
    def __init__(self, writer, read_values, get_values):
        self.writer = writer
        self.read_values = read_values
        self.get_values = get_values
        self.subscribed = set()
        self.pending = defaultdict(lambda: deque(maxlen=64))
        self.handshake = False

    @property
    def ready(self):
        return self.handshake and OUT in self.subscribed and TX in self.subscribed

    def send(self, operation, sequence, payload=b"", status=0):
        if self.writer.transport.get_write_buffer_size() >= 65536:
            self.writer.close()
        if not self.writer.is_closing():
            self.writer.write(HEADER.pack(1, operation, sequence, status, len(payload)) + payload)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("Bridge -> Zwift [%x] op=0x%02x seq=%d status=%d len=%d payload=%s",
                             id(self), operation, sequence, status, len(payload), payload[:96].hex(" "))

    def reply(self, characteristic, data):
        if characteristic in self.subscribed:
            self.send(NOTIFY, 0, characteristic + data)
        else:
            self.pending[characteristic].append(data)

    def sync(self, data):
        if data.startswith(b"RideOn") and len(data) >= 8:
            self.reply(TX, b"RideOn\x02\x00")
            self.reply(OUT, bytes.fromhex("2a 08 03 12 0d 22 0b 52 49 44 45 5f 4f 4e 28 30 29 00"))
            self.handshake = True
        elif data[:1] == b"\x00":
            try:
                object_id = varint_fields(data[1:]).get(1)
            except ValueError:
                return
            if object_id in self.get_values:
                self.reply(TX, b"\x3c\x08" + varint(object_id) + blob(0x12, self.get_values[object_id]))
            else:
                logger.debug("Zwift GET [%x] object=%s has no bridge reply", id(self), object_id)

    def request(self, op, sequence, payload):
        uuid, data = payload[:16], payload[16:]
        properties = PROPERTIES.get(uuid, 0)
        status, response = 0, b""
        if op == DISCOVER and not payload:
            response = b"".join(SERVICES)
        elif op in (CHARACTERISTICS, READ) and len(payload) == 16:
            if op == CHARACTERISTICS and uuid in SERVICES:
                response = uuid + b"".join(key + bytes([flags]) for key, flags in SERVICES[uuid].items())
            elif op == CHARACTERISTICS:
                status = 3
            elif properties & READABLE:
                response = uuid + self.read_values.get(uuid, b"")
            else:
                status = 5 if properties else 4
        elif op == WRITE and len(payload) >= 16:
            if properties & WRITABLE:
                response = uuid
            else:
                status = 5 if properties else 4
        elif op == SUBSCRIBE and data in (b"\x00", b"\x01"):
            if properties & (NOTIFIABLE | INDICATABLE):
                response = uuid
            else:
                status = 5 if properties else 4
        else:
            status = 2
        self.send(op, sequence, response, status)
        if status == 0 and op == WRITE and uuid == RX:
            self.sync(data)
        elif status == 0 and op == SUBSCRIBE:
            if data == b"\x01":
                self.subscribed.add(uuid)
                for message in self.pending.pop(uuid, ()):
                    self.send(NOTIFY, 0, uuid + message)
            else:
                self.subscribed.discard(uuid)

    def send_state(self, state):
        if self.ready:
            self.send(NOTIFY, 0, OUT + keypad(state))


class Controller:
    def __init__(self, name=DEFAULT_NAME):
        self.name = name
        # Use a locally administered virtual identity independent of the physical controller.
        seed = f"zwift-play-bridge\0{gethostname()}\0{name}".encode()
        address = b"\x02" + blake2s(seed, digest_size=5).digest()
        uid = address.hex().upper().encode()
        serial = str(100_000_000 + int.from_bytes(address, "big") % 900_000_000)
        self.txt = (
            f"ble-service-uuids={ADVERTISED_SERVICES}",
            f"mac-address={address.hex('-').upper()}",
            f"serial-number={serial}",
            f"manufacturer-data=4A0906{uid[-4:].decode()}",
        )
        device_info = (
            b"\x08" + varint(515) + blob(0x12, b"\x00\x00\x01\x01")
            + blob(0x1A, b"Zwift Ride") + blob(0x32, b"0B-" + uid)
            + blob(0x3A, b"B.0") + blob(0x42, b"\x08\x02\x10\x01")
            + b"\x48\x01\x50\x08" + blob(0x5A, uid)
        )
        self.read_values = READ_VALUES | {short_uuid(0x2A25): serial.encode()}
        self.get_values = GET_VALUES | {0: blob(0x0A, device_info)}
        self.state = IDLE
        self.sessions = set()
        self.sent_at = monotonic()

    def update(self, state):
        if self.state != state:
            self.state = state
            self.publish()

    def publish(self):
        self.sent_at = monotonic()
        for session in self.sessions:
            session.send_state(self.state)

    async def heartbeat(self):
        while True:
            await asyncio.sleep(0.1)
            if monotonic() - self.sent_at >= (5 if self.state == IDLE else 0.09):
                self.publish()

    async def connect(self, reader, writer):
        session = Session(writer, self.read_values, self.get_values)
        self.sessions.add(session)
        try:
            while True:
                header = await reader.readexactly(HEADER.size)
                version, op, sequence, code, length = HEADER.unpack(header)
                payload = await reader.readexactly(length)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("Zwift -> Bridge [%x] version=%d op=0x%02x seq=%d status=%d "
                                 "uuid=%s len=%d data=%s",
                                 id(session), version, op, sequence, code, payload[:16].hex(), length,
                                 payload[16:112].hex(" "))
                if version != 1:
                    session.send(op, sequence, status=7)
                elif code == 0:
                    ready = session.ready
                    session.request(op, sequence, payload)
                    if session.ready and not ready:
                        logger.info("Zwift connected")
                        session.send_state(self.state)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.sessions.discard(session)
            logger.debug("Zwift Direct Connect session [%x] closed", id(session))
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def close(self):
        self.update(IDLE)
        writers = [session.writer for session in self.sessions]
        for writer in writers:
            writer.close()
        await asyncio.gather(*(writer.wait_closed() for writer in writers), return_exceptions=True)
