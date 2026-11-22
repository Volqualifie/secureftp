"""传输历史的本地持久化。

每个"服务器 + 用户"一个 JSON 文件，保存在用户目录 ~/.secureftp/history/ 下（可用环境变量
SECUREFTP_DATA_DIR 改位置）。只保存任务的元数据（方向、远程路径、本地路径、大小、进度、状态），
不保存密码，也不保存文件内容。写入采用"临时文件 + 原子替换"，中途崩溃不会损坏旧记录。
"""
import hashlib
import json
import os
import re

MAX_RECORDS = 200
FIELDS = ("direction", "remote", "local", "total", "done", "state", "error", "resumed_from")


def default_history_path(server_label: str) -> str:
    base = os.environ.get("SECUREFTP_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".secureftp")
    safe = re.sub(r"[^A-Za-z0-9_.@-]", "_", server_label or "default")[:60]
    tag = hashlib.sha1((server_label or "").encode("utf-8")).hexdigest()[:8]
    return os.path.join(base, "history", f"{safe}-{tag}.json")


def load_records(path: str) -> list:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        recs = data["tasks"] if isinstance(data, dict) else []
    except (OSError, ValueError, KeyError):
        return []                                   # 文件不存在或已损坏：当作没有历史
    out = []
    for r in recs:
        if (isinstance(r, dict) and r.get("direction") in ("upload", "download")
                and isinstance(r.get("remote"), str) and isinstance(r.get("local"), str)):
            out.append(r)
    return out[-MAX_RECORDS:]


def save_records(path: str, records: list) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "tasks": records[-MAX_RECORDS:]}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass                                        # 历史记录写不了不应影响传输本身