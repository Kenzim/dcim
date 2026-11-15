"""IUSB packets and SCSI CD-ROM responses for the AMI MegaRAC HTML5 viewer's CD redirection.

The BMC exposes a USB CD drive to the host and forwards the host's SCSI commands
over ``wss://<bmc>/cd-server``. The client (normally the browser viewer) answers
them from an image. Frames are a 32-byte little-endian IUSB header followed by the
SCSI/control payload; the opcode is the byte at offset 41, which is also the first
byte of the SCSI CDB, so the CDB fields sit at fixed offsets in the frame.
"""
from __future__ import annotations

import struct
from typing import Optional, Protocol

HEADER_LEN = 32
SECTOR = 2048
MAX_READ_BYTES = 16 * 1024 * 1024

AUTH_CMD = 0xF2
DEVICE_INFO = 0xF8
REDIRECTION_ACK = 0xF1
KEEP_ALIVE = 0xF3
KILL_REDIRECTION = 0xF6
SESSION_DISCONNECT = 0xF7

TEST_UNIT_READY = 0x00
START_STOP_UNIT = 0x1B
MEDIUM_REMOVAL = 0x1E
READ_CAPACITY = 0x25
READ_10 = 0x28
READ_TOC = 0x43
READ_12 = 0xA8

DEVICE_TYPE_CD = 5
CLIENT_H5VIEWER = 3

ACK_ACCEPTED = (1, 27, 28)
ACK_NAMES = {
    1: "accepted",
    3: "login failed",
    4: "in use",
    5: "permission denied",
    8: "maximum users",
    9: "port not listening",
    27: "accepted (media boost)",
    28: "accepted (no media boost)",
}

# (sense key, ASC, ASCQ) written at offsets 54..56 of a reply; byte 53 flags a check condition.
SENSE_OK = (0, 0, 0)
SENSE_MEDIUM_CHANGED = (6, 0x28, 0)
SENSE_OUT_OF_RANGE = (5, 0x21, 0)
SENSE_INVALID_OPCODE = (5, 0x20, 0)


class IsoReader(Protocol):
    size: int

    async def read(self, offset: int, length: int) -> bytes: ...


def finalize(packet: bytearray) -> bytes:
    """Fill the length, direction and checksum fields of an outgoing frame."""
    struct.pack_into("<I", packet, 12, len(packet) - HEADER_LEN)
    packet[19] = 0x80
    packet[11] = 0
    packet[11] = (-sum(packet[:HEADER_LEN])) & 0xFF
    return bytes(packet)


def _header(total_len: int) -> bytearray:
    header = bytearray(HEADER_LEN)
    header[0:8] = b"IUSB    "
    header[8], header[9], header[10] = 1, 0, HEADER_LEN
    struct.pack_into("<I", header, 12, total_len - HEADER_LEN)
    header[17], header[18], header[19] = DEVICE_TYPE_CD, 1, 0x80
    return header


def auth_packet(token: str) -> bytes:
    packet = bytearray(62 + HEADER_LEN + 98 + 1)
    packet[:HEADER_LEN] = _header(len(packet))
    packet[41] = AUTH_CMD
    encoded = token.encode()
    packet[63:63 + len(encoded)] = encoded
    return finalize(packet)


def device_info_packet(name: str) -> bytes:
    packet = bytearray(62 - HEADER_LEN + 260 + 1)
    packet[:HEADER_LEN] = _header(len(packet))
    packet[41] = DEVICE_INFO
    struct.pack_into("<I", packet, 62, CLIENT_H5VIEWER)
    encoded = name.encode()[:250] + b"\x00"
    packet[66:66 + len(encoded)] = encoded
    return finalize(packet)


def disconnect_packet() -> bytes:
    packet = bytearray(62)
    packet[:HEADER_LEN] = _header(len(packet))
    packet[41] = SESSION_DISCONNECT
    return finalize(packet)


def opcode(packet: bytes) -> int:
    return packet[41] if len(packet) > 41 else -1


def ack_status(packet: bytes) -> Optional[int]:
    """Status byte of a DEVICE_REDIRECTION_ACK frame, else None."""
    if opcode(packet) != REDIRECTION_ACK or len(packet) <= 62:
        return None
    return packet[62]


def split_frames(buffer: bytearray) -> list[bytes]:
    """Remove and return every complete frame at the start of ``buffer``."""
    frames: list[bytes] = []
    while len(buffer) >= HEADER_LEN:
        total = HEADER_LEN + struct.unpack_from("<I", buffer, 12)[0]
        if len(buffer) < total:
            break
        frames.append(bytes(buffer[:total]))
        del buffer[:total]
    return frames


def _toc(total_sectors: int, allocation: int, msf: bool) -> bytes:
    """One data track starting at LBA 0 plus the lead-out, as a READ TOC reply
    zero-padded to the allocation length."""
    lead_out = total_sectors + 150
    first_start = bytes([0, 0, 2, 0]) if msf else bytes(4)
    last_start = bytes([0, (lead_out // 75 // 60) & 0xFF, (lead_out // 75 % 60) & 0xFF, lead_out % 75])
    body = bytes([1, 1]) + bytes([0, 0x14, 1, 0]) + first_start + bytes([0, 0x16, 0xAA, 0]) + last_start
    toc = struct.pack(">H", len(body)) + body
    return toc[:allocation].ljust(allocation, b"\0") if allocation else toc


class ScsiResponder:
    """Answers the host's SCSI commands (forwarded by the BMC) from an image."""

    def __init__(self, image: IsoReader):
        self.image = image
        self.total_sectors = (image.size + SECTOR - 1) // SECTOR
        self.bytes_served = 0
        self.reads = 0
        self.ops: dict[int, int] = {}
        self._announce_medium = True

    def new_session(self) -> None:
        """A fresh BMC connection: tell the host the medium (re)appeared."""
        self._announce_medium = True

    async def respond(self, request: bytes) -> Optional[bytes]:
        """Reply frame for ``request``; None when the BMC is ending the session."""
        op = opcode(request)
        if op in (KILL_REDIRECTION, SESSION_DISCONNECT):
            return None
        if op == KEEP_ALIVE:
            return finalize(bytearray(request))
        self.ops[op] = self.ops.get(op, 0) + 1
        reply = bytearray(request[:61].ljust(61, b"\0"))
        sense, data = await self._execute(op, request)
        reply[53] = 1 if sense[0] else 0
        reply[54:57] = bytes(sense)
        struct.pack_into("<I", reply, 57, len(data))
        return finalize(reply + data)

    async def _execute(self, op: int, request: bytes) -> tuple[tuple[int, int, int], bytes]:
        if op == TEST_UNIT_READY:
            if self._announce_medium:
                self._announce_medium = False
                return SENSE_MEDIUM_CHANGED, b""
            return SENSE_OK, b""
        if op == READ_CAPACITY:
            return SENSE_OK, struct.pack(">II", max(self.total_sectors - 1, 0), SECTOR)
        if op in (READ_10, READ_12):
            return await self._read(op, request)
        if op == READ_TOC:
            allocation = struct.unpack_from(">H", request, 48)[0]
            return SENSE_OK, _toc(self.total_sectors, allocation, bool(request[42] & 2))
        if op in (START_STOP_UNIT, MEDIUM_REMOVAL):
            return SENSE_OK, b""
        return SENSE_INVALID_OPCODE, b""

    async def _read(self, op: int, request: bytes) -> tuple[tuple[int, int, int], bytes]:
        lba = struct.unpack_from(">I", request, 43)[0]
        count = (
            struct.unpack_from(">H", request, 48)[0]
            if op == READ_10
            else struct.unpack_from(">I", request, 47)[0]
        )
        if lba + count > self.total_sectors or count * SECTOR > MAX_READ_BYTES:
            return SENSE_OUT_OF_RANGE, b""
        data = await self.image.read(lba * SECTOR, count * SECTOR)
        data = data.ljust(count * SECTOR, b"\0")
        self.reads += 1
        self.bytes_served += len(data)
        return SENSE_OK, data
