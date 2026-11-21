
"""存储层：全部是【阻塞式】磁盘操作，调用方必须用 asyncio.to_thread 包起来，
避免卡住事件循环。

上传采用"先写临时文件、校验通过再原子改名"：
    <target>.part       已接收的数据（断点续传依赖它）
    <target>.part.json  记录这次上传声明的 size 与 sha256，只有两者一致才允许续传
校验通过 -> os.replace(.part, target)；校验失败 -> 删除 .part，不会留下损坏的文件。
"""
import hashlib
import json
import os

PART_SUFFIXES = (".part", ".part.json")


def ensure_user_dir(root: str, username: str) -> str:
    path = os.path.join(root, username)
    os.makedirs(path, exist_ok=True)
    return path


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def list_dir(path: str, limit: int = 5000):
    """列目录。隐藏上传临时文件和符号链接；目录在前，按名称排序。"""
    entries = []
    with os.scandir(path) as it:
        for e in it:
            if e.name.lower().endswith(PART_SUFFIXES) or e.is_symlink():
                continue
            try:
                st = e.stat(follow_symlinks=False)
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            entries.append({"name": e.name, "is_dir": is_dir,
                            "size": 0 if is_dir else st.st_size,
                            "mtime": int(st.st_mtime)})
            if len(entries) >= limit:
                break
    entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return entries


def prepare_upload(target: str, size: int, sha256: str, resume: bool):
    """创建/续接 .part 文件，返回 (文件句柄, 已有字节数 offset)。
    仅当 resume=True 且旧的 .part 对应同一个文件（size 与 sha256 一致）时才续传。"""
    os.makedirs(os.path.dirname(target), exist_ok=True)
    part, meta = target + ".part", target + ".part.json"
    offset = 0
    if resume and os.path.isfile(part) and os.path.isfile(meta):
        try:
            with open(meta, encoding="utf-8") as f:
                m = json.load(f)
            if m.get("size") == size and m.get("sha256") == sha256:
                psize = os.path.getsize(part)
                if psize <= size:
                    offset = psize
        except (OSError, ValueError):
            offset = 0
    with open(meta, "w", encoding="utf-8") as f:
        json.dump({"size": size, "sha256": sha256}, f)
    if not os.path.exists(part):
        open(part, "ab").close()
    fh = open(part, "r+b")
    fh.seek(offset)
    fh.truncate()               # 丢弃 offset 之后可能残留的数据
    return fh, offset


def finalize_upload(fh, target: str, expect_sha256: str):
    """关闭 .part、校验哈希、原子改名。返回 (是否成功, 实际哈希)。"""
    part, meta = target + ".part", target + ".part.json"
    fh.flush()
    os.fsync(fh.fileno())
    fh.close()
    actual = sha256_file(part)
    if actual != expect_sha256:
        discard_upload(target)
        return False, actual
    os.replace(part, target)
    try:
        os.remove(meta)
    except OSError:
        pass
    return True, actual


def discard_upload(target: str) -> None:
    for p in (target + ".part", target + ".part.json"):
        try:
            os.remove(p)
        except OSError:
            pass