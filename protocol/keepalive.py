"""启用 TCP KeepAlive（服务端、客户端共用）。

实验要求：代码中要明确指示 keepalive，连接需长期保持，不能一次请求响应后就关闭。
这里分两层保活：
  1. 传输层：SO_KEEPALIVE，由操作系统定期发探测包，发现对端静默掉线。
  2. 应用层：客户端定时发送 PING，服务端回 PONG，超过 idle_timeout 无任何数据则断开。
"""
import socket


def enable_keepalive(sock, idle: int = 30, interval: int = 10, count: int = 3) -> None:
    """idle 秒无数据后开始探测，每 interval 秒一次，连续 count 次无响应判定断线。
    各平台参数名不同，不支持的选项静默跳过（应用层心跳仍然生效）。"""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return
    # Linux
    for name, val in (("TCP_KEEPIDLE", idle), ("TCP_KEEPINTVL", interval), ("TCP_KEEPCNT", count)):
        opt = getattr(socket, name, None)
        if opt is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, opt, val)
            except OSError:
                pass
    # macOS
    opt = getattr(socket, "TCP_KEEPALIVE", None)
    if opt is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, opt, idle)
        except OSError:
            pass
    # Windows
    ioctl = getattr(sock, "ioctl", None)
    if ioctl is not None and hasattr(socket, "SIO_KEEPALIVE_VALS"):
        try:
            ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle * 1000, interval * 1000))
        except OSError:
            pass