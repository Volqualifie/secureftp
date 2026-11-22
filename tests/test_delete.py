"""删除功能（服务端协议 + 安全）与默认限速配置。"""
import os

import pytest
import yaml

from client.errors import TransferError
from client.transfer import upload_file
from protocol.constants import ErrorCode, PacketType
from server.config import load_config
from test_client import env, rnd  # noqa: F401
from test_server_auth import certs  # noqa: F401


def test_delete_file_and_folder(env):
    c = env.client(env.server())
    upload_file(c, env.local("a.bin", rnd(1000, 1)), "d/a.bin")
    upload_file(c, env.local("b.bin", rnd(1000, 2)), "d/sub/b.bin")
    upload_file(c, env.local("c.bin", rnd(1000, 3)), "c.bin")
    assert c.delete("c.bin") == 1
    assert not os.path.exists(env.remote_path("c.bin"))
    assert c.delete("d") == 2                                   # 文件夹连同内容
    assert not os.path.exists(env.remote_path("d"))
    assert c.catalog() == []
    assert any(l[1] == "delete" for l in env.db.logs)           # 记了操作日志


def test_delete_errors(env):
    c = env.client(env.server())
    with pytest.raises(TransferError) as e:
        c.delete("nothing.txt")
    assert e.value.code == ErrorCode.NOT_FOUND
    for bad in ("", "/", "..", "../x", "a/../../x", "x.part", "x.part.json"):
        with pytest.raises(TransferError) as e:
            c.delete(bad)
        assert e.value.code in (ErrorCode.FORBIDDEN, ErrorCode.BAD_REQUEST), bad
    assert os.path.isdir(os.path.join(env.storage, "alice"))     # 用户根目录还在


def test_cannot_delete_other_users_files_or_unauthenticated(env):
    srv = env.server()
    a = env.client(srv, name="alice")
    upload_file(a, env.local("secret.txt", b"secret"), "secret.txt")
    b = env.client(srv, name="bobby")
    with pytest.raises(TransferError):
        b.delete("../alice/secret.txt")                          # 路径穿越
    with pytest.raises(TransferError) as e:
        b.delete("secret.txt")                                   # 只能删自己目录里的同名文件，而它不存在
    assert e.value.code == ErrorCode.NOT_FOUND
    assert os.path.exists(env.remote_path("secret.txt"))
    anon = env.client(srv, login=False)
    r = anon.request(PacketType.DELETE_REQ, {"path": "secret.txt"})
    assert not r.meta["ok"] and os.path.exists(env.remote_path("secret.txt"))


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlink")
def test_delete_symlink_does_not_touch_target(env, tmp_path):
    srv = env.server()
    c = env.client(srv)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")
    link = env.remote_path("link.txt")
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("no permission to create symlink")
    with pytest.raises(TransferError):                           # 链接指向目录外：拒绝
        c.delete("link.txt")
    assert outside.read_text() == "keep me"


def test_default_limits_when_config_omits_them(tmp_path):
    d = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "server", "config.docker.yaml"),
                            encoding="utf-8"))
    for k in ("bandwidth_normal", "bandwidth_vip"):
        d["server"].pop(k)
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(d), encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.bandwidth_normal == 5 * 1024 * 1024 and cfg.bandwidth_vip == 0
    d["server"]["bandwidth_normal"] = -1
    p.write_text(yaml.safe_dump(d), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(str(p))