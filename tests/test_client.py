"""客户端集成测试：真实服务端（后台线程）+ 同步客户端。
覆盖：认证、证书校验、上传下载与进度、取消与断点续传、远程文件变化、
传输中断网自动续传、心跳保活、服务端重启后自动重连、并发上传。
运行：python -m pytest tests/test_client.py -v
"""
import asyncio
import os
import random
import threading
import time

import pytest

from client.errors import AuthError, Cancelled, ConnectError, ConnectionLost, TransferError
from client.net import FTPClient
from client.transfer import download_file, upload_file
from protocol.constants import ErrorCode
from server.main import start_server
from test_server_auth import PW, FakeDB, certs, make_cfg  # noqa: F401  (certs 是 fixture)


def rnd(n, seed=1):
    return random.Random(seed).randbytes(n)


def wait_for(cond, timeout=10.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return False


# ------------------------------------------------------------- 后台线程里的真实服务端
class ServerThread:
    def __init__(self, certs, storage, db, **over):
        self.certs, self.storage, self.db, self.over = certs, storage, db, over
        self.loop = self.thread = self.server = None
        self.port = 0

    def start(self, port=0):
        self.loop = asyncio.new_event_loop()
        ready, err = threading.Event(), []

        def target():
            asyncio.set_event_loop(self.loop)

            async def boot():
                cfg = make_cfg(self.certs, port=port, storage_root=self.storage, **self.over)
                self.server = await start_server(cfg, self.db)
                self.port = self.server.sockets[0].getsockname()[1]

            try:
                self.loop.run_until_complete(boot())
            except Exception as e:      # noqa: BLE001
                err.append(e)
            ready.set()
            if not err:
                self.loop.run_forever()
            self.loop.close()

        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()
        assert ready.wait(10)
        if err:
            raise err[0]

    def stop(self):
        if not self.thread or not self.thread.is_alive():
            return

        async def shutdown():
            self.server.close()
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run_coroutine_threadsafe(shutdown(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)


class Env:
    def __init__(self, certs, tmp_path):
        self.certs, self.tmp = certs, tmp_path
        self.storage = str(tmp_path / "storage")
        self.db = FakeDB()
        self._servers, self._clients = [], []

    def server(self, **over):
        s = ServerThread(self.certs, self.storage, self.db, **over)
        s.start()
        self._servers.append(s)
        return s

    def client(self, srv, login=True, connect=True, name="alice", **kw):
        events = []
        kw.setdefault("ca_cert", self.certs["ca-cert"])
        c = FTPClient("127.0.0.1", srv.port, on_state=lambda s, d: events.append(s), **kw)
        c.events = events
        self._clients.append(c)
        if connect:
            c.connect()
        if connect and login:
            try:
                c.register(name, PW)
            except AuthError:
                pass                    # 已存在
            c.login(name, PW)
        return c

    def local(self, name, data):
        p = self.tmp / "local" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return str(p)

    def remote_path(self, rel, user="alice"):
        return os.path.join(self.storage, user, rel)

    def close(self):
        for c in self._clients:
            c.close()
        for s in self._servers:
            s.stop()


@pytest.fixture
def env(certs, tmp_path):
    e = Env(certs, tmp_path)
    yield e
    e.close()


# ------------------------------------------------------------- 认证与证书
def test_register_login_and_catalog(env):
    c = env.client(env.server())
    assert c.catalog() == []
    assert c.state == "connected" and c.username == "alice"


def test_auth_errors(env):
    srv = env.server()
    c = env.client(srv, login=False)
    c.register("alice", PW)
    with pytest.raises(AuthError) as e:
        c.register("alice", PW)
    assert e.value.code == ErrorCode.CONFLICT
    with pytest.raises(AuthError) as e:
        c.login("alice", "WrongPass1")
    assert e.value.code == ErrorCode.UNAUTHORIZED


def test_untrusted_server_certificate_rejected(env):
    srv = env.server()
    with pytest.raises(ConnectError):       # 客户端只信任指定 CA；换一个不相干的 CA 就必须失败
        env.client(srv, ca_cert=env.certs["client-cert"])


def test_mutual_tls(env):
    srv = env.server(require_client_cert=True)
    c = env.client(srv, login=False)                        # 没带客户端证书
    with pytest.raises((ConnectError, ConnectionLost)):
        c.register("alice", PW)
    c2 = env.client(srv, client_cert=env.certs["client-cert"],
                    client_key=env.certs["client-key"])
    assert c2.catalog() == []


def test_not_logged_in_requests_are_rejected(env):
    c = env.client(env.server(), login=False)
    with pytest.raises(TransferError) as e:
        c.catalog()
    assert e.value.code == ErrorCode.UNAUTHORIZED


# ------------------------------------------------------------- 上传 / 下载
def test_upload_download_roundtrip_with_progress(env):
    c = env.client(env.server())
    data = rnd(700_123)
    src = env.local("a.bin", data)
    calls = []
    r = upload_file(c, src, "docs/a.bin", progress=lambda d, t: calls.append((d, t)))
    assert r["size"] == len(data) and r["resumed_from"] == 0
    assert calls[-1] == (len(data), len(data))
    assert [d for d, _ in calls] == sorted(d for d, _ in calls)        # 进度单调不减
    with open(env.remote_path("docs/a.bin"), "rb") as f:
        assert f.read() == data

    dst = str(env.tmp / "dl" / "out.bin")
    calls.clear()
    download_file(c, "docs/a.bin", dst, progress=lambda d, t: calls.append((d, t)))
    assert open(dst, "rb").read() == data and calls[-1] == (len(data), len(data))
    assert sorted(os.listdir(env.tmp / "dl")) == ["out.bin"]            # 没有 .part 残留
    assert [e["name"] for e in c.catalog("docs")] == ["a.bin"]


def test_empty_file(env):
    c = env.client(env.server())
    upload_file(c, env.local("e.txt", b""), "e.txt")
    dst = str(env.tmp / "dl" / "e.txt")
    download_file(c, "e.txt", dst)
    assert open(dst, "rb").read() == b""


def test_server_rejections_surface_as_transfer_error(env):
    c = env.client(env.server(max_file_size=1000))
    with pytest.raises(TransferError) as e:
        upload_file(c, env.local("big.bin", rnd(5000)), "big.bin")
    assert e.value.code == ErrorCode.TOO_LARGE
    with pytest.raises(TransferError) as e:
        upload_file(c, env.local("s.txt", b"x"), "../evil.txt")
    assert e.value.code == ErrorCode.FORBIDDEN
    with pytest.raises(TransferError) as e:
        download_file(c, "missing.bin", str(env.tmp / "m.bin"))
    assert e.value.code == ErrorCode.NOT_FOUND
    with pytest.raises(TransferError) as e:
        c.catalog("../")
    assert e.value.code == ErrorCode.FORBIDDEN


# ------------------------------------------------------------- 取消与断点续传
def test_upload_cancel_then_resume(env):
    c = env.client(env.server())
    data = rnd(2_000_000)
    src = env.local("big.bin", data)
    cancel = threading.Event()

    def stop_at_300k(done, total):
        if done >= 300_000:
            cancel.set()

    with pytest.raises(Cancelled):
        upload_file(c, src, "big.bin", progress=stop_at_300k, cancel=cancel)
    part = env.remote_path("big.bin.part")
    assert wait_for(lambda: os.path.exists(part) and os.path.getsize(part) > 0)
    assert not os.path.exists(env.remote_path("big.bin"))

    r = upload_file(c, src, "big.bin")                      # 同一条连接，服务端已释放该传输
    assert 0 < r["resumed_from"] <= len(data)
    assert open(env.remote_path("big.bin"), "rb").read() == data
    assert not os.path.exists(part)


def test_download_cancel_then_resume(env):
    c = env.client(env.server())
    data = rnd(3_000_000)
    upload_file(c, env.local("big.bin", data), "big.bin")
    dst = str(env.tmp / "dl" / "big.bin")
    cancel = threading.Event()

    def stop_at_500k(done, total):
        if done >= 500_000:
            cancel.set()

    with pytest.raises(Cancelled):
        download_file(c, "big.bin", dst, progress=stop_at_500k, cancel=cancel)
    assert os.path.getsize(dst + ".part") >= 500_000 and not os.path.exists(dst)

    r = download_file(c, "big.bin", dst)
    assert r["resumed_from"] >= 500_000
    assert open(dst, "rb").read() == data
    assert not os.path.exists(dst + ".part") and not os.path.exists(dst + ".part.json")


def test_download_restarts_when_remote_file_changed(env):
    c = env.client(env.server())
    v1, v2 = rnd(2_000_000, 1), rnd(2_000_000, 2)           # 大小相同，内容不同
    upload_file(c, env.local("v1.bin", v1), "f.bin")
    dst = str(env.tmp / "dl" / "f.bin")
    cancel = threading.Event()
    with pytest.raises(Cancelled):
        download_file(c, "f.bin", dst, cancel=cancel,
                      progress=lambda d, t: cancel.set() if d >= 400_000 else None)
    upload_file(c, env.local("v2.bin", v2), "f.bin")        # 远程文件被替换
    r = download_file(c, "f.bin", dst)
    assert r["resumed_from"] == 0                           # 不能拼接新旧两个版本
    assert open(dst, "rb").read() == v2


# ------------------------------------------------------------- 断网 / 心跳 / 重连
def test_upload_survives_connection_drop(env):
    c = env.client(env.server())
    data = rnd(3_000_000)
    src = env.local("big.bin", data)
    dropped = []

    def drop_once(done, total):
        if done >= 500_000 and not dropped:
            dropped.append(done)
            c.drop_connection()                             # 传输中途"断网"

    r = upload_file(c, src, "big.bin", progress=drop_once)
    assert dropped and r["size"] == len(data)
    assert open(env.remote_path("big.bin"), "rb").read() == data
    assert "reconnecting" in c.events and c.events[-1] == "connected"


def test_download_survives_connection_drop(env):
    c = env.client(env.server())
    data = rnd(3_000_000)
    upload_file(c, env.local("big.bin", data), "big.bin")
    dst = str(env.tmp / "dl" / "big.bin")
    dropped = []

    def drop_once(done, total):
        if done >= 500_000 and not dropped:
            dropped.append(done)
            c.drop_connection()

    r = download_file(c, "big.bin", dst, progress=drop_once)
    assert dropped and open(dst, "rb").read() == data
    assert r["resumed_from"] > 0                            # 重连后是续传，不是重头来


def test_heartbeat_keeps_idle_connection_alive(env):
    # 服务端 3 秒无数据就断开；客户端每 1 秒发一次 PING，所以空闲 5 秒后连接仍在
    c = env.client(env.server(heartbeat_interval=1, idle_timeout=3))
    assert c.heartbeat_interval == 1
    time.sleep(5)
    assert c.catalog() == []
    assert "disconnected" not in c.events


def test_auto_reconnect_after_server_restart(env):
    srv = env.server()
    c = env.client(srv)
    old_token = c._token
    port = srv.port
    srv.stop()
    assert wait_for(lambda: c.state in ("disconnected", "reconnecting"), 10)
    srv.start(port=port)                                    # 同一端口重新启动服务端
    assert c.wait_connected(timeout=30)
    assert c._token != old_token                            # 自动重新登录，拿到新 token
    assert c.catalog() == []


def test_concurrent_uploads_on_one_connection(env):
    c = env.client(env.server())
    files = {f"f{i}.bin": rnd(400_000 + i * 1000, i) for i in range(3)}
    errors = []

    def work(name, data):
        try:
            upload_file(c, env.local(name, data), name)
        except Exception as e:          # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=kv) for kv in files.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors
    for name, data in files.items():
        assert open(env.remote_path(name), "rb").read() == data