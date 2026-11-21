"""帧的编码、解码与完整收帧（异步版给服务端，同步版给客户端）。

TCP 是字节流，没有"消息边界"。这里用【长度前缀】解决粘包/拆包：
先读固定 16 字节报头 -> 校验 -> 按报头里的长度精确读取 JSON 和载荷。
"""
import json
import socket
import struct
from dataclasses import dataclass, field

from .constants import (
    HEADER_FORMAT, HEADER_SIZE, MAGIC, VERSION, VALID_TYPES,
    MAX_JSON_SIZE, MAX_PAYLOAD_SIZE, Flags,
)


class ProtocolError(Exception):
    """收到非法帧（魔数错、长度超限、JSON 损坏等）。服务端应断开该连接。"""


@dataclass
class Packet:
    type: int
    req_id: int = 0
    meta: dict = field(default_factory=dict)
    payload: bytes = b""
    flags: int = Flags.NONE

    def encode(self) -> bytes:
        body = json.dumps(self.meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_JSON_SIZE:
            raise ProtocolError("JSON 元数据过大")
        if len(self.payload) > MAX_PAYLOAD_SIZE:
            raise ProtocolError("载荷过大")
        header = struct.pack(
            HEADER_FORMAT, MAGIC, VERSION, self.type, self.flags,
            self.req_id & 0xFFFFFFFF, len(body), len(self.payload),
        )
        return header + body + self.payload


def parse_header(raw: bytes, max_json: int = MAX_JSON_SIZE,
                 max_payload: int = MAX_PAYLOAD_SIZE):
    """解析并校验 16 字节报头，返回 (type, flags, req_id, json_len, payload_len)。
    在分配任何内存之前先校验长度，防止恶意超大长度字段。"""
    magic, ver, ptype, flags, req_id, jlen, plen = struct.unpack(HEADER_FORMAT, raw)
    if magic != MAGIC:
        raise ProtocolError("魔数错误，非本协议数据")
    if ver != VERSION:
        raise ProtocolError(f"不支持的协议版本 {ver}")
    if ptype not in VALID_TYPES:
        raise ProtocolError(f"未知包类型 {ptype:#x}")
    if jlen > max_json:
        raise ProtocolError("JSON 长度超限")
    if plen > max_payload:
        raise ProtocolError("载荷长度超限")
    return ptype, flags, req_id, jlen, plen


def _build(ptype, flags, req_id, body: bytes, payload: bytes) -> Packet:
    try:
        meta = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProtocolError(f"JSON 损坏: {e}") from None
    if not isinstance(meta, dict):
        raise ProtocolError("JSON 元数据必须是对象")
    return Packet(type=ptype, req_id=req_id, meta=meta, payload=payload, flags=flags)


# ---------------- 异步（服务端，asyncio.StreamReader） ----------------
async def read_packet(reader, max_json: int = MAX_JSON_SIZE,
                      max_payload: int = MAX_PAYLOAD_SIZE) -> Packet:
    """读取一个完整帧。对端正常关闭时抛 asyncio.IncompleteReadError。"""
    raw = await reader.readexactly(HEADER_SIZE)
    ptype, flags, req_id, jlen, plen = parse_header(raw, max_json, max_payload)
    body = await reader.readexactly(jlen) if jlen else b""
    payload = await reader.readexactly(plen) if plen else b""
    return _build(ptype, flags, req_id, body, payload)


async def write_packet(writer, pkt: Packet) -> None:
    writer.write(pkt.encode())
    await writer.drain()


# ---------------- 同步（客户端，socket / SSLSocket） ----------------
def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接已关闭")
        buf.extend(chunk)
    return bytes(buf)


def recv_packet(sock, max_json: int = MAX_JSON_SIZE,
                max_payload: int = MAX_PAYLOAD_SIZE) -> Packet:
    raw = recv_exact(sock, HEADER_SIZE)
    ptype, flags, req_id, jlen, plen = parse_header(raw, max_json, max_payload)
    body = recv_exact(sock, jlen) if jlen else b""
    payload = recv_exact(sock, plen) if plen else b""
    return _build(ptype, flags, req_id, body, payload)


def send_packet(sock, pkt: Packet) -> None:
    sock.sendall(pkt.encode())
