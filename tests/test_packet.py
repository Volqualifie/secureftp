import asyncio
import socket
import struct

import pytest

from protocol.constants import (
    HEADER_FORMAT, HEADER_SIZE, MAGIC, VERSION, MAX_JSON_SIZE, PacketType, Flags,
)
from protocol.packet import (
    Packet, ProtocolError, read_packet, recv_packet, send_packet,
)


def feed(data: bytes, step: int = None) -> asyncio.StreamReader:
    """必须在事件循环内调用（Python 3.12+ 不允许在循环外创建 StreamReader）。"""
    r = asyncio.StreamReader()
    if step:                      # 模拟拆包：每次只到达 step 字节
        for i in range(0, len(data), step):
            r.feed_data(data[i:i + step])
    else:
        r.feed_data(data)
    r.feed_eof()
    return r


def read_n(data: bytes, n: int = 1, step: int = None):
    """在新事件循环里读取 n 个帧，返回列表。"""
    async def run():
        r = feed(data, step)
        return [await read_packet(r) for _ in range(n)]
    return asyncio.run(run())


def test_header_size():
    assert HEADER_SIZE == 16


def test_roundtrip():
    pkt = Packet(PacketType.FILE_DATA, req_id=7, meta={"name": "文件.txt", "n": 1},
                 payload=b"\x00\x01\x02" * 100, flags=Flags.LAST)
    out = read_n(pkt.encode())[0]
    assert (out.type, out.req_id, out.meta, out.payload, out.flags) == \
           (pkt.type, 7, pkt.meta, pkt.payload, Flags.LAST)


def test_empty_meta_and_payload():
    out = read_n(Packet(PacketType.PING).encode())[0]
    assert out.type == PacketType.PING and out.meta == {} and out.payload == b""


def test_fragmented_one_byte_at_a_time():
    """拆包：1 字节 1 字节到达，仍能正确组帧。"""
    pkt = Packet(PacketType.LOGIN_REQ, 1, {"username": "a", "password": "b"})
    out = read_n(pkt.encode(), step=1)[0]
    assert out.meta == pkt.meta


def test_sticky_packets():
    """粘包：两帧连在一起，应能依次读出。"""
    a = Packet(PacketType.PING, 1)
    b = Packet(PacketType.CATALOG_REQ, 2, {"path": "/"})
    x, y = read_n(a.encode() + b.encode(), n=2)
    assert x.req_id == 1 and y.req_id == 2 and y.meta["path"] == "/"


def test_bad_magic():
    raw = struct.pack(HEADER_FORMAT, 0x00, VERSION, PacketType.PING, 0, 0, 0, 0)
    with pytest.raises(ProtocolError):
        read_n(raw)[0]


def test_unknown_type():
    raw = struct.pack(HEADER_FORMAT, MAGIC, VERSION, 0x7F, 0, 0, 0, 0)
    with pytest.raises(ProtocolError):
        read_n(raw)[0]


def test_oversize_length_rejected_before_alloc():
    """恶意超大长度字段（4GB）必须在读取前就被拒绝。"""
    raw = struct.pack(HEADER_FORMAT, MAGIC, VERSION, PacketType.FILE_DATA,
                      0, 0, 0, 0xFFFFFFFF)
    with pytest.raises(ProtocolError):
        read_n(raw)[0]
    raw = struct.pack(HEADER_FORMAT, MAGIC, VERSION, PacketType.LOGIN_REQ,
                      0, 0, MAX_JSON_SIZE + 1, 0)
    with pytest.raises(ProtocolError):
        read_n(raw)[0]


def test_corrupt_json():
    body = b"{not json"
    raw = struct.pack(HEADER_FORMAT, MAGIC, VERSION, PacketType.LOGIN_REQ,
                      0, 0, len(body), 0) + body
    with pytest.raises(ProtocolError):
        read_n(raw)[0]


def test_json_must_be_object():
    body = b"[1,2]"
    raw = struct.pack(HEADER_FORMAT, MAGIC, VERSION, PacketType.LOGIN_REQ,
                      0, 0, len(body), 0) + body
    with pytest.raises(ProtocolError):
        read_n(raw)[0]


def test_truncated_frame():
    raw = Packet(PacketType.FILE_DATA, 1, {}, b"x" * 100).encode()[:-10]
    with pytest.raises(asyncio.IncompleteReadError):
        read_n(raw)[0]


def test_sync_socket_roundtrip():
    a, b = socket.socketpair()
    try:
        pkt = Packet(PacketType.FILE_DATA, 9, {"k": "v"}, b"z" * 50000)
        send_packet(a, pkt)
        out = recv_packet(b)
        assert out.req_id == 9 and out.payload == pkt.payload
    finally:
        a.close(); b.close()