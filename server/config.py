"""加载 config.yaml，并把其中的相对路径解析为相对于配置文件所在目录。"""
import os
from dataclasses import dataclass

import yaml


# 配置文件里没写时的默认限速：普通用户 5 MiB/s，VIP 不限速（0 = 不限速）
DEFAULT_BW_NORMAL = 5 * 1024 * 1024
DEFAULT_BW_VIP = 0


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
    max_file_size: int = 4 * 1024 ** 3        # 单文件上限，默认 4 GiB
    max_concurrent_transfers: int = 4         # 单连接同时进行的传输数
    bandwidth_normal: int = 0                 # 普通用户带宽上限（字节/秒），0 = 不限
    bandwidth_vip: int = 0                    # VIP 用户带宽上限（字节/秒），0 = 不限


def _db_with_env(db: dict) -> dict:
    """环境变量优先于配置文件（容器部署时密码通过环境变量/secret 传入，不写进镜像）。"""
    db = dict(db)
    for key, env in (("host", "FTP_DB_HOST"), ("port", "FTP_DB_PORT"), ("user", "FTP_DB_USER"),
                     ("password", "FTP_DB_PASSWORD"), ("name", "FTP_DB_NAME")):
        if os.environ.get(env):
            db[key] = os.environ[env]
    return db


def load_config(path: str) -> Config:
    base = os.path.dirname(os.path.abspath(path))
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    def rel(p):
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    s, t = raw["server"], raw["tls"]
    for k in ("bandwidth_normal", "bandwidth_vip"):
        if int(s.get(k, 0)) < 0:
            raise ValueError(f"{k} 不能为负数（0 表示不限速）")
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
        db=_db_with_env(raw["database"]),
        max_file_size=int(s.get("max_file_size", 4 * 1024 ** 3)),
        max_concurrent_transfers=int(s.get("max_concurrent_transfers", 4)),
        bandwidth_normal=int(s.get("bandwidth_normal", DEFAULT_BW_NORMAL)),
        bandwidth_vip=int(s.get("bandwidth_vip", DEFAULT_BW_VIP)),
    )