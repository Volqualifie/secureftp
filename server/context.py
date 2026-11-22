"""服务端共享上下文：配置、数据库、限流器。各连接的 Session 共用同一个实例。"""
from .config import Config
from .ratelimit import RateLimiter
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
        self.active_uploads = set()     # 正在被写入的目标路径，防止两个会话同时写同一文件
        self.hash_cache = {}            # (path, mtime_ns, size) -> sha256，避免重复计算大文件哈希
        self.vip_ids = set()            # 运行期间新开通的 VIP：让该用户已有的其它连接立即生效
        self._limiters = {}             # user_id -> RateLimiter（同一用户的所有连接共享）

    def rate_for(self, user) -> int:
        vip = user.get("is_vip") or user["id"] in self.vip_ids
        return self.cfg.bandwidth_vip if vip else self.cfg.bandwidth_normal

    def limiter_for(self, user):
        """返回该用户的限速器；不限速时返回 None。"""
        rate = self.rate_for(user)
        if rate <= 0:
            return None
        lim = self._limiters.get(user["id"])
        if lim is None or lim.rate != rate:         # 首次使用，或管理员改了 VIP 状态后重新登录
            lim = self._limiters[user["id"]] = RateLimiter(rate)
        return lim