"""上传 / 下载（含断点续传、自动重连后续传、取消）。

这些函数是阻塞的，应放在工作线程里调用（命令行 shell、GUI 都是这样用）。

  progress(done_bytes, total_bytes)  进度回调，在调用线程里触发
  cancel                             threading.Event，置位后尽快停止并保留半成品，可再次续传

上传取消：向服务端补发一个"数据不足的 LAST 帧"，服务端会保留 .part 并释放该传输。
下载取消：向服务端发 FILE_DONE{ok:false}，服务端停止推送。
"""
import hashlib
import json
import logging
import os
import time

from protocol.constants import ErrorCode, Flags, PacketType

from .errors import Cancelled, ConnectionLost, TransferError

log = logging.getLogger("client.transfer")

RESPONSE_TIMEOUT = 30.0         # 等待服务端响应（META / UPLOAD_RESP）
DATA_TIMEOUT = 60.0             # 下载时两块数据之间的最长等待
FINISH_TIMEOUT = 600.0          # 上传完成后等服务端校验哈希（大文件较慢）


def sha256_file(path: str, cancel=None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            if cancel is not None and cancel.is_set():
                raise Cancelled("已取消")
            h.update(block)
    return h.hexdigest()


def _notify(progress, done, total):
    if progress is not None:
        try:
            progress(done, total)
        except Exception:
            log.exception("progress 回调出错")


def _get(ch, cancel, timeout):
    """带取消检查的阻塞等待。"""
    end = time.monotonic() + timeout
    while True:
        if cancel is not None and cancel.is_set():
            raise Cancelled("已取消")
        try:
            return ch.get(timeout=min(0.25, max(0.01, end - time.monotonic())))
        except TimeoutError:
            if time.monotonic() >= end:
                raise


def _fail(resp, default="操作失败"):
    return TransferError(resp.meta.get("code"), resp.meta.get("message", default))


def _wait_reconnect(client, cancel, timeout=60.0):
    """等待自动重连完成；期间随时响应取消（否则"暂停"按钮在断网时会卡住最长 60 秒）。"""
    end = time.monotonic() + timeout
    while True:
        if cancel is not None and cancel.is_set():
            raise Cancelled("已取消")
        if client.wait_connected(timeout=0.25):
            return
        if client.state in ("auth_failed", "closed"):
            raise ConnectionLost("连接已关闭或认证失败")
        if time.monotonic() >= end:
            raise ConnectionLost("重连超时")


def _with_retries(client, once, retries, what, cancel=None):
    """断线（ConnectionLost）时等待自动重连后重试；上传冲突（旧连接的传输还没清理完）稍等重试。"""
    attempt = 0
    while True:
        try:
            return once()
        except ConnectionLost:
            attempt += 1
            log.warning("%s 中断（第 %d 次），等待重连后续传", what, attempt)
            if attempt > retries:
                raise
            _wait_reconnect(client, cancel)
        except TransferError as e:
            if e.code == ErrorCode.CONFLICT and attempt < retries and what == "上传":
                attempt += 1
                time.sleep(0.5)
                continue
            raise


# ================================================================ 上传
def upload_file(client, local_path, remote_path, progress=None, cancel=None,
                resume=True, retries=5):
    size = os.path.getsize(local_path)
    digest = sha256_file(local_path, cancel)            # 先算整文件哈希，服务端据此做完整性校验

    def once():
        return _upload_once(client, local_path, remote_path, size, digest,
                            progress, cancel, resume)
    return _with_retries(client, once, retries, "上传", cancel)


def _upload_once(client, local_path, remote_path, size, digest, progress, cancel, resume):
    ch = client.channel()
    try:
        ch.send(PacketType.UPLOAD_REQ,
                {"path": remote_path, "size": size, "sha256": digest},
                flags=Flags.RESUME if resume else Flags.NONE)
        resp = _get(ch, cancel, RESPONSE_TIMEOUT)
        if not resp.meta.get("ok"):
            raise _fail(resp, "上传被拒绝")
        offset = resp.meta["offset"]                    # 服务端已有多少字节，从这里续传
        chunk = int(resp.meta.get("chunk_size", 65536))
        done = offset
        _notify(progress, done, size)

        with open(local_path, "rb") as f:
            f.seek(offset)
            while True:
                if cancel is not None and cancel.is_set():
                    # 发一个数据不足的 LAST 帧：服务端保留 .part 并释放这个传输
                    ch.send(PacketType.FILE_DATA, {}, b"", Flags.LAST)
                    try:
                        ch.get(timeout=5)
                    except (TimeoutError, ConnectionLost):
                        pass
                    raise Cancelled("已取消，已上传部分保留在服务端，可续传")
                data = f.read(chunk)
                done += len(data)
                last = (not data) or done >= size
                ch.send(PacketType.FILE_DATA, {}, data, Flags.LAST if last else Flags.NONE)
                _notify(progress, min(done, size), size)
                early = ch.poll()                       # 服务端提前报错（如超出大小）就别再发了
                if early is not None:
                    if early.type == PacketType.FILE_DONE and not early.meta.get("ok"):
                        raise _fail(early)
                if last:
                    break

        resp = _get(ch, cancel, FINISH_TIMEOUT)
        if resp.type != PacketType.FILE_DONE or not resp.meta.get("ok"):
            raise _fail(resp, "服务端校验失败")
        return {"size": size, "sha256": resp.meta.get("sha256", digest), "resumed_from": offset}
    finally:
        ch.close()


# ================================================================ 下载
def _remove_quiet(*paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def download_file(client, remote_path, local_path, progress=None, cancel=None,
                  resume=True, retries=5):
    def once():
        return _download_once(client, remote_path, local_path, progress, cancel, resume)
    return _with_retries(client, once, retries, "下载", cancel)


def _download_once(client, remote_path, local_path, progress, cancel, resume):
    part, side = local_path + ".part", local_path + ".part.json"
    os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)

    # 本地有半成品且记录完整，就带着"版本指纹"请求续传
    offset, expect = 0, None
    if resume and os.path.isfile(part) and os.path.isfile(side):
        try:
            with open(side, encoding="utf-8") as f:
                m = json.load(f)
            psize = os.path.getsize(part)
            if 0 < psize <= m["size"]:
                offset, expect = psize, m["sha256"]
        except (OSError, ValueError, KeyError):
            offset, expect = 0, None

    for _ in range(2):          # 第二轮：远程文件已变化，丢弃半成品从头下载
        ch = client.channel()
        try:
            meta = {"path": remote_path, "offset": offset}
            if expect and offset > 0:
                meta["expect_sha256"] = expect
            ch.send(PacketType.DOWNLOAD_REQ, meta)
            resp = _get(ch, cancel, RESPONSE_TIMEOUT)
            if not resp.meta.get("ok"):
                if resp.meta.get("code") == ErrorCode.CONFLICT and offset > 0:
                    log.info("远程文件已变化，放弃续传，从头下载")
                    _remove_quiet(part, side)
                    offset, expect = 0, None
                    continue
                raise _fail(resp, "下载被拒绝")
            return _receive(ch, resp, part, side, local_path, offset, progress, cancel)
        finally:
            ch.close()
    raise TransferError(ErrorCode.INTERNAL, "下载失败")


def _receive(ch, resp, part, side, local_path, offset, progress, cancel):
    size, digest = resp.meta["size"], resp.meta["sha256"]
    if resp.meta.get("offset") != offset:
        raise TransferError(ErrorCode.INTERNAL, "服务端返回的 offset 与请求不一致")
    with open(side, "w", encoding="utf-8") as f:
        json.dump({"size": size, "sha256": digest}, f)
    if not os.path.exists(part):
        open(part, "ab").close()

    done = offset
    _notify(progress, done, size)
    with open(part, "r+b") as fh:
        fh.seek(offset)
        fh.truncate()
        while True:
            try:
                pkt = _get(ch, cancel, DATA_TIMEOUT)
            except Cancelled:
                ch.send(PacketType.FILE_DONE, {"ok": False, "cancel": True})
                raise
            if pkt.type != PacketType.FILE_DATA:
                raise _fail(pkt, "下载中断")
            fh.write(pkt.payload)
            done += len(pkt.payload)
            _notify(progress, done, size)
            if pkt.flags & Flags.LAST:
                break

    if done != size:
        raise TransferError(ErrorCode.INTERNAL, f"收到 {done} 字节，应为 {size} 字节")
    if sha256_file(part) != digest:
        _remove_quiet(part, side)
        ch.send(PacketType.FILE_DONE, {"ok": False})
        raise TransferError(ErrorCode.HASH_MISMATCH, "SHA-256 校验失败，已丢弃")
    os.replace(part, local_path)
    _remove_quiet(side)
    ch.send(PacketType.FILE_DONE, {"ok": True})             # 完成确认
    return {"size": size, "sha256": digest, "resumed_from": offset}


# ================================================================ 目录递归
def list_remote_tree(client, root: str, limit: int = 20000):
    """递归列出远程目录下的所有文件，返回 [(远程完整路径, 相对 root 的路径, 大小)]。"""
    import posixpath
    out, stack = [], [(root, "")]
    while stack:
        path, rel = stack.pop()
        for e in client.catalog(path):
            r = posixpath.join(path, e["name"])
            rr = posixpath.join(rel, e["name"]) if rel else e["name"]
            if e["is_dir"]:
                stack.append((r, rr))
            else:
                out.append((r, rr, e["size"]))
                if len(out) > limit:
                    raise TransferError(ErrorCode.TOO_LARGE, f"文件数超过 {limit}，请分批下载")
    return out
