"""开通会员（演示用：点击即开通，不接入真实支付）。

只修改"当前已登录用户自己"的身份，user_id 取自服务端会话而不是请求内容，
所以客户端无法替别人开通，也无法伪造身份。操作幂等：重复开通直接返回成功。
"""
import logging

from protocol.constants import ErrorCode, PacketType
from protocol.packet import Packet

log = logging.getLogger("server.membership")


async def handle_upgrade(session, pkt: Packet) -> None:
    ctx, user = session.ctx, session.user
    R = PacketType.UPGRADE_RESP
    already = bool(user.get("is_vip")) or user["id"] in ctx.vip_ids
    if not already:
        try:
            await ctx.db.set_vip(user["id"])
        except Exception:
            log.exception("开通会员失败 user=%s", user["username"])
            await session.send(Packet(R, pkt.req_id, {
                "ok": False, "code": ErrorCode.INTERNAL, "message": "服务器暂时无法开通，请稍后再试"}))
            return
        ctx.vip_ids.add(user["id"])
        log.info("用户开通会员: %s", user["username"])
    session.user = {**user, "is_vip": 1}
    await session.send(Packet(R, pkt.req_id, {
        "ok": True, "is_vip": True, "already": already, "rate_limit": ctx.rate_for(session.user)}))