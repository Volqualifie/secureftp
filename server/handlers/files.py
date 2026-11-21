"""目录浏览、上传、下载、断点续传。

协议流程
  目录   : CATALOG_REQ{path}                    -> CATALOG_RESP{entries}
  上传   : UPLOAD_REQ{path,size,sha256} [+RESUME] -> UPLOAD_RESP{offset}
           FILE_DATA × n (最后一块带 LAST 标志，可为空载荷)
                                                  -> FILE_DONE{ok,sha256}
  下载   : DOWNLOAD_REQ{path,offset}             -> FILE_META{size,sha256,offset}
           服务端连续推送 FILE_DATA（最后一块带 LAST）；客户端校验后可回 FILE_DONE
同一连接上可并发多个传输，用 ReqID 区分；每个传输占用一个 ReqID，直到结束。
FILE_DATA / FILE_DONE 属于已建立传输的数据流，不要求每帧都带 token（连接已认证）。
"""
import asyncio
import logging
import os
import re
from dataclasses import dataclass

from protocol.constants import ErrorCode, Flags, PacketType
from protocol.packet import Packet

from .. import storage
from ..security import ValidationError, safe_join, validate_rel_path
from .common import fail

log = logging.getLogger("server.files")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class Upload:
    req_id: int
    target: str
    name: str
    size: int
    sha256: str
    received: int
    fh: object


# ---------------------------------------------------------------- 公共工具
def _resolve(session, rel, allow_empty=False) -> str:
    """把客户端给的相对路径解析为服务端真实路径，限定在该用户自己的目录内。"""
    rel = validate_rel_path(rel, allow_empty=allow_empty)
    username = session.user["username"]
    storage.ensure_user_dir(session.ctx.cfg.storage_root, username)
    return safe_join(session.ctx.cfg.storage_root, username, rel)


def _busy(session) -> bool:
    return len(session.uploads) + len(session.downloads) >= \
        session.ctx.cfg.max_concurrent_transfers


def _req_in_use(session, req_id) -> bool:
    return req_id in session.uploads or req_id in session.downloads


async def _file_sha256(ctx, path: str) -> str:
    st = await asyncio.to_thread(os.stat, path)
    key = (path, st.st_mtime_ns, st.st_size)
    digest = ctx.hash_cache.get(key)
    if digest is None:
        digest = await asyncio.to_thread(storage.sha256_file, path)
        if len(ctx.hash_cache) > 1000:
            ctx.hash_cache.clear()
        ctx.hash_cache[key] = digest
    return digest


async def _log_transfer(session, action, name, size, sha256, status):
    try:
        await session.ctx.db.log_transfer(
            session.user["id"], action, name[:255], size, sha256, status)
    except Exception:
        log.warning("写传输日志失败", exc_info=True)


# ---------------------------------------------------------------- 目录
async def handle_catalog(session, pkt: Packet) -> None:
    R = PacketType.CATALOG_RESP
    try:
        target = _resolve(session, pkt.meta.get("path", ""), allow_empty=True)
        rel = validate_rel_path(pkt.meta.get("path", ""), allow_empty=True)
    except ValidationError as e:
        await session.send(fail(R, pkt.req_id, ErrorCode.FORBIDDEN, str(e)))
        return
    if not await asyncio.to_thread(os.path.isdir, target):
        await session.send(fail(R, pkt.req_id, ErrorCode.NOT_FOUND, "目录不存在"))
        return
    entries = await asyncio.to_thread(storage.list_dir, target)
    await session.send(Packet(R, pkt.req_id,
                              {"ok": True, "path": "/" + rel, "entries": entries}))


# ---------------------------------------------------------------- 上传
async def handle_upload_req(session, pkt: Packet) -> None:
    ctx, cfg, m = session.ctx, session.ctx.cfg, pkt.meta
    R = PacketType.UPLOAD_RESP

    size, sha = m.get("size"), m.get("sha256")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0 \
            or not isinstance(sha, str) or not SHA_RE.match(sha):
        await session.send(fail(R, pkt.req_id, ErrorCode.BAD_REQUEST, "size 或 sha256 不合法"))
        return
    if size > cfg.max_file_size:
        await session.send(fail(R, pkt.req_id, ErrorCode.TOO_LARGE,
                                f"文件超过大小上限 {cfg.max_file_size} 字节"))
        return
    try:
        target = _resolve(session, m.get("path"))
    except ValidationError as e:
        await session.send(fail(R, pkt.req_id, ErrorCode.FORBIDDEN, str(e)))
        return
    if _req_in_use(session, pkt.req_id):
        await session.send(fail(R, pkt.req_id, ErrorCode.BAD_REQUEST, "ReqID 正在使用"))
        return
    if _busy(session):
        await session.send(fail(R, pkt.req_id, ErrorCode.RATE_LIMITED, "并发传输数已达上限"))
        return
    if target in ctx.active_uploads:
        await session.send(fail(R, pkt.req_id, ErrorCode.CONFLICT, "该文件正在被上传"))
        return
    if await asyncio.to_thread(os.path.isdir, target):
        await session.send(fail(R, pkt.req_id, ErrorCode.CONFLICT, "存在同名目录"))
        return

    ctx.active_uploads.add(target)
    try:
        fh, offset = await asyncio.to_thread(
            storage.prepare_upload, target, size, sha, bool(pkt.flags & Flags.RESUME))
    except OSError:
        ctx.active_uploads.discard(target)
        log.exception("准备上传失败")
        await session.send(fail(R, pkt.req_id, ErrorCode.INTERNAL, "服务器无法创建文件"))
        return

    session.uploads[pkt.req_id] = Upload(
        pkt.req_id, target, os.path.basename(target), size, sha, offset, fh)
    await session.send(Packet(R, pkt.req_id,
                              {"ok": True, "offset": offset, "chunk_size": cfg.chunk_size}))


def release_upload(session, up: Upload, keep_part: bool = True) -> None:
    """结束一个上传：关文件、释放目标锁。keep_part=False 时同时删除临时文件。"""
    session.uploads.pop(up.req_id, None)
    session.ctx.active_uploads.discard(up.target)
    try:
        up.fh.close()
    except Exception:
        pass
    if not keep_part:
        storage.discard_upload(up.target)


async def _abort_upload(session, up: Upload, code, message, keep_part=True):
    release_upload(session, up, keep_part)
    await _log_transfer(session, "upload", up.name, up.size, up.sha256, "failed")
    await session.send(Packet(PacketType.FILE_DONE, up.req_id,
                              {"ok": False, "code": code, "message": message}))


async def handle_file_data(session, pkt: Packet) -> None:
    up = session.uploads.get(pkt.req_id)
    if up is None:
        await session.send_error(pkt.req_id, ErrorCode.BAD_REQUEST, "没有对应的上传任务")
        return
    n = len(pkt.payload)
    if up.received + n > up.size:
        await _abort_upload(session, up, ErrorCode.TOO_LARGE, "数据超出声明的文件大小",
                            keep_part=False)
        return
    if n:
        try:
            await asyncio.to_thread(up.fh.write, pkt.payload)
        except OSError:
            log.exception("写入失败")
            await _abort_upload(session, up, ErrorCode.INTERNAL, "服务器写入失败")
            return
        up.received += n

    if not (pkt.flags & Flags.LAST):
        return
    if up.received != up.size:
        # 保留 .part，客户端可以断点续传补齐
        await _abort_upload(session, up, ErrorCode.BAD_REQUEST, "数据长度与声明不符")
        return
    try:
        ok, digest = await asyncio.to_thread(
            storage.finalize_upload, up.fh, up.target, up.sha256)
    except OSError:
        log.exception("完成上传失败")
        release_upload(session, up, keep_part=False)
        await session.send(Packet(PacketType.FILE_DONE, up.req_id, {
            "ok": False, "code": ErrorCode.INTERNAL, "message": "服务器无法保存文件"}))
        return
    release_upload(session, up)
    if ok:
        await _log_transfer(session, "upload", up.name, up.size, digest, "ok")
        await session.send(Packet(PacketType.FILE_DONE, up.req_id,
                                  {"ok": True, "size": up.size, "sha256": digest},
                                  flags=Flags.ACK))
    else:
        await _log_transfer(session, "upload", up.name, up.size, digest, "hash_mismatch")
        await session.send(Packet(PacketType.FILE_DONE, up.req_id, {
            "ok": False, "code": ErrorCode.HASH_MISMATCH, "message": "SHA-256 校验失败，已丢弃"}))


async def handle_file_done(session, pkt: Packet) -> None:
    """客户端对下载的确认。仅记录，不回复。"""
    log.info("客户端确认 req=%s ok=%s", pkt.req_id, pkt.meta.get("ok"))


# ---------------------------------------------------------------- 下载
async def handle_download_req(session, pkt: Packet) -> None:
    ctx, cfg, m = session.ctx, session.ctx.cfg, pkt.meta
    R = PacketType.FILE_META
    try:
        target = _resolve(session, m.get("path"))
    except ValidationError as e:
        await session.send(fail(R, pkt.req_id, ErrorCode.FORBIDDEN, str(e)))
        return
    if _req_in_use(session, pkt.req_id):
        await session.send(fail(R, pkt.req_id, ErrorCode.BAD_REQUEST, "ReqID 正在使用"))
        return
    if _busy(session):
        await session.send(fail(R, pkt.req_id, ErrorCode.RATE_LIMITED, "并发传输数已达上限"))
        return
    if not await asyncio.to_thread(os.path.isfile, target):
        await session.send(fail(R, pkt.req_id, ErrorCode.NOT_FOUND, "文件不存在"))
        return

    size = await asyncio.to_thread(os.path.getsize, target)
    offset = m.get("offset", 0)
    if not isinstance(offset, int) or isinstance(offset, bool) or not (0 <= offset <= size):
        await session.send(fail(R, pkt.req_id, ErrorCode.BAD_REQUEST, "offset 不合法"))
        return
    digest = await _file_sha256(ctx, target)
    name = os.path.basename(target)

    # 先登记任务再发 META，避免 META 发出后客户端立刻重用 ReqID 的竞态
    task = asyncio.create_task(
        _stream_file(session, pkt.req_id, target, name, offset, size, digest))
    session.downloads[pkt.req_id] = task
    await session.send(Packet(R, pkt.req_id, {
        "ok": True, "name": name, "size": size, "sha256": digest,
        "offset": offset, "chunk_size": cfg.chunk_size}))


async def _stream_file(session, req_id, target, name, offset, size, digest):
    cfg = session.ctx.cfg
    try:
        fh = await asyncio.to_thread(open, target, "rb")
        try:
            await asyncio.to_thread(fh.seek, offset)
            sent = offset
            while True:
                chunk = await asyncio.to_thread(fh.read, cfg.chunk_size)
                sent += len(chunk)
                last = (not chunk) or sent >= size
                # drain 带超时：对端不收数据超过 idle_timeout 就放弃，避免任务永远挂起
                await asyncio.wait_for(
                    session.send(Packet(PacketType.FILE_DATA, req_id, {}, chunk,
                                        Flags.LAST if last else Flags.NONE)),
                    timeout=cfg.idle_timeout)
                if last:
                    break
        finally:
            fh.close()
        await _log_transfer(session, "download", name, size, digest, "sent")
    except asyncio.CancelledError:
        raise
    except (asyncio.TimeoutError, ConnectionError, OSError):
        log.warning("下载中断: req=%s %s", req_id, name)
        await _log_transfer(session, "download", name, size, digest, "failed")
        session.writer.close()
    except Exception:
        log.exception("下载出错")
        await _log_transfer(session, "download", name, size, digest, "failed")
        session.writer.close()
    finally:
        session.downloads.pop(req_id, None)