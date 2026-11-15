"""IUSB framing and SCSI answers for the MegaRAC CD redirection (no BMC)."""
from __future__ import annotations

import struct

import pytest

from app.services.ipmi_kvm import iusb
from app.services.ipmi_kvm.iusb import SECTOR, ScsiResponder


class _Image:
    def __init__(self, sectors: int):
        self.data = b"".join(bytes([i % 251]) * SECTOR for i in range(sectors))
        self.size = len(self.data)

    async def read(self, offset: int, length: int) -> bytes:
        return self.data[offset:offset + length]


def _request(op: int, *, lba: int = 0, count: int = 0, flags: int = 0, alloc: int = 0) -> bytes:
    """A BMC-to-client SCSI request frame: CDB starts at offset 41."""
    frame = bytearray(62)
    frame[0:8] = b"IUSB    "
    frame[41] = op
    frame[42] = flags
    struct.pack_into(">I", frame, 43, lba)
    if op == iusb.READ_12:
        struct.pack_into(">I", frame, 47, count)
    elif op == iusb.READ_TOC:
        struct.pack_into(">H", frame, 48, alloc)
    else:
        struct.pack_into(">H", frame, 48, count)
    return iusb.finalize(frame)


def _unpack(reply: bytes) -> tuple[int, tuple, bytes]:
    length = struct.unpack_from("<I", reply, 57)[0]
    return reply[53], tuple(reply[54:57]), reply[61:61 + length]


def _checksum_ok(frame: bytes) -> bool:
    return sum(frame[:32]) & 0xFF == 0


def test_auth_packet_layout():
    packet = iusb.auth_packet("tok-123")
    assert packet[:8] == b"IUSB    "
    assert packet[8:11] == bytes([1, 0, 32])
    assert struct.unpack_from("<I", packet, 12)[0] == len(packet) - 32
    assert packet[17:20] == bytes([5, 1, 0x80])
    assert packet[41] == iusb.AUTH_CMD
    assert packet[63:70] == b"tok-123"
    assert _checksum_ok(packet)


def test_device_info_packet_names_the_image():
    packet = iusb.device_info_packet("boot.iso")
    assert packet[41] == iusb.DEVICE_INFO
    assert struct.unpack_from("<I", packet, 62)[0] == iusb.CLIENT_H5VIEWER
    assert packet[66:74] == b"boot.iso" and packet[74] == 0
    assert _checksum_ok(packet)


def test_disconnect_packet():
    packet = iusb.disconnect_packet()
    assert iusb.opcode(packet) == iusb.SESSION_DISCONNECT
    assert _checksum_ok(packet)


def test_ack_status_reads_only_ack_frames():
    ack = bytearray(63)
    ack[41], ack[62] = iusb.REDIRECTION_ACK, 27
    assert iusb.ack_status(bytes(ack)) == 27
    assert iusb.ack_status(_request(iusb.READ_10)) is None


def test_split_frames_keeps_partial_data():
    a, b = iusb.auth_packet("a"), iusb.disconnect_packet()
    buffer = bytearray(a + b[:40])
    assert iusb.split_frames(buffer) == [a]
    buffer.extend(b[40:])
    assert iusb.split_frames(buffer) == [b]
    assert not buffer


@pytest.mark.asyncio
async def test_first_test_unit_ready_reports_medium_change_then_ok():
    responder = ScsiResponder(_Image(4))
    first = _unpack(await responder.respond(_request(iusb.TEST_UNIT_READY)))
    second = _unpack(await responder.respond(_request(iusb.TEST_UNIT_READY)))
    assert first[:2] == (1, iusb.SENSE_MEDIUM_CHANGED)
    assert second[:2] == (0, (0, 0, 0))


@pytest.mark.asyncio
async def test_read_capacity_reports_last_lba_and_sector_size():
    responder = ScsiResponder(_Image(10))
    _, _, data = _unpack(await responder.respond(_request(iusb.READ_CAPACITY)))
    assert struct.unpack(">II", data) == (9, SECTOR)


@pytest.mark.asyncio
@pytest.mark.parametrize("op", [iusb.READ_10, iusb.READ_12])
async def test_read_returns_the_requested_sectors(op):
    image = _Image(8)
    responder = ScsiResponder(image)
    reply = await responder.respond(_request(op, lba=3, count=2))
    check, sense, data = _unpack(reply)
    assert (check, sense) == (0, (0, 0, 0))
    assert data == image.data[3 * SECTOR:5 * SECTOR]
    assert responder.bytes_served == 2 * SECTOR and responder.reads == 1
    assert _checksum_ok(reply)
    assert struct.unpack_from("<I", reply, 12)[0] == len(reply) - 32


@pytest.mark.asyncio
async def test_read_past_the_end_is_rejected():
    responder = ScsiResponder(_Image(4))
    check, sense, data = _unpack(await responder.respond(_request(iusb.READ_10, lba=3, count=2)))
    assert (check, sense, data) == (1, iusb.SENSE_OUT_OF_RANGE, b"")


@pytest.mark.asyncio
async def test_short_image_tail_is_padded_to_a_whole_sector():
    image = _Image(2)
    image.data = image.data[:-100]
    image.size = len(image.data)
    responder = ScsiResponder(image)
    assert responder.total_sectors == 2
    _, _, data = _unpack(await responder.respond(_request(iusb.READ_10, lba=1, count=1)))
    assert len(data) == SECTOR and data.endswith(b"\0" * 100)


@pytest.mark.asyncio
async def test_read_toc_describes_one_data_track_and_lead_out():
    responder = ScsiResponder(_Image(300))
    _, _, data = _unpack(await responder.respond(_request(iusb.READ_TOC, flags=2, alloc=20)))
    assert struct.unpack(">H", data[:2])[0] == 18
    assert len(data) == 20
    assert data[2:4] == bytes([1, 1])
    assert data[5] == 0x14 and data[6] == 1
    assert data[8:12] == bytes([0, 0, 2, 0])  # first track at MSF 00:02:00
    assert data[14] == 0xAA  # lead-out
    lead_out = 300 + 150
    assert data[16:20] == bytes([0, lead_out // 75 // 60, lead_out // 75 % 60, lead_out % 75])


@pytest.mark.asyncio
async def test_read_toc_honours_the_allocation_length():
    responder = ScsiResponder(_Image(10))
    _, _, data = _unpack(await responder.respond(_request(iusb.READ_TOC, alloc=4)))
    assert len(data) == 4


@pytest.mark.asyncio
async def test_read_toc_pads_to_a_larger_allocation_length():
    responder = ScsiResponder(_Image(10))
    _, _, data = _unpack(await responder.respond(_request(iusb.READ_TOC, alloc=32)))
    assert len(data) == 32 and data[20:] == bytes(12)


@pytest.mark.asyncio
async def test_unknown_opcode_is_an_illegal_request():
    responder = ScsiResponder(_Image(1))
    check, sense, _ = _unpack(await responder.respond(_request(0x12)))
    assert (check, sense) == (1, iusb.SENSE_INVALID_OPCODE)


@pytest.mark.asyncio
async def test_keep_alive_is_echoed_and_kill_ends_the_session():
    responder = ScsiResponder(_Image(1))
    ping = _request(iusb.KEEP_ALIVE)
    assert await responder.respond(ping) == ping
    assert await responder.respond(_request(iusb.KILL_REDIRECTION)) is None
    assert await responder.respond(_request(iusb.SESSION_DISCONNECT)) is None
