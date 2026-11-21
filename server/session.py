"""一个客户端连接对应一个 Session：长连接，循环收帧、分发，直到对端断开或超时。

超时规则：
  - 未登录：从连接建立起 auth_timeout 秒内必须登录成功（绝对期限，发 PING 也不能续命）
  - 已登录：idle_timeout 秒内收不到任何帧则断开（客户端每 heartbeat_interval 秒发 PING）
  - 服务端正在向客户端推送下载数据时，不算空闲
"""
import asyncio
import hmac
import logging
import time

from protocol.constants import ErrorCode, PacketType
from protocol.packet import Packet, ProtocolError, read_packet, write_packet

from .handlers import HANDLERS, PUBLIC_TYPES, STREAM_TYPES

log = logging.getLogger("server.session")


class Session:
    def __init__(self, ctx, reader, writer, ip: str):
        self.ctx = ctx
        self.reader = reader
        self.writer = writer
        self.ip = ip
        self.user = None            # 登录后为用户记录 dict
        self.token = None
        self.uploads = {}           # req_id -> files.Upload
        self.downloads = {}         # req_id -> asyncio.Task
        self._write_lock = asyncio.Lock()   # 并发传输时保证帧不交错

    async def send(self, pkt: Packet) -> None:
        async with self._write_lock:
            await write_packet(self.writer, pkt)

    async def send_error(self, req_id: int, code: int, message: str) -> None:
        await self.send(Packet(PacketType.ERROR, req_id,
                               {"ok": False, "code": code, "message": message}))

    def _read_timeout(self, auth_deadline: float) -> float:
        if self.user is None:
            return max(0.1, auth_deadline - time.monotonic())
        return self.ctx.cfg.idle_timeout

    async def _read_frame(self, auth_deadline: float) -> Packet:
        """读一个完整帧。不能用 wait_for 直接包 read_packet 后重试：
        超时取消会丢掉已读走的报头，造成帧错位。这里让读取任务持续运行，只是等待它。"""
        task = asyncio.ensure_future(read_packet(self.reader, self.ctx.cfg.max_frame_size))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=self._read_timeout(auth_deadline))
                if done:
                    return task.result()
                if self.user is not None and self.downloads:
                    continue                    # 正在推送下载数据，不算空闲
                raise asyncio.TimeoutError
        finally:
            if not task.done():
                task.cancel()

    async def run(self) -> None:
        auth_deadline = time.monotonic() + self.ctx.cfg.auth_timeout
        try:
            while True:
                try:
                    pkt = await self._read_frame(auth_deadline)
                except asyncio.TimeoutError:
                    log.info("超时断开: %s (%s)", self.ip,
                             "未登录" if self.user is None else "空闲")
                    return
                except ProtocolError as e:
                    log.warning("非法帧，断开 %s: %s", self.ip, e)
                    try:
                        await self.send_error(0, ErrorCode.BAD_REQUEST, "协议错误")
                    except Exception:
                        pass
                    return
                except (asyncio.IncompleteReadError, ConnectionError):
                    return          # 对端关闭 / 重置

                if not await self._dispatch(pkt):
                    return
        finally:
            await self._cleanup()

    async def _cleanup(self) -> None:
        # 取消进行中的下载；上传的 .part 保留，供断点续传
        tasks = list(self.downloads.values())
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        from .handlers.files import release_upload
        for up in list(self.uploads.values()):
            release_upload(self, up, keep_part=True)
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass

    def _authorized(self, pkt: Packet) -> bool:
        if self.user is None or self.token is None:
            return False
        if pkt.type in STREAM_TYPES:
            return True
        tok = pkt.meta.get("token")
        return isinstance(tok, str) and hmac.compare_digest(tok, self.token)

    async def _dispatch(self, pkt: Packet) -> bool:
        """返回 False 表示应关闭连接。"""
        t = pkt.type

        if t == PacketType.PING:
            await self.send(Packet(PacketType.PONG, pkt.req_id, {}))
            return True
        if t == PacketType.LOGOUT:
            return False

        if t not in PUBLIC_TYPES and not self._authorized(pkt):
            await self.send_error(pkt.req_id, ErrorCode.UNAUTHORIZED, "未登录或 token 无效")
            return True

        handler = HANDLERS.get(t)
        if handler is None:
            await self.send_error(pkt.req_id, ErrorCode.BAD_REQUEST, "不支持的请求类型")
            return True
        try:
            await handler(self, pkt)
        except (ConnectionError, asyncio.CancelledError):
            raise
        except Exception:
            # 业务异常不能把整个服务带崩，更不能把内部细节返回给客户端
            log.exception("处理 %s 时出错", PacketType.NAMES.get(t))
            await self.send_error(pkt.req_id, ErrorCode.INTERNAL, "服务器内部错误")
        return True