"""安全工具：密码哈希、输入校验、连接数限制、登录失败限流、路径校验。"""
import asyncio
import os
import re
import secrets
import time
from collections import defaultdict, deque

import bcrypt

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 72           # bcrypt 只使用前 72 字节


class ValidationError(Exception):
    pass


def validate_username(name) -> str:
    if not isinstance(name, str) or not USERNAME_RE.match(name):
        raise ValidationError("用户名需为 3-32 位字母、数字或下划线")
    return name


def validate_password(pw) -> str:
    if not isinstance(pw, str) or not (MIN_PASSWORD_LEN <= len(pw.encode()) <= MAX_PASSWORD_LEN):
        raise ValidationError(f"密码长度需为 {MIN_PASSWORD_LEN}-{MAX_PASSWORD_LEN} 字节")
    return pw


async def hash_password(pw: str) -> str:
    # bcrypt 是 CPU 密集型，放到线程里避免阻塞事件循环
    return (await asyncio.to_thread(
        bcrypt.hashpw, pw.encode(), bcrypt.gensalt(rounds=12))).decode()


# 用户不存在时也做一次哈希比较，使"用户不存在"与"密码错误"耗时接近，防止用户名枚举
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password", bcrypt.gensalt(rounds=12))


async def verify_password(pw: str, pw_hash) -> bool:
    h = pw_hash.encode() if pw_hash else _DUMMY_HASH
    ok = await asyncio.to_thread(bcrypt.checkpw, pw.encode(), h)
    return ok and pw_hash is not None


def new_token() -> str:
    return secrets.token_urlsafe(32)


class ConnectionLimiter:
    """限制单个 IP 的并发连接数。"""

    def __init__(self, max_per_ip: int):
        self.max = max_per_ip
        self._count = defaultdict(int)

    def acquire(self, ip: str) -> bool:
        if self._count[ip] >= self.max:
            return False
        self._count[ip] += 1
        return True

    def release(self, ip: str) -> None:
        self._count[ip] -= 1
        if self._count[ip] <= 0:
            self._count.pop(ip, None)


class LoginRateLimiter:
    """滑动窗口：同一 (IP) 在 window 秒内失败 max_failures 次后锁定 lockout 秒。"""

    def __init__(self, max_failures: int, window: int, lockout: int, clock=time.monotonic):
        self.max, self.window, self.lockout = max_failures, window, lockout
        self._clock = clock
        self._fails = defaultdict(deque)
        self._locked_until = {}

    def is_locked(self, key: str) -> bool:
        until = self._locked_until.get(key)
        if until is None:
            return False
        if self._clock() >= until:
            del self._locked_until[key]
            self._fails.pop(key, None)
            return False
        return True

    def record_failure(self, key: str) -> None:
        now = self._clock()
        q = self._fails[key]
        q.append(now)
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max:
            self._locked_until[key] = now + self.lockout

    def record_success(self, key: str) -> None:
        self._fails.pop(key, None)


def safe_join(root: str, user_dir: str, rel_path: str) -> str:
    """把用户提交的相对路径解析到 root/user_dir 之内，防止路径穿越（../、绝对路径、符号链接）。
    越界则抛 ValidationError。"""
    base = os.path.realpath(os.path.join(root, user_dir))
    if not isinstance(rel_path, str) or "\x00" in rel_path:
        raise ValidationError("非法路径")
    rel_path = rel_path.replace("\\", "/").lstrip("/")
    target = os.path.realpath(os.path.join(base, rel_path))
    if target != base and not target.startswith(base + os.sep):
        raise ValidationError("路径越界")
    return target


_BAD_CHARS = set('<>:"|?*') | {chr(i) for i in range(32)}
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}}
_INTERNAL_SUFFIXES = (".part", ".part.json")      # 上传临时文件，不允许用户直接访问


def validate_rel_path(rel, allow_empty: bool = False) -> str:
    """校验并规范化客户端给的相对路径，返回用 / 连接的规范形式。
    在 safe_join 之前先做一轮"字面"检查：拒绝 ..、Windows 非法字符与保留名、内部临时文件名。
    这样即使服务端运行在 Windows 上，也不会被 CON、NUL 之类的设备名或 a.txt. 之类的名字绕过。"""
    if not isinstance(rel, str) or len(rel) > 1024 or "\x00" in rel:
        raise ValidationError("非法路径")
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts and not allow_empty:
        raise ValidationError("路径不能为空")
    for p in parts:
        if (p == ".." or len(p) > 200 or p.endswith((" ", "."))
                or any(c in _BAD_CHARS for c in p)
                or p.lower().endswith(_INTERNAL_SUFFIXES)
                or p.split(".")[0].upper() in _WIN_RESERVED):
            raise ValidationError("文件名或路径不合法")
    return "/".join(parts)