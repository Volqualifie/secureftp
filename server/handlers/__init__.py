"""包类型 -> 处理函数的分发表。"""
from protocol.constants import PacketType

from .auth import handle_login, handle_register
from .membership import handle_upgrade
from .files import (
    handle_catalog, handle_delete, handle_download_req, handle_file_data, handle_file_done,
    handle_upload_req,
)

# 未登录也允许的包类型
PUBLIC_TYPES = {
    PacketType.REGISTER_REQ,
    PacketType.LOGIN_REQ,
    PacketType.PING,
}

# 属于已建立传输的数据流：连接已认证即可，不要求每一帧都带 token
STREAM_TYPES = {
    PacketType.FILE_DATA,
    PacketType.FILE_DONE,
}

HANDLERS = {
    PacketType.REGISTER_REQ: handle_register,
    PacketType.LOGIN_REQ: handle_login,
    PacketType.CATALOG_REQ: handle_catalog,
    PacketType.UPLOAD_REQ: handle_upload_req,
    PacketType.FILE_DATA: handle_file_data,
    PacketType.FILE_DONE: handle_file_done,
    PacketType.DOWNLOAD_REQ: handle_download_req,
    PacketType.UPGRADE_REQ: handle_upgrade,
    PacketType.DELETE_REQ: handle_delete,
}