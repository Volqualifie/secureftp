"""服务端共享上下文：配置、数据库、限流器。各连接的 Session 共用同一个实例。"""
from .config import Config
from .security import ConnectionLimiter, LoginRateLimiter


class ServerContext:
    def __init__(self, cfg: Config, db):
        self.cfg = cfg
        self.db = db
        self.conn_limiter = ConnectionLimiter(cfg.max_conn_per_ip)
        self.login_limiter = LoginRateLimiter(
            cfg.login_max_failures, cfg.login_window, cfg.login_lockout)
        # 注册要做 bcrypt（耗 CPU），限制单 IP 频率防止被拿来消耗服务器算力
        self.register_limiter = LoginRateLimiter(10, 3600, 600)