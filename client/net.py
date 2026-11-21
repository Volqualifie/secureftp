"""客户端网络层：TLS 长连接、心跳、自动重连、按 ReqID 复用多个并发请求/传输。

线程模型（每条连接）
  读线程   : 不停收帧，按 ReqID 投递到对应 Channel 的队列；PONG 只刷新"最后收到数据时间"
  心跳线程 : 每 heartbeat_interval 秒发一次 PING；超过 3 倍间隔没收到任何数据则判定断线并关闭连接
  发送     : 任意线程调用，用锁保证帧不交错
监督线程（登录成功后启动）：连接断开后以指数退避重连，并自动重新登录。
长连接始终保持，直到调用 close()——不会"一次请求响应后就关闭 TCP"。
"""
import itertools
import logging
import queue
import socket
import ssl
import threading
import time

from protocol.constants import ErrorCode, Flags, PacketType
from protocol.keepalive import enable_keepalive
from protocol.packet import Packet, ProtocolError, recv_packet, send_packet

from .errors import AuthError, ConnectError, ConnectionLost

log = logging.getLogger("client.net")

_LOST = object()        # 放进队列的哨兵：连接已断开

# 这些类型不需要在 meta 里带 token
_NO_TOKEN = {PacketType.FILE_DATA, PacketType.FILE_DONE, PacketType.PING,
             PacketType.REGISTER_REQ, PacketType.LOGIN_REQ}


class Channel:
    """一次请求/传输占用一个 ReqID；服务端发回的同 ReqID 的帧都会进入这个通道。"""

    def __init__(self, link, req_id: int, token_getter):
        self.link, self.req_id, self._token = link, req_id, token_getter
        self.q = link.register(req_id)

    def send(self, ptype, meta=None, payload: bytes = b"", flags: int = Flags.NONE) -> None:
        meta = dict(meta or {})
        if ptype not in _NO_TOKEN:
            meta["token"] = self._token()
        self.link.send(Packet(ptype, self.req_id, meta, payload, flags))

    def _unwrap(self, item):
        if item is _LOST:
            self.q.put(_LOST)           # 之后再 get 仍然报错
            raise ConnectionLost("连接已断开")
        return item

    def get(self, timeout=None) -> Packet:
        try:
            return self._unwrap(self.q.get(timeout=timeout))
        except queue.Empty:
            raise TimeoutError("等待服务端响应超时") from None

    def poll(self):
        """非阻塞取一帧，没有则返回 None。"""
        try:
            return self._unwrap(self.q.get_nowait())
        except queue.Empty:
            return None

    def close(self) -> None:
        self.link.unregister(self.req_id)


class _Link:
    """一条 TLS 连接。断开后不可复用，重连会创建新的 _Link。"""

    def __init__(self, sock):
        self.sock = sock
        self.closed = threading.Event()
        self.last_rx = time.monotonic()
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._waiters = {}
        self._reader = threading.Thread(target=self._read_loop, name="net-reader", daemon=True)

    def start(self):
        self._reader.start()

    # ---- 通道注册 ----
    def register(self, req_id):
        q = queue.Queue()
        with self._lock:
            if self.closed.is_set():
                q.put(_LOST)
            else:
                self._waiters[req_id] = q
        return q

    def unregister(self, req_id):
        with self._lock:
            self._waiters.pop(req_id, None)

    # ---- 收发 ----
    def _read_loop(self):
        try:
            while True:
                pkt = recv_packet(self.sock)
                self.last_rx = time.monotonic()
                if pkt.type == PacketType.PONG:
                    continue
                with self._lock:
                    q = self._waiters.get(pkt.req_id)
                if q is not None:
                    q.put(pkt)
                else:
                    log.debug("丢弃无人等待的帧 type=%s req=%s", pkt.type, pkt.req_id)
        except (OSError, ssl.SSLError, ConnectionError, ProtocolError, ValueError) as e:
            log.debug("读线程结束: %s", e)
        finally:
            self.close()

    def send(self, pkt: Packet) -> None:
        if self.closed.is_set():
            raise ConnectionLost("连接已断开")
        try:
            with self._send_lock:
                send_packet(self.sock, pkt)
        except (OSError, ssl.SSLError, ValueError) as e:
            self.close()
            raise ConnectionLost(f"发送失败: {e}") from None

    def close(self):
        with self._lock:
            if self.closed.is_set():
                return
            self.closed.set()
            waiters = list(self._waiters.values())
        for q in waiters:
            q.put(_LOST)
        for fn in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close):
            try:
                fn()
            except Exception:
                pass

    # ---- 心跳 ----
    def start_heartbeat(self, interval: float):
        interval = max(0.2, float(interval))
        dead_after = max(3 * interval, 3.0)

        def loop():
            while not self.closed.wait(interval):
                if time.monotonic() - self.last_rx > dead_after:
                    log.warning("%.0f 秒未收到服务端数据，判定断线", dead_after)
                    self.close()
                    return
                # 拿不到发送锁说明正有数据在发（也是活动的证据），这一轮跳过
                if self._send_lock.acquire(timeout=interval):
                    try:
                        send_packet(self.sock, Packet(PacketType.PING, 0))
                    except Exception:
                        self.close()
                        return
                    finally:
                        self._send_lock.release()

        threading.Thread(target=loop, name="net-heartbeat", daemon=True).start()


class FTPClient:
    def __init__(self, host, port, ca_cert, client_cert=None, client_key=None,
                 server_hostname=None, on_state=None, connect_timeout=10.0):
        self.host, self.port = host, int(port)
        self.server_hostname = server_hostname or host
        self.connect_timeout = connect_timeout
        self.on_state = on_state            # on_state(state, detail)，在后台线程回调
        self.username = None
        self.is_vip = False
        self.heartbeat_interval = 30
        self.reconnect_wait = 10.0          # 普通请求遇到断线时，最多等待自动重连多久

        ctx = ssl.create_default_context(cafile=ca_cert)    # 只信任我们自己的 CA，并校验主机名
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if client_cert:
            ctx.load_cert_chain(client_cert, client_key)    # 双向证书认证
        self._ctx = ctx

        self._link = None
        self._token = None
        self._creds = None
        self._state = "disconnected"
        self._state_lock = threading.Lock()
        self._connected = threading.Event()
        self._closing = threading.Event()
        self._supervisor = None
        self._req_ids = itertools.count(1)

    # ------------------------------------------------------------ 状态
    @property
    def state(self) -> str:
        return self._state

    def _set_state(self, state: str, detail: str = "") -> None:
        with self._state_lock:
            if self._state == state and not detail:
                return
            self._state = state
        log.info("连接状态: %s %s", state, detail)
        if self.on_state:
            try:
                self.on_state(state, detail)
            except Exception:
                log.exception("on_state 回调出错")

    def wait_connected(self, timeout=None) -> bool:
        """等待连接（含自动重连后）真正可用。认证失败或已关闭时返回 False。
        不能只看 _connected 标志：断线瞬间监督线程还没来得及清除它，必须同时检查连接本身。"""
        end = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._state in ("auth_failed", "closed"):
                return False
            link = self._link
            if self._connected.is_set() and link is not None and not link.closed.is_set():
                return True
            if end is not None and time.monotonic() >= end:
                return False
            time.sleep(0.05)

    # ------------------------------------------------------------ 连接
    def _open_link(self) -> _Link:
        try:
            raw = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
            try:
                tls = self._ctx.wrap_socket(raw, server_hostname=self.server_hostname)
            except Exception:
                raw.close()
                raise
        except ssl.SSLError as e:
            raise ConnectError(f"TLS 握手失败（证书不受信任或服务端要求客户端证书）：{e}") from None
        except OSError as e:
            raise ConnectError(f"无法连接 {self.host}:{self.port}：{e}") from None
        tls.settimeout(None)
        enable_keepalive(tls)               # 传输层保活；应用层还有 PING/PONG
        link = _Link(tls)
        link.start()
        return link

    def connect(self) -> None:
        old, self._link = self._link, self._open_link()
        if old:
            old.close()
        self._connected.set()
        self._set_state("connected")

    def _require_link(self) -> _Link:
        link = self._link
        if link is None or link.closed.is_set():
            raise ConnectionLost("当前没有可用连接")
        return link

    def _call_on(self, link, ptype, meta, timeout=15.0) -> Packet:
        ch = Channel(link, next(self._req_ids), lambda: self._token)
        try:
            ch.send(ptype, meta)
            return ch.get(timeout)
        finally:
            ch.close()

    # ------------------------------------------------------------ 注册 / 登录
    def register(self, username: str, password: str) -> None:
        r = self._call_on(self._require_link(), PacketType.REGISTER_REQ,
                          {"username": username, "password": password})
        if not r.meta.get("ok"):
            raise AuthError(r.meta.get("code"), r.meta.get("message", "注册失败"))

    def _apply_login(self, link, resp: Packet) -> None:
        self._token = resp.meta["token"]
        self.username = resp.meta.get("username")
        self.is_vip = bool(resp.meta.get("is_vip"))
        self.heartbeat_interval = resp.meta.get("heartbeat_interval", 30)
        link.start_heartbeat(self.heartbeat_interval)

    def login(self, username: str, password: str) -> None:
        link = self._require_link()
        r = self._call_on(link, PacketType.LOGIN_REQ,
                          {"username": username, "password": password})
        if not r.meta.get("ok"):
            raise AuthError(r.meta.get("code"), r.meta.get("message", "登录失败"))
        self._creds = (username, password)      # 仅保存在内存，供断线后自动重新登录
        self._apply_login(link, r)
        self._set_state("connected", f"已登录 {username}")
        if self._supervisor is None:
            self._supervisor = threading.Thread(target=self._supervise, name="net-supervisor",
                                                daemon=True)
            self._supervisor.start()

    # ------------------------------------------------------------ 断线重连
    def _supervise(self):
        while not self._closing.is_set():
            link = self._link
            while link is not None and not link.closed.wait(0.3):
                if self._closing.is_set():
                    return
            if self._closing.is_set():
                return
            self._connected.clear()
            self._set_state("disconnected", "连接断开，准备重连")
            backoff = 0.3
            while not self._closing.is_set():
                self._set_state("reconnecting")
                try:
                    new = self._open_link()
                    try:
                        r = self._call_on(new, PacketType.LOGIN_REQ,
                                          {"username": self._creds[0], "password": self._creds[1]})
                    except Exception:
                        new.close()
                        raise
                    if not r.meta.get("ok"):
                        new.close()
                        self._set_state("auth_failed", r.meta.get("message", ""))
                        return
                    self._link = new
                    self._apply_login(new, r)
                    self._connected.set()
                    self._set_state("connected", "已自动重连")
                    break
                except (ConnectError, ConnectionLost, TimeoutError, OSError) as e:
                    log.info("重连失败: %s，%.1f 秒后重试", e, backoff)
                    if self._closing.wait(backoff):
                        return
                    backoff = min(backoff * 2, 8.0)

    def drop_connection(self) -> None:
        """主动断开当前 TCP 连接（模拟断网）。监督线程会自动重连，传输会自动续传。"""
        if self._link is not None:
            self._link.close()

    # ------------------------------------------------------------ 请求 / 通道
    def channel(self) -> Channel:
        link = self._require_link()
        return Channel(link, next(self._req_ids), lambda: self._token)

    def request(self, ptype, meta=None, flags=Flags.NONE, timeout=15.0) -> Packet:
        # 刚断线时先等自动重连（最多 reconnect_wait 秒），而不是立刻报错
        if not self.wait_connected(timeout=self.reconnect_wait):
            raise ConnectionLost("连接已断开且未能在限定时间内重连")
        ch = self.channel()
        try:
            ch.send(ptype, meta, flags=flags)
            return ch.get(timeout)
        finally:
            ch.close()

    def catalog(self, path: str = ""):
        r = self.request(PacketType.CATALOG_REQ, {"path": path})
        if not r.meta.get("ok"):
            from .errors import TransferError
            raise TransferError(r.meta.get("code"), r.meta.get("message", "目录请求失败"))
        return r.meta["entries"]

    def close(self) -> None:
        self._closing.set()
        link = self._link
        if link is not None and not link.closed.is_set():
            try:
                link.send(Packet(PacketType.LOGOUT, 0))
            except ConnectionLost:
                pass
            link.close()
        self._connected.clear()
        self._set_state("closed")