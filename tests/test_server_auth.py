"""服务端基础集成测试：真实 TLS + 假数据库（不依赖 MySQL）。

覆盖：注册/登录、防用户名枚举、SQL 注入、登录限流、未认证超时、
心跳保活、畸形包、单 IP 连接数、双向证书、路径穿越校验。
运行：python -m pytest tests/test_server_auth.py -v
"""
import asyncio
import os
import ssl

import pytest

from protocol.constants import ErrorCode, PacketType
from protocol.packet import Packet, read_packet, write_packet
from server.config import Config
from server.db import UserExists
from server.main import start_server
from server.security import (
    LoginRateLimiter, ValidationError, hash_password, safe_join,
)

PW = "Passw0rd!"


# ------------------------- 证书夹具 -------------------------
def _make_certs(d):
    """用 cryptography 在 Python 里生成 CA / 服务端 / 客户端证书，不依赖命令行 openssl，
    Windows、Linux、macOS 行为一致。"""
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime.now(datetime.timezone.utc)

    def name(cn):
        return x509.Name([x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
                          x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test"),
                          x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    def key():
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def save(path, obj):
        if isinstance(obj, x509.Certificate):
            data = obj.public_bytes(serialization.Encoding.PEM)
        else:
            data = obj.private_bytes(serialization.Encoding.PEM,
                                     serialization.PrivateFormat.TraditionalOpenSSL,
                                     serialization.NoEncryption())
        path.write_bytes(data)

    def builder(subject, issuer, pub):
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
                .public_key(pub).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=5))
                .not_valid_after(now + datetime.timedelta(days=30)))

    ca_key = key()
    ca_cert = (builder(name("Test CA"), name("Test CA"), ca_key.public_key())
               .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(
                   digital_signature=False, content_commitment=False, key_encipherment=False,
                   data_encipherment=False, key_agreement=False, key_cert_sign=True,
                   crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                              critical=False)
               .sign(ca_key, hashes.SHA256()))
    save(d / "ca-cert.pem", ca_cert)

    def leaf(prefix, cn, eku, san=None, encipher=False):
        k = key()
        b = (builder(name(cn), ca_cert.subject, k.public_key())
             .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
             .add_extension(x509.KeyUsage(
                 digital_signature=True, content_commitment=False, key_encipherment=encipher,
                 data_encipherment=False, key_agreement=False, key_cert_sign=False,
                 crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
             .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
             .add_extension(x509.SubjectKeyIdentifier.from_public_key(k.public_key()),
                            critical=False)
             .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                 ca_key.public_key()), critical=False))
        if san:
            b = b.add_extension(x509.SubjectAlternativeName(san), critical=False)
        save(d / f"{prefix}-key.pem", k)
        save(d / f"{prefix}-cert.pem", b.sign(ca_key, hashes.SHA256()))

    leaf("server", "localhost", ExtendedKeyUsageOID.SERVER_AUTH, encipher=True,
         san=[x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))])
    leaf("client", "client01", ExtendedKeyUsageOID.CLIENT_AUTH)


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    d = tmp_path_factory.mktemp("certs")
    _make_certs(d)
    return {k: str(d / f"{k}.pem") for k in
            ("ca-cert", "server-cert", "server-key", "client-cert", "client-key")}


# ------------------------- 假数据库 -------------------------
class FakeDB:
    def __init__(self):
        self.users = {}
        self.get_calls = []
        self.logs = []                  # 传输日志 (user_id, action, filename, size, sha256, status)

    async def create_user(self, username, password_hash):
        if username in self.users:
            raise UserExists(username)
        self.users[username] = {"id": len(self.users) + 1, "username": username,
                                "password_hash": password_hash, "is_vip": 0}
        return self.users[username]["id"]

    async def get_user(self, username):
        self.get_calls.append(username)
        return self.users.get(username)

    async def log_transfer(self, *a):
        self.logs.append(a)


def make_cfg(certs, **over) -> Config:
    base = dict(
        host="127.0.0.1", port=0, storage_root="/tmp/none",
        max_conn_per_ip=10, max_frame_size=1 << 20, chunk_size=65536,
        heartbeat_interval=30, idle_timeout=90, auth_timeout=15,
        login_max_failures=3, login_window=300, login_lockout=300,
        tls_cert=certs["server-cert"], tls_key=certs["server-key"],
        tls_ca=certs["ca-cert"], require_client_cert=False, db={},
    )
    base.update(over)
    return Config(**base)


# ------------------------- 测试客户端 -------------------------
class Client:
    def __init__(self, reader, writer):
        self.r, self.w = reader, writer
        self._id = 0

    @classmethod
    async def connect(cls, port, certs, with_client_cert=False):
        ctx = ssl.create_default_context(cafile=certs["ca-cert"])
        if with_client_cert:
            ctx.load_cert_chain(certs["client-cert"], certs["client-key"])
        r, w = await asyncio.open_connection("127.0.0.1", port, ssl=ctx,
                                             server_hostname="localhost")
        return cls(r, w)

    async def call(self, ptype, meta=None, timeout=5):
        self._id += 1
        await write_packet(self.w, Packet(ptype, self._id, meta or {}))
        return await asyncio.wait_for(read_packet(self.r), timeout)

    async def closed_within(self, timeout) -> bool:
        """服务端是否在 timeout 秒内关闭了连接。"""
        try:
            await asyncio.wait_for(self.r.read(-1), timeout)   # 读到 EOF（服务端可能先发 ERROR 帧再关闭）
            return True
        except asyncio.TimeoutError:
            return False
        except (ConnectionError, ssl.SSLError, asyncio.IncompleteReadError):
            return True

    async def close(self):
        self.w.close()
        try:
            await self.w.wait_closed()
        except Exception:
            pass


def run_server(certs, scenario, **cfg_over):
    async def main():
        db = FakeDB()
        server = await start_server(make_cfg(certs, **cfg_over), db)
        port = server.sockets[0].getsockname()[1]
        try:
            return await scenario(port, db)
        finally:
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), 3)
            except asyncio.TimeoutError:
                pass
    return asyncio.run(main())


async def register(c, name="alice", pw=PW):
    return await c.call(PacketType.REGISTER_REQ, {"username": name, "password": pw})


async def login(c, name="alice", pw=PW):
    return await c.call(PacketType.LOGIN_REQ, {"username": name, "password": pw})


# ------------------------- 注册 / 登录 -------------------------
def test_register_and_login(certs):
    async def sc(port, db):
        c = await Client.connect(port, certs)
        assert (await register(c)).meta["ok"] is True
        resp = await login(c)
        assert resp.type == PacketType.LOGIN_RESP and resp.meta["ok"] is True
        assert len(resp.meta["token"]) >= 32
        # 数据库里存的是 bcrypt 哈希而不是明文
        stored = db.users["alice"]["password_hash"]
        assert stored.startswith("$2") and PW not in stored
        await c.close()
    run_server(certs, sc)


def test_duplicate_register(certs):
    async def sc(port, db):
        c = await Client.connect(port, certs)
        await register(c)
        r = await register(c)
        assert r.meta["ok"] is False and r.meta["code"] == ErrorCode.CONFLICT
        await c.close()
    run_server(certs, sc)


def test_register_validation(certs):
    async def sc(port, db):
        c = await Client.connect(port, certs)
        assert (await register(c, "ab", PW)).meta["code"] == ErrorCode.BAD_REQUEST
        assert (await register(c, "bad name!", PW)).meta["code"] == ErrorCode.BAD_REQUEST
        assert (await register(c, "bobby", "short")).meta["code"] == ErrorCode.BAD_REQUEST
        assert (await c.call(PacketType.REGISTER_REQ,
                             {"username": 123, "password": None})).meta["ok"] is False
        await c.close()
    run_server(certs, sc)


def test_wrong_password_and_unknown_user_look_identical(certs):
    async def sc(port, db):
        c = await Client.connect(port, certs)
        await register(c)
        wrong = await login(c, "alice", "WrongPass1")
        ghost = await login(c, "nobody1", "WrongPass1")
        assert wrong.meta == ghost.meta
        assert wrong.meta["code"] == ErrorCode.UNAUTHORIZED
        await c.close()
    run_server(certs, sc)


def test_sql_injection_never_reaches_db(certs):
    async def sc(port, db):
        c = await Client.connect(port, certs)
        await register(c)
        r = await c.call(PacketType.LOGIN_REQ,
                         {"username": "' OR 1=1 --", "password": "x"})
        assert r.meta["ok"] is False
        r = await c.call(PacketType.LOGIN_REQ,
                         {"username": "alice' --", "password": "x"})
        assert r.meta["ok"] is False
        assert "' OR 1=1 --" not in db.get_calls and "alice' --" not in db.get_calls
        await c.close()
    run_server(certs, sc)


def test_login_lockout(certs):
    async def sc(port, db):
        c = await Client.connect(port, certs)
        await register(c)
        for _ in range(3):
            assert (await login(c, "alice", "WrongPass1")).meta["code"] == ErrorCode.UNAUTHORIZED
        # 已锁定：即使密码正确也拒绝
        r = await login(c)
        assert r.meta["ok"] is False and r.meta["code"] == ErrorCode.RATE_LIMITED
        await c.close()
    run_server(certs, sc)


# ------------------------- 认证与会话 -------------------------
def test_ping_pong_without_login(certs):
    async def sc(port, db):
        c = await Client.connect(port, certs)
        r = await c.call(PacketType.PING)
        assert r.type == PacketType.PONG
        await c.close()
    run_server(certs, sc)


def test_protected_request_needs_login_and_token(certs):
    async def sc(port, db):
        c = await Client.connect(port, certs)
        r = await c.call(PacketType.CATALOG_REQ, {"path": "/"})
        assert r.type == PacketType.ERROR and r.meta["code"] == ErrorCode.UNAUTHORIZED
        await register(c)
        tok = (await login(c)).meta["token"]
        r = await c.call(PacketType.CATALOG_REQ, {"path": "/"})            # 无 token
        assert r.meta["code"] == ErrorCode.UNAUTHORIZED
        r = await c.call(PacketType.CATALOG_REQ, {"path": "/", "token": "forged"})
        assert r.meta["code"] == ErrorCode.UNAUTHORIZED
        r = await c.call(PacketType.CATALOG_REQ, {"path": "/", "token": tok})
        assert r.type == PacketType.CATALOG_RESP and r.meta["ok"] is True   # 通过认证
        await c.close()
    run_server(certs, sc)


def test_unauthenticated_connection_times_out(certs):
    async def sc(port, db):
        c = await Client.connect(port, certs)
        assert await c.closed_within(4) is True
    run_server(certs, sc, auth_timeout=1)


def test_ping_cannot_extend_auth_deadline(certs):
    """未登录时不停发 PING 也不能续命，auth_timeout 是绝对期限。"""
    async def sc(port, db):
        c = await Client.connect(port, certs)
        loop = asyncio.get_running_loop()
        end = loop.time() + 4
        closed = False
        while loop.time() < end and not closed:
            try:
                await c.call(PacketType.PING, timeout=1)
            except (asyncio.TimeoutError, ConnectionError, ssl.SSLError,
                    asyncio.IncompleteReadError):
                closed = True
            await asyncio.sleep(0.3)
        assert closed
    run_server(certs, sc, auth_timeout=1)


def test_heartbeat_keeps_connection_alive_then_idle_timeout(certs):
    async def sc(port, db):
        c = await Client.connect(port, certs)
        await register(c)
        await login(c)
        for _ in range(6):                     # 2.4s > idle_timeout(1s)，但持续有心跳
            assert (await c.call(PacketType.PING)).type == PacketType.PONG
            await asyncio.sleep(0.4)
        assert await c.closed_within(3) is True   # 停止心跳后被空闲超时断开
    run_server(certs, sc, idle_timeout=1)


# ------------------------- 攻击面 -------------------------
def test_malformed_frame_closes_only_that_connection(certs):
    async def sc(port, db):
        bad = await Client.connect(port, certs)
        bad.w.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n" + b"\x00" * 16)
        await bad.w.drain()
        assert await bad.closed_within(3) is True
        good = await Client.connect(port, certs)          # 服务端没有崩溃
        assert (await good.call(PacketType.PING)).type == PacketType.PONG
        await good.close()
    run_server(certs, sc)


def test_oversize_length_field_rejected(certs):
    import struct
    from protocol.constants import HEADER_FORMAT, MAGIC, VERSION

    async def sc(port, db):
        c = await Client.connect(port, certs)
        c.w.write(struct.pack(HEADER_FORMAT, MAGIC, VERSION, PacketType.FILE_DATA,
                              0, 1, 0, 0xFFFFFFFF))
        await c.w.drain()
        assert await c.closed_within(3) is True
    run_server(certs, sc)


def test_per_ip_connection_limit(certs):
    async def sc(port, db):
        a = await Client.connect(port, certs)
        b = await Client.connect(port, certs)
        third = await Client.connect(port, certs)
        assert await third.closed_within(3) is True
        assert (await a.call(PacketType.PING)).type == PacketType.PONG
        assert (await b.call(PacketType.PING)).type == PacketType.PONG
        await a.close()
        await asyncio.sleep(0.2)
        d = await Client.connect(port, certs)             # 释放名额后可再连
        assert (await d.call(PacketType.PING)).type == PacketType.PONG
        await b.close(); await d.close()
    run_server(certs, sc, max_conn_per_ip=2)


def test_untrusted_server_rejected_by_client(certs):
    """客户端不信任服务端的 CA 时握手必须失败（中间人防御的基础）。"""
    async def sc(port, db):
        ctx = ssl.create_default_context()           # 不加载我们的 CA
        with pytest.raises(ssl.SSLCertVerificationError):
            await asyncio.open_connection("127.0.0.1", port, ssl=ctx,
                                          server_hostname="localhost")
    run_server(certs, sc)


def test_mutual_tls_required(certs):
    async def sc(port, db):
        no_cert = await Client.connect(port, certs, with_client_cert=False)
        try:
            await no_cert.call(PacketType.PING, timeout=3)
            rejected = False
        except (ConnectionError, ssl.SSLError, asyncio.IncompleteReadError):
            rejected = True
        assert rejected
        ok = await Client.connect(port, certs, with_client_cert=True)
        assert (await ok.call(PacketType.PING)).type == PacketType.PONG
        await ok.close()
    run_server(certs, sc, require_client_cert=True)


# ------------------------- 单元：工具函数 -------------------------
def test_safe_join(tmp_path):
    root = str(tmp_path)
    (tmp_path / "u1").mkdir()
    base = os.path.realpath(str(tmp_path / "u1"))
    assert safe_join(root, "u1", "a/b.txt") == os.path.join(base, "a", "b.txt")
    assert safe_join(root, "u1", "/a.txt") == os.path.join(base, "a.txt")   # 绝对路径被当作相对
    assert safe_join(root, "u1", "") == base
    for evil in ("../u2/x", "../../etc/passwd", "a/../../x", "..\\..\\x", "a\x00b"):
        with pytest.raises(ValidationError):
            safe_join(root, "u1", evil)


def test_rate_limiter_window_and_expiry():
    now = [0.0]
    rl = LoginRateLimiter(3, window=10, lockout=20, clock=lambda: now[0])
    for _ in range(2):
        rl.record_failure("ip")
    assert not rl.is_locked("ip")
    rl.record_failure("ip")
    assert rl.is_locked("ip")
    now[0] = 19.9
    assert rl.is_locked("ip")
    now[0] = 20.1
    assert not rl.is_locked("ip")
    # 窗口外的旧失败不累计
    rl.record_failure("x"); now[0] = 50
    rl.record_failure("x"); rl.record_failure("x")
    assert not rl.is_locked("x")


def test_hash_is_salted():
    async def go():
        return await hash_password(PW), await hash_password(PW)
    a, b = asyncio.run(go())
    assert a != b