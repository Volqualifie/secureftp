"""文件传输测试：目录、上传、下载、断点续传、路径穿越、用户隔离、并发与限额。
复用 test_server_auth.py 里的证书夹具、假数据库和测试客户端。
运行：python -m pytest tests/test_files.py -v
"""
import asyncio
import hashlib
import os
import random

import pytest

from protocol.constants import ErrorCode, Flags, PacketType
from protocol.packet import Packet, read_packet, write_packet
from test_server_auth import Client, certs, login, register, run_server  # noqa: F401  (certs 是 fixture)

STEP = 65536


def rnd(n: int, seed: int = 1) -> bytes:
    return random.Random(seed).randbytes(n)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def run(certs, tmp_path, scenario, **over):
    return run_server(certs, scenario, storage_root=str(tmp_path), **over)


# ------------------------------------------------------------- 测试客户端工具
async def authed(port, certs, name="alice"):
    c = await Client.connect(port, certs)
    await register(c, name)
    c.token = (await login(c, name)).meta["token"]
    return c


async def relogin(port, certs, name="alice"):
    c = await Client.connect(port, certs)
    c.token = (await login(c, name)).meta["token"]
    return c


def next_id(c) -> int:
    c._id += 1
    return c._id


async def recv(c, timeout=5) -> Packet:
    return await asyncio.wait_for(read_packet(c.r), timeout)


async def catalog(c, path=""):
    return await c.call(PacketType.CATALOG_REQ, {"token": c.token, "path": path})


async def upload_begin(c, path, data, resume=False, sha_override=None):
    rid = next_id(c)
    meta = {"token": c.token, "path": path, "size": len(data),
            "sha256": sha_override or sha(data)}
    await write_packet(c.w, Packet(PacketType.UPLOAD_REQ, rid, meta,
                                   flags=Flags.RESUME if resume else Flags.NONE))
    return rid, await recv(c)


async def send_chunks(c, rid, data, start=0, step=STEP, last=True):
    """从 start 开始发送 data；last=False 时不带 LAST 标志（模拟传到一半断开）。"""
    pos = start
    while True:
        part = data[pos:pos + step]
        pos += len(part)
        is_last = last and pos >= len(data)
        await write_packet(c.w, Packet(PacketType.FILE_DATA, rid, {}, part,
                                       Flags.LAST if is_last else Flags.NONE))
        if pos >= len(data):
            break


async def upload(c, path, data, resume=False, sha_override=None, step=STEP):
    """完整上传。返回 (FILE_DONE 或失败的 UPLOAD_RESP, 服务端给出的 offset)。"""
    rid, resp = await upload_begin(c, path, data, resume, sha_override)
    if not resp.meta["ok"]:
        return resp, None
    off = resp.meta["offset"]
    await send_chunks(c, rid, data, off, step)
    return await recv(c), off


async def download(c, path, offset=0):
    rid = next_id(c)
    await write_packet(c.w, Packet(PacketType.DOWNLOAD_REQ, rid,
                                   {"token": c.token, "path": path, "offset": offset}))
    meta = await recv(c)
    if not meta.meta["ok"]:
        return meta, None
    buf = bytearray()
    while True:
        p = await recv(c)
        assert p.type == PacketType.FILE_DATA and p.req_id == rid
        buf += p.payload
        if p.flags & Flags.LAST:
            break
    return meta, bytes(buf)


# ------------------------------------------------------------- 基本功能
def test_upload_download_roundtrip(certs, tmp_path):
    data = rnd(200_123)

    async def sc(port, db):
        c = await authed(port, certs)
        done, off = await upload(c, "docs/report.bin", data)
        assert done.type == PacketType.FILE_DONE and done.meta["ok"] is True
        assert done.meta["sha256"] == sha(data) and off == 0
        assert (tmp_path / "alice" / "docs" / "report.bin").read_bytes() == data

        root = (await catalog(c)).meta
        assert [(e["name"], e["is_dir"]) for e in root["entries"]] == [("docs", True)]
        sub = (await catalog(c, "docs")).meta
        assert sub["entries"][0]["name"] == "report.bin"
        assert sub["entries"][0]["size"] == len(data)
        assert sorted(os.listdir(tmp_path / "alice" / "docs")) == ["report.bin"]   # 无 .part 残留

        meta, got = await download(c, "docs/report.bin")
        assert meta.meta["size"] == len(data) and meta.meta["sha256"] == sha(data)
        assert got == data
        await asyncio.sleep(0.2)
        assert [l[2:] for l in db.logs] == [
            ("report.bin", len(data), sha(data), "ok"),
            ("report.bin", len(data), sha(data), "sent")]
        await c.close()
    run(certs, tmp_path, sc)


def test_empty_file(certs, tmp_path):
    async def sc(port, db):
        c = await authed(port, certs)
        done, _ = await upload(c, "empty.txt", b"")
        assert done.meta["ok"] is True
        meta, got = await download(c, "empty.txt")
        assert meta.meta["size"] == 0 and got == b""
        await c.close()
    run(certs, tmp_path, sc)


def test_overwrite_existing(certs, tmp_path):
    async def sc(port, db):
        c = await authed(port, certs)
        await upload(c, "f.txt", b"version one")
        await upload(c, "f.txt", b"version two!")
        assert (await download(c, "f.txt"))[1] == b"version two!"
        await c.close()
    run(certs, tmp_path, sc)


def test_hash_mismatch_discards_file(certs, tmp_path):
    async def sc(port, db):
        c = await authed(port, certs)
        done, _ = await upload(c, "bad.bin", rnd(1000), sha_override=sha(b"something else"))
        assert done.meta["ok"] is False and done.meta["code"] == ErrorCode.HASH_MISMATCH
        assert os.listdir(tmp_path / "alice") == []          # 目标和 .part 都不存在
        assert (await catalog(c)).meta["entries"] == []
        assert db.logs[-1][-1] == "hash_mismatch"
        await c.close()
    run(certs, tmp_path, sc)


def test_catalog_errors(certs, tmp_path):
    async def sc(port, db):
        c = await authed(port, certs)
        await upload(c, "a.txt", b"x")
        assert (await catalog(c, "nope")).meta["code"] == ErrorCode.NOT_FOUND
        assert (await catalog(c, "a.txt")).meta["code"] == ErrorCode.NOT_FOUND   # 文件不是目录
        assert (await download(c, "nope.txt"))[0].meta["code"] == ErrorCode.NOT_FOUND
        await c.close()
    run(certs, tmp_path, sc)


def test_bad_upload_metadata(certs, tmp_path):
    async def sc(port, db):
        c = await authed(port, certs)
        good = sha(b"x")
        for size, digest in (("abc", good), (-1, good), (True, good), (1, good.upper()),
                             (1, "abc"), (1, None), (None, good)):
            rid = next_id(c)
            await write_packet(c.w, Packet(PacketType.UPLOAD_REQ, rid, {
                "token": c.token, "path": "f", "size": size, "sha256": digest}))
            r = await recv(c)
            assert r.meta["ok"] is False and r.meta["code"] == ErrorCode.BAD_REQUEST, (size, digest)
        await c.close()
    run(certs, tmp_path, sc)


def test_too_large_rejected(certs, tmp_path):
    async def sc(port, db):
        c = await authed(port, certs)
        _, resp = await upload_begin(c, "big.bin", rnd(2000))
        assert resp.meta["code"] == ErrorCode.TOO_LARGE
        await c.close()
    run(certs, tmp_path, sc, max_file_size=1000)


# ------------------------------------------------------------- 断点续传
def test_resume_upload_after_disconnect(certs, tmp_path):
    data = rnd(300_000)

    async def sc(port, db):
        c1 = await authed(port, certs)
        rid, resp = await upload_begin(c1, "big.bin", data)
        assert resp.meta["offset"] == 0
        await send_chunks(c1, rid, data[:2 * STEP], 0, last=False)     # 只传 2 块就"断网"
        await asyncio.sleep(0.3)
        assert os.path.getsize(tmp_path / "alice" / "big.bin.part") == 2 * STEP
        await c1.close()
        await asyncio.sleep(0.3)

        c2 = await relogin(port, certs)
        done, off = await upload(c2, "big.bin", data, resume=True)
        assert off == 2 * STEP                                         # 服务端告知从哪里续
        assert done.meta["ok"] is True
        assert (tmp_path / "alice" / "big.bin").read_bytes() == data
        assert sorted(os.listdir(tmp_path / "alice")) == ["big.bin"]
        await c2.close()
    run(certs, tmp_path, sc)


def test_resume_refused_when_content_differs(certs, tmp_path):
    a, b = rnd(200_000, 1), rnd(200_000, 2)

    async def sc(port, db):
        c1 = await authed(port, certs)
        rid, _ = await upload_begin(c1, "f.bin", a)
        await send_chunks(c1, rid, a[:STEP], 0, last=False)
        await asyncio.sleep(0.3)
        await c1.close()
        await asyncio.sleep(0.3)

        c2 = await relogin(port, certs)
        done, off = await upload(c2, "f.bin", b, resume=True)          # 内容不同，不能拼接
        assert off == 0 and done.meta["ok"] is True
        assert (tmp_path / "alice" / "f.bin").read_bytes() == b
        await c2.close()
    run(certs, tmp_path, sc)


def test_no_resume_flag_restarts_from_zero(certs, tmp_path):
    data = rnd(200_000)

    async def sc(port, db):
        c1 = await authed(port, certs)
        rid, _ = await upload_begin(c1, "f.bin", data)
        await send_chunks(c1, rid, data[:STEP], 0, last=False)
        await asyncio.sleep(0.3)
        await c1.close()
        await asyncio.sleep(0.3)

        c2 = await relogin(port, certs)
        done, off = await upload(c2, "f.bin", data, resume=False)
        assert off == 0 and done.meta["ok"] is True
        await c2.close()
    run(certs, tmp_path, sc)


def test_short_last_frame_keeps_part_for_resume(certs, tmp_path):
    data = rnd(100)

    async def sc(port, db):
        c = await authed(port, certs)
        rid, _ = await upload_begin(c, "f.bin", data)
        await send_chunks(c, rid, data[:50], 0, last=True)             # 声明 100 字节却只发 50 就 LAST
        done = await recv(c)
        assert done.meta["ok"] is False and done.meta["code"] == ErrorCode.BAD_REQUEST
        assert os.path.getsize(tmp_path / "alice" / "f.bin.part") == 50
        done, off = await upload(c, "f.bin", data, resume=True)
        assert off == 50 and done.meta["ok"] is True
        await c.close()
    run(certs, tmp_path, sc)


def test_download_resume_offset(certs, tmp_path):
    data = rnd(150_000)

    async def sc(port, db):
        c = await authed(port, certs)
        await upload(c, "f.bin", data)
        meta, got = await download(c, "f.bin", offset=70_000)
        assert meta.meta["offset"] == 70_000 and got == data[70_000:]
        meta, got = await download(c, "f.bin", offset=len(data))       # 已经下完
        assert meta.meta["ok"] is True and got == b""
        for bad in (len(data) + 1, -1, "x", True):
            meta, _ = await download(c, "f.bin", offset=bad)
            assert meta.meta["code"] == ErrorCode.BAD_REQUEST, bad
        await c.close()
    run(certs, tmp_path, sc)


# ------------------------------------------------------------- 安全
def test_path_traversal_blocked(certs, tmp_path):
    async def sc(port, db):
        c = await authed(port, certs)
        for evil in ("../evil.txt", "../../x", "a/../../x", "/../x", "..\\x", "a/../b"):
            done, _ = await upload(c, evil, b"pwn")
            assert done.meta["code"] == ErrorCode.FORBIDDEN, evil
            assert (await catalog(c, evil)).meta["code"] == ErrorCode.FORBIDDEN, evil
            assert (await download(c, evil))[0].meta["code"] == ErrorCode.FORBIDDEN, evil
        assert not (tmp_path / "evil.txt").exists() and not (tmp_path.parent / "evil.txt").exists()
        assert os.listdir(tmp_path) in ([], ["alice"])        # 非法路径在校验阶段就被拒绝，可能连用户目录都没创建
        assert not (tmp_path / "alice").exists() or os.listdir(tmp_path / "alice") == []
        await c.close()
    run(certs, tmp_path, sc)


def test_users_are_isolated(certs, tmp_path):
    async def sc(port, db):
        alice = await authed(port, certs, "alice")
        await upload(alice, "secret.txt", b"alice only")
        bob = await authed(port, certs, "bobby")
        assert (await download(bob, "../alice/secret.txt"))[0].meta["code"] == ErrorCode.FORBIDDEN
        assert (await download(bob, "secret.txt"))[0].meta["code"] == ErrorCode.NOT_FOUND
        assert (await catalog(bob)).meta["entries"] == []
        assert (await catalog(bob, "../")).meta["code"] == ErrorCode.FORBIDDEN
        await alice.close(); await bob.close()
    run(certs, tmp_path, sc)


def test_symlink_escape_blocked(certs, tmp_path):
    outside = tmp_path.parent / "outside_secret"
    outside.mkdir(exist_ok=True)
    (outside / "passwd.txt").write_text("top secret")

    async def sc(port, db):
        c = await authed(port, certs)
        await catalog(c)                                   # 触发创建用户目录
        try:
            os.symlink(outside / "passwd.txt", tmp_path / "alice" / "link.txt")
            os.symlink(outside, tmp_path / "alice" / "linkdir", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("当前系统不允许创建符号链接（Windows 需管理员或开发者模式）")
        assert (await download(c, "link.txt"))[0].meta["code"] == ErrorCode.FORBIDDEN
        assert (await catalog(c, "linkdir")).meta["code"] == ErrorCode.FORBIDDEN
        done, _ = await upload(c, "linkdir/new.txt", b"x")
        assert done.meta["code"] == ErrorCode.FORBIDDEN
        assert (await catalog(c)).meta["entries"] == []    # 符号链接不在列表里显示
        assert not (outside / "new.txt").exists()
        await c.close()
    run(certs, tmp_path, sc)


def test_internal_and_reserved_names_rejected(certs, tmp_path):
    async def sc(port, db):
        c = await authed(port, certs)
        for name in ("a.part", "x.PART.json", "CON", "nul.txt", "bad:name", "q?.txt",
                     "trail.", "sp ace ", "d/aux", "x" * 201):
            done, _ = await upload(c, name, b"x")
            assert done.meta["code"] == ErrorCode.FORBIDDEN, name
        assert not (tmp_path / "alice").exists() or os.listdir(tmp_path / "alice") == []
        await c.close()
    run(certs, tmp_path, sc)


def test_part_files_hidden_from_catalog(certs, tmp_path):
    data = rnd(200_000)

    async def sc(port, db):
        c = await authed(port, certs)
        rid, _ = await upload_begin(c, "f.bin", data)
        await send_chunks(c, rid, data[:STEP], 0, last=False)
        await asyncio.sleep(0.2)
        assert sorted(os.listdir(tmp_path / "alice")) == ["f.bin.part", "f.bin.part.json"]
        assert (await catalog(c)).meta["entries"] == []     # 临时文件对用户不可见
        assert (await download(c, "f.bin.part"))[0].meta["code"] == ErrorCode.FORBIDDEN
        await send_chunks(c, rid, data, STEP)
        assert (await recv(c)).meta["ok"] is True
        await c.close()
    run(certs, tmp_path, sc)


def test_overflow_aborts_upload_but_keeps_connection(certs, tmp_path):
    async def sc(port, db):
        c = await authed(port, certs)
        rid, _ = await upload_begin(c, "f.bin", rnd(100))
        await write_packet(c.w, Packet(PacketType.FILE_DATA, rid, {}, rnd(200), Flags.NONE))
        done = await recv(c)
        assert done.meta["ok"] is False and done.meta["code"] == ErrorCode.TOO_LARGE
        assert os.listdir(tmp_path / "alice") == []         # 恶意数据不留痕
        assert (await c.call(PacketType.PING)).type == PacketType.PONG
        await c.close()
    run(certs, tmp_path, sc)


def test_file_data_requires_auth_and_active_upload(certs, tmp_path):
    async def sc(port, db):
        anon = await Client.connect(port, certs)
        await write_packet(anon.w, Packet(PacketType.FILE_DATA, 1, {}, b"x", Flags.LAST))
        r = await recv(anon)
        assert r.type == PacketType.ERROR and r.meta["code"] == ErrorCode.UNAUTHORIZED
        await anon.close()

        c = await authed(port, certs)
        await write_packet(c.w, Packet(PacketType.FILE_DATA, 99, {}, b"x", Flags.LAST))
        r = await recv(c)
        assert r.type == PacketType.ERROR and r.meta["code"] == ErrorCode.BAD_REQUEST
        await c.close()
    run(certs, tmp_path, sc)


def test_upload_and_download_need_valid_token(certs, tmp_path):
    async def sc(port, db):
        c = await authed(port, certs)
        for ptype, meta in (
                (PacketType.UPLOAD_REQ, {"path": "f", "size": 1, "sha256": sha(b"x")}),
                (PacketType.DOWNLOAD_REQ, {"path": "f"}),
                (PacketType.CATALOG_REQ, {"path": ""})):
            r = await c.call(ptype, dict(meta, token="forged"))
            assert r.type == PacketType.ERROR and r.meta["code"] == ErrorCode.UNAUTHORIZED
            r = await c.call(ptype, meta)                    # 完全不带 token
            assert r.type == PacketType.ERROR and r.meta["code"] == ErrorCode.UNAUTHORIZED
        await c.close()
    run(certs, tmp_path, sc)


# ------------------------------------------------------------- 并发
def test_concurrent_transfer_limit_per_connection(certs, tmp_path):
    async def sc(port, db):
        c = await authed(port, certs)
        data = rnd(1000)
        rid, resp = await upload_begin(c, "a.bin", data)
        assert resp.meta["ok"] is True
        _, resp = await upload_begin(c, "b.bin", data)
        assert resp.meta["code"] == ErrorCode.RATE_LIMITED
        meta, _ = await download(c, "a.bin")
        assert meta.meta["code"] == ErrorCode.RATE_LIMITED
        await send_chunks(c, rid, data)                       # 完成第一个后名额释放
        assert (await recv(c)).meta["ok"] is True
        done, _ = await upload(c, "b.bin", data)
        assert done.meta["ok"] is True
        await c.close()
    run(certs, tmp_path, sc, max_concurrent_transfers=1)


def test_same_target_cannot_be_uploaded_twice(certs, tmp_path):
    data = rnd(100_000)

    async def sc(port, db):
        c1 = await authed(port, certs)
        c2 = await relogin(port, certs)
        rid, resp = await upload_begin(c1, "x.bin", data)
        assert resp.meta["ok"] is True
        _, resp2 = await upload_begin(c2, "x.bin", data)
        assert resp2.meta["code"] == ErrorCode.CONFLICT
        await send_chunks(c1, rid, data)
        assert (await recv(c1)).meta["ok"] is True
        done, _ = await upload(c2, "x.bin", data)             # 释放后可以再传
        assert done.meta["ok"] is True
        await c1.close(); await c2.close()
    run(certs, tmp_path, sc)


def test_interleaved_uploads_on_one_connection(certs, tmp_path):
    da, db_ = rnd(150_000, 3), rnd(90_000, 4)

    async def sc(port, db):
        c = await authed(port, certs)
        ra, _ = await upload_begin(c, "a.bin", da)
        rb, _ = await upload_begin(c, "b.bin", db_)
        pa = pb = 0
        step = 30_000
        while pa < len(da) or pb < len(db_):                  # 两个文件的数据块交替发送
            if pa < len(da):
                part = da[pa:pa + step]; pa += len(part)
                await write_packet(c.w, Packet(PacketType.FILE_DATA, ra, {}, part,
                                               Flags.LAST if pa >= len(da) else Flags.NONE))
            if pb < len(db_):
                part = db_[pb:pb + step]; pb += len(part)
                await write_packet(c.w, Packet(PacketType.FILE_DATA, rb, {}, part,
                                               Flags.LAST if pb >= len(db_) else Flags.NONE))
        results = {p.req_id: p for p in (await recv(c), await recv(c))}
        assert results[ra].meta["ok"] and results[rb].meta["ok"]
        assert (tmp_path / "alice" / "a.bin").read_bytes() == da
        assert (tmp_path / "alice" / "b.bin").read_bytes() == db_
        await c.close()
    run(certs, tmp_path, sc)


def test_concurrent_downloads_from_two_connections(certs, tmp_path):
    data = rnd(600_000)

    async def sc(port, db):
        c1 = await authed(port, certs)
        await upload(c1, "big.bin", data)
        c2 = await relogin(port, certs)
        (m1, g1), (m2, g2) = await asyncio.gather(
            download(c1, "big.bin"), download(c2, "big.bin"))
        assert g1 == data and g2 == data
        await c1.close(); await c2.close()
    run(certs, tmp_path, sc)


def test_client_disconnect_mid_download_does_not_break_server(certs, tmp_path):
    data = rnd(3_000_000)

    async def sc(port, db):
        c = await authed(port, certs)
        await upload(c, "big.bin", data)
        rid = next_id(c)
        await write_packet(c.w, Packet(PacketType.DOWNLOAD_REQ, rid,
                                       {"token": c.token, "path": "big.bin"}))
        assert (await recv(c)).meta["ok"] is True
        await recv(c)                                         # 收到第一块就掉线
        await c.close()
        await asyncio.sleep(0.5)
        c2 = await relogin(port, certs)
        assert (await c2.call(PacketType.PING)).type == PacketType.PONG
        assert (await download(c2, "big.bin"))[1] == data     # 服务端状态干净，可正常再下载
        await c2.close()
    run(certs, tmp_path, sc)


def test_download_expect_sha256_guards_resume(certs, tmp_path):
    data = rnd(100_000)

    async def sc(port, db):
        c = await authed(port, certs)
        await upload(c, "f.bin", data)

        async def dl(meta_extra):
            rid = next_id(c)
            await write_packet(c.w, Packet(PacketType.DOWNLOAD_REQ, rid, {
                "token": c.token, "path": "f.bin", "offset": 1000, **meta_extra}))
            return await recv(c)

        ok = await dl({"expect_sha256": sha(data)})               # 版本一致：允许续传
        assert ok.meta["ok"] is True
        while not ((await recv(c)).flags & Flags.LAST):
            pass
        bad = await dl({"expect_sha256": sha(b"another version")})  # 版本不一致：拒绝
        assert bad.meta["ok"] is False and bad.meta["code"] == ErrorCode.CONFLICT
        await c.close()
    run(certs, tmp_path, sc)


def test_download_cancel_frees_slot(certs, tmp_path):
    data = rnd(3_000_000)

    async def sc(port, db):
        c = await authed(port, certs)
        await upload(c, "big.bin", data)
        rid = next_id(c)
        await write_packet(c.w, Packet(PacketType.DOWNLOAD_REQ, rid,
                                       {"token": c.token, "path": "big.bin"}))
        assert (await recv(c)).meta["ok"] is True
        await recv(c)                                           # 收到第一块
        await write_packet(c.w, Packet(PacketType.FILE_DONE, rid, {"ok": False, "cancel": True}))
        await asyncio.sleep(0.5)
        # 名额只有 1 个：取消生效后才能开始新的传输
        rid2 = next_id(c)
        await write_packet(c.w, Packet(PacketType.UPLOAD_REQ, rid2, {
            "token": c.token, "path": "n.txt", "size": 1, "sha256": sha(b"x")}))
        while True:
            p = await recv(c)
            if p.type == PacketType.UPLOAD_RESP:                # 跳过取消前已在途的 FILE_DATA
                break
        assert p.meta["ok"] is True
        await c.close()
    run(certs, tmp_path, sc, max_concurrent_transfers=1)