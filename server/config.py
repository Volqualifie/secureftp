"""加载 config.yaml，并把其中的相对路径解析为相对于配置文件所在目录。"""
import os
from dataclasses import dataclass

import yaml


@dataclass
class Config:
    host: str
    port: int
    storage_root: str
    max_conn_per_ip: int
    max_frame_size: int
    chunk_size: int
    heartbeat_interval: int
    idle_timeout: int
    auth_timeout: int           # 连接后多久内必须登录，否则断开（防空连接占坑）
    login_max_failures: int     # 窗口内最多失败次数
    login_window: int           # 失败统计窗口（秒）
    login_lockout: int          # 超限后锁定时长（秒）
    tls_cert: str
    tls_key: str
    tls_ca: str
    require_client_cert: bool
    db: dict


def load_config(path: str) -> Config:
    base = os.path.dirname(os.path.abspath(path))
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    def rel(p):
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    s, t = raw["server"], raw["tls"]
    return Config(
        host=s["host"],
        port=int(s["port"]),
        storage_root=rel(s["storage_root"]),
        max_conn_per_ip=int(s.get("max_conn_per_ip", 10)),
        max_frame_size=int(s.get("max_frame_size", 1048576)),
        chunk_size=int(s.get("chunk_size", 65536)),
        heartbeat_interval=int(s.get("heartbeat_interval", 30)),
        idle_timeout=int(s.get("idle_timeout", 90)),
        auth_timeout=int(s.get("auth_timeout", 15)),
        login_max_failures=int(s.get("login_max_failures", 5)),
        login_window=int(s.get("login_window", 300)),
        login_lockout=int(s.get("login_lockout", 300)),
        tls_cert=rel(t["cert"]),
        tls_key=rel(t["key"]),
        tls_ca=rel(t["ca"]),
        require_client_cert=bool(t.get("require_client_cert", False)),
        db=raw["database"],
    )