"""命令行客户端（联调与演示用）：登录后保持一条长连接，在交互 shell 里执行命令。

    python -m client.cli --user alice
    python -m client.cli --user alice --register        # 先注册再登录

shell 命令：
    ls [路径]            列目录            cd <目录> / pwd
    put <本地> [远程]    上传（断点续传）   get <远程> [本地]   下载（断点续传）
    drop                 模拟断网（会自动重连；传输中执行可看到自动续传）
    help / quit
Ctrl+C 可暂停正在进行的传输，重新执行同一条命令即可续传。
"""
import argparse
import getpass
import os
import posixpath
import shlex
import sys
import threading

from .errors import AuthError, Cancelled, ClientError, ConnectError, TransferError
from .net import FTPClient
from .transfer import download_file, upload_file


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def make_progress(label):
    state = {"last": -1}

    def cb(done, total):
        pct = 100 if total == 0 else int(done * 100 / total)
        if pct != state["last"]:
            state["last"] = pct
            sys.stdout.write(f"\r{label} {pct:3d}%  {human(done)}/{human(total)}   ")
            sys.stdout.flush()
    return cb


def run_transfer(fn, *args, label):
    """在工作线程里跑传输，主线程等待；Ctrl+C 触发取消（保留半成品）。"""
    cancel = threading.Event()
    result = {}

    def work():
        try:
            result["ok"] = fn(*args, progress=make_progress(label), cancel=cancel)
        except BaseException as e:      # noqa: BLE001  交给主线程展示
            result["err"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    try:
        while t.is_alive():
            t.join(0.2)
    except KeyboardInterrupt:
        cancel.set()
        t.join()
    print()
    if "err" in result:
        e = result["err"]
        if isinstance(e, Cancelled):
            print("已暂停，重新执行同一条命令即可续传")
        elif isinstance(e, (ClientError, OSError, TimeoutError)):
            print(f"失败：{e}")
        else:
            raise e
    else:
        r = result["ok"]
        extra = f"（从 {human(r['resumed_from'])} 处续传）" if r.get("resumed_from") else ""
        print(f"完成 {human(r['size'])}  sha256={r['sha256'][:16]}… {extra}")


def shell(client: FTPClient):
    cwd = ""            # 远程当前目录（相对用户根目录）

    def rpath(p):
        if not p:
            return cwd
        full = posixpath.normpath(posixpath.join(cwd, p.replace("\\", "/")))
        if full == ".":
            return ""
        if full == ".." or full.startswith("../"):
            raise ClientError("路径超出根目录")
        return full.lstrip("/")

    print("输入 help 查看命令，quit 退出")
    while True:
        try:
            line = input(f"[{client.username}@{client.state}] /{cwd}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        try:
            argv = shlex.split(line)
        except ValueError as e:
            print(f"命令解析失败：{e}")
            continue
        cmd, args = argv[0].lower(), argv[1:]
        try:
            if cmd in ("quit", "exit"):
                break
            elif cmd == "help":
                print(__doc__)
            elif cmd == "pwd":
                print("/" + cwd)
            elif cmd == "ls":
                entries = client.catalog(rpath(args[0]) if args else cwd)
                for e in entries:
                    print(f"{'d' if e['is_dir'] else '-'} {human(e['size']):>8}  {e['name']}")
                print(f"共 {len(entries)} 项")
            elif cmd == "cd":
                target = rpath(args[0]) if args else ""
                client.catalog(target)          # 目录不存在会抛 TransferError
                cwd = target
            elif cmd == "put" and args:
                local = args[0]
                remote = rpath(args[1]) if len(args) > 1 else rpath(os.path.basename(local))
                run_transfer(upload_file, client, local, remote, label=f"上传 {os.path.basename(local)}")
            elif cmd == "get" and args:
                remote = rpath(args[0])
                local = args[1] if len(args) > 1 else os.path.basename(remote)
                run_transfer(download_file, client, remote, local, label=f"下载 {os.path.basename(remote)}")
            elif cmd == "drop":
                client.drop_connection()
                print("已断开 TCP 连接，客户端将自动重连")
            else:
                print("未知命令，输入 help 查看")
        except (ClientError, OSError, TimeoutError) as e:
            print(f"错误：{e}")


def main():
    ap = argparse.ArgumentParser(description="安全文件传输命令行客户端")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9443)
    ap.add_argument("--ca", default="certs/ca-cert.pem", help="CA 证书（只信任它签发的服务端证书）")
    ap.add_argument("--cert", help="客户端证书（服务端开启双向认证时需要）")
    ap.add_argument("--key", help="客户端私钥")
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", help="不填则交互输入（避免出现在命令行历史里）")
    ap.add_argument("--register", action="store_true", help="先注册该用户再登录")
    args = ap.parse_args()

    password = args.password or getpass.getpass("密码: ")
    def on_state(state, detail):
        if state in ("disconnected", "reconnecting", "auth_failed") or "重连" in detail:
            print(f"\n[连接状态] {state} {detail}".rstrip())

    client = FTPClient(args.host, args.port, args.ca, args.cert, args.key, on_state=on_state)
    try:
        client.connect()
        if args.register:
            client.register(args.user, password)
            print("注册成功")
        client.login(args.user, password)
    except (ConnectError, AuthError) as e:
        print(f"失败：{e}")
        client.close()
        return 1
    print(f"已登录 {client.username}，长连接已建立（心跳 {client.heartbeat_interval} 秒）")
    try:
        shell(client)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())