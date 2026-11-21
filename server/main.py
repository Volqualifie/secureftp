"""服务端入口：TLS 监听，每个连接一个 Session。

运行（在项目根目录）：
    python -m server.main --config server/config.yaml
"""
import argparse
import asyncio
import logging
import ssl

from protocol.keepalive import enable_keepalive

from .config import Config, load_config
from .context import ServerContext
from .db import Database
from .session import Session

log = logging.getLogger("server")


def build_ssl_context(cfg: Config) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2        # 拒绝 SSL3/TLS1.0/1.1
    ctx.load_cert_chain(cfg.tls_cert, cfg.tls_key)
    if cfg.require_client_cert:                         # 双向证书认证（加分项）
        ctx.load_verify_locations(cfg.tls_ca)
        ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


async def start_server(cfg: Config, db) -> asyncio.base_events.Server:
    sctx = ServerContext(cfg, db)

    async def on_connect(reader, writer):
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else "unknown"
        if not sctx.conn_limiter.acquire(ip):
            log.warning("单 IP 连接数超限，拒绝 %s", ip)
            writer.close()
            return
        try:
            sock = writer.get_extra_info("socket")
            if sock is not None:
                enable_keepalive(sock, idle=cfg.heartbeat_interval)
            await Session(sctx, reader, writer, ip).run()
        except Exception:
            log.exception("连接 %s 异常", ip)
        finally:
            sctx.conn_limiter.release(ip)

    return await asyncio.start_server(
        on_connect, cfg.host, cfg.port,
        ssl=build_ssl_context(cfg),
        ssl_handshake_timeout=10,       # 握手超时，防止只建连不握手的慢速攻击
        backlog=128,
    )


async def amain(config_path: str) -> None:
    cfg = load_config(config_path)
    server = await start_server(cfg, Database(cfg.db))
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    log.info("服务已启动，监听 %s (TLS, 客户端证书=%s)", addrs, cfg.require_client_cert)
    async with server:
        await server.serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="server/config.yaml")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(amain(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
