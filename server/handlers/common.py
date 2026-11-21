from protocol.packet import Packet


def fail(ptype: int, req_id: int, code: int, message: str) -> Packet:
    """统一的失败响应：{ok: false, code, message}。"""
    return Packet(ptype, req_id, {"ok": False, "code": code, "message": message})