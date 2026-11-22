"""传输任务与调度器。

任务状态机：
    WAITING ──调度──▶ RUNNING ──完成──▶ DONE
       ▲                │  ╲──失败──▶ FAILED ──继续──▶ WAITING
       │                │
     继续             暂停 ▶ PAUSING ──线程退出──▶ PAUSED ──继续──▶ WAITING
所有状态修改都发生在界面线程；工作线程只做传输，并通过信号汇报进度与结果。
"暂停"= 取消当前传输但保留半成品（服务端 .part / 本地 .part），"继续"= 重新排队并断点续传。
"""
import posixpath
import threading
import time

from PySide6.QtCore import QObject, Signal

from ..errors import Cancelled, ClientError
from ..transfer import download_file, upload_file

WAITING, RUNNING, PAUSING, PAUSED, DONE, FAILED = (
    "waiting", "running", "pausing", "paused", "done", "failed")
ACTIVE = (WAITING, RUNNING, PAUSING)


class TransferTask(QObject):
    progress_sig = Signal(object, object)           # (task, (done, total))
    finished_sig = Signal(object, object, object)   # (task, 结果类型, 载荷)

    def __init__(self, tid, direction, remote, local, total=0):
        super().__init__()
        self.id, self.direction = tid, direction            # "upload" | "download"
        self.remote, self.local = remote, local
        self.name = posixpath.basename(remote) or remote
        self.total, self.done, self.speed = total, 0, 0.0
        self.state, self.error, self.result = WAITING, "", None
        self.cancel = threading.Event()
        self.thread = None
        self.remove_after = False
        self._t = None              # 测速基线（界面线程使用）
        self._d = 0
        self._last_emit = 0.0       # 进度信号节流（工作线程使用）

    # ---- 工作线程 ----
    def _progress_cb(self, done, total):
        now = time.monotonic()
        if done >= total or now - self._last_emit >= 0.1:       # 最多约 10 次/秒，避免刷爆界面
            self._last_emit = now
            self.progress_sig.emit(self, (done, total))

    def run(self, client, cancel):
        self._last_emit = 0.0
        try:
            if self.direction == "upload":
                r = upload_file(client, self.local, self.remote,
                                progress=self._progress_cb, cancel=cancel)
            else:
                r = download_file(client, self.remote, self.local,
                                  progress=self._progress_cb, cancel=cancel)
            self.finished_sig.emit(self, "done", r)
        except Cancelled:
            self.finished_sig.emit(self, "cancelled", None)
        except (ClientError, OSError, TimeoutError) as e:
            self.finished_sig.emit(self, "failed", str(e) or type(e).__name__)
        except Exception as e:      # noqa: BLE001  未预料的错误也要让界面看到，不能让线程静默消失
            self.finished_sig.emit(self, "failed", f"内部错误：{e!r}")


class TransferManager(QObject):
    task_added = Signal(object)
    task_updated = Signal(object)
    task_removed = Signal(object)
    task_done = Signal(object)

    def __init__(self, client, max_parallel=3, parent=None):
        super().__init__(parent)
        self.client = client
        self.max_parallel = max_parallel        # 同时进行的传输数；其余排队（服务端单连接上限默认 4）
        self.tasks = []
        self._next_id = 1

    # ---- 添加 ----
    def _add(self, direction, remote, local, total):
        t = TransferTask(self._next_id, direction, remote, local, total)
        self._next_id += 1
        t.progress_sig.connect(self._on_progress)
        t.finished_sig.connect(self._on_finished)
        self.tasks.append(t)
        self.task_added.emit(t)
        self._schedule()
        return t

    def add_upload(self, local, remote):
        import os
        try:
            total = os.path.getsize(local)
        except OSError:
            total = 0
        return self._add("upload", remote, local, total)

    def add_download(self, remote, local, total=0):
        return self._add("download", remote, local, total)

    # ---- 历史记录 ----
    def restore(self, records):
        """把上次保存的记录恢复成任务。未完成的一律置为"已暂停"，由用户决定是否继续。"""
        for r in records:
            t = TransferTask(self._next_id, r["direction"], r["remote"], r["local"],
                             int(r.get("total") or 0))
            self._next_id += 1
            t.done = int(r.get("done") or 0)
            state = r.get("state")
            if state == DONE:
                t.state, t.result = DONE, {"size": t.total, "resumed_from": int(r.get("resumed_from") or 0)}
                t.done = t.total
            elif state == FAILED:
                t.state, t.error = FAILED, r.get("error") or "上次传输失败"
            else:
                t.state = PAUSED
            t.progress_sig.connect(self._on_progress)
            t.finished_sig.connect(self._on_finished)
            self.tasks.append(t)
            self.task_added.emit(t)

    def to_records(self):
        out = []
        for t in self.tasks:
            state = t.state if t.state in (DONE, FAILED) else PAUSED
            out.append({"direction": t.direction, "remote": t.remote, "local": t.local,
                        "total": t.total, "done": t.done, "state": state, "error": t.error,
                        "resumed_from": int((t.result or {}).get("resumed_from", 0))})
        return out

    # ---- 控制 ----
    def pause(self, t):
        if t.state == WAITING:
            t.state = PAUSED
        elif t.state == RUNNING:
            t.state = PAUSING
            t.cancel.set()
        else:
            return
        self.task_updated.emit(t)
        self._schedule()

    def resume(self, t):
        if t.state in (PAUSED, FAILED):
            t.state, t.error = WAITING, ""
            self.task_updated.emit(t)
            self._schedule()

    def remove(self, t):
        if t.state in (RUNNING, PAUSING):
            t.remove_after = True               # 等工作线程退出后再移除
            t.state = PAUSING
            t.cancel.set()
            self.task_updated.emit(t)
        else:
            self._remove_now(t)

    def pause_all(self):
        for t in list(self.tasks):
            self.pause(t)

    def clear_finished(self):
        for t in [t for t in self.tasks if t.state == DONE]:
            self._remove_now(t)

    def active_count(self):
        return sum(1 for t in self.tasks if t.state in ACTIVE)

    def shutdown(self, timeout=5.0):
        """退出前暂停全部传输并等待线程结束，半成品保留，下次可续传。"""
        self.pause_all()
        end = time.monotonic() + timeout
        for t in self.tasks:
            if t.thread is not None:
                t.thread.join(max(0.0, end - time.monotonic()))

    def _remove_now(self, t):
        if t in self.tasks:
            self.tasks.remove(t)
            self.task_removed.emit(t)

    # ---- 调度 ----
    def _schedule(self):
        running = sum(1 for t in self.tasks if t.state in (RUNNING, PAUSING))
        for t in self.tasks:
            if running >= self.max_parallel:
                break
            if t.state == WAITING:
                self._start(t)
                running += 1

    def _start(self, t):
        t.state = RUNNING
        t.cancel = threading.Event()
        t.speed, t._t, t._d = 0.0, None, 0
        t.thread = threading.Thread(target=t.run, args=(self.client, t.cancel),
                                    name=f"transfer-{t.id}", daemon=True)
        t.thread.start()
        self.task_updated.emit(t)

    # ---- 工作线程回调（经信号排队到界面线程）----
    def _on_progress(self, t, pair):
        if t.state not in (RUNNING, PAUSING):
            return
        done, total = pair
        now = time.monotonic()
        if t._t is None:
            t._t, t._d, t.speed = now, done, 0.0
        else:
            dt = now - t._t
            if dt >= 0.25 or done >= total:
                inst = (done - t._d) / dt if dt > 0 else 0.0
                t.speed = inst if t.speed == 0 else 0.6 * t.speed + 0.4 * inst   # 平滑
                t._t, t._d = now, done
        t.done, t.total = done, total
        self.task_updated.emit(t)

    def _on_finished(self, t, outcome, payload):
        t.thread = None
        t.speed = 0.0
        if outcome == "done":
            t.state, t.result = DONE, payload
            t.total = t.done = payload["size"]
            self.task_updated.emit(t)
            self.task_done.emit(t)
        elif outcome == "cancelled":
            if t.remove_after:
                self._remove_now(t)
            else:
                t.state = PAUSED
                self.task_updated.emit(t)
        else:
            t.state, t.error = FAILED, payload
            self.task_updated.emit(t)
            if t.remove_after:
                self._remove_now(t)
        self._schedule()