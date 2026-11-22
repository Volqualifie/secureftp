"""注册与登录。

响应约定：*_RESP 的 meta 一定含 ok；失败时再带 code 和 message。
登录失败对"用户不存在"和"密码错误"返回完全相同的信息，避免用户名枚举。
"""
import logging

from protocol.constants import ErrorCode, PacketType
from protocol.packet import Packet

from ..db import UserExists
from ..security import (
    ValidationError, hash_password, new_token, validate_password,
    validate_username, verify_password,
)

log = logging.getLogger("server.auth")


def _fail(ptype, req_id, code, msg) -> Packet:
    return Packet(ptype, req_id, {"ok": False, "code": code, "message": msg})


async def handle_register(session, pkt: Packet) -> None:
    ctx, ip = session.ctx, session.ip
    if ctx.register_limiter.is_locked(ip):
        await session.send(_fail(PacketType.REGISTER_RESP, pkt.req_id,
                                 ErrorCode.RATE_LIMITED, "操作过于频繁，请稍后再试"))
        return
    ctx.register_limiter.record_failure(ip)      # 每次尝试都计数

    try:
        username = validate_username(pkt.meta.get("username"))
        password = validate_password(pkt.meta.get("password"))
    except ValidationError as e:
        await session.send(_fail(PacketType.REGISTER_RESP, pkt.req_id,
                                 ErrorCode.BAD_REQUEST, str(e)))
        return

    try:
        await ctx.db.create_user(username, await hash_password(password))
    except UserExists:
        await session.send(_fail(PacketType.REGISTER_RESP, pkt.req_id,
                                 ErrorCode.CONFLICT, "用户名已存在"))
        return
    log.info("新用户注册: %s (%s)", username, ip)
    await session.send(Packet(PacketType.REGISTER_RESP, pkt.req_id, {"ok": True}))


async def handle_login(session, pkt: Packet) -> None:
    ctx, ip = session.ctx, session.ip
    if ctx.login_limiter.is_locked(ip):
        await session.send(_fail(PacketType.LOGIN_RESP, pkt.req_id,
                                 ErrorCode.RATE_LIMITED, "失败次数过多，已被暂时锁定"))
        return

    generic = _fail(PacketType.LOGIN_RESP, pkt.req_id,
                    ErrorCode.UNAUTHORIZED, "用户名或密码错误")
    username, password = pkt.meta.get("username"), pkt.meta.get("password")
    if not isinstance(username, str) or not isinstance(password, str) \
            or len(password.encode()) > 1024:
        ctx.login_limiter.record_failure(ip)
        await session.send(generic)
        return

    # 先校验用户名格式再查库；格式不合法的输入（如 ' OR 1=1 --）根本不会进入 SQL。
    # 即便进入，db 层也是参数化查询。
    user = None
    if _valid_name(username):
        user = await ctx.db.get_user(username)

    ok = await verify_password(password[:1024], user["password_hash"] if user else None)
    if not ok:
        ctx.login_limiter.record_failure(ip)
        log.warning("登录失败: user=%r ip=%s", username[:32], ip)
        await session.send(generic)
        return

    ctx.login_limiter.record_success(ip)
    session.user = user
    session.token = new_token()
    log.info("登录成功: %s (%s)", user["username"], ip)
    await session.send(Packet(PacketType.LOGIN_RESP, pkt.req_id, {
        "ok": True,
        "token": session.token,
        "username": user["username"],
        "is_vip": bool(user["is_vip"]),
        "rate_limit": ctx.rate_for(user),        # 字节/秒，0 = 不限速
        "heartbeat_interval": ctx.cfg.heartbeat_interval,
    }))


def _valid_name(name: str) -> bool:
    try:
        validate_username(name)
        return True
    except ValidationError:
        return False