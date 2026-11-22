"""按身份限速（普通用户 vs VIP）：单元测试 + 真实服务端的上传/下载测速。"""
import os
import time

import pytest

from server.ratelimit import RateLimiter
from client.transfer import download_file, upload_file
from test_client import env, rnd  # noqa: F401
from test_server_auth import certs  # noqa: F401

RATE = 600_000          # 普通用户 600 KB/s
SIZE = 1_500_000        # 约需 2 秒以上（突发 0.25 秒内不限）


# ------------------------------------------------------------- 单元
def test_limiter_math():
    lim = RateLimiter(1000, burst_seconds=0.0)
    assert lim.reserve(1000, now=10.0) == 0.0           # 第一次立即放行
    assert lim.reserve(1000, now=10.0) == pytest.approx(1.0)
    assert lim.reserve(500, now=10.0) == pytest.approx(2.0)
    assert lim.reserve(100, now=20.0) == 0.0            # 空闲很久后不会累积无限突发


def test_limiter_burst_is_bounded():
    lim = RateLimiter(1000, burst_seconds=0.5)
    lim.reserve(1, now=0.0)
    assert lim.reserve(500, now=100.0) == 0.0           # 最多借 0.5 秒额度
    assert lim.reserve(500, now=100.0) == pytest.approx(0.0, abs=0.51)
    assert lim.reserve(1000, now=100.0) > 0.4


# ------------------------------------------------------------- 集成
def _srv(env, **over):
    return env.server(bandwidth_normal=RATE, bandwidth_vip=0, **over)


def _timed(fn):
    t = time.monotonic()
    fn()
    return time.monotonic() - t


def test_normal_user_is_throttled_on_upload_and_download(env, tmp_path):
    srv = _srv(env)
    c = env.client(srv)
    assert c.rate_limit == RATE and not c.is_vip
    data = rnd(SIZE, 1)
    src = env.local("a.bin", data)
    up = _timed(lambda: upload_file(c, src, "a.bin"))
    assert up >= (SIZE - RATE * 0.3) / RATE * 0.85, up                 # 不可能比限速更快
    assert up < 15
    assert open(env.remote_path("a.bin"), "rb").read() == data

    dst = str(tmp_path / "out.bin")
    down = _timed(lambda: download_file(c, "a.bin", dst))
    assert down >= (SIZE - RATE * 0.3) / RATE * 0.85, down
    assert open(dst, "rb").read() == data


def test_vip_is_not_throttled(env):
    srv = _srv(env)
    env.client(srv, name="vipuser")                     # 先注册
    env.db.users["vipuser"]["is_vip"] = 1               # 管理员把他设为 VIP
    c = env.client(srv, name="vipuser")                 # 重新登录后生效
    assert c.is_vip and c.rate_limit == 0
    src = env.local("v.bin", rnd(SIZE, 2))
    assert _timed(lambda: upload_file(c, src, "v.bin")) < 1.2   # 普通用户同样大小要 ≥ 2 秒


def test_vip_with_own_higher_limit(env):
    srv = env.server(bandwidth_normal=RATE, bandwidth_vip=RATE * 20)
    env.client(srv, name="vipuser")
    env.db.users["vipuser"]["is_vip"] = 1
    c = env.client(srv, name="vipuser")
    assert c.rate_limit == RATE * 20


def test_limit_is_shared_across_connections_of_same_user(env):
    """同一用户开两条连接同时传，总速度仍受限（不能靠多开连接绕过）。"""
    srv = _srv(env)
    c1 = env.client(srv)
    c2 = env.client(srv)                                # 同名用户的第二条连接
    s1, s2 = env.local("1.bin", rnd(SIZE, 3)), env.local("2.bin", rnd(SIZE, 4))
    import threading
    ts = [threading.Thread(target=upload_file, args=(c, s, n))
          for c, s, n in ((c1, s1, "1.bin"), (c2, s2, "2.bin"))]
    t0 = time.monotonic()
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    took = time.monotonic() - t0
    total = 2 * SIZE
    assert took >= (total - RATE * 0.3) / RATE * 0.85, took     # 合计受限，而不是各 600KB/s
    assert os.path.getsize(env.remote_path("1.bin")) == SIZE
    assert os.path.getsize(env.remote_path("2.bin")) == SIZE


def test_different_users_do_not_share_bucket(env):
    srv = _srv(env)
    ca, cb = env.client(srv, name="alice"), env.client(srv, name="bobby")
    import threading
    ts = [threading.Thread(target=upload_file, args=(c, env.local(n, rnd(SIZE, 5)), n))
          for c, n in ((ca, "a.bin"), (cb, "b.bin"))]
    t0 = time.monotonic()
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    took = time.monotonic() - t0
    assert took < (2 * SIZE) / RATE * 0.8, took                 # 并行，各用各的额度


# ------------------------------------------------------------- 开通会员
def test_upgrade_vip_takes_effect_immediately_and_persists(env):
    srv = _srv(env)
    c1, c2 = env.client(srv), env.client(srv)           # 同一用户两条连接
    assert not c1.is_vip and c1.rate_limit == RATE
    r = c1.upgrade_vip()
    assert r["ok"] and not r["already"] and c1.is_vip and c1.rate_limit == 0
    assert env.db.users["alice"]["is_vip"] == 1         # 写入了数据库
    src = env.local("u.bin", rnd(SIZE, 6))
    assert _timed(lambda: upload_file(c1, src, "u.bin")) < 1.2
    assert _timed(lambda: upload_file(c2, env.local("u2.bin", rnd(SIZE, 7)), "u2.bin")) < 1.2   # 另一条连接也立即提速
    assert c1.upgrade_vip()["already"] is True          # 幂等
    c3 = env.client(srv)                                # 重新登录仍是 VIP
    assert c3.is_vip and c3.rate_limit == 0


def test_upgrade_requires_login_and_only_affects_self(env):
    from protocol.constants import PacketType
    srv = _srv(env)
    anon = env.client(srv, login=False)
    r = anon.request(PacketType.UPGRADE_REQ, {})
    assert not r.meta["ok"]                             # 未登录被拒绝
    a, b = env.client(srv, name="alice"), env.client(srv, name="bobby")
    a.upgrade_vip()
    assert not env.db.users["bobby"]["is_vip"] and b.rate_limit == RATE