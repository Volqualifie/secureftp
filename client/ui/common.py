"""界面公共工具：后台执行（不阻塞界面）与格式化。

线程规则：网络、磁盘操作一律在后台线程做；后台线程只通过 Qt 信号把结果交回界面线程，
绝不直接操作任何控件。
"""
import threading
import time

from PySide6.QtCore import QObject, Signal


def human_size(n) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_speed(bps: float) -> str:
    return "" if bps <= 0 else human_size(bps) + "/s"


def human_time(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


class Call(QObject):
    """在后台线程执行 fn()，完成后回到创建它的（界面）线程调用 on_ok(结果) 或 on_err(异常)。"""

    _finished = Signal(object, object)
    _alive = set()              # 持有引用，防止后台线程运行期间对象被回收

    def __init__(self, fn, on_ok=None, on_err=None):
        super().__init__()
        self._fn, self._ok, self._err = fn, on_ok, on_err
        self._finished.connect(self._deliver)       # 连接到本对象的方法 -> 跨线程时自动排队到界面线程
        Call._alive.add(self)

    def start(self):
        threading.Thread(target=self._run, name="ui-call", daemon=True).start()

    def _run(self):
        try:
            self._finished.emit(self._fn(), None)
        except BaseException as e:      # noqa: BLE001  交给界面线程处理
            self._finished.emit(None, e)

    def _deliver(self, result, error):
        Call._alive.discard(self)
        try:
            if error is None:
                if self._ok:
                    self._ok(result)
            elif self._err:
                self._err(error)
        finally:
            self.deleteLater()


def run_in_background(fn, on_ok=None, on_err=None) -> None:
    Call(fn, on_ok, on_err).start()