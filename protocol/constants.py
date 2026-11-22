"""协议常量：报头格式、包类型、标志位、错误码、限制。

报头 (16 字节，网络字节序)：
  0        1        2        3        4                8
  +--------+--------+--------+--------+----------------+
  | Magic  | Version|  Type  | Flags  |     ReqID      |
  +--------+--------+--------+--------+----------------+
  |      JSON 长度 (4B)       |      载荷长度 (4B)      |
  +---------------------------+------------------------+
一个完整帧 = 报头 || JSON 元数据 || 载荷
"""
import struct

MAGIC = 0xA5
VERSION = 1

HEADER_FORMAT = "!BBBBIII"          # Magic, Version, Type, Flags, ReqID, JsonLen, PayloadLen
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
assert HEADER_SIZE == 16

# ---- 限制（防止恶意超大长度字段导致内存耗尽）----
MAX_JSON_SIZE = 1 * 1024 * 1024         # 元数据最大 1 MiB
MAX_PAYLOAD_SIZE = 1 * 1024 * 1024      # 单帧载荷最大 1 MiB（文件按 64 KiB 分块）
CHUNK_SIZE = 64 * 1024


class PacketType:
    REGISTER_REQ = 0x01
    REGISTER_RESP = 0x02
    LOGIN_REQ = 0x03
    LOGIN_RESP = 0x04
    CATALOG_REQ = 0x05      # 远程目录请求
    CATALOG_RESP = 0x06
    DOWNLOAD_REQ = 0x07     # 下载请求（可带 offset 断点续传）
    FILE_META = 0x08        # 文件元数据：name/size/sha256/offset
    FILE_DATA = 0x09        # 文件数据块
    FILE_DONE = 0x0A        # 接收完成确认（含校验结果）
    UPLOAD_REQ = 0x0B       # 上传请求：name/size/sha256
    UPLOAD_RESP = 0x0C      # 服务端回复：同意并给出已有 offset
    PING = 0x0E
    PONG = 0x0F
    ERROR = 0x10
    LOGOUT = 0x11
    UPGRADE_REQ = 0x12      # 开通会员（模拟，无真实支付）
    UPGRADE_RESP = 0x13
    DELETE_REQ = 0x14       # 删除自己目录下的文件/文件夹
    DELETE_RESP = 0x15

    NAMES = {}


PacketType.NAMES = {
    v: k for k, v in vars(PacketType).items()
    if k.isupper() and isinstance(v, int)
}
VALID_TYPES = set(PacketType.NAMES)


class Flags:
    NONE = 0x00
    LAST = 0x01     # 本帧是该传输的最后一块
    ACK = 0x02      # 本帧是确认
    RESUME = 0x04   # 断点续传请求


class ErrorCode:
    OK = 0
    BAD_REQUEST = 400
    UNAUTHORIZED = 401
    FORBIDDEN = 403         # 路径穿越等
    NOT_FOUND = 404
    CONFLICT = 409          # 用户名已存在
    TOO_LARGE = 413
    RATE_LIMITED = 429
    HASH_MISMATCH = 460
    INTERNAL = 500